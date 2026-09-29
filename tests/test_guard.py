import pytest

from whytypedtools_eval.sandbox.cli import run
from whytypedtools_eval.sandbox.guard import SandboxGuardError, assert_sandbox_repo, assert_write_allowed
from whytypedtools_eval.sandbox.seed import seed

SANDBOX = "me/sandbox"


def test_same_repo_passes_case_insensitively():
    assert_sandbox_repo("me/sandbox", SANDBOX)
    assert_sandbox_repo(" Me/SandBox ", SANDBOX)


@pytest.mark.parametrize("target", ["me/other", "other/sandbox", "me/sandbox2", "me/sand", "me/sandbox/x"])
def test_different_repo_rejected(target):
    with pytest.raises(SandboxGuardError):
        assert_sandbox_repo(target, SANDBOX)


@pytest.mark.parametrize(
    "path",
    [
        "repos/me/sandbox2/issues",  # prefix trap
        "/repos/me/sandbox-old/labels",
        "repos/me/other/issues",
        "repos/someone/sandbox/issues",
        "user/repos",
        "repos/me",
    ],
)
def test_write_to_other_path_rejected(path):
    with pytest.raises(SandboxGuardError):
        assert_write_allowed("POST", path, SANDBOX)


@pytest.mark.parametrize("path", ["repos/me/sandbox/issues", "/repos/ME/Sandbox/labels/bug"])
def test_write_to_sandbox_allowed(path):
    assert_write_allowed("PATCH", path, SANDBOX)


@pytest.mark.parametrize("method", ["DELETE", "PATCH", "PUT"])
def test_repo_level_writes_forbidden(method):
    with pytest.raises(SandboxGuardError, match="repo-level"):
        assert_write_allowed(method, "repos/me/sandbox", SANDBOX)


def test_reads_are_not_restricted():
    assert_write_allowed("GET", "repos/anyone/anything", SANDBOX)


def test_client_blocks_write_before_sending(client, mock_api):
    route = mock_api.route()
    with pytest.raises(SandboxGuardError):
        client.post("repos/me/sandbox2/issues", {"title": "x"})
    with pytest.raises(SandboxGuardError):
        client.delete("repos/me/sandbox")
    assert route.call_count == 0


def test_script_refuses_other_repo_without_network(settings, mock_api, capsys):
    route = mock_api.route()
    code = run(seed, "seed", ["--repo", "me/sandbox2"], settings=settings)
    assert code == 2
    assert "not SANDBOX_REPO" in capsys.readouterr().err
    assert route.call_count == 0
