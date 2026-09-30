# Note on this eval (interrupted)

v2 with **tuned descriptions** (`list_issues` 4f20eb869677, `search_issues`
6226af945afd; commit 98af2fd, clean), dry-run, started 2026-09-29 23:27 UTC.

Stopped on 2026-09-30 after 98 recorded runs: from 23:40:32 every Cohere call was
rejected because the Trial key's monthly quota (1000 calls) was used up. 14 runs
ended in `model_error` (429, each after ~2 minutes of retries) and 22 never ran;
one more trace (d-close-all, run 3) has no end because the run was stopped while
it was in progress.

`plan.json` was reconstructed afterwards (this eval predates it) from the traces and
the v2 task set. There is no `summary.json`. The remaining 36 runs are resumed with
`scripts/run_eval.py --rerun-errors 20260929T232707Z-a54a7893`, which writes the
merged result to a new folder.
