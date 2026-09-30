import runpy
import shutil
from pathlib import Path

import pytest
import yaml

from whytypedtools_eval.evals.tasks import Task

ROOT = Path(__file__).resolve().parents[1]
SCAFFOLD = runpy.run_path(str(ROOT / "scripts" / "new_tool.py"))
REGISTRY = ROOT / "src" / "whytypedtools_eval" / "tools" / "registry.py"


@pytest.fixture
def tree(tmp_path):
    """A copy of the parts of the project the scaffold reads and writes."""
    tools = tmp_path / "src" / "whytypedtools_eval" / "tools"
    tools.mkdir(parents=True)
    shutil.copy(REGISTRY, tools / "registry.py")
    for name in ("list_issues", "search_issues", "get_issue", "add_label"):
        (tools / name).mkdir()
        (tools / name / "tool.py").write_text("", encoding="utf-8")
    (tmp_path / "tests" / "tools").mkdir(parents=True)
    return tmp_path


def created(tree, name):
    tools = tree / "src" / "whytypedtools_eval" / "tools"
    return {
        "tool": tools / name / "tool.py",
        "init": tools / name / "__init__.py",
        "description": tools / name / "description.md",
        "eval_cases": tools / name / "eval_cases.yaml",
        "scenarios": tree / "tests" / "fixtures" / name / "scenarios.yaml",
        "test": tree / "tests" / "tools" / f"test_{name}.py",
    }


@pytest.mark.parametrize("writes", [False, True])
def test_scaffold_creates_a_complete_tool_wired_to_the_gate(tree, writes, capsys):
    args = ["probe_tool", "--overlaps", "get_issue"] + (["--writes"] if writes else [])
    assert SCAFFOLD["main"](args, root=tree) == 0
    files = created(tree, "probe_tool")
    for path in files.values():
        assert path.is_file(), path
    for key in ("tool", "init", "test"):
        compile(files[key].read_text(encoding="utf-8"), str(files[key]), "exec")
    registry = (tree / "src" / "whytypedtools_eval" / "tools" / "registry.py").read_text(encoding="utf-8")
    compile(registry, "registry.py", "exec")
    assert registry.index("from whytypedtools_eval.tools.probe_tool import") < registry.index("# scaffold: imports")
    spec_line = 'ToolSpec("probe_tool", ProbeToolInput, ProbeToolOutput, probe_tool' + (", read_only=False" if writes else "")
    assert spec_line in registry and registry.index(spec_line) < registry.index("# scaffold: specs")
    cases = yaml.safe_load(files["eval_cases"].read_text(encoding="utf-8"))
    assert (cases["version"], cases["tool"], cases["overlaps"]) == (2, "probe_tool", "get_issue")
    tasks = [Task.model_validate(t) for t in cases["tasks"]]
    categories = sorted(t.category for t in tasks)
    expected = ["functional", "tool_selection", "tool_selection"] + (["dangerous", "injection"] if writes else [])
    assert categories == sorted(expected)
    assert {t.expect.tool for t in tasks if t.category == "tool_selection"} == {"probe_tool", "get_issue"}
    scenarios = yaml.safe_load(files["scenarios"].read_text(encoding="utf-8"))
    assert scenarios["tool"] == "probe_tool" and all(s.get("writes", False) is writes for s in scenarios["scenarios"])
    # Every placeholder is marked, so the gate's no_todo check fails until it is filled in.
    for key in ("tool", "description", "eval_cases", "scenarios", "test"):
        assert "TODO" in files[key].read_text(encoding="utf-8"), key
    assert "test_tool_gate.py -k probe_tool" in capsys.readouterr().out


def test_dry_run_writes_nothing(tree, capsys):
    before = (tree / "src" / "whytypedtools_eval" / "tools" / "registry.py").read_text(encoding="utf-8")
    assert SCAFFOLD["main"](["probe_tool", "--overlaps", "get_issue", "--dry-run"], root=tree) == 0
    assert "would write src/whytypedtools_eval/tools/probe_tool/tool.py" in capsys.readouterr().out
    assert not any(p.exists() for p in created(tree, "probe_tool").values())
    assert (tree / "src" / "whytypedtools_eval" / "tools" / "registry.py").read_text(encoding="utf-8") == before


@pytest.mark.parametrize("args,message", [
    (["Bad-Name", "--overlaps", "get_issue"], "not a valid new tool name"),
    (["github_api", "--overlaps", "get_issue"], "not a valid new tool name"),
    (["get_issue", "--overlaps", "list_issues"], "already exists"),
    (["probe_tool", "--overlaps", "no_such_tool"], "must be an existing tool"),
])
def test_scaffold_refuses_bad_input(tree, args, message, capsys):
    assert SCAFFOLD["main"](args, root=tree) == 2
    assert message in capsys.readouterr().err


def test_scaffold_refuses_to_overwrite(tree, capsys):
    target = created(tree, "probe_tool")["test"]
    target.write_text("keep me", encoding="utf-8")
    assert SCAFFOLD["main"](["probe_tool", "--overlaps", "get_issue"], root=tree) == 2
    assert target.read_text(encoding="utf-8") == "keep me" and "already exists" in capsys.readouterr().err
    assert not created(tree, "probe_tool")["tool"].exists()


def test_registry_without_markers_is_refused(tree):
    registry = tree / "src" / "whytypedtools_eval" / "tools" / "registry.py"
    registry.write_text(registry.read_text(encoding="utf-8").replace("# scaffold: specs", ""), encoding="utf-8")
    with pytest.raises(SystemExit, match="no scaffold markers"):
        SCAFFOLD["main"](["probe_tool", "--overlaps", "get_issue"], root=tree)
