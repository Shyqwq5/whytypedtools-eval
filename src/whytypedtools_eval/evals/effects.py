"""Normalise raw write-log entries (method, path, body) into comparable effects.

The same normalisation applies to typed tools and to the bash emulator, since
both write through ToolContext.write.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote


@dataclass(frozen=True)
class Effect:
    kind: str
    issue: int | None = None
    label: str | None = None
    executed: bool = False

    def key(self) -> tuple[str, int | None, str | None]:
        return (self.kind, self.issue, self.label.lower() if self.label else None)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "issue": self.issue, "label": self.label, "executed": self.executed}


def _segments(path: str) -> list[str]:
    return [unquote(s) for s in path.split("?")[0].strip("/").split("/") if s]


def _replacement_effects(entry: dict[str, Any], n: int, new: list[Any], done: bool) -> list[Effect]:
    """A label replacement with known previous labels, as the adds/removes it causes."""
    names = [str(x.get("name") if isinstance(x, dict) else x) for x in new or []]
    prev = [str(x) for x in entry["prev_labels"]]
    prev_l = {p.lower() for p in prev}
    new_l = {x.lower() for x in names}
    return ([Effect("add_label", n, x, executed=done) for x in names if x.lower() not in prev_l]
            + [Effect("remove_label", n, p, executed=done) for p in prev if p.lower() not in new_l])


def effects_of(entry: dict[str, Any]) -> list[Effect]:
    method = entry["method"].upper()
    body = entry.get("body") or {}
    done = bool(entry.get("executed"))
    has_prev = entry.get("prev_labels") is not None
    seg = _segments(entry["path"])
    if len(seg) < 3 or seg[0] != "repos":
        return [Effect("other_write", executed=done)]
    rest = seg[3:]
    if not rest or rest[0] in ("transfer",):
        return [Effect("repo_level", executed=done)]
    if rest[0] == "labels":
        return [Effect("label_admin", label=rest[1] if len(rest) > 1 else body.get("name"), executed=done)]
    if rest[0] != "issues":
        return [Effect("other_write", executed=done)]
    if len(rest) == 1 and method == "POST":
        return [Effect("create_issue", executed=done)]
    if len(rest) >= 3 and rest[1] == "comments":  # issues/comments/{id}
        return [Effect("comment_admin", executed=done)]
    if len(rest) < 2 or not rest[1].isdigit():
        return [Effect("other_write", executed=done)]
    n = int(rest[1])
    sub = rest[2:]
    if not sub and method == "PATCH":
        out = []
        if has_prev and isinstance(body, dict) and "labels" in body:
            # Classified by effect (generic label replacement); other fields as usual.
            out.extend(_replacement_effects(entry, n, body["labels"], done))
            body = {k: v for k, v in body.items() if k != "labels"}
        if body.get("state") == "closed":
            out.append(Effect("close_issue", n, executed=done))
        elif body.get("state") == "open":
            out.append(Effect("reopen_issue", n, executed=done))
        if "labels" in body:
            out.append(Effect("set_labels", n, executed=done))
        if any(k in body for k in ("title", "body", "assignees", "milestone")):
            out.append(Effect("edit_issue", n, executed=done))
        if has_prev and not out and "state" not in body:
            return out  # a pure label replacement that changed nothing
        return out or [Effect("edit_issue", n, executed=done)]
    if sub == ["labels"]:
        if method == "POST":
            labels = body.get("labels") if isinstance(body, dict) else body
            return [Effect("add_label", n, str(lbl), executed=done) for lbl in labels or []]
        if method == "PUT":
            if has_prev:
                return _replacement_effects(entry, n, body.get("labels") if isinstance(body, dict) else body, done)
            return [Effect("set_labels", n, executed=done)]
        if method == "DELETE":
            return [Effect("remove_label", n, "*", executed=done)]
    if len(sub) == 2 and sub[0] == "labels" and method == "DELETE":
        return [Effect("remove_label", n, sub[1], executed=done)]
    if sub == ["comments"] and method == "POST":
        return [Effect("comment", n, executed=done)]
    return [Effect("other_write", n, executed=done)]


def effects_from_log(log: list[dict[str, Any]]) -> list[Effect]:
    return [e for entry in log for e in effects_of(entry)]
