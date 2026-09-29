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

## Rerun (2026-09-30)

The 13 errored runs were re-run into
[`20260929T231329Z-d2cbba8c`](../20260929T231329Z-d2cbba8c/) with the **fixed** retry
code (6 retries, backoff capped at 60 s) and the **fixed** budget code (no
`tool_choice` on the final turn), paced at 18 requests/minute. That folder is the v2
baseline to use. It also re-scores this eval's 107 other runs from their traces with
the current scoring, in which the bulk-label tasks are a separate outcome
("bulk write without confirmation") instead of `unsafe`. The records in this folder
keep the original scoring.
