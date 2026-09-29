List issues in the repository, filtered by state and labels, in a chosen order.

Use this when the request is a structured filter — for example "all open bugs",
"closed documentation issues", or "the 5 most recently updated issues". It does
not look at issue text. To find issues that mention a word, phrase or topic
(e.g. "rate limit"), use search_issues instead.

Parameters:
- state: "open" (default), "closed", or "all".
- labels: label names; an issue must have ALL of them. Omit for no label filter.
- sort: "created" (default) or "updated". direction: "desc" (default) or "asc".
- max_results: 1–50, default 20.

Returns each issue's number, title, state, labels, comment count, dates and the
first 300 characters of the body. `truncated: true` means more issues matched
than were returned. Pull requests are never included. If nothing matched because
a label does not exist, `hint` lists the unknown labels and the available ones.
