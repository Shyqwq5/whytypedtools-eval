"""Run eval tasks x configurations x repetitions, score them and write results/.

Sandbox handling (docs/design/eval-mvp.md):
- live: the sandbox is reset before the first run. After any run whose write
  log has an executed write, the sandbox is diffed against the seed (reset in
  dry-run mode); if it drifted, it is reset. Every tool write goes through
  ToolContext.write, so a run without logged writes cannot have changed it.
- dry_run: nothing is written. The sandbox must already be in the seed state.

Runs are ordered run -> task -> config so configurations see the same
conditions (rate limits, time of day) as closely as possible.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from whytypedtools_eval.agent.loop import DEFAULT_MAX_TOOL_CALLS, SystemPrompt, run_agent
from whytypedtools_eval.agent.model import ChatModel
from whytypedtools_eval.agent.trace import REDACTED, new_run_id, read_trace, sha256_json, sha256_text, utc_now
from whytypedtools_eval.evals.configs import CONFIGS, TYPED
from whytypedtools_eval.evals.effects import Effect
from whytypedtools_eval.evals.report import aggregate, to_markdown
from whytypedtools_eval.evals.scoring import BulkSpec, score_run
from whytypedtools_eval.evals.tasks import Task, prompt_for
from whytypedtools_eval.github import GitHubClient
from whytypedtools_eval.tools.base import WriteMode

# Placeholder for the real sandbox repo in committed results (as in fixtures).
PUBLIC_REPO = "sandbox-owner/whytypedtools-sandbox"


class EvalError(RuntimeError):
    """The eval cannot start or continue safely."""


class SandboxControl(Protocol):
    def keymap(self) -> dict[str, int]: ...

    def drift(self) -> list[str]:
        """Writes a reset would make now (empty = sandbox is in the seed state)."""
        ...

    def reset(self) -> list[str]:
        """Restore the seed state (live writes). Returns the writes made."""
        ...


def _sandbox_requests(sandbox: SandboxControl) -> int:
    """GitHub requests made by the sandbox control so far (0 if it doesn't count)."""
    return getattr(sandbox, "requests_made", 0)


@dataclass(frozen=True)
class EvalPlan:
    configs: list[str]
    tasks: list[Task]
    runs: int
    write_mode: WriteMode
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS
    task_set: str = ""
    task_set_sha256: str = ""
    # Rerun mode: run only these (config, task_id, run_index) and merge them with
    # `base_records` (the parent eval's other runs) into a new results folder.
    only: frozenset[tuple[str, str, int]] | None = None
    base_records: tuple[dict[str, Any], ...] = ()
    parent_eval: str | None = None


class _Sanitiser:
    """Redacts secrets and the real repo name from anything written to results/."""

    def __init__(self, secrets: list[str], repo: str) -> None:
        self.secrets = [s for s in secrets if len(s) >= 8]
        self.repo = re.compile(re.escape(repo), re.IGNORECASE)

    def __call__(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, REDACTED)
        return self.repo.sub(PUBLIC_REPO, text)


def run_eval(
    plan: EvalPlan,
    *,
    model: ChatModel,
    tool_client: GitHubClient,
    repo: str,
    sandbox: SandboxControl,
    results_dir: Path,
    trace_dir: Path,
    system_prompt: SystemPrompt | None = None,
    secrets: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    exposure: dict[str, str] | None = None,
    bulk_loader: Callable[[dict[str, int]], dict[str, BulkSpec]] | None = None,
    emit: Callable[[str], None] = print,
) -> Path:
    """Run the plan and return the results directory for this eval."""
    unknown = [c for c in plan.configs if c not in CONFIGS]
    if unknown:
        raise EvalError(f"unknown configuration(s): {', '.join(unknown)}")
    system_prompt = system_prompt or SystemPrompt.load()
    clean = _Sanitiser(secrets or [], repo)

    before = _sandbox_requests(sandbox)
    if plan.write_mode == "live":
        emit("resetting the sandbox to the seed state before the eval")
        sandbox.reset()
    else:
        pending = sandbox.drift()
        if pending:
            raise EvalError(
                f"sandbox is not in the seed state ({len(pending)} pending change(s)); "
                "run scripts/reset_sandbox.py first"
            )
    initial_sandbox_requests = _sandbox_requests(sandbox) - before
    keymap = sandbox.keymap()
    missing = sorted({k for t in plan.tasks for k in t.referenced_keys()} - set(keymap))
    if missing:
        raise EvalError(f"sandbox/state.json has no issue number for: {', '.join(missing)}")

    started = utc_now()
    eval_id = new_run_id(started)
    out = results_dir / eval_id
    out.mkdir(parents=True, exist_ok=False)
    # Bulk targets are issue numbers, so they are resolved once the keymap is known.
    bulk_specs = bulk_loader(keymap) if bulk_loader else {}
    tasks_by_id = {t.id: t for t in plan.tasks}
    # Re-score the parent's records with the current scoring, from their traces,
    # so the merged result is scored consistently.
    records = [
        _rescore(r, tasks_by_id[r["task_id"]], trace_dir, plan, keymap, bulk_specs) for r in plan.base_records
    ]
    combos = [
        (run_index, task, config)
        for run_index in range(plan.runs)
        for task in plan.tasks
        for config in plan.configs
        if plan.only is None or (config, task.id, run_index) in plan.only
    ]
    # Written before any run, so an interrupted eval can be resumed with --rerun-errors.
    (out / "plan.json").write_text(json.dumps({
        "eval_id": eval_id,
        "started_at": started.isoformat(timespec="seconds"),
        "configs": plan.configs,
        "runs": plan.runs,
        "task_set": plan.task_set,
        "task_set_sha256": plan.task_set_sha256,
        "task_ids": [t.id for t in plan.tasks],
        "write_mode": plan.write_mode,
        "max_tool_calls": plan.max_tool_calls,
        "parent_eval": plan.parent_eval,
    }, indent=2) + "\n", encoding="utf-8", newline="\n")
    aborted: str | None = None

    with (out / "runs.jsonl").open("x", encoding="utf-8", newline="\n") as runs_file:
        for record in records:
            runs_file.write(clean(json.dumps(record, ensure_ascii=False)) + "\n")
        for done, (run_index, task, config) in enumerate(combos, start=1):
            prompt = prompt_for(task, keymap)
            setup = CONFIGS[config](tool_client, repo, plan.write_mode, prompt)
            requests_before = tool_client.requests_made
            result = run_agent(
                prompt,
                model=model,
                call_tool=setup.call_tool,
                tools=setup.tools,
                system_prompt=system_prompt,
                max_tool_calls=plan.max_tool_calls,
                trace_dir=trace_dir / eval_id,
                secrets=secrets or [],
                metadata={
                    **(metadata or {}),
                    "eval_id": eval_id,
                    "config": config,
                    "task_id": task.id,
                    "run_index": run_index,
                    "write_mode": plan.write_mode,
                    **setup.metadata,
                },
            )
            record = score_run(
                task,
                events=read_trace(result.trace_path),
                write_log=setup.ctx.write_log,
                keymap=keymap,
                write_mode=plan.write_mode,
                typed=setup.typed,
                bulk=bulk_specs.get(task.id),
            )
            record.update(config=config, run_index=run_index, run_id=result.run_id,
                          trace=result.trace_path.name, drift=None,
                          github_requests=tool_client.requests_made - requests_before,
                          sandbox_requests=0, rerun_of=plan.parent_eval)

            if plan.write_mode == "live" and any(w["executed"] for w in setup.ctx.write_log):
                sb_before = _sandbox_requests(sandbox)
                drift = sandbox.drift()
                record["drift"] = len(drift)
                if drift:
                    emit(f"  sandbox drifted ({len(drift)} change(s)); resetting")
                    sandbox.reset()
                    keymap = sandbox.keymap()
                record["sandbox_requests"] = _sandbox_requests(sandbox) - sb_before

            records.append(record)
            runs_file.write(clean(json.dumps(record, ensure_ascii=False)) + "\n")
            runs_file.flush()
            if record.get("bulk"):
                verdict = "BULK WRITE" if record["bulk"]["labelled"] else "NO BULK WRITE"
            else:
                verdict = "PASS" if record["passed"] else "FAIL"
            emit(f"[{done}/{len(combos)}] {config} {task.id} run {run_index + 1}: {verdict} "
                 f"({record['safety']['outcome']}, {record['status']})")
            if (record.get("error_detail") or {}).get("fatal"):
                # Every later run would fail the same way (e.g. quota used up): stop here.
                aborted = record["error_detail"]["message"]
                emit(f"stopping: {aborted}")
                break

    meta = {
        **(metadata or {}),
        "eval_id": eval_id,
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": utc_now().isoformat(timespec="seconds"),
        "model": model.describe(),
        "configs": plan.configs,
        "runs": plan.runs,
        "tasks": len(plan.tasks),
        "task_set": plan.task_set,
        "task_set_sha256": plan.task_set_sha256,
        "task_ids": [t.id for t in plan.tasks],
        "tasks_sha256": sha256_json([t.model_dump() for t in plan.tasks]),
        "write_mode": plan.write_mode,
        "max_tool_calls": plan.max_tool_calls,
        "parent_eval": plan.parent_eval,
        "reruns": len(records) - len(plan.base_records) if plan.parent_eval else 0,
        "aborted": aborted,
        "planned_runs": len(combos) + len(plan.base_records),
        "system_prompt_sha256": sha256_text(system_prompt.text),
        "github_requests": {
            "tools": sum(r["github_requests"] for r in records),
            "sandbox_after_runs": sum(r["sandbox_requests"] for r in records),
            "sandbox_initial": initial_sandbox_requests,
        },
    }
    summary = aggregate(records, plan.tasks, exposure or {})
    (out / "summary.json").write_text(
        clean(json.dumps({"meta": meta, **summary}, indent=2, ensure_ascii=False)) + "\n",
        encoding="utf-8", newline="\n",
    )
    (out / "summary.md").write_text(clean(to_markdown(summary, meta)), encoding="utf-8", newline="\n")
    emit(f"results: {out}")
    if aborted:
        raise EvalError(f"eval stopped early: {aborted} Completed runs are saved in {out}; "
                        f"resume with --rerun-errors {eval_id}")
    return out


_CARRIED = ("config", "run_index", "run_id", "trace", "drift", "github_requests", "sandbox_requests", "rerun_of")


def _rescore(record: dict[str, Any], task: Task, trace_dir: Path, plan: EvalPlan,
             keymap: dict[str, int], bulk_specs: dict[str, BulkSpec]) -> dict[str, Any]:
    path = next((trace_dir / (plan.parent_eval or "")).rglob(record["trace"]), None)
    if path is None:
        raise EvalError(f"trace {record['trace']} of the parent eval is missing; cannot re-score it")
    fresh = score_run(
        task,
        events=read_trace(path),
        write_log=[],
        effects=[Effect(**e) for e in record["effects"]],
        keymap=keymap,
        write_mode=plan.write_mode,
        typed=record["config"] in TYPED,
        bulk=bulk_specs.get(task.id),
    )
    fresh.update({k: record.get(k) for k in _CARRIED})
    return fresh
