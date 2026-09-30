Call the GitHub REST API (https://api.github.com) for this repository's issues.

Use `{repo}` in the path for the repository, e.g. `repos/{repo}/issues`. The
request is sent with the repository's credentials; you never need a token.

Useful endpoints:
- List issues: GET `repos/{repo}/issues` with query `state` ("open" default,
  "closed", "all"), `labels` (comma-separated; an issue must have all of them),
  `sort`, `direction`, `per_page` (max 100), `page`. Pull requests can appear in
  this list (they have a `pull_request` field).
- Search issues: GET `search/issues` with `q`, e.g.
  `repo:{repo} is:issue "rate limit" is:open label:bug`.
- One issue: GET `repos/{repo}/issues/{number}`; its comments: GET
  `repos/{repo}/issues/{number}/comments`.
- Repository labels: GET `repos/{repo}/labels`.
- Add labels to an issue: POST `repos/{repo}/issues/{number}/labels` with body
  `{"labels": ["bug"]}`. Other writes (close, comment, edit) use the usual REST
  endpoints. Writes change the repository.

Limits: only this repository can be read (paths under `repos/{repo}/`, and
`search/issues` with `repo:{repo}` in `q`). Writes are allowed only under
`repos/{repo}/`; deleting comments, labels or issues and changing repository
settings are not permitted. Responses longer than 48,000 characters are cut at
whole items: then `truncated` is true and `items_dropped` says how many items were
left out; use `per_page` and `page` to get the rest.

Returns `status` (the HTTP status), `body` (GitHub's JSON response) and, for
paged lists, `link` with the next/last page paths. Errors are GitHub's own status
and message.
