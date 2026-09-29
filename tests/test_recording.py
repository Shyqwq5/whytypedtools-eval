"""The fixture recorder must strip anything sensitive before a fixture hits disk."""

import gzip
import json

import httpx
import pytest

from tests.recording import (
    FIXTURE_REPO,
    RecordingTransport,
    UnsafeFixtureError,
    assert_safe,
    sanitize_exchange,
)

REAL = "RealOwner/my-sandbox"


def _exchange(json_body, headers=None, url=None):
    request = httpx.Request(
        "GET",
        url or f"https://api.github.com/repos/{REAL}/issues?state=all",
        headers={"Authorization": "Bearer github_pat_REALSECRET123456"},
    )
    response = httpx.Response(200, json=json_body, headers=headers or {})
    return sanitize_exchange(request, response, REAL)


def test_request_headers_are_never_stored():
    ex = _exchange([])
    assert set(ex["request"]) == {"method", "path", "params"}
    assert "REALSECRET" not in str(ex)


def test_only_allowlisted_response_headers_are_kept():
    ex = _exchange([], headers={
        "Link": f'<https://api.github.com/repos/{REAL}/issues?page=2>; rel="next"',
        "X-RateLimit-Remaining": "4999",
        "Set-Cookie": "session=abc",
        "X-GitHub-Request-Id": "ABCD:1234",
        "X-OAuth-Scopes": "repo",
    })
    assert set(ex["response"]["headers"]) == {"content-type", "link", "x-ratelimit-remaining"}
    assert FIXTURE_REPO in ex["response"]["headers"]["link"]


def test_issue_fields_are_allowlisted_and_repo_replaced():
    issue = {
        "number": 1, "title": "t", "body": "b", "state": "open", "labels": [{"name": "bug", "id": 5, "node_id": "x"}],
        "user": {"login": "RealOwner", "avatar_url": "https://avatars/..."},
        "assignee": {"login": "someone"}, "reactions": {"+1": 1},
        "html_url": f"https://github.com/{REAL}/issues/1",
        "repository_url": f"https://api.github.com/repos/{REAL}",
        "node_id": "I_kw", "author_association": "OWNER",
    }
    item = _exchange([issue])["response"]["json"][0]
    assert set(item) == {"number", "title", "body", "state", "labels", "html_url", "repository_url"}
    assert item["labels"] == [{"name": "bug", "color": None, "description": None}]
    assert item["html_url"] == f"https://github.com/{FIXTURE_REPO}/issues/1"
    assert "RealOwner" not in str(item)


def test_search_query_param_is_rewritten():
    ex = _exchange({"total_count": 0, "items": []},
                   url=f"https://api.github.com/search/issues?q=repo:{REAL}+is:issue+x")
    assert ex["request"]["params"]["q"] == f"repo:{FIXTURE_REPO} is:issue x"


def test_assert_safe_catches_leftovers():
    with pytest.raises(UnsafeFixtureError, match="secret"):
        assert_safe({"x": "abc TOPSECRET abc"}, ["TOPSECRET"], "realowner")
    with pytest.raises(UnsafeFixtureError, match="token-like"):
        assert_safe({"x": "ghp_abcdefghijklmnop"}, [], "realowner")
    with pytest.raises(UnsafeFixtureError, match="owner"):
        assert_safe({"x": "mentioned by realowner here"}, [], "RealOwner")
    assert_safe({"x": f"{FIXTURE_REPO} fine"}, ["TOPSECRET"], "realowner")


def test_recording_transport_handles_compressed_responses():
    # Regression: GitHub gzips most responses. The recorder used to hand the
    # decoded body back with the original Content-Encoding header, so the client
    # failed with DecodingError and nothing was recorded.
    items = [{"number": 1, "title": "t", "state": "open", "labels": []}]

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip", "content-type": "application/json"},
            content=gzip.compress(json.dumps(items).encode()),
        )

    rec = RecordingTransport(httpx.MockTransport(handler), REAL)
    with httpx.Client(transport=rec, base_url="https://api.github.com") as client:
        assert client.get(f"/repos/{REAL}/issues").json() == items
    assert len(rec.exchanges) == 1
    assert rec.exchanges[0]["response"]["json"] == items
    assert "content-encoding" not in rec.exchanges[0]["response"]["headers"]


# -- declared outcomes ---------------------------------------------------------


