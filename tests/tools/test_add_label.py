import pytest

from tests.conftest import REPO
from tests.recording import load_fixture, replay_call
from whytypedtools_eval.tools.base import ToolContext
from whytypedtools_eval.tools.registry import TOOLS, call_tool, list_tools


def run(mock_api, name):
    result, replayer = replay_call(mock_api, load_fixture("add_label", name))
    assert replayer.done, "not every recorded request was replayed"
    return result


def test_fixtures_come_from_the_fake_only():
    for name in ("add_label_new", "add_label_case_insensitive", "add_label_already_present",
                 "add_label_unknown", "add_label_not_found"):
        assert load_fixture("add_label", name)["source"] == "fake"


def test_adds_label(mock_api):
    assert run(mock_api, "add_label_new")["result"] == {
        "number": 21, "added": ["bug"], "already_present": [], "labels": ["bug"],
    }


def test_names_are_matched_case_insensitively(mock_api):
    fixture = load_fixture("add_label", "add_label_case_insensitive")
    assert fixture["exchanges"][-1]["request"]["method"] == "POST"
    result = run(mock_api, "add_label_case_insensitive")["result"]
    assert result["added"] == ["bug", "question"]


def test_already_present_makes_no_write(mock_api):
    fixture = load_fixture("add_label", "add_label_already_present")
    assert [e["request"]["method"] for e in fixture["exchanges"]] == ["GET", "GET"]
    result = run(mock_api, "add_label_already_present")["result"]
    assert result["added"] == [] and result["already_present"] == ["bug"]


def test_unknown_label_is_not_created(mock_api):
    fixture = load_fixture("add_label", "add_label_unknown")
    assert all(e["request"]["method"] == "GET" for e in fixture["exchanges"])
    error = run(mock_api, "add_label_unknown")["error"]
    assert error["type"] == "invalid_input"
    assert "urgent" in error["message"] and "Available labels: api, bug" in error["message"]


def test_missing_issue(mock_api):
    assert run(mock_api, "add_label_not_found")["error"]["type"] == "not_found"


def test_dry_run_records_but_does_not_write(fake, client):
    fake.labels["bug"] = {"name": "bug", "color": "d73a4a", "description": ""}
    n = fake.add_issue("Login flickers")
    ctx = ToolContext(client, REPO, write_mode="dry_run")
    result = call_tool(ctx, "add_label", {"number": n, "labels": ["bug"]})
    assert result["result"] == {"number": n, "added": ["bug"], "already_present": [], "labels": ["bug"]}
    assert fake.writes == []
    assert ctx.write_log == [{"method": "POST", "path": f"repos/{REPO}/issues/{n}/labels",
                              "body": {"labels": ["bug"]}, "executed": False}]


def test_live_write_is_logged_as_executed(fake, client):
    fake.labels["bug"] = {"name": "bug", "color": "d73a4a", "description": ""}
    n = fake.add_issue("Login flickers")
    ctx = ToolContext(client, REPO)
    call_tool(ctx, "add_label", {"number": n, "labels": ["bug"]})
    assert [w[0] for w in fake.writes] == ["POST"]
    assert ctx.write_log[0]["executed"] is True
    assert fake.issues[n]["labels"] == [{"name": "bug"}]


def test_failed_live_write_is_logged_as_attempted(fake, client, mock_api):
    fake.labels["bug"] = {"name": "bug", "color": "d73a4a", "description": ""}
    n = fake.add_issue("x")
    ctx = ToolContext(client, REPO)
    original = fake.handler

    def deny_posts(request):
        if request.method == "POST":
            import httpx
            return httpx.Response(403, json={"message": "Resource not accessible by personal access token"})
        return original(request)

    mock_api.routes.clear()
    mock_api.route().mock(side_effect=deny_posts)
    result = call_tool(ctx, "add_label", {"number": n, "labels": ["bug"]})
    assert result["error"]["type"] == "forbidden"
    assert ctx.write_log[0]["executed"] is False


@pytest.mark.parametrize("labels", [[], ["a"] * 6, ["a,b"]])
def test_label_list_is_validated(client, labels):
    result = call_tool(ToolContext(client, REPO), "add_label", {"number": 1, "labels": labels})
    assert result["error"]["type"] == "invalid_input"


def test_only_add_label_is_a_write_tool():
    assert [name for name, spec in TOOLS.items() if not spec.read_only] == ["add_label"]
    assert "changes the repository" in {t["name"]: t for t in list_tools()}["add_label"]["description"]
