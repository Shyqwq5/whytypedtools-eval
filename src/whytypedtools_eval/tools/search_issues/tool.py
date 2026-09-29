"""search_issues: full-text search over issues, always scoped to the sandbox repo."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, field_validator

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

# Qualifiers the agent may use inside `query`. Everything else (repo:, org:,
# user:, is:, type:, ...) could widen or change the search scope and is rejected.
ALLOWED_QUALIFIERS = frozenset({"in", "created", "updated", "closed", "comments", "no", "label"})

# A quoted phrase, or a run of non-space characters. An unbalanced quote falls
# through to the second branch, so a qualifier after it is still checked.
_TOKEN = re.compile(r'"[^"]*"|\S+')
_QUALIFIER = re.compile(r"^-?([A-Za-z_][A-Za-z0-9_-]*):")


def forbidden_qualifiers(query: str) -> list[str]:
    found = []
    for token in _TOKEN.findall(query):
        if token.startswith('"') and token.endswith('"') and len(token) > 1:
            continue
        m = _QUALIFIER.match(token)
        if m and m.group(1).lower() not in ALLOWED_QUALIFIERS:
            found.append(m.group(1) + ":")
    return found


class SearchIssuesInput(ToolInput):
    query: str = Field(
        min_length=1,
        max_length=200,
        description="Keywords to search for. Quote exact phrases. "
        "Allowed qualifiers: in:, created:, updated:, closed:, comments:, no:, label:.",
    )
    state: IssueState = Field("all", description='Issue state: "open", "closed" or "all".')
    labels: list[Label] = Field(
        default=[], max_length=10, description="Only issues that have ALL of these labels."
    )
    sort: Literal["best_match", "created", "updated", "comments"] = Field(
        "best_match", description="Sort field."
    )
    direction: Direction = Field("desc", description="Sort direction (ignored for best_match).")
    max_results: int = Field(10, ge=1, le=50, description="Maximum number of issues to return.")

    @field_validator("query")
    @classmethod
    def _scope_cannot_change(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("query must contain keywords")
        bad = forbidden_qualifiers(v)
        if bad:
            raise ValueError(
                f"qualifiers {', '.join(sorted(set(bad)))} are not allowed; the search is always "
                "limited to this repository's issues. Use the state and labels parameters instead."
            )
        return v


class SearchIssuesOutput(ToolOutput):
    effective_query: str
    total_count: int
    issues: list[IssueSummary]
    returned: int
    truncated: bool
    incomplete_results: bool | None = None
    hint: LabelHint | None = None


def build_query(repo: str, inp: SearchIssuesInput) -> str:
    """The repo and type scope come first and are always added by the tool."""
    parts = [f"repo:{repo}", "is:issue"]
    if inp.state != "all":
        parts.append(f"state:{inp.state}")
    parts.extend(f'label:"{name}"' for name in inp.labels)
    parts.append(inp.query)
    return " ".join(parts)


def _in_repo(item: dict[str, Any], repo: str) -> bool:
    return str(item.get("repository_url", "")).lower().endswith(f"/repos/{repo.lower()}")


def search_issues(ctx: ToolContext, inp: SearchIssuesInput) -> SearchIssuesOutput:
    query = build_query(ctx.sandbox_repo, inp)
    params: dict[str, str | int] = {"q": query, "per_page": min(ctx.page_size, inp.max_results)}
    if inp.sort != "best_match":
        params["sort"] = inp.sort
        params["order"] = inp.direction

    issues: list[IssueSummary] = []
    total_count = 0
    incomplete = False
    url: str | None = "search/issues"
    request_params: dict[str, str | int] | None = params
    while url and len(issues) < inp.max_results:
        resp = ctx.client.request("GET", url, params=request_params)
        page = resp.json()
        total_count = page.get("total_count", 0)
        incomplete = incomplete or bool(page.get("incomplete_results"))
        for item in page.get("items", []):
            # Defence in depth: drop anything outside the sandbox or not an issue.
            if "pull_request" in item or not _in_repo(item, ctx.sandbox_repo):
                continue
            if len(issues) < inp.max_results:
                issues.append(IssueSummary.from_api(item))
        url = resp.links.get("next", {}).get("url")
        request_params = None  # the next URL already carries the query string

    hint = label_hint(ctx, inp.labels) if not issues and inp.labels else None
    return SearchIssuesOutput(
        effective_query=query,
        total_count=total_count,
        issues=issues,
        returned=len(issues),
        truncated=total_count > len(issues),
        incomplete_results=True if incomplete else None,
        hint=hint,
    )
