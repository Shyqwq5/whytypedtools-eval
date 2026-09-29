"""Command-line entry point for scripts/run_agent.py."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from whytypedtools_eval.agent.cohere_model import DEFAULT_SEED, DEFAULT_TEMPERATURE, CohereModel
from whytypedtools_eval.agent.loop import (
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_TRACE_DIR,
    PROJECT_ROOT,
    SystemPrompt,
    ToolCaller,
    run_agent,
)
from whytypedtools_eval.agent.model import ChatModel
from whytypedtools_eval.agent.trace import git_info
from whytypedtools_eval.config import ConfigError, Settings, load_settings
from whytypedtools_eval.tools import registry
from whytypedtools_eval.tools.base import ToolContext

ModelFactory = Callable[[Settings, argparse.Namespace], ChatModel]
ToolCallerFactory = Callable[[Settings], ToolCaller]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run the test agent on one task against the sandbox repo.")
    p.add_argument("task", help="The user request for the agent.")
    p.add_argument("--model", help="Cohere model name. Defaults to COHERE_MODEL or the built-in default.")
    p.add_argument("--max-tool-calls", type=int, default=DEFAULT_MAX_TOOL_CALLS)
    p.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED, help="Best-effort sampling seed.")
    p.add_argument("--no-seed", action="store_true", help="Do not send a seed.")
    p.add_argument("--thinking", choices=["enabled", "disabled", "api-default"], default="enabled",
                   help="Cohere reasoning setting. api-default sends nothing.")
    p.add_argument("--system-prompt", type=Path, default=DEFAULT_SYSTEM_PROMPT)
    p.add_argument("--trace-dir", type=Path, default=DEFAULT_TRACE_DIR)
    return p


def _cohere_model(settings: Settings, args: argparse.Namespace) -> ChatModel:
    return CohereModel.from_settings(
        settings,
        model=args.model,
        temperature=args.temperature,
        seed=None if args.no_seed else args.seed,
        thinking=None if args.thinking == "api-default" else args.thinking,
    )


def _registry_caller(settings: Settings) -> ToolCaller:
    ctx = ToolContext.from_settings(settings)
    return lambda name, args: registry.call_tool(ctx, name, args)


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    model_factory: ModelFactory = _cohere_model,
    tool_caller_factory: ToolCallerFactory = _registry_caller,
) -> int:
    args = _parser().parse_args(argv)
    # Issue text can contain characters the Windows console encoding lacks.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if args.max_tool_calls < 0:
        print("error: --max-tool-calls must be >= 0", file=sys.stderr)
        return 2
    try:
        settings = settings or load_settings(PROJECT_ROOT / ".env")
        model = model_factory(settings, args)
        system_prompt = SystemPrompt.load(args.system_prompt)
    except (ConfigError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    secrets = [settings.github_token.get_secret_value()]
    if settings.cohere_api_key is not None:
        secrets.append(settings.cohere_api_key.get_secret_value())

    result = run_agent(
        args.task,
        model=model,
        call_tool=tool_caller_factory(settings),
        system_prompt=system_prompt,
        max_tool_calls=args.max_tool_calls,
        trace_dir=args.trace_dir,
        secrets=secrets,
        metadata=git_info(PROJECT_ROOT),
    )
    print(result.final_answer if result.final_answer is not None else "(no answer)")
    print()
    print(f"status: {result.status}  model calls: {result.model_calls}  tool calls: {result.tool_calls}")
    print(f"trace: {result.trace_path}")
    return 0 if result.status == "completed" else 1
