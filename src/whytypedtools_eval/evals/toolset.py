"""The tool-set check: what registering new tools does to the typed agent.

A new tool can change behaviour on tasks it was never meant for (the agent now picks
it instead of an existing tool), so the check reruns every v2 task with the new tool
registered, plus the new tool's own tasks, as configuration `tool_e+<name>`, and
compares each task with the current typed results (docs/design/tool-gate.md).

- `toolset_task_set(tools)`: the v2 tasks plus each tool's `eval_cases.yaml` tasks,
  as one task set whose hash is stable (so an interrupted run can be resumed).
- `compare(...)`: per-task changes in first tool, pass rate, calls and input tokens.
- `to_markdown(...)`: the report.

Nothing here calls a model; the paid run is `scripts/toolset_check.py --run`.
"""

from __future__ import annotations

import hashlib
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from whytypedtools_eval.evals.tasks import DEFAULT_TASKS, Task, TaskSet, load_task_set
from whytypedtools_eval.tools import registry

# The typed results a new tool set is compared with: tool_e with the current (tuned)
# descriptions, the eval whose tool hashes tests/tools/frozen_tools.json pins.
DEFAULT_BASELINE = "20260930T002252Z-1d0367af"
EVAL_CASES_VERSION = 2
# Relative change in mean calls or input tokens that is flagged.
COST_FLAG = 0.20


def eval_cases_path(tool: str, tools_dir: Path | None = None) -> Path:
    return (tools_dir or registry.TOOLS_DIR) / tool / "eval_cases.yaml"


def load_eval_cases(tool: str, tools_dir: Path | None = None) -> dict[str, Any]:
    """A tool's eval_cases.yaml. Version 2 holds tasks in the evals/tasks_v2.yaml schema."""
    data = yaml.safe_load(eval_cases_path(tool, tools_dir).read_text(encoding="utf-8"))
    if data.get("version") != EVAL_CASES_VERSION or data.get("tool") != tool:
        raise ValueError(f"{tool}/eval_cases.yaml: expected version {EVAL_CASES_VERSION} for tool {tool!r}")
    return data


def tool_tasks(tool: str, tools_dir: Path | None = None) -> list[Task]:
    return [Task.model_validate(t) for t in load_eval_cases(tool, tools_dir).get("tasks") or []]


def toolset_task_set(tools: list[str], tools_dir: Path | None = None,
                     base_path: Path = DEFAULT_TASKS) -> TaskSet:
    """v2 plus the tools' own tasks. The hash covers the v2 file and every eval_cases.yaml."""
    base = load_task_set(base_path)
    digest = hashlib.sha256(base.sha256.encode())
    tasks = list(base.tasks)
    for tool in tools:
        digest.update(eval_cases_path(tool, tools_dir).read_bytes())
        tasks += tool_tasks(tool, tools_dir)
    ids = [t.id for t in tasks]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"duplicate task ids across the tool set: {dupes}")
    unknown = sorted({t.expect.tool for t in tasks if t.expect.tool and t.expect.tool not in registry.TOOLS})
    if unknown:
        raise ValueError(f"tasks expect unregistered tools: {unknown}")
    return TaskSet(f"{base.name}+{'+'.join(tools)}", base_path, digest.hexdigest(), tasks)


# -- comparison -------------------------------------------------------------------


def _outcome(r: dict[str, Any]) -> str:
    if r.get("bulk"):
        b = r["bulk"]
        return f"bulk {b['labelled']}/{b['targets']}"
    return "pass" if r["passed"] else "fail"


def _without_tool_args(r: dict[str, Any]) -> bool | None:
    """Passed on answer, writes and safety alone (the tool and argument checks ignored)."""
    if r.get("passed") is None:
        return None
    if r.get("error"):
        return False
    rest = [v for k, v in r["checks"].items() if k not in ("tool", "args") and v is not None]
    return all(rest) and r["safety"]["outcome"] != "unsafe"


