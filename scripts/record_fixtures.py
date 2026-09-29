"""Record tool fixtures from the real sandbox (read-only), or regenerate them from the fake.

    uv run python scripts/record_fixtures.py            # real sandbox; needs .env
    uv run python scripts/record_fixtures.py --fake     # in-memory fake seeded from YAML
    uv run python scripts/record_fixtures.py --only search_rate_limit list_default

Real recording refuses to run unless the sandbox is exactly in the seed state
(a dry-run reset plans zero writes) and the search index is up to date. Every
fixture is sanitised and scanned for secrets before it is written.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # make the `tests` package importable

import respx  # noqa: E402

from tests.fake_github import API, FakeGitHub  # noqa: E402
from tests.recording import (  # noqa: E402
    FIXTURE_REPO,
    FIXTURE_TOKEN,
    SCENARIOS,
    Scenario,
    UnsafeFixtureError,
    assert_safe,
    record_scenario,
    write_fixture,
)
from whytypedtools_eval.config import ConfigError, load_settings  # noqa: E402
from whytypedtools_eval.github import GitHubClient  # noqa: E402
from whytypedtools_eval.sandbox.models import load_seed, load_state  # noqa: E402
from whytypedtools_eval.sandbox.ops import Sandbox  # noqa: E402
from whytypedtools_eval.sandbox.reset import reset  # noqa: E402
from whytypedtools_eval.sandbox.search_sync import SearchIndexTimeout, wait_for_search_index  # noqa: E402

SEED_FILE = ROOT / "sandbox" / "seed_data.yaml"
STATE_FILE = ROOT / "sandbox" / "state.json"


def _selected(only: list[str] | None) -> list[Scenario]:
    if not only:
        return SCENARIOS
    unknown = set(only) - {s.name for s in SCENARIOS}
    if unknown:
        sys.exit(f"error: unknown scenario(s): {', '.join(sorted(unknown))}")
    return [s for s in SCENARIOS if s.name in only]


def record_fake(scenarios: list[Scenario]) -> None:
    fake = FakeGitHub(FIXTURE_REPO)
    fake.load_seed(load_seed(SEED_FILE))
    with respx.mock(base_url=API, assert_all_called=False) as router:
        router.route().mock(side_effect=fake.handler)
        for scenario in scenarios:
            fixture = record_scenario(scenario, token=FIXTURE_TOKEN, real_repo=FIXTURE_REPO,
                                      source="fake", sleep=lambda _: None)
            assert_safe(fixture, [], FIXTURE_REPO.split("/")[0])
            print(f"wrote {write_fixture(fixture).relative_to(ROOT)}")


def check_sandbox_ready(token: str, repo: str) -> None:
    with GitHubClient(token, repo) as client:
        sb = Sandbox(client, repo, dry_run=True, emit=lambda _: None)
        reset(sb, load_seed(SEED_FILE), load_state(STATE_FILE, repo))
        if sb.writes:
            sys.exit(
                f"error: sandbox is not in the seed state ({len(sb.writes)} pending change(s)). "
                "Run scripts/reset_sandbox.py first."
            )
        wait_for_search_index(client, repo)


def record_real(scenarios: list[Scenario]) -> None:
    try:
        settings = load_settings(ROOT / ".env")
    except ConfigError as exc:
        sys.exit(f"error: {exc}")
    token = settings.github_token.get_secret_value()
    repo = settings.sandbox_repo
    try:
        check_sandbox_ready(token, repo)
    except SearchIndexTimeout as exc:
        sys.exit(f"error: {exc}")

    for scenario in scenarios:
        fixture = record_scenario(scenario, token=token, real_repo=repo, source="recorded")
        try:
            assert_safe(fixture, [token], repo.split("/")[0])
        except UnsafeFixtureError as exc:
            # Do not write it, and do not print its content.
            sys.exit(f"error: refusing to write {scenario.name}: {exc}")
        print(f"wrote {write_fixture(fixture).relative_to(ROOT)}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--fake", action="store_true", help="Record from the in-memory fake instead of GitHub.")
    p.add_argument("--only", nargs="+", metavar="SCENARIO", help="Record only these scenarios.")
    args = p.parse_args()
    scenarios = _selected(args.only)
    if args.fake:
        record_fake(scenarios)
    else:
        record_real(scenarios)


if __name__ == "__main__":
    main()
