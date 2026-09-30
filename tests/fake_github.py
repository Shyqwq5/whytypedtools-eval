"""In-memory fake of the GitHub REST API endpoints this project uses.

Used by unit tests (via respx) and by `scripts/record_fixtures.py --fake` to
produce placeholder fixtures before real ones are recorded. The search
implementation is deliberately simple (case-insensitive substring matching);
real GitHub search semantics come from recorded fixtures.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import unquote, urlencode

import httpx

API = "https://api.github.com"
_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
_SEARCH_TOKEN = re.compile(r'-?[A-Za-z_]+:"[^"]*"|"[^"]*"|\S+')


class FakeGitHub:
    def __init__(self, repo: str, max_page_size: int = 100) -> None:
        self.repo = repo
        self.max_page_size = max_page_size
        self.labels: dict[str, dict[str, Any]] = {}
        self.issues: dict[int, dict[str, Any]] = {}
        self.comments: dict[int, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self._next_number = 1
        self._next_comment = 1000
        self._tick = 0
        # Simulated search index lag: a frozen copy served for N search calls.
        self._stale_index: dict[int, dict[str, Any]] | None = None
        self._stale_calls_left = 0

    @property
    def writes(self) -> list[tuple[str, str, Any]]:
        return [c for c in self.calls if c[0] != "GET"]

    # -- direct state helpers for arranging tests ---------------------------

    def _now(self) -> str:
        self._tick += 1
        return (_EPOCH + timedelta(minutes=self._tick)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def add_issue(self, title: str, *, state: str = "open", labels: list[str] | None = None,
                  body: str = "", pull_request: bool = False) -> int:
        number = self._next_number
        self._next_number += 1
        now = self._now()
        self.issues[number] = {
            "number": number,
            "title": title,
            "body": body,
            "state": state,
            "state_reason": "completed" if state == "closed" else None,
            "labels": [{"name": n} for n in labels or []],
            "assignees": [],
            "milestone": None,
            "locked": False,
            "created_at": now,
            "updated_at": now,
        }
        if pull_request:
            self.issues[number]["pull_request"] = {"url": "..."}
        return number

    def touch(self, number: int) -> None:
        self.issues[number]["updated_at"] = self._now()

    def add_comment(self, number: int, body: str) -> int:
        cid = self._next_comment
        self._next_comment += 1
        self.comments[cid] = {"id": cid, "issue": number, "body": body, "created_at": self._now()}
        if number in self.issues:
            self.touch(number)
        return cid

    def comments_on(self, number: int) -> list[str]:
        return [c["body"] for c in sorted(self.comments.values(), key=lambda c: c["id"])
                if c["issue"] == number]

    def by_title(self, title: str) -> list[dict[str, Any]]:
        return [i for i in self.issues.values() if i["title"] == title]

    def freeze_search_index(self, for_calls: int) -> None:
        """Serve the current issues from search for the next `for_calls` searches."""
        self._stale_index = copy.deepcopy(self.issues)
        self._stale_calls_left = for_calls

    def load_seed(self, data: Any) -> dict[str, int]:
        """Populate directly from SeedData (no HTTP). Returns key -> number."""
        for spec in data.labels:
            self.labels[spec.name] = {"name": spec.name, "color": spec.color,
                                      "description": spec.description}
        mapping = {}
        for spec in data.issues:
            n = self.add_issue(spec.title, state=spec.state, labels=spec.labels, body=spec.body)
            if spec.state == "closed":
                self.issues[n]["state_reason"] = spec.state_reason
            for c in spec.comments:
                self.add_comment(n, c.body)
            mapping[spec.key] = n
        return mapping

    # -- JSON views ---------------------------------------------------------

    def issue_json(self, issue: dict[str, Any]) -> dict[str, Any]:
        n = issue["number"]
        return {
            **copy.deepcopy(issue),
            "comments": sum(1 for c in self.comments.values() if c["issue"] == n),
            "repository_url": f"{API}/repos/{self.repo}",
            "html_url": f"https://github.com/{self.repo}/issues/{n}",
        }

    # -- HTTP handling ------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        self.calls.append((request.method, path, body))
        if "invalid" in request.headers.get("Authorization", ""):
            return httpx.Response(401, json={"message": "Bad credentials"})
        if path == "/search/issues" and request.method == "GET":
            return self._search(request)
        prefix = f"/repos/{self.repo}"
        if not path.lower().startswith(prefix.lower() + "/"):
            return httpx.Response(404, json={"message": "Not Found"})
        sub = path[len(prefix):]
        method = request.method

        if sub == "/labels":
            if method == "GET":
                return self._page(request, sorted(self.labels.values(), key=lambda lbl: lbl["name"]))
            if method == "POST":
                self.labels[body["name"]] = {"name": body["name"], "color": body["color"],
                                             "description": body.get("description", "")}
                return httpx.Response(201, json=self.labels[body["name"]])
        if m := re.fullmatch(r"/labels/(.+)", sub):
            name = unquote(m.group(1))
            if method == "PATCH":
                label = self.labels.pop(name)
                label.update(name=body["new_name"], color=body["color"], description=body["description"])
                self.labels[label["name"]] = label
                for issue in self.issues.values():
                    for lbl in issue["labels"]:
                        if lbl["name"] == name:
                            lbl["name"] = label["name"]
                return httpx.Response(200, json=label)
            if method == "DELETE":
                del self.labels[name]
                for issue in self.issues.values():
                    issue["labels"] = [lbl for lbl in issue["labels"] if lbl["name"] != name]
                return httpx.Response(204)
        if sub == "/issues":
            if method == "GET":
                return self._list_issues(request)
            if method == "POST":
                number = self.add_issue(body["title"], body=body.get("body", ""),
                                        labels=body.get("labels"))
                return httpx.Response(201, json=self.issue_json(self.issues[number]))
        if m := re.fullmatch(r"/issues/(\d+)(/.*)?", sub):
            if int(m.group(1)) not in self.issues:
                return httpx.Response(404, json={"message": "Not Found"})
        if m := re.fullmatch(r"/issues/(\d+)/labels", sub):
            issue = self.issues[int(m.group(1))]
            if method == "POST":
                # Like GitHub: unknown labels are created on the fly.
                for name in body["labels"]:
                    self.labels.setdefault(name, {"name": name, "color": "ededed", "description": ""})
                    if name not in [lbl["name"] for lbl in issue["labels"]]:
                        issue["labels"].append({"name": name})
                self.touch(issue["number"])
                return httpx.Response(200, json=[
                    {**self.labels.get(lbl["name"], {"name": lbl["name"]}), "id": 1} for lbl in issue["labels"]
                ])
        if m := re.fullmatch(r"/issues/(\d+)", sub):
            issue = self.issues[int(m.group(1))]
            if method == "GET":
                return httpx.Response(200, json=self.issue_json(issue))
            if method == "PATCH":
                for field in ("title", "body"):
                    if field in body:
                        issue[field] = body[field]
                if "labels" in body:
                    issue["labels"] = [{"name": n} for n in body["labels"]]
                if "assignees" in body:
                    issue["assignees"] = [{"login": a} for a in body["assignees"]]
                if "milestone" in body:
                    issue["milestone"] = None if body["milestone"] is None else {"number": body["milestone"]}
                if body.get("state") == "closed":
                    issue["state"] = "closed"
                    issue["state_reason"] = body.get("state_reason", "completed")
                elif body.get("state") == "open":
                    issue["state"], issue["state_reason"] = "open", "reopened"
                self.touch(issue["number"])
                return httpx.Response(200, json=self.issue_json(issue))
        if m := re.fullmatch(r"/issues/(\d+)/lock", sub):
            issue = self.issues[int(m.group(1))]
            if method in ("PUT", "DELETE"):
                issue["locked"] = method == "PUT"
                return httpx.Response(204)
        if m := re.fullmatch(r"/issues/(\d+)/comments", sub):
            number = int(m.group(1))
            if method == "GET":
                since = request.url.params.get("since", "")
                items = [{"id": c["id"], "body": c["body"], "created_at": c["created_at"],
                          "updated_at": c["created_at"]}
                         for c in self.comments.values() if c["issue"] == number and c["created_at"] >= since]
                return self._page(request, sorted(items, key=lambda c: c["id"]))
            if method == "POST":
                cid = self.add_comment(number, body["body"])
                return httpx.Response(201, json={"id": cid, "body": body["body"]})
        if m := re.fullmatch(r"/issues/comments/(\d+)", sub):
            if method == "DELETE":
                comment = self.comments.pop(int(m.group(1)))
                if comment["issue"] in self.issues:
                    self.touch(comment["issue"])
                return httpx.Response(204)
        return httpx.Response(404, json={"message": f"unhandled {method} {sub}"})

    def _list_issues(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        state = params.get("state", "open")
        items = [self.issue_json(i) for i in self.issues.values()]
        if state != "all":
            items = [i for i in items if i["state"] == state]
        if params.get("labels"):
            wanted = {name.strip().lower() for name in params["labels"].split(",")}
            items = [i for i in items if wanted <= {lbl["name"].lower() for lbl in i["labels"]}]
        key = {"created": "number", "updated": "updated_at", "comments": "comments"}[params.get("sort", "created")]
        items.sort(key=lambda i: (i[key], i["number"]), reverse=params.get("direction", "desc") == "desc")
        return self._page(request, items)

    def _search(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        tokens = _SEARCH_TOKEN.findall(params.get("q", ""))
        if not any(t.lower() in ("is:issue", "is:pull-request", "is:pr", "type:issue", "type:pr") for t in tokens):
            # Real API behaviour (observed 2026-09-30).
            return httpx.Response(422, json={"message": "Query must include 'is:issue' or 'is:pull-request'"})
        operators = sum(1 for t in tokens if t in ("AND", "OR", "NOT"))
        if operators > 5:
            return httpx.Response(422, json={
                "message": "Validation Failed",
                # Wording as returned by the real search API (observed in a recording).
                "errors": [{"message": "More than five AND / OR / NOT operators were used.",
                            "code": "invalid"}],
            })
        source = self.issues
        if self._stale_index is not None:
            source = self._stale_index
            self._stale_calls_left -= 1
            if self._stale_calls_left <= 0:
                self._stale_index = None

        repo_ok, is_issue, state = False, False, None
        labels: list[str] = []
        not_labels: list[str] = []
        scopes: set[str] = set()
        groups: list[list[str]] = [[]]
        for tok in tokens:
            m = re.fullmatch(r'(-?)([A-Za-z_]+):"?([^"]*)"?', tok)
            if m and not tok.startswith('"'):
                neg, qual, val = m.group(1), m.group(2).lower(), m.group(3)
                if qual == "repo":
                    repo_ok = val.lower() == self.repo.lower()
                elif qual == "is":
                    is_issue = val == "issue"
                elif qual == "state":
                    state = val
                elif qual == "label":
                    (not_labels if neg else labels).append(val.lower())
                elif qual == "in":
                    scopes.add(val)
                continue
            if tok == "OR":
                groups.append([])
            elif tok not in ("AND",):
                groups[-1].append(tok.strip('"').lower())
        groups = [g for g in groups if g]

        def text(issue: dict[str, Any]) -> str:
            parts = []
            if not scopes or "title" in scopes:
                parts.append(issue["title"])
            if not scopes or "body" in scopes:
                parts.append(issue.get("body") or "")
            if not scopes or "comments" in scopes:
                parts.extend(self.comments_on(issue["number"]))
            return "\n".join(parts).lower()

        matched = []
        if repo_ok and is_issue:
            for issue in source.values():
                if "pull_request" in issue or (state and issue["state"] != state):
                    continue
                names = {lbl["name"].lower() for lbl in issue["labels"]}
                if not set(labels) <= names or names & set(not_labels):
                    continue
                t = text(issue)
                if groups and not any(all(term in t for term in g) for g in groups):
                    continue
                matched.append(self.issue_json(issue))

        sort = params.get("sort")
        if sort:
            key = {"created": "number", "updated": "updated_at", "comments": "comments"}[sort]
            matched.sort(key=lambda i: (i[key], i["number"]), reverse=params.get("order", "desc") == "desc")
        else:
            # best_match: real GitHub ranks by relevance; the fake just orders by
            # number. Result sets and total_count matched the real API in every
            # recorded scenario; only this order differs (see README "Findings").
            # Tests must not depend on best_match order.
            matched.sort(key=lambda i: i["number"])
        per_page = min(int(params.get("per_page", 30)), self.max_page_size)
        page = int(params.get("page", 1))
        chunk = matched[(page - 1) * per_page: page * per_page]
        headers = {}
        if page * per_page < len(matched):
            nxt = {**params, "page": str(page + 1), "per_page": str(per_page)}
            headers["Link"] = f'<{API}/search/issues?{urlencode(nxt)}>; rel="next"'
        payload = {"total_count": len(matched), "incomplete_results": False, "items": chunk}
        return httpx.Response(200, json=payload, headers=headers)

    def _page(self, request: httpx.Request, items: list[Any]) -> httpx.Response:
        params = dict(request.url.params)
        per_page = min(int(params.get("per_page", 30)), self.max_page_size)
        page = int(params.get("page", 1))
        chunk = items[(page - 1) * per_page: page * per_page]
        headers = {}
        if page * per_page < len(items):
            nxt = {**params, "page": str(page + 1), "per_page": str(per_page)}
            headers["Link"] = f'<{API}{request.url.path}?{urlencode(nxt)}>; rel="next"'
        return httpx.Response(200, json=chunk, headers=headers)
