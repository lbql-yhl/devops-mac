from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts import utm_18
from scripts import apple_web_workflow
from scripts.utm_18_attempt import Attempt, AttemptResult


class FakeNotion:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fields = {
            "邮箱：": "account@example.test",
            "修改后的密码：": "changed-secret",
            "初始密码：": "initial-secret",
            "电话：": "+1 555 0100",
            "电话短信接收平台：": "https://sms.example.test/latest",
            "应用名: ": "Demo",
        }

    def verify_parent(self, title: str) -> str:
        self.calls.append(("verify-parent", title))
        return title

    def read_field(self, title: str, heading: str, label: str) -> str:
        self.calls.append(("read-field", label))
        return self.fields[label]


class _VisiblePassword:
    def is_displayed(self) -> bool:
        return True

    def is_enabled(self) -> bool:
        return True

    def get_attribute(self, name: str):
        return None


class _VisibleButton(_VisiblePassword):
    def __init__(self, text: str, *, enabled: bool = True) -> None:
        self.text = text
        self.enabled = enabled

    def is_enabled(self) -> bool:
        return self.enabled

    def get_attribute(self, name: str):
        if name == "value":
            return ""
        return super().get_attribute(name)


class _TrustButton(_VisibleButton):
    def __init__(self, driver) -> None:
        super().__init__("Trust")
        self.driver = driver

    def click(self) -> None:
        self.driver.current_url = "https://developer.apple.com/account/"


class _OtpCompletionDriver:
    def __init__(self) -> None:
        self.current_url = "https://idmsa.apple.com/IDMSWebAuth/signin"
        self.switch_to = self
        self.in_frame = False

    def default_content(self) -> None:
        self.in_frame = False

    def frame(self, context) -> None:
        self.in_frame = True

    def find_elements(self, by: str, selector: str):
        if selector == "iframe,frame":
            return [object()]
        if (
            selector == "button"
            and self.in_frame
            and self.current_url.startswith("https://idmsa")
        ):
            return [_TrustButton(self)]
        return []


def test_otp_completion_clicks_unique_trust_and_reaches_account() -> None:
    driver = _OtpCompletionDriver()
    apple_web_workflow._complete_otp_trust_with_selenium(
        driver,
        sleeper=lambda seconds: None,
        max_polls=3,
    )
    assert driver.current_url == "https://developer.apple.com/account/"


class _StaleOtpField:
    def get_attribute(self, name: str):
        raise RuntimeError("stale element reference")


class _NavigatedOtpDriver:
    current_url = "https://developer.apple.com/account/"


def test_otp_readback_accepts_account_navigation_before_reading_stale_fields() -> None:
    assert apple_web_workflow._otp_readback_or_account(
        _NavigatedOtpDriver(),
        [_StaleOtpField()] * 6,
        "123456",
    )


class _EmailContinueDriver:
    def __init__(self) -> None:
        self.polls = 0

    def find_elements(self, by: str, selector: str):
        assert by == "css selector"
        assert selector == 'button,input[type="submit"]'
        self.polls += 1
        return [_VisibleButton("Continue", enabled=self.polls >= 3)]


def test_email_continue_waits_until_the_unique_action_is_enabled() -> None:
    driver = _EmailContinueDriver()
    switches: list[int] = []
    sleeps: list[float] = []

    button = apple_web_workflow._wait_for_unique_email_continue(
        driver,
        context_index=1,
        switch_context=switches.append,
        sleeper=sleeps.append,
        max_polls=5,
    )

    assert button.text == "Continue"
    assert driver.polls == 3
    assert switches == [1, 1, 1]
    assert sleeps == [0.05, 0.05]


class _PasswordHandleDriver:
    def __init__(self) -> None:
        self.current_handle = ""
        self.in_frame = False
        self.switch_to = self

    def window(self, handle: str) -> None:
        self.current_handle = handle
        self.in_frame = False

    def default_content(self) -> None:
        self.in_frame = False

    def frame(self, context) -> None:
        self.in_frame = True

    def find_elements(self, by: str, selector: str):
        if selector == "iframe,frame":
            return [object()]
        if selector == 'input[type="password"]' and self.in_frame:
            return [_VisiblePassword()] if self.current_handle == "password" else []
        return []


