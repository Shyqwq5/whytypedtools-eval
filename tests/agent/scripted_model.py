"""A ChatModel that replays a fixed script of turns. Never calls a real API."""

from __future__ import annotations

import copy
import json
from typing import Any

from whytypedtools_eval.agent.model import Message, ModelError, ModelTurn, ToolCall, Usage


def call(name: str, args: dict[str, Any] | str | None = None, id: str | None = None) -> ToolCall:
    raw = args if isinstance(args, str) else json.dumps(args or {})
    return ToolCall(id=id or f"call_{name}", name=name, arguments=raw)


def tools_turn(*calls: ToolCall, plan: str | None = "I will use a tool.") -> ModelTurn:
    return ModelTurn(text=None, tool_calls=list(calls), finish_reason="tool_call",
                     usage=Usage(input_tokens=100, output_tokens=10), tool_plan=plan)


def answer(text: str | None, finish_reason: str = "complete") -> ModelTurn:
    return ModelTurn(text=text, tool_calls=[], finish_reason=finish_reason,
                     usage=Usage(input_tokens=200, output_tokens=20))


class ScriptedModel:
    def __init__(self, *script: ModelTurn | ModelError | Exception) -> None:
        self.script = list(script)
        # What the model was shown on each call: (messages, allow_tools).
        self.seen: list[tuple[list[Message], bool]] = []

    def describe(self) -> dict[str, Any]:
        return {"provider": "scripted", "model": "scripted", "temperature": 0.0, "seed": None}

    def step(self, messages: list[Message], tools: list[dict[str, Any]], *, allow_tools: bool = True) -> ModelTurn:
        self.seen.append((copy.deepcopy(messages), allow_tools))
        if not self.script:
            raise AssertionError("scripted model ran out of turns")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
