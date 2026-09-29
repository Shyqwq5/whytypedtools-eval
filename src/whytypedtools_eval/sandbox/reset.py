"""Restore the sandbox to exactly the seed state.

- Seed labels are recreated or corrected; any other label is deleted.
- Seed issues (found by number from state.json, title as fallback) get their
  title, body, state and labels restored, assignees and milestone cleared, and
  are unlocked; comments not in the seed are deleted and missing seed comments
  are re-added. Missing seed issues are recreated.
- Non-seed issues cannot be deleted via REST, so they are closed as
  not_planned with all labels removed and their title prefixed with
  ARTIFACT_PREFIX, so they are easy to tell apart from seed issues.
- The repository itself is never deleted or modified.
"""

from __future__ import annotations

from typing import Any

from whytypedtools_eval.sandbox.models import IssueSpec, SandboxState, SeedData
from whytypedtools_eval.sandbox.ops import Sandbox, norm_text, resolve_issue

ARTIFACT_PREFIX = "[eval-artifact] "


def reset(sb: Sandbox, data: SeedData, state: SandboxState) -> SandboxState:
    _reset_labels(sb, data)

    by_number = {issue["number"]: issue for issue in sb.list_issues()}
    claimed: set[int] = set()
    for spec in data.issues:
        number = resolve_issue(spec, state, by_number, claimed)
        if number is None:
            number = sb.create_issue(spec)
            if number is not None:
                claimed.add(number)
                state.issues[spec.key] = number
            continue
        claimed.add(number)
        state.issues[spec.key] = number
        fields = issue_diff(spec, by_number[number])
        if fields:
            sb.update_issue(number, fields)
        if by_number[number].get("locked"):
            sb.unlock_issue(number)
        _reset_comments(sb, number, spec)

    for number, issue in sorted(by_number.items()):
        if number in claimed:
            continue
        patch: dict[str, Any] = {}
        if not issue["title"].startswith(ARTIFACT_PREFIX):
            patch["title"] = ARTIFACT_PREFIX + issue["title"]
        if issue["state"] != "closed" or issue.get("state_reason") != "not_planned":
            patch.update(state="closed", state_reason="not_planned")
        if issue.get("labels"):
            patch["labels"] = []
        if patch:
            sb.update_issue(number, patch)

    # Drop stale keys: issues removed from the seed, or not created (dry-run).
    seed_keys = {spec.key for spec in data.issues}
    state.issues = {k: v for k, v in state.issues.items() if k in seed_keys and v in claimed}
    return state


def _reset_labels(sb: Sandbox, data: SeedData) -> None:
    current = {label["name"].lower(): label for label in sb.list_labels()}
    wanted = {spec.name.lower() for spec in data.labels}
    for spec in data.labels:
        cur = current.get(spec.name.lower())
        if cur is None:
            sb.create_label(spec)
        elif (
            cur["name"] != spec.name
            or cur["color"].lower() != spec.color.lower()
            or (cur.get("description") or "") != spec.description
        ):
            sb.update_label(cur["name"], spec)
    for name, cur in current.items():
        if name not in wanted:
            sb.delete_label(cur["name"])


def issue_diff(spec: IssueSpec, current: dict[str, Any]) -> dict[str, Any]:
    """Fields to PATCH so that `current` matches `spec`."""
    fields: dict[str, Any] = {}
    if current["title"] != spec.title:
        fields["title"] = spec.title
    if norm_text(current.get("body")) != norm_text(spec.body):
        fields["body"] = spec.body
    current_labels = {label["name"].lower() for label in current.get("labels", [])}
    if current_labels != {name.lower() for name in spec.labels}:
        fields["labels"] = spec.labels
    # Seed issues never have assignees or a milestone.
    if current.get("assignees"):
        fields["assignees"] = []
    if current.get("milestone") is not None:
        fields["milestone"] = None
    if spec.state == "open":
        if current["state"] != "open":
            fields["state"] = "open"
    elif current["state"] != "closed" or current.get("state_reason") != spec.state_reason:
        fields.update(state="closed", state_reason=spec.state_reason)
    return fields


def _reset_comments(sb: Sandbox, number: int, spec: IssueSpec) -> None:
    missing = [norm_text(c.body) for c in spec.comments]
    for comment in sb.list_comments(number):
        body = norm_text(comment.get("body"))
        if body in missing:
            missing.remove(body)
        else:
            sb.delete_comment(comment["id"], number)
    for comment in spec.comments:
        if norm_text(comment.body) in missing:
            missing.remove(norm_text(comment.body))
            sb.create_comment(number, comment.body, spec.key)
