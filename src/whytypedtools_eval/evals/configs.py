"""Tool configurations under test. Each run gets a fresh setup (and write log).

Only tool_e (typed tools) exists so far; the generic GitHub API baseline is
designed in docs/design/generic-api-baseline.md and will add its variants here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from whytypedtools_eval.agent.loop import ToolCaller
from whytypedtools_eval.github import GitHubClient
from whytypedtools_eval.tools import registry
from whytypedtools_eval.tools.base import ToolContext, WriteMode


@dataclass
class ToolSetup:
    config: str
    typed: bool
    tools: list[dict[str, Any]]
    call_tool: ToolCaller
    ctx: ToolContext
    metadata: dict[str, Any] = field(default_factory=dict)


def typed_tools(client: GitHubClient, repo: str, write_mode: WriteMode, task: str) -> ToolSetup:
    ctx = ToolContext(client, repo, write_mode=write_mode)
    return ToolSetup(
        config="tool_e",
        typed=True,
        tools=registry.list_tools(),
        call_tool=lambda name, args: registry.call_tool(ctx, name, args),
        ctx=ctx,
    )


SetupFactory = Callable[[GitHubClient, str, WriteMode, str], ToolSetup]

CONFIGS: dict[str, SetupFactory] = {"tool_e": typed_tools}