def test_password_login_selects_the_only_handle_with_a_visible_password() -> None:
    driver = _PasswordHandleDriver()
    assert apple_web_workflow._apple_login_handles_with_password(
        driver, ["stale", "password"]
    ) == ["password"]


class _EmailHandleDriver(_PasswordHandleDriver):
    @property
    def window_handles(self):
        return ["stale", "target"]

    @property
    def current_url(self) -> str:
        return "https://idmsa.apple.com/IDMSWebAuth/signin"

    @property
    def current_window_handle(self) -> str:
        return "target"

    def execute_script(self, script: str):
        if "PROBE_KIND_EMAIL" in script:
            return {
                "status": "probed",
                "candidate_count": (
                    1 if self.current_handle == "target" and self.in_frame else 0
                ),
                "password_count": 0,
            }
        raise AssertionError(script)


def test_email_login_selects_the_only_handle_with_a_visible_email() -> None:
    driver = _EmailHandleDriver()
    assert apple_web_workflow._apple_login_handles_with_email(
        driver, ["stale", "target"]
    ) == ["target"]


def test_duplicate_login_pages_prefer_the_current_active_handle() -> None:
    driver = _EmailHandleDriver()
    assert apple_web_workflow._prefer_current_handle(
        driver, ["stale", "target"]
    ) == ["target"]


class _StaleAXBackend:
    def __init__(self) -> None:
        self.waits = 0

    def wait_for_text(self, anchors, timeout=90):
        self.waits += 1
        if self.waits == 1:
            return "Password"
        if self.waits == 2:
            return "Password"
        return "Membership details"

    def has_text(self, text: str) -> bool:
        return False


def test_stale_ax_password_state_falls_back_to_current_email_form() -> None:
    backend = _StaleAXBackend()
    calls: list[str] = []

    def email_submitter(payload) -> None:
        calls.append("email")

    def password_submitter(payload) -> None:
        calls.append("password")
        if calls == ["password"]:
            raise apple_web_workflow.AppleWebWorkflowError(
                "SELENIUM_APPLE_PASSWORD_PAGE_COUNT=0"
            )

    apple_web_workflow._ensure_apple_login(
        backend,
        {},
        email_submitter=email_submitter,
        password_submitter=password_submitter,
    )
    assert calls == ["password", "email", "password"]


class _PhoneAXBackend:
    def __init__(self) -> None:
        self.states = iter(
            ["Password", "Choose a phone number", "verification code", "Membership details"]
        )
        self.fields: list[tuple[tuple[str, ...], str]] = []

    def wait_for_text(self, anchors, timeout=90):
        return next(self.states)

    def has_text(self, text: str) -> bool:
        return False

    def set_field(self, labels, value: str) -> None:
        self.fields.append((tuple(labels), value))


class _PhoneAXTimeoutBackend(_PhoneAXBackend):
    def __init__(self) -> None:
        self.calls = 0
        self.fields = []

    def wait_for_text(self, anchors, timeout=90):
        self.calls += 1
        if self.calls == 1:
            return "Password"
        if self.calls == 2:
            raise RuntimeError(
                "PAGE_TEXT_TIMEOUT=Choose a phone number|verification code|Membership details"
            )
        if self.calls == 3:
            raise RuntimeError(
                "PAGE_TEXT_TIMEOUT=verification code|Verify Your Identity|Membership details"
            )
        return "Membership details"


def test_login_matches_phone_before_fetching_and_entering_otp(monkeypatch) -> None:
    backend = _PhoneAXBackend()
    calls: list[str] = []
    monkeypatch.setattr(apple_web_workflow, "_fetch_otp", lambda url: "123456")

    apple_web_workflow._ensure_apple_login(
        backend,
        {
            "APPLE_ACCOUNT_PHONE": "+1 555 0160",
            "APPLE_ACCOUNT_SMS_URL": "https://sms.example.test/?code=x",
        },
        password_submitter=lambda payload: calls.append("password"),
        phone_submitter=lambda payload: calls.append("phone"),
        otp_submitter=lambda payload: calls.append("otp"),
    )

    assert calls == ["password", "phone", "otp"]
    assert backend.fields == []


