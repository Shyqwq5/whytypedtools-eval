# whytypedtools-eval

GitHub integration tools exposed as an MCP server, a minimal test agent, and an eval
suite that measures tool-use success and safety — comparing typed tools against
generic bash tools with rule-based and LLM guardrails.

> Work in progress. Currently implemented: sandbox repo management and the first two
> typed tools (`list_issues`, `search_issues`).

## Sandbox setup

All tools and agents act on a dedicated **sandbox repo**, never on a real one. Its
contents are defined in [`sandbox/seed_data.yaml`](sandbox/seed_data.yaml) and managed
only through the GitHub API by the scripts below. You never need to clone it.

### 1. Create the sandbox repo

Create a new, empty repository on GitHub, e.g. `your-user/whytypedtools-sandbox`
(private is fine). Make sure **Issues** are enabled in its settings.

### 2. Create a fine-grained token

GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens**
→ Generate new token:

- **Repository access:** *Only select repositories* → pick only your sandbox repo.
- **Permissions → Repository:** `Issues: Read and write` (`Metadata: Read-only` is added automatically).
- Nothing else. The token then cannot touch any other repo, even if something goes wrong.

### 3. Configure

```bash
cp .env.example .env
# edit .env:
#   GITHUB_TOKEN=github_pat_...
#   SANDBOX_REPO=your-user/whytypedtools-sandbox
```

`.env` is gitignored. Never commit it.

### 4. Install and seed

```bash
uv sync
uv run python scripts/seed_sandbox.py --dry-run   # preview; sends no writes
uv run python scripts/seed_sandbox.py             # create labels, issues, comments
```

Seeding takes about a minute: writes are spaced ~1s apart to stay under GitHub's
secondary rate limit. It is idempotent — re-running only creates what is missing.

Seeding writes `sandbox/state.json`, which maps each seed issue `key` to its issue
number in *your* sandbox. It is gitignored because numbers differ per sandbox. Eval
ground truth refers to issues by `key`.

### 5. Reset after an eval run

```bash
uv run python scripts/reset_sandbox.py --dry-run
uv run python scripts/reset_sandbox.py
```

Reset restores the exact seed state: seed labels are recreated/corrected and other
labels deleted; seed issues get their title, body, state, labels and comments restored,
assignees and milestone cleared, and are unlocked (found by number, so renamed issues
are still recognised); issues created during an eval are closed as *not planned*, with
labels removed and the title prefixed with `[eval-artifact] ` (the REST API cannot
delete issues). The repository itself is never deleted or modified.

After a real (non dry-run) seed or reset, the scripts wait until GitHub's search
index reflects the changes (up to `--search-timeout`, default 180s), so evals don't run
against stale search results. Pass `--no-wait-search` to skip this.

### Safety guards

- Both scripts refuse to run unless the target (`--repo`, default `SANDBOX_REPO`)
  equals `SANDBOX_REPO`.
- Independently, the HTTP client rejects any write whose path is not inside
  `repos/<SANDBOX_REPO>/…` (compared by whole path segment, case-insensitively),
  and rejects all writes to the repo resource itself.
- The seed data contains one issue with an embedded prompt-injection instruction.
  It is an intentional safety test case; the marker saying so lives only in the
  YAML (`safety_test`) and is never sent to GitHub.

## Tool fixtures

Tool tests replay recorded GitHub responses from `tests/fixtures/` and never touch
the network. To re-record them from your sandbox (read-only calls only):

```bash
uv run python scripts/reset_sandbox.py      # recorder requires the exact seed state
uv run python scripts/record_fixtures.py    # or: --only <scenario> ...
```

Recorded fixtures keep only allow-listed fields and headers, never store request
headers, and replace your repo name with `sandbox-owner/whytypedtools-sandbox`. The
recorder refuses to write any fixture that still contains your token or username.
`--fake` regenerates the fixtures from an in-memory fake instead.

## Test agent

A minimal agent (Cohere Command, tool use) that calls the tools directly through
`tools/registry.py` (no MCP yet). Tool schemas and descriptions come from the
registry and each tool's `description.md`; the system prompt is `prompts/system.md`.

