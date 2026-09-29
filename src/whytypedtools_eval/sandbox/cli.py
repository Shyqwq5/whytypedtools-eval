"""Command-line entry points for scripts/seed_sandbox.py and scripts/reset_sandbox.py."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from whytypedtools_eval.config import ConfigError, Settings, load_settings
from whytypedtools_eval.github import GitHubClient, GitHubError
from whytypedtools_eval.sandbox.guard import SandboxGuardError, assert_sandbox_repo
from whytypedtools_eval.sandbox.models import SandboxState, SeedData, load_seed, load_state, save_state
from whytypedtools_eval.sandbox.ops import Sandbox
from whytypedtools_eval.sandbox.reset import reset
from whytypedtools_eval.sandbox.search_sync import SearchIndexTimeout, wait_for_search_index
from whytypedtools_eval.sandbox.seed import seed

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SEED = PROJECT_ROOT / "sandbox" / "seed_data.yaml"
DEFAULT_STATE = PROJECT_ROOT / "sandbox" / "state.json"

Operation = Callable[[Sandbox, SeedData, SandboxState], SandboxState]


def _parser(description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--repo", help="Target repo (owner/name). Must equal SANDBOX_REPO. Defaults to it.")
    p.add_argument("--seed-file", type=Path, default=DEFAULT_SEED)
    p.add_argument("--state-file", type=Path, default=DEFAULT_STATE)
    p.add_argument("--dry-run", action="store_true", help="Show planned writes without sending them.")
    p.add_argument(
        "--no-wait-search",
        action="store_true",
        help="Don't wait for the search index to reflect the changes afterwards.",
    )
    p.add_argument("--search-timeout", type=float, default=180.0, help="Seconds to wait for the search index.")
    return p


def run(
    operation: Operation,
    description: str,
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    client_factory: Callable[[Settings], GitHubClient] | None = None,
) -> int:
    args = _parser(description).parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        settings = settings or load_settings(PROJECT_ROOT / ".env")
        target = args.repo or settings.sandbox_repo
        # Guard runs before any network access.
        assert_sandbox_repo(target, settings.sandbox_repo)
        data = load_seed(args.seed_file)
        state = load_state(args.state_file, settings.sandbox_repo)
    except (ConfigError, SandboxGuardError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    factory = client_factory or _default_client
    mode = " (dry-run)" if args.dry_run else ""
    print(f"{operation.__name__} {settings.sandbox_repo}{mode}")
    try:
        with factory(settings) as client:
            sb = Sandbox(client, settings.sandbox_repo, dry_run=args.dry_run)
            state = operation(sb, data, state)
            if not args.dry_run:
                # Save before waiting so a search timeout doesn't lose the mapping.
                save_state(args.state_file, state)
                if not args.no_wait_search:
                    wait_for_search_index(client, settings.sandbox_repo, timeout=args.search_timeout)
    except SearchIndexTimeout as exc:
        print(f"error: {exc}. Changes were applied; re-run later or check search manually.", file=sys.stderr)
        return 1
    except (GitHubError, SandboxGuardError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"dry-run: {len(sb.writes)} write(s) planned, none sent.")
    else:
        print(f"done: {len(sb.writes)} write(s). State saved to {args.state_file}.")
    return 0


def _default_client(settings: Settings) -> GitHubClient:
    return GitHubClient(settings.github_token.get_secret_value(), settings.sandbox_repo)


def seed_main(argv: Sequence[str] | None = None) -> int:
    return run(seed, "Create missing seed labels and issues in the sandbox repo.", argv)


def reset_main(argv: Sequence[str] | None = None) -> int:
    return run(reset, "Restore the sandbox repo to exactly the seed state.", argv)
