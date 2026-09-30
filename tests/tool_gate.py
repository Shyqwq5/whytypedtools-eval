"""The offline tool gate: checks every registered tool must pass (docs/design/tool-gate.md).

Each check takes a tool name and returns the set of failing items (empty = pass), so
known exceptions can be matched item by item and a fixed item shows up as a stale
exception. No check touches the network: HTTP goes to respx mocks or the in-memory
fake. tests/tools/test_tool_gate.py runs every check for every tool in the registry.
"""

from __future__ import annotations

import inspect
import json
import re
import typing
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import respx
import yaml
from pydantic import BaseModel

from tests.fake_github import API, FakeGitHub
from tests.recording import FIXTURE_REPO, FIXTURE_TOKEN, FIXTURES_DIR, all_fixtures, load_fixture
from whytypedtools_eval.github import GitHubClient
from whytypedtools_eval.sandbox.models import load_seed
from whytypedtools_eval.tools.base import ToolContext
from whytypedtools_eval.tools.registry import TOOLS, ToolSpec, call_tool

ROOT = Path(__file__).resolve().parents[1]
SEED_FILE = ROOT / "sandbox" / "seed_data.yaml"
EXCEPTIONS_FILE = Path(__file__).resolve().parent / "tools" / "gate_exceptions.yaml"
# A second repository: tools run against it must scope every request to it, which
# shows the scope comes from the ToolContext, not from the arguments or a constant.
GATE_REPO = "gate-owner/gate-repo"
OTHER_REPO = "other-owner/other-repo"
# Input names that would let the agent choose where a request goes.
SCOPE_NAMES = frozenset({"repo", "repository", "owner", "org", "organization", "user", "path", "url",
                         "endpoint", "host", "base_url", "api_url"})
ERROR_KEYS = {"type", "message", "retryable", "retry_after_seconds"}
ECHO_MARKER = "GATE_ECHO_VALUE"

CheckFn = Callable[[str], set[str]]


# -- helpers --------------------------------------------------------------------


def spec(name: str) -> ToolSpec:
    return TOOLS[name]


def fixtures_of(name: str) -> list[dict[str, Any]]:
    return [f for f in all_fixtures() if f["tool"] == name]


def ok_fixtures(name: str) -> list[dict[str, Any]]:
    return [f for f in fixtures_of(name) if (f.get("result") or {}).get("ok")]


def example_args(name: str) -> dict[str, Any] | None:
    oks = ok_fixtures(name)
    return dict(oks[0]["args"]) if oks else None


