import json
import runpy
from pathlib import Path

import pytest
import yaml

from whytypedtools_eval.evals import cli
from whytypedtools_eval.evals.configs import toolset_config
from whytypedtools_eval.evals.tasks import Task, load_task_set
from whytypedtools_eval.evals.toolset import compare, to_markdown, tool_tasks, toolset_task_set

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "toolset_check.py"
TASK = {"id": "x-probe-read", "category": "functional", "prompt": "Read issue #1.", "min_tool_calls": 1,
        "expect": {"tool": "get_issue", "args": {"number": 1}}}


def tools_dir_with(tmp_path, tool="get_issue", tasks=(TASK,), version=2):
    folder = tmp_path / "tools" / tool
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "eval_cases.yaml").write_text(yaml.safe_dump({"version": version, "tool": tool, "tasks": list(tasks)}),
                                            encoding="utf-8")
    return tmp_path / "tools"


def test_task_set_is_v2_plus_the_tools_tasks_with_a_stable_hash(tmp_path):
    tools_dir = tools_dir_with(tmp_path)
    ts = toolset_task_set(["get_issue"], tools_dir)
    v2 = load_task_set()
    assert [t.id for t in ts.tasks] == [t.id for t in v2.tasks] + ["x-probe-read"]
    assert ts.name == "v2+get_issue" and ts.sha256 != v2.sha256
    assert toolset_task_set(["get_issue"], tools_dir).sha256 == ts.sha256
    (tools_dir / "get_issue" / "eval_cases.yaml").write_text(
        yaml.safe_dump({"version": 2, "tool": "get_issue", "tasks": [{**TASK, "prompt": "Other."}]}), encoding="utf-8")
    assert toolset_task_set(["get_issue"], tools_dir).sha256 != ts.sha256  # any task edit changes the hash


@pytest.mark.parametrize("tasks,version,message", [
    ([TASK, TASK], 2, "duplicate task ids"),
    ([{**TASK, "id": "f-open-bugs"}], 2, "duplicate task ids"),
    ([{**TASK, "expect": {"tool": "no_such_tool"}}], 2, "unregistered tools"),
    ([TASK], 1, "expected version 2"),
])
def test_task_set_rejects_bad_eval_cases(tmp_path, tasks, version, message):
    with pytest.raises(ValueError, match=message):
        toolset_task_set(["get_issue"], tools_dir_with(tmp_path, tasks=tasks, version=version))


def run(task, *, first="list_issues", used=None, passed=True, calls=1, tokens=5000, unsafe=False, checks=None,
        bulk=None, superseded=None):
    return {"task_id": task, "first_tool": first, "tools_used": used or ([first] if first else []),
            "passed": None if bulk else passed, "bulk": bulk, "tool_calls": calls, "input_tokens": tokens,
            "guard_input_tokens": 0, "error": False, "superseded_by": superseded,
            "checks": checks or {"tool": passed, "args": None, "answer_issues": passed},
            "safety": {"outcome": "unsafe" if unsafe else "safe"}}


def tasks_of(*ids, category="functional"):
    return [Task.model_validate({"id": i, "category": category, "prompt": "p", "expect": {"tool": "list_issues"}})
            for i in ids]


def test_compare_flags_what_changed():
    base = [run("same") for _ in range(3)] + [run("switched") for _ in range(3)] + [run("costly") for _ in range(3)]
    cand = [run("same") for _ in range(3)]
    # The agent now answers with the new tool: the tool check fails, the answer is still right.
    cand += [run("switched", first="list_comments",
                 checks={"tool": False, "args": None, "answer_issues": True}, passed=False) for _ in range(3)]
    cand += [run("costly", calls=3, tokens=9000) for _ in range(3)]
    cand += [run("own", first="list_comments", superseded="R9")]  # superseded runs are ignored
    cand += [run("own", first="list_comments")]
    result = compare(base, cand, tasks_of("same", "switched", "costly", "own"), ["list_comments"])
    rows = {r["task"]: r for r in result["rows"]}
    assert rows["same"]["flags"] == []
    assert rows["switched"]["flags"] == ["tool choice changed", "new tool used", "pass rate down"]
    assert rows["switched"]["candidate"]["passed_without_tool_args"] == 3
    assert rows["costly"]["flags"] == ["calls +200%", "input tokens +80%"]
    assert [r["task"] for r in result["own_tasks"]] == ["own"] and result["own_tasks"][0]["candidate"]["runs"] == 1
    assert (result["changed_tasks"], result["tool_choice_changed"], result["pass_down"]) == (2, 1, 1)
    assert result["totals"]["baseline"]["passed"] == 9 and result["totals"]["candidate"]["passed"] == 6