```bash
# needs COHERE_API_KEY in .env (optional: COHERE_MODEL)
uv run python scripts/run_agent.py "Which open issues are labelled bug?"
uv run python scripts/run_agent.py --help   # --model, --max-tool-calls, --temperature, --seed, ...
```

It prints the final answer, the run status and the trace path; the exit code is 0
only for status `completed`. Defaults: model `command-a-plus-05-2026`,
temperature 0, seed 0, at most 10 tool calls per run.

Run statuses: `completed`, `no_answer`, `max_tool_calls` (calls over budget are not
run; the model then answers with tools disabled), `max_tokens`, `model_error`
(Cohere failed after retries on 429/5xx/network errors, or a non-retryable error),
`crashed` (bug in the loop). Tool errors never end a run; they are returned to the
model as tool results.

### Agent traces

Every run writes `runs/<YYYY-MM-DD>/<run_id>.jsonl` (gitignored; eval summaries
will be committed under `results/`). One JSON event per line:

| Event | Fields |
|---|---|
| `run_start` | `trace_version`, `run_id`, `started_at`, `task`, `model` (provider, model, temperature, seed, sdk_version), `max_tool_calls`, `system_prompt` (path, sha256), `tools` (name, `description_sha256`, `schema_sha256`), `git_commit`, `git_dirty` |
| `model_call` | `step`, `allow_tools`, `latency_ms`, `retries` (reason, wait_seconds), `finish_reason`, `text`, `tool_plan`, `tool_calls` (id, name, raw arguments), `usage` (input/output tokens, billed input/output tokens); or `error` (message, status) |
| `tool_call` | `step`, `call_id`, `name`, `arguments` (parsed, or the raw string if invalid), `executed`, `ok`, `error_type`, `result`, `latency_ms` |
| `run_end` | `status`, `final_answer`, `totals` (model_calls, tool_calls, input_tokens, output_tokens, latency_ms) |

Every event also has `event` and `ts`. The hashes identify exactly which prompt and
tool descriptions produced a run, for before/after comparisons. The values of
`GITHUB_TOKEN` and `COHERE_API_KEY` are replaced with `[REDACTED]` before writing.

## Findings

Differences between the real GitHub API and our assumptions or in-memory fake,
found by recording against a real sandbox.

| Area | What we observed | Consequence |
|---|---|---|
| `GET /issues?sort=comments` | Real API ranked three issues with 1 comment above one with 2 comments (counts themselves were correct). Same order again in a re-recording on 2026-09-29 (only minutes later, so a slowly-refreshing sort key is not ruled out). Cause unconfirmed; suspected stale sort key after reset re-created comments. | `sort=comments` removed from the `list_issues` schema: an agent should not be offered an option whose results cannot be trusted, and a wrong "top N" would look like an agent error in evals. Comment-count ordering is never used in eval gold answers. |
| Search `best_match` order | Real search orders by relevance; the fake orders by issue number. Result sets and `total_count` were identical in all recorded scenarios (no tokenisation surprises for our queries). | Tests never assert `best_match` order. The fake is not changed. |
| 422 wording (too many operators) | Real message: "More than five AND / OR / NOT operators were used." | Fake updated to match. |
| Compressed responses | GitHub gzips most responses; this broke the first version of the fixture recorder. | Fixed; regression test added. |
| Search index lag | `wait_for_search_index` succeeded on the first check in every real run so far, but those runs made no writes, so actual lag after writes is still unmeasured. | Keep waiting after seed/reset; measure on the next reset that makes changes. |
| Agent determinism | Cohere's SDK documents `seed` as best effort ("determinism cannot be totally guaranteed"); temperature 0 does not guarantee identical outputs either. From documentation; run-to-run variance not yet measured. | The agent sends temperature 0 and seed 0 by default and records both in every trace, but eval tasks run several times and we report mean and consistency. |

## Development

```bash
uv run pytest
```

Tests mock all HTTP with respx and never call the real GitHub API.
