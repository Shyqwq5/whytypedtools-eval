"""Wait for GitHub's search index to catch up with the repo's actual issues.

The search index lags behind writes. After seed/reset, evals that use
search_issues would otherwise run against stale data. The REST issues list is
the source of truth; we poll search until every issue's fingerprint matches.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from whytypedtools_eval.github import GitHubClient

Fingerprint = tuple[str, str, tuple[str, ...], str]


class SearchIndexTimeout(TimeoutError):
    pass


def fingerprint(item: dict[str, Any]) -> Fingerprint:
    # updated_at changes on any edit (including the body), so it also catches
    # body changes that the other fields would miss.
    labels = tuple(sorted(lbl["name"].lower() for lbl in item.get("labels", [])))
    return (item["title"], item["state"], labels, item.get("updated_at") or "")


def rest_fingerprints(client: GitHubClient, repo: str) -> dict[int, Fingerprint]:
    items = client.paginate(f"repos/{repo}/issues", {"state": "all"})
    return {i["number"]: fingerprint(i) for i in items if "pull_request" not in i}


def search_fingerprints(client: GitHubClient, repo: str) -> dict[int, Fingerprint]:
    found: dict[int, Fingerprint] = {}
    url: str | None = "search/issues"
    params: dict[str, Any] | None = {"q": f"repo:{repo} is:issue", "per_page": 100}
    while url:
        resp = client.request("GET", url, params=params)
        for item in resp.json().get("items", []):
            found[item["number"]] = fingerprint(item)
        url = resp.links.get("next", {}).get("url")
        params = None
    return found


def index_diff(expected: dict[int, Fingerprint], indexed: dict[int, Fingerprint]) -> list[int]:
    """Issue numbers whose indexed state differs from the REST state."""
    numbers = expected.keys() | indexed.keys()
    return sorted(n for n in numbers if expected.get(n) != indexed.get(n))


def wait_for_search_index(
    client: GitHubClient,
    repo: str,
    *,
    timeout: float = 180.0,
    interval: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    emit: Callable[[str], None] = print,
) -> None:
    """Block until search results match the REST issue list, or raise SearchIndexTimeout."""
    expected = rest_fingerprints(client, repo)
    deadline = clock() + timeout
    while True:
        stale = index_diff(expected, search_fingerprints(client, repo))
        if not stale:
            emit("search index is up to date")
            return
        if clock() + interval > deadline:
            raise SearchIndexTimeout(
                f"search index still stale after {timeout:.0f}s for issues {stale[:10]}"
            )
        emit(f"search index stale for {len(stale)} issue(s); retrying in {interval:.0f}s")
        sleep(interval)