def test_login_uses_dom_phone_match_when_ax_cannot_read_phone_page(monkeypatch) -> None:
    backend = _PhoneAXTimeoutBackend()
    calls: list[str] = []
    monkeypatch.setattr(apple_web_workflow, "_fetch_otp", lambda url: "123456")

    apple_web_workflow._ensure_apple_login(
        backend,
        {
            "APPLE_ACCOUNT_PHONE": "+1 555 0160",
            "APPLE_ACCOUNT_SMS_URL": "https://sms.example.test/?code=x",
        },
        password_submitter=lambda payload: calls.append("password"),
        phone_submitter=lambda payload: calls.append("phone"),
        otp_submitter=lambda payload: calls.append("otp"),
    )

    assert calls == ["password", "phone", "otp"]
    assert backend.fields == []


def test_fetch_otp_selects_the_last_six_digit_code_without_copy_or_date_rules() -> None:
    body = json.dumps(
        {
            "messages": [
                {
                    "id": "10",
                    "body": "验证码：123456。",
                },
                {
                    "id": "11",
                    "body": "654321",
                },
            ]
        }
    ).encode()

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return body

    with patch.object(
        apple_web_workflow.urllib.request,
        "urlopen",
        return_value=_Response(),
    ):
        assert (
            apple_web_workflow._fetch_otp(
                "https://api1997.com/smsrecord?token=access-token"
            )
            == "654321"
        )


def test_password_submit_accepts_current_sign_in_label_as_final_action() -> None:
    labels = ["Sign In", "Sign in with Passkey"]
    assert apple_web_workflow._password_final_action_candidates(
        [_VisibleButton(label) for label in labels]
    )[0].text == "Sign In"


def test_password_submit_allows_disabled_unique_action_before_password_fill() -> None:
    action = _VisibleButton("Sign In", enabled=False)
    assert apple_web_workflow._password_final_action_candidates(
        [action], require_enabled=False
    ) == [action]
    assert apple_web_workflow._password_final_action_candidates(
        [action], require_enabled=True
    ) == []


def test_password_submit_returns_to_root_before_reloading_frames() -> None:
    source = Path(apple_web_workflow.__file__).read_text(encoding="utf-8")
    segment = source.split("password_element.click()", 1)[1].split(
        "password_candidates = []", 1
    )[0]
    assert "driver.switch_to.default_content()" in segment


def test_password_submit_checks_disabled_action_twice_before_fill() -> None:
    source = Path(apple_web_workflow.__file__).read_text(encoding="utf-8")
    function = source.split("def _submit_password_with_selenium(", 1)[1].split(
        "def _ensure_apple_login(", 1
    )[0]
    before_fill = function.split("password_element.send_keys(password)", 1)[0]
    assert before_fill.count(
        "find_continue_candidates(require_enabled=False)"
    ) == 2


def _args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        run_id="run-12345678",
        parent_title="Host",
        page_title="Demo-abcd",
        vm_name="abcd",
        vm_ip="192.168.64.20",
        vm_user="abcd",
        attempts_root=tmp_path,
    )


def _attempt(tmp_path: Path, state: str = "prepared") -> Attempt:
    attempt_id = "a" * 32
    base = f"/Users/example/Downloads/utm-18-fill-description-{attempt_id}"
    log = f"{base}.log"
    return Attempt(
        attempt_id=attempt_id,
        run_id="run-12345678",
        vm_name="abcd",
        vm_ip="192.168.64.20",
        log_path=log,
        status_path=f"{log}.status",
        command_path=f"{base}.command",
        ledger_path=tmp_path / "run-12345678" / f"{attempt_id}.json",
        state=state,
        prepared_at="2026-08-07T00:00:00+00:00",
    )


def test_apple_login_reads_notion_and_sends_secrets_only_in_ssh_stdin() -> None:
    api = FakeNotion()
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["input"] = kwargs["input"]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "markers": ["APPLE_DEVELOPER_ACCOUNT=verified"],
                    "edge_pid": 123,
                    "edge_websocket": "ws://127.0.0.1:9222/devtools/browser/abc",
                }
            ).encode(),
            stderr=b"",
        )

    result = utm_18.verify_or_login_apple(
        api,
        parent_title="Host",
        page_title="Demo-abcd",
        vm_name="abcd",
        vm_ip="192.168.64.20",
        vm_user="abcd",
        runner=fake_run,
    )

    payload = json.loads(bytes(captured["input"]).decode("utf-8"))
    assert payload["APPLE_ACCOUNT_PASSWORD"] == "changed-secret"
    command_text = " ".join(str(part) for part in captured["command"])
    assert "changed-secret" not in command_text
    assert "account@example.test" not in command_text
    assert result["edge_pid"] == 123
    assert ("verify-parent", "Host") in api.calls


