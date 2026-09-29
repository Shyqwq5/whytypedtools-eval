"""Safety guards that keep every write pointed at the sandbox repo.

Two layers:
1. `assert_sandbox_repo`: scripts refuse to start unless the target repo is the
   configured SANDBOX_REPO.
2. `assert_write_allowed`: the HTTP client checks every non-GET request path, so
   even a bug in script logic cannot write anywhere else.

GitHub owner/repo names are case-insensitive, so comparisons are lowercased.
Paths are compared by whole segments, so `repos/u/sandbox2` never matches `u/sandbox`.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from whytypedtools_eval.config import REPO_PATTERN

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class SandboxGuardError(RuntimeError):
    """Raised when an operation would touch something other than the sandbox."""


def normalize_repo(repo: str) -> str:
    repo = repo.strip()
    if not REPO_PATTERN.match(repo):
        raise SandboxGuardError(f"Invalid repo {repo!r}; expected owner/name.")
    return repo.lower()


def assert_sandbox_repo(target: str, sandbox_repo: str) -> None:
    """Refuse to run unless `target` is exactly the configured sandbox repo."""
    if normalize_repo(target) != normalize_repo(sandbox_repo):
        raise SandboxGuardError(
            f"Refusing to run: target repo {target!r} is not SANDBOX_REPO ({sandbox_repo!r})."
        )


def _path_segments(path_or_url: str) -> list[str]:
    path = urlsplit(path_or_url).path if "://" in path_or_url else path_or_url
    return [seg for seg in path.split("/") if seg]


def assert_write_allowed(method: str, path_or_url: str, sandbox_repo: str) -> None:
    """Allow a non-read request only if it targets a sub-resource of the sandbox repo."""
    method = method.upper()
    if method in READ_METHODS:
        return
    segments = _path_segments(path_or_url)
    if len(segments) < 3 or segments[0] != "repos":
        raise SandboxGuardError(f"Refusing {method} {path_or_url}: not a repo-scoped path.")
    owner, name = normalize_repo(sandbox_repo).split("/")
    if segments[1].lower() != owner or segments[2].lower() != name:
        raise SandboxGuardError(
            f"Refusing {method} {path_or_url}: target is not SANDBOX_REPO ({sandbox_repo})."
        )
    if len(segments) == 3:
        # Writes to the repo resource itself (DELETE = delete repo, PATCH = rename,
        # visibility, archive) are never allowed.
        raise SandboxGuardError(f"Refusing {method} {path_or_url}: repo-level writes are forbidden.")
