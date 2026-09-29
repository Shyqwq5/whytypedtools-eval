# Note on this eval

v2 dry-run baseline (current descriptions). 13 of 120 runs ended in `model_error`
because of two infrastructure bugs, both fixed afterwards:

- 8 runs: HTTP 429 from Cohere after 4 retries (~25 s of backoff).
- 5 runs: HTTP 400 on the forced final turn after the tool budget, because the model
  rejects `tool_choice`. These are the bulk-label tasks; their writes (captured in
  dry-run) happened before the error, so their safety outcome (`unsafe`) is valid.

The 107 other runs are unaffected. Errored runs are re-run with
`scripts/run_eval.py --rerun-errors 20260929T224147Z-2645532a`, which writes a
merged result to a new folder.
