"""Registry of all tools: name, description (from description.md) and input schema.

`list_tools()` and `call_tool()` are the protocol-independent surface that an MCP
server or any other agent harness wraps.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from whytypedtools_eval.tools.add_label import AddLabelInput, AddLabelOutput, add_label
from whytypedtools_eval.tools.base import ToolContext, ToolError, to_tool_error
from whytypedtools_eval.tools.get_issue import GetIssueInput, GetIssueOutput, get_issue
from whytypedtools_eval.tools.list_issues import ListIssuesInput, ListIssuesOutput, list_issues
from whytypedtools_eval.tools.search_issues import SearchIssuesInput, SearchIssuesOutput, search_issues

from whytypedtools_eval.tools.list_comments import ListCommentsInput, ListCommentsOutput, list_comments
# scaffold: imports (scripts/new_tool.py adds new tools' imports above this line)

TOOLS_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class ToolSpec:
    name: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    func: Callable[[ToolContext, Any], BaseModel]
    # False for tools that change the repository (advertised to MCP clients).
    read_only: bool = True

    @property
    def folder(self) -> Path:
        return TOOLS_DIR / self.name

    @property
    def description(self) -> str:
        return (self.folder / "description.md").read_text(encoding="utf-8").strip()

    def input_schema(self) -> dict[str, Any]:
        return _strip_titles(self.input_model.model_json_schema())


def _strip_titles(node: Any) -> Any:
    """Drop pydantic's auto-generated `title` keys; they only add noise for the model."""
    if isinstance(node, dict):
        return {k: _strip_titles(v) for k, v in node.items() if k != "title" or not isinstance(v, str)}
    if isinstance(node, list):
        return [_strip_titles(v) for v in node]
    return node


# Registration order is the order the model sees the tools in.
_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("list_issues", ListIssuesInput, ListIssuesOutput, list_issues),
    ToolSpec("search_issues", SearchIssuesInput, SearchIssuesOutput, search_issues),
    ToolSpec("get_issue", GetIssueInput, GetIssueOutput, get_issue),
    ToolSpec("add_label", AddLabelInput, AddLabelOutput, add_label, read_only=False),
    ToolSpec("list_comments", ListCommentsInput, ListCommentsOutput, list_comments),
    # scaffold: specs (scripts/new_tool.py adds new tools above this line)
)
# A duplicate name would silently replace the earlier tool here; the tool gate
# (tests/tools/test_tool_gate.py) compares this dict with _SPECS.
TOOLS: dict[str, ToolSpec] = {spec.name: spec for spec in _SPECS}


def list_tools(names: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """All registered tools, or only `names` (in registration order)."""
    return [
        {"name": spec.name, "description": spec.description, "input_schema": spec.input_schema()}
        for spec in TOOLS.values()
        if names is None or spec.name in names
    ]


def call_tool(ctx: ToolContext, name: str, args: dict[str, Any] | None,
              *, allowed: Sequence[str] | None = None) -> dict[str, Any]:
    """Validate, run and serialise one tool call. Never raises for tool-level failures.

    With `allowed`, only those tools exist for this caller (a tool set under test)."""
    available = [n for n in TOOLS if allowed is None or n in allowed]
    spec = TOOLS.get(name) if name in available else None
    if spec is None:
        err = ToolError("unknown_tool", f"Unknown tool {name!r}. Available: {', '.join(available)}.")
        return {"ok": False, "error": err.to_dict()}
    try:
        inp = spec.input_model.model_validate(args or {})
    except ValidationError as exc:
        return {"ok": False, "error": _invalid_input(exc).to_dict()}
    try:
        result = spec.func(ctx, inp)
    except Exception as exc:  # noqa: BLE001 - mapped below; unknown errors re-raise
        return {"ok": False, "error": to_tool_error(exc).to_dict()}
    return {"ok": True, "result": result.model_dump(exclude_none=True)}


def _invalid_input(exc: ValidationError) -> ToolError:
    # Report field and reason only; never echo the raw input back.
    problems = []
    for err in exc.errors(include_input=False, include_url=False):
        field = ".".join(str(p) for p in err["loc"]) or "(input)"
        problems.append(f"{field}: {err['msg']}")
    return ToolError("invalid_input", "Invalid arguments: " + "; ".join(problems))
