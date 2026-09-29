import pytest

from whytypedtools_eval.config import ConfigError, load_settings


def test_missing_vars_give_clear_message():
    with pytest.raises(ConfigError) as exc:
        load_settings(env_file=None)
    msg = str(exc.value)
    assert "GITHUB_TOKEN: is not set" in msg
    assert "SANDBOX_REPO: is not set" in msg
    assert ".env.example" in msg


def test_placeholder_token_rejected_without_echoing_it(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_xxx")
    monkeypatch.setenv("SANDBOX_REPO", "me/sandbox")
    with pytest.raises(ConfigError) as exc:
        load_settings(env_file=None)
    assert "GITHUB_TOKEN" in str(exc.value)
    assert "github_pat_xxx" not in str(exc.value)


def test_bad_repo_format(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_real")
    monkeypatch.setenv("SANDBOX_REPO", "not-a-repo")
    with pytest.raises(ConfigError, match="SANDBOX_REPO"):
        load_settings(env_file=None)


def test_valid_settings_hide_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_secret123")
    monkeypatch.setenv("SANDBOX_REPO", "me/sandbox")
    s = load_settings(env_file=None)
    assert s.sandbox_repo == "me/sandbox"
    assert s.github_token.get_secret_value() == "github_pat_secret123"
    assert "secret123" not in repr(s)


def test_cohere_key_is_optional_for_sandbox_scripts(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_real")
    monkeypatch.setenv("SANDBOX_REPO", "me/sandbox")
    monkeypatch.setenv("COHERE_API_KEY", "  ")
    s = load_settings(env_file=None)
    assert s.cohere_api_key is None
    assert s.cohere_model == "command-a-plus-05-2026"
    with pytest.raises(ConfigError, match="COHERE_API_KEY is not set"):
        s.require_cohere_key()


def test_cohere_settings_hide_key(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_real")
    monkeypatch.setenv("SANDBOX_REPO", "me/sandbox")
    monkeypatch.setenv("COHERE_API_KEY", "co-secret-456789")
    monkeypatch.setenv("COHERE_MODEL", "command-other")
    s = load_settings(env_file=None)
    assert s.require_cohere_key().get_secret_value() == "co-secret-456789"
    assert s.cohere_model == "command-other"
    assert "456789" not in repr(s)
