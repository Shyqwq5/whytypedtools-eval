r"""Safety-vs-usability chart for the README (see whytypedtools_eval.evals.chart).

The README chart (docs/safety_usability.svg):

    uv run python scripts/make_chart.py \
        --point "tool_e: typed tools" 20260929T231329Z-d2cbba8c tool_e \
        --point "tool_e: typed, tuned" 20260930T002252Z-1d0367af tool_e \
        --point "tool_a: generic API, no guard" 20260930T111611Z-f7571c8a tool_a \
        --point "tool_d: generic API + rules + LLM guard" 20260930T111611Z-f7571c8a tool_d \
        --note "Automatic scores, dry-run, command-a-plus-05-2026. Per configuration: 93 benign and 45 safety runs; bulk-label tasks reported separately." \
        --out docs/safety_usability.svg

Reads only results/ (no API calls).
"""

import argparse
import sys
from pathlib import Path

from whytypedtools_eval.evals.chart import load_records, point_from_records, render_svg

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--point", nargs=3, action="append", required=True, metavar=("LABEL", "EVAL_ID", "CONFIG"),
                   help="one configuration of one eval; repeat for each point")
    p.add_argument("--results-dir", type=Path, default=ROOT / "results")
    p.add_argument("--title", default="Safety vs usability (task set v2, 3 runs per task)")
    p.add_argument("--note", default="")
    p.add_argument("--out", type=Path, default=ROOT / "docs" / "safety_usability.svg")
    args = p.parse_args(argv)
    try:
        points = [point_from_records(label, load_records(args.results_dir, eval_id, config))
                  for label, eval_id, config in args.point]
    except (OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_svg(points, args.title, args.note), encoding="utf-8", newline="\n")
    for pt in points:
        print(f"{pt.label}: solved {pt.passed}/{pt.benign}, unsafe {pt.unsafe}/{pt.safety_runs}, "
              f"over-blocked {pt.over_blocked}, {pt.input_per_run:,.0f} input/run")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
