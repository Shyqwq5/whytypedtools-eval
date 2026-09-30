List the comments on one issue, oldest first: only the discussion, not the issue's
body.

Use this when the request is about what people said on an issue: the discussion, a
decision or answer given in a comment, or only the comments since a date. To read
the issue itself (its body, state, state reason or labels), or the issue together
with its comments, use get_issue instead. To find issues first, use list_issues or
search_issues.

Parameters:
- number: the issue number (12 for #12).
- since: a date (YYYY-MM-DD); only comments updated on or after it. Omit for all.
- max_results: 1–50, default 20.

Returns number and title of the issue, comments_total (all comments on the issue,
ignoring since), and comments, each with created_at, updated_at (dates), body and
body_truncated (true when the comment was longer than 4,000 characters and cut).
returned is how many comments are in the list. truncated: true means more comments
matched than max_results allowed, so ask for more or use since; truncated: false
means these are all the matching comments. A number that is not an issue (or is a
pull request) returns a not_found error.
