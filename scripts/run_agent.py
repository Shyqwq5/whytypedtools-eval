"""Thin wrapper; see whytypedtools_eval.agent.cli. Run: uv run python scripts/run_agent.py --help"""

import sys

from whytypedtools_eval.agent.cli import main

if __name__ == "__main__":
    sys.exit(main())
