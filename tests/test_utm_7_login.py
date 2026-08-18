#!/usr/bin/env python3
"""Contract tests for the non-visual UTM-7 Apple Account runner."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT))

import scripts.utm_7_login as runner  # noqa: E402


class FakeNotion:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.values = {
            "用户名：": "",
            "邮箱：": "account@example.test",
            "修改后的密码：": "",
            "初始密码：": "initial-secret",
            "电话：": "+1 555 0100",
            "电话短信接收平台：": "https://sms.example.test/latest",
            "生日（格式年/月/日）：": "",
        }

    def verify_parent(self, title: str) -> str:
        self.calls.append(("verify-parent", title))
        return "parent-id"

    def read_field(self, title: str, heading: str, label: str) -> str:
        self.calls.append(("read-field", label))
        return self.values[label]

    def set_field(
        self,
        title: str,
        heading: str,
        label: str,
        value: str,
        *,
        replace_existing: bool = False,
    ) -> bool:
        self.calls.append(("set-field", label))
        current = self.values[label]
        if current == value:
            return False
        if current and not replace_existing:
            raise RuntimeError("field conflict")
        self.values[label] = value
        return True


class UTM7LoginRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.shared_temp = tempfile.TemporaryDirectory()
        self.password_patch = patch.dict(
            os.environ, {"SUBMISSION_GUEST_PASSWORD": "9072"}
        )
        self.shared_patch = patch.object(
            runner, "SHARED_DIR", Path(self.shared_temp.name)
        )
        self.password_patch.start()
        self.shared_patch.start()

    def tearDown(self) -> None:
        self.shared_patch.stop()
        self.password_patch.stop()
        self.shared_temp.cleanup()

    def test_existing_helper_bridge_temporarily_installs_all_runtime_values(self) -> None:
        payload = {
            "APPLE_ACCOUNT_EMAIL": "account@example.test",
            "APPLE_ACCOUNT_PASSWORD": "initial-secret",
            "APPLE_ACCOUNT_PHONE": "+1 555 0100",
            "APPLE_ACCOUNT_SMS_URL": "https://sms.example.test/latest",
            "SUBMISSION_GUEST_PASSWORD": "6194",
        }
        observed_environment: dict[str, str | None] = {}

        def observe_guest_script(path, *, run_name):
            self.assertEqual(path, "/guest/apple_account_login.py")
            self.assertEqual(run_name, "__main__")
            for key in payload:
                observed_environment[key] = os.environ.get(key)
            os.environ["SUBMISSION_GUEST_PASSWORD"] = "changed-by-helper"

        original_environment = {
            "PREEXISTING_GUEST_VALUE": "preserved",
            "APPLE_ACCOUNT_EMAIL": "preexisting@example.test",
        }
        with patch.dict(os.environ, original_environment, clear=True):
            with patch.object(
                sys,
                "stdin",
                io.StringIO(json.dumps(payload)),
            ), patch.object(
                sys,
                "argv",
                ["-c", "/guest/apple_account_login.py"],
            ), patch(
                "runpy.run_path",
                side_effect=observe_guest_script,
            ):
                exec(runner.EXISTING_HELPER_BRIDGE, {})

            self.assertEqual(os.environ, original_environment)

        self.assertEqual(observed_environment, payload)

    def test_main_reports_elapsed_seconds(self) -> None:
        argv = [
            "utm_7_login.py",
            "--parent-title",
            "Host",
            "--page-title",
            "App-test1",
            "--vm-ip",
            "192.0.2.10",
            "--vm-user",
            "test1",
        ]
        stdout = io.StringIO()
        with patch.object(sys, "argv", argv), patch.object(
            runner, "run", return_value=0
        ), patch("time.monotonic", side_effect=(100.0, 112.34)), contextlib.redirect_stdout(
            stdout
        ):
            result = runner.main()

        self.assertEqual(result, 0)
        self.assertEqual(
            stdout.getvalue().splitlines(),
            ["开始执行：utm-login", "执行成功：utm-login；UTM_7=verified"],
        )

    def test_main_preserves_the_sanitized_guest_failure_detail(self) -> None:
        argv = [
            "utm_7_login.py",
            "--parent-title",
            "Host",
            "--page-title",
            "App-test1",
            "--vm-ip",
            "192.0.2.10",
            "--vm-user",
            "test1",
        ]
        stderr = io.StringIO()
        with patch.object(sys, "argv", argv), patch.object(
            runner,
            "run",
            side_effect=RuntimeError(
                "APPLE_ACCOUNT_HELPER_FAILED_EXIT_5: 未找到 Email or Phone Number 输入框。"
            ),
        ), contextlib.redirect_stderr(stderr):
            result = runner.main()

        self.assertEqual(result, 1)
        self.assertEqual(
            stderr.getvalue().splitlines(),
            [
                "执行报错：utm-login；APPLE_ACCOUNT_HELPER_FAILED_EXIT_5: "
                "未找到 Email or Phone Number 输入框。"
            ],
        )

    def test_visible_target_email_is_the_terminal_login_success_condition(self) -> None:
        source = (ROOT / "scripts" / "find_system_settings_general.py").read_text(
            encoding="utf-8"
        )
        verification = source.split(
            "def complete_verification_code_workflow", 1
        )[1].split("def find_login_field", 1)[0]
        login = source.split("def run_apple_account_login_workflow", 1)[1].split(
            "def run_apple_account_change_password_workflow", 1
        )[0]

        self.assertNotIn("return invoke_post_login_prompts", verification)
        self.assertNotIn("return invoke_post_login_prompts", login)
        self.assertGreaterEqual(verification.count("return 0"), 2)
        self.assertGreaterEqual(login.count("return 0"), 2)

    def test_personal_information_birthday_page_is_a_signed_in_resume_state(self) -> None:
        source = (ROOT / "scripts" / "find_system_settings_general.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("def is_signed_in_apple_account_page", source)
        self.assertIn('tree_contains_text_casefold(roots, "Birthday")', source)
        self.assertGreaterEqual(source.count("is_signed_in_apple_account_page("), 4)

    def test_each_login_run_reopens_system_settings_from_a_fresh_process(self) -> None:
        source = (ROOT / "scripts" / "find_system_settings_general.py").read_text(
            encoding="utf-8"
        )
        launch = source.split("def launch_system_settings", 1)[1].split(
            "def get_running_system_settings", 1
        )[0]

        self.assertIn("runningApplicationsWithBundleIdentifier_", launch)
        self.assertIn("application.terminate()", launch)
        self.assertIn('["open", "-b", SYSTEM_SETTINGS_BUNDLE_ID]', launch)
        self.assertLess(
            launch.index("application.terminate()"),
            launch.index('["open", "-b", SYSTEM_SETTINGS_BUNDLE_ID]'),
        )

    def test_skill_contract_is_command_only(self) -> None:
        for path in (
            ROOT / "skills" / "utm-login" / "SKILL.md",
            ROOT / "docs" / "utm-login.md",
        ):
            text = path.read_text(encoding="utf-8")
            self.assertIn(str(ROOT / "scripts" / "utm_7_login.py"), text)
            self.assertIn("不读取或解释脚本内容", text)
            for implementation_detail in (
                "AppleAccountScriptsBackup",
                "初始密码：",
                "sshd-keygen-wrapper",
                "Open System Settings",
                "Modify Settings",
            ):
                self.assertNotIn(implementation_detail, text)

    def test_skill_is_execution_first_and_has_no_obsolete_transport(self) -> None:
        text = (ROOT / "skills" / "utm-login" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        for heading in ("## 执行", "## 输出与交接"):
            self.assertIn(heading, text)
        for stale_heading in ("## 执行流程", "## 本技能自动恢复矩阵"):
            self.assertNotIn(stale_heading, text)
        for obsolete in (
            "BatchMode SSH",
            "项目 SSH 公钥",
            "把项目内四个 helper 上传",
            "python3 services/feishu_bot.py notify-fault",
        ):
            self.assertNotIn(obsolete, text)

    def test_syncs_all_helpers_atomically_without_deleting_other_backup_files(self) -> None:
        source_dir = ROOT / "scripts"
        backup_dir = Path(self.shared_temp.name) / "AppleAccountScriptsBackup"
        backup_dir.mkdir()
        (backup_dir / "keep-user-file.txt").write_text("keep", encoding="utf-8")
        (backup_dir / "apple_account_login.py").write_text(
            "# stale\n", encoding="utf-8"
        )

        with patch.object(os, "replace", wraps=os.replace) as replace_file, patch.object(
            os, "fsync", wraps=os.fsync
        ) as fsync_file:
            hashes = runner.sync_helpers_to_shared_backup(
                source_dir, Path(self.shared_temp.name)
            )

        self.assertEqual(set(hashes), set(runner.SHARED_HELPER_FILES))
        self.assertEqual(len(hashes), 8)
        self.assertIn("apple_account_profile.py", hashes)
        self.assertIn("apple_web_workflow.py", hashes)
        self.assertIn("edge_accessibility.py", hashes)
        self.assertGreaterEqual(replace_file.call_count, len(runner.SHARED_HELPER_FILES))
        self.assertGreaterEqual(fsync_file.call_count, len(runner.SHARED_HELPER_FILES) + 1)
        self.assertEqual(
            (backup_dir / "keep-user-file.txt").read_text(encoding="utf-8"), "keep"
        )
        self.assertFalse(any(path.name.startswith(".utm-7-sync-") for path in backup_dir.iterdir()))
        for name in runner.SHARED_HELPER_FILES:
            self.assertEqual(
                (backup_dir / name).read_bytes(), (source_dir / name).read_bytes()
            )

    def test_default_path_uses_guest_shared_mount_without_scp(self) -> None:
        api = FakeNotion()
        captured: dict[str, object] = {}

        def fake_run(command, **kwargs):
            captured.setdefault("commands", []).append(command)
            if "apple_account_profile.py" in " ".join(str(part) for part in command):
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "name": "Jan Haren",
                            "birth_year": 2000,
                            "birth_month": 1,
                            "birth_day": 1,
                        }
                    ).encode(),
                    stderr=b"",
                )
            if kwargs.get("input") is not None:
                captured["payload"] = kwargs["input"]
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.object(
            runner, "api_from_env", return_value=api
        ), patch.object(
            runner.subprocess, "run", side_effect=fake_run
        ):
            args = SimpleNamespace(
                vm_user="test1",
                vm_ip="192.0.2.10",
                parent_title="Host",
                page_title="App-test1",
                guest_dir="",
            )
            result = runner.run(args)

        self.assertEqual(result, 0)
        self.assertFalse(
            any("/usr/bin/scp" in command for command in captured["commands"])
        )
        final_command = next(
            command
            for command in captured["commands"]
            if any("runpy.run_path" in str(part) for part in command)
        )
        command_text = " ".join(final_command)
        self.assertIn(
            "/Volumes/My Shared Files/共享文件/AppleAccountScriptsBackup/apple_account_login.py",
            command_text,
        )
        self.assertNotIn(
            "/Users/example/Downloads/AppleAccountScriptsBackup", command_text
        )
        self.assertNotIn("account@example.test", command_text)
        self.assertNotIn("initial-secret", command_text)

    def test_guest_login_retries_once_after_a_transient_ui_failure(self) -> None:
        api = FakeNotion()
        login_results = iter(
            (
                SimpleNamespace(
                    returncode=5,
                    stdout=b"",
                    stderr="未找到 Email or Phone Number 输入框。\n".encode(),
                ),
                SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
            )
        )
        login_calls = 0

        def fake_run(command, **_kwargs):
            nonlocal login_calls
            command_text = " ".join(str(part) for part in command)
            if "apple_account_login.py" in command_text:
                login_calls += 1
                return next(login_results)
            if "apple_account_profile.py" in command_text:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "name": "Jan Haren",
                            "birth_year": 2000,
                            "birth_month": 1,
                            "birth_day": 1,
                        }
                    ).encode(),
                    stderr=b"",
                )
            raise AssertionError(command_text)

        args = SimpleNamespace(
            vm_user="test1",
            vm_ip="192.0.2.10",
            parent_title="Host",
            page_title="App-test1",
            guest_dir="/Users/example/Downloads",
        )
        with patch.object(runner, "api_from_env", return_value=api), patch.object(
            runner, "_prepare_shared_helpers", return_value={}
        ), patch.object(runner, "_ensure_remote_pyobjc"), patch.object(
            runner, "_ensure_remote_accessibility"
        ) as ensure_accessibility, patch.object(
            runner, "_recover_guest_desktop", create=True
        ) as recover_guest_desktop, patch.object(
            runner.subprocess, "run", side_effect=fake_run
        ), patch.object(runner.time, "sleep") as wait:
            result = runner.run(args)

        self.assertEqual(result, 0)
        self.assertEqual(login_calls, 2)
        self.assertEqual(ensure_accessibility.call_count, 2)
        recover_guest_desktop.assert_called_once_with("test1", "192.0.2.10")
        wait.assert_called_once_with(2)

    def test_persistent_guest_login_failure_reports_sanitized_actual_detail(self) -> None:
        api = FakeNotion()
        detail = "未找到 Email or Phone Number 输入框。"
        guest_secret = os.environ["SUBMISSION_GUEST_PASSWORD"]

        def fake_run(command, **_kwargs):
            command_text = " ".join(str(part) for part in command)
            if "apple_account_login.py" in command_text:
                return SimpleNamespace(
                    returncode=5,
                    stdout=b"",
                    stderr=(f"{detail} runtime={guest_secret}\n").encode(),
                )
            raise AssertionError(command_text)

        args = SimpleNamespace(
            vm_user="test1",
            vm_ip="192.0.2.10",
            parent_title="Host",
            page_title="App-test1",
            guest_dir="/Users/example/Downloads",
        )
        with patch.object(runner, "api_from_env", return_value=api), patch.object(
            runner, "_prepare_shared_helpers", return_value={}
        ), patch.object(runner, "_ensure_remote_pyobjc"), patch.object(
            runner, "_ensure_remote_accessibility"
        ), patch.object(
            runner, "_recover_guest_desktop", create=True
        ), patch.object(runner.subprocess, "run", side_effect=fake_run), patch.object(
            runner.time, "sleep"
        ):
            with self.assertRaisesRegex(RuntimeError, detail) as raised:
                runner.run(args)

        message = str(raised.exception)
        self.assertNotIn("account@example.test", message)
        self.assertNotIn("initial-secret", message)
        self.assertNotIn(guest_secret, message)

    def test_sms_delivery_timeout_resumes_without_reentering_the_guest_desktop(self) -> None:
        api = FakeNotion()
        login_results = iter(
            (
                SimpleNamespace(
                    returncode=5,
                    stdout=b"",
                    stderr=(
                        "短信页面中未找到唯一可识别的六位验证码"
                        "（超时：30秒）。\n"
                    ).encode(),
                ),
                SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
            )
        )

        def fake_run(command, **_kwargs):
            command_text = " ".join(str(part) for part in command)
            if "apple_account_login.py" in command_text:
                return next(login_results)
            if "apple_account_profile.py" in command_text:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "name": "Jan Haren",
                            "birth_year": 2000,
                            "birth_month": 1,
                            "birth_day": 1,
                        }
                    ).encode(),
                    stderr=b"",
                )
            raise AssertionError(command_text)

        args = SimpleNamespace(
            vm_user="test1",
            vm_ip="192.0.2.10",
            parent_title="Host",
            page_title="App-test1",
            guest_dir="/Users/example/Downloads",
        )
        with patch.object(runner, "api_from_env", return_value=api), patch.object(
            runner, "_prepare_shared_helpers", return_value={}
        ), patch.object(runner, "_ensure_remote_pyobjc"), patch.object(
            runner, "_ensure_remote_accessibility"
        ), patch.object(
            runner, "_recover_guest_desktop", create=True
        ) as recover_guest_desktop, patch.object(
            runner.subprocess, "run", side_effect=fake_run
        ), patch.object(runner.time, "sleep") as wait:
            result = runner.run(args)

        self.assertEqual(result, 0)
        recover_guest_desktop.assert_not_called()
        wait.assert_called_once_with(10)

    def test_guest_verification_receives_all_seven_exact_shared_hashes(self) -> None:
        expected = {
            name: f"{index:064x}"
            for index, name in enumerate(runner.SHARED_HELPER_FILES, start=1)
        }
        with patch.object(
            runner.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
        ) as run_command:
            runner._verify_guest_shared_helpers(
                "test1", "192.0.2.10", runner.GUEST_SHARED_HELPER_DIR, expected
            )

        command_text = " ".join(run_command.call_args.args[0])
        for name, digest in expected.items():
            self.assertIn(f"{runner.GUEST_SHARED_HELPER_DIR}/{name}", command_text)
            self.assertIn(digest, command_text)

    def test_profile_read_failure_preserves_the_safe_guest_reason(self) -> None:
        with patch.object(
            runner.subprocess,
            "run",
            return_value=SimpleNamespace(
                returncode=1,
                stdout=b"",
                stderr=(
                    b"APPLE_ACCOUNT_PROFILE=blocked "
                    b"reason=APPLE_BIRTHDAY_READ_EXHAUSTED:APPLE_BIRTHDAY_NOT_UNIQUE\n"
                ),
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "APPLE_BIRTHDAY_READ_EXHAUSTED:APPLE_BIRTHDAY_NOT_UNIQUE",
            ):
                runner._read_guest_profile(
                    "test1",
                    "192.0.2.10",
                    runner.GUEST_SHARED_HELPER_DIR,
                    "account@example.test",
                )

    def test_shared_helper_preparation_repairs_and_rechecks_three_rounds(self) -> None:
        expected = {name: "a" * 64 for name in runner.SHARED_HELPER_FILES}
        with patch.object(
            runner, "sync_helpers_to_shared_backup", return_value=expected
        ) as sync_backup, patch.object(
            runner,
            "_verify_guest_shared_helpers",
            side_effect=(RuntimeError("mount stale"), RuntimeError("mount stale"), None),
        ) as verify_guest, patch.object(
            runner, "_refresh_guest_shared_mount", return_value=None, create=True
        ) as refresh_mount, patch.object(runner.time, "sleep") as wait:
            actual = runner._prepare_shared_helpers(
                "test1", "192.0.2.10", runner.GUEST_SHARED_HELPER_DIR
            )

        self.assertEqual(actual, expected)
        self.assertEqual(sync_backup.call_count, 3)
        self.assertEqual(verify_guest.call_count, 3)
        self.assertEqual(refresh_mount.call_count, 1)
        self.assertEqual([call.args[0] for call in wait.call_args_list], [5, 10])

    def test_reads_only_initial_password_for_apple_account_login(self) -> None:
        api = FakeNotion()
        api.values["修改后的密码："] = "modified-secret"
        guest_fixture_value = "6194"
        captured: dict[str, object] = {}

        def fake_run(command, **kwargs):
            captured.setdefault("commands", []).append(command)
            if "apple_account_profile.py" in " ".join(str(part) for part in command):
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "name": "Jan Haren",
                            "birth_year": 2000,
                            "birth_month": 1,
                            "birth_day": 1,
                        }
                    ).encode(),
                    stderr=b"",
                )
            if kwargs.get("input") is not None:
                captured["payload"] = kwargs["input"]
                return SimpleNamespace(returncode=0)
            return SimpleNamespace(returncode=0)

        with patch.object(
            runner, "api_from_env", return_value=api
        ), patch.object(
            runner.subprocess, "run", side_effect=fake_run
        ), patch.object(
            runner, "guest_password", return_value=guest_fixture_value
        ):
            args = SimpleNamespace(
                vm_user="test1",
                vm_ip="192.0.2.10",
                parent_title="Host",
                page_title="App-test1",
                guest_dir="/Users/example/Downloads",
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = runner.run(args)

        self.assertEqual(result, 0)
        self.assertIn(("read-field", "初始密码："), api.calls)
        self.assertNotIn(("read-field", "修改后的密码："), api.calls)
        payload = json.loads(bytes(captured["payload"]).decode("utf-8"))
        self.assertEqual(payload["APPLE_ACCOUNT_PASSWORD"], "initial-secret")
        self.assertEqual(payload["APPLE_ACCOUNT_EMAIL"], "account@example.test")
        self.assertEqual(payload["SUBMISSION_GUEST_PASSWORD"], guest_fixture_value)
        final_command = next(
            command
            for command in captured["commands"]
            if any("runpy.run_path" in str(part) for part in command)
        )
        command_text = " ".join(final_command)
        self.assertNotIn("account@example.test", command_text)
        self.assertNotIn("initial-secret", command_text)
        self.assertNotIn(guest_fixture_value, command_text)
        self.assertNotIn(guest_fixture_value, stdout.getvalue())
        for path in Path(self.shared_temp.name).rglob("*"):
            if path.is_file():
                self.assertNotIn(guest_fixture_value.encode(), path.read_bytes())
        self.assertIn("APPLE_ACCOUNT=verified", stdout.getvalue())
        self.assertIn("UTM_7=verified", stdout.getvalue())

    def test_profile_writeback_formats_apple_components_as_notion_year_month_day(self) -> None:
        api = FakeNotion()
        profile = {
            "name": "Jan Haren",
            "birth_year": 2000,
            "birth_month": 1,
            "birth_day": 2,
        }

        runner.write_profile_to_notion(api, "App-test1", profile)

        self.assertEqual(api.values["用户名："], "Jan Haren")
        self.assertEqual(api.values["生日（格式年/月/日）："], "2000/1/2")

    def test_profile_writeback_corrects_an_existing_inaccurate_birthday(self) -> None:
        api = FakeNotion()
        api.values["用户名："] = "Jan Haren"
        api.values["生日（格式年/月/日）："] = "1999/12/31"

        runner.write_profile_to_notion(
            api,
            "App-test1",
            {
                "name": "Jan Haren",
                "birth_year": 2001,
                "birth_month": 4,
                "birth_day": 13,
            },
        )

        self.assertEqual(api.values["生日（格式年/月/日）："], "2001/4/13")

    def test_host_validation_rejects_an_invalid_calendar_birthday(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "APPLE_ACCOUNT_PROFILE_INVALID"):
            runner._validated_profile(
                {
                    "name": "Jan Haren",
                    "birth_year": 2000,
                    "birth_month": 2,
                    "birth_day": 31,
                }
            )

    def test_profile_writeback_blocks_conflicting_nonempty_value_without_mutation(self) -> None:
        api = FakeNotion()
        api.values["用户名："] = "Different Person"
        before = dict(api.values)

        with self.assertRaisesRegex(RuntimeError, "NOTION_PROFILE_CONFLICT"):
            runner.write_profile_to_notion(
                api,
                "App-test1",
                {
                    "name": "Jan Haren",
                    "birth_year": 2000,
                    "birth_month": 1,
                    "birth_day": 2,
                },
            )

        self.assertEqual(api.values, before)

    def test_profile_writeback_rolls_back_first_field_when_second_write_fails(self) -> None:
        api = FakeNotion()
        original_set_field = api.set_field

        def failing_set_field(title, heading, label, value, *, replace_existing=False):
            if label == "生日（格式年/月/日）：" and value:
                raise RuntimeError("write failed")
            return original_set_field(
                title,
                heading,
                label,
                value,
                replace_existing=replace_existing,
            )

        api.set_field = failing_set_field  # type: ignore[method-assign]
        with self.assertRaisesRegex(RuntimeError, "write failed"):
            runner.write_profile_to_notion(
                api,
                "App-test1",
                {
                    "name": "Jan Haren",
                    "birth_year": 2000,
                    "birth_month": 1,
                    "birth_day": 2,
                },
            )
        self.assertEqual(api.values["用户名："], "")
        self.assertEqual(api.values["生日（格式年/月/日）："], "")

    def test_missing_pyobjc_is_installed_then_rechecked_in_fresh_process(self) -> None:
        with patch.object(
            runner, "_probe_remote_pyobjc", side_effect=(False, True)
        ) as probe, patch.object(
            runner.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
        ) as run_command:
            runner._ensure_remote_pyobjc("test1", "192.0.2.10")

        self.assertEqual(probe.call_count, 2)
        self.assertEqual(run_command.call_count, 1)
        command_text = " ".join(run_command.call_args.args[0])
        self.assertIn("pip install --user", command_text)
        self.assertIn("pyobjc-framework-ApplicationServices", command_text)

    def test_missing_accessibility_is_recovered_by_remote_apple_events(self) -> None:
        synthetic_value = "9072"
        with patch.dict(
            os.environ, {"SUBMISSION_GUEST_PASSWORD": synthetic_value}
        ), patch.object(
            runner, "_probe_remote_accessibility", side_effect=(False, True)
        ) as probe, patch.object(
            runner.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout="verified\n", stderr=""),
        ) as run_command:
            runner._ensure_remote_accessibility("test1", "192.0.2.10")

        self.assertEqual(probe.call_count, 2)
        self.assertEqual(run_command.call_count, 2)
        launch_command = run_command.call_args_list[0].args[0]
        self.assertIn("System Events", " ".join(launch_command))
        command = run_command.call_args_list[1].args[0]
        source = run_command.call_args_list[1].kwargs["input"]
        self.assertEqual(command, ["/usr/bin/osascript", "-"])
        self.assertNotIn(synthetic_value, " ".join(command))
        self.assertIn(synthetic_value, source)
        self.assertFalse(hasattr(runner, "FIXED_PASSWORD"))
        self.assertIn("Open System Settings", source)
        self.assertIn("sshd-keygen-wrapper", source)
        self.assertIn('text field whose name is "Password"', source)
        self.assertIn("Modify Settings", source)
        self.assertIn("delay 3", source)

    def test_rejects_invalid_vm_user_before_notion_reads(self) -> None:
        api = FakeNotion()
        with patch.object(runner, "api_from_env", return_value=api):
            args = SimpleNamespace(
                vm_user="not-a-user",
                vm_ip="192.0.2.10",
                parent_title="Host",
                page_title="App-test1",
                guest_dir="",
            )
            with self.assertRaisesRegex(RuntimeError, "simple macOS account"):
                runner.run(args)
        self.assertEqual(api.calls, [])


if __name__ == "__main__":
    unittest.main()