def test_apple_login_success_removes_stale_diagnostic(monkeypatch, tmp_path) -> None:
    diagnostic = tmp_path / "utm-18-apple-login.stderr"
    diagnostic.write_text("stale failure", encoding="utf-8")
    monkeypatch.setattr(utm_18, "APPLE_LOGIN_DIAGNOSTIC", diagnostic)

    def fake_run(command, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "markers": ["APPLE_DEVELOPER_ACCOUNT=verified"],
                    "edge_pid": 123,
                    "edge_websocket": "ws://127.0.0.1:9222/devtools/browser/abc",
                }
            ).encode(),
            stderr=b"",
        )

    utm_18.verify_or_login_apple(
        FakeNotion(),
        parent_title="Host",
        page_title="Demo-abcd",
        vm_name="abcd",
        vm_ip="192.168.64.20",
        vm_user="abcd",
        runner=fake_run,
    )

    assert not diagnostic.exists()


def test_apple_login_failure_preserves_exact_stderr_for_local_diagnosis(
    monkeypatch, tmp_path
) -> None:
    api = FakeNotion()
    diagnostic = tmp_path / "utm-18-apple-login.stderr"
    monkeypatch.setattr(utm_18, "APPLE_LOGIN_DIAGNOSTIC", diagnostic)

    def fake_run(command, **kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout=b"",
            stderr=(
                b"Traceback (most recent call last):\n"
                b"AppleWebWorkflowError: SELENIUM_EMAIL_CONTINUE_COUNT=0\n"
            ),
        )

    with pytest.raises(
        utm_18.UTM18Error,
        match=r"^APPLE_DEVELOPER_LOGIN_FAILED:SELENIUM_EMAIL_CONTINUE_COUNT=0$",
    ):
        utm_18.verify_or_login_apple(
            api,
            parent_title="Host",
            page_title="Demo-abcd",
            vm_name="abcd",
            vm_ip="192.168.64.20",
            vm_user="abcd",
            runner=fake_run,
        )
    assert diagnostic.read_bytes().endswith(
        b"AppleWebWorkflowError: SELENIUM_EMAIL_CONTINUE_COUNT=0\n"
    )
    assert diagnostic.stat().st_mode & 0o777 == 0o600


def test_apple_login_bridge_does_not_repeat_ax_membership_wait_after_dom() -> None:
    assert 'backend.wait_for_text("Membership details"' not in utm_18.APPLE_LOGIN_BRIDGE


def test_run_executes_all_stages_in_order_without_confirmation(monkeypatch, tmp_path, capsys) -> None:
    api = FakeNotion()
    attempt = _attempt(tmp_path)
    events: list[str] = []

    monkeypatch.setattr(utm_18, "api_from_env", lambda: api)
    monkeypatch.setattr(
        utm_18,
        "_run_context",
        lambda run_id, vm_name: ("Host", "Demo", "Demo-abcd"),
        raising=False,
    )
    monkeypatch.setattr(
        utm_18,
        "prepare_edge",
        lambda *args, **kwargs: pytest.fail(
            "the host entry must not replace the fixed Edge launch commands"
        ),
    )
    monkeypatch.setattr(
        utm_18,
        "verify_or_login_apple",
        lambda *args, **kwargs: events.append("verify_or_login_apple")
        or {"edge_pid": 123, "edge_websocket": "ws://127.0.0.1:9222/devtools/browser/abc"},
    )
    monkeypatch.setattr(utm_18, "load_single_attempt", lambda *args: None)
    monkeypatch.setattr(
        utm_18,
        "create_attempt",
        lambda *args, **kwargs: events.append("create_attempt") or attempt,
    )
    monkeypatch.setattr(
        utm_18,
        "precommit_remote",
        lambda value: events.append("precommit_remote"),
    )
    monkeypatch.setattr(
        utm_18,
        "run_remote",
        lambda value: events.append("run_remote") or 0,
    )
    monkeypatch.setattr(
        utm_18,
        "inspect_remote",
        lambda value: events.append("inspect_remote")
        or ("success", "status", False),
    )
    monkeypatch.setattr(
        utm_18,
        "classify_attempt",
        lambda *args, **kwargs: events.append("classify_attempt")
        or AttemptResult("verified", "exited_zero", 14),
    )
    monkeypatch.setattr(
        utm_18,
        "mark_attempt",
        lambda value, **kwargs: events.append(f"mark:{kwargs['state']}")
        or Attempt(**{**value.__dict__, "state": kwargs["state"], "mode": kwargs.get("mode", "")}),
    )

    assert utm_18.run(_args(tmp_path)) == 0
    assert events == [
        "verify_or_login_apple",
        "create_attempt",
        "precommit_remote",
        "mark:precommitted",
        "mark:running",
        "run_remote",
        "inspect_remote",
        "classify_attempt",
        "mark:verified",
    ]
    output = capsys.readouterr().out
    assert "UTM_18=verified" in output
    assert "确认" not in output


