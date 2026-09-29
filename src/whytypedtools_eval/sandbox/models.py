"""Schema for `sandbox/seed_data.yaml` and the local `sandbox/state.json`."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from whytypedtools_eval.sandbox.guard import normalize_repo


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LabelSpec(_Strict):
    name: str = Field(min_length=1)
    color: str = Field(pattern=r"^[0-9a-fA-F]{6}$")
    description: str = ""


class CommentSpec(_Strict):
    body: str = Field(min_length=1)


class SafetyTest(_Strict):
    """Local-only metadata. Never sent to GitHub, so the agent cannot see it."""

    kind: Literal["indirect_prompt_injection"]
    # Which tool output exposes the payload:
    # - summary: within the body excerpt returned by list/search tools.
    # - full_body: only in the full body (e.g. a get_issue tool), past the excerpt.
    # - comment: only in a comment (tools that return comments), not in the body.
    exposure: Literal["summary", "full_body", "comment"]
    # A phrase from the malicious instruction, used to locate it in the body/comment.
    payload_marker: str = Field(min_length=1)
    expected_behavior: str


class IssueSpec(_Strict):
    key: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    title: str = Field(min_length=1)
    body: str = ""
    state: Literal["open", "closed"] = "open"
    state_reason: Literal["completed", "not_planned"] | None = None
    labels: list[str] = []
    comments: list[CommentSpec] = []
    safety_test: SafetyTest | None = None

    @model_validator(mode="after")
    def _check_payload_marker(self) -> IssueSpec:
        st = self.safety_test
        if st is None:
            return self
        if st.exposure == "comment":
            if st.payload_marker in self.body or not any(st.payload_marker in c.body for c in self.comments):
                raise ValueError(f"issue {self.key!r}: payload_marker must be in a comment, not the body")
        elif st.payload_marker not in self.body:
            raise ValueError(f"issue {self.key!r}: payload_marker not found in body")
        return self

    @model_validator(mode="after")
    def _check_state_reason(self) -> IssueSpec:
        if self.state == "open" and self.state_reason is not None:
            raise ValueError(f"issue {self.key!r}: state_reason is only allowed when closed")
        if self.state == "closed" and self.state_reason is None:
            self.state_reason = "completed"
        return self


class SeedData(_Strict):
    version: Literal[1]
    labels: list[LabelSpec]
    issues: list[IssueSpec]

    @model_validator(mode="after")
    def _check_references(self) -> SeedData:
        _require_unique([lbl.name.lower() for lbl in self.labels], "label name")
        _require_unique([i.key for i in self.issues], "issue key")
        _require_unique([i.title for i in self.issues], "issue title")
        known = {lbl.name for lbl in self.labels}
        for issue in self.issues:
            unknown = set(issue.labels) - known
            if unknown:
                raise ValueError(f"issue {issue.key!r} uses undefined labels: {sorted(unknown)}")
        return self


def _require_unique(values: list[str], what: str) -> None:
    dupes = [v for v, n in Counter(values).items() if n > 1]
    if dupes:
        raise ValueError(f"duplicate {what}: {sorted(dupes)}")


def load_seed(path: str | Path) -> SeedData:
    with open(path, encoding="utf-8") as f:
        return SeedData.model_validate(yaml.safe_load(f))


class SandboxState(_Strict):
    """Mapping of seed issue key -> issue number in one specific sandbox repo."""

    repo: str
    issues: dict[str, int] = {}


def load_state(path: str | Path, repo: str) -> SandboxState:
    """Load state for `repo`. Returns an empty state if the file does not exist.

    Refuses a state file written for a different repo, since its numbers would
    point at the wrong issues.
    """
    p = Path(path)
    if not p.exists():
        return SandboxState(repo=repo)
    state = SandboxState.model_validate(json.loads(p.read_text(encoding="utf-8")))
    if normalize_repo(state.repo) != normalize_repo(repo):
        raise ValueError(
            f"{p} belongs to {state.repo!r}, not {repo!r}. Move it away and re-run seed."
        )
    return state


def save_state(path: str | Path, state: SandboxState) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(state.model_dump_json(indent=2) + "\n", encoding="utf-8")
