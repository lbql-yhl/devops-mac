from pathlib import Path
import inspect
import plistlib
import subprocess
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scripts.utm_post_clone as post_clone  # noqa: E402

from scripts.utm_post_clone import (  # noqa: E402
    PostCloneError,
    choose_unique_ip,
    desktop_state_verified,
    demo_cleanup_script,
    demo_cleanup_verified,
    demo_retirement_script,
    complete_setup_assistant_script,
    manifest,
    settings_verified,
    setup_assistant_script,
    setup_assistant_verified,
    guest_shutdown_script,
    _settings_read_script,
    _settings_write_script,
    target_bundle,
)


def test_target_bundle_is_exact_and_non_symlink(tmp_path: Path) -> None:
    images = tmp_path / "images"
    images.mkdir()
    bundle = images / "yhlw.utm"
    bundle.mkdir()
    (bundle / "config.plist").write_bytes(
        plistlib.dumps(
            {
                "Information": {"UUID": "11111111-1111-1111-1111-111111111111"},
                "Network": [{"MacAddress": "02:00:00:00:00:01"}],
            }
        )
    )
    assert target_bundle(images, "yhlw") == bundle.resolve()
    try:
        target_bundle(images, "abcd")
    except PostCloneError as error:
        assert "bundle" in str(error)
    else:
        raise AssertionError("missing exact target was accepted")


def test_run_reports_child_stderr_instead_of_hiding_root_cause(monkeypatch) -> None:
    def failed_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0],
            1,
            stdout="UTM_TARGET_STATUS_READS=stopped,stopped\n",
            stderr="UTM_1_AX=blocked: SHARING_BUTTON_COUNT=0\n",
        )

    monkeypatch.setattr(post_clone.subprocess, "run", failed_run)
    try:
        post_clone._run(["utm-1-test"])
    except PostCloneError as error:
        assert "SHARING_BUTTON_COUNT=0" in str(error)
    else:
        raise AssertionError("child stderr root cause was hidden")


def test_hardware_setup_persists_randomized_mac_before_dependency_checks(
    monkeypatch, tmp_path
) -> None:
    database = tmp_path / "inventory.sqlite3"
    bundle = tmp_path / "rbqi.utm"
    events: list[tuple[object, ...]] = []
    args = SimpleNamespace(
        vm_name="rbqi",
        shared_dir=tmp_path / "shared",
        images_dir=tmp_path,
        database=database,
    )

    monkeypatch.setattr(post_clone, "ensure_stopped", lambda name: events.append(("stop", name)))
    monkeypatch.setattr(post_clone, "_run", lambda command: events.append(("utm-1", tuple(command))))
    monkeypatch.setattr(
        post_clone,
        "config_identity",
        lambda _bundle: ("FC31DB80-A5BB-46D3-B323-D64E734F9CDD", "0e:63:02:91:b7:2a"),
    )
    monkeypatch.setattr(
        post_clone,
        "update_guest_mac",
        lambda db, name, mac: events.append(("inventory", db, name, mac)),
        raising=False,
    )

    evidence = post_clone.perform_hardware_setup(
        args, bundle, "FC31DB80-A5BB-46D3-B323-D64E734F9CDD"
    )

    assert ("inventory", database, "rbqi", "0e:63:02:91:b7:2a") in events
    assert evidence["mac"] == "0e:63:02:91:b7:2a"


def test_ensure_started_uses_exact_ax_recovery_for_stopped_vm(monkeypatch) -> None:
    states = iter(["stopped", "starting", "running"])
    events: list[str] = []

    monkeypatch.setattr(post_clone, "_status", lambda _vm_name: next(states))
    monkeypatch.setattr(post_clone, "_start_stopped_vm", events.append)
    monkeypatch.setattr(post_clone.time, "sleep", lambda _seconds: None)

    post_clone.ensure_started("rbqi")

    assert events == ["rbqi"]


def test_ax_start_recovery_locks_exact_bundle_window_and_run_button() -> None:
    source = inspect.getsource(post_clone._start_stopped_vm)

    assert "target_bundle(VM_IMAGES_DIR, vm_name)" in source
    assert "kAXIdentifierAttribute" in source
    assert "play.circle.fill" in source
    assert "kAXPressAction" in source
    assert "if not target_windows" in source
    assert "len(target_windows) != 1" not in source


def test_ax_start_prefers_exact_library_detail_over_console_control() -> None:
    controls = [
        {
            "window_title": "UTM – rbqi",
            "identifier": "play.circle.fill",
            "description": "运行",
            "node": "library",
        },
        {
            "window_title": "rbqi",
            "identifier": "play.circle.fill",
            "description": "开始",
            "node": "console",
        },
    ]

    assert post_clone._choose_utm_run_control(controls, "rbqi") == "library"


