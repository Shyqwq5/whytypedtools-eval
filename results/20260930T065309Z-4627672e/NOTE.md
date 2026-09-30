# Note on this eval (generic baseline verification)

8 tasks × 1 run × {tool_a, tool_d}, dry-run, with the rules fixed in
docs/design/generic-api-baseline.md (commits 6196f96..bb63467).

Verification checks (design doc, "Runs"):

| Check | Result |
|---|---|
| All 16 runs finish without crashes or harness errors | pass (0 errors, 0 internal tool errors) |
| Every generic call scored through the mapping | pass (54 calls, none unmapped) |
| Truncated responses valid JSON with marker and dropped-item count | pass (10 truncations: 15 items kept, 7 dropped, 46.5k chars) |
| No guard failures | **fail**: 3 guard calls in `h-okta-label-question` (tool_d) got HTTP 422 from Cohere |
| Calibrated full-run estimate under 15M input tokens | pass at the point estimate (10.6M; range 7.4M–15.9M) |

Because one check failed, the full run was not started. Replaying the same guard
request afterwards succeeded, and Cohere's error text was not recorded (by
design, only the status code is kept), so the cause of the 422 is unknown.
