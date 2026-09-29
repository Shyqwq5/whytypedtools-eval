"""add_label: add existing labels to one issue (the only write tool)."""

from __future__ import annotations

from pydantic import Field

from whytypedtools_eval.tools.base import Label, ToolContext, ToolError, ToolInput, ToolOutput


class AddLabelInput(ToolInput):
    number: int = Field(ge=1, description="The issue number, e.g. 12 for #12.")
    labels: list[Label] = Field(
        min_length=1, max_length=5, description="Names of existing labels to add. Case-insensitive."
    )


class AddLabelOutput(ToolOutput):
    number: int
    added: list[str]
    already_present: list[str]
    labels: list[str]


def add_label(ctx: ToolContext, inp: AddLabelInput) -> AddLabelOutput:
    # GitHub would silently create unknown labels; only existing ones are allowed.
    available = {name.lower(): name for name in ctx.repo_labels()}
    unknown = [name for name in inp.labels if name.lower() not in available]
    if unknown:
        raise ToolError(
            "invalid_input",
            f"Unknown label(s): {', '.join(unknown)}. Labels cannot be created with this tool. "
            f"Available labels: {', '.join(sorted(available.values(), key=str.lower))}.",
        )
    wanted = list(dict.fromkeys(available[name.lower()] for name in inp.labels))

    item = ctx.client.get(f"repos/{ctx.sandbox_repo}/issues/{inp.number}")
    if "pull_request" in item:
        raise ToolError("not_found", f"#{inp.number} is a pull request, not an issue.")
    current = [lbl["name"] for lbl in item.get("labels", [])]
    present = {name.lower() for name in current}
    added = [name for name in wanted if name.lower() not in present]
    already = [name for name in wanted if name.lower() in present]

    labels = current + added
    if added:
        resp = ctx.write("POST", f"repos/{ctx.sandbox_repo}/issues/{inp.number}/labels", {"labels": added})
        if resp is not None:
            labels = [lbl["name"] for lbl in resp]
    return AddLabelOutput(number=inp.number, added=added, already_present=already, labels=labels)
