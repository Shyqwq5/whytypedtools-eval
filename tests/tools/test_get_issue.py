from tests.conftest import REPO, SEED_FILE
from tests.recording import load_fixture, replay_call
from whytypedtools_eval.sandbox.models import load_seed
from whytypedtools_eval.tools.base import ToolContext
from whytypedtools_eval.tools.get_issue.tool import MAX_BODY_CHARS
from whytypedtools_eval.tools.registry import call_tool

SEED = {i.key: i for i in load_seed(SEED_FILE).issues}


def run(mock_api, name):
    result, replayer = replay_call(mock_api, load_fixture("get_issue", name))
    assert replayer.done, "not every recorded request was replayed"
    return result


def test_full_body_and_comments(mock_api):
    issue = run(mock_api, "get_with_comments")["result"]
    seed = SEED["rate-limit-429"]
    assert issue["title"] == seed.title
    assert issue["body"] == seed.body.strip()
    assert [c["body"] for c in issue["comments"]] == [c.body for c in seed.comments]
    assert issue["comments_total"] == 2 and issue["comments_truncated"] is False
    assert set(issue["comments"][0]) == {"created_at", "body", "body_truncated"}


def test_comments_can_be_skipped(mock_api):
    fixture = load_fixture("get_issue", "get_without_comments_flag")
    assert len(fixture["exchanges"]) == 1  # no comments request
    issue = run(mock_api, "get_without_comments_flag")["result"]
    assert issue["comments"] == [] and issue["comments_total"] == 2
    assert issue["comments_truncated"] is False


def test_no_comments_means_no_comments_request(mock_api):
    assert len(load_fixture("get_issue", "get_no_comments")["exchanges"]) == 1
    assert run(mock_api, "get_no_comments")["result"]["comments"] == []


def test_full_body_exposes_payload_past_the_excerpt(mock_api):
    # The injection issue's payload sits past the 300-char excerpt; get_issue shows it.
    body = run(mock_api, "get_injection_full_body")["result"]["body"]
    marker = SEED["injection-close-all"].safety_test.payload_marker
    assert marker in body
    assert body.index(marker) > 300


def test_not_found(mock_api):
    assert run(mock_api, "get_not_found")["error"]["type"] == "not_found"


def test_pull_request_is_rejected(fake, client):
    n = fake.add_issue("A PR", pull_request=True)
    result = call_tool(ToolContext(client, REPO), "get_issue", {"number": n})
    assert result["error"]["type"] == "not_found"
    assert "pull request" in result["error"]["message"]


def test_long_body_and_comment_limit(fake, client):
    n = fake.add_issue("Long", body="x" * (MAX_BODY_CHARS + 50))
    for i in range(4):
        fake.add_comment(n, f"c{i}")
    issue = call_tool(ToolContext(client, REPO), "get_issue", {"number": n, "max_comments": 3})["result"]
    assert issue["body_truncated"] is True and len(issue["body"]) == MAX_BODY_CHARS + 1
    assert [c["body"] for c in issue["comments"]] == ["c0", "c1", "c2"]
    assert issue["comments_truncated"] is True


def test_invalid_number(client):
    result = call_tool(ToolContext(client, REPO), "get_issue", {"number": 0})
    assert result["error"]["type"] == "invalid_input"
