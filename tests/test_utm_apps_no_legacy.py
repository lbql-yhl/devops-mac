from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_utm_apps_host_has_no_python_accessibility_recovery() -> None:
    source = (ROOT / "scripts" / "utm_apps.py").read_text(encoding="utf-8")
    for stale in (
        "apple_web_workflow",
        "edge_accessibility",
        "selenium",
        "ApplicationServices",
        "AppKit",
        "Quartz",
        "PLAYWRIGHT_LEDGER_ADOPTION_BRIDGE",
        "PYTHON_GUEST_FILENAMES",
    ):
        assert stale not in source, stale


def test_utm_apps_guest_files_have_no_accessibility_or_python_fallback() -> None:
    combined = "\n".join(
        (ROOT / "scripts" / filename).read_text(encoding="utf-8")
        for filename in (
            "session.mjs",
            "utm_10_login.mjs",
            "utm_11_one.mjs",
            "utm_12_one.mjs",
            "utm_13_one.mjs",
        )
    )
    for stale in (
        "Accessibility",
        "ApplicationServices",
        "AppKit",
        "Quartz",
        "selenium",
        "python3",
        ".py\"",
    ):
        assert stale not in combined, stale


def test_utm_apps_uses_only_the_four_playwright_stage_files() -> None:
    from scripts import utm_apps

    assert utm_apps.GUEST_WRAPPERS == {
        "utm-10": "utm_10_login.mjs",
        "utm-11": "utm_11_one.mjs",
        "utm-12": "utm_12_one.mjs",
        "utm-13": "utm_13_one.mjs",
    }
    assert utm_apps.GUEST_WRAPPER_FILENAMES == (
        "session.mjs",
        "utm_10_login.mjs",
        "utm_11_one.mjs",
        "utm_12_one.mjs",
        "utm_13_one.mjs",
    )


def test_clone_delivery_contains_every_utm_apps_playwright_file() -> None:
    from scripts import utm_apps_delivery

    assert utm_apps_delivery.PLAYWRIGHT_GUEST_FILES == (
        "session.mjs",
        "utm_10_login.mjs",
        "utm_11_one.mjs",
        "utm_12_one.mjs",
        "utm_13_one.mjs",
    )


def test_delivery_does_not_delete_shared_or_guest_legacy_files() -> None:
    source = (ROOT / "scripts" / "utm_apps_delivery.py").read_text(
        encoding="utf-8"
    )
    assert "LEGACY_GUEST_FILES" not in source
    assert "legacy.unlink" not in source
    assert '"/bin/rm -f "' not in source
    assert "/tmp/utm10-native-press" not in source


def test_utm12_does_not_recheck_login() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    assert "input[type='password']" not in source
    assert "input[autocomplete~='username']" not in source
    assert "rerun utm-10 first" not in source


def test_old_python_stage_wrappers_are_removed() -> None:
    for filename in ("utm_10.py", "utm_11.py", "utm_12.py", "utm_13.py"):
        assert not (ROOT / "scripts" / filename).exists(), filename


def test_utm_apps_contracts_name_only_playwright_stage_files() -> None:
    for relative in ("AGENTS.md", "README.md"):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "utm_12.py" not in source, relative
        assert "utm_13.py" not in source, relative
        assert "utm_12_one.mjs" not in source, relative
        assert "utm_13_one.mjs" not in source, relative
        assert "skills/<skill>/SKILL.md" in source, relative
    docs = (ROOT / "docs/utm-apps.md").read_text(encoding="utf-8")
    assert "$PROJECT_ROOT/scripts/utm_apps.py" in docs
    assert "utm_12_one.mjs" not in docs
    assert "utm_13_one.mjs" not in docs
