import pytest
import yaml

from tests.recording import all_fixtures, replay_call
from whytypedtools_eval.tools.registry import TOOLS, list_tools


def test_registered_tools():
    # The frozen tools come first, in their original order (tests/tools/test_frozen_tools.py).
    assert list(TOOLS)[:4] == ["list_issues", "search_issues", "get_issue", "add_label"]


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_each_tool_folder_is_complete(name):
    folder = TOOLS[name].folder
    for filename in ("tool.py", "description.md", "eval_cases.yaml"):
        assert (folder / filename).is_file(), f"{name} is missing {filename}"
    assert len(TOOLS[name].description) > 100
    cases = yaml.safe_load((folder / "eval_cases.yaml").read_text(encoding="utf-8"))
    assert cases["tool"] == name
    # Version 1: the frozen tools' placeholders (their tasks are in evals/tasks_v2.yaml).
    # Version 2: a new tool's own tasks for the tool-set check (tests/tool_gate.py).
    assert (cases["version"], type(cases.get("cases"))) == (1, list) or cases["version"] == 2


def test_list_tools_shape():
    tools = {t["name"]: t for t in list_tools()}
    assert set(tools) == set(TOOLS)
    for tool in tools.values():
        schema = tool["input_schema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert "title" not in schema
        assert all("title" not in prop for prop in schema["properties"].values())


def test_defaults_are_visible_and_intentionally_different():
    props = {t["name"]: t["input_schema"]["properties"] for t in list_tools()}
    assert props["list_issues"]["state"]["default"] == "open"
    assert props["search_issues"]["state"]["default"] == "all"
    assert props["list_issues"]["max_results"]["default"] == 20
    assert props["search_issues"]["max_results"]["default"] == 10
    assert list_tools()[1]["input_schema"]["required"] == ["query"]


def test_descriptions_agree_with_schema_defaults():
    descriptions = {t["name"]: t["description"] for t in list_tools()}
    assert '"open" (default)' in descriptions["list_issues"]
    assert '"all" (default)' in descriptions["search_issues"]


@pytest.mark.parametrize(
    "fixture",
    [f for f in all_fixtures() if "result" in f],
    ids=lambda f: f"{f['tool']}/{f['scenario']}",
)
def test_replay_reproduces_recorded_output(mock_api, fixture):
    # Regression guard: tool output for each recorded API exchange is stable.
    result, replayer = replay_call(mock_api, fixture)
    assert replayer.done
    assert result == fixture["result"]
