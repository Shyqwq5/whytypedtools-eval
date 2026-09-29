"""list_issues: structured filtering of repo issues (no text matching)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from whytypedtools_eval.tools.base import (
    Direction,
    IssueState,
    IssueSummary,
    Label,
    LabelHint,
    ToolContext,
    ToolInput,
    ToolOutput,
    label_hint,
)


class ListIssuesInput(ToolInput):
    state: IssueState = Field("open", description='Issue state: "open", "closed" or "all".')
    labels: list[Label] = Field(
        default=[], max_length=10, description="Only issues that have ALL of these labels."
    )
    # "comments" is deliberately not offered: GitHub returned it out of order (README "Findings").
    sort: Literal["created", "updated"] = Field("created", description="Sort field.")
    direction: Direction = Field("desc", description="Sort direction.")
    max_results: int = Field(20, ge=1, le=50, description="Maximum number of issues to return.")


class ListIssuesOutput(ToolOutput):
    issues: list[IssueSummary]
    returned: int
    truncated: bool
    hint: LabelHint | None = None


def list_issues(ctx: ToolContext, inp: ListIssuesInput) -> ListIssuesOutput:
    params: dict[str, str | int] = {
        "state": inp.state,
        "sort": inp.sort,
        "direction": inp.direction,
        "per_page": ctx.page_size,
    }
    if inp.labels:
        params["labels"] = ",".join(inp.labels)

    issues: list[IssueSummary] = []
    truncated = False
    for item in ctx.client.paginate(f"repos/{ctx.sandbox_repo}/issues", params):
        if "pull_request" in item:
            continue
        if len(issues) == inp.max_results:
            truncated = True
            break
        issues.append(IssueSummary.from_api(item))

    hint = label_hint(ctx, inp.labels) if not issues and inp.labels else None
    return ListIssuesOutput(issues=issues, returned=len(issues), truncated=truncated, hint=hint)
