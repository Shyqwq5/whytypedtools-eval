Get one issue by number, with its full body and comments.

Use this when you need everything about a specific issue — the complete
description, the discussion in the comments, or details that list_issues and
search_issues cut off (they only return the first 300 characters of the body).
To find issues in the first place, use list_issues (filter by state/labels) or
search_issues (find text).

Parameters:
- number: the issue number (12 for #12).
- include_comments: true (default) to also return comments, oldest first.
- max_comments: 1–50, default 20.

Returns number, title, state, labels, dates, the full body, and the comments
(`comments_total` is the total count; `comments_truncated: true` means not all were
returned). Pull requests are not issues and return a not_found error.
