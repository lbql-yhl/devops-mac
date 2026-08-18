from __future__ import annotations

import ipaddress
import io
import os
import subprocess
import traceback
from pathlib import Path

import pytest
import scripts.utm_clash_ip as clash_module

from scripts.utm_clash_ip import (
    ClashIPError,
    EGRESS_SOURCES,
    ProxyValues,
    parse_proxy_stdin,
    patch_profile_registry,
    patch_top_level_scalars,
    render_headless_runtime_profile,
    render_socks5_profile,
    validate_proxy_source_arguments,
    Runner,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PROXY_VALUE = "proxy-password"


def test_render_profile_has_one_private_proxy_and_proxy_match_rule() -> None:
    values = ProxyValues(
        host=ipaddress.IPv4Address("203.0.113.9"),
        port=1080,
        username="proxy-user",
        password=FIXTURE_PROXY_VALUE,
    )

    rendered = render_socks5_profile(values)

    assert rendered == '''port: 7890
socks-port: 7891
allow-lan: false
mode: rule
log-level: info

dns:
  enable: true
  listen: 0.0.0.0:53
  enhanced-mode: fake-ip
  fake-ip-range: 198.18.0.1/16
  nameserver:
    - 8.8.8.8
    - 1.1.1.1
  fallback:
    - https://dns.google/dns-query
    - https://cloudflare-dns.com/dns-query

proxies:
  - name: "My-SOCKS5-Proxy"
    type: socks5
    server: "203.0.113.9"
    port: 1080
    username: "proxy-user"
    password: "proxy-password"

proxy-groups:
  - name: "PROXY"
    type: select
    proxies:
      - "My-SOCKS5-Proxy"

rules:
  - DOMAIN-SUFFIX,apple.com,PROXY
  - DOMAIN-SUFFIX,icloud.com,PROXY
  - DOMAIN-SUFFIX,mobileme.icloud.com,PROXY
  - DOMAIN-SUFFIX,me.com,PROXY
  - DOMAIN-SUFFIX,mzstatic.com,PROXY
  - DOMAIN-SUFFIX,itunes.apple.com,PROXY
  - DOMAIN-SUFFIX,apps.apple.com,PROXY
  - DOMAIN-SUFFIX,appstoreconnect.apple.com,PROXY
  - DOMAIN-SUFFIX,testflight.apple.com,PROXY
  - DOMAIN-SUFFIX,developer.apple.com,PROXY
  - DOMAIN-KEYWORD,apple,PROXY
  - DOMAIN-KEYWORD,icloud,PROXY
  - DOMAIN-KEYWORD,appstore,PROXY
  - GEOIP,CN,DIRECT
  - MATCH,PROXY
'''


def test_headless_runtime_profile_adds_only_fixed_runtime_scalars() -> None:
    profile = "port: 7890\nsocks-port: 7891\nmode: rule\n"

    rendered = render_headless_runtime_profile(profile)

    assert rendered == (
        "ipv6: false\n"
        "unified-delay: true\n"
        "port: 7890\n"
        "socks-port: 7891\n"
        "mode: rule\n"
    )
    assert rendered.removeprefix(
        "ipv6: false\nunified-delay: true\n"
    ) == profile


def test_notion_titles_default_to_bound_host_and_application_vm_page() -> None:
    resolver = getattr(clash_module, "resolve_notion_titles", None)
    assert resolver is not None

    parent, page = resolver(
        application_name="PanSwap",
        vm_name="stgf",
        parent_title=None,
        page_title=None,
        proxy_stdin=False,
        environment={"SUBMISSION_HOST_MACHINE": "Mac-Studio-01"},
    )

    assert parent == "Mac-Studio-01"
    assert page == "PanSwap-stgf"


def test_notion_titles_reject_a_parent_that_differs_from_bound_host() -> None:
    resolver = getattr(clash_module, "resolve_notion_titles", None)
    assert resolver is not None

    with pytest.raises(ClashIPError, match="^NOTION_PARENT_TITLE_MISMATCH$"):
        resolver(
            application_name="PanSwap",
            vm_name="stgf",
            parent_title="模板",
            page_title="PanSwap-stgf",
            proxy_stdin=False,
            environment={"SUBMISSION_HOST_MACHINE": "Mac-Studio-01"},
        )


def test_patch_top_level_scalars_changes_only_unique_requested_keys() -> None:
    before = """enable_tun_mode: false
enable_system_proxy: true
enable_auto_launch: false
enable_silent_start: false
theme_mode: system
"""

    after = patch_top_level_scalars(
        before,
        {
            "enable_tun_mode": True,
            "enable_system_proxy": False,
            "enable_auto_launch": True,
            "enable_silent_start": True,
        },
    )

    assert "enable_tun_mode: true" in after
    assert "enable_system_proxy: false" in after
    assert "enable_auto_launch: true" in after
    assert "enable_silent_start: true" in after
    assert "theme_mode: system" in after


def test_patch_top_level_scalars_rejects_missing_or_duplicate_keys() -> None:
    with pytest.raises(ValueError, match="missing"):
        patch_top_level_scalars("ipv6: true\n", {"unified-delay": True})

    with pytest.raises(ValueError, match="duplicate"):
        patch_top_level_scalars(
            "ipv6: true\nipv6: false\n", {"ipv6": False}
        )


def test_patch_profile_registry_selects_one_local_profile_idempotently() -> None:
    before = """current: null
chain: []
valid: []
items:
- uid: Merge
  type: merge
  name: Merge
- uid: Script
  type: script
  name: Script
"""

    once = patch_profile_registry(
        before, uid="LjexpSocks5", name="My-SOCKS5-Proxy"
    )
    twice = patch_profile_registry(
        once, uid="LjexpSocks5", name="My-SOCKS5-Proxy"
    )

    assert once == twice
    assert once.count("current: LjexpSocks5") == 1
    assert once.count("- uid: LjexpSocks5") == 1
    assert "  type: local" in once
    assert "  file: LjexpSocks5.yaml" in once
    assert "- uid: Merge" in once
    assert "- uid: Script" in once


def test_patch_profile_registry_accepts_fresh_null_items() -> None:
    before = "# Clash Verge\n\ncurrent: null\nitems: null\n"

    once = patch_profile_registry(
        before, uid="LgdpdSocks5", name="My-SOCKS5-Proxy"
    )
    twice = patch_profile_registry(
        once, uid="LgdpdSocks5", name="My-SOCKS5-Proxy"
    )

    assert once == twice
    assert once.count("current: LgdpdSocks5") == 1
    assert once.count("items:") == 1
    assert "items: null" not in once
    assert once.count("- uid: LgdpdSocks5") == 1


def test_parse_proxy_stdin_accepts_exact_json_without_printing_values() -> None:
    values = parse_proxy_stdin(
        io.StringIO(
            '{"host":"203.0.113.9","port":1080,'
            '"username":"proxy-user","password":"proxy-password"}\n'
        )
    )

    assert values == ProxyValues(
        host=ipaddress.IPv4Address("203.0.113.9"),
        port=1080,
        username="proxy-user",
        password=FIXTURE_PROXY_VALUE,
    )


def test_parse_proxy_stdin_rejects_extra_or_missing_fields() -> None:
    with pytest.raises(ValueError, match="keys"):
        parse_proxy_stdin(
            io.StringIO(
                '{"host":"203.0.113.9","port":1080,'
                '"username":"u","password":"p","extra":"x"}\n'
            )
        )
    with pytest.raises(ValueError, match="keys"):
        parse_proxy_stdin(io.StringIO('{"host":"203.0.113.9"}\n'))


def test_proxy_sources_are_explicit_and_mutually_exclusive() -> None:
    validate_proxy_source_arguments(
        proxy_stdin=True, parent_title=None, page_title=None
    )
    validate_proxy_source_arguments(
        proxy_stdin=False, parent_title="host", page_title="app-gdpd"
    )
    with pytest.raises(ValueError, match="mutually exclusive"):
        validate_proxy_source_arguments(
            proxy_stdin=True, parent_title="host", page_title="app-gdpd"
        )
    with pytest.raises(ValueError, match="required"):
        validate_proxy_source_arguments(
            proxy_stdin=False, parent_title=None, page_title=None
        )


def test_notion_read_field_passes_one_exact_heading_argument(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[list[str]] = []
    values = {
        "代理ip:": "203.0.113.9",
        "代理端口:": "1080",
        "代理用户名：": "proxy-user",
        "代理用户密码：": "proxy-password",
    }

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if "read-field" in command:
            assert command.count("--heading") == 1
            heading = command.index("--heading")
            assert command[heading + 1] == "账号信息"
            label = command[command.index("--label") + 1]
            output = Path(command[command.index("--out") + 1])
            output.write_text(values[label], encoding="utf-8")
            os.chmod(output, 0o600)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = Runner(
        vm_name="gdpd",
        application_name="test",
        vm_ip=None,
        parent_title="host",
        page_title="app-gdpd",
        proxy_stdin=False,
        database=tmp_path / "inventory.sqlite3",
        images_dir=tmp_path / "images",
    )
    values_result = runner.read_notion_proxy(tmp_path)

    assert str(values_result.host) == "203.0.113.9"
    assert values_result.port == 1080
    assert len([command for command in commands if "read-field" in command]) == 4


def test_egress_sources_are_three_command_only_https_fallbacks() -> None:
    assert len(EGRESS_SOURCES) == 3
    assert [name for name, _ in EGRESS_SOURCES] == ["ipify", "ip_sb", "icanhazip"]
    assert all(url.startswith("https://") for _, url in EGRESS_SOURCES)


def test_runner_is_command_only_and_can_start_only_the_exact_bound_vm() -> None:
    text = (ROOT / "scripts" / "utm_clash_ip.py").read_text(encoding="utf-8")

    assert "ensure_exact_vm_started" in text
    assert "require_exact_bound_vm" in text
    assert "--application-name" in text
    assert "--proxy-stdin" in text
    assert "cua-driver" not in text
    assert "utm_clash_ip_ocr" not in text
    assert "VNRecognizeTextRequest" not in text
    assert "get_window_state" not in text
    assert "screenshot_out_file" not in text
    assert "/usr/bin/osascript" not in text
    assert "tell application \"System Events\"" not in text
    assert "BatchMode=yes" not in text
    assert "password_environment()" in text
    assert 'print("SOCKS5_TEMPLATE=exact")' in text
    assert "CONFIG_ROLLBACK=verified" in text
    assert "before_payloads" in text
    assert "CLASH_RECOVERY_RESTART=verified" in text


def test_runner_persists_ui_settings_and_uses_the_official_clash_lifecycle() -> None:
    text = (ROOT / "scripts" / "utm_clash_ip.py").read_text(encoding="utf-8")

    assert "GUEST_CONSOLE=headless_verified" in text
    assert "GUEST_CONSOLE=failed" not in text
    assert "config=base/'config.yaml'" in text
    assert "'enable_tun_mode':True" in text
    assert "'enable_system_proxy':False" in text
    assert "'enable_auto_launch':True" in text
    assert "'enable_silent_start':True" in text
    assert "config_payload=patch_scalars(config.read_text(),{'ipv6':False,'unified-delay':True}).encode()" in text
    assert "/bin/launchctl bootstrap system \"$plist\"" in text
    assert "/usr/bin/open -gja 'Clash Verge'" in text
    assert "CLASH_HELPER=loaded_verified" in text
    assert "CLASH_APP=started_verified" in text
    assert "CLASH_POST_RESTART_SETTINGS=verified" in text
    assert "OFFICIAL_CLASH_LIFECYCLE=verified" in text
    assert "socket='/tmp/verge/verge-mihomo.sock'" in text
    assert 'if self.console_state == "interactive":' in text
    assert "CLASH_HEADLESS_FALLBACK=verified" in text
    assert "HEADLESS_SERVICE_BYPASS" not in text
    assert "HEADLESS_CLASH_START" not in text


def test_headless_start_uses_one_root_lifecycle_with_bounded_repair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = Runner(
        vm_name="gdpd",
        application_name="test",
        vm_ip="192.0.2.10",
        parent_title="host",
        page_title="test-gdpd",
        proxy_stdin=False,
        database=tmp_path / "inventory.sqlite3",
        images_dir=tmp_path / "images",
    )
    runner.console_state = "headless"
    scripts: list[str] = []

    def fake_sudo(script: str, **_: object) -> str:
        scripts.append(script)
        return (
            "CLASH_HEADLESS_REPAIR=verified\n"
            "CLASH_HEADLESS_FALLBACK=verified\n"
        )

    monkeypatch.setattr(runner, "_run_sudo", fake_sudo)

    runner._start_official_clash()

    assert len(scripts) == 1
    assert "/bin/launchctl bootout system" in scripts[0]
    assert "/bin/launchctl bootstrap system" not in scripts[0]
    assert "CLASH_HEADLESS_REPAIR=verified" in scripts[0]
    assert "for attempt in 1 2" in scripts[0]
    output = capsys.readouterr().out
    assert "CLASH_HEADLESS_REPAIR=verified" in output
    assert "CLASH_HEADLESS_FALLBACK=verified" in output


def test_stop_clash_matches_core_with_or_without_arguments() -> None:
    text = (ROOT / "scripts" / "utm_clash_ip.py").read_text(encoding="utf-8")

    assert (
        "for pid in $(/usr/bin/pgrep -f "
        "'^/Applications/Clash Verge[.]app/Contents/MacOS/verge-mihomo( |$)' "
        "|| true);"
    ) in text


def test_headless_verification_requires_helper_absent() -> None:
    text = (ROOT / "scripts" / "utm_clash_ip.py").read_text(encoding="utf-8")

    assert text.count(
        "helper_ok=helper_loaded if console_state=='interactive' else not helper_loaded"
    ) >= 2


def test_embedded_guest_scripts_unpack_only_the_arguments_the_runner_passes() -> None:
    text = (ROOT / "scripts" / "utm_clash_ip.py").read_text(encoding="utf-8")
    probe = text.split("    def _probe_clash_app_data_state", 1)[1].split(
        "    def _write_guest_app_config", 1
    )[0]
    recovery = text.split("    def _verify_config_recovery", 1)[1].split(
        "    def _start_official_clash", 1
    )[0]

    assert "user=sys.argv[1]" in probe
    assert "user,console_state=sys.argv[1:]" in recovery


def test_runner_applies_runtime_tun_and_profile_through_unix_controller() -> None:
    text = (ROOT / "scripts" / "utm_clash_ip.py").read_text(encoding="utf-8")

    assert "def apply_runtime_state" in text
    assert "RUNTIME_COMMAND_STATE=verified" in text
    assert "method='PATCH'" in text
    assert "'/configs'" in text
    assert "method='PUT'" in text
    assert "'/proxies/PROXY'" in text
    assert "self.apply_runtime_state()" in text


def test_runner_rechecks_settings_after_an_independent_app_restart() -> None:
    text = (ROOT / "scripts" / "utm_clash_ip.py").read_text(encoding="utf-8")

    assert "def restart_clash_for_persistence_check" in text
    assert "self.restart_clash_for_persistence_check()" in text
    assert text.count("self.verify_clash()") >= 2
    assert "CLASH_POST_RESTART_SETTINGS=verified" in text


def test_verify_clash_retries_a_transient_read_only_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = Runner(
        vm_name="gdpd",
        application_name="test",
        vm_ip="192.0.2.10",
        parent_title="host",
        page_title="app-gdpd",
        proxy_stdin=False,
        database=tmp_path / "inventory.sqlite3",
        images_dir=tmp_path / "images",
    )
    runner.console_state = "interactive"
    calls = 0
    sleeps: list[int] = []
    success = "\n".join(
        [
            "CLASH=verified",
            "CLASH_PROCESS=verified",
            "CLASH_HELPER=verified",
            "UI_SETTINGS=verified",
            "PROFILE=verified",
            "RUNTIME_TUN=verified",
        ]
    ) + "\n"

    def fake_run_ssh(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise subprocess.CalledProcessError(
                1, ["ssh"], output="", stderr="PROFILE_DELAY=failed\n"
            )
        return subprocess.CompletedProcess(["ssh"], 0, success, "")

    monkeypatch.setattr(runner, "_run_ssh", fake_run_ssh)
    monkeypatch.setattr(clash_module, "sleep", lambda delay: sleeps.append(delay), raising=False)

    runner.verify_clash()

    assert calls == 2
    assert sleeps == [5]
    assert "CLASH_READBACK_RECOVERY=verified" in capsys.readouterr().out


def test_configure_clash_prepares_fresh_app_data_before_config_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner = Runner(
        vm_name="gdpd",
        application_name="test",
        vm_ip="192.0.2.10",
        parent_title="host",
        page_title="app-gdpd",
        proxy_stdin=False,
        database=tmp_path / "inventory.sqlite3",
        images_dir=tmp_path / "images",
    )
    events: list[str] = []

    monkeypatch.setattr(
        runner, "_ensure_clash_app_data", lambda: events.append("ensure-app-data")
    )
    monkeypatch.setattr(runner, "_stop_clash", lambda: events.append("stop"))
    monkeypatch.setattr(
        runner, "_write_guest_app_config", lambda: events.append("write")
    )
    monkeypatch.setattr(
        runner, "_start_official_clash", lambda: events.append("start")
    )
    monkeypatch.setattr(
        runner, "apply_runtime_state", lambda: events.append("runtime")
    )

    runner.configure_clash()

    assert events == ["ensure-app-data", "stop", "write", "start", "runtime"]


def test_fresh_headless_clash_data_stops_without_console_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner = Runner(
        vm_name="gdpd",
        application_name="test",
        vm_ip="192.0.2.10",
        parent_title="host",
        page_title="app-gdpd",
        proxy_stdin=False,
        database=tmp_path / "inventory.sqlite3",
        images_dir=tmp_path / "images",
    )
    runner.console_state = "headless"
    monkeypatch.setattr(
        runner, "_probe_clash_app_data_state", lambda: "absent", raising=False
    )
    monkeypatch.setattr(
        runner,
        "_run_ssh",
        lambda *_args, **_kwargs: pytest.fail("headless bootstrap must not run"),
    )

    with pytest.raises(ClashIPError, match="fresh headless"):
        runner._ensure_clash_app_data()

    source = (ROOT / "scripts" / "utm_clash_ip.py").read_text(encoding="utf-8")
    assert "def _auto_login_console" not in source


def test_run_sudo_preserves_sanitized_error_and_redacts_exception_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from scripts import utm_post_clone_guest

    runner = Runner(
        vm_name="gdpd",
        application_name="test",
        vm_ip="192.0.2.10",
        parent_title="host",
        page_title="app-gdpd",
        proxy_stdin=False,
        database=tmp_path / "inventory.sqlite3",
        images_dir=tmp_path / "images",
    )
    sentinel = "SENSITIVE_SENTINEL"
    script_body = "PRIVATE_SCRIPT_BODY"

    def fake_sudo(*_: object, **__: object) -> str:
        timeout = subprocess.TimeoutExpired(
            ["ssh", f"sudo script={sentinel}"], 150
        )
        raise utm_post_clone_guest.GuestAutomationError(
            "privileged guest command timed out"
        ) from timeout

    monkeypatch.setattr(utm_post_clone_guest, "ssh_sudo_script", fake_sudo)

    with pytest.raises(
        ClashIPError,
        match=(
            r"^guest privileged command failed: "
            r"privileged guest command timed out$"
        ),
    ) as info:
        runner._run_sudo(script_body)

    rendered = "".join(
        traceback.format_exception(
            type(info.value), info.value, info.value.__traceback__
        )
    )
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True
    assert sentinel not in rendered
    assert script_body not in rendered


def test_configure_clash_recovers_one_verified_rollback_in_same_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = Runner(
        vm_name="gdpd",
        application_name="test",
        vm_ip="192.0.2.10",
        parent_title="host",
        page_title="app-gdpd",
        proxy_stdin=False,
        database=tmp_path / "inventory.sqlite3",
        images_dir=tmp_path / "images",
    )
    events: list[str] = []
    write_attempts = 0

    def fake_run_ssh(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        nonlocal write_attempts
        write_attempts += 1
        events.append(f"write-{write_attempts}")
        if write_attempts == 1:
            raise subprocess.CalledProcessError(
                91,
                ["ssh"],
                output="CONFIG_ROLLBACK=verified\n",
                stderr="CONFIG_FAILURE_STAGE=write_profile\n",
            )
        return subprocess.CompletedProcess(
            ["ssh"],
            0,
            "UI_PERSISTENT_SETTINGS=verified\nCONFIG_WRITE=atomic_verified\n",
            "",
        )

    monkeypatch.setattr(runner, "_run_ssh", fake_run_ssh)
    monkeypatch.setattr(runner, "_ensure_clash_app_data", lambda: None, raising=False)
    monkeypatch.setattr(runner, "_stop_clash", lambda: events.append("stop"))
    monkeypatch.setattr(
        runner, "_start_official_clash", lambda: events.append("start")
    )
    monkeypatch.setattr(
        runner,
        "_verify_config_recovery",
        lambda: events.append("diagnose"),
        raising=False,
    )
    monkeypatch.setattr(
        runner, "apply_runtime_state", lambda: events.append("runtime")
    )

    runner.configure_clash()

    assert events == [
        "stop",
        "write-1",
        "start",
        "diagnose",
        "stop",
        "write-2",
        "start",
        "runtime",
    ]
    assert write_attempts == 2
    output = capsys.readouterr().out
    assert "CONFIG_FAILURE_STAGE=write_profile" in output
    assert "CONFIG_ROLLBACK=verified" in output
    assert "CONFIG_RECOVERY_DIAGNOSTICS=verified" in output
    assert "CONFIG_REPAIR_RETRY=verified" in output


def test_configure_clash_does_not_retry_an_unclassified_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner = Runner(
        vm_name="gdpd",
        application_name="test",
        vm_ip="192.0.2.10",
        parent_title="host",
        page_title="app-gdpd",
        proxy_stdin=False,
        database=tmp_path / "inventory.sqlite3",
        images_dir=tmp_path / "images",
    )
    events: list[str] = []

    def fake_run_ssh(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        events.append("write")
        raise subprocess.CalledProcessError(
            91,
            ["ssh"],
            output="CONFIG_ROLLBACK=verified\n",
            stderr="CONFIG_FAILURE_STAGE=untrusted-stage\n",
        )

    monkeypatch.setattr(runner, "_run_ssh", fake_run_ssh)
    monkeypatch.setattr(runner, "_ensure_clash_app_data", lambda: None, raising=False)
    monkeypatch.setattr(runner, "_stop_clash", lambda: events.append("stop"))
    monkeypatch.setattr(runner, "_start_official_clash", lambda: events.append("start"))
    monkeypatch.setattr(
        runner, "_verify_config_recovery", lambda: events.append("diagnose")
    )

    with pytest.raises(ClashIPError, match="unknown"):
        runner.configure_clash()

    assert events == ["stop", "write"]


def test_configure_clash_never_attempts_a_third_config_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = Runner(
        vm_name="gdpd",
        application_name="test",
        vm_ip="192.0.2.10",
        parent_title="host",
        page_title="app-gdpd",
        proxy_stdin=False,
        database=tmp_path / "inventory.sqlite3",
        images_dir=tmp_path / "images",
    )
    writes = 0
    events: list[str] = []

    def fake_run_ssh(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        nonlocal writes
        writes += 1
        events.append(f"write-{writes}")
        raise subprocess.CalledProcessError(
            91,
            ["ssh"],
            output="CONFIG_ROLLBACK=verified\n",
            stderr="CONFIG_FAILURE_STAGE=write_profile\n",
        )

    monkeypatch.setattr(runner, "_run_ssh", fake_run_ssh)
    monkeypatch.setattr(runner, "_ensure_clash_app_data", lambda: None, raising=False)
    monkeypatch.setattr(runner, "_stop_clash", lambda: events.append("stop"))
    monkeypatch.setattr(runner, "_start_official_clash", lambda: events.append("start"))
    monkeypatch.setattr(
        runner, "_verify_config_recovery", lambda: events.append("diagnose")
    )

    with pytest.raises(ClashIPError, match="write_profile"):
        runner.configure_clash()

    assert writes == 2
    assert events == [
        "stop",
        "write-1",
        "start",
        "diagnose",
        "stop",
        "write-2",
        "start",
    ]
    output = capsys.readouterr().out
    assert "CONFIG_REPAIR_RETRY=failed" in output
    assert "CONFIG_REPAIR_RETRY_LIMIT=exhausted" in output


def test_config_recovery_diagnostics_reject_unexpected_guest_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner = Runner(
        vm_name="gdpd",
        application_name="test",
        vm_ip="192.0.2.10",
        parent_title="host",
        page_title="app-gdpd",
        proxy_stdin=False,
        database=tmp_path / "inventory.sqlite3",
        images_dir=tmp_path / "images",
    )
    output = (
        "CONFIG_RECOVERY_DIAGNOSTIC_1=verified\n"
        "CONFIG_RECOVERY_DIAGNOSTIC_2=verified\n"
        "CONFIG_RECOVERY_DIAGNOSTIC_3=verified\n"
        "UNEXPECTED_GUEST_OUTPUT=present\n"
    )
    monkeypatch.setattr(
        runner,
        "_run_ssh",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["ssh"], 0, output, ""
        ),
    )

    with pytest.raises(ClashIPError, match="diagnostics failed"):
        runner._verify_config_recovery()


def test_runner_leaves_system_dns_untouched_with_official_tun() -> None:
    text = (ROOT / "scripts" / "utm_clash_ip.py").read_text(encoding="utf-8")

    assert "def configure_guest_dns" not in text
    assert "def restore_guest_dns" not in text
    assert "def commit_guest_dns" not in text
    assert "-setdnsservers" not in text
    assert "GUEST_DNS=loopback_verified" not in text
    assert "GUEST_DNS_ROLLBACK=verified" not in text
    assert "GUEST_DNS_STATE=committed" not in text
    assert "SYSTEM_DNS=unchanged_verified" in text
    assert "'dns-hijack':['any:53','tcp://any:53']" in text


def test_visual_ocr_helper_has_been_removed() -> None:
    assert not (ROOT / "scripts" / "utm_clash_ip_ocr.swift").exists()
