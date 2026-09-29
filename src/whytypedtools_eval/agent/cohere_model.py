"""Cohere Chat API (v2) adapter for the agent loop.

This is the only place that knows Cohere's message and tool formats. Tool
definitions are converted from the registry specs; nothing is written by hand.

Retries are done here, not by the SDK (its retries are disabled), so every retry
is visible in the trace. Error messages never include response bodies or headers.
"""

from __future__ import annotations

import dataclasses
import random
import time
from collections.abc import Callable
from typing import Any

import cohere
import httpx
from cohere.core.api_error import ApiError

from whytypedtools_eval.agent.model import Message, ModelError, ModelTurn, Retry, ToolCall, Usage
from whytypedtools_eval.config import Settings

DEFAULT_TEMPERATURE = 0.0
# Cohere's `seed` is best effort: identical requests may still differ.
DEFAULT_SEED = 0
# Reasoning is on by default for models that support it; we send the setting
# explicitly so a silent API default change cannot alter results.
DEFAULT_THINKING = "enabled"
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRIES = 4
BACKOFF_BASE_S = 2.0
# Longest single wait; a Retry-After above this fails the run instead of blocking.
MAX_WAIT_S = 60.0
REQUEST_TIMEOUT_S = 120.0


def to_cohere_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]},
        }
        for t in tools
    ]


def to_cohere_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m["role"]
        if role in ("system", "user"):
            out.append({"role": role, "content": m["content"]})
        elif role == "assistant":
            msg: dict[str, Any] = {"role": "assistant"}
            calls: list[ToolCall] = m.get("tool_calls") or []
            if calls:
                if m.get("tool_plan"):
                    msg["tool_plan"] = m["tool_plan"]
                # Send the model's own reasoning back so later steps keep its context.
                if m.get("thinking"):
                    msg["content"] = [{"type": "thinking", "thinking": m["thinking"]}]
                msg["tool_calls"] = [
                    {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                    for c in calls
                ]
            else:
                msg["content"] = m.get("content") or ""
            out.append(msg)
        elif role == "tool":
            # Tool results go back as documents: data for the model, not instructions.
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": m["tool_call_id"],
                    "content": [{"type": "document", "document": {"data": m["content"]}}],
                }
            )
        else:
            raise ValueError(f"unknown message role {role!r}")
    return out


def parse_response(resp: Any) -> ModelTurn:
    msg = resp.message
    texts = [item.text for item in (msg.content or []) if getattr(item, "type", None) == "text"]
    thoughts = [item.thinking for item in (msg.content or []) if getattr(item, "type", None) == "thinking"]
    calls = [
        ToolCall(id=c.id, name=(c.function.name if c.function else "") or "",
                 arguments=(c.function.arguments if c.function else None) or "{}")
        for c in (msg.tool_calls or [])
    ]
    return ModelTurn(
        text="".join(texts) if texts else None,
        tool_calls=calls,
        finish_reason=str(resp.finish_reason).lower(),
        usage=_usage(resp.usage),
        tool_plan=msg.tool_plan,
        thinking="\n".join(thoughts) if thoughts else None,
    )


def _usage(usage: Any) -> Usage:
    if usage is None:
        return Usage()

    def _int(obj: Any, name: str) -> int | None:
        value = getattr(obj, name, None) if obj is not None else None
        return int(value) if value is not None else None

    return Usage(
        input_tokens=_int(usage.tokens, "input_tokens"),
        output_tokens=_int(usage.tokens, "output_tokens"),
        billed_input_tokens=_int(usage.billed_units, "input_tokens"),
        billed_output_tokens=_int(usage.billed_units, "output_tokens"),
    )


def _retry_after(exc: ApiError) -> float | None:
    headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
    try:
        return float(headers["retry-after"])
    except (KeyError, ValueError):
        return None


class CohereModel:
    def __init__(
        self,
        client: Any,
        model: str,
        *,
        temperature: float = DEFAULT_TEMPERATURE,
        seed: int | None = DEFAULT_SEED,
        thinking: str | None = DEFAULT_THINKING,
        max_retries: int = MAX_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._client = client
        self.model = model
        self.temperature = temperature
        self.seed = seed
        self.thinking = thinking
        self.max_retries = max_retries
        self._sleep = sleep
        self._rng = rng or random.Random()

    @classmethod
    def from_settings(cls, settings: Settings, *, model: str | None = None, **kwargs: Any) -> CohereModel:
        key = settings.require_cohere_key()
        client = cohere.ClientV2(api_key=key.get_secret_value(), timeout=REQUEST_TIMEOUT_S)
        return cls(client, model or settings.cohere_model, **kwargs)

    def describe(self) -> dict[str, Any]:
        return {
            "provider": "cohere",
            "model": self.model,
            "temperature": self.temperature,
            "seed": self.seed,
            "thinking": self.thinking,
            "sdk_version": cohere.__version__,
        }

    def step(self, messages: list[Message], tools: list[dict[str, Any]], *, allow_tools: bool = True) -> ModelTurn:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": to_cohere_messages(messages),
            "tools": to_cohere_tools(tools),
            "temperature": self.temperature,
            "request_options": {"max_retries": 0},
        }
        if self.seed is not None:
            request["seed"] = self.seed
        if self.thinking is not None:
            request["thinking"] = {"type": self.thinking}
        if not allow_tools:
            request["tool_choice"] = "NONE"

        retries: list[Retry] = []
        while True:
            try:
                resp = self._client.chat(**request)
            except ApiError as exc:
                status = exc.status_code
                if status not in RETRYABLE_STATUSES:
                    raise ModelError(f"Cohere API returned HTTP {status}.", status=status, retries=retries) from None
                reason = f"http_{status}"
                hinted = _retry_after(exc)
            except httpx.TransportError as exc:
                status, reason, hinted = None, type(exc).__name__, None
            else:
                return dataclasses.replace(parse_response(resp), retries=retries)

            if len(retries) >= self.max_retries:
                raise ModelError(
                    f"Cohere API still failing after {len(retries)} retries ({reason}).",
                    status=status,
                    retries=retries,
                )
            backoff = BACKOFF_BASE_S * 2 ** len(retries) * (0.5 + self._rng.random() / 2)
            wait = max(hinted or 0.0, backoff)
            if wait > MAX_WAIT_S:
                raise ModelError(
                    f"Cohere API asked to wait {wait:.0f}s ({reason}); giving up.",
                    status=status,
                    retries=retries,
                )
            retries.append(Retry(reason=reason, wait_seconds=round(wait, 2)))
            self._sleep(wait)
