import random

import cohere
import httpx
import pytest
from cohere.core.api_error import ApiError

from whytypedtools_eval.agent.cohere_model import (
    MAX_RETRIES,
    CohereModel,
    to_cohere_messages,
    to_cohere_tools,
)
from whytypedtools_eval.agent.model import ModelError, ToolCall
from whytypedtools_eval.config import ConfigError, Settings
from whytypedtools_eval.tools import registry


def text_response(text="Answer.", finish="COMPLETE"):
    return cohere.V2ChatResponse(
        id="r1",
        finish_reason=finish,
        message=cohere.AssistantMessageResponse(
            content=[cohere.TextAssistantMessageResponseContentItem(text=text)]
        ),
        usage=cohere.Usage(
            tokens=cohere.UsageTokens(input_tokens=120.0, output_tokens=8.0),
            billed_units=cohere.UsageBilledUnits(input_tokens=100.0, output_tokens=8.0),
        ),
    )


def tool_response():
    return cohere.V2ChatResponse(
        id="r2",
        finish_reason="TOOL_CALL",
        message=cohere.AssistantMessageResponse(
            tool_plan="I will list open issues.",
            tool_calls=[
                cohere.ToolCallV2(
                    id="tc1",
                    function=cohere.ToolCallV2Function(name="list_issues", arguments='{"state": "open"}'),
                )
            ],
        ),
    )


class FakeClient:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def chat(self, **kwargs):
        self.requests.append(kwargs)
        item = self.outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make(client, **kwargs):
    sleeps = []
    kwargs.setdefault("rng", random.Random(0))
    model = CohereModel(client, "command-test", sleep=sleeps.append, **kwargs)
    return model, sleeps


MESSAGES = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]


def test_tools_come_from_registry():
    specs = registry.list_tools()
    converted = to_cohere_tools(specs)
    assert [t["function"]["name"] for t in converted] == list(registry.TOOLS)
    for tool, name in zip(converted, registry.TOOLS):
        assert tool["type"] == "function"
        assert tool["function"]["description"] == registry.TOOLS[name].description
        assert tool["function"]["parameters"] == registry.TOOLS[name].input_schema()


def test_message_conversion():
    call = ToolCall("tc1", "list_issues", '{"state": "open"}')
    converted = to_cohere_messages([
        *MESSAGES,
        {"role": "assistant", "content": None, "tool_plan": "plan", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "tc1", "content": '{"ok": true}'},
        {"role": "assistant", "content": "final", "tool_plan": None, "tool_calls": []},
    ])
    assert converted[:2] == MESSAGES
    assert converted[2] == {
        "role": "assistant", "tool_plan": "plan",
        "tool_calls": [{"id": "tc1", "type": "function",
                        "function": {"name": "list_issues", "arguments": '{"state": "open"}'}}],
    }
    assert converted[3] == {
        "role": "tool", "tool_call_id": "tc1",
        "content": [{"type": "document", "document": {"data": '{"ok": true}'}}],
    }
    assert converted[4] == {"role": "assistant", "content": "final"}


def test_request_settings():
    client = FakeClient(text_response())
    model, _ = make(client)
    model.step(MESSAGES, registry.list_tools())
    req = client.requests[0]
    assert req["model"] == "command-test"
    assert req["temperature"] == 0.0
    assert req["seed"] == 0
    assert req["request_options"] == {"max_retries": 0}
    assert req["thinking"] == {"type": "enabled"}
    assert "tool_choice" not in req
    assert len(req["tools"]) == len(registry.TOOLS)


def test_tools_disabled_and_no_seed():
    client = FakeClient(text_response())
    model, _ = make(client, seed=None)
    model.step(MESSAGES, registry.list_tools(), allow_tools=False)
    req = client.requests[0]
    # Command A+ rejects tool_choice (HTTP 400), so disabling tools sends nothing extra.
    assert "tool_choice" not in req
    assert len(req["tools"]) == len(registry.TOOLS)  # still declared for earlier tool messages
    assert "seed" not in req
    assert model.describe()["seed"] is None


def test_thinking_can_be_left_to_api_default():
    client = FakeClient(text_response())
    model, _ = make(client, thinking=None)
    model.step(MESSAGES, [])
    assert "thinking" not in client.requests[0]
    assert model.describe()["thinking"] is None


def test_thinking_is_parsed_and_sent_back():
    resp = cohere.V2ChatResponse(
        id="r3",
        finish_reason="TOOL_CALL",
        message=cohere.AssistantMessageResponse(
            content=[cohere.ThinkingAssistantMessageResponseContentItem(thinking="The user wants bugs.")],
            tool_calls=[cohere.ToolCallV2(id="tc1", function=cohere.ToolCallV2Function(
                name="list_issues", arguments="{}"))],
        ),
    )
    model, _ = make(FakeClient(resp))
    turn = model.step(MESSAGES, [])
    assert turn.thinking == "The user wants bugs."
    assert turn.text is None
    (assistant,) = to_cohere_messages([{"role": "assistant", "content": None, "tool_plan": None,
                                        "thinking": turn.thinking, "tool_calls": turn.tool_calls}])
    assert assistant["content"] == [{"type": "thinking", "thinking": "The user wants bugs."}]
    assert "tool_plan" not in assistant


