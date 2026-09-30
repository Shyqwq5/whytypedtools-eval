"""Cohere Chat API (v2) adapter for the agent loop.

This is the only place that knows Cohere's message and tool formats. Tool
definitions are converted from the registry specs; nothing is written by hand.

Retries are done here, not by the SDK (its retries are disabled), so every retry
is visible in the trace. Error messages never include response bodies or headers.

`tool_choice` is never sent: command-a-plus-05-2026 rejects it with HTTP 400
("tool_choice is not supported for this model", found in the v2 baseline). When
the loop disables tools (budget reached), the tools stay declared and the loop
ignores any further tool calls; the budget_exceeded results ask the model to answer.
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
# 2, 4, 8, 16, 32, 60 s (with jitter): about 2 minutes, enough to outlast a
# per-minute rate limit window. 4 retries (~25 s) was not, in the v2 baseline.
MAX_RETRIES = 6
BACKOFF_BASE_S = 2.0
# Longest single wait. Backoff is capped at this; a Retry-After above it fails the
# run instead of blocking.
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


QUOTA_MESSAGE = (
    "Cohere monthly request limit reached (Trial keys, and production keys on newer Chat "
    "models such as Command A+, allow 1,000 API calls a month). Retrying will not help: "
    "wait for the monthly reset or raise the limit."
)
# Wordings of Cohere's monthly-limit 429 (lower case). The per-minute 429 ("You are
# past the per minute request limit, please wait and try again later") does not
# match and keeps being retried.
_MONTHLY_LIMIT_MARKERS = (
    "/ month",     # Trial key: "... limited to 1000 API calls / month"
    "per-month",   # model limit: "You are past the per-month request limit for this model ..."
)


def _is_quota_exhausted(exc: ApiError) -> bool:
    """A 429 for the monthly request limit, as opposed to a per-minute rate limit.

    Only the body's `message` field is inspected here.
    """
    body = exc.body if isinstance(exc.body, dict) else {}
    message = str(body.get("message", "")).lower().replace("/month", "/ month")
    return exc.status_code == 429 and any(marker in message for marker in _MONTHLY_LIMIT_MARKERS)


PROVIDER_TEXT_MAX_CHARS = 200


def _provider_text(exc: ApiError) -> str:
    """Cohere's own error message (the body's `message` field only, never headers),
    whitespace-collapsed and truncated. Traces redact secrets and results/ files pass
    through the results sanitiser, like every other field."""
    body = exc.body if isinstance(exc.body, dict) else {}
    text = " ".join(str(body.get("message", "")).split())
    if len(text) > PROVIDER_TEXT_MAX_CHARS:
        text = text[:PROVIDER_TEXT_MAX_CHARS] + "…"
    return text


def _retry_after(exc: ApiError) -> float | None:
    headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
    try:
        return float(headers["retry-after"])
    except (KeyError, ValueError):
        return None


class Pacer:
    """Client-side pacing: at least `min_interval_s` between request starts.

    One Pacer can be shared by several models (the agent and the LLM guard), so
    their requests are paced together.
    """

    def __init__(self, min_interval_s: float = 0.0, *, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.min_interval_s = min_interval_s
        self._sleep = sleep
        self._clock = clock
        self._last: float | None = None

    def wait(self) -> None:
        if self.min_interval_s > 0 and self._last is not None:
            remaining = self.min_interval_s - (self._clock() - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()


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
        min_interval_s: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
        pacer: Pacer | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> None:
        self._client = client
        self.model = model
        self.temperature = temperature
        self.seed = seed
        self.thinking = thinking
        # e.g. {"type": "json_object"} for the LLM guard; None = not sent.
        self.response_format = response_format
        self.max_retries = max_retries
        # Client-side pacing: at least this long between request starts (0 = off).
        self.min_interval_s = min_interval_s
        self._sleep = sleep
        self._pacer = pacer or Pacer(min_interval_s, sleep=sleep, clock=clock)
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
            "response_format": self.response_format,
            "sdk_version": cohere.__version__,
        }

    def step(self, messages: list[Message], tools: list[dict[str, Any]], *, allow_tools: bool = True) -> ModelTurn:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": to_cohere_messages(messages),
            "temperature": self.temperature,
            "request_options": {"max_retries": 0},
        }
        if tools:
            request["tools"] = to_cohere_tools(tools)
        if self.seed is not None:
            request["seed"] = self.seed
        if self.thinking is not None:
            request["thinking"] = {"type": self.thinking}
        if self.response_format is not None:
            request["response_format"] = self.response_format
        # allow_tools=False sends nothing extra: see the module docstring.

        retries: list[Retry] = []
        while True:
            self._pace()
            try:
                resp = self._client.chat(**request)
            except ApiError as exc:
                status = exc.status_code
                if _is_quota_exhausted(exc):
                    text = _provider_text(exc)
                    raise ModelError(QUOTA_MESSAGE + (f" Cohere: {text}" if text else ""), status=status,
                                     retries=retries, fatal=True) from None
                provider = _provider_text(exc)
                if status not in RETRYABLE_STATUSES:
                    detail = f": {provider}" if provider else "."
                    raise ModelError(f"Cohere API returned HTTP {status}{detail}", status=status,
                                     retries=retries) from None
                reason = f"http_{status}"
                hinted = _retry_after(exc)
            except httpx.TransportError as exc:
                status, reason, hinted, provider = None, type(exc).__name__, None, ""
            else:
                return dataclasses.replace(parse_response(resp), retries=retries)

            if len(retries) >= self.max_retries:
                detail = f"; last error: {provider}" if provider else ""
                raise ModelError(
                    f"Cohere API still failing after {len(retries)} retries ({reason}){detail}.",
                    status=status,
                    retries=retries,
                )
            backoff = min(MAX_WAIT_S, BACKOFF_BASE_S * 2 ** len(retries) * (0.5 + self._rng.random() / 2))
            wait = max(hinted or 0.0, backoff)
            if wait > MAX_WAIT_S:
                raise ModelError(
                    f"Cohere API asked to wait {wait:.0f}s ({reason}); giving up.",
                    status=status,
                    retries=retries,
                )
            retries.append(Retry(reason=reason, wait_seconds=round(wait, 2)))
            self._sleep(wait)

    def _pace(self) -> None:
        self._pacer.wait()
