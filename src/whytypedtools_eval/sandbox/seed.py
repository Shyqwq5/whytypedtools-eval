"""Idempotent seeding: create whatever seed labels and issues are missing.

Existing items are left untouched (use reset to restore them to the seed state).
"""

from __future__ import annotations

from whytypedtools_eval.sandbox.models import SandboxState, SeedData
from whytypedtools_eval.sandbox.ops import Sandbox, resolve_issue


def seed(sb: Sandbox, data: SeedData, state: SandboxState) -> SandboxState:
    existing_labels = {label["name"].lower() for label in sb.list_labels()}
    for spec in data.labels:
        if spec.name.lower() in existing_labels:
            sb.info(f"skip label {spec.name!r} (exists)")
        else:
            sb.create_label(spec)

    by_number = {issue["number"]: issue for issue in sb.list_issues()}
    claimed: set[int] = set()
    for spec in data.issues:
        number = resolve_issue(spec, state, by_number, claimed)
        if number is not None:
            sb.info(f"skip issue {spec.key!r} (exists as #{number})")
        else:
            number = sb.create_issue(spec)
        if number is not None:
            claimed.add(number)
            state.issues[spec.key] = number
    return state
