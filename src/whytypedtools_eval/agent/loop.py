"""The agent loop: model <-> tools until a final answer or a stop condition.

Stop conditions (the run's `status`):
- completed:      the model answered in text without requesting tools.
- no_answer:      the model stopped without text and without tool calls.
- max_tool_calls: the model requested more tool calls than the budget. Calls over
                  budget are not executed (they get a `budget_exceeded` result),
                  then the model gets one last turn with tools disabled.
- max_tokens:     the model's output was cut off.
- model_error:    the model API failed (after retries) or reported an error.

Every model turn requests at least one tool call or ends the run, so the tool
budget also bounds the number of model calls (at most max_tool_calls + 2).
Tool failures of any kind become tool results for the model; they never end the run.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from whytypedtools_eval.agent.model import ChatModel, Message, ModelError, ModelTurn, ToolCall
from whytypedtools_eval.agent.trace import (
    TRACE_VERSION,
    TraceWriter,
    new_run_id,
    sha256_json,
    sha256_text,
    utc_now,
)
from whytypedtools_eval.tools import registry

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SYSTEM_PROMPT = PROJECT_ROOT / "prompts" / "system.md"
DEFAULT_TRACE_DIR = PROJECT_ROOT / "runs"
DEFAULT_MAX_TOOL_CALLS = 10

ToolCaller = Callable[[str, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class SystemPrompt:
    path: Path
    text: str

    @classmethod
    def load(cls, path: Path = DEFAULT_SYSTEM_PROMPT) -> SystemPrompt:
        return cls(path, path.read_text(encoding="utf-8").strip())

    def display_path(self) -> str:
        try:
            return self.path.resolve().relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            return str(self.path)


@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: str
    final_answer: str | None
    trace_path: Path
    model_calls: int
    tool_calls: int


def _error_result(type_: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {"type": type_, "message": message, "retryable": False, "retry_after_seconds": None},
    }


def _parse_arguments(raw: str) -> dict[str, Any] | None:
    try:
        value = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _execute(call_tool: ToolCaller, call: ToolCall, args: dict[str, Any] | None) -> dict[str, Any]:
    if args is None:
        return _error_result("invalid_input", "Tool arguments must be a JSON object.")
    try:
        return call_tool(call.name, args)
    except Exception as exc:  # noqa: BLE001 - a broken tool must not end the run
        # Class name only: exception text may carry request details.
        return _error_result("internal_error", f"The tool failed unexpectedly ({type(exc).__name__}).")


class _Totals:
    def __init__(self) -> None:
        self.model_calls = 0
        self.tool_calls = 0
        self.tokens: dict[str, int | None] = {"input_tokens": None, "output_tokens": None}

    def add_usage(self, turn: ModelTurn) -> None:
        for key in self.tokens:
            value = getattr(turn.usage, key)
            if value is not None:
                self.tokens[key] = (self.tokens[key] or 0) + value


def run_agent(
    task: str,
    *,
    model: ChatModel,
    call_tool: ToolCaller,
    tools: list[dict[str, Any]] | None = None,
    system_prompt: SystemPrompt | None = None,
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
    trace_dir: Path = DEFAULT_TRACE_DIR,
    secrets: Sequence[str] = (),
    metadata: dict[str, Any] | None = None,
) -> RunResult:
    if max_tool_calls < 0:
        raise ValueError("max_tool_calls must be >= 0")
    tools = registry.list_tools() if tools is None else tools
    system_prompt = system_prompt or SystemPrompt.load()
    started_at = utc_now()
    run_id = new_run_id(started_at)
    t0 = time.perf_counter()
    totals = _Totals()

    with TraceWriter.for_run(trace_dir, run_id, started_at, secrets=list(secrets)) as trace:

        def finish(status: str, answer: str | None, **extra: Any) -> RunResult:
            trace.write(
                "run_end",
                status=status,
                final_answer=answer,
                totals={
                    "model_calls": totals.model_calls,
                    "tool_calls": totals.tool_calls,
                    **totals.tokens,
                    "latency_ms": round((time.perf_counter() - t0) * 1000),
                },
                **extra,
            )
            return RunResult(run_id, status, answer, trace.path, totals.model_calls, totals.tool_calls)

        trace.write(
            "run_start",
            trace_version=TRACE_VERSION,
            run_id=run_id,
            started_at=started_at.isoformat(timespec="seconds"),
            task=task,
            model=model.describe(),
            max_tool_calls=max_tool_calls,
            system_prompt={"path": system_prompt.display_path(), "sha256": sha256_text(system_prompt.text)},
            tools=[
                {
                    "name": t["name"],
                    "description_sha256": sha256_text(t["description"]),
                    "schema_sha256": sha256_json(t["input_schema"]),
                }
                for t in tools
            ],
            **(metadata or {}),
        )

        messages: list[Message] = [
            {"role": "system", "content": system_prompt.text},
            {"role": "user", "content": task},
        ]
        budget_hit = False
        try:
            while True:
                allow_tools = not budget_hit
                step = totals.model_calls + 1
                started = time.perf_counter()
                try:
                    turn = model.step(messages, tools, allow_tools=allow_tools)
                except ModelError as exc:
                    totals.model_calls += 1
                    trace.write(
                        "model_call",
                        step=step,
                        allow_tools=allow_tools,
                        latency_ms=round((time.perf_counter() - started) * 1000),
                        retries=[r.__dict__ for r in exc.retries],
                        error={"message": exc.message, "status": exc.status},
                    )
                    return finish("model_error", None)
                totals.model_calls += 1
                totals.add_usage(turn)
                trace.write(
                    "model_call",
                    step=step,
                    allow_tools=allow_tools,
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    retries=[r.__dict__ for r in turn.retries],
                    finish_reason=turn.finish_reason,
                    text=turn.text,
                    tool_plan=turn.tool_plan,
                    thinking=turn.thinking,
                    tool_calls=[{"id": c.id, "name": c.name, "arguments": c.arguments} for c in turn.tool_calls],
                    usage=turn.usage.to_dict(),
                )

                if turn.finish_reason in ("error", "timeout"):
                    return finish("model_error", turn.text)
                if budget_hit:
                    # Tools were disabled; any tool calls here are ignored.
                    return finish("max_tool_calls", turn.text)
                if turn.finish_reason == "max_tokens":
                    return finish("max_tokens", turn.text)
                if not turn.tool_calls:
                    return finish("completed" if turn.text else "no_answer", turn.text)

                messages.append(
                    {"role": "assistant", "content": turn.text, "tool_plan": turn.tool_plan,
                     "thinking": turn.thinking, "tool_calls": turn.tool_calls}
                )
                # Execute in the order the model gave; the budget counts calls, not turns.
                for call in turn.tool_calls:
                    args = _parse_arguments(call.arguments)
                    started = time.perf_counter()
                    if totals.tool_calls >= max_tool_calls:
                        budget_hit = True
                        executed = False
                        result = _error_result(
                            "budget_exceeded",
                            f"Tool call budget of {max_tool_calls} reached; this call was not run. "
                            "Answer with the information you already have.",
                        )
                    else:
                        executed = True
                        totals.tool_calls += 1
                        result = _execute(call_tool, call, args)
                    trace.write(
                        "tool_call",
                        step=step,
                        call_id=call.id,
                        name=call.name,
                        arguments=args if args is not None else call.arguments,
                        executed=executed,
                        ok=bool(result.get("ok")),
                        error_type=(result.get("error") or {}).get("type"),
                        result=result,
                        latency_ms=round((time.perf_counter() - started) * 1000),
                    )
                    messages.append(
                        {"role": "tool", "tool_call_id": call.id,
                         "content": json.dumps(result, ensure_ascii=False)}
                    )
        except Exception as exc:
            # A bug in the loop itself: leave a closing record, then fail loudly.
            finish("crashed", None, error={"type": type(exc).__name__})
            raise
