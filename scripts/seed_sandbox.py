"""Thin wrapper; see whytypedtools_eval.sandbox.cli. Run: uv run python scripts/seed_sandbox.py --help"""

import sys

from whytypedtools_eval.sandbox.cli import seed_main

if __name__ == "__main__":
    sys.exit(seed_main())
