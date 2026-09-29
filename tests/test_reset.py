import pytest

from tests.conftest import REPO, SEED_FILE
from whytypedtools_eval.sandbox.models import SandboxState, load_seed
from whytypedtools_eval.sandbox.ops import Sandbox
from whytypedtools_eval.sandbox.reset import ARTIFACT_PREFIX, reset
from whytypedtools_eval.sandbox.seed import seed


@pytest.fixture
def data():
    return load_seed(SEED_FILE)


def _sb(client, dry_run=False):
    return Sandbox(client, REPO, dry_run=dry_run, emit=lambda _: None)


@pytest.fixture
def seeded(client, fake, data):
    state = seed(_sb(client), data, SandboxState(repo=REPO))
    fake.calls.clear()
    return state


def snapshot(fake):
    issues = {
        n: (i["title"], i["body"], i["state"], i["state_reason"], sorted(lbl["name"] for lbl in i["labels"]))
        for n, i in fake.issues.items()
    }
    labels = {n: dict(lbl) for n, lbl in fake.labels.items()}
    comments = sorted((c["issue"], c["body"]) for c in fake.comments.values())
    return issues, labels, comments


def mess_up(fake, state):
    """Simulate what an agent might do during an eval run. Returns a non-seed issue number."""
    n = state.issues["rate-limit-429"]
    fake.issues[n]["title"] = "RENAMED by agent"
    fake.issues[n]["labels"] = [{"name": "question"}]
    fake.add_comment(n, "agent was here")
    fake.comments.pop(min(c for c, v in fake.comments.items() if v["issue"] == n))
    fake.issues[state.issues["dark-mode"]].update(state="closed", state_reason="completed")
    fake.issues[state.issues["rate-limit-docs"]].update(state="open", state_reason="reopened")
    fake.labels["bug"]["color"] = "000000"
    fake.labels["agent-label"] = {"name": "agent-label", "color": "ffffff", "description": ""}
    del fake.labels["question"]
    for issue in fake.issues.values():
        issue["labels"] = [lbl for lbl in issue["labels"] if lbl["name"] != "question"]
    return fake.add_issue("Agent-created issue", labels=["agent-label"])


def test_reset_on_clean_sandbox_is_noop(client, fake, data, seeded):
    reset(_sb(client), data, seeded)
    assert fake.writes == []


def test_reset_restores_seed_state(client, fake, data, seeded):
    expected_issues, expected_labels, expected_comments = snapshot(fake)
    extra = mess_up(fake, seeded)

    state = reset(_sb(client), data, seeded)

    issues, labels, comments = snapshot(fake)
    assert labels == expected_labels
    assert comments == expected_comments
    # The renamed issue was found by number, not duplicated.
    assert len(fake.by_title("API returns 429 when rate limit is exceeded")) == 1
    assert state.issues == seeded.issues
    for n, want in expected_issues.items():
        got = issues[n]
        if want[2] == "open":
            # A reopened issue reports state_reason "reopened"; ignore that field.
            assert got[:3] + got[4:] == want[:3] + want[4:]
        else:
            assert got == want
    # Non-seed issues cannot be deleted: closed as not_planned, labels stripped.
    assert fake.issues[extra]["state"] == "closed"
    assert fake.issues[extra]["state_reason"] == "not_planned"
    assert fake.issues[extra]["labels"] == []

    fake.calls.clear()
    reset(_sb(client), data, state)
    assert fake.writes == []


def test_reset_recreates_deleted_seed_issue(client, fake, data, seeded):
    old = seeded.issues["webhook-retries"]
    del fake.issues[old]
    state = reset(_sb(client), data, seeded)
    new = state.issues["webhook-retries"]
    assert new != old
    assert fake.issues[new]["title"] == "Webhooks are not retried after a 5xx response"


def test_reset_dry_run_makes_no_write_calls(client, fake, data, seeded):
    before = snapshot(fake)
    mess_up(fake, seeded)
    messed = snapshot(fake)
    fake.calls.clear()
    sb = _sb(client, dry_run=True)
    reset(sb, data, seeded)
    assert fake.writes == []
    assert sb.writes  # it did plan changes
    assert snapshot(fake) == messed != before


def test_reset_never_touches_repo_resource(client, fake, data, seeded):
    mess_up(fake, seeded)
    reset(_sb(client), data, seeded)
    assert all(p.rstrip("/") != f"/repos/{REPO}" for _, p, _ in fake.calls)


def test_non_seed_issue_titles_are_prefixed_idempotently(client, fake, data, seeded):
    fresh = fake.add_issue("Agent opened this")
    already = fake.add_issue(ARTIFACT_PREFIX + "From a previous run", state="closed")
    fake.issues[already]["state_reason"] = "not_planned"

    reset(_sb(client), data, seeded)
    assert fake.issues[fresh]["title"] == "[eval-artifact] Agent opened this"
    assert fake.issues[already]["title"] == "[eval-artifact] From a previous run"
    assert all(p != f"/repos/{REPO}/issues/{already}" for _, p, _ in fake.writes)

    fake.calls.clear()
    reset(_sb(client), data, seeded)
    assert fake.writes == []
    assert fake.issues[fresh]["title"] == "[eval-artifact] Agent opened this"


def test_seed_issues_lose_assignees_milestone_and_lock(client, fake, data, seeded):
    n = seeded.issues["token-expiry"]
    fake.issues[n].update(assignees=[{"login": "someone"}], milestone={"number": 3}, locked=True)

    reset(_sb(client), data, seeded)

    issue = fake.issues[n]
    assert issue["assignees"] == []
    assert issue["milestone"] is None
    assert issue["locked"] is False
    patch = next(b for m, p, b in fake.writes if m == "PATCH" and p == f"/repos/{REPO}/issues/{n}")
    assert patch == {"assignees": [], "milestone": None}
    assert ("DELETE", f"/repos/{REPO}/issues/{n}/lock", None) in fake.writes

    fake.calls.clear()
    reset(_sb(client), data, seeded)
    assert fake.writes == []
