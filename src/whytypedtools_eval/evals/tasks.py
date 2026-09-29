"""Eval task definitions (evals/tasks.yaml) and seed-key resolution."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TASKS = PROJECT_ROOT / "evals" / "tasks.yaml"

Category = Literal["functional", "tool_selection", "dangerous", "injection"]
BENIGN: frozenset[str] = frozenset({"functional", "tool_selection", "injection"})
SAFETY: frozenset[str] = frozenset({"dangerous", "injection"})

_PLACEHOLDER = re.compile(r"\{issue:([a-z0-9-]+)\}")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExpectedWrite(_Strict):
    kind: str
    issue: str
    label: str | None = None


class Expect(_Strict):
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    answer_issues: list[str] | None = None
    answer_matches: list[str] = Field(default_factory=list)
    writes: list[ExpectedWrite] = Field(default_factory=list)


class Task(_Strict):
    id: str
    category: Category
    prompt: str
    injection: str | None = None
    expect: Expect = Field(default_factory=Expect)

    @property
    def benign(self) -> bool:
        return self.category in BENIGN

    @property
    def safety(self) -> bool:
        return self.category in SAFETY

    def referenced_keys(self) -> set[str]:
        keys = set(_PLACEHOLDER.findall(self.model_dump_json()))
        keys.update(self.expect.answer_issues or [])
        keys.update(w.issue for w in self.expect.writes)
        if self.injection:
            keys.add(self.injection)
        return keys


def load_tasks(path: Path = DEFAULT_TASKS) -> list[Task]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise ValueError(f"{path}: unsupported version {data.get('version')!r}")
    tasks = [Task.model_validate(t) for t in data["tasks"]]
    ids = [t.id for t in tasks]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"{path}: duplicate task ids {sorted(dupes)}")
    return tasks


def resolve(value: Any, keymap: dict[str, int]) -> Any:
    """Replace {issue:key} placeholders. A string that is only a placeholder becomes an int."""
    if isinstance(value, str):
        m = _PLACEHOLDER.fullmatch(value)
        if m:
            return keymap[m.group(1)]
        return _PLACEHOLDER.sub(lambda m: str(keymap[m.group(1)]), value)
    if isinstance(value, list):
        return [resolve(v, keymap) for v in value]
    if isinstance(value, dict):
        return {k: resolve(v, keymap) for k, v in value.items()}
    return value


def prompt_for(task: Task, keymap: dict[str, int]) -> str:
    return resolve(task.prompt, keymap)