def _models_in(annotation: Any) -> Iterator[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
    for arg in typing.get_args(annotation):
        yield from _models_in(arg)


def output_fields(model: type[BaseModel], prefix: str = "") -> list[str]:
    """Dotted paths of every field the tool can return, nested models included."""
    out: list[str] = []
    for fname, field in model.model_fields.items():
        out.append(prefix + fname)
        for sub in _models_in(field.annotation):
            out += output_fields(sub, prefix + fname + ".")
    return out


def truncation_flags(name: str) -> list[str]:
    return [f for f in output_fields(spec(name).output_model)
            if f.rsplit(".", 1)[-1] == "truncated" or f.endswith("_truncated")]


def paginates(name: str) -> bool:
    source = inspect.getsource(inspect.getmodule(spec(name).func))
    return ".paginate(" in source or 'links.get("next")' in source


def _named(word: str, text: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(word)}(?![A-Za-z0-9_])", text) is not None


def _true_somewhere(data: Any, key: str) -> bool:
    if isinstance(data, dict):
        return any((k == key and v is True) or _true_somewhere(v, key) for k, v in data.items())
    if isinstance(data, list):
        return any(_true_somewhere(v, key) for v in data)
    return False


def _client(token: str = FIXTURE_TOKEN, repo: str = FIXTURE_REPO, sleeps: list[float] | None = None) -> GitHubClient:
    return GitHubClient(token, repo, fail_fast_after=5.0, max_retries=2, write_interval=0, search_interval=0,
                        sleep=(sleeps if sleeps is not None else []).append)


def _seeded_fake(repo: str) -> FakeGitHub:
    fake = FakeGitHub(repo)
    fake.load_seed(load_seed(SEED_FILE))
    return fake


def _scoped(method: str, path: str, params: dict[str, Any], repo: str) -> bool:
    path = path.lstrip("/")
    if path.lower().startswith(f"repos/{repo.lower()}/"):
        return True
    q = str(params.get("q", ""))
    return method == "GET" and path == "search/issues" and q.lower().startswith(f"repo:{repo.lower()} ")


# -- checks ---------------------------------------------------------------------


def check_folder(name: str) -> set[str]:
    folder = spec(name).folder
    wanted = {"__init__.py": folder / "__init__.py", "tool.py": folder / "tool.py",
              "description.md": folder / "description.md", "eval_cases.yaml": folder / "eval_cases.yaml",
              f"tests/tools/test_{name}.py": ROOT / "tests" / "tools" / f"test_{name}.py"}
    missing = {label for label, path in wanted.items() if not path.is_file()}
    if not list((FIXTURES_DIR / name).glob("*.json")):
        missing.add(f"tests/fixtures/{name}/*.json")
    return missing


def check_inputs_described(name: str) -> set[str]:
    props = spec(name).input_schema().get("properties", {})
    desc = spec(name).description
    bad = {f"{k}: no schema description" for k, v in props.items() if not str(v.get("description", "")).strip()}
    return bad | {f"{k}: not named in description.md" for k in props if not _named(k, desc)}


def check_invalid_input(name: str) -> set[str]:
    args = example_args(name)
    if args is None:
        return {"no example arguments (needs an ok fixture)"}
    schema = spec(name).input_schema()
    props, required = schema.get("properties", {}), schema.get("required", [])
    cases: dict[str, tuple[dict[str, Any], str]] = {"unknown field": ({**args, "gate_unknown_field": 1},
                                                                      "gate_unknown_field")}
    for field in required:
        cases[f"missing {field}"] = ({k: v for k, v in args.items() if k != field}, field)
    for field in props:
        cases[f"wrong type for {field}"] = ({**args, field: {ECHO_MARKER: 1}}, field)
    failing = set()
    with respx.mock(base_url=API, assert_all_called=False) as router:
        sent = router.route().mock(return_value=httpx.Response(500))
        with _client() as client:
            for label, (bad_args, field) in cases.items():
                result = call_tool(ToolContext(client, FIXTURE_REPO), name, bad_args)
                error = result.get("error") or {}
                if result.get("ok") or error.get("type") != "invalid_input":
                    failing.add(f"{label}: not rejected as invalid_input")
                elif set(error) != ERROR_KEYS or error["retryable"] is not False:
                    failing.add(f"{label}: error payload is not {sorted(ERROR_KEYS)}, retryable false")
                elif field not in error["message"]:
                    failing.add(f"{label}: message does not name {field!r}")
                elif ECHO_MARKER in error["message"]:
                    failing.add(f"{label}: message echoes the input value")
        if sent.called:
            failing.add("a request was sent for invalid arguments")
    return failing


def check_replay_success(name: str) -> set[str]:
    sources = {"recorded"} if spec(name).read_only else {"recorded", "fake"}
    if not any(f.get("source") in sources for f in ok_fixtures(name)):
        return {f"no successful fixture recorded from {' or '.join(sorted(sources))}"}
    return set()


def _error_templates() -> dict[str, tuple[Callable[[], Any], str, bool]]:
    def response(tool: str, scenario: str) -> Callable[[], httpx.Response]:
        r = load_fixture(tool, scenario)["exchanges"][0]["response"]
        return lambda: httpx.Response(r["status"], headers=r["headers"], json=r["json"])

    def transport() -> Any:
        raise httpx.ConnectError("gate: connection refused")

    return {
        # template: (response factory, expected error type, expected retryable)
        "401 bad credentials": (response("list_issues", "list_auth_failed"), "auth_failed", False),
        "404 not found": (response("list_issues", "list_not_found"), "not_found", False),
        "403 forbidden": (response("synthetic", "forbidden_403"), "forbidden", False),
        "403 primary rate limit": (response("synthetic", "primary_rate_limited_403"), "rate_limited", True),
        "429 secondary rate limit": (response("synthetic", "search_rate_limited_429"), "rate_limited", True),
        "500 server error": (response("synthetic", "server_error_500"), "upstream_error", True),
        "connection error": (transport, "upstream_error", True),
    }


ERROR_TEMPLATES = sorted(_error_templates())


def check_replay_error(name: str, template: str) -> set[str]:
    """Every request of the tool's first ok call gets the template's error response."""
    args = example_args(name)
    if args is None:
        return {"no example arguments (needs an ok fixture)"}
    factory, expected_type, retryable = _error_templates()[template]
    sleeps: list[float] = []
    with respx.mock(base_url=API, assert_all_called=False) as router:
        router.route().mock(side_effect=lambda request: factory())
        with _client(sleeps=sleeps) as client:
            try:
                result = call_tool(ToolContext(client, FIXTURE_REPO, write_mode="dry_run"), name, args)
            except Exception as exc:  # noqa: BLE001 - an escaping exception is the failure
                return {f"{template}: raised {type(exc).__name__}"}
    error = result.get("error") or {}
    if result.get("ok") or error.get("type") != expected_type or error.get("retryable") is not retryable:
        return {f"{template}: got {error.get('type') or 'ok'} (retryable {error.get('retryable')})"}
    if sum(sleeps) > 5.0:
        return {f"{template}: blocked for {sum(sleeps):.0f}s instead of failing fast"}
    return set()


def check_replay_pagination(name: str) -> set[str]:
    if not paginates(name):
        return set()
    for f in fixtures_of(name):
        ex = f["exchanges"]
        if any('rel="next"' in (e["response"]["headers"].get("link") or "") for e in ex[:-1]):
            return set()
    return {"paginates, but no fixture spans two pages"}


def check_replay_truncation(name: str) -> set[str]:
    results = [f.get("result") for f in fixtures_of(name)]
    return {flag for flag in truncation_flags(name)
            if not any(_true_somewhere(r, flag.rsplit(".", 1)[-1]) for r in results)}


def check_fields_described(name: str) -> set[str]:
    desc = spec(name).description
    return {f for f in output_fields(spec(name).output_model) if not _named(f.rsplit(".", 1)[-1], desc)}


def check_no_scope_inputs(name: str) -> set[str]:
    return {k for k in spec(name).input_schema().get("properties", {}) if k.lower() in SCOPE_NAMES}


def _dynamic_call(args: dict[str, Any], name: str, *, write_mode: str,
                  client_repo: str = GATE_REPO) -> tuple[list[str], list[dict], dict]:
    """One call against a fresh seeded fake for GATE_REPO: (methods sent, write log, result)."""
    fake = _seeded_fake(GATE_REPO)
    with respx.mock(base_url=API, assert_all_called=False) as router:
        router.route().mock(side_effect=fake.handler)
        with _client(repo=client_repo) as client:
            ctx = ToolContext(client, GATE_REPO, write_mode=write_mode)  # type: ignore[arg-type]
            result = call_tool(ctx, name, args)
    return [m for m, _, _ in fake.calls], ctx.write_log, result


def _next_link(headers: dict[str, str]) -> tuple[str, dict[str, str]] | None:
    m = re.search(r'<([^>]+)>;\s*rel="next"', headers.get("link") or "")
    if not m:
        return None
    url = httpx.URL(m.group(1))
    return url.path, dict(url.params)


def check_requests_scoped(name: str) -> set[str]:
    failing = set()
    for f in fixtures_of(name):
        repo = FIXTURE_REPO + f.get("repo_suffix", "")
        previous_next = None
        for e in f["exchanges"]:
            r = e["request"]
            # GitHub's own next-page link (it may address the repo by id) continues a
            # request that was scoped; anything else must name the sandbox itself.
            continued = previous_next == (r["path"], r["params"])
            previous_next = _next_link(e["response"]["headers"])
            if not continued and not _scoped(r["method"], r["path"], r["params"], repo):
                failing.add(f"fixture {f['scenario']}: {r['method']} {r['path']} is not scoped to the sandbox")
    if example_args(name) is None:
        return failing | {"no example arguments (needs an ok fixture)"}
    fake_requests: list[tuple[str, str, dict]] = []
    fake = _seeded_fake(GATE_REPO)

    def capture(request: httpx.Request) -> httpx.Response:
        fake_requests.append((request.method, request.url.path, dict(request.url.params)))
        return fake.handler(request)

    with respx.mock(base_url=API, assert_all_called=False) as router:
        router.route().mock(side_effect=capture)
        with _client(repo=GATE_REPO) as client:
            for f in ok_fixtures(name):
                call_tool(ToolContext(client, GATE_REPO, write_mode="dry_run"), name, f["args"])
    if not fake_requests:
        failing.add("made no request against a second repository")
    for method, path, params in fake_requests:
        if not _scoped(method, path, params, GATE_REPO):
            failing.add(f"with the context set to another repository: {method} {path} is not scoped to it")
    return failing


def check_writes_sandboxed(name: str) -> set[str]:
    if example_args(name) is None:
        return {"no example arguments (needs an ok fixture)"}
    failing = set()
    wrote_any = False
    for f in ok_fixtures(name):
        if spec(name).read_only:
            methods, log, _ = _dynamic_call(f["args"], name, write_mode="live")
            if any(m != "GET" for m in methods) or log:
                failing.add("declared read-only, but wrote")
            continue
        # Dry run: only reads reach GitHub, and every logged write targets the sandbox.
        methods, log, _ = _dynamic_call(f["args"], name, write_mode="dry_run")
        if any(m != "GET" for m in methods):
            failing.add("dry run sent a write")
        if any(not str(w["path"]).lower().startswith(f"repos/{GATE_REPO}/") for w in log):
            failing.add("a logged write is outside the sandbox")
        if not log:
            continue  # e.g. the label was already present: nothing to write
        wrote_any = True
        # Live, with the client guarding a different sandbox: refused before sending.
        methods, _, result = _dynamic_call(f["args"], name, write_mode="live", client_repo=OTHER_REPO)
        if any(m != "GET" for m in methods):
            failing.add("a live write reached a repository other than the client's sandbox")
        if (result.get("error") or {}).get("type") != "forbidden":
            failing.add("a live write outside the client's sandbox was not refused as forbidden")
    if not spec(name).read_only and not wrote_any:
        failing.add("write tool: no ok fixture makes a write")
    return failing


CHECKS: dict[str, CheckFn] = {
    "folder": check_folder,
    "inputs_described": check_inputs_described,
    "invalid_input": check_invalid_input,
    "replay_success": check_replay_success,
    "replay_pagination": check_replay_pagination,
    "replay_truncation": check_replay_truncation,
    "fields_described": check_fields_described,
    "no_scope_inputs": check_no_scope_inputs,
    "requests_scoped": check_requests_scoped,
    "writes_sandboxed": check_writes_sandboxed,
}
for _template in ERROR_TEMPLATES:
    CHECKS[f"replay_error[{_template}]"] = (lambda t: lambda name: check_replay_error(name, t))(_template)


# -- known exceptions -------------------------------------------------------------


def load_exceptions() -> dict[str, Any]:
    return yaml.safe_load(EXCEPTIONS_FILE.read_text(encoding="utf-8"))


def excepted_items(exceptions: dict[str, Any], tool: str, check: str) -> set[str]:
    entry = (exceptions.get("exceptions") or {}).get(tool, {}).get(check)
    return set(entry["items"]) if entry else set()


def run_check(check: str, tool: str) -> set[str]:
    return CHECKS[check](tool)


def dump(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)
