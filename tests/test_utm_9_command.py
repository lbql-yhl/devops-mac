#!/usr/bin/env python3
"""Contract tests for the command-only UTM-9 CSR runner."""

from __future__ import annotations

import json
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from scripts import utm_9
from scripts import utm_9_guest


DUMMY_GUEST_VALUE = "9072"


@pytest.fixture(autouse=True)
def configured_guest_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUBMISSION_GUEST_PASSWORD", DUMMY_GUEST_VALUE)


def test_certtool_input_uses_rsa_2048_sha256_and_exact_subject() -> None:
    payload = utm_9_guest.build_certtool_input(
        key_label="utm-9-0123456789abcdef",
        challenge="utm-9-0123456789abcdef",
        common_name="Example User",
        email="account@example.test",
    )

    assert payload.decode("utf-8").splitlines() == [
        "utm-9-0123456789abcdef",
        "r",
        "2048",
        "y",
        "s",
        "2",
        "y",
        "utm-9-0123456789abcdef",
        "Example User",
        "",
        "",
        "",
        "",
        "account@example.test",
        "y",
    ]


def test_guest_payload_rejects_control_characters_and_wrong_path() -> None:
    valid = {
        "expected_user": "abcd",
        "csr_path": "/Users/example/Desktop/CertificateSigningRequest.certSigningRequest",
        "key_label": "utm-9-0123456789abcdef",
        "challenge": "utm-9-0123456789abcdef",
        "keychain_password": DUMMY_GUEST_VALUE,
        "common_name": "Example User",
        "email": "account@example.test",
    }
    assert utm_9_guest.validate_payload(valid, require_subject=True) == valid

    for field, value in (
        ("email", "account@example.test\nleak"),
        ("common_name", "Example\rUser"),
        ("csr_path", "/Users/example/Desktop/CertificateSigningRequest.certSigningRequest"),
    ):
        invalid = dict(valid)
        invalid[field] = value
        with pytest.raises(utm_9_guest.GuestError):
            utm_9_guest.validate_payload(invalid, require_subject=True)


def test_keychain_unlock_uses_password_prompt_without_password_in_argv() -> None:
    calls: list[tuple[list[str], str, bytes]] = []

    def fake_prompt(command: list[str], password: str, prompt: bytes) -> int:
        calls.append((command, password, prompt))
        return 0

    utm_9_guest.unlock_keychain(
        Path("/Users/example/Library/Keychains/login.keychain-db"),
        DUMMY_GUEST_VALUE,
        prompt_runner=fake_prompt,
    )

    assert len(calls) == 1
    command, password, prompt = calls[0]
    assert command == [
        "/usr/bin/security",
        "unlock-keychain",
        "/Users/example/Library/Keychains/login.keychain-db",
    ]
    assert password == DUMMY_GUEST_VALUE
    assert DUMMY_GUEST_VALUE not in command
    assert prompt == b"password to unlock"


def test_real_pty_password_prompt_supplies_password_without_argv(tmp_path: Path) -> None:
    helper = tmp_path / "prompt.py"
    helper.write_text(
        """import sys, termios
fd = sys.stdin.fileno()
settings = termios.tcgetattr(fd)
hidden = list(settings)
hidden[3] &= ~termios.ECHO
termios.tcsetattr(fd, termios.TCSANOW, hidden)
sys.stdout.write('password to unlock test: ')
sys.stdout.flush()
password = sys.stdin.readline().rstrip('\\r\\n')
termios.tcsetattr(fd, termios.TCSANOW, settings)
raise SystemExit(0 if password == '9072' else 7)
""",
        encoding="utf-8",
    )

    command = [sys.executable, str(helper)]
    assert utm_9_guest._pty_password_prompt(
        command, DUMMY_GUEST_VALUE, b"password to unlock"
    ) == 0
    assert DUMMY_GUEST_VALUE not in command


def test_keychain_application_label_parser_returns_exact_sha1() -> None:
    output = """
attributes:
    0x00000001 <blob>=\"utm-9-0123456789abcdef\"
    0x00000006 <blob>=0x282EF39BEF0D25ED12F10CC3959EBE7E2DF2BAE0
"""
    assert (
        utm_9_guest.parse_application_label(output)
        == "282ef39bef0d25ed12f10cc3959ebe7e2df2bae0"
    )
    with pytest.raises(utm_9_guest.GuestError):
        utm_9_guest.parse_application_label(output + output)


