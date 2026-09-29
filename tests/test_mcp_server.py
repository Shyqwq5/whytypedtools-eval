import json

import pytest
from mcp import Client

from tests.conftest import REPO
from whytypedtools_eval.mcp_server import build_server
from whytypedtools_eval.tools import registry
from whytypedtools_eval.tools.base import ToolContext

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def server(fake, client):
    fake.labels["bug"] = {"name": "bug", "color": "d73a4a", "description": ""}
    fake.add_issue("Login flickers on Safari", labels=["bug"])
    contexts = []

    def factory():
        contexts.append(ToolContext(client, REPO, write_mode="dry_run"))
        return contexts[-1]

    srv = build_server(factory)
    srv.contexts = contexts  # type: ignore[attr-defined]
    return srv


async def test_lists_registry_tools_verbatim(server):
    async with Client(server) as mcp:
        tools = {t.name: t for t in (await mcp.list_tools()).tools}
    assert list(tools) == list(registry.TOOLS)
    for spec in registry.list_tools():
        tool = tools[spec["name"]]
        assert tool.description == spec["description"]
        assert tool.input_schema == spec["input_schema"]
    assert tools["add_label"].annotations.read_only_hint is False
    assert tools["list_issues"].annotations.read_only_hint is True


async def test_listing_needs_no_context(server):
    async with Client(server) as mcp:
        await mcp.list_tools()
    assert server.contexts == []


async def test_call_returns_registry_payload(server):
    async with Client(server) as mcp:
        result = await mcp.call_tool("list_issues", {"labels": ["bug"]})
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["ok"] is True
    assert [i["title"] for i in payload["result"]["issues"]] == ["Login flickers on Safari"]
    assert result.structured_content == payload


async def test_errors_are_flagged_and_validated_by_registry(server):
    async with Client(server) as mcp:
        unknown_field = await mcp.call_tool("list_issues", {"colour": "red"})
        bad_scope = await mcp.call_tool("search_issues", {"query": "repo:other/repo x"})
    for result in (unknown_field, bad_scope):
        assert result.is_error is True
        assert json.loads(result.content[0].text)["error"]["type"] == "invalid_input"


async def test_writes_follow_the_context_write_mode(server, fake):
    n = fake.add_issue("Unlabeled")
    async with Client(server) as mcp:
        result = await mcp.call_tool("add_label", {"number": n, "labels": ["bug"]})
        result2 = await mcp.call_tool("get_issue", {"number": n})
    assert json.loads(result.content[0].text)["result"]["added"] == ["bug"]
    assert result2.is_error is False
    assert fake.writes == []
    assert server.contexts[0].write_log[0]["executed"] is False
    assert len(server.contexts) == 1  # one context for the whole session
