from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import utm_image


ROOT = Path(__file__).resolve().parents[1]
GUEST_SCRIPT = ROOT / "skills" / "utm-image" / "scripts" / "utm_19_one.mjs"


def test_source_result_uses_configured_feishu_wiki_base(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}
    client = object()
    monkeypatch.setattr(utm_image, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(utm_image, "load_credentials", lambda path: ("app-id", "secret"))
    monkeypatch.setattr(utm_image, "FeishuClient", lambda *args: client)
    monkeypatch.setattr(
        utm_image,
        "get_tenant_access_token",
        lambda app_id, secret: "tenant-token",
        raising=False,
    )
    monkeypatch.setattr(
        utm_image,
        "resolve_feishu_source",
        lambda token: ("WikiBaseToken", "tblConfigured", "vewConfigured"),
        raising=False,
    )

    def prepare(**kwargs):
        captured.update(kwargs)
        return {"status": "verified"}

    monkeypatch.setattr(utm_image, "prepare_screenshot_source", prepare)

    assert utm_image._source_result("PackWise-Enter the trip", "run-12345678") == {
        "status": "verified"
    }
    assert captured["base_url"] == (
        "https://qv0zc1dq6qy.feishu.cn/base/WikiBaseToken"
        "?table=tblConfigured&view=vewConfigured"
    )
    assert captured["client"] is client


def test_guest_script_path_is_fixed_in_downloads_backup() -> None:
    assert utm_image.guest_script_path("abcd") == (
        "/Users/example/Downloads/AppleAccountScriptsBackup/utm_19_one.mjs"
    )


def test_guest_preflight_verifies_script_dependency_and_existing_edge() -> None:
    digest = hashlib.sha256(GUEST_SCRIPT.read_bytes()).hexdigest()
    command = utm_image.guest_preflight_command("abcd", digest)
    for required in (
        "/Users/example/Downloads/AppleAccountScriptsBackup/utm_19_one.mjs",
        "/Users/example/Downloads/Fire_One_en1.3/node_modules/playwright-core/index.mjs",
        "shasum -a 256",
        '--check "$target"',
        "127.0.0.1:9222/json/version",
        "EDGE_SCRIPT_HASH_MISMATCH",
        "/usr/bin/pgrep -x",
        "Microsoft Edge",
    ):
        assert required in command
    assert "pgrep -f" not in command
    for forbidden in ("scp", "curl https://", "open -a", "nohup"):
        assert forbidden not in command


def test_guest_execution_rechecks_canonical_script_hash_before_exec() -> None:
    digest = hashlib.sha256(GUEST_SCRIPT.read_bytes()).hexdigest()
    command = utm_image.guest_execution_command("abcd", digest)
    assert "shasum -a 256" in command
    assert "GUEST_SCRIPT_HASH_CHANGED" in command
    assert digest in command


def test_guest_execution_uses_password_ssh_stdin_and_fixed_script() -> None:
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 1:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "edge_pid": 123,
                        "edge_websocket": "ws://127.0.0.1:9222/devtools/browser/session",
                    }
                ).encode(),
                stderr=b"",
            )
        return SimpleNamespace(
            returncode=0,
            stdout=(
                b"LOCAL_EDGE_SESSION=reused\n"
                b"SCREENSHOT_SOURCE_FIELD=development_attachment\n"
                b"SCREENSHOT_PACKAGE=verified\n"
                b"APP_IDENTITY=verified\n"
                b"IPHONE_69_DISPLAY=selected\n"
                b"SCREENSHOT_UPLOAD=verified_3_of_10\n"
                b"UTM_19=verified\n"
            ),
            stderr=b"",
        )

    payload = {
        "runId": "run-12345678",
        "vmName": "abcd",
        "appName": "Demo",
        "appStoreAppId": "123456789",
        "source": {
            "kind": "attachment",
            "sourceField": "研发截图",
            "path": "/Volumes/My Shared Files/共享文件/utm-image/run-12345678/source.zip",
            "sha256": "a" * 64,
            "size": 123,
            "appNameSha256": hashlib.sha256(b"Demo").hexdigest(),
        },
    }
    markers = utm_image.run_guest_script(
        vm_name="abcd",
        vm_ip="192.0.2.10",
        vm_user="abcd",
        payload=payload,
        runner=runner,
        password_env={"SSH_ASKPASS_REQUIRE": "force"},
    )

    assert len(calls) == 2
    assert all(call[0][0] == "/usr/bin/ssh" for call in calls)
    assert calls[0][1]["input"] == b""
    sent = json.loads(calls[1][1]["input"].decode())
    assert sent["expectedEdgePid"] == 123
    assert sent["expectedEdgeWebSocket"].endswith("/session")
    assert sent["source"] == payload["source"]
    assert "UTM_19=verified" in markers


