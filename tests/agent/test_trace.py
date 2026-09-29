import re

import pytest

from whytypedtools_eval.agent.trace import TraceWriter, git_info, new_run_id, read_trace, sha256_json


def test_events_are_jsonl_with_timestamps(tmp_path):
    path = tmp_path / "t.jsonl"
    with TraceWriter(path) as trace:
        trace.write("run_start", task="x")
        trace.write("run_end", status="completed")
    events = read_trace(path)
    assert [e["event"] for e in events] == ["run_start", "run_end"]
    assert all(e["ts"].endswith("+00:00") for e in events)
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_lines_are_flushed_immediately(tmp_path):
    path = tmp_path / "t.jsonl"
    trace = TraceWriter(path)
    trace.write("run_start")
    assert read_trace(path)[0]["event"] == "run_start"
    trace.close()


def test_redacts_known_secrets_anywhere(tmp_path):
    path = tmp_path / "t.jsonl"
    with TraceWriter(path, secrets=["github_pat_abcdef123456", "co-key-987654321"]) as trace:
        trace.write("tool_call", arguments={"q": "github_pat_abcdef123456"}, result="x co-key-987654321 y")
    text = path.read_text(encoding="utf-8")
    assert "abcdef123456" not in text and "987654321" not in text
    assert text.count("[REDACTED]") == 2


def test_short_secrets_are_ignored(tmp_path):
    path = tmp_path / "t.jsonl"
    with TraceWriter(path, secrets=["", "abc"]) as trace:
        trace.write("x", text="abc")
    assert read_trace(path)[0]["text"] == "abc"


def test_never_overwrites_an_existing_trace(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text("old\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        TraceWriter(path)


def test_run_ids_are_sortable_and_unique():
    ids = {new_run_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{8}", i) for i in ids)


def test_schema_hash_ignores_key_order():
    assert sha256_json({"a": 1, "b": [1, 2]}) == sha256_json({"b": [1, 2], "a": 1})
    assert sha256_json({"a": 1}) != sha256_json({"a": 2})


def test_git_info_outside_repo(tmp_path):
    info = git_info(tmp_path)
    assert set(info) == {"git_commit", "git_dirty"}