def test_desktop_snapshot_ignores_only_finder_ds_store(tmp_path: Path) -> None:
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    (desktop / ".localized").write_text("", encoding="utf-8")
    before = utm_9_guest.desktop_snapshot(desktop)
    (desktop / ".DS_Store").write_bytes(b"finder metadata")

    assert utm_9_guest.desktop_snapshot(desktop) == before


def test_exact_owned_run_requires_one_local_vm_match() -> None:
    payload = {
        "runs": [
            {
                "id": "run-123",
                "app_name": "ExampleApp",
                "vm_name": "abcd",
                "submission_data": {"host_machine": "current-host"},
            }
        ]
    }

    run = utm_9.exact_owned_run(
        payload,
        run_id="run-123",
        vm_name="abcd",
        local_host="current-host",
    )
    assert run["app_name"] == "ExampleApp"

    with pytest.raises(utm_9.UTM9Error, match="RUN_HOST_OWNERSHIP_MISMATCH"):
        utm_9.exact_owned_run(
            payload,
            run_id="run-123",
            vm_name="abcd",
            local_host="other-host",
        )


def test_independent_target_requires_bound_running_vm_and_exact_mac_ip(
    tmp_path: Path,
) -> None:
    target = object()
    calls: list[tuple[object, ...]] = []

    def bound_loader(database, images_dir, vm_name, app_name):
        calls.append((database, images_dir, vm_name, app_name))
        return target

    def status_loader(vm_name):
        calls.append(("status", vm_name))
        return "running"

    def ip_loader(value):
        calls.append(("ip", value))
        return "192.0.2.10"

    utm_9.verify_independent_test_target(
        app_name="SawSet",
        vm_name="qmis",
        vm_ip="192.0.2.10",
        database=tmp_path / "vm-inventory.sqlite3",
        images_dir=tmp_path / "images",
        bound_loader=bound_loader,
        status_loader=status_loader,
        ip_loader=ip_loader,
    )

    assert calls == [
        (tmp_path / "vm-inventory.sqlite3", tmp_path / "images", "qmis", "SawSet"),
        ("status", "qmis"),
        ("ip", target),
    ]


def test_independent_target_rejects_non_running_or_different_ip(tmp_path: Path) -> None:
    common = {
        "app_name": "SawSet",
        "vm_name": "qmis",
        "vm_ip": "192.0.2.10",
        "database": tmp_path / "vm-inventory.sqlite3",
        "images_dir": tmp_path / "images",
        "bound_loader": lambda *args: object(),
    }
    with pytest.raises(utm_9.UTM9Error, match="INDEPENDENT_VM_NOT_RUNNING"):
        utm_9.verify_independent_test_target(
            **common,
            status_loader=lambda _: "stopped",
            ip_loader=lambda _: "192.0.2.10",
        )
    with pytest.raises(utm_9.UTM9Error, match="INDEPENDENT_VM_IP_MISMATCH"):
        utm_9.verify_independent_test_target(
            **common,
            status_loader=lambda _: "started",
            ip_loader=lambda _: "192.0.2.11",
        )


def test_attempt_is_mode_600_idempotent_and_context_locked(tmp_path: Path) -> None:
    attempt_path = tmp_path / "runtime" / "attempts" / "run-123" / "utm-9.json"
    context = {
        "run_id": "run-123",
        "app_name": "ExampleApp",
        "vm_name": "abcd",
        "vm_ip": "192.0.2.10",
        "vm_user": "abcd",
        "csr_path": "/Users/example/Desktop/CertificateSigningRequest.certSigningRequest",
    }
    before = {"desktop_count": 3, "desktop_digest": "a" * 64}

    first = utm_9.prepare_attempt(attempt_path, context, before)
    second = utm_9.prepare_attempt(attempt_path, context, before)

    assert first["attempt_id"] == second["attempt_id"]
    assert first["status"] == "prepared"
    assert attempt_path.stat().st_mode & 0o777 == 0o600
    changed = dict(context)
    changed["vm_ip"] = "192.0.2.11"
    with pytest.raises(utm_9.UTM9Error, match="ATTEMPT_CONTEXT_MISMATCH=vm_ip"):
        utm_9.prepare_attempt(attempt_path, changed, before)