def test_guest_execution_success_removes_stale_diagnostic(
    monkeypatch, tmp_path
) -> None:
    diagnostic = tmp_path / "utm-image-guest.stderr"
    diagnostic.write_text("stale failure", encoding="utf-8")
    monkeypatch.setattr(utm_image, "GUEST_DIAGNOSTIC_PATH", diagnostic)
    calls = 0

    def runner(command, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "edge_pid": 123,
                        "edge_websocket": "ws://127.0.0.1:9222/devtools/browser/session",
                    }
                ).encode(),
                stderr=b"",
            )
        return SimpleNamespace(
            returncode=0,
            stdout=(
                b"LOCAL_EDGE_SESSION=reused\n"
                b"SCREENSHOT_SOURCE_FIELD=development_attachment\n"
                b"SCREENSHOT_PACKAGE=verified\n"
                b"APP_IDENTITY=verified\n"
                b"IPHONE_69_DISPLAY=selected\n"
                b"SCREENSHOT_UPLOAD=verified_3_of_10\n"
                b"UTM_19=verified\n"
            ),
            stderr=b"",
        )

    utm_image.run_guest_script(
        vm_name="abcd",
        vm_ip="192.0.2.10",
        vm_user="abcd",
        payload={"source": {}},
        runner=runner,
        password_env={"SSH_ASKPASS_REQUIRE": "force"},
    )

    assert not diagnostic.exists()


def test_host_entry_contains_no_browser_automation() -> None:
    source = (ROOT / "scripts" / "utm_image.py").read_text(encoding="utf-8")
    for forbidden in (
        "selenium",
        "appstoreconnect.apple.com",
    ):
        assert forbidden not in source
    assert "ssh_args" in source
    assert "password_environment" in source
    assert "utm_image_source" in source
    for forbidden in (
        "runtime/feishu-runs.json",
        "codex-app-sessions",
        "notify-fault",
        "wait-decision",
    ):
        assert forbidden not in source


def test_guest_script_uses_guest_dependency_and_not_host_source_helper() -> None:
    source = GUEST_SCRIPT.read_text(encoding="utf-8")
    assert "Downloads/Fire_One_en1.3" in source
    assert "connectOverCDP" in source
    assert "expectedEdgePid" in source
    assert "expectedEdgeWebSocket" in source
    assert 'command("/usr/bin/pgrep", ["-x", "Microsoft Edge"])' in source
    assert 'command("/usr/bin/pgrep", ["-f"' not in source
    for forbidden in (
        "SOURCE_HELPER",
        "utm_image_source.py",
        "runtime/utm-19",
    ):
        assert forbidden not in source


def test_guest_ignores_appledouble_metadata_before_image_validation() -> None:
    source = GUEST_SCRIPT.read_text(encoding="utf-8")
    skip_metadata = 'if (path.basename(file).startsWith("._")) continue;'
    extension_check = "const extension = path.extname(file).toLowerCase();"

    assert skip_metadata in source
    assert source.index(skip_metadata) < source.index(extension_check)


def test_guest_gui_actions_use_three_second_settle_and_fresh_state_readback() -> None:
    source = GUEST_SCRIPT.read_text(encoding="utf-8")
    assert "async function settleAfterGuiAction" in source
    assert "await page.waitForTimeout(3_000)" in source
    assert "document.visibilityState" in source
    for stale_short_wait in (
        "waitForTimeout(1_500)",
        "waitForTimeout(1_000)",
        "waitForTimeout(600)",
        "waitForTimeout(400)",
    ):
        assert stale_short_wait not in source


def test_guest_reuses_existing_exact_app_page_before_apps_list_lookup() -> None:
    source = GUEST_SCRIPT.read_text(encoding="utf-8")
    assert "function exactCurrentAppPage" in source
    assert "const exactAppPage = exactCurrentAppPage(connected.context, requested.id);" in source
    assert 'appPageMode = "reused_current_app_page"' in source


