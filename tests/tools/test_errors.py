import json
import logging

import httpx
import pytest

from tests.recording import FIXTURE_REPO, FIXTURE_TOKEN, INVALID_TOKEN, all_fixtures, load_fixture, replay_call
from whytypedtools_eval.github import GitHubClient
from whytypedtools_eval.tools.base import ToolContext
from whytypedtools_eval.tools.registry import call_tool


def run_synthetic(mock_api, name, sleeps=None):
    result, replayer = replay_call(mock_api, load_fixture("synthetic", name), sleeps=sleeps)
    assert replayer.done
    return result


def test_forbidden(mock_api):
    error = run_synthetic(mock_api, "forbidden_403")["error"]
    assert error == {
        "type": "forbidden",
        "message": error["message"],
        "retryable": False,
        "retry_after_seconds": None,
    }
    assert "permission" in error["message"]


def test_search_secondary_rate_limit_fails_fast(mock_api):
    sleeps: list[float] = []
    error = run_synthetic(mock_api, "search_rate_limited_429", sleeps)["error"]
    assert error["type"] == "rate_limited"
    assert error["retryable"] is True
    assert error["retry_after_seconds"] == 60
    assert sleeps == []  # the tool did not block


def test_primary_rate_limit_fails_fast(mock_api):
    sleeps: list[float] = []
    error = run_synthetic(mock_api, "primary_rate_limited_403", sleeps)["error"]
    assert error["type"] == "rate_limited"
    assert error["retry_after_seconds"] > 60
    assert sleeps == []


def test_server_error_is_retryable(mock_api):
    error = run_synthetic(mock_api, "server_error_500")["error"]
    assert error["type"] == "upstream_error"
    assert error["retryable"] is True


def _call_list(mock_api, **mock):
    mock_api.get(f"/repos/{FIXTURE_REPO}/issues").mock(**mock)
    with GitHubClient(FIXTURE_TOKEN, FIXTURE_REPO) as client:
        return call_tool(ToolContext(client, FIXTURE_REPO), "list_issues", {})


@pytest.mark.parametrize("exc", [httpx.ConnectError("boom"), httpx.ReadTimeout("slow")])
def test_transport_errors_are_retryable(mock_api, exc):
    error = _call_list(mock_api, side_effect=exc)["error"]
    assert error["type"] == "upstream_error"
    assert error["retryable"] is True
    assert "Could not reach GitHub" in error["message"]


def test_undecodable_body_is_not_retryable(mock_api):
    # Claims gzip but isn't: the exact failure the broken recorder produced.
    # A raw stream (not content=) so decoding happens in the client, as with a real response.
    resp = httpx.Response(200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(b"[]"))
    error = _call_list(mock_api, return_value=resp)["error"]
    assert error["type"] == "invalid_response"
    assert error["retryable"] is False
    assert "Could not reach" not in error["message"]


def test_non_json_body_is_not_retryable(mock_api):
    resp = httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>maintenance</html>")
    error = _call_list(mock_api, return_value=resp)["error"]
    assert error["type"] == "invalid_response"
    assert error["retryable"] is False


def test_unknown_tool(mock_api):
    with GitHubClient(FIXTURE_TOKEN, FIXTURE_REPO) as client:
        result = call_tool(ToolContext(client, FIXTURE_REPO), "delete_repo", {})
    assert result["error"]["type"] == "unknown_tool"
    assert "list_issues" in result["error"]["message"]


def _error_fixtures():
    return [(f["tool"] if f["source"] != "synthetic" else "synthetic", f["scenario"])
            for f in all_fixtures()
            if f["source"] == "synthetic" or not f.get("result", {}).get("ok", True)]


@pytest.mark.parametrize("folder, name", _error_fixtures())
def test_errors_never_leak_token_or_headers(mock_api, caplog, folder, name):
    caplog.set_level(logging.DEBUG)
    result, _ = replay_call(mock_api, load_fixture(folder, name))
    text = json.dumps(result) + caplog.text
    assert result["ok"] is False
    for secret in (FIXTURE_TOKEN, INVALID_TOKEN, "Bearer", "Authorization"):
        assert secret not in text


def test_recorded_fixtures_contain_no_request_headers():
    for fixture in all_fixtures():
        for exchange in fixture["exchanges"]:
            assert set(exchange["request"]) == {"method", "path", "params"}
            assert "authorization" not in {h.lower() for h in exchange["response"]["headers"]}
