# Design note: the tool gate, the tool-set check and the scaffold (steps 9 and 8)

Status: in progress (2026-09-30). Order: inventory, offline gate, tool-set check,
how-to and scaffold, then a 5th tool added through the scaffold.

Every tool, new or existing, has to pass one gate before it can be used in an eval.
The four current tools (`list_issues`, `search_issues`, `get_issue`, `add_label`) are
**frozen**: their descriptions and schemas are exactly the ones the reported typed
results were produced with (tuned eval `20260930T002252Z-1d0367af`), so a failed check
on one of them is recorded as a known exception with its reason, never fixed by
changing the tool.

## 1. Inventory: what existed for the four tools before the gate

Legend: **yes** = an automated check existed; **partial** = some tools or some cases;
**no** = nothing automated.

| Check | list_issues | search_issues | get_issue | add_label | Where |
|---|---|---|---|---|---|
| Unknown / missing / wrongly-typed arguments give a structured `invalid_input` naming the field | partial | partial | partial | partial | pydantic `extra="forbid"` models and `registry._invalid_input` apply to all four, but tests cover a few hand-picked cases per tool (`tests/tools/test_<tool>.py`, `tests/test_mcp_server.py`); nothing checks every field of every tool |
| Every input parameter has a description | no | no | no | no | (all four do, unchecked) |
| Replay of a recorded real-API success | yes | yes | yes | no (fake only, by policy: writes are never recorded against the real sandbox) | `tests/tools/test_registry.py::test_replay_reproduces_recorded_output` |
| Replay: auth failure (401) | yes | yes | no | no | recorded fixtures |
| Replay: not found (404) | yes | no | yes | yes (fake) | recorded / fake fixtures |
| Replay: rate limits (403 primary, 429 secondary) | yes (synthetic 403) | yes (synthetic 429) | no | no | `tests/fixtures/synthetic/` |
| Replay: server error (5xx) and permission errors (403) | yes (synthetic) | no | no | no | `tests/fixtures/synthetic/` |
| Replay: pagination (`Link: rel="next"`) | yes | yes | no (comments are paginated, never recorded over 2 pages) | n/a | recorded fixtures |
| Replay: truncation flags set | yes (`truncated`, `body_truncated`) | yes (`truncated`, `body_truncated`) | no (`body_truncated`, `comments_truncated` never true in a fixture) | n/a | recorded fixtures |
| Every returned field is named in the description | no | no | no | no | the `state_reason` lesson from tuning round 1 was fixed by hand for list/search only |
| No input can carry the repository / scope | partial | yes | partial | partial | search: `forbidden_qualifiers` + tests; the others have no such field, unchecked |
| Every request targets the sandbox, scope added by the tool | partial | yes | partial | partial | search: `test_every_recorded_search_is_scoped_to_the_sandbox`; the replay tests pin exact recorded paths for all tools, but nothing states the rule |
| Writes can only reach the sandbox | yes (all writes) | – | – | yes | client guard `assert_write_allowed` on every non-GET (`tests/test_guard.py`); dry-run write log (`tests/tools/test_add_label.py`) |
| Read-only tools never write | no | no | no | – | unchecked |
| MCP lists the tool with the registry's name, description and schema | yes | yes | yes | yes | `tests/test_mcp_server.py::test_lists_registry_tools_verbatim` |
| The advertised schema is valid JSON Schema | no | no | no | no | only shape checks (`type: object`, `additionalProperties: false`) |
| No two tools share a name | no | no | no | no | the registry is a dict built from a tuple: a duplicate would silently replace the earlier tool; the generic tool's name (`github_api`) is not compared |
| Tool folder complete (`tool.py`, `description.md`, `eval_cases.yaml`) | yes | yes | yes | yes | `tests/tools/test_registry.py::test_each_tool_folder_is_complete` |
| Descriptions and schemas frozen to the reported results | no | no | no | no | the hashes are in the traces and the tuning report, but no test compares them |
| Adding a tool leaves `tool_e` unchanged | no | – | – | – | `tool_e` used `registry.list_tools()`, so registering a 5th tool would silently change what `tool_e` means |

Field-name gaps found while taking the inventory (returned fields whose names never
appear in the description):

- `list_issues`: `issues.created_at`, `updated_at`, `body_excerpt`, `body_truncated`;
  `hint.message`, `unknown_labels`, `available_labels` (described in prose: "dates",
  "the first 300 characters of the body", "lists the unknown labels").
- `search_issues`: `effective_query`, `truncated`, `incomplete_results`, the same issue
  fields, `hint.unknown_labels`, `available_labels`. `truncated` here means
  `total_count > returned`, which the description never says.
- `get_issue`: **`state_reason`** (returned since the start, never described: the
  same gap tuning round 1 fixed for list/search), `created_at`, `updated_at`,
  `body_truncated`, `comments.created_at`, `comments.body_truncated`.
