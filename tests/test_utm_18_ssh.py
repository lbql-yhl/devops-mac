from pathlib import Path
import importlib.util
import shlex

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "utm_18_ssh.py"


@pytest.fixture(autouse=True)
def configured_guest_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUBMISSION_GUEST_PASSWORD", "9072")


def _load_module():
    assert SCRIPT.is_file(), "utm-script password SSH wrapper is missing"
    spec = importlib.util.spec_from_file_location("utm_18_ssh_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_wrapper_uses_shared_configured_password_transport_only() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "scripts.ssh_password" in source
    for obsolete in (
        "SUBMISSION_SSH_PRIVATE_KEY",
        "SUBMISSION_SSH_PUBLIC_KEY",
        "BatchMode=yes",
        "IdentitiesOnly=yes",
        '"-i"',
        "sshpass",
    ):
        assert obsolete not in source, obsolete


def test_wrapper_builds_password_ssh_command_without_exposing_password(monkeypatch) -> None:
    module = _load_module()
    synthetic_value = "9072"
    monkeypatch.setenv("SUBMISSION_GUEST_PASSWORD", synthetic_value)
    command, env = module.build_invocation(
        "wsyy",
        "192.168.64.26",
        ["/usr/bin/id", "-un"],
    )
    joined = " ".join(command)
    assert command[0] == "/usr/bin/ssh"
    assert "wsyy@192.168.64.26" in command
    assert "BatchMode=no" in joined
    assert "PubkeyAuthentication=no" in joined
    assert "PreferredAuthentications=password,keyboard-interactive" in joined
    assert synthetic_value not in joined
    assert "SUBMISSION_GUEST_PASSWORD" not in env
    assert env["SUBMISSION_SSH_ASKPASS_SOCKET"]
    assert env["SUBMISSION_SSH_ASKPASS_CAPABILITY"]
    assert env["SSH_ASKPASS_REQUIRE"] == "force"


def test_wrapper_preserves_remote_argument_boundaries() -> None:
    module = _load_module()
    command, _ = module.build_invocation(
        "wsyy",
        "192.168.64.26",
        ["/bin/zsh", "-lc", "printf '%s\\n' 'hello world'"],
    )
    assert command[-1] == shlex.join(
        ["/bin/zsh", "-lc", "printf '%s\\n' 'hello world'"]
    )
