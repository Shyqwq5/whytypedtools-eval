import pytest

from tests.conftest import REPO, SEED_FILE
from whytypedtools_eval.sandbox.models import load_seed
from whytypedtools_eval.sandbox.search_sync import SearchIndexTimeout, wait_for_search_index


@pytest.fixture
def seeded_fake(fake):
    fake.load_seed(load_seed(SEED_FILE))
    return fake


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _wait(client, clock, **kw):
    msgs = []
    wait_for_search_index(client, REPO, sleep=clock.sleep, clock=clock, emit=msgs.append, **kw)
    return msgs


def test_up_to_date_index_returns_immediately(client, seeded_fake):
    clock = FakeClock()
    _wait(client, clock)
    assert clock.now == 0


def test_waits_until_stale_index_catches_up(client, seeded_fake):
    seeded_fake.freeze_search_index(for_calls=2)
    number = seeded_fake.by_title("Dark mode for the web app")[0]["number"]
    seeded_fake.issues[number]["state"] = "closed"
    seeded_fake.touch(number)

    clock = FakeClock()
    msgs = _wait(client, clock, interval=10)
    assert clock.now == 20
    assert msgs[-1] == "search index is up to date"


def test_body_only_edit_is_detected_via_updated_at(client, seeded_fake):
    seeded_fake.freeze_search_index(for_calls=1)
    number = seeded_fake.by_title("Dark mode for the web app")[0]["number"]
    seeded_fake.issues[number]["body"] = "edited"
    seeded_fake.touch(number)
    clock = FakeClock()
    _wait(client, clock, interval=5)
    assert clock.now == 5


def test_times_out(client, seeded_fake):
    seeded_fake.freeze_search_index(for_calls=100)
    seeded_fake.add_issue("New issue not yet indexed")
    clock = FakeClock()
    with pytest.raises(SearchIndexTimeout):
        _wait(client, clock, timeout=30, interval=10)
    assert clock.now <= 30


def test_cli_can_skip_the_wait(tmp_path, settings, fake):
    from tests.test_seed import _client_factory
    from whytypedtools_eval.sandbox.cli import run
    from whytypedtools_eval.sandbox.seed import seed

    args = ["--state-file", str(tmp_path / "state.json"), "--no-wait-search"]
    assert run(seed, "seed", args, settings=settings, client_factory=_client_factory) == 0
    assert not any(path == "/search/issues" for _, path, _ in fake.calls)

    fake.calls.clear()
    assert run(seed, "seed", args[:2], settings=settings, client_factory=_client_factory) == 0
    assert any(path == "/search/issues" for _, path, _ in fake.calls)
