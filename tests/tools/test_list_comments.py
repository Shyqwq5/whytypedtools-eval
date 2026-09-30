"""Tool-specific tests for list_comments.

The shared gate (tests/tools/test_tool_gate.py) already runs every check on this
tool: validation, replay of recorded responses and errors, field descriptions,
sandbox scoping, writes, MCP listing. Test what is specific to list_comments here.
"""

import pytest

from tests.conftest import REPO
from tests.recording import all_fixtures, replay_call
from whytypedtools_eval.tools.base import ToolContext
from whytypedtools_eval.tools.list_comments.tool import MAX_COMMENT_CHARS
from whytypedtools_eval.tools.registry import call_tool


@pytest.mark.parametrize("fixture", [f for f in all_fixtures() if f["tool"] == "list_comments"],
                         ids=lambda f: f["scenario"])
def test_replay_matches_the_recording(mock_api, fixture):
    result, replayer = replay_call(mock_api, fixture)
    assert replayer.done
    assert result == fixture["result"]


def call(client, **args):
    return call_tool(ToolContext(client, REPO), "list_comments", args)


def test_only_comments_oldest_first_with_the_issue_title(fake, client):
    n = fake.add_issue("Crash on start", body="The issue body is not returned.")
    fake.add_comment(n, "first")
    fake.add_comment(n, "second")
    result = call(client, number=n)["result"]
    assert (result["title"], result["comments_total"], result["returned"], result["truncated"]) == (
        "Crash on start", 2, 2, False)
    assert [c["body"] for c in result["comments"]] == ["first", "second"]
    assert "body" not in result and "The issue body" not in str(result)


def test_since_filters_and_is_sent_as_a_utc_timestamp(fake, client, mock_api):
    n = fake.add_issue("Old discussion")
    fake.add_comment(n, "old")
    assert call(client, number=n, since="2099-01-01")["result"]["comments"] == []
    assert [c["body"] for c in call(client, number=n, since="2000-01-01")["result"]["comments"]] == ["old"]
    sent = [c for c in mock_api.calls if c.request.url.path.endswith("/comments")]
    assert sent[0].request.url.params["since"] == "2099-01-01T00:00:00Z"


def test_max_results_sets_truncated(fake, client):
    n = fake.add_issue("Busy")
    for i in range(3):
        fake.add_comment(n, f"c{i}")
    result = call(client, number=n, max_results=2)["result"]
    assert (result["returned"], result["truncated"], result["comments_total"]) == (2, True, 3)


def test_no_comments_means_no_comments_request(fake, client, mock_api):
    n = fake.add_issue("Quiet")
    assert call(client, number=n)["result"]["comments"] == []
    assert [c.request.url.path for c in mock_api.calls] == [f"/repos/{REPO}/issues/{n}"]


def test_long_comments_are_cut(fake, client):
    n = fake.add_issue("Long")
    fake.add_comment(n, "x" * (MAX_COMMENT_CHARS + 10))
    comment = call(client, number=n)["result"]["comments"][0]
    assert comment["body_truncated"] is True and len(comment["body"]) == MAX_COMMENT_CHARS + 1


def test_pull_requests_are_not_issues(fake, client):
    n = fake.add_issue("A pull request")
    fake.issues[n]["pull_request"] = {}
    error = call(client, number=n)["error"]
    assert error["type"] == "not_found" and "pull request" in error["message"]


def test_bad_since_is_invalid_input(client):
    error = call(client, number=1, since="last week")["error"]
    assert error["type"] == "invalid_input" and "since" in error["message"]
