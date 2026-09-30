# Note on this eval (generic baseline full run, stopped)

Full run (40 tasks × 3 runs × {tool_a, tool_d}, dry-run) with the rules fixed in
docs/design/generic-api-baseline.md. It stopped after 16 of 240 runs, as the
infrastructure-failure rule requires:

- `tool_d f-label-webhook-api` run 1: 3 guard calls failed with a provider error,
  so the run was rerun once (`superseded_by` / `rerun_of` set; both kept here).
- The rerun: 2 guard calls failed the same way, so the eval stopped.

Cohere's error text (now recorded): HTTP 422, "your request resulted in an invalid
tool generation. Try updating the messages or tool definitions". The guard call
declares no tools, so the guard model attempted a tool call anyway. Reproduced
deterministically afterwards through the runtime code path (2/2); the same
messages succeed with Cohere's JSON response format or with thinking enabled.
Changing either is a change to the fixed guard setup, so it was not made.

Completed runs: 16 (plus the superseded one). Spend: 431k input, 16k output
tokens, 56 model calls. Resume with `--rerun-errors 20260930T073700Z-3611c162`
once the guard setup is decided.