def test_parse_text_response():
    model, _ = make(FakeClient(text_response("All good.")))
    turn = model.step(MESSAGES, [])
    assert (turn.text, turn.tool_calls, turn.finish_reason) == ("All good.", [], "complete")
    assert turn.usage.to_dict() == {"input_tokens": 120, "output_tokens": 8,
                                    "billed_input_tokens": 100, "billed_output_tokens": 8}
    assert turn.retries == []


def test_parse_tool_response():
    model, _ = make(FakeClient(tool_response()))
    turn = model.step(MESSAGES, [])
    assert turn.finish_reason == "tool_call"
    assert turn.tool_plan == "I will list open issues."
    assert turn.tool_calls == [ToolCall("tc1", "list_issues", '{"state": "open"}')]
    assert turn.usage.input_tokens is None


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retries_transient_errors(status):
    client = FakeClient(ApiError(status_code=status, body={}), text_response())
    model, sleeps = make(client)
    turn = model.step(MESSAGES, [])
    assert turn.text == "Answer."
    assert len(sleeps) == 1
    assert [r.reason for r in turn.retries] == [f"http_{status}"]


def test_honours_retry_after():
    client = FakeClient(ApiError(status_code=429, headers={"Retry-After": "30"}, body={}), text_response())
    model, sleeps = make(client)
    model.step(MESSAGES, [])
    assert sleeps == [30.0]


def test_retry_after_too_long_gives_up():
    client = FakeClient(ApiError(status_code=429, headers={"retry-after": "3600"}, body={}))
    model, sleeps = make(client)
    with pytest.raises(ModelError, match="wait 3600s") as exc:
        model.step(MESSAGES, [])
    assert sleeps == []
    assert exc.value.status == 429


def test_backoff_grows_and_gives_up():
    client = FakeClient(*[ApiError(status_code=503, body={}) for _ in range(MAX_RETRIES + 1)])
    model, sleeps = make(client)
    with pytest.raises(ModelError, match=f"after {MAX_RETRIES} retries") as exc:
        model.step(MESSAGES, [])
    assert len(sleeps) == MAX_RETRIES
    assert sleeps == sorted(sleeps)
    assert len(exc.value.retries) == MAX_RETRIES
    # Backoff is capped instead of failing, and together outlasts a one-minute window.
    assert max(sleeps) <= 60 and sum(sleeps) > 60


def test_pacing_spaces_out_requests():
    now = [0.0]
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        now[0] += s

    client = FakeClient(text_response(), text_response(), text_response())
    model = CohereModel(client, "m", min_interval_s=3.0, sleep=sleep, clock=lambda: now[0])
    model.step(MESSAGES, [])
    now[0] += 1.0
    model.step(MESSAGES, [])
    now[0] += 5.0
    model.step(MESSAGES, [])
    assert sleeps == [2.0]  # only the second call was too early


def test_transport_errors_are_retried():
    client = FakeClient(httpx.ConnectTimeout("slow"), text_response())
    model, sleeps = make(client)
    turn = model.step(MESSAGES, [])
    assert [r.reason for r in turn.retries] == ["ConnectTimeout"]


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_client_errors_fail_fast_without_body(status):
    body = {"message": "invalid api token sk-leaky-body"}
    client = FakeClient(ApiError(status_code=status, headers={"x-debug": "leak"}, body=body))
    model, sleeps = make(client)
    with pytest.raises(ModelError) as exc:
        model.step(MESSAGES, [])
    assert sleeps == []
    assert exc.value.status == status
    assert "leak" not in str(exc.value)


def test_from_settings_requires_key():
    settings = Settings(_env_file=None, github_token="github_pat_test", sandbox_repo="me/sandbox")  # type: ignore[call-arg]
    with pytest.raises(ConfigError, match="COHERE_API_KEY"):
        CohereModel.from_settings(settings)


def test_describe():
    model, _ = make(FakeClient(), temperature=0.0, seed=7)
    info = model.describe()
    assert info | {"sdk_version": None} == {
        "provider": "cohere", "model": "command-test", "temperature": 0.0, "seed": 7,
        "thinking": "enabled", "sdk_version": None,
    }
    assert info["sdk_version"] == cohere.__version__


def test_monthly_quota_429_fails_fast():
    body = {"message": "You are using a Trial key, which is limited to 1000 API calls / month. You can ..."}
    client = FakeClient(ApiError(status_code=429, body=body), text_response())
    model, sleeps = make(client)
    with pytest.raises(ModelError) as exc:
        model.step(MESSAGES, [])
    assert exc.value.fatal is True and exc.value.status == 429
    assert "monthly API call quota" in exc.value.message and "Trial key, which" not in exc.value.message
    assert sleeps == [] and len(client.requests) == 1


def test_per_minute_429_is_still_retried():
    client = FakeClient(ApiError(status_code=429, body={"message": "too many requests"}), text_response())
    model, sleeps = make(client)
    assert model.step(MESSAGES, []).text == "Answer."
    assert len(sleeps) == 1
