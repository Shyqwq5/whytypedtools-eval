"""Shared fixtures. All HTTP is mocked with respx; no test touches the real API."""

from __future__ import annotations

from pathlib import Path

import pytest
import respx

from whytypedtools_eval.config import Settings
from tests.fake_github import API, FakeGitHub
from whytypedtools_eval.github import GitHubClient

REPO = "me/sandbox"
SEED_FILE = Path(__file__).resolve().parents[1] / "sandbox" / "seed_data.yaml"


@pytest.fixture(autouse=True)
def _no_real_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's real token out of every test."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("SANDBOX_REPO", raising=False)


@pytest.fixture
def mock_api():
    # assert_all_mocked=True (default) makes any unmocked request fail loudly.
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield router


@pytest.fixture
def fake(mock_api: respx.Router) -> FakeGitHub:
    gh = FakeGitHub(REPO)
    mock_api.route().mock(side_effect=gh.handler)
    return gh


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, github_token="github_pat_test", sandbox_repo=REPO)  # type: ignore[call-arg]


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def client(sleeps: list[float]) -> GitHubClient:
    c = GitHubClient("github_pat_test", REPO, write_interval=0, search_interval=0, sleep=sleeps.append)
    yield c
    c.close()
