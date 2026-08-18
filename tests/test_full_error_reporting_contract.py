from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import clean_cli, utm_7_login, utm_8_change_password, utm_9


ROOT = Path(__file__).resolve().parents[1]

REMAINING_HOST_ENTRIES = (
    "utm_notion.py",
    "utm_clash_ip.py",
    "utm_7_login.py",
    "utm_8_change_password.py",
    "utm_9.py",
    "utm_apps.py",
    "utm_business.py",
    "utm_clone_and_initialize.py",
    "utm_vm_clone_steps.py",
)


def test_remaining_host_entries_preserve_complete_error_detail() -> None:
    for name in REMAINING_HOST_ENTRIES:
        source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        main_source = source[source.rindex("return run_clean_cli(") :]
        assert "preserve_error_detail=True" in main_source, name


def test_full_reason_keeps_nested_cause_messages_without_truncation() -> None:
    tail = "x" * 500
    try:
        try:
            raise ValueError(f"lowercase inner detail, punctuation (!): {tail}")
        except ValueError as error:
            raise RuntimeError("outer workflow failed") from error
    except RuntimeError as error:
        rendered = clean_cli.full_reason(error)

    assert rendered == (
        "outer workflow failed | caused by ValueError: "
        f"lowercase inner detail, punctuation (!): {tail}"
    )
    assert rendered.endswith(tail)


def test_edit_guest_failure_keeps_all_lines_and_only_redacts_password() -> None:
    tail = "z" * 500
    detail = utm_8_change_password._safe_guest_failure_detail(
        f"first diagnostic line\nsecond line password=secret-pass {tail}\n",
        "secret-pass",
    )
    assert detail == (
        "first diagnostic line | second line password=<redacted> " + tail
    )


def test_login_guest_failure_keeps_all_lines_without_length_limit() -> None:
    tail = "q" * 500
    detail = utm_7_login._redacted_process_detail(
        f"first login detail\nsecond detail for account@example.test {tail}\n",
        ("account@example.test",),
    )
    assert detail == "first login detail | second detail for <redacted> " + tail


def test_key_guest_failure_keeps_actual_stderr_and_redacts_configured_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synthetic_value = "9072"
    completed = SimpleNamespace(
        returncode=9,
        stdout=b"",
        stderr=(
            b"keychain helper failed while clicking Allow\n"
            + f"authorization password={synthetic_value} was rejected\n".encode()
        ),
    )
    monkeypatch.setenv("SUBMISSION_GUEST_PASSWORD", synthetic_value)
    monkeypatch.setattr(utm_9, "_run_ssh", lambda *_args, **_kwargs: completed)

    with pytest.raises(
        utm_9.UTM9Error,
        match=(
            r"^GUEST_CREATE_EXIT=9: keychain helper failed while clicking Allow"
            r" \| authorization password=<redacted> was rejected$"
        ),
    ):
        utm_9.call_guest(
            vm_user="abcd",
            vm_ip="192.0.2.10",
            guest_dir="/Users/example/Downloads",
            mode="create",
            payload=b"{}",
        )


def test_key_error_does_not_rewrite_unrelated_numeric_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = SimpleNamespace(
        returncode=7,
        stdout=b"",
        stderr=b"tool reported status 9876 without a password field\n",
    )
    monkeypatch.setenv("SUBMISSION_GUEST_PASSWORD", "9072")
    monkeypatch.setattr(utm_9, "_run_ssh", lambda *_args, **_kwargs: completed)
    with pytest.raises(utm_9.UTM9Error, match=r"status 9876"):
        utm_9.call_guest(
            vm_user="abcd",
            vm_ip="192.0.2.10",
            guest_dir="/Users/example/Downloads",
            mode="verify",
            payload=b"{}",
        )


def test_clash_privileged_failure_keeps_sanitized_actual_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from scripts import utm_clash_ip, utm_post_clone_guest

    runner = utm_clash_ip.Runner(
        vm_name="abcd",
        application_name="Demo",
        vm_ip="192.0.2.10",
        parent_title="Host",
        page_title="Demo-abcd",
        proxy_stdin=False,
        database=tmp_path / "inventory.sqlite3",
        images_dir=tmp_path / "images",
    )

    def fail(*_args, **_kwargs):
        raise utm_post_clone_guest.GuestAutomationError(
            "privileged guest command timed out after 45 seconds"
        ) from subprocess.TimeoutExpired(["ssh", "private script body"], 45)

    monkeypatch.setattr(utm_post_clone_guest, "ssh_sudo_script", fail)
    with pytest.raises(
        utm_clash_ip.ClashIPError,
        match=(
            r"^guest privileged command failed: "
            r"privileged guest command timed out after 45 seconds$"
        ),
    ) as captured:
        runner._run_sudo("private script body")

    assert captured.value.__cause__ is None
