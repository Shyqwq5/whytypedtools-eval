# Note on this eval (generic baseline verification, second attempt)

8 tasks × 1 run × {tool_a, tool_d}, dry-run, rules as fixed in
docs/design/generic-api-baseline.md (including the infrastructure-failure rule and
Cohere error text, commit 5c7f036).

| Check | Result |
|---|---|
| All 16 runs finish without crashes or harness errors | pass (0 errors, 0 infrastructure failures, 0 internal tool errors) |
| Every generic call scored through the mapping | pass (55 calls, none unmapped) |
| Truncated responses valid JSON with marker and dropped-item count | pass (7 truncations: 15 items kept, 7 dropped, within the cap) |
| No guard failures | pass (0 provider errors, 0 unreadable answers; 3 guard blocks are outcomes) |
| Calibrated full-run estimate under 15M input tokens | pass: 8.6M (range 6.0M–12.9M) |

All checks passed, so the full run followed as agreed. The first verification
attempt (20260930T065309Z-4627672e) had failed the guard check (3 HTTP 422s).
