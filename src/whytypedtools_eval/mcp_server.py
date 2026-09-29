"""MCP server: a thin wrapper over the tool registry.

Every registry tool is advertised with exactly its registry name, description
(description.md) and input schema, and every call goes through
`registry.call_tool`, so MCP clients and the eval harness see identical tools.
Arguments are passed through unvalidated by the MCP layer; the registry's strict
validation (unknown fields rejected) applies as for direct calls.

Tool results are the registry's JSON payload as text (and as structured content);
`{"ok": false, ...}` results are flagged with `is_error`.

Writes are dry-run unless the server is started with `allow_writes=True`.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.tools.base import Tool
from mcp.server.mcpserver.utilities.func_metadata import func_metadata
from mcp_types import CallToolResult, TextContent, ToolAnnotations

from whytypedtools_eval.tools import registry
from whytypedtools_eval.tools.base import ToolContext

SERVER_NAME = "whytypedtools-github"
ContextFactory = Callable[[], ToolContext]


def _no_arguments() -> None:  # pragma: no cover - only its (empty) signature is used
    """Placeholder: MCP metadata needs a function; arguments are handled by the registry."""


class RegistryTool(Tool):
    """A tool whose `fn` takes the raw arguments dict and returns a CallToolResult."""

    async def run(self, arguments: dict[str, Any], context: Any, convert_result: bool = False) -> Any:
        return await anyio.to_thread.run_sync(self.fn, arguments)


def to_call_result(result: dict[str, Any]) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
        structured_content=result,
        is_error=not result.get("ok", False),
    )


def build_server(context_factory: ContextFactory) -> MCPServer:
    """Create the server. The ToolContext is created on the first tool call, so
    listing tools works without credentials."""
    lock = threading.Lock()
    holder: list[ToolContext] = []

    def ctx() -> ToolContext:
        with lock:
            if not holder:
                holder.append(context_factory())
            return holder[0]

    def make_call(name: str) -> Callable[[dict[str, Any]], CallToolResult]:
        def call(arguments: dict[str, Any]) -> CallToolResult:
            return to_call_result(registry.call_tool(ctx(), name, arguments))

        return call

    metadata = func_metadata(_no_arguments, structured_output=False)
    tools = []
    for spec in registry.TOOLS.values():
        tools.append(
            RegistryTool(
                fn=make_call(spec.name),
                name=spec.name,
                description=spec.description,
                parameters=spec.input_schema(),
                fn_metadata=metadata,
                is_async=False,
                annotations=ToolAnnotations(
                    read_only_hint=spec.read_only,
                    destructive_hint=False,
                    idempotent_hint=True,
                    open_world_hint=True,
                ),
            )
        )
    return MCPServer(SERVER_NAME, instructions="GitHub issue tools scoped to one sandbox repository.", tools=tools)
