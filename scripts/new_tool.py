r"""Scaffold a new typed tool, wired to the tool gate (docs/how-to-add-a-tool.md).

    uv run python scripts/new_tool.py list_comments --overlaps get_issue
    uv run python scripts/new_tool.py close_issue --overlaps add_label --writes
    uv run python scripts/new_tool.py list_comments --overlaps get_issue --dry-run

Creates the tool folder (tool.py, description.md, eval_cases.yaml), its recording
scenarios and test stub, and registers it in tools/registry.py. Every placeholder is
marked TODO, and the gate (tests/tools/test_tool_gate.py) runs on the new tool at
once: it fails, listing what is still missing, until the tool is complete.
Nothing is sent anywhere; --dry-run only prints what would be written.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "src" / "whytypedtools_eval" / "tools"
REGISTRY = TOOLS_DIR / "registry.py"
IMPORT_MARKER = "# scaffold: imports"
SPEC_MARKER = "    # scaffold: specs"
NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
RESERVED = {"github_api", "registry", "base"}


def camel(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def tool_py(name: str, overlaps: str, writes: bool) -> str:
    c = camel(name)
    if writes:
        body = f'''    # Writes go through ctx.write: logged, dry-run aware, and the client refuses any
    # path outside the sandbox. Build the path from ctx.sandbox_repo, never from input.
    # TODO: validate first (e.g. that the issue exists), then write.
    path = f"repos/{{ctx.sandbox_repo}}/issues/{{inp.number}}"
    ctx.write("PATCH", path, {{}})
    raise NotImplementedError("TODO: return a {c}Output")'''
    else:
        body = f'''    # Read with ctx.client.get / ctx.client.paginate on a path built from
    # ctx.sandbox_repo (never from input). Keep the output compact and capped.
    ctx.client.get(f"repos/{{ctx.sandbox_repo}}/issues/{{inp.number}}")
    raise NotImplementedError("TODO: build a {c}Output from the response")'''
    return f'''"""{name}: TODO one line on what it does, and when to use it instead of {overlaps}."""

from __future__ import annotations

from pydantic import Field

from whytypedtools_eval.tools.base import ToolContext, ToolInput, ToolOutput


class {c}Input(ToolInput):
    # Every field needs a description, and no field may choose the repository: the
    # tool adds the sandbox scope itself (the gate checks both).
    number: int = Field(ge=1, description="TODO: the issue number, e.g. 12 for #12.")


class {c}Output(ToolOutput):
    # Every field returned here must be named in description.md (the gate checks it).
    number: int


def {name}(ctx: ToolContext, inp: {c}Input) -> {c}Output:
{body}
'''


def description_md(name: str, overlaps: str, writes: bool) -> str:
    change = " This changes the repository." if writes else ""
    return f"""TODO: one sentence on what {name} does.{change}

Use this when TODO: the requests it is for. For TODO, use {overlaps} instead: TODO say
what {overlaps} does that this tool does not, and the other way round.

Parameters:
- number: TODO.

Returns TODO: name every returned field by its exact name (number, ...), say what
each flag such as `truncated` means when it is true and when it is false, and which
errors the agent can act on.
"""


def eval_cases_yaml(name: str, overlaps: str, writes: bool) -> str:
    safety = ""
    if writes:
        safety = f"""
  - id: {name.replace('_', '-')}-dangerous
    category: dangerous
    prompt: "TODO: a harmful request this tool could carry out (it must refuse or be blocked)."
    expect: {{}}

  - id: {name.replace('_', '-')}-injection
    category: injection
    injection: injection-summary
    prompt: "TODO: a benign request that makes the agent read an injection issue."
    expect: {{}}
"""
    slug = name.replace("_", "-")
    return f"""# Tasks for the tool-set check (scripts/toolset_check.py), in the task schema of
# evals/tasks_v2.yaml ({{issue:<seed key>}} placeholders, answer_issues as seed keys).
# They run with the 40 v2 tasks as tool_e+{name}. The gate requires a functional
# task expecting this tool, a tool_selection pair with `overlaps`, and dangerous and
# injection tasks for a write tool.
version: 2
tool: {name}
overlaps: {overlaps}
tasks:
  - id: {slug}-functional
    category: functional
    min_tool_calls: 1
    prompt: "TODO: a request this tool answers best, e.g. about issue #{{issue:rate-limit-429}}."
    expect:
      tool: {name}
      args: {{}}

  - id: {slug}-pick-{name.split('_')[0]}
    category: tool_selection
    min_tool_calls: 1
    prompt: "TODO: a request that needs {name} and could be mistaken for one for {overlaps}."
    expect:
      tool: {name}

  - id: {slug}-pick-{overlaps.replace('_', '-')}
    category: tool_selection
    min_tool_calls: 1
    prompt: "TODO: a similar request that needs {overlaps}, not {name}."
    expect:
      tool: {overlaps}
{safety}"""


def scenarios_yaml(name: str, writes: bool) -> str:
    note = ("# Write scenarios are recorded from the in-memory fake only (writes: true).\n" if writes else
            "# Record them read-only from the real sandbox: scripts/record_fixtures.py --tool " + name + "\n")
    w = "\n    writes: true" if writes else ""
    return f"""# Recording scenarios for {name} (tests/recording.py loads this file).
{note}# Fields: name (unique), args, page_size, auth (valid|invalid), repo_suffix, expect
# (ok or the expected error type), writes. The gate needs a successful recording, a
# two-page recording if the tool paginates, and each truncation flag set once.
tool: {name}
scenarios:
  - name: {name}_ok
    args: {{number: 1}}  # TODO: arguments for a typical successful call{w}
  - name: {name}_not_found
    args: {{number: 9999}}
    expect: not_found{w}
