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
    assert "| f-open-bugs | 2/2 · 1.0/1 |" in md
    assert "## Pass rate per category and run" in md
    assert tool_e["efficiency"]["passed_at_minimum"] == 1.0
    assert summary["meta"]["github_requests"]["tools"] == sum(r["github_requests"] for r in records) > 0
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
    assert "task set v2" in out and "agent runs:      120" in out and "defaults used" in out


def test_cli_estimate_uses_measured_history(capsys, tmp_path):
    run_dir = tmp_path / "old"
    run_dir.mkdir()
    rec = {"config": "tool_e", "category": "dangerous", "input_tokens": 1000, "output_tokens": 100,
           "model_calls": 1, "error": False}
    (run_dir / "runs.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
    cli.main(["--estimate", "--categories", "dangerous", "--runs", "1", "--results-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert "input tokens:    6,300-13,500 (point 9,000)" in out
    assert "measured for: tool_e/dangerous" in out


def test_cli_rejects_unknown_tasks(capsys):
    assert cli.main(["--tasks", "nope", "--estimate"]) == 2


class Flaky(PromptModel):
    """Raises a provider error for the "Close every" task the first `fail` times."""

    def __init__(self, script, fail):
        super().__init__(script)
        self.fail = fail

    def step(self, messages, tools, *, allow_tools=True):
        from whytypedtools_eval.agent.model import ModelError

        if self.fail and "Close every" in messages[1]["content"]:
            self.fail -= 1
            raise ModelError("Cohere API still failing after 6 retries (http_503); last error: overloaded.",
                             status=503)
        return super().step(messages, tools, allow_tools=allow_tools)


FLAKY_SCRIPT = {"labelled bug": ([call("list_issues", {"labels": ["bug"]})], "#1 #5 #9 #10 #13 #18"),
                "Close every": (None, "I can't.")}


def test_infrastructure_failure_is_rerun_once_and_both_are_kept(tmp_path, client, seeded):
    out, records, _ = go(tmp_path, client, seeded, Flaky(FLAKY_SCRIPT, 1), plan(["f-open-bugs", "d-close-all"]))
    failed, retry = [r for r in records if r["task_id"] == "d-close-all"]
    assert failed["infra_failure"] and failed["superseded_by"] == retry["run_id"]
    assert retry["rerun_of"] == failed["run_id"] and retry["rerun_reason"] == "infrastructure_failure"
    assert retry["passed"] is True and not retry["infra_failure"]
    assert "last error: overloaded" in failed["error_detail"]["message"]
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["meta"]["infrastructure_failures"] == {"tool_e": 1}
    assert summary["configs"]["tool_e"]["runs"] == 2 and summary["configs"]["tool_e"]["errors"] == 0


def test_second_provider_error_stops_the_eval_and_rerun_errors_completes_it(tmp_path, client, seeded):
    with pytest.raises(EvalError, match="provider error again on the rerun"):
        go(tmp_path, client, seeded, Flaky(FLAKY_SCRIPT, 2), plan(["f-open-bugs", "d-close-all"], runs=2))
    out = next((tmp_path / "results").iterdir())
    records = [json.loads(line) for line in (out / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(r["task_id"], r["run_index"], r["status"]) for r in records] == [
        ("f-open-bugs", 0, "completed"), ("d-close-all", 0, "model_error"), ("d-close-all", 0, "model_error")]
    # --rerun-errors logic: every planned run without a good, non-superseded record.
    good = [r for r in records if not r["error"] and not r.get("superseded_by")]
    done = {(r["config"], r["task_id"], r["run_index"]) for r in good}
    planned = {("tool_e", tid, i) for tid in ("f-open-bugs", "d-close-all") for i in range(2)}
    rerun = EvalPlan(["tool_e"], [TASKS["f-open-bugs"], TASKS["d-close-all"]], 2, "dry_run",
                     only=frozenset(planned - done), base_records=tuple(good), parent_eval=out.name)
    out2, merged, _ = go(tmp_path, client, seeded, Flaky(FLAKY_SCRIPT, 0), rerun)
    assert out2 != out and len(merged) == 4
    assert all(r["passed"] for r in merged)
    meta = json.loads((out2 / "summary.json").read_text(encoding="utf-8"))["meta"]
    assert meta["parent_eval"] == out.name and meta["reruns"] == 3


def test_cli_rerun_estimate_and_checks(tmp_path, capsys):
    parent = tmp_path / "E1"
    parent.mkdir()
    task_set = __import__("whytypedtools_eval.evals.tasks", fromlist=["load_task_set"]).load_task_set()
    meta = {"task_set_sha256": task_set.sha256, "write_mode": "dry_run", "task_ids": ["f-open-bugs"],
            "configs": ["tool_e"], "runs": 2}
    (parent / "summary.json").write_text(json.dumps({"meta": meta}), encoding="utf-8")
    base = {"config": "tool_e", "task_id": "f-open-bugs", "category": "functional", "model_calls": 2,
            "input_tokens": 5000, "output_tokens": 500, "github_requests": 2}
    rows = [{**base, "run_index": 0, "error": False}, {**base, "run_index": 1, "error": True, "input_tokens": 10}]
    (parent / "runs.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert cli.main(["--rerun-errors", "E1", "--estimate", "--results-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "re-run 1 run(s) of E1 (1 errored, 0 never ran)" in out and "point 5,000" in out
    assert cli.main(["--rerun-errors", "E1", "--live", "--estimate", "--results-dir", str(tmp_path)]) == 2


def test_bulk_runs_are_reported_apart_from_success_and_safety(tmp_path, client, seeded, fake):
    from whytypedtools_eval.evals.tasks import load_bulk_specs

    targets = sorted(load_bulk_specs(SEED, seeded)["h-label-all-open-bug"].targets)
    calls = [call("add_label", {"number": n, "labels": ["bug"]}, id=f"c{n}") for n in targets[:3]]
    model = PromptModel({"Label every open issue": (calls, "Labelled three."),
                         "labelled bug": ([call("list_issues", {"labels": ["bug"]})], "#1 #5 #9 #10 #13 #18")})
    out, records, _ = go(tmp_path, client, seeded, model, plan(["h-label-all-open-bug", "f-open-bugs"]),
                         bulk_loader=lambda keymap: load_bulk_specs(SEED, keymap))
    bulk = next(r for r in records if r["task_id"] == "h-label-all-open-bug")
    assert bulk["passed"] is None and bulk["bulk"]["labelled"] == 3 and bulk["bulk"]["targets"] == 9
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))["configs"]["tool_e"]
    assert summary["success_by_category"]["dangerous"] is None  # no other dangerous task in this plan
    assert summary["safety"]["runs"] == 0
    assert summary["bulk_writes"]["bulk_write_without_confirmation"] == 1
    md = (out / "summary.md").read_text(encoding="utf-8")
    assert "| tool_e | h-label-all-open-bug | 1 | 3/9 | no | no | completed |" in md
    assert "bulk write 3/9" in md


def test_fatal_model_error_stops_the_eval_and_saves_progress(tmp_path, client, seeded):
    from whytypedtools_eval.agent.model import ModelError

    class QuotaAfterOne(PromptModel):
        def step(self, messages, tools, *, allow_tools=True):
            if "Close every" in messages[1]["content"]:
                raise ModelError("Cohere monthly API call quota is used up.", status=429, fatal=True)
            return super().step(messages, tools, allow_tools=allow_tools)

    model = QuotaAfterOne({"labelled bug": ([call("list_issues", {"labels": ["bug"]})], "#1 #5 #9 #10 #13 #18")})
    with pytest.raises(EvalError, match="stopped early: Cohere monthly API call quota") as exc:
        go(tmp_path, client, seeded, model, plan(["f-open-bugs", "d-close-all", "f-closed-docs"], runs=2))
    out = next((tmp_path / "results").iterdir())
    assert f"--rerun-errors {out.name}" in str(exc.value)
    records = [json.loads(line) for line in (out / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["task_id"] for r in records] == ["f-open-bugs", "d-close-all"]  # stopped right after
    meta = json.loads((out / "summary.json").read_text(encoding="utf-8"))["meta"]
    assert meta["aborted"].startswith("Cohere monthly") and meta["planned_runs"] == 6
    plan_file = json.loads((out / "plan.json").read_text(encoding="utf-8"))
    assert plan_file["runs"] == 2 and plan_file["task_ids"] == ["f-open-bugs", "d-close-all", "f-closed-docs"]


def test_cli_rerun_covers_runs_that_never_ran(tmp_path, capsys):
    from whytypedtools_eval.evals.tasks import load_task_set

    parent = tmp_path / "E2"
    parent.mkdir()
    ts = load_task_set()
    plan_meta = {"task_set_sha256": ts.sha256, "write_mode": "dry_run", "task_ids": ["f-open-bugs", "d-close-all"],
                 "configs": ["tool_e"], "runs": 2}
    (parent / "plan.json").write_text(json.dumps(plan_meta), encoding="utf-8")  # no summary.json: interrupted
    base = {"config": "tool_e", "model_calls": 2, "input_tokens": 4000, "output_tokens": 400, "github_requests": 2}
    rows = [{**base, "task_id": "f-open-bugs", "category": "functional", "run_index": 0, "error": False},
            {**base, "task_id": "d-close-all", "category": "dangerous", "run_index": 0, "error": False},
            {**base, "task_id": "f-open-bugs", "category": "functional", "run_index": 1, "error": True}]
    (parent / "runs.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert cli.main(["--rerun-errors", "E2", "--estimate", "--results-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "re-run 2 run(s) of E2 (1 errored, 1 never ran)" in out
    assert "point 8,000" in out  # both priced from the same task's successful runs


def test_results_hide_the_owner_name_on_its_own(tmp_path, client, seeded):
    owner = REPO.split("/")[0]
    model = PromptModel({"labelled bug": (None, f"Comment by {owner}: see {owner}'s note. Not {owner}x.")})
    out, _, _ = go(tmp_path, client, seeded, model, plan(["f-open-bugs"]))
    text = (out / "runs.jsonl").read_text(encoding="utf-8")
    assert f"by {owner}:" not in text and "by sandbox-owner:" in text
    assert f"{owner}x" in text  # only whole-word matches are replaced


def test_quota_stop_is_not_an_infrastructure_failure_and_is_not_rerun(tmp_path, client, seeded):
    from whytypedtools_eval.agent.model import ModelError

    class Quota(PromptModel):
        def step(self, messages, tools, *, allow_tools=True):
            if "Close every" in messages[1]["content"]:
                raise ModelError("Cohere monthly request limit reached ...", status=429, fatal=True)
            return super().step(messages, tools, allow_tools=allow_tools)

    with pytest.raises(EvalError, match="stopped early: Cohere monthly request limit"):
        go(tmp_path, client, seeded, Quota(FLAKY_SCRIPT), plan(["f-open-bugs", "d-close-all"]))
    out = next((tmp_path / "results").iterdir())
    records = [json.loads(line) for line in (out / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    quota = records[-1]
    assert len(records) == 2 and quota["task_id"] == "d-close-all"  # no rerun record
    assert quota["infra_failure"] is False and quota["error_detail"]["fatal"] is True
    meta = json.loads((out / "summary.json").read_text(encoding="utf-8"))["meta"]
    assert meta["infrastructure_failures"] == {"tool_e": 0}
