"""Protocol-independent building blocks shared by all tools.

Tools take a `ToolContext` plus a validated pydantic input model and return a
compact pydantic output model. Failures are raised as `ToolError`, which carries
a structured, agent-readable payload. Nothing here knows about MCP.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

import json

import httpx
from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints

from whytypedtools_eval.config import Settings
from whytypedtools_eval.github import GitHubClient, GitHubError, RateLimitError
from whytypedtools_eval.sandbox.guard import SandboxGuardError

EXCERPT_CHARS = 300
# Tool calls wait out only short rate limits; anything longer is reported to the agent.
TOOL_FAIL_FAST_AFTER_S = 5.0
TOOL_MAX_RETRIES = 2


def _label_chars(value: str) -> str:
    # Commas separate labels in the list API; quotes delimit them in search queries.
    if "," in value or '"' in value:
        raise ValueError("label names cannot contain commas or double quotes")
    return value


Label = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=50),
    AfterValidator(_label_chars),
]

IssueState = Literal["open", "closed", "all"]
Direction = Literal["asc", "desc"]
# live: writes go to GitHub. dry_run: writes are recorded but not sent, and tools
# return the result they would have returned, so the agent cannot tell.
WriteMode = Literal["live", "dry_run"]


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolContext:
    """Everything a tool needs to run: a client, the repo it is scoped to, and how
    to treat writes. Every write made (or skipped in dry-run) is kept in `write_log`.
    """

    def __init__(
        self,
        client: GitHubClient,
        sandbox_repo: str,
        *,
        page_size: int = 50,
        write_mode: WriteMode = "live",
    ) -> None:
        self.client = client
        self.sandbox_repo = sandbox_repo
        self.page_size = page_size
        self.write_mode = write_mode
        self.write_log: list[dict[str, Any]] = []
        self._labels: list[str] | None = None

    @classmethod
    def from_settings(cls, settings: Settings, *, write_mode: WriteMode = "live") -> ToolContext:
        client = GitHubClient(
            settings.github_token.get_secret_value(),
            settings.sandbox_repo,
            fail_fast_after=TOOL_FAIL_FAST_AFTER_S,
            max_retries=TOOL_MAX_RETRIES,
        )
        return cls(client, settings.sandbox_repo, write_mode=write_mode)

    def write(self, method: str, path: str, body: Any = None, *, meta: dict[str, Any] | None = None) -> Any | None:
        """Send a write (live) or only record it (dry_run). Returns the JSON body or None.

        The entry is logged before sending, so a failed live write still shows up
        as attempted; `executed` is set once GitHub accepted it. `meta` adds fields to
        the log entry (e.g. `prev_labels` for a label replacement, so the write can be
        classified by its effect).
        """
        entry: dict[str, Any] = {"method": method.upper(), "path": path, "body": body, "executed": False,
                                 **(meta or {})}
        self.write_log.append(entry)
        if self.write_mode == "dry_run":
            return None
        resp = self.client.request(method, path, json=body)
        entry["executed"] = True
        return resp.json() if resp.content else None

    def repo_labels(self) -> list[str]:
        """Label names in the repo, fetched once per context (session)."""
        if self._labels is None:
            self._labels = [lbl["name"] for lbl in self.client.paginate(f"repos/{self.sandbox_repo}/labels")]
        return self._labels


class IssueSummary(BaseModel):
    number: int
    title: str
    state: Literal["open", "closed"]
    state_reason: str | None = None
    labels: list[str]
    comments: int
    created_at: str
    updated_at: str
    body_excerpt: str
    body_truncated: bool

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> IssueSummary:
        body = (item.get("body") or "").replace("\r\n", "\n").strip()
        truncated = len(body) > EXCERPT_CHARS
        return cls(
            number=item["number"],
            title=item["title"],
            state=item["state"],
            state_reason=item.get("state_reason") if item["state"] == "closed" else None,
            labels=[lbl["name"] for lbl in item.get("labels", [])],
            comments=item.get("comments", 0),
            created_at=(item.get("created_at") or "")[:10],
            updated_at=(item.get("updated_at") or "")[:10],
            body_excerpt=body[:EXCERPT_CHARS] + ("…" if truncated else ""),
            body_truncated=truncated,
        )


class LabelHint(BaseModel):
    message: str
    unknown_labels: list[str]
    available_labels: list[str]


def label_hint(ctx: ToolContext, labels: list[str]) -> LabelHint | None:
    """Explain an empty result caused by labels that don't exist. None if all exist."""
    available = ctx.repo_labels()
    known = {name.lower() for name in available}
    unknown = [name for name in labels if name.lower() not in known]
    if not unknown:
        return None
    return LabelHint(
        message="No issues matched. Some requested labels do not exist in this repository.",
        unknown_labels=unknown,
        available_labels=sorted(available, key=str.lower),
    )


ErrorType = Literal[
    "unknown_tool",
    "invalid_input",
    "auth_failed",
    "forbidden",
    "not_found",
    "validation_failed",
    "rate_limited",
    "upstream_error",
    "invalid_response",
]


class ToolError(Exception):
    """A failure the agent can read and act on. Never contains secrets or headers."""

    def __init__(
        self,
        type: ErrorType,
        message: str,
        *,
        retryable: bool = False,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.type = type
        self.message = message
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "message": self.message,
            "retryable": self.retryable,
            "retry_after_seconds": self.retry_after_seconds,
        }


def to_tool_error(exc: Exception) -> ToolError:
    """Map client/transport exceptions to structured tool errors."""
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, RateLimitError):
        wait = max(1, round(exc.retry_after))
        return ToolError(
            "rate_limited",
            f"GitHub rate limit reached. Wait about {wait} seconds before calling a GitHub tool again.",
            retryable=True,
            retry_after_seconds=wait,
        )
    if isinstance(exc, GitHubError):
        if exc.status == 401:
            return ToolError(
                "auth_failed",
                "GitHub rejected the credentials. This cannot be fixed by retrying; "
                "the operator must update the token.",
            )
        if exc.status == 403:
            return ToolError(
                "forbidden",
                "The token does not have permission for this request. Retrying will not help.",
            )
        if exc.status == 404:
            return ToolError(
                "not_found",
                "GitHub returned 404: the repository or resource was not found, "
                "or the token cannot see it.",
            )
        if exc.status == 422:
            return ToolError(
                "validation_failed",
                f"GitHub rejected the request as invalid: {exc.detail}. Adjust the arguments and try again.",
            )
        if exc.status >= 500:
            return ToolError(
                "upstream_error",
                f"GitHub returned a server error ({exc.status}). Retrying later may work.",
                retryable=True,
            )
        return ToolError("upstream_error", f"GitHub returned an unexpected status {exc.status}.")
    if isinstance(exc, SandboxGuardError):
        return ToolError("forbidden", "The request was blocked by the sandbox guard.")
    if isinstance(exc, httpx.TransportError):
        # Connection, timeout and protocol failures: transient, worth retrying.
        return ToolError(
            "upstream_error",
            f"Could not reach GitHub ({type(exc).__name__}). Retrying later may work.",
            retryable=True,
        )
    if isinstance(exc, (httpx.DecodingError, json.JSONDecodeError)):
        # GitHub answered, but the body could not be decoded or parsed. Retrying
        # the same call will almost certainly fail the same way.
        return ToolError(
            "invalid_response",
            f"GitHub sent a response that could not be read ({type(exc).__name__}). "
            "Retrying will not help.",
        )
    if isinstance(exc, httpx.HTTPError):
        return ToolError("upstream_error", f"HTTP error talking to GitHub ({type(exc).__name__}).")
    raise exc