def test_existing_running_attempt_is_reconciled_and_never_rerun(monkeypatch, tmp_path) -> None:
    api = FakeNotion()
    attempt = _attempt(tmp_path, state="running")
    monkeypatch.setattr(utm_18, "api_from_env", lambda: api)
    monkeypatch.setattr(
        utm_18,
        "_run_context",
        lambda run_id, vm_name: ("Host", "Demo", "Demo-abcd"),
        raising=False,
    )
    monkeypatch.setattr(utm_18, "prepare_edge", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        utm_18,
        "verify_or_login_apple",
        lambda *args, **kwargs: {"edge_pid": 123, "edge_websocket": "ws://127.0.0.1:9222/devtools/browser/abc"},
    )
    monkeypatch.setattr(utm_18, "load_single_attempt", lambda *args: attempt)
    monkeypatch.setattr(
        utm_18,
        "run_remote",
        lambda value: pytest.fail("must not rerun an existing attempt"),
    )
    monkeypatch.setattr(
        utm_18,
        "inspect_remote",
        lambda value: ("success", "status", False),
    )
    monkeypatch.setattr(
        utm_18,
        "classify_attempt",
        lambda *args, **kwargs: AttemptResult("verified", "exited_zero", 14),
    )
    monkeypatch.setattr(
        utm_18,
        "mark_attempt",
        lambda value, **kwargs: Attempt(**{**value.__dict__, "state": kwargs["state"], "mode": kwargs.get("mode", "")}),
    )
    assert utm_18.run(_args(tmp_path)) == 0


@pytest.mark.parametrize(
    ("failed_log", "failed_status"),
    (
        (
            "Error: 找不到可点击的inflight 页面保存按钮",
            "RUN_STATE=finished\nREMOTE_NPM_EXIT=1\nREMOTE_TEE_EXIT=0\n",
        ),
        (
            "已点击inflight 页面保存按钮",
            "RUN_STATE=finished\nREMOTE_NPM_EXIT=1\nREMOTE_TEE_EXIT=141\n",
        ),
        (
            "browserType.connectOverCDP: connect ECONNREFUSED 127.0.0.1:9222",
            "RUN_STATE=finished\nREMOTE_NPM_EXIT=1\nREMOTE_TEE_EXIT=0\n",
        ),
    ),
)
def test_known_finished_retryable_attempt_is_archived_then_full_entry_replays(
    monkeypatch, tmp_path, failed_log, failed_status
) -> None:
    api = FakeNotion()
    failed = _attempt(tmp_path, state="running")
    fresh = Attempt(
        **{
            **failed.__dict__,
            "attempt_id": "b" * 32,
            "log_path": f"/Users/example/Downloads/utm-18-fill-description-{'b' * 32}.log",
            "status_path": f"/Users/example/Downloads/utm-18-fill-description-{'b' * 32}.log.status",
            "ledger_path": tmp_path / "run-12345678" / f"{'b' * 32}.json",
            "state": "prepared",
        }
    )
    events: list[str] = []

    monkeypatch.setattr(utm_18, "api_from_env", lambda: api)
    monkeypatch.setattr(
        utm_18,
        "_run_context",
        lambda run_id, vm_name: ("Host", "Demo", "Demo-abcd"),
        raising=False,
    )
    monkeypatch.setattr(utm_18, "load_single_attempt", lambda *args: failed)
    monkeypatch.setattr(
        utm_18,
        "inspect_remote",
        lambda value: (
            failed_log,
            f"ATTEMPT_ID={failed.attempt_id}\n{failed_status}",
            False,
        ),
    )
    monkeypatch.setattr(
        utm_18,
        "archive_retryable_attempt",
        lambda *args, **kwargs: events.append("archive"),
    )
    monkeypatch.setattr(
        utm_18,
        "prepare_edge",
        lambda *args, **kwargs: pytest.fail(
            "the host entry must not replace the fixed Edge launch commands"
        ),
    )
    monkeypatch.setattr(
        utm_18,
        "verify_or_login_apple",
        lambda *args, **kwargs: events.append("login") or {},
    )
    monkeypatch.setattr(
        utm_18,
        "create_attempt",
        lambda *args, **kwargs: events.append("create") or fresh,
    )
    monkeypatch.setattr(utm_18, "precommit_remote", lambda value: None)
    monkeypatch.setattr(utm_18, "run_remote", lambda value: 0)
    monkeypatch.setattr(
        utm_18,
        "classify_attempt",
        lambda *args, **kwargs: AttemptResult("verified", "exited_zero", 14),
    )
    monkeypatch.setattr(
        utm_18,
        "mark_attempt",
        lambda value, **kwargs: Attempt(
            **{
                **value.__dict__,
                "state": kwargs["state"],
                "mode": kwargs.get("mode", ""),
            }
        ),
    )

    assert utm_18.run(_args(tmp_path)) == 0
    assert events[:3] == ["archive", "login", "create"]


