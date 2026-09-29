"""Provider-neutral types for the agent loop.

The loop talks to a `ChatModel`; the Cohere adapter and the scripted test model
both implement it. Messages are plain dicts in a neutral shape:

- {"role": "system" | "user", "content": str}
- {"role": "assistant", "content": str | None, "tool_plan": str | None,
   "thinking": str | None, "tool_calls": list[ToolCall]}
- {"role": "tool", "tool_call_id": str, "content": str}   # JSON-encoded result
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

Message = dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    # Raw JSON string as produced by the model; parsed (and validated) by the loop.
    arguments: str


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    billed_input_tokens: int | None = None
    billed_output_tokens: int | None = None

    def to_dict(self) -> dict[str, int | None]:
        return asdict(self)


@dataclass(frozen=True)
class Retry:
    """One failed attempt that was retried; recorded in the trace."""

    reason: str
    wait_seconds: float


@dataclass(frozen=True)
class ModelTurn:
    text: str | None
    tool_calls: list[ToolCall]
    # Normalised to: "complete", "tool_call", "max_tokens", "stop_sequence", "error", "timeout".
    finish_reason: str
    usage: Usage = field(default_factory=Usage)
    tool_plan: str | None = None
    # Reasoning text, if the model returned it (Cohere "thinking" content).
    thinking: str | None = None
    retries: list[Retry] = field(default_factory=list)


class ModelError(Exception):
    """The model API failed and retrying (more) will not help. Message is secret-free."""

    def __init__(self, message: str, *, status: int | None = None, retries: list[Retry] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.retries = retries or []


class ChatModel(Protocol):
    def describe(self) -> dict[str, Any]:
        """Provider, model name and sampling settings, as recorded in the trace."""
        ...

    def step(self, messages: list[Message], tools: list[dict[str, Any]], *, allow_tools: bool = True) -> ModelTurn:
        """Run one model call. `tools` are registry specs (name, description, input_schema).

        With `allow_tools=False` the model must answer in text (tools stay declared,
        because earlier messages reference them). Raises `ModelError` on failure.
        """
        ...
