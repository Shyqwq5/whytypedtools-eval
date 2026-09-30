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
from urllib.parse import quote, unquote_plus

import httpx
import respx
import yaml

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
    # Scenarios that write are only ever recorded from the in-memory fake.
    writes: bool = False


SCENARIOS: list[Scenario] = [
    # list_issues
    Scenario("list_default", "list_issues"),
    Scenario("list_closed", "list_issues", {"state": "closed"}),
    Scenario("list_all_bug", "list_issues", {"state": "all", "labels": ["bug"]}),
    Scenario("list_bug_and_api", "list_issues", {"labels": ["bug", "api"]}),
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
    # get_issue (numbers are seed order: 1 = rate-limit-429, 5 = search-timeout,
    # 9 = injection-close-all)
    Scenario("get_with_comments", "get_issue", {"number": 1}),
    Scenario("get_without_comments_flag", "get_issue", {"number": 1, "include_comments": False}),
    Scenario("get_no_comments", "get_issue", {"number": 5}),
    Scenario("get_injection_full_body", "get_issue", {"number": 9}),
    Scenario("get_not_found", "get_issue", {"number": 9999}, expect="not_found"),
    # Added for the tool gate: comments over two pages, and a comment list cut short.
    Scenario("get_comments_paginated", "get_issue", {"number": 1}, page_size=1),
    Scenario("get_comments_truncated", "get_issue", {"number": 1, "max_comments": 1}),
    # add_label (fake only). Each writing scenario uses its own issue so recording
    # order doesn't matter: 21 = unlabeled-login, 17 = dark-mode.
    Scenario("add_label_new", "add_label", {"number": 21, "labels": ["bug"]}, writes=True),
    Scenario("add_label_case_insensitive", "add_label", {"number": 17, "labels": ["BUG", "Question"]},
             writes=True),
    Scenario("add_label_already_present", "add_label", {"number": 1, "labels": ["bug"]}, writes=True),
    Scenario("add_label_unknown", "add_label", {"number": 1, "labels": ["urgent"]}, writes=True,
             expect="invalid_input"),
    Scenario("add_label_not_found", "add_label", {"number": 9999, "labels": ["bug"]}, writes=True,
             expect="not_found"),
]




def load_scenario_files(fixtures_dir: Path = FIXTURES_DIR) -> list[Scenario]:
    """Scenarios of tools added through the scaffold: tests/fixtures/<tool>/scenarios.yaml.

    Format: {tool: <name>, scenarios: [{name, args, page_size, auth, repo_suffix,
    expect, writes}]}; only `name` is required."""
    out = []
    for path in sorted(fixtures_dir.glob("*/scenarios.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        tool = data["tool"]
        if tool != path.parent.name:
            raise ValueError(f"{path}: tool {tool!r} does not match its folder")
        out += [Scenario(tool=tool, **s) for s in data.get("scenarios") or []]
    return out


SCENARIOS += load_scenario_files()
_names = [s.name for s in SCENARIOS]
if len(_names) != len(set(_names)):
    raise ValueError(f"duplicate scenario names: {sorted({n for n in _names if _names.count(n) > 1})}")


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
    if "id" in data and "body" in data:
        # A comment: drop author and URLs.
        return {k: data.get(k) for k in ("id", "body", "created_at", "updated_at")}
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
    """Replace the real repo in plain and URL-encoded form (e.g. a `q=repo%3Aowner%2Fname`
    query inside a Link header). Encoded forms get the encoded placeholder, so URLs
    still decode to FIXTURE_REPO and replay matches.
    """
    for real, placeholder in (
        (quote(real_repo, safe=""), quote(FIXTURE_REPO, safe="")),  # owner%2Fname
        (real_repo, FIXTURE_REPO),
    ):
        text = re.sub(re.escape(real), placeholder, text, flags=re.IGNORECASE)
    return text


def _fully_decoded(text: str, rounds: int = 3) -> str:
    """URL-decode repeatedly so identifiers can't hide behind (double) encoding."""
    for _ in range(rounds):
        decoded = unquote_plus(text)
        if decoded == text:
            break
        text = decoded
    return text


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
    """Refuse to write a fixture that still contains a secret or the real owner name.

    The scan runs on the raw text and on its URL-decoded form: an encoded
    `%3Aowner` would otherwise slip past the word-boundary check.
    """
    raw = json.dumps(fixture)
    text = raw + "\n" + _fully_decoded(raw)
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
    if scenario.writes and source != "fake":
        raise UnsafeFixtureError(f"scenario {scenario.name!r} writes; record it from the fake only")
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
