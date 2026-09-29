"""Shared fixtures. All HTTP is mocked with respx; no test touches the real API."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlencode

import httpx
import pytest
import respx

from whytypedtools_eval.config import Settings
from whytypedtools_eval.github import GitHubClient

API = "https://api.github.com"
REPO = "me/sandbox"
SEED_FILE = Path(__file__).resolve().parents[1] / "sandbox" / "seed_data.yaml"


class FakeGitHub:
    """In-memory GitHub REST API covering the label/issue/comment endpoints we use."""

    def __init__(self, repo: str = REPO, max_page_size: int = 100) -> None:
        self.repo = repo
        self.max_page_size = max_page_size
        self.labels: dict[str, dict[str, Any]] = {}
        self.issues: dict[int, dict[str, Any]] = {}
        self.comments: dict[int, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self._next_number = 1
        self._next_comment = 1000

    @property
    def writes(self) -> list[tuple[str, str, Any]]:
        return [c for c in self.calls if c[0] != "GET"]

    # -- direct state helpers for arranging tests ---------------------------

    def add_issue(self, title: str, *, state: str = "open", labels: list[str] | None = None,
                  body: str = "", pull_request: bool = False) -> int:
        number = self._next_number
        self._next_number += 1
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
        }
        if pull_request:
            self.issues[number]["pull_request"] = {"url": "..."}
        return number

    def add_comment(self, number: int, body: str) -> int:
        cid = self._next_comment
        self._next_comment += 1
        self.comments[cid] = {"id": cid, "issue": number, "body": body}
        return cid

    def comments_on(self, number: int) -> list[str]:
        return [c["body"] for c in sorted(self.comments.values(), key=lambda c: c["id"])
                if c["issue"] == number]

    def by_title(self, title: str) -> list[dict[str, Any]]:
        return [i for i in self.issues.values() if i["title"] == title]

    # -- HTTP handling ------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        self.calls.append((request.method, path, body))
        prefix = f"/repos/{self.repo}"
        if not path.lower().startswith(prefix.lower() + "/"):
            return httpx.Response(404, json={"message": "Not Found"})
        sub = path[len(prefix):]
        m, method = None, request.method

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
                items = sorted(self.issues.values(), key=lambda i: -i["number"])
                return self._page(request, items)
            if method == "POST":
                number = self.add_issue(body["title"], body=body.get("body", ""),
                                        labels=body.get("labels"))
                return httpx.Response(201, json=self.issues[number])
        if m := re.fullmatch(r"/issues/(\d+)", sub):
            issue = self.issues[int(m.group(1))]
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
                return httpx.Response(200, json=issue)
        if m := re.fullmatch(r"/issues/(\d+)/lock", sub):
            issue = self.issues[int(m.group(1))]
            if method in ("PUT", "DELETE"):
                issue["locked"] = method == "PUT"
                return httpx.Response(204)
        if m := re.fullmatch(r"/issues/(\d+)/comments", sub):
            number = int(m.group(1))
            if method == "GET":
                items = [{"id": c["id"], "body": c["body"]} for c in self.comments.values()
                         if c["issue"] == number]
                return self._page(request, sorted(items, key=lambda c: c["id"]))
            if method == "POST":
                cid = self.add_comment(number, body["body"])
                return httpx.Response(201, json={"id": cid, "body": body["body"]})
        if m := re.fullmatch(r"/issues/comments/(\d+)", sub):
            if method == "DELETE":
                del self.comments[int(m.group(1))]
                return httpx.Response(204)
        return httpx.Response(404, json={"message": f"unhandled {method} {sub}"})

    def _page(self, request: httpx.Request, items: list[Any]) -> httpx.Response:
        params = dict(request.url.params)
        if params.get("state", "open") != "all" and request.url.path.endswith("/issues"):
            items = [i for i in items if i["state"] == params.get("state", "open")]
        per_page = min(int(params.get("per_page", 30)), self.max_page_size)
        page = int(params.get("page", 1))
        chunk = items[(page - 1) * per_page: page * per_page]
        headers = {}
        if page * per_page < len(items):
            nxt = {**params, "page": str(page + 1), "per_page": str(per_page)}
            headers["Link"] = f'<{API}{request.url.path}?{urlencode(nxt)}>; rel="next"'
        return httpx.Response(200, json=chunk, headers=headers)


@pytest.fixture(autouse=True)
def _no_real_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's real token out of every test."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("SANDBOX_REPO", raising=False)


@pytest.fixture
def mock_api():
    # assert_all_mocked=True (default) makes any unmocked request fail loudly.
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield router


@pytest.fixture
def fake(mock_api: respx.Router) -> FakeGitHub:
    gh = FakeGitHub()
    mock_api.route().mock(side_effect=gh.handler)
    return gh


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, github_token="github_pat_test", sandbox_repo=REPO)  # type: ignore[call-arg]


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def client(sleeps: list[float]) -> GitHubClient:
    c = GitHubClient("github_pat_test", REPO, write_interval=0, sleep=sleeps.append)
    yield c
    c.close()
