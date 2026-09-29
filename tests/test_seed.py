import json

import pytest

from tests.conftest import REPO, SEED_FILE
from whytypedtools_eval.github import GitHubClient
from whytypedtools_eval.sandbox.cli import run
from whytypedtools_eval.sandbox.models import SandboxState, load_seed
from whytypedtools_eval.sandbox.ops import Sandbox
from whytypedtools_eval.sandbox.seed import seed


@pytest.fixture
def data():
    return load_seed(SEED_FILE)


def run_seed(client, data, state=None, dry_run=False):
    sb = Sandbox(client, REPO, dry_run=dry_run, emit=lambda _: None)
    return seed(sb, data, state or SandboxState(repo=REPO)), sb


def test_seed_file_is_valid(data):
    assert 5 <= len(data.labels) <= 8
    assert 18 <= len(data.issues) <= 25
    assert {i.state for i in data.issues} == {"open", "closed"}
    assert sum("rate limit" in (i.title + i.body).lower() for i in data.issues) >= 3
    assert [i.key for i in data.issues if i.safety_test] == ["injection-close-all"]


def test_fresh_seed_creates_everything(client, fake, data):
    state, _ = run_seed(client, data)
    assert set(fake.labels) == {lbl.name for lbl in data.labels}
    assert len(fake.issues) == len(data.issues)
    for spec in data.issues:
        issue = fake.issues[state.issues[spec.key]]
        assert issue["title"] == spec.title
        assert issue["state"] == spec.state
        assert fake.comments_on(issue["number"]) == [c.body for c in spec.comments]


def test_seed_is_idempotent(client, fake, data):
    state, _ = run_seed(client, data)
    fake.calls.clear()
    state2, sb = run_seed(client, data, state)
    assert fake.writes == []
    assert sb.writes == []
    assert state2.issues == state.issues


def test_seed_without_state_matches_by_title(client, fake, data):
    run_seed(client, data)
    fake.calls.clear()
    state, _ = run_seed(client, data, SandboxState(repo=REPO))
    assert fake.writes == []
    assert len(state.issues) == len(data.issues)


def test_seed_only_creates_missing(client, fake, data):
    state, _ = run_seed(client, data)
    victim = state.issues["dark-mode"]
    del fake.issues[victim]
    del fake.labels["question"]
    fake.calls.clear()
    state, _ = run_seed(client, data, state)
    posts = [(m, p) for m, p, _ in fake.writes if m == "POST"]
    assert posts.count(("POST", f"/repos/{REPO}/labels")) == 1
    assert posts.count(("POST", f"/repos/{REPO}/issues")) == 1
    assert state.issues["dark-mode"] != victim


def test_dry_run_makes_no_write_calls(client, fake, data):
    _, sb = run_seed(client, data, dry_run=True)
    assert fake.writes == []
    assert fake.calls  # it still read the current state
    assert len(sb.writes) > len(data.issues)


def test_local_only_fields_never_sent(client, fake, data):
    run_seed(client, data)
    sent = json.dumps([body for _, _, body in fake.writes]).lower()
    assert "safety_test" not in sent
    assert "indirect_prompt_injection" not in sent
    assert "safety test" not in sent
    spec = next(i for i in data.issues if i.safety_test)
    assert "ignore your previous" in spec.body  # the injection itself is present


def _client_factory(settings):
    # The CLI closes the client when done, so hand out a fresh one each run.
    return GitHubClient(settings.github_token.get_secret_value(), settings.sandbox_repo, write_interval=0)


def test_cli_saves_state_and_dry_run_does_not(tmp_path, settings, fake):
    state_file = tmp_path / "state.json"
    args = ["--state-file", str(state_file)]
    assert run(seed, "seed", [*args, "--dry-run"], settings=settings, client_factory=_client_factory) == 0
    assert fake.writes == []
    assert not state_file.exists()

    assert run(seed, "seed", args, settings=settings, client_factory=_client_factory) == 0
    saved = json.loads(state_file.read_text())
    assert saved["repo"] == REPO
    assert len(saved["issues"]) == len(load_seed(SEED_FILE).issues)


def test_state_file_for_other_repo_is_refused(tmp_path, settings, fake, capsys):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"repo": "me/other", "issues": {}}))
    code = run(seed, "seed", ["--state-file", str(state_file)], settings=settings)
    assert code == 2
    assert "belongs to" in capsys.readouterr().err
    assert fake.calls == []
