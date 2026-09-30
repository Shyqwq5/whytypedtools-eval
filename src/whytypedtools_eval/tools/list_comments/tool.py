"""list_comments: only the discussion on one issue, optionally only recent comments.

Overlaps get_issue, which also returns comments (with the issue's body). Added
through the scaffold as the fifth tool, so the tool-set check has an overlap to
measure (docs/design/tool-gate.md).
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field

from whytypedtools_eval.tools.base import ToolContext, ToolError, ToolInput, ToolOutput

# Longer comments are cut; GitHub allows up to 65,536 characters per comment.
MAX_COMMENT_CHARS = 4000


class ListCommentsInput(ToolInput):
    number: int = Field(ge=1, description="The issue number, e.g. 12 for #12.")
    since: date | None = Field(
        None, description="Only comments updated on or after this date (YYYY-MM-DD). Omit for all comments.")
    max_results: int = Field(20, ge=1, le=50, description="Maximum number of comments to return (oldest first).")


class IssueComment(BaseModel):
    created_at: str
    updated_at: str
    body: str
    body_truncated: bool


class ListCommentsOutput(ToolOutput):
    number: int
    title: str
    comments_total: int
    comments: list[IssueComment]
    returned: int
    truncated: bool


def _cut(text: str | None) -> tuple[str, bool]:
    text = (text or "").replace("\r\n", "\n").strip()
    return (text[:MAX_COMMENT_CHARS] + "…", True) if len(text) > MAX_COMMENT_CHARS else (text, False)


def list_comments(ctx: ToolContext, inp: ListCommentsInput) -> ListCommentsOutput:
    item = ctx.client.get(f"repos/{ctx.sandbox_repo}/issues/{inp.number}")
    if "pull_request" in item:
        raise ToolError("not_found", f"#{inp.number} is a pull request, not an issue.")

    params: dict[str, str | int] = {"per_page": min(inp.max_results, ctx.page_size)}
    if inp.since is not None:
        params["since"] = datetime.combine(inp.since, datetime.min.time()).isoformat() + "Z"
    comments: list[IssueComment] = []
    truncated = False
    if item.get("comments", 0):
        for c in ctx.client.paginate(f"repos/{ctx.sandbox_repo}/issues/{inp.number}/comments", params):
            if len(comments) == inp.max_results:
                truncated = True
                break
            body, cut = _cut(c.get("body"))
            comments.append(IssueComment(created_at=(c.get("created_at") or "")[:10],
                                         updated_at=(c.get("updated_at") or c.get("created_at") or "")[:10],
                                         body=body, body_truncated=cut))
    return ListCommentsOutput(number=item["number"], title=item["title"], comments_total=item.get("comments", 0),
                              comments=comments, returned=len(comments), truncated=truncated)
