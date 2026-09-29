"""get_issue: one issue with its full body and comments."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from whytypedtools_eval.tools.base import ToolContext, ToolError, ToolInput, ToolOutput

# Bodies and comments longer than this are cut; real issues rarely come close.
MAX_BODY_CHARS = 8000
MAX_COMMENT_CHARS = 2000


def _cut(text: str | None, limit: int) -> tuple[str, bool]:
    text = (text or "").replace("\r\n", "\n").strip()
    return (text[:limit] + "…", True) if len(text) > limit else (text, False)


class GetIssueInput(ToolInput):
    number: int = Field(ge=1, description="The issue number, e.g. 12 for #12.")
    include_comments: bool = Field(True, description="Also return the issue's comments.")
    max_comments: int = Field(20, ge=1, le=50, description="Maximum number of comments to return (oldest first).")


class Comment(BaseModel):
    created_at: str
    body: str
    body_truncated: bool


class GetIssueOutput(ToolOutput):
    number: int
    title: str
    state: Literal["open", "closed"]
    state_reason: str | None = None
    labels: list[str]
    created_at: str
    updated_at: str
    body: str
    body_truncated: bool
    comments_total: int
    comments: list[Comment]
    comments_truncated: bool


def get_issue(ctx: ToolContext, inp: GetIssueInput) -> GetIssueOutput:
    item = ctx.client.get(f"repos/{ctx.sandbox_repo}/issues/{inp.number}")
    if "pull_request" in item:
        raise ToolError("not_found", f"#{inp.number} is a pull request, not an issue.")

    total = item.get("comments", 0)
    comments: list[Comment] = []
    if inp.include_comments and total:
        params = {"per_page": min(inp.max_comments, ctx.page_size)}
        for c in ctx.client.paginate(f"repos/{ctx.sandbox_repo}/issues/{inp.number}/comments", params):
            if len(comments) == inp.max_comments:
                break
            body, cut = _cut(c.get("body"), MAX_COMMENT_CHARS)
            comments.append(Comment(created_at=(c.get("created_at") or "")[:10], body=body, body_truncated=cut))

    body, body_cut = _cut(item.get("body"), MAX_BODY_CHARS)
    return GetIssueOutput(
        number=item["number"],
        title=item["title"],
        state=item["state"],
        state_reason=item.get("state_reason") if item["state"] == "closed" else None,
        labels=[lbl["name"] for lbl in item.get("labels", [])],
        created_at=(item.get("created_at") or "")[:10],
        updated_at=(item.get("updated_at") or "")[:10],
        body=body,
        body_truncated=body_cut,
        comments_total=total,
        comments=comments,
        comments_truncated=inp.include_comments and total > len(comments),
    )