def test_sensitive_subject_values_are_stdin_only() -> None:
    attempt = {
        "attempt_id": "01234567-89ab-cdef-0123-456789abcdef",
        "key_label": "utm-9-0123456789abcdef",
        "challenge": "utm-9-0123456789abcdef",
        "csr_path": "/Users/example/Desktop/CertificateSigningRequest.certSigningRequest",
        "desktop_before": {"desktop_count": 3, "desktop_digest": "a" * 64},
    }
    payload = utm_9.build_create_payload(
        vm_user="abcd",
        email="account@example.test",
        common_name="Example User",
        attempt=attempt,
    )
    command = utm_9.remote_helper_command(
        vm_user="abcd",
        guest_dir="/Users/example/Downloads",
        mode="create",
    )

    decoded = json.loads(payload.decode("utf-8"))
    assert decoded["email"] == "account@example.test"
    assert decoded["common_name"] == "Example User"
    assert decoded["keychain_password"] == DUMMY_GUEST_VALUE
    joined = " ".join(command)
    assert "account@example.test" not in joined
    assert "Example User" not in joined
    assert DUMMY_GUEST_VALUE not in joined
    assert command[-2:] == ["--mode", "create"]


def test_managed_guest_helper_hash_drift_is_atomically_upgraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "utm_9_guest.py"
    source.write_text(
        f'{utm_9.GUEST_HELPER_MANAGED_MARKER}\nprint("managed")\n',
        encoding="utf-8",
    )
    digest = utm_9.hashlib.sha256(source.read_bytes()).hexdigest()
    old_digest = "a" * 64
    ssh_calls: list[object] = []
    ssh_results = iter(
        (
            subprocess.CompletedProcess([], 0, f"PRESENT {old_digest} abcd 700 1\n".encode(), b""),
            subprocess.CompletedProcess([], 0, b"UPDATED\n", b""),
            subprocess.CompletedProcess([], 0, f"PRESENT {digest} abcd 700 1\n".encode(), b""),
        )
    )
    scp_calls: list[list[str]] = []

    def fake_ssh(*args, **kwargs):
        ssh_calls.append(args[2])
        return next(ssh_results)

    def fake_scp(command, **kwargs):
        scp_calls.append(command)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(utm_9, "GUEST_HELPER_SOURCE", source)
    utm_9.install_guest_helper(
        "abcd",
        "192.0.2.10",
        "/Users/example/Downloads",
        ssh_runner=fake_ssh,
        scp_runner=fake_scp,
        token_factory=lambda: "stabletoken",
    )

    assert len(scp_calls) == 1
    assert scp_calls[0][-1].endswith("utm_9_guest.py.tmp-stabletoken")
    assert old_digest in str(ssh_calls[1])


def test_unmanaged_guest_helper_hash_drift_still_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "utm_9_guest.py"
    source.write_text(
        f'{utm_9.GUEST_HELPER_MANAGED_MARKER}\nprint("managed")\n',
        encoding="utf-8",
    )
    old_digest = "a" * 64
    monkeypatch.setattr(utm_9, "GUEST_HELPER_SOURCE", source)

    def fake_ssh(*args, **kwargs):
        return subprocess.CompletedProcess(
            [], 0, f"PRESENT {old_digest} abcd 700 0\n".encode(), b""
        )

    with pytest.raises(utm_9.UTM9Error, match="GUEST_HELPER_CONFLICT"):
        utm_9.install_guest_helper(
            "abcd",
            "192.0.2.10",
            "/Users/example/Downloads",
            ssh_runner=fake_ssh,
            scp_runner=lambda *args, **kwargs: pytest.fail("must not copy conflict"),
        )


