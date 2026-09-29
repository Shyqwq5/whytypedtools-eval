import httpx
import pytest

from tests.conftest import SEED_FILE
from tests.recording import FIXTURE_REPO, load_fixture, replay_call
from whytypedtools_eval.github import GitHubClient
from whytypedtools_eval.sandbox.models import load_seed
from whytypedtools_eval.tools.base import EXCERPT_CHARS, ToolContext
from whytypedtools_eval.tools.registry import call_tool

SEED = load_seed(SEED_FILE)
SUMMARY_KEYS = {
    "number", "title", "state", "state_reason", "labels", "comments",
    "created_at", "updated_at", "body_excerpt", "body_truncated",
}


def run(mock_api, name):
    result, replayer = replay_call(mock_api, load_fixture("list_issues", name))
    assert replayer.done, "not every recorded request was replayed"
    return result


def seed_titles(pred):
    return {i.title for i in SEED.issues if pred(i)}


def titles(result):
    return {i["title"] for i in result["result"]["issues"]}


# -- happy paths and filters ---------------------------------------------------


def test_default_lists_open_issues(mock_api):
    result = run(mock_api, "list_default")
    assert result["ok"]
    issues = result["result"]["issues"]
    assert issues and all(i["state"] == "open" for i in issues)
    assert titles(result) <= seed_titles(lambda i: i.state == "open")
    assert result["result"]["returned"] == len(issues) <= 20


def test_closed_filter(mock_api):
    result = run(mock_api, "list_closed")
    assert titles(result) == seed_titles(lambda i: i.state == "closed")
    assert all(i["state_reason"] in ("completed", "not_planned") for i in result["result"]["issues"])


def test_single_label_across_states(mock_api):
    result = run(mock_api, "list_all_bug")
    assert titles(result) == seed_titles(lambda i: "bug" in i.labels)


def test_multiple_labels_are_and(mock_api):
    result = run(mock_api, "list_bug_and_api")
    expected = seed_titles(lambda i: i.state == "open" and {"bug", "api"} <= set(i.labels))
    assert titles(result) == expected
    assert len(expected) >= 2


def test_sort_by_comments(mock_api):
    # KNOWN WEAK: only checks that the returned page is non-increasing. In a real
    # recording GitHub ranked three 1-comment issues above a 2-comment one, so the
    # "top N by comments" is not trustworthy (README "Findings"). Strengthen to a
    # true top-N check after the re-recording experiment; if the order is still
    # wrong, sort=comments will be removed from the tool schema instead.
    issues = run(mock_api, "list_most_commented")["result"]["issues"]
    counts = [i["comments"] for i in issues]
    assert counts == sorted(counts, reverse=True)
    assert len(issues) == 3


def test_ascending_creation_order(mock_api):
    issues = run(mock_api, "list_oldest_first")["result"]["issues"]
    numbers = [i["number"] for i in issues]
    assert numbers == sorted(numbers)
    assert len(issues) == 5


def test_pagination_collects_across_pages(mock_api):
    fixture = load_fixture("list_issues", "list_paginated")
    assert len(fixture["exchanges"]) >= 3
    result = run(mock_api, "list_paginated")
    assert result["result"]["returned"] == 8
    assert result["result"]["truncated"] is True
    assert len({i["number"] for i in result["result"]["issues"]}) == 8


# -- empty results and label hints ---------------------------------------------


def test_empty_result_with_existing_labels_has_no_hint(mock_api):
    result = run(mock_api, "list_empty_known_labels")
    assert result["result"]["issues"] == []
    assert "hint" not in result["result"]


def test_labels_are_only_fetched_for_empty_labelled_results():
    def paths(name):
        return [e["request"]["path"] for e in load_fixture("list_issues", name)["exchanges"]]

    labels_path = f"/repos/{FIXTURE_REPO}/labels"
    assert labels_path in paths("list_empty_known_labels")
    assert labels_path in paths("list_unknown_label")
    for name in ("list_all_bug", "list_bug_and_api", "list_default"):
        assert labels_path not in paths(name)


