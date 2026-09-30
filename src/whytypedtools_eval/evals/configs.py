"""Tool configurations under test. Each run gets a fresh setup (and write log).

- tool_e: typed tools, pinned to the four tools its reported results used.
- tool_e+<name>[+<name>...]: tool_e plus newly registered tools (the tool-set check,
  docs/design/tool-gate.md).
- tool_a: generic `github_api`, sandbox protections only.
- tool_d: generic `github_api` with the rule guard, then the LLM guard.

Design of the generic variants: docs/design/generic-api-baseline.md.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from whytypedtools_eval.agent.loop import ToolCaller
from whytypedtools_eval.agent.model import ChatModel
from whytypedtools_eval.agent.trace import sha256_text
from whytypedtools_eval.generic import tool as generic_tool
from whytypedtools_eval.generic.guards import GUARD_PROMPT, MAX_ISSUES_PER_RUN, GuardChain, LLMGuard, RuleGuard
from whytypedtools_eval.github import GitHubClient
from whytypedtools_eval.tools import registry
from whytypedtools_eval.tools.base import ToolContext, WriteMode


@dataclass
class SetupContext:
    client: GitHubClient
    repo: str
    write_mode: WriteMode
    task: str  # the resolved user prompt (guards judge requests against it)
    guard_model: ChatModel | None = None


@dataclass
class ToolSetup:
    config: str
    typed: bool
    tools: list[dict[str, Any]]
    call_tool: ToolCaller
    ctx: ToolContext
    metadata: dict[str, Any] = field(default_factory=dict)


# tool_e is pinned: registering a new tool must never change what tool_e means, or
# its reported results could no longer be reproduced. New tools are evaluated as
# tool_e+<name> (tests/tools/test_frozen_tools.py checks both).
TOOL_E_TOOLS: tuple[str, ...] = ("list_issues", "search_issues", "get_issue", "add_label")
TOOLSET_PREFIX = "tool_e+"


def typed_tools(sc: SetupContext, tools: Sequence[str] = TOOL_E_TOOLS, config: str = "tool_e") -> ToolSetup:
    ctx = ToolContext(sc.client, sc.repo, write_mode=sc.write_mode)
    allowed = tuple(tools)
    return ToolSetup(
        config=config,
        typed=True,
        tools=registry.list_tools(allowed),
        call_tool=lambda name, args: registry.call_tool(ctx, name, args, allowed=allowed),
        ctx=ctx,
    )


def _generic(config: str, sc: SetupContext, guard: GuardChain | None, meta: dict[str, Any]) -> ToolSetup:
    ctx = ToolContext(sc.client, sc.repo, write_mode=sc.write_mode)
    tool = generic_tool.GitHubApiTool(ctx, sc.task, guard=guard)
    return ToolSetup(
        config=config,
        typed=False,
        tools=[generic_tool.spec()],
        call_tool=tool,
        ctx=ctx,
        metadata={"response_cap_chars": generic_tool.RESPONSE_CAP_CHARS, **meta},
    )


def generic_no_guard(sc: SetupContext) -> ToolSetup:
    return _generic("tool_a", sc, None, {"guards": []})


def generic_rules_and_llm(sc: SetupContext) -> ToolSetup:
    if sc.guard_model is None:
        raise ValueError("tool_d needs a guard model")
    guard = GuardChain([RuleGuard(sc.repo), LLMGuard(sc.guard_model)])
    meta = {
        "guards": ["rules", "llm"],
        "rule_guard_max_issues": MAX_ISSUES_PER_RUN,
        "guard_prompt_sha256": sha256_text(GUARD_PROMPT.read_text(encoding="utf-8").strip()),
        "guard_model": sc.guard_model.describe(),
    }
    return _generic("tool_d", sc, guard, meta)


SetupFactory = Callable[[SetupContext], ToolSetup]

CONFIGS: dict[str, SetupFactory] = {
    "tool_e": typed_tools,
    "tool_a": generic_no_guard,
    "tool_d": generic_rules_and_llm,
}
# Configurations whose tool choice and arguments are checked directly against the
# task; the generic ones are checked through the committed mapping instead.
TYPED: frozenset[str] = frozenset({"tool_e"})
GENERIC: frozenset[str] = frozenset({"tool_a", "tool_d"})
NEEDS_GUARD_MODEL: frozenset[str] = frozenset({"tool_d"})


def toolset_config(extra: Sequence[str]) -> str:
    """The config name for tool_e plus `extra` registered tools, e.g. tool_e+list_comments."""
    return TOOLSET_PREFIX + "+".join(extra)


def _toolset_extra(config: str) -> tuple[str, ...] | None:
    if not config.startswith(TOOLSET_PREFIX):
        return None
    extra = tuple(config[len(TOOLSET_PREFIX):].split("+"))
    ok = extra and len(set(extra)) == len(extra) and all(
        n in registry.TOOLS and n not in TOOL_E_TOOLS for n in extra)
    return extra if ok else None


def resolve(config: str) -> SetupFactory | None:
    """The setup for a configuration name, including tool_e+<name> tool sets; None if unknown."""
    if config in CONFIGS:
        return CONFIGS[config]
    extra = _toolset_extra(config)
    if extra is None:
        return None
    tools = TOOL_E_TOOLS + extra
    return lambda sc: typed_tools(sc, tools, config)


def is_typed(config: str) -> bool:
    return config in TYPED or _toolset_extra(config) is not None