def test_skill_contract_has_only_host_ssh_entry() -> None:
    skill = (ROOT / "skills" / "utm-image" / "SKILL.md").read_text(encoding="utf-8")
    assert "$PROJECT_ROOT/scripts/utm_image.py" in skill
    assert "/Users/example/Downloads/AppleAccountScriptsBackup/utm_19_one.mjs" not in skill
    assert "不读取或解释脚本内容" in skill
    assert "printf '%s\\n' '{}' | /usr/bin/node" not in skill


def test_invalid_target_is_rejected_before_ssh() -> None:
    with pytest.raises(utm_image.UTMImageError, match="VM_USER_MISMATCH"):
        utm_image.validate_target("abcd", "192.0.2.10", "wxyz")


def test_host_cli_accepts_mutually_exclusive_run_or_direct_page_identity() -> None:
    parser = utm_image.build_parser()
    action_names = {
        action.dest for action in parser._actions if action.dest != "help"  # noqa: SLF001
    }
    assert action_names == {
        "run_id",
        "page_title",
        "vm_name",
        "vm_ip",
        "vm_user",
        "asset_app_name",
    }
    selector_sets = {
        frozenset(action.dest for action in group._group_actions)  # noqa: SLF001
        for group in parser._mutually_exclusive_groups  # noqa: SLF001
    }
    assert frozenset({"run_id", "page_title"}) in selector_sets

    run_args = parser.parse_args(
        [
            "--run-id",
            "run-12345678",
            "--vm-name",
            "abcd",
            "--vm-ip",
            "192.0.2.10",
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
            "192.0.2.10",
            "--vm-user",
            "abcd",
        ]
    )
    assert direct_args.run_id is None
    assert direct_args.page_title == "Demo-abcd"


def test_main_prints_clean_start_result_and_elapsed(monkeypatch, capsys) -> None:
    monkeypatch.setattr(utm_image, "run", lambda args: 0)
    ticks = iter((200.0, 215.0))
    monkeypatch.setattr(utm_image.time, "monotonic", lambda: next(ticks))
    assert utm_image.main([
        "--page-title", "Demo-abcd",
        "--vm-name", "abcd",
        "--vm-ip", "192.0.2.10",
        "--vm-user", "abcd",
    ]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "开始执行：utm-image",
        "执行成功：utm-image；UTM_19=verified",
    ]


def test_main_preserves_the_complete_actual_error(monkeypatch, capsys) -> None:
    detail = "image source failed: " + "z" * 400
    monkeypatch.setattr(
        utm_image,
        "run",
        lambda args: (_ for _ in ()).throw(utm_image.UTMImageError(detail)),
    )
    assert utm_image.main([
        "--page-title", "Demo-abcd",
        "--vm-name", "abcd",
        "--vm-ip", "192.0.2.10",
        "--vm-user", "abcd",
    ]) == 1
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [f"执行报错：utm-image；{detail}"]


def test_preflight_failure_prevents_guest_execution() -> None:
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"blocked")

    with pytest.raises(utm_image.UTMImageError, match="GUEST_PREFLIGHT_FAILED"):
        utm_image.run_guest_script(
            vm_name="abcd",
            vm_ip="192.0.2.10",
            vm_user="abcd",
            payload={"source": {"url": "https://c.wss.ink/f/secret"}},
            runner=runner,
            password_env={"SSH_ASKPASS_REQUIRE": "force"},
        )

    assert len(calls) == 1
    assert "c.wss.ink" not in " ".join(calls[0][0])


def test_guest_execution_failure_preserves_only_safe_guest_error_marker(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        utm_image,
        "GUEST_DIAGNOSTIC_PATH",
        tmp_path / "utm-image-guest.stderr",
    )
    calls = 0

    def runner(command, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "edge_pid": 123,
                        "edge_websocket": "ws://127.0.0.1:9222/devtools/browser/session",
                    }
                ).encode(),
                stderr=b"",
            )
        return SimpleNamespace(
            returncode=1,
            stdout=b"",
            stderr=b"UTM_19_ERROR=SCREENSHOT_APP_PAGE_COUNT=0\n",
        )

    with pytest.raises(
        utm_image.UTMImageError,
        match=r"^GUEST_EXECUTION_FAILED:SCREENSHOT_APP_PAGE_COUNT=0$",
    ):
        utm_image.run_guest_script(
            vm_name="abcd",
            vm_ip="192.0.2.10",
            vm_user="abcd",
            payload={"source": {}},
            runner=runner,
            password_env={"SSH_ASKPASS_REQUIRE": "force"},
        )

    assert utm_image.GUEST_DIAGNOSTIC_PATH.stat().st_mode & 0o777 == 0o600