def test_unknown_label_gets_hint(mock_api):
    result = run(mock_api, "list_unknown_label")
    hint = result["result"]["hint"]
    assert hint["unknown_labels"] == ["bugs"]
    assert "bug" in hint["available_labels"]


def test_label_lookup_is_cached_per_context(mock_api):
    route_issues = mock_api.get(f"/repos/{FIXTURE_REPO}/issues").mock(return_value=httpx.Response(200, json=[]))
    route_labels = mock_api.get(f"/repos/{FIXTURE_REPO}/labels").mock(
        return_value=httpx.Response(200, json=[{"name": "bug", "color": "d73a4a"}])
    )
    with GitHubClient("github_pat_fixture", FIXTURE_REPO, search_interval=0) as client:
        ctx = ToolContext(client, FIXTURE_REPO)
        for _ in range(3):
            assert call_tool(ctx, "list_issues", {"labels": ["nope"]})["result"]["hint"]
        # Normal requests with results never touch the labels endpoint.
    assert route_issues.call_count == 3
    assert route_labels.call_count == 1


# -- errors --------------------------------------------------------------------


def test_auth_failure(mock_api):
    result = run(mock_api, "list_auth_failed")
    assert result == {"ok": False, "error": result["error"]}
    assert result["error"]["type"] == "auth_failed"
    assert result["error"]["retryable"] is False


def test_not_found(mock_api):
    result = run(mock_api, "list_not_found")
    assert result["error"]["type"] == "not_found"


# -- output shape --------------------------------------------------------------


def test_pull_requests_are_excluded(mock_api):
    items = [
        {"number": 2, "title": "A PR", "state": "open", "labels": [], "pull_request": {}},
        {"number": 1, "title": "An issue", "state": "open", "labels": [], "body": "x"},
    ]
    mock_api.get(f"/repos/{FIXTURE_REPO}/issues").mock(return_value=httpx.Response(200, json=items))
    with GitHubClient("github_pat_fixture", FIXTURE_REPO) as client:
        result = call_tool(ToolContext(client, FIXTURE_REPO), "list_issues", {})
    assert [i["title"] for i in result["result"]["issues"]] == ["An issue"]


def test_output_is_compact(mock_api):
    issues = run(mock_api, "list_all_bug")["result"]["issues"]
    for issue in issues:
        assert set(issue) <= SUMMARY_KEYS
        assert len(issue["body_excerpt"]) <= EXCERPT_CHARS + 1
        assert len(issue["created_at"]) == 10
    long_ones = [i for i in issues if i["body_truncated"]]
    assert long_ones and all(i["body_excerpt"].endswith("…") for i in long_ones)


def test_exposure_paths_match_seed_design(mock_api):
    # The summary-exposed injection is visible in list results;
    # the full-body one is not (only its lead-in may be).
    by_title = {i["title"]: i for i in run(mock_api, "list_all_bug")["result"]["issues"]}
    for spec in (i for i in SEED.issues if i.safety_test):
        excerpt = by_title[spec.title]["body_excerpt"]
        visible = spec.safety_test.payload_marker in excerpt
        assert visible == (spec.safety_test.exposure == "summary"), spec.key


# -- input validation ----------------------------------------------------------


@pytest.mark.parametrize(
    "args, field",
    [
        ({"state": "opened"}, "state"),
        ({"max_results": 0}, "max_results"),
        ({"max_results": 51}, "max_results"),
        ({"labels": ["bug,api"]}, "labels.0"),
        ({"labels": [""]}, "labels.0"),
        ({"labels": ["x"] * 11}, "labels"),
        ({"sort": "title"}, "sort"),
        ({"query": "rate limit"}, "query"),  # search argument passed to the wrong tool
    ],
)
def test_invalid_input_is_rejected_before_any_request(mock_api, args, field):
    route = mock_api.route()
    with GitHubClient("github_pat_fixture", FIXTURE_REPO) as client:
        result = call_tool(ToolContext(client, FIXTURE_REPO), "list_issues", args)
    assert result["ok"] is False
    assert result["error"]["type"] == "invalid_input"
    assert field in result["error"]["message"]
    assert route.call_count == 0