def test_guest_helper_source_syntax_is_checked_before_remote_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "utm_9_guest.py"
    source.write_text(
        f"{utm_9.GUEST_HELPER_MANAGED_MARKER}\nthis is invalid ???\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(utm_9, "GUEST_HELPER_SOURCE", source)

    with pytest.raises(utm_9.UTM9Error, match="GUEST_HELPER_SOURCE_SYNTAX_INVALID"):
        utm_9.install_guest_helper(
            "abcd",
            "192.0.2.10",
            "/Users/example/Downloads",
            ssh_runner=lambda *args, **kwargs: pytest.fail("must validate locally first"),
        )

def test_guest_error_preserves_complete_stderr_with_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = subprocess.CompletedProcess(
        [],
        1,
        b"",
        b"UTM_9_GUEST=blocked:DESKTOP_NON_TARGET_DRIFT\nignored raw details\n",
    )
    monkeypatch.setattr(utm_9, "_run_ssh", lambda *args, **kwargs: result)

    with pytest.raises(
        utm_9.UTM9Error,
        match=(
            r"^GUEST_VERIFY_EXIT=1: "
            r"UTM_9_GUEST=blocked:DESKTOP_NON_TARGET_DRIFT"
            r" \| ignored raw details$"
        ),
    ) as error:
        utm_9.call_guest(
            vm_user="abcd",
            vm_ip="192.0.2.10",
            guest_dir="/Users/example/Downloads",
            mode="verify",
            payload=b"{}",
        )

    assert "ignored raw details" in str(error.value)


def test_canonical_skill_and_docs_use_only_the_command_entry() -> None:
    root = Path(__file__).resolve().parents[1]
    skill = (root / "skills" / "utm-key" / "SKILL.md").read_text(encoding="utf-8")
    docs = (root / "docs" / "utm-key.md").read_text(encoding="utf-8")
    canonical = skill + "\n" + docs
    for required in (
        "$PROJECT_ROOT/scripts/utm_9.py",
        "--run-id '<run-id>'",
        "--page-title '<page-title>'",
        "--vm-name '<vm-name>'",
        "--vm-ip '<vm-ip>'",
        "--vm-user '<vm-user>'",
        "不读取或解释脚本内容",
    ):
        assert required in skill, required
        assert required in docs, required
    for implementation_detail in (
        "/usr/bin/certtool",
        "CSR_PRIVATE_KEY=verified",
        "项目托管标记",
        "SQLite/bundle/UUID/MAC",
    ):
        assert implementation_detail not in canonical
    for stale in (
        'open -a "Keychain Access"',
        "Computer Use",
        "证书助理",
        "SUBMISSION_SSH_PRIVATE_KEY",
        "BatchMode=yes",
        "右键",
        "Paste",
    ):
        assert stale not in canonical, stale


def test_project_summaries_are_indexes_not_keychain_implementation_docs() -> None:
    root = Path(__file__).resolve().parents[1]
    summaries = (
        (root / "AGENTS.md").read_text(encoding="utf-8")
        + "\n"
        + (root / "README.md").read_text(encoding="utf-8")
    )
    assert "skills/<skill>/SKILL.md" in summaries
    assert "不读取或解释脚本内容" in summaries
    for implementation_detail in (
        "--page-title '<应用名>-<vm_name>'",
        "SQLite/bundle/UUID/MAC",
        "PTY",
        "项目托管标记",
    ):
        assert implementation_detail not in summaries


def test_host_runner_reads_notion_and_finishes_one_stable_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runs_file = tmp_path / "feishu-runs.json"
    runs_file.write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "id": "run-123",
                        "app_name": "ExampleApp",
                        "vm_name": "abcd",
                        "submission_data": {"host_machine": "current-host"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    class FakeNotion:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        def verify_parent(self, title: str) -> None:
            self.calls.append(("verify-parent", title))

        def read_field(self, title: str, heading: str, label: str) -> str:
            self.calls.append(("read-field", title, heading, label))
            return {
                "邮箱：": "account@example.test",
                "用户名：": "Example User",
            }[label]

    notion = FakeNotion()
    calls: list[tuple[str, dict[str, object]]] = []
    digest = "a" * 64
    verification = {
        "status": "verified",
        "csr_bytes": 685,
        "csr_sha256": "b" * 64,
        "key_application_label": "c" * 40,
        "desktop_count": 3,
        "desktop_digest": digest,
    }

    def fake_call_guest(**kwargs):
        mode = str(kwargs["mode"])
        payload = json.loads(bytes(kwargs["payload"]).decode("utf-8"))
        calls.append((mode, payload))
        if mode == "probe":
            return {
                "status": "probed",
                "identity_verified": True,
                "keychain_unlocked": True,
                "csr_exists": False,
                "key_exists": False,
                "desktop_count": 3,
                "desktop_digest": digest,
            }
        return dict(verification)

    monkeypatch.setattr(utm_9, "RUNS_FILE", runs_file)
    monkeypatch.setattr(utm_9, "ATTEMPT_ROOT", tmp_path / "attempts")
    monkeypatch.setenv("SUBMISSION_HOST_MACHINE", "current-host")
    monkeypatch.setattr(utm_9, "api_from_env", lambda: notion)
    monkeypatch.setattr(utm_9, "install_guest_helper", lambda *args: None)
    monkeypatch.setattr(utm_9, "call_guest", fake_call_guest)

    result = utm_9.run(
        Namespace(
            run_id="run-123",
            vm_name="abcd",
            vm_ip="192.0.2.10",
            vm_user="abcd",
        )
    )

    assert result == 0
    assert notion.calls == [
        ("verify-parent", "current-host"),
        ("read-field", "ExampleApp-abcd", "账号信息", "邮箱："),
        ("read-field", "ExampleApp-abcd", "账号信息", "用户名："),
    ]
    assert [mode for mode, _ in calls] == ["probe", "probe", "create", "verify", "verify"]
    create_payload = next(payload for mode, payload in calls if mode == "create")
    assert create_payload["email"] == "account@example.test"
    assert create_payload["common_name"] == "Example User"
    output = capsys.readouterr().out
    assert "account@example.test" not in output
    assert "Example User" not in output
    assert "CSR_PRIVATE_KEY=verified" in output
    assert "UTM_9=verified" in output
    marker = json.loads((tmp_path / "attempts" / "run-123" / "utm-9.json").read_text())
    assert marker["status"] == "complete"
    assert marker["create_invocations"] == 1
    assert marker["evidence"]["verification_reads"] == 2


def test_independent_page_mode_reads_exact_notion_page_without_run_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FakeNotion:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        def verify_parent(self, title: str) -> None:
            self.calls.append(("verify-parent", title))

        def read_field(self, title: str, heading: str, label: str) -> str:
            self.calls.append(("read-field", title, heading, label))
            return {"邮箱：": "account@example.test", "用户名：": "Example User"}[label]

    notion = FakeNotion()
    target_checks: list[dict[str, object]] = []
    digest = "a" * 64
    verification = {
        "status": "verified",
        "csr_bytes": 685,
        "csr_sha256": "b" * 64,
        "key_application_label": "c" * 40,
        "desktop_count": 3,
        "desktop_digest": digest,
    }

    def fake_call_guest(**kwargs):
        if kwargs["mode"] == "probe":
            return {
                "status": "probed",
                "identity_verified": True,
                "keychain_unlocked": True,
                "csr_exists": False,
                "key_exists": False,
                "desktop_count": 3,
                "desktop_digest": digest,
            }
        return dict(verification)

    monkeypatch.setattr(utm_9, "RUNS_FILE", tmp_path / "missing-runs.json")
    monkeypatch.setattr(utm_9, "ATTEMPT_ROOT", tmp_path / "attempts")
    monkeypatch.setenv("SUBMISSION_HOST_MACHINE", "current-host")
    monkeypatch.setattr(utm_9, "api_from_env", lambda: notion)
    monkeypatch.setattr(utm_9, "install_guest_helper", lambda *args: None)
    monkeypatch.setattr(utm_9, "call_guest", fake_call_guest)
    monkeypatch.setattr(
        utm_9,
        "verify_independent_test_target",
        lambda **kwargs: target_checks.append(kwargs),
    )

    result = utm_9.run(
        Namespace(
            run_id=None,
            page_title="SawSet-qmis",
            vm_name="qmis",
            vm_ip="192.0.2.10",
            vm_user="qmis",
        )
    )

    assert result == 0
    assert target_checks == [
        {
            "app_name": "SawSet",
            "vm_name": "qmis",
            "vm_ip": "192.0.2.10",
        }
    ]
    assert notion.calls == [
        ("verify-parent", "current-host"),
        ("read-field", "SawSet-qmis", "账号信息", "邮箱："),
        ("read-field", "SawSet-qmis", "账号信息", "用户名："),
    ]
    assert "UTM_9=verified" in capsys.readouterr().out


def test_read_only_recovery_runs_three_rounds_without_repeating_create() -> None:
    attempts: list[int] = []
    waits: list[int] = []

    def transient_read() -> str:
        attempts.append(len(attempts) + 1)
        if len(attempts) < 3:
            raise RuntimeError("transient")
        return "verified"

    result = utm_9.retry_read_only(
        transient_read,
        label="SSH_PROBE",
        sleeper=lambda seconds: waits.append(seconds),
    )

    assert result == "verified"
    assert attempts == [1, 2, 3]
    assert waits == [5, 10]


def test_read_only_recovery_preserves_last_safe_classification() -> None:
    def classified_failure() -> None:
        raise utm_9.UTM9Error("GUEST_VERIFY_DESKTOP_NON_TARGET_DRIFT")

    with pytest.raises(
        utm_9.UTM9Error,
        match=(
            "CSR_VERIFY_FIRST_RECOVERY_EXHAUSTED:"
            "GUEST_VERIFY_DESKTOP_NON_TARGET_DRIFT"
        ),
    ):
        utm_9.retry_read_only(
            classified_failure,
            label="CSR_VERIFY_FIRST",
            sleeper=lambda _: None,
        )
