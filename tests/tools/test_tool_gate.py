"""The offline tool gate, run for every registered tool (docs/design/tool-gate.md).

A check fails for a tool unless its failing items equal the tool's known exceptions
exactly (tests/tools/gate_exceptions.yaml): a new failure fails the gate, and so does
an exception whose item now passes (remove it). Only the frozen tools may have
exceptions; a new tool must pass every check.
"""

import re

import anyio
import pytest
from jsonschema import Draft202012Validator

from tests.tool_gate import CHECKS, excepted_items, load_exceptions, run_check
from whytypedtools_eval.generic.tool import NAME as GENERIC_TOOL_NAME
from whytypedtools_eval.mcp_server import build_server
from whytypedtools_eval.tools import registry

EXCEPTIONS = load_exceptions()
TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


@pytest.mark.parametrize("check", sorted(CHECKS))
@pytest.mark.parametrize("tool", list(registry.TOOLS))
def test_gate(tool, check):
    failing = run_check(check, tool)
    excepted = excepted_items(EXCEPTIONS, tool, check)
    assert failing - excepted == set(), f"{tool} fails {check}: {sorted(failing - excepted)}"
    assert excepted - failing == set(), (
        f"stale exception for {tool} {check} (now passes; remove it from gate_exceptions.yaml): "
        f"{sorted(excepted - failing)}")


def test_exceptions_only_for_frozen_tools_and_known_checks():
    frozen = set(EXCEPTIONS["frozen_tools"])
    for tool, checks in (EXCEPTIONS.get("exceptions") or {}).items():
        assert tool in frozen, f"{tool} is not frozen: a new tool must pass every check, not be excepted"
        for check, entry in checks.items():
            assert check in CHECKS, f"unknown check {check!r} in the exceptions for {tool}"
            assert entry.get("reason", "").strip() and entry.get("items"), f"{tool} {check}: needs items and a reason"


def test_names_are_unique_and_valid():
    names = [spec.name for spec in registry._SPECS]
    assert len(names) == len(set(names)), f"duplicate tool names: {names}"
    assert list(registry.TOOLS) == names
    assert GENERIC_TOOL_NAME not in names, "a typed tool must not reuse the generic tool's name"
    for name in names:
        assert TOOL_NAME.match(name), f"{name!r} is not a valid tool name"


@pytest.fixture(scope="module")
def mcp_tools():
    from mcp import Client

    async def listing():
        async with Client(build_server(lambda: pytest.fail("listing must not need a context"))) as client:
            return (await client.list_tools()).tools

    return anyio.run(listing)


@pytest.mark.parametrize("tool", list(registry.TOOLS))
def test_mcp_lists_the_tool_with_a_valid_schema(mcp_tools, tool):
    listed = [t for t in mcp_tools if t.name == tool]
    assert len(listed) == 1, f"MCP lists {tool} {len(listed)} times"
    spec = registry.TOOLS[tool]
    assert listed[0].description == spec.description
    assert listed[0].input_schema == spec.input_schema()
    Draft202012Validator.check_schema(listed[0].input_schema)
    assert listed[0].annotations.read_only_hint is spec.read_only
    assert [t.name for t in mcp_tools] == list(registry.TOOLS)