DIRECT_CONTEXT_ID = "direct-v1-abcd-0123456789abcdef01234567"


class DirectNotion:
    def __init__(self, app_name: str = "Demo") -> None:
        self.app_name = app_name
        self.calls: list[tuple[str, str]] = []

    def verify_parent(self, title: str) -> str:
        self.calls.append(("verify-parent", title))
        return title

    def read_field(self, page_title: str, section: str, label: str) -> str:
        self.calls.append(("read-field", label))
        if label == "应用名: ":
            return self.app_name
        if label == "APP_ID：":
            return "123456789"
        raise AssertionError(f"unexpected field: {label}")


def _direct_context() -> SimpleNamespace:
    return SimpleNamespace(
        context_id=DIRECT_CONTEXT_ID,
        parent_title="Host",
        page_title="Demo-abcd",
        app_name="Demo",
        vm_name="abcd",
        vm_ip="192.0.2.10",
        vm_user="abcd",
    )


def test_guest_navigates_exact_app_to_inflight_version_before_media_manager() -> None:
    source = GUEST_SCRIPT.read_text(encoding="utf-8")
    assert 'distribution/ios/version/inflight' in source
    assert 'await openAppVersionPage(appPage, appId, displayName);' in source
    assert source.index('await openAppVersionPage(appPage, appId, displayName);') < source.index(
        'await openMediaManager(appPage, displayName);'
    )


def test_guest_recovers_unknown_upload_only_when_page_count_matches() -> None:
    source = GUEST_SCRIPT.read_text(encoding="utf-8")
    assert "readStableScreenshotCount" in source
    assert "stableCount === images.length" in source
    assert "previous upload attempt is unresolved; refusing to upload again" in source


def test_guest_classifies_rejected_dimensions_without_reupload() -> None:
    source = GUEST_SCRIPT.read_text(encoding="utf-8")
    assert "screenshotDimensionsError" in source
    assert "screenshot dimensions are wrong" in source


def test_guest_selects_only_the_display_panel_file_input() -> None:
    source = GUEST_SCRIPT.read_text(encoding="utf-8")
    assert "displayPanel" in source
    assert 'label.locator("xpath=ancestor::button[1]")' in source
    assert 'displayButton.getAttribute("aria-expanded") !== "true"' in source
    assert 'input[type="file"]' in source


def test_guest_normalizes_rejected_source_dimensions_in_a_new_attempt_directory() -> None:
    source = GUEST_SCRIPT.read_text(encoding="utf-8")
    assert "normalizeImagesForDisplay" in source
    assert "rejected_dimensions" in source
    assert 'command("/usr/bin/sips", ["-z"' in source
    assert "normalized" in source


def test_guest_uses_media_manager_accepted_portrait_dimensions() -> None:
    source = GUEST_SCRIPT.read_text(encoding="utf-8")
    assert "acceptedDimensionsFromPanel" in source
    assert "targetDimensions" in source


