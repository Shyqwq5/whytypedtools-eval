"""Run the GitHub tools as an MCP server over stdio.

    uv run python scripts/mcp_server.py                  # writes are dry-run
    uv run python scripts/mcp_server.py --allow-writes   # add_label changes the sandbox

Reads GITHUB_TOKEN and SANDBOX_REPO from .env on the first tool call.
"""

import argparse
import sys

from whytypedtools_eval.config import load_settings
from whytypedtools_eval.mcp_server import build_server
from whytypedtools_eval.tools.base import ToolContext

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="GitHub tools MCP server (stdio).")
    p.add_argument("--allow-writes", action="store_true", help="Let write tools change the sandbox.")
    args = p.parse_args()
    mode = "live" if args.allow_writes else "dry_run"
    server = build_server(lambda: ToolContext.from_settings(load_settings(), write_mode=mode))
    server.run("stdio")
    sys.exit(0)