def test_ax_start_prefers_unique_target_toolbar_control_over_sidebar_controls() -> None:
    controls = [
        {
            "window_title": "UTM – jaux",
            "identifier": "play",
            "description": "运行",
            "container_role": "AXToolbar",
            "node": "target-toolbar",
        },
        *[
            {
                "window_title": "UTM – jaux",
                "identifier": "play.circle.fill",
                "description": "运行",
                "container_role": "AXCell",
                "node": f"sidebar-{index}",
            }
            for index in range(18)
        ],
    ]

    assert post_clone._choose_utm_run_control(controls, "jaux") == "target-toolbar"


def test_clash_bootstrap_preserves_launchservices_error_detail() -> None:
    source = inspect.getsource(post_clone.bootstrap_clash_app_data)

    assert "result.stderr" in source
    assert "result.stdout" in source
    assert "result.returncode" in source
    assert "CLASH_APP_BOOTSTRAP=launch_failed:" in source


def test_clash_bootstrap_restarts_stalled_hidden_app_before_initialization() -> None:
    source = inspect.getsource(post_clone.bootstrap_clash_app_data)

    assert "stalled_pids" in source
    assert "os.kill(pid, signal.SIGTERM)" in source
    assert "['/usr/bin/open',str(app)]" in source
    assert "['/usr/bin/open','-g',str(app)]" not in source
    assert "['/usr/bin/open','-gja',str(app)]" not in source
    assert "for _ in range(20):" in source
    assert "seed_payloads" in source
    assert "enable_tun_mode: false" in source
    assert "ipv6: false" in source
    assert "current: null" in source
    assert "os.replace(temporary,path)" in source


def test_choose_unique_ip_requires_exact_intersection() -> None:
    assert choose_unique_ip(["192.0.2.10"], ["192.0.2.10"]) == "192.0.2.10"
    for utm, arp in (([], ["192.0.2.10"]), (["192.0.2.10", "192.0.2.11"], ["192.0.2.10", "192.0.2.11"])):
        try:
            choose_unique_ip(utm, arp)
        except PostCloneError as error:
            assert "IP_MATCH_COUNT" in str(error)
        else:
            raise AssertionError("non-unique IP intersection was accepted")


def test_settings_verified_requires_all_zero_values() -> None:
    values = {
        "AutomaticCheckEnabled": "0",
        "AutomaticDownload": "0",
        "AutomaticallyInstallMacOSUpdates": "0",
        "AutomaticallyInstallAppUpdates": "0",
        "CriticalUpdateInstall": "0",
        "ConfigDataInstall": "0",
        "AutoUpdate": "0",
        "sleep": "0",
        "displaysleep": "0",
        "disksleep": "0",
        "screensaver_idle": "0",
        "askForPassword": "0",
        "screenLock": "off",
        "LOCK_SCREEN_SHOW_CLOCK": "0",
        "LOCK_SCREEN_SHOW_24_HOUR": "0",
        "LOCK_SCREEN_SHOW_USER_PHOTO": "0",
        "LOCK_SCREEN_SHOW_PASSWORD_HINTS": "0",
        "LOCK_SCREEN_SHOW_MESSAGE": "0",
        "LOCK_SCREEN_SHOW_POWER_BUTTONS": "0",
    }
    assert settings_verified(values)
    values["screenLock"] = "300"
    assert not settings_verified(values)


def test_lock_screen_settings_are_explicitly_disabled() -> None:
    write_script = _settings_write_script()
    read_script = _settings_read_script()
    # These are the values represented by the Lock Screen pane: no screen
    # saver/display timeout, no password prompt, and no auxiliary lock-screen
    # switches that can re-enable an interactive lock flow.
    for marker in (
        "LOCK_SCREEN_SHOW_CLOCK=0",
        "LOCK_SCREEN_SHOW_24_HOUR=0",
        "LOCK_SCREEN_SHOW_USER_PHOTO=0",
        "LOCK_SCREEN_SHOW_PASSWORD_HINTS=0",
        "LOCK_SCREEN_SHOW_MESSAGE=0",
        "LOCK_SCREEN_SHOW_POWER_BUTTONS=0",
    ):
        assert marker in write_script
        assert marker.split("=", 1)[0] in read_script
    assert "/usr/sbin/sysadminctl -screenLock status" in read_script
    assert "screenLock=off" in read_script
    assert "disable_screen_lock(vm_name, ip)" in inspect.getsource(
        __import__("scripts.utm_vm_clone_steps", fromlist=["run_settings"]).run_settings
    )


