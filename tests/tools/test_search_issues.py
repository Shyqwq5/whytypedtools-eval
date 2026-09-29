import httpx
import pytest

from tests.conftest import SEED_FILE
from tests.recording import FIXTURE_REPO, all_fixtures, load_fixture, replay_call
from whytypedtools_eval.github import GitHubClient
from whytypedtools_eval.sandbox.models import load_seed
from whytypedtools_eval.tools.base import ToolContext
from whytypedtools_eval.tools.registry import call_tool
from whytypedtools_eval.tools.search_issues.tool import SearchIssuesInput, build_query

SEED = load_seed(SEED_FILE)
SCOPE = f"repo:{FIXTURE_REPO} is:issue"


def run(mock_api, name):
    result, replayer = replay_call(mock_api, load_fixture("search_issues", name))
    assert replayer.done, "not every recorded request was replayed"
    return result


def titles(result):
    return {i["title"] for i in result["result"]["issues"]}


# -- scope ---------------------------------------------------------------------


@pytest.mark.parametrize("fixture", [f for f in all_fixtures() if f["tool"] == "search_issues"],
                         ids=lambda f: f["scenario"])
def test_every_recorded_search_is_scoped_to_the_sandbox(fixture):
    for exchange in fixture["exchanges"]:
        if exchange["request"]["path"] == "/search/issues" and "q" in exchange["request"]["params"]:
            assert exchange["request"]["params"]["q"].startswith(SCOPE + " ")


@pytest.mark.parametrize(
    "query",
    [
        "repo:someone/else rate limit",
        "rate limit repo:someone/else",
        "-repo:x rate",
        "REPO:x rate",
        "org:someone rate",
        "user:someone rate",
        "is:pr rate",
        "is:open rate",
        "type:pr rate",
        '"unbalanced repo:someone/else',
        'label:bug repo:x',
    ],
)
def test_scope_changing_qualifiers_are_rejected(mock_api, query):
    route = mock_api.route()
    with GitHubClient("github_pat_fixture", FIXTURE_REPO) as client:
        result = call_tool(ToolContext(client, FIXTURE_REPO), "search_issues", {"query": query})
    assert result["error"]["type"] == "invalid_input"
    assert "not allowed" in result["error"]["message"]
    assert route.call_count == 0


@pytest.mark.parametrize(
    "query",
    ['"repo:x" is a phrase', "rate in:title", "timeout label:bug", "-label:bug slow", "comments:>1 x",
     "created:>2024-01-01 x", "no:label x", "plain words"],
)
def test_allowed_queries_keep_the_scope_first(query):
    q = build_query(FIXTURE_REPO, SearchIssuesInput(query=query))
    assert q.startswith(SCOPE + " ")
    assert q.endswith(query)


def test_state_and_labels_become_qualifiers():
    q = build_query(FIXTURE_REPO, SearchIssuesInput(query="x", state="open", labels=["bug", "good first"]))
    assert q == f'{SCOPE} state:open label:"bug" label:"good first" x'
    assert build_query(FIXTURE_REPO, SearchIssuesInput(query="x")) == f"{SCOPE} x"


def test_results_outside_the_sandbox_are_dropped(mock_api):
    items = [
        {"number": 1, "title": "Ours", "state": "open", "labels": [],
         "repository_url": f"https://api.github.com/repos/{FIXTURE_REPO}"},
        {"number": 2, "title": "Theirs", "state": "open", "labels": [],
         "repository_url": "https://api.github.com/repos/other/repo"},
        {"number": 3, "title": "Prefix trap", "state": "open", "labels": [],
         "repository_url": f"https://api.github.com/repos/{FIXTURE_REPO}2"},
        {"number": 4, "title": "A PR", "state": "open", "labels": [], "pull_request": {},
         "repository_url": f"https://api.github.com/repos/{FIXTURE_REPO}"},
    ]
    mock_api.get("/search/issues").mock(
        return_value=httpx.Response(200, json={"total_count": 4, "incomplete_results": False, "items": items})
    )
    with GitHubClient("github_pat_fixture", FIXTURE_REPO, search_interval=0) as client:
        result = call_tool(ToolContext(client, FIXTURE_REPO), "search_issues", {"query": "x"})
    assert titles(result) == {"Ours"}


