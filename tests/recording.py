"""Fixture recording, sanitising and replay, shared by scripts/record_fixtures.py and tests.

Fixture file format (tests/fixtures/<tool>/<scenario>.json):

    {
      "scenario": "...", "tool": "...", "args": {...}, "page_size": 50,
      "auth": "valid" | "invalid", "repo_suffix": "",
      "source": "recorded" | "fake" | "synthetic",
      "exchanges": [{"request": {"method", "path", "params"},
                     "response": {"status", "headers", "json"}}],
      "result": <call_tool output at recording time>
    }

Only allow-listed response headers and JSON fields are kept, the real sandbox
repo is replaced by FIXTURE_REPO, and request headers are never stored.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
import respx

from whytypedtools_eval.github import GitHubClient
from whytypedtools_eval.tools.base import ToolContext
from whytypedtools_eval.tools.registry import call_tool

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
FIXTURE_REPO = "sandbox-owner/whytypedtools-sandbox"
FIXTURE_TOKEN = "github_pat_fixture"
INVALID_TOKEN = "github_pat_invalid_fixture_0000"
NOT_FOUND_SUFFIX = "-does-not-exist"

KEPT_HEADERS = frozenset({
    "content-type", "link", "retry-after",
    "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
    "x-ratelimit-used", "x-ratelimit-resource",
})
ISSUE_FIELDS = frozenset({
    "number", "title", "body", "state", "state_reason", "labels", "comments",
    "created_at", "updated_at", "closed_at", "locked", "html_url", "repository_url",
})
TOKEN_PATTERN = re.compile(r"\b(ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{8,}")


@dataclass(frozen=True)
class Scenario:
    name: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    page_size: int = 50
    auth: Literal["valid", "invalid"] = "valid"
    repo_suffix: str = ""
    # "ok" or the expected error type. A recording whose outcome differs is
    # rejected, so a broken run can never produce plausible-looking fixtures.
    expect: str = "ok"


SCENARIOS: list[Scenario] = [
    # list_issues
    Scenario("list_default", "list_issues"),
    Scenario("list_closed", "list_issues", {"state": "closed"}),
    Scenario("list_all_bug", "list_issues", {"state": "all", "labels": ["bug"]}),
    Scenario("list_bug_and_api", "list_issues", {"labels": ["bug", "api"]}),
    Scenario("list_most_commented", "list_issues", {"state": "all", "sort": "comments", "max_results": 3}),
    Scenario("list_oldest_first", "list_issues",
             {"state": "all", "sort": "created", "direction": "asc", "max_results": 5}),
    Scenario("list_paginated", "list_issues", {"state": "all", "max_results": 8}, page_size=3),
    Scenario("list_empty_known_labels", "list_issues", {"labels": ["performance", "question"]}),
    Scenario("list_unknown_label", "list_issues", {"labels": ["bugs"]}),
    Scenario("list_auth_failed", "list_issues", auth="invalid", expect="auth_failed"),
    Scenario("list_not_found", "list_issues", repo_suffix=NOT_FOUND_SUFFIX, expect="not_found"),
    # search_issues
    Scenario("search_rate_limit", "search_issues", {"query": "rate limit"}),
    Scenario("search_rate_limit_open", "search_issues", {"query": "rate limit", "state": "open"}),
    Scenario("search_phrase_in_title", "search_issues", {"query": '"rate limit" in:title'}),
    Scenario("search_large_performance", "search_issues", {"query": "large", "labels": ["performance"]}),
    Scenario("search_sorted_created_asc", "search_issues",
             {"query": "export", "sort": "created", "direction": "asc"}),
    Scenario("search_paginated", "search_issues",
             {"query": "timeout OR export OR limit", "max_results": 5}, page_size=2),
    Scenario("search_no_results", "search_issues", {"query": "xyzzyplugh"}),
    Scenario("search_unknown_label", "search_issues", {"query": "timeout", "labels": ["perf"]}),
    Scenario("search_too_many_operators", "search_issues", {"query": "a OR b OR c OR d OR e OR f OR g"},
             expect="validation_failed"),
    Scenario("search_webhook_timeout", "search_issues", {"query": "webhook timeout"}),
    Scenario("search_auth_failed", "search_issues", {"query": "rate limit"}, auth="invalid",
             expect="auth_failed"),
]


# -- sanitising --------------------------------------------------------------


class UnsafeFixtureError(RuntimeError):
    """Raised when a fixture still contains something that must not be committed."""


class UnexpectedOutcomeError(RuntimeError):
    """Raised when a scenario's result doesn't match its declared expectation."""


def outcome(result: dict[str, Any]) -> str:
    return "ok" if result.get("ok") else result["error"]["type"]


def check_outcome(scenario: Scenario, result: dict[str, Any]) -> None:
    actual = outcome(result)
    if actual != scenario.expect:
        detail = "" if actual == "ok" else f": {result['error']['message']}"
        raise UnexpectedOutcomeError(
            f"scenario {scenario.name!r} expected {scenario.expect!r} but got {actual!r}{detail}"
        )


def _scrub_issue(item: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in item.items() if k in ISSUE_FIELDS}
    out["labels"] = [
        {k: lbl.get(k) for k in ("name", "color", "description")} for lbl in item.get("labels", [])
    ]
    if "pull_request" in item:
        out["pull_request"] = {}
    return out


def scrub_json(data: Any) -> Any:
    """Keep only the fields tools and tests need."""
    if isinstance(data, list):
        return [scrub_json(x) for x in data]
    if not isinstance(data, dict):
        return data
    if "number" in data and "title" in data:
        return _scrub_issue(data)
    if "items" in data:
        return {
            "total_count": data.get("total_count", 0),
            "incomplete_results": data.get("incomplete_results", False),
            "items": [scrub_json(x) for x in data["items"]],
        }
    if "name" in data and "color" in data:
        return {k: data.get(k) for k in ("name", "color", "description")}
    out: dict[str, Any] = {}
    if "message" in data:
        out["message"] = data["message"]
    if isinstance(data.get("errors"), list):
        out["errors"] = [
            {k: e.get(k) for k in ("message", "code", "field") if isinstance(e, dict) and k in e}
            for e in data["errors"]
        ]
    return out


def _replace_repo(text: str, real_repo: str) -> str:
    return re.sub(re.escape(real_repo), FIXTURE_REPO, text, flags=re.IGNORECASE)


def sanitize_exchange(request: httpx.Request, response: httpx.Response, real_repo: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        body = None
    headers = {k.lower(): v for k, v in response.headers.items() if k.lower() in KEPT_HEADERS}
    exchange = {
        "request": {
            "method": request.method,
            "path": request.url.path,
            "params": dict(request.url.params),
        },
        "response": {"status": response.status_code, "headers": headers, "json": scrub_json(body)},
    }
    return json.loads(_replace_repo(json.dumps(exchange), real_repo))


def assert_safe(fixture: dict[str, Any], secrets: list[str], real_owner: str) -> None:
    """Refuse to write a fixture that still contains a secret or the real owner name."""
    text = json.dumps(fixture)
    for secret in secrets:
        if secret and secret in text:
            raise UnsafeFixtureError("fixture contains a secret value")
    if TOKEN_PATTERN.search(text.replace(FIXTURE_TOKEN, "").replace(INVALID_TOKEN, "")):
        raise UnsafeFixtureError("fixture contains a token-like string")
    if real_owner.lower() != FIXTURE_REPO.split("/")[0] and re.search(
        rf"(?<![A-Za-z0-9-]){re.escape(real_owner)}(?![A-Za-z0-9-])", text, flags=re.IGNORECASE
    ):
        raise UnsafeFixtureError("fixture still contains the real repo owner name")


# -- recording ---------------------------------------------------------------


class RecordingTransport(httpx.BaseTransport):
    def __init__(self, inner: httpx.BaseTransport, real_repo: str) -> None:
        self.inner = inner
        self.real_repo = real_repo
        self.exchanges: list[dict[str, Any]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self.inner.handle_request(request)
        # read() returns the *decoded* body (e.g. gunzipped). The response handed
        # back must therefore drop Content-Encoding/Content-Length, or the client
        # would try to decode the plain body a second time.
        content = response.read()
        headers = [
            (k, v) for k, v in response.headers.multi_items()
            if k.lower() not in ("content-encoding", "content-length")
        ]
        decoded = httpx.Response(response.status_code, headers=headers, content=content, request=request)
        self.exchanges.append(sanitize_exchange(request, decoded, self.real_repo))
        return decoded


def record_scenario(
    scenario: Scenario,
    *,
    token: str,
    real_repo: str,
    source: str,
    inner: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    transport = RecordingTransport(inner or httpx.HTTPTransport(), real_repo)
    kwargs: dict[str, Any] = {"fail_fast_after": 5.0, "max_retries": 2, "transport": transport}
    if sleep is not None:
        kwargs["sleep"] = sleep
    use_token = INVALID_TOKEN if scenario.auth == "invalid" else token
    repo = real_repo + scenario.repo_suffix
    with GitHubClient(use_token, real_repo, **kwargs) as client:
        ctx = ToolContext(client, repo, page_size=scenario.page_size)
        result = call_tool(ctx, scenario.tool, scenario.args)
    check_outcome(scenario, result)
    fixture = {
        "scenario": scenario.name,
        "tool": scenario.tool,
        "args": scenario.args,
        "page_size": scenario.page_size,
        "auth": scenario.auth,
        "repo_suffix": scenario.repo_suffix,
        "expect": scenario.expect,
        "source": source,
        "exchanges": transport.exchanges,
        "result": result,
    }
    return json.loads(_replace_repo(json.dumps(fixture), real_repo))


def fixture_path(tool: str, name: str) -> Path:
    return FIXTURES_DIR / tool / f"{name}.json"


def write_fixture(fixture: dict[str, Any]) -> Path:
    path = fixture_path(fixture["tool"], fixture["scenario"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return path


# -- replay ------------------------------------------------------------------


def load_fixture(tool: str, name: str) -> dict[str, Any]:
    return json.loads(fixture_path(tool, name).read_text(encoding="utf-8"))


def all_fixtures() -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(FIXTURES_DIR.glob("*/*.json"))]


class Replayer:
    """Serve a fixture's responses in order, asserting each request matches exactly."""

    def __init__(self, fixture: dict[str, Any]) -> None:
        self.exchanges = fixture["exchanges"]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        i = len(self.requests)
        self.requests.append(request)
        if i >= len(self.exchanges):
            raise AssertionError(f"unexpected extra request: {request.method} {request.url}")
        expected = self.exchanges[i]["request"]
        actual = {"method": request.method, "path": request.url.path, "params": dict(request.url.params)}
        if actual != expected:
            raise AssertionError(f"request {i} mismatch:\n expected {expected}\n actual   {actual}")
        resp = self.exchanges[i]["response"]
        return httpx.Response(resp["status"], headers=resp["headers"], json=resp["json"])

    @property
    def done(self) -> bool:
        return len(self.requests) == len(self.exchanges)


def replay_call(
    router: respx.Router, fixture: dict[str, Any], *, sleeps: list[float] | None = None
) -> tuple[dict[str, Any], Replayer]:
    """Run the fixture's tool call against its recorded responses."""
    replayer = Replayer(fixture)
    router.route().mock(side_effect=replayer)
    token = INVALID_TOKEN if fixture.get("auth") == "invalid" else FIXTURE_TOKEN
    sink = sleeps if sleeps is not None else []
    with GitHubClient(token, FIXTURE_REPO, fail_fast_after=5.0, max_retries=2,
                      search_interval=0, sleep=sink.append) as client:
        ctx = ToolContext(client, FIXTURE_REPO + fixture.get("repo_suffix", ""),
                          page_size=fixture.get("page_size", 50))
        result = call_tool(ctx, fixture["tool"], fixture["args"])
    return result, replayer
