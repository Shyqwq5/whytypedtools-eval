r"""Tool-set check (docs/design/tool-gate.md): tool_e plus new tools vs the current typed results.

    uv run python scripts/toolset_check.py                         # plan and cost only; no API call
    uv run python scripts/toolset_check.py --run                   # the paid run, then the report
    uv run python scripts/toolset_check.py --compare EVAL_ID       # report for an existing run

--tools defaults to every registered tool outside tool_e. The run repeats the 40 v2
tasks and the new tools' own tasks (their eval_cases.yaml) 3 times each with the
Cohere agent, in dry-run: about 350 Command A+ calls plus the new tasks, so it needs
the owner's quota and go-ahead. If it is interrupted, resume it with

    uv run python scripts/run_eval.py --toolset <tools...> --rerun-errors EVAL_ID

and then run --compare EVAL_ID. The report is written to results/EVAL_ID/toolset_report.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from whytypedtools_eval.evals import cli
from whytypedtools_eval.evals.configs import TOOL_E_TOOLS, toolset_config
from whytypedtools_eval.evals.toolset import DEFAULT_BASELINE, compare, to_markdown, toolset_task_set
from whytypedtools_eval.tools import registry

ROOT = Path(__file__).resolve().parents[1]


def _records(folder: Path, config: str) -> list[dict]:
    rows = [json.loads(line) for line in (folder / "runs.jsonl").read_text(encoding="utf-8").splitlines() if line]
    return [r for r in rows if r["config"] == config]


def report(results_dir: Path, candidate_id: str, tools: list[str], baseline_id: str,
           tools_dir: Path | None = None) -> Path:
    config = toolset_config(tools)
    candidate = _records(results_dir / candidate_id, config)
    if not candidate:
        raise ValueError(f"{candidate_id} has no runs of {config}")
    baseline = _records(results_dir / baseline_id, "tool_e")
    tasks = toolset_task_set(tools, tools_dir).tasks
    result = compare(baseline, candidate, tasks, tools)
    out = results_dir / candidate_id / "toolset_report.md"
    out.write_text(to_markdown(result, baseline_id=baseline_id, candidate_id=candidate_id),
                   encoding="utf-8", newline="\n")
    print(f"shared tasks changed: {result['changed_tasks']}/{result['shared_tasks']} "
          f"(first tool changed in {result['tool_choice_changed']}, new tool used in {result['new_tool_used_in']}, "
          f"pass rate down in {result['pass_down']})")
    print(f"wrote {out}")
    return out


def main(argv: list[str] | None = None, *, tools_dir: Path | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tools", nargs="+", help="new tools to add to tool_e (default: all registered outside tool_e)")
    p.add_argument("--baseline", default=DEFAULT_BASELINE, help=f"typed eval to compare with (default {DEFAULT_BASELINE})")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--results-dir", type=Path, default=ROOT / "results")
    action = p.add_mutually_exclusive_group()
    action.add_argument("--run", action="store_true", help="run the paid eval (Cohere), then write the report")
    action.add_argument("--compare", metavar="EVAL_ID", help="only write the report for an existing run")
    args = p.parse_args(argv)
    tools = args.tools or [n for n in registry.TOOLS if n not in TOOL_E_TOOLS]
    if not tools:
        print("error: no registered tool outside tool_e; nothing to check", file=sys.stderr)
        return 2
    try:
        if args.compare:
            report(args.results_dir, args.compare, tools, args.baseline, tools_dir)
            return 0
        common = ["--toolset", *tools, "--runs", str(args.runs), "--results-dir", str(args.results_dir)]
        if not args.run:
            print(f"tool-set check for {toolset_config(tools)} against {args.baseline} (no API call; add --run "
                  "to spend it)")
            return cli.main([*common, "--estimate"])
        before = set(args.results_dir.iterdir()) if args.results_dir.exists() else set()
        code = cli.main(common)
        new = sorted(set(args.results_dir.iterdir()) - before)
        if code != 0 or not new:
            return code or 1
        report(args.results_dir, new[-1].name, tools, args.baseline, tools_dir)
        return 0
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