# -- happy paths and filters ---------------------------------------------------


def test_keyword_search(mock_api):
    result = run(mock_api, "search_rate_limit")
    assert {"API returns 429 when rate limit is exceeded", "Document rate limit headers"} <= titles(result)
    assert result["result"]["effective_query"] == f"{SCOPE} rate limit"
    assert result["result"]["total_count"] >= result["result"]["returned"]


def test_default_state_is_all(mock_api):
    states = {i["state"] for i in run(mock_api, "search_rate_limit")["result"]["issues"]}
    assert states == {"open", "closed"}


def test_state_filter(mock_api):
    result = run(mock_api, "search_rate_limit_open")
    assert all(i["state"] == "open" for i in result["result"]["issues"])
    assert "Document rate limit headers" not in titles(result)


def test_phrase_in_title(mock_api):
    result = run(mock_api, "search_phrase_in_title")
    assert titles(result)
    assert all("rate limit" in t.lower() for t in titles(result))


def test_label_filter(mock_api):
    # "large" also appears in a non-performance issue, which the label must exclude.
    result = run(mock_api, "search_large_performance")
    assert titles(result)
    assert all("performance" in i["labels"] for i in result["result"]["issues"])
    assert "Make the per-tenant rate limit configurable" not in titles(result)


def test_sorted_ascending(mock_api):
    numbers = [i["number"] for i in run(mock_api, "search_sorted_created_asc")["result"]["issues"]]
    assert numbers == sorted(numbers) and numbers


def test_pagination(mock_api):
    fixture = load_fixture("search_issues", "search_paginated")
    assert len(fixture["exchanges"]) >= 3
    result = run(mock_api, "search_paginated")
    assert result["result"]["returned"] == 5
    assert result["result"]["truncated"] == (result["result"]["total_count"] > 5)


def test_no_results(mock_api):
    result = run(mock_api, "search_no_results")
    assert result["result"]["issues"] == []
    assert result["result"]["total_count"] == 0
    assert "hint" not in result["result"]


def test_unknown_label_gets_hint(mock_api):
    hint = run(mock_api, "search_unknown_label")["result"]["hint"]
    assert hint["unknown_labels"] == ["perf"]
    assert "performance" in hint["available_labels"]


def test_summary_injection_is_exposed_by_search(mock_api):
    spec = next(i for i in SEED.issues if i.key == "injection-summary")
    hit = next(i for i in run(mock_api, "search_webhook_timeout")["result"]["issues"] if i["title"] == spec.title)
    assert spec.safety_test.payload_marker in hit["body_excerpt"]


# -- errors --------------------------------------------------------------------


def test_github_422_is_reported_with_details(mock_api):
    error = run(mock_api, "search_too_many_operators")["error"]
    assert error["type"] == "validation_failed"
    assert "operators" in error["message"]
    assert error["retryable"] is False


def test_auth_failure(mock_api):
    assert run(mock_api, "search_auth_failed")["error"]["type"] == "auth_failed"


# -- input validation ----------------------------------------------------------


@pytest.mark.parametrize(
    "args, field",
    [
        ({}, "query"),
        ({"query": ""}, "query"),
        ({"query": "   "}, "query"),
        ({"query": "x" * 201}, "query"),
        ({"query": "x", "state": "any"}, "state"),
        ({"query": "x", "sort": "relevance"}, "sort"),
        ({"query": "x", "max_results": 100}, "max_results"),
        ({"query": "x", "labels": ['a"b']}, "labels.0"),
        ({"query": "x", "repo": "other/repo"}, "repo"),
    ],
)
def test_invalid_input_is_rejected_before_any_request(mock_api, args, field):
    route = mock_api.route()
    with GitHubClient("github_pat_fixture", FIXTURE_REPO) as client:
        result = call_tool(ToolContext(client, FIXTURE_REPO), "search_issues", args)
    assert result["error"]["type"] == "invalid_input"
    assert field in result["error"]["message"]
    assert route.call_count == 0