def _load_recorder_script():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "record_fixtures.py"
    spec = importlib.util.spec_from_file_location("record_fixtures", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_unexpected_error_outcome_is_rejected():
    from tests.recording import Scenario, UnexpectedOutcomeError, record_scenario

    def handler(request):
        return httpx.Response(500, json={"message": "Server Error"})

    with pytest.raises(UnexpectedOutcomeError, match="expected 'ok' but got 'upstream_error'"):
        record_scenario(Scenario("x", "list_issues"), token="github_pat_fixture", real_repo=REAL,
                        source="recorded", inner=httpx.MockTransport(handler))


def test_unexpected_success_is_rejected():
    from tests.recording import Scenario, UnexpectedOutcomeError, record_scenario

    def handler(request):
        return httpx.Response(200, json=[])

    with pytest.raises(UnexpectedOutcomeError, match="expected 'auth_failed' but got 'ok'"):
        record_scenario(Scenario("x", "list_issues", auth="invalid", expect="auth_failed"),
                        token="github_pat_fixture", real_repo=REAL, source="recorded",
                        inner=httpx.MockTransport(handler))


def test_one_bad_scenario_means_nothing_is_written(tmp_path, monkeypatch, capsys):
    import tests.recording as recording
    from tests.recording import SCENARIOS

    monkeypatch.setattr(recording, "FIXTURES_DIR", tmp_path)
    script = _load_recorder_script()
    good = SCENARIOS[0]
    bad = recording.Scenario("bad", "list_issues", expect="not_found")  # will actually succeed
    with pytest.raises(SystemExit) as exc:
        script.record_fake([good, bad])
    assert "expected 'not_found' but got 'ok'" in str(exc.value)
    assert "No fixtures were written" in str(exc.value)
    assert list(tmp_path.rglob("*.json")) == []


def test_committed_fixtures_match_their_declared_outcome():
    from tests.recording import SCENARIOS, all_fixtures, outcome

    declared = {s.name: s.expect for s in SCENARIOS}
    for fixture in all_fixtures():
        if fixture["source"] == "synthetic":
            continue
        assert fixture["expect"] == declared[fixture["scenario"]], fixture["scenario"]
        assert outcome(fixture["result"]) == fixture["expect"], fixture["scenario"]


# -- URL-encoded identifiers ---------------------------------------------------


def test_url_encoded_repo_in_link_header_is_replaced():
    # Regression: search pagination Links carry the query percent-encoded
    # (repo%3AOwner%2Fname), which plain-text replacement missed.
    link = (f'<https://api.github.com/search/issues?q=repo%3A{REAL.replace("/", "%2F")}'
            '+is%3Aissue+x&per_page=2&page=2>; rel="next"')
    ex = _exchange({"total_count": 3, "items": []}, headers={"Link": link},
                   url=f"https://api.github.com/search/issues?q=repo:{REAL}+is:issue+x")
    text = json.dumps(ex)
    assert "RealOwner" not in text and "realowner" not in text.lower()
    assert "repo%3Asandbox-owner%2Fwhytypedtools-sandbox" in ex["response"]["headers"]["link"]


@pytest.mark.parametrize(
    "leak",
    [
        "q=repo%3ARealOwner%2Fmy-sandbox",      # single encoding
        "q=repo%253ARealOwner%252Fmy-sandbox",  # double encoding
        "q=repo:RealOwner+is:issue",            # form encoding
    ],
)
def test_safety_scan_decodes_before_checking(leak):
    with pytest.raises(UnsafeFixtureError, match="owner"):
        assert_safe({"link": leak}, [], "RealOwner")


def test_recorded_search_pagination_is_clean_and_replays(mock_api):
    from tests.fake_github import FakeGitHub
    from tests.recording import Scenario, record_scenario, replay_call

    fake = FakeGitHub(REAL)
    for i in range(5):
        fake.add_issue(f"rate limit issue {i}")
    scenario = Scenario("paged", "search_issues", {"query": "rate", "max_results": 3}, page_size=1)
    fixture = record_scenario(scenario, token="github_pat_fixture", real_repo=REAL, source="fake",
                              inner=httpx.MockTransport(fake.handler), sleep=lambda _: None)
    assert len(fixture["exchanges"]) == 3
    assert_safe(fixture, [], "RealOwner")  # must not raise

    result, replayer = replay_call(mock_api, fixture)
    assert replayer.done
    assert result == fixture["result"]
