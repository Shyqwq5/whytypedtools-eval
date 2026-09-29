"""Command-line entry point for scripts/run_eval.py."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from whytypedtools_eval.agent.cohere_model import CohereModel
from whytypedtools_eval.agent.loop import DEFAULT_MAX_TOOL_CALLS, DEFAULT_TRACE_DIR, PROJECT_ROOT
from whytypedtools_eval.agent.model import ChatModel
from whytypedtools_eval.agent.trace import git_info
from whytypedtools_eval.config import ConfigError, Settings, load_settings
from whytypedtools_eval.evals.configs import CONFIGS
from whytypedtools_eval.evals.estimate import estimate, estimate_reruns, format_estimate
from whytypedtools_eval.evals.runner import EvalError, EvalPlan, SandboxControl, run_eval
from whytypedtools_eval.evals.tasks import DEFAULT_TASKS, load_task_set
from whytypedtools_eval.github import GitHubClient, GitHubError
from whytypedtools_eval.sandbox.guard import SandboxGuardError
from whytypedtools_eval.sandbox.models import load_seed, load_state, save_state
from whytypedtools_eval.sandbox.ops import Sandbox
from whytypedtools_eval.sandbox.reset import reset
from whytypedtools_eval.sandbox.search_sync import SearchIndexTimeout, wait_for_search_index
from whytypedtools_eval.tools.base import TOOL_FAIL_FAST_AFTER_S, TOOL_MAX_RETRIES

SEED_FILE = PROJECT_ROOT / "sandbox" / "seed_data.yaml"
STATE_FILE = PROJECT_ROOT / "sandbox" / "state.json"
RESULTS_DIR = PROJECT_ROOT / "results"


class GitHubSandbox:
    """SandboxControl over the real sandbox, using the seed/reset code."""

    def __init__(self, settings: Settings, *, emit: Callable[[str], None] = print) -> None:
        self.repo = settings.sandbox_repo
        # Patient client, like the reset script.
        self.client = GitHubClient(settings.github_token.get_secret_value(), self.repo)
        self.seed = load_seed(SEED_FILE)
        self.emit = emit

    @property
    def requests_made(self) -> int:
        return self.client.requests_made

    def keymap(self) -> dict[str, int]:
        return dict(load_state(STATE_FILE, self.repo).issues)

    def drift(self) -> list[str]:
        sb = Sandbox(self.client, self.repo, dry_run=True, emit=lambda _: None)
        reset(sb, self.seed, load_state(STATE_FILE, self.repo))
        return sb.writes

    def reset(self) -> list[str]:
        sb = Sandbox(self.client, self.repo, dry_run=False, emit=self.emit)
        state = reset(sb, self.seed, load_state(STATE_FILE, self.repo))
        save_state(STATE_FILE, state)
        wait_for_search_index(self.client, self.repo, emit=self.emit)
        return sb.writes


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run the eval tasks against the sandbox and write results/.")
    p.add_argument("--configs", nargs="+", default=["tool_e"], choices=sorted(CONFIGS))
    p.add_argument("--runs", type=int, default=3, help="Repetitions per task (default 3).")
    p.add_argument("--tasks", nargs="+", metavar="ID", help="Only these task ids.")
    p.add_argument("--categories", nargs="+", choices=["functional", "tool_selection", "dangerous", "injection"])
    p.add_argument("--live", action="store_true",
                   help="Let write tools change the sandbox. Resets the sandbox first and after any run that wrote.")
    p.add_argument("--estimate", action="store_true", help="Only print the expected cost; call no API.")
    p.add_argument("--max-tool-calls", type=int, default=DEFAULT_MAX_TOOL_CALLS)
    p.add_argument("--tasks-file", type=Path, default=DEFAULT_TASKS)
    p.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    p.add_argument("--trace-dir", type=Path, default=DEFAULT_TRACE_DIR)
    p.add_argument("--max-model-rpm", type=float, metavar="N",
                   help="Pace Cohere requests to at most N per minute (client side).")
    p.add_argument("--rerun-errors", metavar="EVAL_ID",
                   help="Re-run only the errored runs of an earlier eval (same task set and mode) and "
                        "write a merged result to a new folder.")
    return p


def _cohere(settings: Settings, min_interval_s: float) -> ChatModel:
    return CohereModel.from_settings(settings, min_interval_s=min_interval_s)


def _load_parent(results_dir: Path, eval_id: str) -> tuple[dict, list[dict]]:
    folder = results_dir / eval_id
    meta = json.loads((folder / "summary.json").read_text(encoding="utf-8"))["meta"]
    records = [json.loads(line) for line in (folder / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    return meta, records


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    model_factory: Callable[[Settings, float], ChatModel] = _cohere,
    sandbox_factory: Callable[[Settings], SandboxControl] = GitHubSandbox,
    client_factory: Callable[[Settings], GitHubClient] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if args.runs < 1 or args.max_tool_calls < 0:
        print("error: --runs must be >= 1 and --max-tool-calls >= 0", file=sys.stderr)
        return 2
    task_set = load_task_set(args.tasks_file)
    tasks = task_set.tasks
    if args.tasks:
        unknown = set(args.tasks) - {t.id for t in tasks}
        if unknown:
            print(f"error: unknown task id(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        tasks = [t for t in tasks if t.id in args.tasks]
    if args.categories:
        tasks = [t for t in tasks if t.category in args.categories]
    if not tasks:
        print("error: no tasks selected", file=sys.stderr)
        return 2

    mode = "live" if args.live else "dry_run"
    rerun: dict = {}
    if args.rerun_errors:
        try:
            meta, parent = _load_parent(args.results_dir, args.rerun_errors)
        except OSError as exc:
            print(f"error: cannot read eval {args.rerun_errors}: {exc}", file=sys.stderr)
            return 2
        if meta.get("task_set_sha256") != task_set.sha256 or meta.get("write_mode") != mode:
            print("error: the parent eval used a different task set or write mode", file=sys.stderr)
            return 2
        errors = [r for r in parent if r["error"]]
        if not errors:
            print(f"eval {args.rerun_errors} has no errored runs; nothing to do")
            return 0
        ids = set(meta["task_ids"])
        tasks = [t for t in task_set.tasks if t.id in ids]
        args.configs, args.runs = meta["configs"], meta["runs"]
        rerun = {
            "only": frozenset((r["config"], r["task_id"], r["run_index"]) for r in errors),
            "base_records": tuple(r for r in parent if not r["error"]),
            "parent_eval": args.rerun_errors,
            "errors": errors,
        }

    if args.estimate:
        if rerun:
            print(f"plan: re-run {len(rerun['errors'])} errored run(s) of {rerun['parent_eval']}; mode {mode}")
            print(format_estimate(estimate_reruns(rerun["errors"], list(rerun["base_records"]))))
            return 0
        print(f"plan: task set {task_set.name}, {len(tasks)} task(s) x {args.runs} run(s) x {', '.join(args.configs)}; "
              f"mode {'live' if args.live else 'dry-run'}")
        print(format_estimate(estimate(tasks, args.configs, args.runs, args.results_dir)))
        return 0

    try:
        settings = settings or load_settings(PROJECT_ROOT / ".env")
        model = model_factory(settings, 60.0 / args.max_model_rpm if args.max_model_rpm else 0.0)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    client = (client_factory or (lambda s: GitHubClient(
        s.github_token.get_secret_value(), s.sandbox_repo,
        fail_fast_after=TOOL_FAIL_FAST_AFTER_S, max_retries=TOOL_MAX_RETRIES,
    )))(settings)
    secrets = [settings.github_token.get_secret_value()]
    if settings.cohere_api_key is not None:
        secrets.append(settings.cohere_api_key.get_secret_value())
    seed = load_seed(SEED_FILE)
    exposure = {i.key: i.safety_test.exposure for i in seed.issues if i.safety_test}

    plan = EvalPlan(args.configs, tasks, args.runs, mode, args.max_tool_calls,
                    task_set=task_set.name, task_set_sha256=task_set.sha256,
                    only=rerun.get("only"), base_records=rerun.get("base_records", ()),
                    parent_eval=rerun.get("parent_eval"))
    try:
        run_eval(
            plan,
            model=model,
            tool_client=client,
            repo=settings.sandbox_repo,
            sandbox=sandbox_factory(settings),
            results_dir=args.results_dir,
            trace_dir=args.trace_dir,
            secrets=secrets,
            metadata=git_info(PROJECT_ROOT),
            exposure=exposure,
        )
    except (EvalError, SearchIndexTimeout, GitHubError, SandboxGuardError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
