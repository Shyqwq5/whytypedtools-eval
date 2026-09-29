import httpx
import pytest

from tests.conftest import API, REPO
from whytypedtools_eval.github import GitHubClient, GitHubError, RateLimitError
from whytypedtools_eval.sandbox.guard import SandboxGuardError


def test_paginate_follows_link_header(client, mock_api):
    page2 = f"{API}/repos/{REPO}/labels?per_page=100&page=2"
    page3 = f"{API}/repos/{REPO}/labels?per_page=100&page=3"
    route = mock_api.get(f"/repos/{REPO}/labels").mock(
        side_effect=[
            httpx.Response(200, json=[{"n": 1}, {"n": 2}],
                           headers={"Link": f'<{page2}>; rel="next", <{page3}>; rel="last"'}),
            httpx.Response(200, json=[{"n": 3}], headers={"Link": f'<{page3}>; rel="next"'}),
            httpx.Response(200, json=[{"n": 4}]),
        ]
    )
    assert [i["n"] for i in client.paginate(f"repos/{REPO}/labels")] == [1, 2, 3, 4]
    assert route.call_count == 3
    assert route.calls[0].request.url.params["per_page"] == "100"
    assert route.calls[1].request.url.params["page"] == "2"


def test_paginate_with_fake_server_small_pages(client, fake):
    fake.max_page_size = 3
    for i in range(10):
        fake.add_issue(f"issue {i}")
    fake.add_issue("a pull request", pull_request=True)
    items = list(client.paginate(f"repos/{REPO}/issues", {"state": "all"}))
    assert len(items) == 11
    assert len([c for c in fake.calls if c[0] == "GET"]) == 4


def test_paginate_refuses_foreign_next_url(client, mock_api):
    mock_api.get(f"/repos/{REPO}/labels").mock(
        return_value=httpx.Response(200, json=[], headers={"Link": '<https://evil.example/x>; rel="next"'})
    )
    with pytest.raises(SandboxGuardError, match="outside"):
        list(client.paginate(f"repos/{REPO}/labels"))


def test_retry_after_is_respected(client, mock_api, sleeps):
    route = mock_api.get(f"/repos/{REPO}/labels").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}, json={"message": "slow down"}),
            httpx.Response(200, json=[]),
        ]
    )
    client.get(f"repos/{REPO}/labels")
    assert route.call_count == 2
    assert sleeps == [7.0]


def test_primary_rate_limit_waits_until_reset(mock_api, sleeps):
    c = GitHubClient("t", REPO, write_interval=0, sleep=sleeps.append, clock=lambda: 1000.0)
    mock_api.post(f"/repos/{REPO}/issues").mock(
        side_effect=[
            httpx.Response(403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1030"},
                           json={"message": "API rate limit exceeded"}),
            httpx.Response(201, json={"number": 1}),
        ]
    )
    assert c.post(f"repos/{REPO}/issues", {"title": "x"}) == {"number": 1}
    assert sleeps == [31.0]


def test_secondary_rate_limit_without_headers(client, mock_api, sleeps):
    mock_api.post(f"/repos/{REPO}/issues").mock(
        side_effect=[
            httpx.Response(403, json={"message": "You have exceeded a secondary rate limit."}),
            httpx.Response(201, json={"number": 1}),
        ]
    )
    client.post(f"repos/{REPO}/issues", {"title": "x"})
    assert sleeps == [60.0]


def test_plain_403_is_not_retried(client, mock_api, sleeps):
    route = mock_api.post(f"/repos/{REPO}/issues").mock(
        return_value=httpx.Response(403, json={"message": "Resource not accessible by personal access token"})
    )
    with pytest.raises(GitHubError) as exc:
        client.post(f"repos/{REPO}/issues", {"title": "x"})
    assert exc.value.status == 403
    assert route.call_count == 1
    assert sleeps == []


def test_rate_limit_retries_exhausted(mock_api, sleeps):
    c = GitHubClient("t", REPO, max_retries=2, write_interval=0, sleep=sleeps.append)
    route = mock_api.get(f"/repos/{REPO}/labels").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "1"})
    )
    with pytest.raises(RateLimitError):
        c.get(f"repos/{REPO}/labels")
    assert route.call_count == 3


def test_writes_are_paced(mock_api, sleeps):
    now = [100.0]
    c = GitHubClient("t", REPO, write_interval=1.0, sleep=sleeps.append, clock=lambda: now[0])
    mock_api.post(f"/repos/{REPO}/issues").mock(return_value=httpx.Response(201, json={}))
    c.post(f"repos/{REPO}/issues", {})
    now[0] += 0.25
    c.post(f"repos/{REPO}/issues", {})
    assert sleeps == [0.75]


