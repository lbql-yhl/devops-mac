from pathlib import Path
import importlib.util
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "utm_18_edge.py"


def _load_module():
    assert SCRIPT.is_file(), "single UTM-18 Edge preparation entry is missing"
    spec = importlib.util.spec_from_file_location("utm_18_edge_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_single_entry_uses_password_ssh_and_never_starts_vm() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "scripts.ssh_password" in source
    assert '"status", vm_name' in source
    assert "utmctl start" not in source
    assert '"start", vm_name' not in source
    for obsolete in (
        "SUBMISSION_SSH_PRIVATE_KEY",
        "SUBMISSION_SSH_PUBLIC_KEY",
        "BatchMode=yes",
        "IdentitiesOnly=yes",
        '"-i"',
        "sshpass",
    ):
        assert obsolete not in source, obsolete


def test_edge_pid_probe_matches_only_the_main_edge_executable() -> None:
    module = _load_module()
    assert (
        "pgrep -f '^/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge "
        ".*--remote-debugging-port=9222'"
        in module.REMOTE_EDGE_SCRIPT
    )


def test_edge_listener_is_condition_polled_before_validation() -> None:
    module = _load_module()
    listener_section = module.REMOTE_EDGE_SCRIPT.split(
        'print -r -- "EDGE_CDP_PROCESS=verified"', 1
    )[1]
    assert "for attempt in {1..30}" in listener_section
    assert "lsof -nP -iTCP:9222 -sTCP:LISTEN -t 2>/dev/null || true" in listener_section
    assert (
        '[[ "$listener_count" == "1" && "$listener_pids" == "$edge_pid" '
        "&& \"$listener_names\" == 'n127.0.0.1:9222' ]]"
        in listener_section
    )


def test_prepare_edge_runs_status_then_one_password_ssh() -> None:
    module = _load_module()
    calls: list[tuple[list[str], dict]] = []

    def fake_run(command, **kwargs):
        calls.append((list(command), kwargs))
        if command[0].endswith("utmctl"):
            return subprocess.CompletedProcess(command, 0, "started\n", "")
        return subprocess.CompletedProcess(
            command,
            0,
            "\n".join(
                (
                    "SSH_TARGET=verified",
                    "EDGE_OLD_PROCESS=stopped",
                    "EDGE_CDP_PROCESS=verified",
                    "EDGE_CDP_PORT_9222=verified",
                    "EDGE_CDP_HTTP=verified",
                )
            )
            + "\n",
            "",
        )

    result = module.prepare_edge(
        "wsyy",
        "192.168.64.26",
        "wsyy",
        runner=fake_run,
        password_env={"SSH_ASKPASS_REQUIRE": "force"},
        utmctl="/opt/homebrew/bin/utmctl",
    )

    assert len(calls) == 2
    assert calls[0][0] == ["/opt/homebrew/bin/utmctl", "status", "wsyy"]
    ssh_command, ssh_kwargs = calls[1]
    assert ssh_command[0] == "/usr/bin/ssh"
    assert "wsyy@192.168.64.26" in ssh_command
    assert "PubkeyAuthentication=no" in " ".join(ssh_command)
    assert ssh_kwargs["input"].count("Microsoft Edge") >= 2
    assert "--remote-debugging-port=9222" in ssh_kwargs["input"]
    assert "http://127.0.0.1:9222/json/version" in ssh_kwargs["input"]
    assert result[-1] == "UTM_18_EDGE_PREPARE=verified"


def test_prepare_edge_rejects_non_running_vm_before_ssh() -> None:
    module = _load_module()
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, "stopped\n", "")

    with pytest.raises(module.UTM18EdgeError, match="VM_NOT_STARTED"):
        module.prepare_edge(
            "wsyy",
            "192.168.64.26",
            "wsyy",
            runner=fake_run,
            password_env={"SSH_ASKPASS_REQUIRE": "force"},
            utmctl="/opt/homebrew/bin/utmctl",
        )
    assert calls == [["/opt/homebrew/bin/utmctl", "status", "wsyy"]]