- `add_label`: `already_present` (described as "the ones that were already present").

## 2. The offline gate (CI, no network)

`tests/tools/test_tool_gate.py` runs every check below for **every tool in the
registry**, so the four current tools run it too. The checks live in
`tests/tool_gate.py`; each returns the set of failing items. CI runs the gate as its
own step before the full suite (sockets disabled).

| Check | Rule |
|---|---|
| `folder` | `__init__.py`, `tool.py`, `description.md`, `eval_cases.yaml`, `tests/tools/test_<name>.py` and at least one fixture in `tests/fixtures/<name>/` |
| `inputs_described` | every input property has a schema description and is named in `description.md` |
| `invalid_input` | with the arguments of the tool's first ok fixture: an unknown field, each missing required field and each wrongly-typed field give `invalid_input` with exactly `{type, message, retryable: false, retry_after_seconds}`, the message names the field and never echoes the value, and no request is sent |
| `replay_success` | at least one successful fixture recorded from the real API (`source: recorded`); write tools may use `fake`, since writes are never recorded against the real sandbox |
| `replay_error[...]` | seven error templates taken from recorded and synthetic responses (401, 404, 403 forbidden, 403 primary rate limit, 429 secondary rate limit, 500, connection error) are served to every request of the tool's first ok call; the tool must return the matching structured error (`auth_failed`, `not_found`, `forbidden`, `rate_limited` retryable, `upstream_error` retryable) without raising and without blocking more than the 5-second fail-fast budget |
| `replay_pagination` | a tool whose code follows pages (`paginate(` or a `next` link) has a fixture that spans two pages |
| `replay_truncation` | every truncation flag in the output model (`truncated`, `*_truncated`) is `true` in at least one fixture |
| `fields_described` | every field the output model can return (nested models included) is named in `description.md` |
| `no_scope_inputs` | no input can carry a scope (`repo`, `owner`, `org`, `user`, `path`, `url`, ...) |
| `requests_scoped` | every request in every fixture targets `repos/<sandbox>/…` or is a search whose `q` starts with `repo:<sandbox> ` (a request that follows GitHub's own `rel="next"` link continues a scoped one); and, run against a second repository through the ToolContext, every request names that repository, so the scope comes from the context and not from the arguments or a constant |
| `writes_sandboxed` | a read-only tool never writes (live mode, in-memory fake); a write tool sends no write in dry-run, logs writes only under the sandbox, and in live mode a client guarding a different sandbox refuses the write as `forbidden` before anything is sent |
| MCP (`test_mcp_lists_the_tool_with_a_valid_schema`) | the MCP server lists the tool once, with the registry's name, description and schema; the schema passes the JSON Schema 2020-12 meta-schema; `read_only_hint` matches |
| names (`test_names_are_unique_and_valid`) | no two registered tools share a name (checked on the registration tuple, since the dict would hide a duplicate), names match `^[a-z][a-z0-9_]{1,63}$`, and none reuses the generic tool's name `github_api` |

**Known exceptions** (`tests/tools/gate_exceptions.yaml`) are allowed only for the four
frozen tools. A check passes when its failing items equal the listed exceptions
exactly, so a new failure fails the gate and so does a listed item that starts
passing. Current exceptions:

| Tool | Check | Items | Why it stays |
|---|---|---|---|
| list_issues | fields_described | 7 (`created_at`, `updated_at`, `body_excerpt`, `body_truncated`, `hint.*`) | described in prose; frozen |
| search_issues | fields_described | 9, incl. `truncated`, `incomplete_results`, `effective_query` | three fields never mentioned at all; frozen |
| get_issue | fields_described | 6, incl. **`state_reason`** | returned but never described (tuning round 1 fixed this for list/search only); frozen |
| get_issue | replay_truncation | `body_truncated`, `comments.body_truncated` | no seed issue or comment reaches 8,000 / 2,000 characters; adding one would change the seed, all fixtures and the gold answers |
| add_label | fields_described | `already_present` | described in prose; frozen |

**Gaps closed without touching a tool:** two read-only fixtures recorded from the real
sandbox for get_issue (`get_comments_paginated`: comments over two pages;
`get_comments_truncated`: `comments_truncated: true`). The seven error templates, which
list/search had only partly (and get_issue/add_label not at all), now run for every
tool. Observation, not changed (frozen): with `max_comments` reached exactly at a page
boundary, get_issue requests one more page before stopping.

**Frozen tools and tool_e.** `tests/tools/test_frozen_tools.py` compares the four
tools' names, order, description hashes and schema hashes with the `run_start` of the
tuned typed eval `20260930T002252Z-1d0367af` (`tests/tools/frozen_tools.json`).
`tool_e` is now pinned to those four tools (`configs.TOOL_E_TOOLS`): before, it used
the whole registry, so registering a fifth tool would have silently changed what
`tool_e` means. A new tool is evaluated as `tool_e+<name>`.