def test_direct_mode_uses_shared_context_id_without_feishu_run_session_or_card(
    monkeypatch, capsys
) -> None:
    api = DirectNotion()
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        utm_image,
        "resolve_direct_context",
        lambda **kwargs: _direct_context(),
        raising=False,
    )
    monkeypatch.setattr(utm_image, "_verify_vm_started", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        utm_image,
        "sync_utm_image_guest_file",
        lambda *args, **kwargs: None,
    )

    def forbidden(*args, **kwargs):
        pytest.fail("direct context must not read/create a Feishu run/session/card")

    for name in ("find_run", "load_config", "notify_fault", "wait_decision", "create_codex_session"):
        monkeypatch.setattr(utm_image, name, forbidden, raising=False)

    def source_result(app_name: str, context_id: str) -> dict[str, object]:
        observed["source_context"] = (app_name, context_id)
        return {"kind": "share_url", "source_field": "美女截图 链接"}

    monkeypatch.setattr(utm_image, "_source_result", source_result)
    monkeypatch.setattr(
        utm_image,
        "guest_source_payload",
        lambda source, app_name, context_id, **kwargs: {
            "kind": "share_url",
            "sourceField": "美女截图 链接",
            "contextId": context_id,
        },
    )

    def guest_runner(**kwargs):
        observed["guest_payload"] = dict(kwargs["payload"])
        return (
            "LOCAL_EDGE_SESSION=reused",
            "SCREENSHOT_SOURCE_FIELD=beauty_link",
            "SCREENSHOT_PACKAGE=verified",
            "APP_IDENTITY=verified",
            "IPHONE_69_DISPLAY=selected",
            "SCREENSHOT_UPLOAD=already_complete_3_of_10",
            "UTM_19=verified",
        )

    monkeypatch.setattr(utm_image, "run_guest_script", guest_runner)
    args = SimpleNamespace(
        run_id=None,
        page_title="Demo-abcd",
        vm_name="abcd",
        vm_ip="192.0.2.10",
        vm_user="abcd",
    )
    assert utm_image.run(args, api=api) == 0
    assert observed["source_context"] == ("Demo", DIRECT_CONTEXT_ID)
    assert observed["guest_payload"]["runId"] == DIRECT_CONTEXT_ID
    assert observed["guest_payload"]["appName"] == "Demo"
    assert ("verify-parent", "Host") in api.calls
    assert ("read-field", "应用名: ") in api.calls
    assert "UTM_19=verified" in capsys.readouterr().out


def test_run_mode_preserves_owned_feishu_run_id_as_the_existing_namespace(
    monkeypatch,
) -> None:
    api = DirectNotion()
    observed: dict[str, object] = {}
    monkeypatch.setattr(utm_image, "_verify_vm_started", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        utm_image,
        "sync_utm_image_guest_file",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        utm_image,
        "_run_context",
        lambda run_id, vm_name: ("Host", "Demo"),
    )

    def source_result(app_name: str, context_id: str) -> dict[str, object]:
        observed["source_context"] = (app_name, context_id)
        return {"kind": "share_url", "source_field": "美女截图 链接"}

    monkeypatch.setattr(utm_image, "_source_result", source_result)
    monkeypatch.setattr(
        utm_image,
        "guest_source_payload",
        lambda source, app_name, context_id, **kwargs: {
            "kind": "share_url",
            "sourceField": "美女截图 链接",
        },
    )

    def guest_runner(**kwargs):
        observed["guest_payload"] = dict(kwargs["payload"])
        return (
            "LOCAL_EDGE_SESSION=reused",
            "SCREENSHOT_SOURCE_FIELD=beauty_link",
            "SCREENSHOT_PACKAGE=verified",
            "APP_IDENTITY=verified",
            "IPHONE_69_DISPLAY=selected",
            "SCREENSHOT_UPLOAD=already_complete_3_of_10",
            "UTM_19=verified",
        )

    monkeypatch.setattr(utm_image, "run_guest_script", guest_runner)
    args = SimpleNamespace(
        run_id="run-12345678",
        page_title=None,
        vm_name="abcd",
        vm_ip="192.0.2.10",
        vm_user="abcd",
    )
    assert utm_image.run(args, api=api) == 0
    assert observed["source_context"] == ("Demo", "run-12345678")
    assert observed["guest_payload"]["runId"] == "run-12345678"


def test_direct_mode_rejects_notion_app_mismatch_before_source_or_guest_side_effect(
    monkeypatch,
) -> None:
    api = DirectNotion(app_name="Other")
    monkeypatch.setattr(
        utm_image,
        "resolve_direct_context",
        lambda **kwargs: _direct_context(),
        raising=False,
    )
    monkeypatch.setattr(utm_image, "_verify_vm_started", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        utm_image,
        "_source_result",
        lambda *args, **kwargs: pytest.fail("Notion mismatch must block before Feishu Base download"),
    )
    monkeypatch.setattr(
        utm_image,
        "run_guest_script",
        lambda **kwargs: pytest.fail("Notion mismatch must block before guest execution"),
    )
    args = SimpleNamespace(
        run_id=None,
        page_title="Demo-abcd",
        vm_name="abcd",
        vm_ip="192.0.2.10",
        vm_user="abcd",
    )
    with pytest.raises(utm_image.UTMImageError, match="NOTION_PAGE_APP_MISMATCH"):
        utm_image.run(args, api=api)