def task_stats(records: list[dict[str, Any]], new_tools: list[str]) -> dict[str, Any]:
    scored = [r for r in records if r.get("passed") is not None]
    return {
        "runs": len(records),
        "first_tool": dict(sorted(Counter(r.get("first_tool") or "(none)" for r in records).items())),
        "used_new_tool": sum(any(t in new_tools for t in r.get("tools_used") or []) for r in records),
        "passed": sum(bool(r["passed"]) for r in scored),
        "scored": len(scored),
        "passed_without_tool_args": sum(bool(_without_tool_args(r)) for r in scored),
        "unsafe": sum(r["safety"]["outcome"] == "unsafe" for r in records),
        "outcomes": dict(sorted(Counter(_outcome(r) for r in records).items())),
        "calls": statistics.fmean(r["tool_calls"] for r in records),
        "input_tokens": statistics.fmean((r.get("input_tokens") or 0) + (r.get("guard_input_tokens") or 0)
                                         for r in records),
    }


def _flags(base: dict[str, Any], cand: dict[str, Any]) -> list[str]:
    flags = []
    if base["first_tool"] != cand["first_tool"]:
        flags.append("tool choice changed")
    if cand["used_new_tool"]:
        flags.append("new tool used")
    if base["scored"] and cand["scored"]:
        b, c = base["passed"] / base["scored"], cand["passed"] / cand["scored"]
        if c < b:
            flags.append("pass rate down")
        elif c > b:
            flags.append("pass rate up")
    if cand["unsafe"] > base["unsafe"]:
        flags.append("more unsafe runs")
    elif base["outcomes"] != cand["outcomes"] and not base["scored"]:
        flags.append("outcome changed")
    for key, label in (("calls", "calls"), ("input_tokens", "input tokens")):
        if base[key] and abs(cand[key] - base[key]) / base[key] > COST_FLAG:
            flags.append(f"{label} {'+' if cand[key] > base[key] else '-'}{abs(cand[key] / base[key] - 1):.0%}")
    return flags


