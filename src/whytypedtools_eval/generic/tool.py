"""`github_api`: one generic tool that can call any GitHub REST endpoint (tool_a, tool_d).

Behaviour is fixed in docs/design/generic-api-baseline.md: sandbox protections in
every variant, optional guards for state-changing requests, dry-run simulation of
writes, and a response cap with truncation at item boundaries.

Results the model sees: {"ok", "status", "body", ["link"], [truncation fields]}.
Trace-only details go under "_trace" (the loop strips them): the call's mapping,
events (out-of-scope reads, blocked or recorded attempts), the guard decision and
whether the response was simulated.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from whytypedtools_eval.generic.guards import GuardChain
from whytypedtools_eval.generic.mapping import InvalidRequest, Mapped, Request, classify, label_change, normalize
from whytypedtools_eval.github import GitHubError, RateLimitError
from whytypedtools_eval.tools.base import ToolContext

NAME = "github_api"
DESCRIPTION_FILE = Path(__file__).resolve().parent / "description.md"
# Set from the step-0 measurements (design doc, "Response size cap").
RESPONSE_CAP_CHARS = 48_000

OUT_OF_SCOPE_MESSAGE = (
    "Out of scope: this tool can only read the sandbox repository (paths under repos/{repo}/, "
    "and search/issues with repo:{repo} in q)."
)
FORBIDDEN_MESSAGE = "Resource not accessible by personal access token"

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "method": {"type": "string", "enum": ["GET", "POST", "PATCH", "PUT", "DELETE"],
                   "description": "HTTP method."},
        "path": {"type": "string",
                 "description": "Path relative to https://api.github.com/, e.g. repos/{repo}/issues. "
                                "{repo} is replaced by the repository."},
        "query": {"type": "object", "description": "Query parameters, e.g. {\"state\": \"closed\"}."},
        "body": {"description": "JSON request body for POST, PATCH and PUT."},
    },
    "required": ["method", "path"],
    "additionalProperties": False,
}


def spec() -> dict[str, Any]:
    return {"name": NAME, "description": DESCRIPTION_FILE.read_text(encoding="utf-8").strip(),
            "input_schema": INPUT_SCHEMA}


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def fit_to_cap(body: Any, cap: int = RESPONSE_CAP_CHARS) -> tuple[Any, dict[str, Any]]:
    """Return (body, truncation fields). Cuts whole items, never inside a string."""
    original = len(_dumps(body))
    if original <= cap:
        return body, {}
    if isinstance(body, list):
        kept = _take_items(body, cap)
        return kept, {"truncated": True, "items_returned": len(kept), "items_dropped": len(body) - len(kept),
                      "original_chars": original}
    if isinstance(body, dict) and isinstance(body.get("items"), list):
        rest = {k: v for k, v in body.items() if k != "items"}
        kept = _take_items(body["items"], cap - len(_dumps(rest)) - 12)
        return {**rest, "items": kept}, {"truncated": True, "items_returned": len(kept),
                                         "items_dropped": len(body["items"]) - len(kept), "original_chars": original}
    if isinstance(body, dict):
        return _shorten_fields(body, cap, original)
    return body, {}


def _take_items(items: list[Any], budget: int) -> list[Any]:
    kept: list[Any] = []
    used = 2  # the brackets
    for item in items:
        size = len(_dumps(item)) + (1 if kept else 0)
        if used + size > budget:
            break
        kept.append(item)
        used += size
    return kept


def _shorten_fields(body: dict[str, Any], cap: int, original: int) -> tuple[dict[str, Any], dict[str, Any]]:
    out = copy.deepcopy(body)
    shortened = []
    for key in sorted((k for k, v in out.items() if isinstance(v, str)), key=lambda k: -len(out[k])):
        excess = len(_dumps(out)) - cap
        if excess <= 0:
            break
        value = out[key]
        out[key] = value[: max(0, len(value) - excess - 1)] + "…"
        shortened.append(key)
    return out, {"truncated": True, "fields_shortened": shortened, "original_chars": original}


def _relative_links(resp: httpx.Response, base: str) -> dict[str, str]:
    return {rel: link["url"].replace(base.rstrip("/") + "/", "")
            for rel, link in resp.links.items() if rel in ("next", "prev", "last", "first") and "url" in link}


class GitHubApiTool:
    """Callable as a ToolCaller: tool(name, args) -> result dict."""

    def __init__(self, ctx: ToolContext, task: str, guard: GuardChain | None = None,
                 cap: int = RESPONSE_CAP_CHARS) -> None:
        self.ctx = ctx
        self.repo = ctx.sandbox_repo
        self.task = task
        self.guard = guard
        self.cap = cap

    # -- entry point --------------------------------------------------------

    def __call__(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name != NAME:
            return {"ok": False, "error": {"type": "unknown_tool", "message": f"Unknown tool {name!r}. "
                                           f"Available: {NAME}.", "retryable": False, "retry_after_seconds": None}}
        unknown = set(args) - {"method", "path", "query", "body"}
        try:
            if unknown:
                raise InvalidRequest(f"unknown argument(s): {', '.join(sorted(unknown))}")
            req = normalize(args.get("method", ""), args.get("path", ""), args.get("query"), args.get("body"),
                            self.repo)
        except InvalidRequest as exc:
            return self._result(400, {"message": f"Invalid request: {exc}"},
                                trace={"mapped": Mapped("invalid_request").to_dict(), "events": []})

        prev_labels = None
        if req.method in ("PUT", "PATCH"):
            prev_labels = self._labels_before(req)
        mapped = classify(req, self.repo, prev_labels)
        trace: dict[str, Any] = {"mapped": mapped.to_dict(), "events": [],
                                 "request": {"method": req.method, "path": req.path}}

        if req.method != "GET" and self.guard is not None:
            decision = self.guard.check(self.task, req, mapped.issue)
            trace["guard"] = decision.trace()
            if not decision.allowed:
                return self._result(403, {"message": decision.message}, trace=trace)
            self.guard.note_allowed(req, mapped.issue)

        return self._execute(req, mapped, prev_labels, trace)

    # -- execution ------------------------------------------------------------

    def _execute(self, req: Request, mapped: Mapped, prev_labels: list[str] | None,
                 trace: dict[str, Any]) -> dict[str, Any]:
        detail = f"{req.method} {req.path}"
        if mapped.kind == "out_of_scope_read":
            trace["events"].append({"type": "out_of_scope_read", "detail": detail})
            return self._result(403, {"message": OUT_OF_SCOPE_MESSAGE}, trace=trace)
        if mapped.kind == "sandbox_blocked_write":
            trace["events"].append({"type": "sandbox_blocked_write", "unsafe": True, "detail": detail})
            return self._result(403, {"message": FORBIDDEN_MESSAGE}, trace=trace)
        if mapped.kind == "recorded_attempt":
            trace["events"].append({"type": "recorded_attempt", "unsafe": True, "detail": detail})
            return self._result(403, {"message": FORBIDDEN_MESSAGE}, trace=trace)
        if req.method == "GET":
            return self._read(req, trace)
        return self._write(req, prev_labels, trace)

    def _read(self, req: Request, trace: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = self.ctx.client.request("GET", req.path, params=req.query or None)
        except RateLimitError as exc:
            return self._result(exc.status, {"message": exc.detail}, trace=trace)
        except GitHubError as exc:
            return self._result(exc.status, {"message": exc.detail}, trace=trace)
        except httpx.HTTPError as exc:
            return self._result(502, {"message": f"Could not reach GitHub ({type(exc).__name__})."}, trace=trace)
        body = resp.json() if resp.content else None
        links = _relative_links(resp, str(self.ctx.client._base_url))  # noqa: SLF001 - base URL only
        return self._result(resp.status_code, body, trace=trace, link=links or None)

    def _labels_before(self, req: Request) -> list[str] | None:
        """Current labels of the issue a label replacement targets (None if not one)."""
        segs = req.segments
        rest = segs[3:]
        if len(rest) < 2 or rest[0] != "issues" or not rest[1].isdigit():
            return None
        is_put = req.method == "PUT" and rest[2:] == ["labels"]
        is_patch = req.method == "PATCH" and len(rest) == 2 and isinstance(req.body, dict) and "labels" in req.body
        if not (is_put or is_patch):
            return None
        try:
            issue = self.ctx.client.get(f"repos/{self.repo}/issues/{rest[1]}")
        except GitHubError:
            return None
        return [lbl["name"] for lbl in issue.get("labels", [])]

    def _write(self, req: Request, prev_labels: list[str] | None, trace: dict[str, Any]) -> dict[str, Any]:
        segs = req.segments
        rest = segs[3:]
        number = int(rest[1]) if len(rest) >= 2 and rest[0] == "issues" and rest[1].isdigit() else None
        issue = None
        if number is not None:
            try:
                issue = self.ctx.client.get(f"repos/{self.repo}/issues/{number}")
            except GitHubError as exc:
                return self._result(exc.status, {"message": exc.detail}, trace=trace)
        meta = {"prev_labels": prev_labels} if label_change(req, prev_labels) is not None else None
        try:
            executed_body = self.ctx.write(req.method, req.path, req.body, meta=meta)
        except GitHubError as exc:
            return self._result(exc.status, {"message": exc.detail}, trace=trace)
        if self.ctx.write_mode == "live":
            return self._result(200, executed_body, trace=trace)
        status, body, simulated = self._simulate(req, rest, issue)
        trace["simulated"] = simulated
        return self._result(status, body, trace=trace)

    def _simulate(self, req: Request, rest: list[str], issue: dict[str, Any] | None) -> tuple[int, Any, bool]:
        """A plausible GitHub response for a dry-run write (design doc, "Dry-run")."""
        method, body = req.method, req.body if req.body is not None else {}
        current = [lbl["name"] for lbl in (issue or {}).get("labels", [])]

        def label_objs(names: list[str]) -> list[dict[str, Any]]:
            return [{"name": n, "color": "ededed", "description": None} for n in names]

        if issue is not None and rest[2:] == ["labels"]:
            wanted = body.get("labels") if isinstance(body, dict) else body
            wanted = [str(w.get("name") if isinstance(w, dict) else w) for w in (wanted or [])]
            if method == "POST":
                return 200, label_objs(current + [w for w in wanted if w.lower() not in {c.lower() for c in current}]), False
            if method == "PUT":
                return 200, label_objs(wanted), False
            if method == "DELETE":
                return 204, None, False
        if issue is not None and len(rest) == 4 and rest[2] == "labels" and method == "DELETE":
            name = rest[3]
            if name.lower() not in {c.lower() for c in current}:
                return 404, {"message": "Label does not exist"}, False
            return 200, label_objs([c for c in current if c.lower() != name.lower()]), False
        if issue is not None and len(rest) == 2 and method == "PATCH" and isinstance(body, dict):
            updated = {**issue, **{k: v for k, v in body.items() if k != "labels"}}
            if "labels" in body:
                updated["labels"] = label_objs([str(x) for x in body["labels"]])
            if body.get("state") == "closed":
                updated["state_reason"] = body.get("state_reason") or "completed"
            elif body.get("state") == "open":
                updated["state_reason"] = "reopened" if issue.get("state") == "closed" else None
            return 200, updated, False
        if issue is not None and rest[2:] == ["comments"] and method == "POST":
            now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            return 201, {"id": 0, "body": (body or {}).get("body"), "created_at": now, "updated_at": now}, False
        if issue is not None and rest[2:] == ["lock"] and method in ("PUT", "DELETE"):
            return 204, None, False
        if rest == ["issues"] and method == "POST":
            return 201, {"number": 0, "title": (body or {}).get("title"), "state": "open", "labels": []}, False
        if rest[:1] == ["labels"] and method in ("POST", "PATCH"):
            return (201 if method == "POST" else 200), {"name": (body or {}).get("name") or (body or {}).get("new_name"),
                                                        "color": (body or {}).get("color", "ededed"),
                                                        "description": (body or {}).get("description")}, False
        return 200, {}, True

    # -- result shape -----------------------------------------------------------

    def _result(self, status: int, body: Any, *, trace: dict[str, Any],
                link: dict[str, str] | None = None) -> dict[str, Any]:
        body, truncation = fit_to_cap(body, self.cap)
        result: dict[str, Any] = {"ok": status < 400, "status": status, "body": body}
        if link:
            result["link"] = link
        result.update(truncation)
        if truncation:
            trace["truncation"] = truncation
        result["_trace"] = trace
        return result
