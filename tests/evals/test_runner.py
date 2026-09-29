import json

import pytest

from tests.agent.scripted_model import call
from tests.conftest import REPO, SEED_FILE
from whytypedtools_eval.agent.model import ModelTurn, Usage
from whytypedtools_eval.evals import cli
from whytypedtools_eval.evals.runner import EvalError, EvalPlan, run_eval
from whytypedtools_eval.evals.tasks import load_tasks
from whytypedtools_eval.sandbox.models import load_seed

SEED = load_seed(SEED_FILE)
TASKS = {t.id: t for t in load_tasks()}
SECRET = "github_pat_runner_secret_123"


class PromptModel:
    """Answers each task by prompt: optional tool calls first, then a final text."""

    def __init__(self, script):
        self.script = script  # prompt substring -> (list of ToolCall | None, answer)
        self.calls = 0

    def describe(self):
        return {"provider": "scripted", "model": "scripted", "temperature": 0.0, "seed": None, "thinking": None}

    def step(self, messages, tools, *, allow_tools=True):
        self.calls += 1
        prompt = messages[1]["content"]
        tool_calls, answer = next(v for k, v in self.script.items() if k in prompt)
        used = sum(1 for m in messages if m["role"] == "tool")
        if tool_calls and used < len(tool_calls):
            return ModelTurn(None, [tool_calls[used]], "tool_call", Usage(100, 10))
        return ModelTurn(answer, [], "complete", Usage(200, 20))


class FakeSandbox:
    def __init__(self, keymap, drift=None):
        self._keymap = keymap
        self._drift = list(drift or [])
        self.resets = 0
        self.drift_checks = 0

    def keymap(self):
        return dict(self._keymap)

    def drift(self):
        self.drift_checks += 1
        return list(self._drift)

    def reset(self):
        self.resets += 1
        self._drift = []
        return []


@pytest.fixture
def seeded(fake):
    return fake.load_seed(SEED)


def plan(ids, mode="dry_run", runs=1):
    return EvalPlan(["tool_e"], [TASKS[i] for i in ids], runs, mode)


def go(tmp_path, client, keymap, model, p, sandbox=None, **kw):
    sandbox = sandbox or FakeSandbox(keymap)
    out = run_eval(p, model=model, tool_client=client, repo=REPO, sandbox=sandbox,
                   results_dir=tmp_path / "results", trace_dir=tmp_path / "runs",
                   secrets=[SECRET], emit=lambda _: None, **kw)
    records = [json.loads(line) for line in (out / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    return out, records, sandbox


def test_dry_run_eval_end_to_end(tmp_path, client, seeded, fake):
    n = seeded["unlabeled-login"]
    model = PromptModel({
        "labelled bug": ([call("list_issues", {"labels": ["bug"]})], "#1, #5, #9, #10, #13, #18"),
        "Add the bug label": ([call("add_label", {"number": n, "labels": ["bug"]})], "Done."),
        "Close every open issue": (None, "I can't do that."),
    })
    out, records, sandbox = go(tmp_path, client, seeded, model,
                               plan(["f-open-bugs", "f-label-unlabeled", "d-close-all"], runs=2),
                               exposure={"injection-summary": "summary"})
    assert len(records) == 6
    assert all(r["passed"] for r in records), [r["task_id"] for r in records if not r["passed"]]
    assert fake.writes == []  # dry run: add_label was captured, not sent
    assert sandbox.resets == 0 and sandbox.drift_checks == 1
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    tool_e = summary["configs"]["tool_e"]
    assert tool_e["success_rate"] == 1.0 and tool_e["safety"]["block_rate"] == 1.0
    assert tool_e["consistency"]["tasks_consistent"] == 1.0
    assert summary["meta"]["write_mode"] == "dry_run" and summary["meta"]["runs"] == 2
    md = (out / "summary.md").read_text(encoding="utf-8")
    assert "| f-open-bugs | 2/2 |" in md
    traces = list((tmp_path / "runs").rglob("*.jsonl"))
    assert len(traces) == 6
    start = json.loads(traces[0].read_text(encoding="utf-8").splitlines()[0])
    assert start["config"] == "tool_e" and start["eval_id"] == out.name


def test_results_hide_secrets_and_repo(tmp_path, client, seeded):
    model = PromptModel({"labelled bug": (None, f"see https://github.com/{REPO}/issues/1 {SECRET}")})
    out, records, _ = go(tmp_path, client, seeded, model, plan(["f-open-bugs"]))
    text = (out / "runs.jsonl").read_text(encoding="utf-8")
    assert SECRET not in text and REPO not in text
    assert "sandbox-owner/whytypedtools-sandbox" in text


def test_dry_run_refuses_a_drifted_sandbox(tmp_path, client, seeded):
    with pytest.raises(EvalError, match="not in the seed state"):
        go(tmp_path, client, seeded, PromptModel({}), plan(["f-open-bugs"]),
           sandbox=FakeSandbox(seeded, drift=["close #1"]))


def test_live_resets_first_and_after_writes(tmp_path, client, seeded, fake):
    n = seeded["unlabeled-login"]
    model = PromptModel({
        "Add the bug label": ([call("add_label", {"number": n, "labels": ["bug"]})], "Done."),
        "labelled bug": (None, "#1 #5 #9 #10 #13 #18"),
    })

    class DriftAfterWrite(FakeSandbox):
        def drift(self):
            self.drift_checks += 1
            return ["remove label bug from #21"] if fake.writes else []

        def reset(self):
            self.resets += 1
            fake.calls.clear()
            return []

    sandbox = DriftAfterWrite(seeded)
    _, records, _ = go(tmp_path, client, seeded, model, plan(["f-label-unlabeled", "f-open-bugs"], "live"),
                       sandbox=sandbox)
    assert sandbox.resets == 2  # before the eval, and after the writing run
    assert records[0]["drift"] == 1 and records[1]["drift"] is None
    assert records[0]["passed"] is True


def test_missing_keys_and_unknown_config(tmp_path, client, seeded):
    with pytest.raises(EvalError, match="no issue number"):
        go(tmp_path, client, {"dark-mode": 17}, PromptModel({}), plan(["f-open-bugs"]))
    bad = EvalPlan(["tool_z"], [TASKS["f-open-bugs"]], 1, "dry_run")
    with pytest.raises(EvalError, match="unknown configuration"):
        go(tmp_path, client, seeded, PromptModel({}), bad)


def test_cli_estimate_needs_no_credentials(capsys, tmp_path):
    assert cli.main(["--estimate", "--results-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "agent runs:      90" in out and "defaults used" in out


def test_cli_estimate_uses_measured_history(capsys, tmp_path):
    run_dir = tmp_path / "old"
    run_dir.mkdir()
    rec = {"config": "tool_e", "category": "dangerous", "input_tokens": 1000, "output_tokens": 100,
           "model_calls": 1, "error": False}
    (run_dir / "runs.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
    cli.main(["--estimate", "--categories", "dangerous", "--runs", "1", "--results-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert "input tokens:    4,900-10,500 (point 7,000)" in out
    assert "measured for: tool_e/dangerous" in out


def test_cli_rejects_unknown_tasks(capsys):
    assert cli.main(["--tasks", "nope", "--estimate"]) == 2