def compare(baseline: list[dict[str, Any]], candidate: list[dict[str, Any]], tasks: list[Task],
            new_tools: list[str]) -> dict[str, Any]:
    """Per-task comparison of the candidate tool set with the baseline typed results.

    Records are runs.jsonl rows; superseded runs are ignored. Tasks without baseline
    runs (the new tools' own tasks) are reported on their own."""
    def by_task(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for r in records:
            if not r.get("superseded_by"):
                out.setdefault(r["task_id"], []).append(r)
        return out

    base, cand = by_task(baseline), by_task(candidate)
    rows, own = [], []
    for task in tasks:
        if task.id not in cand:
            continue
        c = task_stats(cand[task.id], new_tools)
        if task.id in base:
            b = task_stats(base[task.id], new_tools)
            rows.append({"task": task.id, "category": task.category, "expected_tool": task.expect.tool,
                         "baseline": b, "candidate": c, "flags": _flags(b, c)})
        else:
            own.append({"task": task.id, "category": task.category, "expected_tool": task.expect.tool,
                        "candidate": c})

    def total(side: str, key: str, benign: bool | None = None) -> int:
        return sum(r[side][key] for r in rows
                   if benign is None or (r["category"] != "dangerous") == benign)

    return {
        "new_tools": new_tools,
        "shared_tasks": len(rows),
        "changed_tasks": sum(bool(r["flags"]) for r in rows),
        "tool_choice_changed": sum("tool choice changed" in r["flags"] for r in rows),
        "new_tool_used_in": sum(bool(r["candidate"]["used_new_tool"]) for r in rows),
        "pass_down": sum("pass rate down" in r["flags"] for r in rows),
        "totals": {side: {"passed": total(side, "passed"), "scored": total(side, "scored"),
                          "passed_without_tool_args": total(side, "passed_without_tool_args"),
                          "unsafe": total(side, "unsafe"),
                          "calls": round(sum(r[side]["calls"] * r[side]["runs"] for r in rows)),
                          "input_tokens": round(sum(r[side]["input_tokens"] * r[side]["runs"] for r in rows))}
                   for side in ("baseline", "candidate")},
        "rows": rows,
        "own_tasks": own,
    }


def _dist(d: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in d.items())


def _rate(s: dict[str, Any]) -> str:
    return f"{s['passed']}/{s['scored']}" if s["scored"] else _dist(s["outcomes"])


def to_markdown(report: dict[str, Any], *, baseline_id: str, candidate_id: str) -> str:
    t = report["totals"]
    tools = ", ".join(f"`{n}`" for n in report["new_tools"])
    lines = [
        f"# Tool-set check: tool_e + {tools}",
        "",
        f"Baseline: typed results [`{baseline_id}`](../{baseline_id}/) (tool_e, current descriptions). "
        f"Candidate: `{candidate_id}` (tool_e + {tools}, same tasks, model, prompt and budget). "
        "Automatic scores; see docs/design/tool-gate.md.",
        "",
        "## Summary",
        "",
        "| | Baseline | Candidate |",
        "|---|---|---|",
        f"| Shared tasks passed (runs) | {t['baseline']['passed']}/{t['baseline']['scored']} | "
        f"{t['candidate']['passed']}/{t['candidate']['scored']} |",
        f"| – without the tool/argument checks | {t['baseline']['passed_without_tool_args']} | "
        f"{t['candidate']['passed_without_tool_args']} |",
        f"| Unsafe runs | {t['baseline']['unsafe']} | {t['candidate']['unsafe']} |",
        f"| Tool calls (all shared runs) | {t['baseline']['calls']} | {t['candidate']['calls']} |",
        f"| Input tokens (all shared runs) | {t['baseline']['input_tokens']:,} | {t['candidate']['input_tokens']:,} |",
        "",
        f"- Shared tasks with any change: **{report['changed_tasks']}/{report['shared_tasks']}**; first-tool "
        f"choice changed in {report['tool_choice_changed']}; the new tool was used in "
        f"{report['new_tool_used_in']}; pass rate down in {report['pass_down']}.",
        "- A pass-rate drop with an unchanged \"without the tool/argument checks\" count means the agent "
        "answered correctly with the new tool where the task expects an existing one: decide whether the "
        "new tool is a legitimate alternative (then the task's expectation needs a new task-set version) "
        "or a confusion (then the new tool's description needs work).",
        "",
        "## Changed tasks",
        "",
    ]
    header = ["| Task | Expected tool | First tool: baseline → candidate | Passed | w/o tool/args | "
              "Calls / run | Input / run | Flags |", "|---|---|---|---|---|---|---|---|"]

    def row(r: dict[str, Any]) -> str:
        b, c = r["baseline"], r["candidate"]
        return (f"| `{r['task']}` | {r['expected_tool'] or '–'} | {_dist(b['first_tool'])} → {_dist(c['first_tool'])} | "
                f"{_rate(b)} → {_rate(c)} | {b['passed_without_tool_args']} → {c['passed_without_tool_args']} | "
                f"{b['calls']:.1f} → {c['calls']:.1f} | {b['input_tokens'] / 1000:.1f}k → "
                f"{c['input_tokens'] / 1000:.1f}k | {'; '.join(r['flags']) or '–'} |")

    changed = [r for r in report["rows"] if r["flags"]]
    lines += (header + [row(r) for r in changed]) if changed else ["No shared task changed."]
    lines += ["", "## The new tools' own tasks", ""]
    if report["own_tasks"]:
        lines += ["| Task | Category | Expected tool | First tool | Passed | Unsafe | Calls / run | Input / run |",
                  "|---|---|---|---|---|---|---|---|"]
        for r in report["own_tasks"]:
            c = r["candidate"]
            lines.append(f"| `{r['task']}` | {r['category']} | {r['expected_tool'] or '–'} | {_dist(c['first_tool'])} | "
                         f"{_rate(c)} | {c['unsafe']} | {c['calls']:.1f} | {c['input_tokens'] / 1000:.1f}k |")
    else:
        lines.append("None.")
    lines += ["", "## All shared tasks", ""] + header + [row(r) for r in report["rows"]]
    return "\n".join(lines) + "\n"
