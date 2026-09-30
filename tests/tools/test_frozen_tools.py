"""The four tools behind the reported typed results are frozen (docs/design/tool-gate.md).

Their names, order, descriptions and input schemas must equal what the latest typed
eval recorded in its traces, and tool_e must stay exactly that set however many tools
are registered.
"""

import json
from pathlib import Path

from whytypedtools_eval.agent.trace import sha256_json, sha256_text
from whytypedtools_eval.evals.configs import TOOL_E_TOOLS, is_typed, resolve, toolset_config
from whytypedtools_eval.tools import registry

FROZEN = json.loads((Path(__file__).with_name("frozen_tools.json")).read_text(encoding="utf-8"))["tools"]


def fingerprint(tools):
    return [{"name": t["name"], "description_sha256": sha256_text(t["description"]),
             "schema_sha256": sha256_json(t["input_schema"])} for t in tools]


def test_frozen_tools_are_unchanged():
    assert fingerprint(registry.list_tools(TOOL_E_TOOLS)) == FROZEN


def test_tool_e_is_pinned_to_the_frozen_tools(client):
    setup = resolve("tool_e")(_setup_context(client))
    assert fingerprint(setup.tools) == FROZEN
    extra = [n for n in registry.TOOLS if n not in TOOL_E_TOOLS]
    if extra:  # a registered newer tool is not reachable through tool_e
        result = setup.call_tool(extra[0], {})
        assert result["error"]["type"] == "unknown_tool"


def test_toolset_configs_add_only_registered_new_tools(client):
    assert resolve("tool_e+nope") is None and resolve("tool_e+list_issues") is None
    assert resolve(toolset_config(["x", "x"])) is None and not is_typed("tool_e+nope")
    extra = [n for n in registry.TOOLS if n not in TOOL_E_TOOLS]
    if extra:
        setup = resolve(toolset_config(extra))(_setup_context(client))
        assert [t["name"] for t in setup.tools] == list(TOOL_E_TOOLS) + extra
        assert setup.typed and is_typed(setup.config)


def _setup_context(client):
    from tests.conftest import REPO
    from whytypedtools_eval.evals.configs import SetupContext

    return SetupContext(client, REPO, "dry_run", "task")
