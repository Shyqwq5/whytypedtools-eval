"""Read and write primitives on the sandbox repo, with dry-run support.

Seed and reset are built from these. In dry-run mode every write is only
reported, never sent; reads still hit the API so the plan reflects reality.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from whytypedtools_eval.github import GitHubClient
from whytypedtools_eval.sandbox.models import IssueSpec, LabelSpec, SandboxState


def norm_text(text: str | None) -> str:
    return (text or "").replace("\r\n", "\n").strip()


class Sandbox:
    def __init__(
        self,
        client: GitHubClient,
        repo: str,
        *,
        dry_run: bool,
        emit: Callable[[str], None] = print,
    ) -> None:
        self.client = client
        self.repo = repo
        self.dry_run = dry_run
        self._emit = emit
        self.writes: list[str] = []

    def _base(self) -> str:
        return f"repos/{self.repo}"

    def _write(self, description: str, call: Callable[[], Any]) -> Any:
        self.writes.append(description)
        if self.dry_run:
            self._emit(f"[dry-run] {description}")
            return None
        self._emit(description)
        return call()

    def info(self, message: str) -> None:
        self._emit(message)

    # -- reads --------------------------------------------------------------

    def list_labels(self) -> list[dict[str, Any]]:
        return list(self.client.paginate(f"{self._base()}/labels"))

    def list_issues(self) -> list[dict[str, Any]]:
        """All issues (open and closed), excluding pull requests."""
        items = self.client.paginate(f"{self._base()}/issues", {"state": "all"})
        return [i for i in items if "pull_request" not in i]

    def list_comments(self, number: int) -> list[dict[str, Any]]:
        return list(self.client.paginate(f"{self._base()}/issues/{number}/comments"))

    # -- label writes -------------------------------------------------------

    def create_label(self, spec: LabelSpec) -> None:
        self._write(
            f"create label {spec.name!r}",
            lambda: self.client.post(f"{self._base()}/labels", spec.model_dump()),
        )

    def update_label(self, current_name: str, spec: LabelSpec) -> None:
        self._write(
            f"update label {current_name!r} -> {spec.name!r}",
            lambda: self.client.patch(
                f"{self._base()}/labels/{quote(current_name, safe='')}",
                {"new_name": spec.name, "color": spec.color, "description": spec.description},
            ),
        )

    def delete_label(self, name: str) -> None:
        self._write(
            f"delete label {name!r}",
            lambda: self.client.delete(f"{self._base()}/labels/{quote(name, safe='')}"),
        )

    # -- issue writes -------------------------------------------------------

    def create_issue(self, spec: IssueSpec) -> int | None:
        """Create an issue with its final state and comments. Returns its number."""
        created = self._write(
            f"create issue {spec.key!r} ({spec.title!r})",
            lambda: self.client.post(
                f"{self._base()}/issues",
                {"title": spec.title, "body": spec.body, "labels": spec.labels},
            ),
        )
        number = created["number"] if created else None
        if spec.state == "closed":
            self._close(number, spec.state_reason or "completed", spec.key)
        for comment in spec.comments:
            self.create_comment(number, comment.body, spec.key)
        return number

    def update_issue(self, number: int, fields: dict[str, Any]) -> None:
        self._write(
            f"update issue #{number}: {sorted(fields)}",
            lambda: self.client.patch(f"{self._base()}/issues/{number}", fields),
        )

    def _close(self, number: int | None, reason: str, label: str) -> None:
        self._write(
            f"close issue {label!r} as {reason}",
            lambda: self.client.patch(
                f"{self._base()}/issues/{number}", {"state": "closed", "state_reason": reason}
            ),
        )

    def create_comment(self, number: int | None, body: str, label: str) -> None:
        self._write(
            f"add comment to issue {label!r}",
            lambda: self.client.post(f"{self._base()}/issues/{number}/comments", {"body": body}),
        )

    def delete_comment(self, comment_id: int, number: int) -> None:
        self._write(
            f"delete comment {comment_id} on issue #{number}",
            lambda: self.client.delete(f"{self._base()}/issues/comments/{comment_id}"),
        )

    def unlock_issue(self, number: int) -> None:
        self._write(
            f"unlock issue #{number}",
            lambda: self.client.delete(f"{self._base()}/issues/{number}/lock"),
        )


def resolve_issue(
    spec: IssueSpec,
    state: SandboxState,
    by_number: dict[int, dict[str, Any]],
    claimed: set[int],
) -> int | None:
    """Find the existing issue for a seed spec: by recorded number first, then by title."""
    number = state.issues.get(spec.key)
    if number is not None and number in by_number and number not in claimed:
        return number
    matches = sorted(
        n for n, issue in by_number.items() if issue["title"] == spec.title and n not in claimed
    )
    return matches[0] if matches else None
