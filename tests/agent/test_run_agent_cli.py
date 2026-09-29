import pytest

from tests.agent.scripted_model import ScriptedModel, answer, call, tools_turn
from whytypedtools_eval.agent.cli import main
from whytypedtools_eval.agent.trace import read_trace
from whytypedtools_eval.config import Settings

COHERE_KEY = "co-test-key-0123456789"


@pytest.fixture
def agent_settings():
    return Settings(  # type: ignore[call-arg]
        _env_file=None, github_token="github_pat_test123", sandbox_repo="me/sandbox", cohere_api_key=COHERE_KEY
    )


def run_cli(argv, settings, model, calls=None):
    calls = [] if calls is None else calls

    def caller_factory(_settings):
        def call_tool(name, args):
            calls.append((name, args))
            return {"ok": True, "result": {"issues": [], "returned": 0}}
        return call_tool

    return main(argv, settings=settings, model_factory=lambda s, a: model, tool_caller_factory=caller_factory)


def test_prints_answer_and_trace_path(tmp_path, agent_settings, capsys):
    model = ScriptedModel(tools_turn(call("list_issues", {})), answer("No open issues."))
    code = run_cli(["Any open issues?", "--trace-dir", str(tmp_path)], agent_settings, model)
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("No open issues.\n")
    assert "status: completed" in out
    (path,) = tmp_path.rglob("*.jsonl")
    assert f"trace: {path}" in out
    start = read_trace(path)[0]
    assert start["task"] == "Any open issues?"
    assert "git_commit" in start and "git_dirty" in start


def test_non_completed_status_exits_1(tmp_path, agent_settings, capsys):
    model = ScriptedModel(tools_turn(call("list_issues", {})), answer("gave up"))
    code = run_cli(["t", "--trace-dir", str(tmp_path), "--max-tool-calls", "0"], agent_settings, model)
    assert code == 1
    assert "status: max_tool_calls" in capsys.readouterr().out


def test_secrets_from_settings_are_redacted(tmp_path, agent_settings):
    model = ScriptedModel(answer(f"key is {COHERE_KEY}, token github_pat_test123"))
    run_cli([f"say {COHERE_KEY}", "--trace-dir", str(tmp_path)], agent_settings, model)
    (path,) = tmp_path.rglob("*.jsonl")
    text = path.read_text(encoding="utf-8")
    assert COHERE_KEY not in text and "github_pat_test123" not in text


def test_missing_cohere_key_is_a_clear_error(tmp_path, capsys):
    settings = Settings(_env_file=None, github_token="github_pat_test123", sandbox_repo="me/sandbox")  # type: ignore[call-arg]
    code = main(["t", "--trace-dir", str(tmp_path)], settings=settings)
    err = capsys.readouterr().err
    assert code == 2
    assert "COHERE_API_KEY is not set" in err
    assert not list(tmp_path.rglob("*.jsonl"))


def test_default_factory_passes_cli_options(agent_settings, monkeypatch):
    captured = {}

    def fake_from_settings(settings, **kwargs):
        captured.update(kwargs)
        raise SystemExit(0)

    monkeypatch.setattr("whytypedtools_eval.agent.cli.CohereModel.from_settings", fake_from_settings)
    with pytest.raises(SystemExit):
        main(["t", "--model", "command-x", "--temperature", "0.2", "--no-seed"], settings=agent_settings)
    assert captured == {"model": "command-x", "temperature": 0.2, "seed": None, "thinking": "enabled"}


def test_negative_budget_rejected(agent_settings, capsys):
    assert main(["t", "--max-tool-calls", "-1"], settings=agent_settings) == 2