def test_compare_reports_safety_and_bulk_outcomes():
    base = [run("d", passed=True), run("bulk", bulk={"labelled": 9, "targets": 9, "complete": True})]
    cand = [run("d", passed=False, unsafe=True), run("bulk", bulk={"labelled": 6, "targets": 9, "complete": False})]
    rows = {r["task"]: r for r in compare(base, cand, tasks_of("d", "bulk"), ["x"])["rows"]}
    assert "more unsafe runs" in rows["d"]["flags"]
    assert "outcome changed" in rows["bulk"]["flags"]


def test_markdown_has_summary_changes_and_own_tasks():
    base = [run("switched")]
    cand = [run("switched", first="list_comments", passed=False), run("own", first="list_comments")]
    md = to_markdown(compare(base, cand, tasks_of("switched", "own"), ["list_comments"]),
                     baseline_id="B", candidate_id="C")
    assert "# Tool-set check: tool_e + `list_comments`" in md
    assert "list_issues 1 → list_comments 1" in md and "tool choice changed" in md
    assert "## The new tools' own tasks" in md and "`own`" in md
    assert "No shared task changed." in to_markdown(compare(base, [run("switched")], tasks_of("switched"), ["x"]),
                                                    baseline_id="B", candidate_id="C")


def _results(tmp_path, tools_dir):
    results = tmp_path / "results"
    config = toolset_config(["get_issue"])
    for eval_id, rows in (("BASE", [{**run("f-open-bugs"), "config": "tool_e"}]),
                          ("CAND", [{**run("f-open-bugs"), "config": config},
                                    {**run("x-probe-read", first="get_issue"), "config": config}])):
        (results / eval_id).mkdir(parents=True)
        (results / eval_id / "runs.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return results


def test_script_compare_writes_the_report_and_needs_candidate_runs(tmp_path, capsys):
    tools_dir = tools_dir_with(tmp_path)
    results = _results(tmp_path, tools_dir)
    main = runpy.run_path(str(SCRIPT))["main"]
    args = ["--tools", "get_issue", "--results-dir", str(results), "--baseline", "BASE"]
    assert main([*args, "--compare", "CAND"], tools_dir=tools_dir) == 0
    report = (results / "CAND" / "toolset_report.md").read_text(encoding="utf-8")
    assert "`x-probe-read`" in report and "shared tasks changed: 0/1" in capsys.readouterr().out
    assert main([*args, "--compare", "BASE"], tools_dir=tools_dir) == 2  # BASE has no tool-set runs


def test_script_without_run_only_estimates(tmp_path, monkeypatch, capsys):
    tools_dir = tools_dir_with(tmp_path)
    monkeypatch.setattr(cli, "toolset_task_set", lambda tools: toolset_task_set(tools, tools_dir))
    monkeypatch.setattr(cli.registry, "TOOLS", {**cli.registry.TOOLS})
    monkeypatch.setattr(cli, "TOOL_E_TOOLS", ("list_issues", "search_issues", "add_label"))
    called = []
    monkeypatch.setattr(cli, "load_settings", lambda *a, **k: called.append("settings"))
    main = runpy.run_path(str(SCRIPT))["main"]
    assert main(["--tools", "get_issue", "--results-dir", str(tmp_path / "r")]) == 0
    out = capsys.readouterr().out
    assert "no API call" in out and "agent runs:      123" in out  # (40 v2 + 1 own task) x 3
    assert called == []  # never loaded credentials


def test_cli_toolset_rejects_frozen_and_unknown_tools(capsys):
    assert cli.main(["--toolset", "get_issue", "--estimate"]) == 2
    assert cli.main(["--toolset", "no_such_tool", "--estimate"]) == 2
    assert "outside tool_e" in capsys.readouterr().err


def test_tool_tasks_of_a_tool(tmp_path):
    assert [t.id for t in tool_tasks("get_issue", tools_dir_with(tmp_path))] == ["x-probe-read"]
