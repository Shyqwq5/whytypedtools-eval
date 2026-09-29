"""Per-run JSONL traces for failure analysis and before/after comparisons.

One file per run, one JSON event per line, flushed as it happens so a crashed
run still leaves a readable prefix. Event types: run_start, model_call,
tool_call, run_end (format documented in README "Agent traces").

Known secret values are replaced with "[REDACTED]" before anything is written.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TRACE_VERSION = 1
REDACTED = "[REDACTED]"
# Shorter "secrets" would redact ordinary text; real keys are much longer.
MIN_SECRET_LEN = 8


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_run_id(now: datetime | None = None) -> str:
    """Sortable and unique: 20260929T211500Z-1a2b3c4d."""
    now = now or utc_now()
    return f"{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"


def git_info(repo_root: Path) -> dict[str, Any]:
    """Commit and dirty flag of the code that produced the run; None if unavailable."""

    def git(*args: str) -> str | None:
        try:
            out = subprocess.run(
                ["git", *args], cwd=repo_root, capture_output=True, text=True, timeout=10, check=True
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip()

    commit = git("rev-parse", "--short", "HEAD")
    status = git("status", "--porcelain")
    return {"git_commit": commit, "git_dirty": None if status is None else bool(status)}


class TraceWriter:
    def __init__(self, path: Path, *, secrets: list[str] | None = None) -> None:
        self.path = path
        self._secrets = sorted({s for s in (secrets or []) if len(s) >= MIN_SECRET_LEN}, key=len, reverse=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("x", encoding="utf-8", newline="\n")

    @classmethod
    def for_run(cls, trace_dir: Path, run_id: str, started_at: datetime, **kwargs: Any) -> TraceWriter:
        return cls(trace_dir / f"{started_at:%Y-%m-%d}" / f"{run_id}.jsonl", **kwargs)

    def write(self, event: str, **fields: Any) -> None:
        record = {"event": event, "ts": utc_now().isoformat(timespec="milliseconds"), **fields}
        line = json.dumps(record, ensure_ascii=False, default=str)
        for secret in self._secrets:
            line = line.replace(secret, REDACTED)
        self._fh.write(line + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> TraceWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_trace(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