def test_lock_screen_preferences_are_written_for_console_user() -> None:
    script = _settings_write_script()
    assert "SUDO_USER" in script
    assert "sudo -u" in script
    assert "SUDO_USER" in _settings_read_script()


def test_manifest_detects_hidden_files_and_content_changes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / ".hidden").write_text("one", encoding="utf-8")
    (source / "nested").mkdir()
    (source / "nested" / "file").write_text("two", encoding="utf-8")
    assert manifest(source) == manifest(source)
    (destination / ".hidden").write_text("one", encoding="utf-8")
    (destination / "nested").mkdir()
    (destination / "nested" / "file").write_text("different", encoding="utf-8")
    assert manifest(source) != manifest(destination)


def test_setup_assistant_probe_never_requests_guest_tcc() -> None:
    script = setup_assistant_script()
    assert "System Events" not in script
    assert "osascript" not in script
    assert "pgrep -u" in script
    assert 'pgrep -x "Setup Assistant"' not in script
    assert "-MiniBuddyYes" in script
    assert setup_assistant_verified("SETUP_ASSISTANT=absent\nFINDER=ready\n")
    assert setup_assistant_verified("SETUP_ASSISTANT_ACTION=host_input_not_now\nSETUP_ASSISTANT=absent\nFINDER=ready\n")
    assert not setup_assistant_verified("SETUP_ASSISTANT_ACTION=not_now_clicked\nFINDER=ready\n")


def test_minibuddy_completion_is_command_verified_without_tcc() -> None:
    script = complete_setup_assistant_script()
    for marker in (
        "/var/db/.AppleSetupDone",
        "-MiniBuddyYes",
        "DidSeeAccessibility",
        "MiniBuddyShouldLaunchToResumeSetup",
        "kill -TERM",
        "SETUP_ASSISTANT=absent",
    ):
        assert marker in script
    assert "open -a Finder" not in script
    assert "System Events" not in script
    assert "osascript" not in script
    assert "pgrep -u" in script
    assert 'pgrep -x "Setup Assistant"' not in script
    assert hasattr(post_clone, "launch_finder_script")
    launcher = post_clone.launch_finder_script()
    for marker in (
        "launchctl asuser",
        'sudo -u "$target_user"',
        "open -a Finder",
        "FINDER=ready",
    ):
        assert marker in launcher
    desktop_source = inspect.getsource(post_clone._guest_enter_desktop)
    assert "complete_setup_assistant_script()" in desktop_source
    assert "launch_finder_script()" in desktop_source
    assert "ssh_sudo_script(" in desktop_source


def test_guest_window_points_are_normalized_and_bounded() -> None:
    assert hasattr(post_clone, "guest_window_point")
    guest_window_point = post_clone.guest_window_point
    bounds = (100.0, 200.0, 1000.0, 800.0)
    assert guest_window_point(bounds, 0.50, 0.08) == (600.0, 264.0)
    assert guest_window_point(bounds, 0.76, 0.80) == (860.0, 840.0)
    for invalid in ((0.0, 0.5), (1.0, 0.5), (0.5, 0.0), (0.5, 1.0)):
        try:
            guest_window_point(bounds, *invalid)
        except PostCloneError:
            pass
        else:
            raise AssertionError("unsafe edge point was accepted")


def test_desktop_flow_uses_exact_host_ax_target_and_no_guest_apple_events() -> None:
    focus_source = inspect.getsource(post_clone._focus_utm_target)
    desktop_source = inspect.getsource(post_clone._enter_guest_desktop)
    assert "exact_guest_window" in focus_source
    assert "exact_utm_window" not in focus_source
    assert "kAXFrontmostAttribute" in focus_source
    assert "System Events" not in desktop_source
    assert "osascript" not in desktop_source
    assert "_allow_guest_accessibility" not in desktop_source


def test_final_admin_uses_fixed_password_ssh_without_key_install() -> None:
    source = inspect.getsource(post_clone.perform_account_setup)
    assert "ensure_ssh(" in source
    assert "ssh-copy-id" not in source
    assert "authorized_keys" not in source


def test_guest_desktop_verification_requires_exact_console_user() -> None:
    ready = "CONSOLE_USER=yhlw\nFINDER=ready\nSETUP_ASSISTANT=absent\n"
    assert desktop_state_verified(ready, "yhlw")
    assert not desktop_state_verified(ready.replace("yhlw", "root"), "yhlw")
    assert not desktop_state_verified("CONSOLE_USER=yhlw\nFINDER=ready\n", "yhlw")


def test_final_shutdown_uses_guest_macos_shutdown_command() -> None:
    script = guest_shutdown_script()
    assert "/sbin/shutdown -h now" in script
    assert "shutdown -r" not in script


