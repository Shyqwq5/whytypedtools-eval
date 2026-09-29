"""Estimate model calls, tokens, GitHub requests and time for an eval plan.

No API is called. Per-run averages come from earlier eval results for the same
configuration and task category (results/*/runs.jsonl) when available, and
otherwise from rough defaults based on the first real agent runs.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

from whytypedtools_eval.evals.tasks import Task

# (model calls, input tokens, output tokens) per run for typed tools, by category.
# Based on the first real runs (2 model calls, ~4k input, ~450 output tokens for a
# one-tool task, thinking enabled); padded for multi-step tasks.
DEFAULTS: dict[str, tuple[float, float, float]] = {
    "functional": (2.4, 4800, 550),
    "tool_selection": (2.2, 4300, 500),
    "dangerous": (1.4, 2500, 350),
    "injection": (2.8, 6500, 700),
}
GITHUB_READS_PER_RUN = 3
# A drift check reads labels, issues and every issue's comments; a reset after a
# write adds a few writes and a search-index check.
REQUESTS_PER_RESET = 32
SECONDS_PER_RUN = 7
SECONDS_PER_RESET = 20
LOW, HIGH = 0.7, 1.5


def load_history(results_dir: Path) -> dict[tuple[str, str], tuple[float, ...]]:
    """(config, category) -> (model calls, input tokens, output tokens, GitHub requests or -1)."""
    samples: dict[tuple[str, str], list[tuple[float, ...]]] = defaultdict(list)
    for path in sorted(results_dir.glob("*/runs.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if r.get("input_tokens") is None or r.get("error"):
                continue
            gh = r.get("github_requests")
            samples[(r["config"], r["category"])].append(
                (r.get("model_calls") or 0, r["input_tokens"], r.get("output_tokens") or 0, -1 if gh is None else gh)
            )
    out: dict[tuple[str, str], tuple[float, ...]] = {}
    for k, v in samples.items():
        gh_known = [x[3] for x in v if x[3] >= 0]
        out[k] = (*(statistics.fmean(x[i] for x in v) for i in range(3)),
                  statistics.fmean(gh_known) if gh_known else -1)
    return out


def estimate(tasks: list[Task], configs: list[str], runs: int, results_dir: Path) -> dict[str, Any]:
    history = load_history(results_dir)
    calls = tokens_in = tokens_out = gh_reads = 0.0
    sources: dict[str, str] = {}
    for config in configs:
        for task in tasks:
            per = history.get((config, task.category))
            sources[f"{config}/{task.category}"] = "measured" if per else "default"
            per = per or (*DEFAULTS[task.category], -1)
            calls += per[0] * runs
            tokens_in += per[1] * runs
            tokens_out += per[2] * runs
            gh_reads += (per[3] if per[3] >= 0 else GITHUB_READS_PER_RUN) * runs
    n_runs = runs * len(tasks) * len(configs)
    writing = sum(1 for t in tasks if t.expect.writes) * runs * len(configs)
    return {
        "agent_runs": n_runs,
        "model_calls": round(calls),
        "input_tokens": round(tokens_in),
        "output_tokens": round(tokens_out),
        "range": {"low": LOW, "high": HIGH},
        "github_requests": round(gh_reads) + (writing + 1) * REQUESTS_PER_RESET,
        "expected_resets": writing,
        "minutes": round((n_runs * SECONDS_PER_RUN + (writing + 1) * SECONDS_PER_RESET) / 60),
        "sources": sources,
    }


def format_estimate(est: dict[str, Any]) -> str:
    lo, hi = est["range"]["low"], est["range"]["high"]

    def span(x: int) -> str:
        return f"{round(x * lo):,}-{round(x * hi):,} (point {x:,})"

    measured = sorted(k for k, v in est["sources"].items() if v == "measured")
    return "\n".join([
        f"agent runs:      {est['agent_runs']}",
        f"model calls:     {span(est['model_calls'])}",
        f"input tokens:    {span(est['input_tokens'])}",
        f"output tokens:   {span(est['output_tokens'])}",
        f"GitHub requests: ~{est['github_requests']:,} (limit 5,000/hour); resets after writes: ~{est['expected_resets']}",
        f"wall time:       ~{est['minutes']} min",
        f"per-run averages measured for: {', '.join(measured) or 'none (defaults used)'}",
    ])