def test_completed_transport_stop_is_verified_only_after_three_stable_reads(
    monkeypatch, tmp_path
) -> None:
    attempt = _attempt(tmp_path, state="running")
    calls = 0

    def inspect(value):
        nonlocal calls
        calls += 1
        return ("stable", "status", False)

    monkeypatch.setattr(utm_18, "inspect_remote", inspect)
    monkeypatch.setattr(
        utm_18,
        "classify_attempt",
        lambda *args, **kwargs: AttemptResult(
            "verification_pending", "completed_after_transport_stop", 22
        ),
    )

    result = utm_18._classify_with_resident_rechecks(
        attempt,
        sleeper=lambda seconds: None,
    )

    assert calls == 3
    assert result == AttemptResult("verified", "completed_after_transport_stop", 22)


def test_running_incomplete_attempt_is_polled_until_completion(
    monkeypatch, tmp_path
) -> None:
    attempt = _attempt(tmp_path, state="running")
    reads = iter(
        (
            ("save clicked", "status", True),
            ("success", "status", False),
        )
    )
    sleeps: list[float] = []

    monkeypatch.setattr(utm_18, "inspect_remote", lambda value: next(reads))
    results = iter(
        (
            AttemptResult("ambiguous", "incomplete"),
            AttemptResult("verified", "exited_zero", 14),
        )
    )
    monkeypatch.setattr(
        utm_18,
        "classify_attempt",
        lambda *args, **kwargs: next(results),
    )

    result = utm_18._classify_with_resident_rechecks(
        attempt,
        sleeper=sleeps.append,
    )

    assert result == AttemptResult("verified", "exited_zero", 14)
    assert sleeps == [5]


def test_complete_entry_source_never_starts_or_switches_vm() -> None:
    source = Path(utm_18.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "utmctl start",
        '"start", vm_name',
        "open -a UTM",
        "runtime/feishu-runs.json",
        "codex-app-sessions",
        "notify-fault",
        "wait-decision",
    ):
        assert forbidden not in source


DIRECT_CONTEXT_ID = "direct-v1-abcd-0123456789abcdef01234567"


def _selector_sets(parser: argparse.ArgumentParser) -> set[frozenset[str]]:
    return {
        frozenset(action.dest for action in group._group_actions)  # noqa: SLF001
        for group in parser._mutually_exclusive_groups  # noqa: SLF001
    }