def test_demo_cleanup_checks_account_process_and_home_path() -> None:
    script = demo_cleanup_script()
    assert "pgrep -u demo" in script
    assert "id demo" in script
    assert 'demo_home=' in script
    assert 'dscl . -read "$demo_home"' in script
    assert "DEMO_ID=absent" in script
    assert "DEMO_DSCL=absent" in script
    assert "DEMO_HOME=absent" in script


def test_demo_cleanup_removes_clash_desired_state_that_recreates_demo_home() -> None:
    script = demo_cleanup_script()
    for required in (
        "/var/root/.local/state/clash-verge-service/desired-state.json",
        "core_should_be_running",
        "config_dir",
        '"$demo_home/"',
        "launchctl bootout system",
        "CLASH_STALE_STATE=absent",
        "STALE_DEMO_PATH_PROCESS=absent",
    ):
        assert required in script

    assert 'launchctl bootout system "$clash_plist"' in script
    assert 'launchctl disable "system/$clash_label"' in script
    assert 'launchctl bootout "system/$clash_label"' in script
    assert 'launchctl enable "system/$clash_label"' not in script
    assert 'launchctl bootstrap system "$clash_plist"' not in script
    assert script.index('launchctl bootout system "$clash_plist"') < script.index(
        'pkill -TERM -f "$stale_demo_pattern"'
    )

    assert 'live_pids()' in script
    assert '/bin/kill -TERM $stale_pids' in script
    assert '/bin/kill -KILL $stale_pids' in script
    assert '/bin/kill -TERM $app_pids' in script
    assert '/bin/kill -KILL $app_pids' in script
    assert "Z*|'')" in script
    assert 'for attempt in 1 2 3 4 5 6 7 8 9 10' in script
    assert script.index('/bin/rm -f -- "$clash_state" "$clash_runtime"') < script.index(
        '/bin/kill -TERM $stale_pids'
    )
    assert "killall" not in script

    old_success = (
        "DEMO_ID=absent\nDEMO_DSCL=absent\n"
        "DEMO_STATE=absent\nDEMO_HOME=absent\n"
    )
    assert not demo_cleanup_verified(old_success)
    assert demo_cleanup_verified(
        old_success
        + "CLASH_STALE_STATE=absent\n"
        + "STALE_DEMO_PATH_PROCESS=absent\n"
    )


def test_demo_retirement_stops_only_the_logged_out_demo_user_domain() -> None:
    script = demo_retirement_script()
    assert "CONSOLE_USER" in script
    assert "user/$demo_uid" in script
    assert "launchctl bootout" in script
    assert "pgrep -u demo" in script
    assert 'target_user="${SUDO_USER:-}"' in script
    assert 'sysadminctl -deleteUser demo -secure -adminUser "$target_user" -adminPassword -' in script
    assert "killall" not in script
    phase_source = inspect.getsource(post_clone.perform_account_setup)
    assert "demo_retirement_script()" in phase_source


def test_stale_demo_clash_cleanup_failure_is_recovered_by_guest_reboot() -> None:
    source = inspect.getsource(post_clone.perform_account_setup)
    assert "STALE_DEMO_PATH_PROCESS=present" in source
    assert "guest_shutdown_script()" in source
    assert "ensure_stopped(args.vm_name)" in source
    assert "ensure_started(args.vm_name)" in source


def test_account_probe_repairs_only_residual_demo_home_before_verification() -> None:
    source = inspect.getsource(post_clone.perform_account_setup)
    assert "residual_demo_home_cleanup_script()" in source
    assert source.index("residual_demo_home_cleanup_script()") < source.index(
        "guest_shutdown_script()"
    )


def test_main_uses_vm_name_from_current_workflow_environment(monkeypatch) -> None:
    observed: dict[str, str] = {}

    monkeypatch.setenv("VM_NAME", "abcd")
    monkeypatch.setattr(sys, "argv", [str(Path(post_clone.__file__).resolve())])
    monkeypatch.setattr(post_clone, "run_step_handoff", lambda vm_name: observed.update(vm_name=vm_name))

    assert post_clone.main() == 0
    assert observed == {"vm_name": "abcd"}


def test_cli_vm_name_still_overrides_environment(monkeypatch) -> None:
    observed: dict[str, str] = {}

    monkeypatch.setenv("VM_NAME", "abcd")
    monkeypatch.setattr(
        sys,
        "argv",
        [str(Path(post_clone.__file__).resolve()), "--vm-name", "wxyz"],
    )
    monkeypatch.setattr(post_clone, "run_step_handoff", lambda vm_name: observed.update(vm_name=vm_name))

    assert post_clone.main() == 0
    assert observed["vm_name"] == "wxyz"
