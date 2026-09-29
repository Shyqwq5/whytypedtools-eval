"""The fixture recorder must strip anything sensitive before a fixture hits disk."""

import httpx
import pytest

from tests.recording import FIXTURE_REPO, UnsafeFixtureError, assert_safe, sanitize_exchange

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
