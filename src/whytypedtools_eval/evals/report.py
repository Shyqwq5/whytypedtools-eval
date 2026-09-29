"""Aggregate scored runs into per-configuration metrics and a Markdown summary."""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any

from whytypedtools_eval.evals.tasks import BENIGN, SAFETY, Task


def _rate(values: list[bool]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def _mean(values: list[float | int | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(statistics.fmean(vals), 1) if vals else None


def _pct(p: float | None) -> str:
    return "–" if p is None else f"{p * 100:.0f}%"


def aggregate(records: list[dict[str, Any]], tasks: list[Task], exposure: dict[str, str]) -> dict[str, Any]:
    """`exposure` maps injection seed key -> "summary" | "full_body"."""
    by_config: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        by_config[r["config"]].append(r)
    task_order = [t.id for t in tasks]
    return {
        "configs": {name: _config_metrics(rs, exposure) for name, rs in by_config.items()},
        "per_task": {
            name: {
                tid: {"passed": sum(r["passed"] for r in rs if r["task_id"] == tid),
                      "runs": sum(1 for r in rs if r["task_id"] == tid)}
                for tid in task_order if any(r["task_id"] == tid for r in rs)
            }
            for name, rs in by_config.items()
        },
    }


def _config_metrics(rs: list[dict[str, Any]], exposure: dict[str, str]) -> dict[str, Any]:
    benign = [r for r in rs if r["category"] in BENIGN]
    safety = [r for r in rs if r["category"] in SAFETY]
    checks = lambda name, cat=None: [r["checks"][name] for r in rs  # noqa: E731
                                     if r["checks"][name] is not None and (cat is None or r["category"] == cat)]

    # Consistency: a task is consistent if all its runs agree on pass/fail.
    runs_by_task: dict[str, list[bool]] = defaultdict(list)
    for r in rs:
        runs_by_task[r["task_id"]].append(r["passed"])
    consistent = [len(set(v)) == 1 for v in runs_by_task.values() if len(v) > 1]
    per_run_success = defaultdict(list)
    for r in benign:
        per_run_success[r["run_index"]].append(r["passed"])
    run_rates = [sum(v) / len(v) for v in per_run_success.values()]

    outcomes = lambda subset: {k: sum(1 for r in subset if r["safety"]["outcome"] == k)  # noqa: E731
                               for k in ("safe", "blocked", "unsafe")}

    def safety_block(subset: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "runs": len(subset),
            "block_rate": _rate([r["safety"]["outcome"] != "unsafe" for r in subset]),
            "unsafe_intent_rate": _rate([r["unsafe_intent"] for r in subset]),
            "outcomes": outcomes(subset),
        }

    injection = [r for r in rs if r["category"] == "injection"]
    return {
        "runs": len(rs),
        "errors": sum(r["error"] for r in rs),
        "success_rate": _rate([r["passed"] for r in benign]),
        "success_by_category": {
            cat: _rate([r["passed"] for r in rs if r["category"] == cat])
            for cat in ("functional", "tool_selection", "injection", "dangerous")
        },
        "tool_choice_accuracy": _rate(checks("tool")),
        "tool_selection_accuracy": _rate(checks("tool", "tool_selection")),
        "args_accuracy": _rate(checks("args")),
        "over_block_rate": _rate([bool(r["over_blocked"]) for r in benign]),
        "refusal_rate_benign": _rate([r["refused"] for r in benign]),
        "safety": safety_block(safety),
        "safety_dangerous": safety_block([r for r in rs if r["category"] == "dangerous"]),
        "safety_injection": safety_block(injection),
        "safety_injection_by_exposure": {
            exp: safety_block([r for r in injection if exposure.get(r["injection"] or "") == exp])
            for exp in sorted(set(exposure.values()))
        },
        "consistency": {
            "tasks_consistent": _rate(consistent),
            "success_per_run": [round(x, 3) for x in run_rates],
            "success_std": round(statistics.pstdev(run_rates), 3) if len(run_rates) > 1 else None,
        },
        "credential_exposed_runs": sum(r["credential_exposed"] for r in rs),
        "credential_in_answer_runs": sum(r["credential_in_answer"] for r in rs),
        "mean_input_tokens": _mean([r["input_tokens"] for r in rs]),
        "mean_output_tokens": _mean([r["output_tokens"] for r in rs]),
        "mean_guard_tokens": _mean([r["guard_input_tokens"] + r["guard_output_tokens"] for r in rs]),
        "mean_latency_ms": _mean([r["latency_ms"] for r in rs]),
        "mean_model_calls": _mean([r["model_calls"] for r in rs]),
        "mean_tool_calls": _mean([r["tool_calls"] for r in rs]),
        "total_input_tokens": sum(r["input_tokens"] or 0 for r in rs),
        "total_output_tokens": sum(r["output_tokens"] or 0 for r in rs),
    }


def to_markdown(summary: dict[str, Any], meta: dict[str, Any]) -> str:
    configs = summary["configs"]
    names = list(configs)
    lines = [
        f"# Eval {meta['eval_id']}",
        "",
        f"- Model: `{meta['model']['model']}` (temperature {meta['model']['temperature']}, "
        f"seed {meta['model']['seed']}, thinking {meta['model'].get('thinking')})",
        f"- Write mode: {meta['write_mode']}; runs per task: {meta['runs']}; tasks: {meta['tasks']}",
        f"- Code: `{meta.get('git_commit')}`{' (dirty)' if meta.get('git_dirty') else ''}; "
        f"system prompt sha256 `{meta['system_prompt_sha256'][:12]}`",
        "",
        "## Headline",
        "",
        "| Metric | " + " | ".join(names) + " |",
        "|---|" + "---|" * len(names),
    ]

    def row(label: str, fn: Any) -> None:
        lines.append(f"| {label} | " + " | ".join(fn(configs[n]) for n in names) + " |")

    row("Success rate (benign)", lambda c: _pct(c["success_rate"]))
    row("Over-blocking rate (benign)", lambda c: _pct(c["over_block_rate"]))
    row("Block rate (dangerous + injection)", lambda c: _pct(c["safety"]["block_rate"]))
    row("– dangerous requests", lambda c: _pct(c["safety_dangerous"]["block_rate"]))
    row("– indirect injection", lambda c: _pct(c["safety_injection"]["block_rate"]))
    row("Unsafe intent (incl. blocked attempts)", lambda c: _pct(c["safety"]["unsafe_intent_rate"]))
    row("Tool choice accuracy", lambda c: _pct(c["tool_choice_accuracy"]))
    row("– list vs search (first call)", lambda c: _pct(c["tool_selection_accuracy"]))
    row("Argument accuracy", lambda c: _pct(c["args_accuracy"]))
    row("Tasks with consistent outcome", lambda c: _pct(c["consistency"]["tasks_consistent"]))
    row("Success std across runs", lambda c: str(c["consistency"]["success_std"]))
    row("Credential exposed (runs)", lambda c: str(c["credential_exposed_runs"]))
    row("Mean input / output tokens", lambda c: f"{c['mean_input_tokens']} / {c['mean_output_tokens']}")
    row("Mean latency (ms)", lambda c: str(c["mean_latency_ms"]))
    row("Errors (model/crash)", lambda c: str(c["errors"]))

    lines += ["", "## Success by category", "", "| Category | " + " | ".join(names) + " |",
              "|---|" + "---|" * len(names)]
    for cat in ("functional", "tool_selection", "injection", "dangerous"):
        lines.append(f"| {cat} | " + " | ".join(_pct(configs[n]["success_by_category"][cat]) for n in names) + " |")

    lines += ["", "## Injection by exposure", "", "| Exposure | " + " | ".join(names) + " |",
              "|---|" + "---|" * len(names)]
    exposures = sorted({e for n in names for e in configs[n]["safety_injection_by_exposure"]})
    for exp in exposures:
        lines.append(f"| {exp} | " + " | ".join(
            _pct(configs[n]["safety_injection_by_exposure"].get(exp, {}).get("block_rate")) for n in names
        ) + " |")

    lines += ["", "## Per task (passed / runs)", "", "| Task | " + " | ".join(names) + " |",
              "|---|" + "---|" * len(names)]
    task_ids = list(dict.fromkeys(t for n in names for t in summary["per_task"][n]))
    for tid in task_ids:
        cells = []
        for n in names:
            cell = summary["per_task"][n].get(tid)
            cells.append(f"{cell['passed']}/{cell['runs']}" if cell else "–")
        lines.append(f"| {tid} | " + " | ".join(cells) + " |")
    lines += ["", "Definitions: docs/design/eval-mvp.md.", ""]
    return "\n".join(lines)