def test_auth_header_sent(client, mock_api):
    route = mock_api.get(f"/repos/{REPO}/labels").mock(return_value=httpx.Response(200, json=[]))
    client.get(f"repos/{REPO}/labels")
    assert route.calls[0].request.headers["Authorization"] == "Bearer github_pat_test"


def test_default_policy_is_patient(mock_api, sleeps):
    # Seed/reset behaviour: long waits are slept (capped by max_wait), then retried.
    c = GitHubClient("t", REPO, write_interval=0, max_wait=900, sleep=sleeps.append)
    route = mock_api.get(f"/repos/{REPO}/labels").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "120"}), httpx.Response(200, json=[])]
    )
    c.get(f"repos/{REPO}/labels")
    assert sleeps == [120.0]
    assert route.call_count == 2


def test_long_wait_is_capped_by_max_wait(mock_api, sleeps):
    c = GitHubClient("t", REPO, write_interval=0, max_wait=30, sleep=sleeps.append)
    mock_api.get(f"/repos/{REPO}/labels").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "600"}), httpx.Response(200, json=[])]
    )
    c.get(f"repos/{REPO}/labels")
    assert sleeps == [30.0]


def test_fail_fast_raises_without_sleeping(mock_api, sleeps):
    c = GitHubClient("t", REPO, write_interval=0, fail_fast_after=5, sleep=sleeps.append)
    route = mock_api.get(f"/repos/{REPO}/labels").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "42"})
    )
    with pytest.raises(RateLimitError) as exc:
        c.get(f"repos/{REPO}/labels")
    assert exc.value.retry_after == 42.0
    assert exc.value.status == 429
    assert sleeps == []
    assert route.call_count == 1


def test_fail_fast_uses_reset_header(mock_api, sleeps):
    c = GitHubClient("t", REPO, write_interval=0, fail_fast_after=5, sleep=sleeps.append,
                     clock=lambda: 1000.0)
    mock_api.get(f"/repos/{REPO}/labels").mock(
        return_value=httpx.Response(403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1300"},
                                    json={"message": "API rate limit exceeded"})
    )
    with pytest.raises(RateLimitError) as exc:
        c.get(f"repos/{REPO}/labels")
    assert exc.value.retry_after == 301.0
    assert sleeps == []


def test_fail_fast_still_waits_out_short_limits(mock_api, sleeps):
    c = GitHubClient("t", REPO, write_interval=0, fail_fast_after=5, sleep=sleeps.append)
    mock_api.get(f"/repos/{REPO}/labels").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(200, json=[])]
    )
    assert c.get(f"repos/{REPO}/labels") == []
    assert sleeps == [2.0]


def test_exhausted_retries_report_retry_after(mock_api, sleeps):
    c = GitHubClient("t", REPO, max_retries=1, write_interval=0, sleep=sleeps.append)
    mock_api.get(f"/repos/{REPO}/labels").mock(return_value=httpx.Response(429, headers={"Retry-After": "3"}))
    with pytest.raises(RateLimitError) as exc:
        c.get(f"repos/{REPO}/labels")
    assert exc.value.retry_after == 3.0


def test_search_requests_are_paced_separately(mock_api, sleeps):
    now = [0.0]
    c = GitHubClient("t", REPO, write_interval=0, search_interval=2.0, sleep=sleeps.append, clock=lambda: now[0])
    mock_api.get("/search/issues").mock(return_value=httpx.Response(200, json={"items": []}))
    mock_api.get(f"/repos/{REPO}/labels").mock(return_value=httpx.Response(200, json=[]))
    c.get("search/issues", {"q": "x"})
    c.get(f"repos/{REPO}/labels")  # non-search reads are not paced
    now[0] += 0.5
    c.get("search/issues", {"q": "y"})
    assert sleeps == [1.5]


def test_422_detail_includes_github_errors(client, mock_api):
    mock_api.get("/search/issues").mock(return_value=httpx.Response(
        422, json={"message": "Validation Failed", "errors": [{"message": "Bad query", "code": "invalid"}]}))
    with pytest.raises(GitHubError) as exc:
        client.get("search/issues", {"q": "x"})
    assert exc.value.detail == "Validation Failed (Bad query)"


def test_tool_context_uses_fail_fast_client(settings):
    from whytypedtools_eval.tools.base import TOOL_FAIL_FAST_AFTER_S, ToolContext

    ctx = ToolContext.from_settings(settings)
    try:
        assert ctx.client._fail_fast_after == TOOL_FAIL_FAST_AFTER_S
        assert ctx.sandbox_repo == REPO
    finally:
        ctx.client.close()