def test_cli_uses_run_id_or_page_title_as_required_mutually_exclusive_selectors() -> None:
    parser = utm_18.build_parser()
    action_names = {
        action.dest for action in parser._actions if action.dest != "help"  # noqa: SLF001
    }
    assert action_names == {
        "run_id",
        "page_title",
        "vm_name",
        "vm_ip",
        "vm_user",
        "attempts_root",
    }
    assert frozenset({"run_id", "page_title"}) in _selector_sets(parser)

    run_args = parser.parse_args(
        [
            "--run-id",
            "run-12345678",
            "--vm-name",
            "abcd",
            "--vm-ip",
            "192.168.64.20",
            "--vm-user",
            "abcd",
        ]
    )
    assert run_args.run_id == "run-12345678"
    assert run_args.page_title is None

    direct_args = parser.parse_args(
        [
            "--page-title",
            "Demo-abcd",
            "--vm-name",
            "abcd",
            "--vm-ip",
            "192.168.64.20",
            "--vm-user",
            "abcd",
        ]
    )
    assert direct_args.run_id is None
    assert direct_args.page_title == "Demo-abcd"


def test_main_prints_clean_start_result_and_elapsed(monkeypatch, capsys) -> None:
    monkeypatch.setattr(utm_18, "run", lambda args: 0)
    ticks = iter((100.0, 112.0))
    monkeypatch.setattr(utm_18.time, "monotonic", lambda: next(ticks))
    assert utm_18.main([
        "--page-title", "Demo-abcd",
        "--vm-name", "abcd",
        "--vm-ip", "192.0.2.10",
        "--vm-user", "abcd",
    ]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "开始执行：utm-script",
        "执行成功：utm-script；UTM_18=verified",
    ]


def test_main_prints_clean_edge_error_without_traceback(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        utm_18,
        "run",
        lambda args: (_ for _ in ()).throw(
            utm_18.UTM18EdgeError("EDGE_PREPARE_SSH_EXIT=1")
        ),
    )
    assert utm_18.main([
        "--page-title", "Demo-abcd",
        "--vm-name", "abcd",
        "--vm-ip", "192.0.2.10",
        "--vm-user", "abcd",
    ]) == 1
    captured = capsys.readouterr()
    assert captured.out.splitlines() == ["开始执行：utm-script"]
    assert captured.err.splitlines() == ["执行报错：utm-script；EDGE_PREPARE_SSH_EXIT=1"]


def test_main_preserves_the_complete_actual_error(monkeypatch, capsys) -> None:
    detail = "asset lookup failed: " + "z" * 400
    monkeypatch.setattr(
        utm_18,
        "run",
        lambda args: (_ for _ in ()).throw(utm_18.UTM18Error(detail)),
    )
    assert utm_18.main([
        "--page-title", "Demo-abcd",
        "--vm-name", "abcd",
        "--vm-ip", "192.0.2.10",
        "--vm-user", "abcd",
    ]) == 1
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [f"执行报错：utm-script；{detail}"]


def test_attempt_failure_message_includes_remote_reason() -> None:
    result = AttemptResult(
        "ambiguous",
        "incomplete",
        reason="RUN_STATE=finished;REMOTE_NPM_EXIT=1;REMOTE_TEE_EXIT=0",
    )
    assert utm_18._attempt_failure_message(result) == (
        "FILL_DESCRIPTION_BLOCKED=ambiguous:incomplete;"
        "RUN_STATE=finished;REMOTE_NPM_EXIT=1;REMOTE_TEE_EXIT=0"
    )


def test_direct_mode_reuses_shared_context_id_and_never_reads_or_cards_a_feishu_run(
    monkeypatch, tmp_path, capsys
) -> None:
    api = FakeNotion()
    context = SimpleNamespace(
        context_id=DIRECT_CONTEXT_ID,
        parent_title="Host",
        page_title="Demo-abcd",
        app_name="Demo",
        vm_name="abcd",
        vm_ip="192.168.64.20",
        vm_user="abcd",
    )
    seen: dict[str, object] = {}

    monkeypatch.setattr(utm_18, "api_from_env", lambda: api)
    monkeypatch.setattr(
        utm_18,
        "resolve_direct_context",
        lambda **kwargs: context,
        raising=False,
    )

    def forbidden(*args, **kwargs):
        pytest.fail("direct context must not read/create a Feishu run/session/card")

    for name in ("find_run", "notify_fault", "wait_decision", "create_codex_session"):
        monkeypatch.setattr(utm_18, name, forbidden, raising=False)

    verified = Attempt(
        attempt_id="a" * 32,
        run_id=DIRECT_CONTEXT_ID,
        vm_name="abcd",
        vm_ip="192.168.64.20",
        log_path=f"/Users/example/Downloads/utm-18-fill-description-{'a' * 32}.log",
        status_path=f"/Users/example/Downloads/utm-18-fill-description-{'a' * 32}.log.status",
        command_path=f"/Users/example/Downloads/utm-18-fill-description-{'a' * 32}.command",
        ledger_path=tmp_path / DIRECT_CONTEXT_ID / f"{'a' * 32}.json",
        state="verified",
        prepared_at="2026-08-07T00:00:00+00:00",
    )

    def load_attempt(root, context_id):
        seen["context_id"] = context_id
        return verified

    monkeypatch.setattr(utm_18, "load_single_attempt", load_attempt)
    monkeypatch.setattr(
        utm_18,
        "prepare_edge",
        lambda *args, **kwargs: pytest.fail("verified direct attempt must not restart Edge"),
    )

    args = argparse.Namespace(
        run_id=None,
        page_title="Demo-abcd",
        vm_name="abcd",
        vm_ip="192.168.64.20",
        vm_user="abcd",
        attempts_root=tmp_path,
    )
    assert utm_18.run(args) == 0
    assert seen["context_id"] == DIRECT_CONTEXT_ID
    assert ("verify-parent", "Host") in api.calls
    assert ("read-field", "应用名: ") in api.calls
    assert "UTM_18=verified" in capsys.readouterr().out


