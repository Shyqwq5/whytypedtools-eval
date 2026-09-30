# Note on this eval (generic baseline verification, third attempt)

8 tasks × 1 run × {tool_a, tool_d}, dry-run, after the guard change to JSON output
(commit 4b0b3ff). Checks (design doc, "Runs"):

| Check | Result |
|---|---|
| No crashes or harness errors | pass (16 runs, 0 errors, 0 infrastructure failures, 0 internal tool errors) |
| Every generic call scored through the mapping | pass (75 calls, none unmapped) |
| Truncated responses valid with marker and dropped count | pass (8) |
| No guard failures | pass (0; 8 guard blocks are outcomes) |
| Calibrated full-run estimate under 15M input tokens | **fail: 15.6M** (range 10.9M–23.4M) |

The fresh full run was not started. Most of the increase over the previous
verification (8.6M) comes from tool_a, which has no guard: its single
`s-search-rate-limit` run went from 23.6k to 281.8k input tokens (8 → 10 tool
calls) and stands in for 6 tasks; `f-open-bugs` went from 1 to 6 calls and stands
in for 11. One sample per proxy task makes the estimate noisy.
