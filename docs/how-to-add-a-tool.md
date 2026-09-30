# How to add a tool

A new typed tool is done when it passes the **offline gate** in CI and its
**tool-set check** shows what it changes for the agent. Background and rules:
[design note](design/tool-gate.md). The four current tools are frozen; never edit
them to make room for a new one.

## 1. Decide what it is for, and what it overlaps

Write down which requests the tool is for and which existing tool it is most easily
confused with (`--overlaps`). Every tool overlaps with something; the tool-set check
measures whether the agent now picks the new tool where it should not, or stops
picking the old one where it should.

## 2. Scaffold it

```
uv run python scripts/new_tool.py <name> --overlaps <existing tool> [--writes] [--dry-run]
```

This creates, all marked with `TODO`:

| File | What to do |
|---|---|
| `src/whytypedtools_eval/tools/<name>/tool.py` | input and output models, the function |
| `src/whytypedtools_eval/tools/<name>/description.md` | what the model sees |
| `src/whytypedtools_eval/tools/<name>/eval_cases.yaml` | its tasks for the tool-set check |
| `tests/fixtures/<name>/scenarios.yaml` | the calls to record as fixtures |
| `tests/tools/test_<name>.py` | tool-specific tests (replay test included) |
| `tools/registry.py` | the tool is registered (not in `tool_e`, which stays pinned) |

The gate runs on the tool at once and lists what is missing:

```
uv run pytest tests/tools/test_tool_gate.py -k <name>
```

## 3. Write the description first

`description.md` is the interface the model sees. The gate requires every input and
every returned field to be **named by its exact name** (the `state_reason` lesson: a
field the description never mentions is a field the agent does not use). Say:

- when to use the tool, and when to use the overlapped tool instead;
- each parameter with its default and limits;
- each returned field; for every flag (`truncated`, `*_truncated`) what `true` and
  `false` mean (tuning round 1: the agent re-listed because it did not trust
  `truncated: false`);
- the errors the agent can act on.

## 4. Implement it

- Inputs are a pydantic `ToolInput` (unknown fields rejected). Every field has a
  `description`; no field may choose the repository (`repo`, `owner`, `path`, `url`,
  ...). Build every path from `ctx.sandbox_repo`; for search, prefix the query with
  `repo:<sandbox> is:issue` in the tool.
- Read with `ctx.client.get` / `ctx.client.paginate`. Cap results (`max_results`) and
  text, and say so with a truncation flag.
- Write only through `ctx.write(...)`: it logs the write, honours dry-run, and the
  client refuses any path outside the sandbox. Mark the spec `read_only=False`.
- Raise `ToolError` for failures the agent can act on; transport and GitHub errors
  are mapped for you (`to_tool_error`).
- If the tool uses an endpoint the in-memory fake (`tests/fake_github.py`) does not
  serve, add it there: the gate runs the tool against the fake for a second repository.

## 5. Record fixtures

List the calls in `tests/fixtures/<name>/scenarios.yaml`: a typical success, the
errors it can meet (e.g. `not_found`), two pages if it paginates (`page_size: 1`),
and a call that sets each truncation flag. Then:

```
uv run python scripts/record_fixtures.py --tool <name>          # read-only, real sandbox
uv run python scripts/record_fixtures.py --tool <name> --fake   # write tools: fake only
```

Real recording is read-only and refuses to run unless the sandbox is in its seed
state; every fixture is sanitised and scanned for secrets before it is written.
Rate limits, 5xx and connection errors are not recorded: the gate serves seven error
templates to every tool. A case the sandbox cannot produce may be a hand-written
fixture with `"source": "synthetic"` and a `note`.

## 6. Write its tasks

`eval_cases.yaml` uses the task schema of `evals/tasks_v2.yaml` (`{issue:<seed key>}`
placeholders, gold answers as seed keys, `min_tool_calls` for benign tasks). The
gate requires a functional task expecting the tool, a `tool_selection` pair (one
task expecting it, one expecting the overlapped tool), and a dangerous and an
injection task for a write tool.

## 7. Pass the gate, then the whole suite

```
uv run pytest tests/tools/test_tool_gate.py -k <name>
uv run pytest
```

The gate has no exceptions for new tools (`tests/tools/gate_exceptions.yaml` is for
the frozen four only), and the whole suite, including the scoring baseline and the
frozen-tools test, must pass unchanged.

## 8. Run the tool-set check (costs model calls)

```
uv run python scripts/toolset_check.py                 # plan and estimate, no API call
uv run python scripts/toolset_check.py --run           # ~350 Command A+ calls + the new tasks
```

The run needs the owner's quota and go-ahead. It writes
`results/<eval-id>/toolset_report.md`: per task, the first tool, pass rate (also
without the tool/argument checks), unsafe runs, calls and input tokens against the
current typed results, with every change flagged. A drop in pass rate with the
answers still right means the new tool is used where a v2 task expects an old one:
decide whether that is a legitimate alternative (the task set needs a new version)
or a confusion (fix the new tool's description), then rerun.
