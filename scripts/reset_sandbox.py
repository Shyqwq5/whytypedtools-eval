"""Thin wrapper; see whytypedtools_eval.sandbox.cli. Run: uv run python scripts/reset_sandbox.py --help"""

import sys

from whytypedtools_eval.sandbox.cli import reset_main

if __name__ == "__main__":
    sys.exit(reset_main())
