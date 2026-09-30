"""Re-score real, sanitised eval traces and fail if any score changes.

The fixtures (tests/scoring_baseline/, built by
scripts/build_scoring_baseline.py) hold 14 runs from committed evals: typed and
generic, pass and fail, bulk writes, guard blocks, guard failures, an out-of-scope
first call, a truncated response. If a change to the scorer, the task files or the
generic mapping changes any of their scores, this test fails, so reported results
can't change silently. If a change is intended, rebuild the fixtures and explain
the changed scores in the commit.

The runs are re-scored from their stored write effects, so this test does not
exercise the write-effects classification; that is pinned by tests/evals/test_effects.py.
"""

import json
import re
from pathlib import Path

import pytest

from tests.conftest import SEED_FILE
from whytypedtools_eval.agent.trace import read_trace
from whytypedtools_eval.evals.configs import TYPED
from whytypedtools_eval.evals.effects import Effect
from whytypedtools_eval.evals.generic_mapping import load_generic_mapping
from whytypedtools_eval.evals.scoring import score_run
from whytypedtools_eval.evals.tasks import load_bulk_specs, load_task_set
from whytypedtools_eval.sandbox.models import load_seed

BASELINE = Path(__file__).resolve().parents[1] / "scoring_baseline"
MANIFEST = json.loads((BASELINE / "manifest.json").read_text(encoding="utf-8"))
TASK_SET = load_task_set()
TASKS = {t.id: t for t in TASK_SET.tasks}
KEYMAP = MANIFEST["keymap"]
BULK = load_bulk_specs(load_seed(SEED_FILE), KEYMAP)
MAPPING = load_generic_mapping(TASK_SET)


def test_baseline_matches_the_current_task_set_and_covers_every_path():
    assert MANIFEST["task_set_sha256"] == TASK_SET.sha256
    cases = MANIFEST["cases"]
    assert {c["config"] for c in cases} == {"tool_e", "tool_a", "tool_d"}
    expected = [c["expected"] for c in cases]
    assert any(e["passed"] is True for e in expected) and any(e["passed"] is False for e in expected)
    assert any(e["bulk"] for e in expected)
    assert any(e["guard_blocks"] for e in expected) and any(e["guard_failures"] for e in expected)
    assert any(e["event_counts"].get("out_of_scope_read") for e in expected)


@pytest.mark.parametrize("case", MANIFEST["cases"], ids=lambda c: c["name"])
def test_rescoring_gives_the_recorded_scores(case):
    events = read_trace(BASELINE / case["trace"])
    extra = {}
    if case["config"] not in TYPED:
        entry = MAPPING[case["task_id"]]
        extra = {"min_calls_override": entry["min_tool_calls"], "also_accepted_as": entry["also_accepted_as"]}
    record = score_run(TASKS[case["task_id"]], events=events, write_log=[],
                       effects=[Effect(**e) for e in case["effects"]], keymap=KEYMAP,
                       write_mode=case["write_mode"], typed=case["config"] in TYPED,
                       bulk=BULK.get(case["task_id"]), **extra)
    changed = {k: (v, record[k]) for k, v in case["expected"].items() if record[k] != v}
    assert not changed, f"scores changed (recorded, now): {changed}"


def test_fixtures_hold_no_credentials_or_user_objects():
    token = re.compile(r"\b(ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}")
    for path in BASELINE.iterdir():
        text = path.read_text(encoding="utf-8")
        assert not token.search(text), path.name
        assert "avatar_url" not in text and "gravatar_id" not in text, path.name
