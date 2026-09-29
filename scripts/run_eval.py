"""Thin wrapper; see whytypedtools_eval.evals.cli. Run: uv run python scripts/run_eval.py --help

    uv run python scripts/run_eval.py --estimate            # cost only, no API calls
    uv run python scripts/run_eval.py --runs 1 --tasks f-open-bugs   # dry run (no writes)
    uv run python scripts/run_eval.py --live                # writes to the sandbox; resets it
"""

import sys

from whytypedtools_eval.evals.cli import main

if __name__ == "__main__":
    sys.exit(main())