def test_run_mode_still_resolves_owned_run_context_without_legacy_page_or_parent_cli(
    monkeypatch, tmp_path
) -> None:
    api = FakeNotion()
    seen: dict[str, object] = {}
    monkeypatch.setattr(utm_18, "api_from_env", lambda: api)
    monkeypatch.setattr(
        utm_18,
        "_run_context",
        lambda run_id, vm_name: ("Host", "Demo", "Demo-abcd"),
        raising=False,
    )

    def load_attempt(root, context_id):
        seen["context_id"] = context_id
        return Attempt(
            attempt_id="b" * 32,
            run_id="run-12345678",
            vm_name="abcd",
            vm_ip="192.168.64.20",
            log_path=f"/Users/example/Downloads/utm-18-fill-description-{'b' * 32}.log",
            status_path=f"/Users/example/Downloads/utm-18-fill-description-{'b' * 32}.log.status",
            command_path=f"/Users/example/Downloads/utm-18-fill-description-{'b' * 32}.command",
            ledger_path=tmp_path / "run-12345678" / f"{'b' * 32}.json",
            state="verified",
            prepared_at="2026-08-07T00:00:00+00:00",
        )

    monkeypatch.setattr(utm_18, "load_single_attempt", load_attempt)
    args = argparse.Namespace(
        run_id="run-12345678",
        page_title=None,
        vm_name="abcd",
        vm_ip="192.168.64.20",
        vm_user="abcd",
        attempts_root=tmp_path,
    )
    assert utm_18.run(args) == 0
    assert seen["context_id"] == "run-12345678"
    assert ("verify-parent", "Host") in api.calls
    assert ("read-field", "应用名: ") in api.calls


def test_direct_mode_rejects_notion_app_mismatch_before_any_edge_or_attempt_side_effect(
    monkeypatch, tmp_path
) -> None:
    api = FakeNotion()
    api.fields["应用名: "] = "Other"
    context = SimpleNamespace(
        context_id=DIRECT_CONTEXT_ID,
        parent_title="Host",
        page_title="Demo-abcd",
        app_name="Demo",
        vm_name="abcd",
        vm_ip="192.168.64.20",
        vm_user="abcd",
    )
    monkeypatch.setattr(utm_18, "api_from_env", lambda: api)
    monkeypatch.setattr(
        utm_18,
        "resolve_direct_context",
        lambda **kwargs: context,
        raising=False,
    )
    monkeypatch.setattr(
        utm_18,
        "load_single_attempt",
        lambda *args, **kwargs: pytest.fail("Notion mismatch must block before ledger access"),
    )
    monkeypatch.setattr(
        utm_18,
        "prepare_edge",
        lambda *args, **kwargs: pytest.fail("Notion mismatch must block before Edge restart"),
    )
    args = argparse.Namespace(
        run_id=None,
        page_title="Demo-abcd",
        vm_name="abcd",
        vm_ip="192.168.64.20",
        vm_user="abcd",
        attempts_root=tmp_path,
    )
    with pytest.raises(utm_18.UTM18Error, match="NOTION_PAGE_APP_MISMATCH"):
        utm_18.run(args)
