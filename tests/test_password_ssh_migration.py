from pathlib import Path
import subprocess
import sys

import pytest

from scripts import utm_post_clone_guest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def configured_guest_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUBMISSION_GUEST_PASSWORD", "9072")


def test_guest_ssh_failure_preserves_stdout_and_stderr(monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        raise subprocess.CalledProcessError(
            1,
            ["ssh"],
            output="remote stdout detail\n",
            stderr="remote stderr detail\n",
        )

    monkeypatch.setattr(utm_post_clone_guest.subprocess, "run", fail)

    with pytest.raises(utm_post_clone_guest.GuestAutomationError) as captured:
        utm_post_clone_guest._run(["ssh"])

    assert "remote stdout detail" in str(captured.value)
    assert "remote stderr detail" in str(captured.value)


def test_guest_ssh_scripts_share_configured_password_transport() -> None:
    for relative in (
        "scripts/utm_2_guest_identity.py",
        "scripts/utm_7_login.py",
        "scripts/utm_8_change_password.py",
        "scripts/utm_9.py",
        "scripts/utm_18_edge.py",
        "scripts/utm_18_ssh.py",
        "scripts/utm_21_clone.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "scripts.ssh_password" in source, relative
        assert "SSH_PRIVATE_KEY" not in source, relative
        assert "SUBMISSION_SSH_PRIVATE_KEY" not in source, relative
        assert "BatchMode=yes" not in source, relative
        assert '"-i"' not in source, relative
        assert "--ssh-key" not in source, relative


def test_preflight_no_longer_requires_ssh_keys_or_keygen() -> None:
    source = (ROOT / "scripts/preflight.py").read_text(encoding="utf-8")
    for obsolete in (
        "SSH_PRIVATE_KEY",
        "SSH_PUBLIC_KEY",
        "SUBMISSION_SSH_PRIVATE_KEY",
        "SUBMISSION_SSH_PUBLIC_KEY",
        "ssh_keygen",
    ):
        assert obsolete not in source


def test_project_configuration_no_longer_exposes_ssh_key_paths() -> None:
    obsolete = (
        "SSH_PRIVATE_KEY",
        "SSH_PUBLIC_KEY",
        "SUBMISSION_SSH_PRIVATE_KEY",
        "SUBMISSION_SSH_PUBLIC_KEY",
    )
    for relative in (
        "services/project_paths.py",
        ".env.example",
        "skills/_shared/AUTOMATION_CONTRACT.md",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        for value in obsolete:
            assert value not in source, f"{relative}: {value}"


def test_password_ssh_entrypoints_run_by_absolute_path() -> None:
    for relative in (
        "scripts/utm_post_clone.py",
        "scripts/utm_2_guest_identity.py",
        "scripts/utm_7_login.py",
        "scripts/utm_8_change_password.py",
        "scripts/utm_18_edge.py",
        "scripts/utm_18_ssh.py",
        "scripts/utm_21_clone.py",
    ):
        result = subprocess.run(
            [sys.executable, str((ROOT / relative).resolve()), "--help"],
            cwd="/tmp",
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, f"{relative}: {result.stderr}"