"""


def test_py(name: str) -> str:
    return f'''"""Tool-specific tests for {name}.

The shared gate (tests/tools/test_tool_gate.py) already runs every check on this
tool: validation, replay of recorded responses and errors, field descriptions,
sandbox scoping, writes, MCP listing. Test what is specific to {name} here.
"""

import pytest

from tests.recording import all_fixtures, replay_call


@pytest.mark.parametrize("fixture", [f for f in all_fixtures() if f["tool"] == "{name}"],
                         ids=lambda f: f["scenario"])
def test_replay_matches_the_recording(mock_api, fixture):
    result, replayer = replay_call(mock_api, fixture)
    assert replayer.done
    assert result == fixture["result"]


def test_behaviour():
    pytest.fail("TODO: assert what {name} must do, e.g. against the in-memory fake (the `fake` fixture)")
'''


def init_py(name: str) -> str:
    c = camel(name)
    return (f"from whytypedtools_eval.tools.{name}.tool import {c}Input, {c}Output, {name}\n\n"
            f'__all__ = ["{c}Input", "{c}Output", "{name}"]\n')


def registry_text(text: str, name: str, writes: bool) -> str:
    c = camel(name)
    if IMPORT_MARKER not in text or SPEC_MARKER not in text:
        raise SystemExit("error: registry.py has no scaffold markers")
    imp = f"from whytypedtools_eval.tools.{name} import {c}Input, {c}Output, {name}\n"
    spec = f'    ToolSpec("{name}", {c}Input, {c}Output, {name}{", read_only=False" if writes else ""}),\n'
    return text.replace(IMPORT_MARKER, imp + IMPORT_MARKER, 1).replace(SPEC_MARKER, spec + SPEC_MARKER, 1)


def plan(name: str, overlaps: str, writes: bool, root: Path = ROOT) -> dict[Path, str]:
    tools = root / "src" / "whytypedtools_eval" / "tools"
    files = {
        tools / name / "__init__.py": init_py(name),
        tools / name / "tool.py": tool_py(name, overlaps, writes),
        tools / name / "description.md": description_md(name, overlaps, writes),
        tools / name / "eval_cases.yaml": eval_cases_yaml(name, overlaps, writes),
        root / "tests" / "fixtures" / name / "scenarios.yaml": scenarios_yaml(name, writes),
        root / "tests" / "tools" / f"test_{name}.py": test_py(name),
    }
    registry = tools / "registry.py"
    files[registry] = registry_text(registry.read_text(encoding="utf-8"), name, writes)
    return files


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("name", help="snake_case tool name, e.g. list_comments")
    p.add_argument("--overlaps", required=True, help="the existing tool it is most easily confused with")
    p.add_argument("--writes", action="store_true", help="the tool changes the repository")
    p.add_argument("--dry-run", action="store_true", help="print what would be written; write nothing")
    args = p.parse_args(argv)
    tools = root / "src" / "whytypedtools_eval" / "tools"
    existing = {d.name for d in tools.iterdir() if (d / "tool.py").is_file()}
    if not NAME.match(args.name) or args.name in RESERVED:
        print(f"error: {args.name!r} is not a valid new tool name (snake_case, not {sorted(RESERVED)})", file=sys.stderr)
        return 2
    if args.name in existing:
        print(f"error: tool {args.name!r} already exists", file=sys.stderr)
        return 2
    if args.overlaps not in existing:
        print(f"error: --overlaps must be an existing tool: {', '.join(sorted(existing))}", file=sys.stderr)
        return 2
    files = plan(args.name, args.overlaps, args.writes, root)
    for path, text in files.items():
        rel = path.relative_to(root).as_posix()
        if path.exists() and path.name != "registry.py":
            print(f"error: {rel} already exists; nothing written", file=sys.stderr)
            return 2
        print(f"{'would write' if args.dry_run else 'write'} {rel}")
    if args.dry_run:
        return 0
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    print(f"\nNext: docs/how-to-add-a-tool.md. The gate now runs on {args.name}:\n"
          f"  uv run pytest tests/tools/test_tool_gate.py -k {args.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
