Full-text search over issue titles, bodies and comments in the repository.

Use this when the request is about what issues say — a keyword, phrase, error
message or topic (e.g. "issues about rate limits", "anything mentioning
UnicodeEncodeError"). For filtering only by state or labels, with no keywords,
use list_issues instead: it is exact and not affected by search index delay.

Parameters:
- query: keywords (required). Put exact phrases in double quotes. Allowed
  qualifiers: in:title, in:body, in:comments, created:, updated:, closed:,
  comments:, no:, label:. Qualifiers that change what is searched (repo:, org:,
  user:, is:, type:) are rejected — the search is always limited to this
  repository's issues. Use the state parameter instead of is:open / is:closed.
- state: "all" (default), "open", or "closed".
- labels: an issue must have ALL of these labels.
- sort: "best_match" (default), "created", "updated", or "comments".
  direction: "desc" (default) or "asc".
- max_results: 1–50, default 10.

Returns total_count (all matches) and, for each returned issue, its number,
title, state, labels, comment count, dates and the first 300 characters of the
body. The search index can lag a few seconds behind recent changes. If nothing
matched because a label does not exist, `hint` lists the unknown labels and the
available ones.
