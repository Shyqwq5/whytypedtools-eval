"""Minimal GitHub REST client: auth, pagination, rate limits and the write guard."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from whytypedtools_eval.sandbox.guard import READ_METHODS, SandboxGuardError, assert_write_allowed

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
# Used when GitHub signals a secondary rate limit without saying how long to wait.
SECONDARY_LIMIT_FALLBACK_S = 60.0


class GitHubError(RuntimeError):
    def __init__(self, status: int, method: str, path: str, message: str) -> None:
        super().__init__(f"GitHub {method} {path} failed with {status}: {message}")
        self.status = status


class RateLimitError(GitHubError):
    """Raised when rate limiting persists after all retries."""


class GitHubClient:
    """Synchronous GitHub client scoped to one sandbox repo for writes.

    - Every non-GET request is checked by `assert_write_allowed`.
    - `paginate` follows `Link: rel="next"` headers.
    - 429 and rate-limit 403 responses are retried after waiting for `Retry-After`
      or until `X-RateLimit-Reset`.
    - Writes are spaced at least `write_interval` seconds apart to avoid GitHub's
      secondary (content-creation) rate limit.
    """

    def __init__(
        self,
        token: str,
        sandbox_repo: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        max_retries: int = 5,
        write_interval: float = 1.0,
        max_wait: float = 900.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.sandbox_repo = sandbox_repo
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._write_interval = write_interval
        self._max_wait = max_wait
        self._sleep = sleep
        self._clock = clock
        self._last_write: float | None = None
        self._http = httpx.Client(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "whytypedtools-eval",
            },
            timeout=30.0,
            transport=transport,
        )

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # -- public API ---------------------------------------------------------

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.request("GET", path, params=params).json()

    def post(self, path: str, json: Any = None) -> Any:
        return self.request("POST", path, json=json).json()

    def patch(self, path: str, json: Any = None) -> Any:
        return self.request("PATCH", path, json=json).json()

    def delete(self, path: str) -> None:
        self.request("DELETE", path)

    def paginate(self, path: str, params: dict[str, Any] | None = None) -> Iterator[Any]:
        """Yield items from every page of a list endpoint."""
        params = {"per_page": 100, **(params or {})}
        url: str | None = path
        while url:
            resp = self.request("GET", url, params=params)
            yield from resp.json()
            url = resp.links.get("next", {}).get("url")
            # The next URL already carries the query string.
            params = None

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        method = method.upper()
        is_write = method not in READ_METHODS
        if is_write:
            if "://" in path:
                raise SandboxGuardError(f"Refusing {method} {path}: writes must use relative paths.")
            assert_write_allowed(method, path, self.sandbox_repo)
        elif "://" in path and not path.startswith(self._base_url):
            raise SandboxGuardError(f"Refusing to follow URL outside {self._base_url}: {path}")

        for attempt in range(self._max_retries + 1):
            if is_write:
                self._pace_writes()
            resp = self._http.request(method, path, **kwargs)
            if is_write:
                self._last_write = self._clock()

            wait = self._rate_limit_wait(resp)
            if wait is None:
                break
            if attempt == self._max_retries:
                raise RateLimitError(resp.status_code, method, path, "rate limited; retries exhausted")
            log.warning("Rate limited on %s %s; waiting %.0fs (attempt %d)", method, path, wait, attempt + 1)
            self._sleep(wait)

        if resp.is_error:
            raise GitHubError(resp.status_code, method, path, _error_message(resp))
        return resp

    # -- internals ----------------------------------------------------------

    def _pace_writes(self) -> None:
        if self._last_write is None or self._write_interval <= 0:
            return
        remaining = self._write_interval - (self._clock() - self._last_write)
        if remaining > 0:
            self._sleep(remaining)

    def _rate_limit_wait(self, resp: httpx.Response) -> float | None:
        """Return seconds to wait if `resp` is a rate-limit response, else None."""
        if resp.status_code not in (403, 429):
            return None
        retry_after = resp.headers.get("Retry-After")
        remaining = resp.headers.get("X-RateLimit-Remaining")
        reset = resp.headers.get("X-RateLimit-Reset")

        if retry_after is not None:
            wait = _to_float(retry_after, SECONDARY_LIMIT_FALLBACK_S)
        elif remaining == "0" and reset is not None:
            wait = _to_float(reset, 0.0) - self._clock() + 1.0
        elif resp.status_code == 429 or "rate limit" in _error_message(resp).lower():
            wait = SECONDARY_LIMIT_FALLBACK_S
        else:
            # A plain 403 is a permission problem, not a rate limit.
            return None
        return min(max(wait, 1.0), self._max_wait)


def _to_float(value: str, default: float) -> float:
    try:
        return float(value)
    except ValueError:
        return default


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(body, dict):
        return str(body.get("message", body))[:200]
    return str(body)[:200]
