from pathlib import Path


GUEST = Path("/Users/example/Downloads/find_system_settings_general.py")


def test_password_change_wakes_change_button_with_literal_suffix_key() -> None:
    source = GUEST.read_text(encoding="utf-8")
    assert "append_password_wakeup_key" in source
    assert 'keystroke "y"' in source
    assert "value[:-1]" in source
    assert "PASSWORD_WAKEUP_Y=verified" in source
    assert 'tell process "System Settings" to keystroke "y"' in source
    assert "CGEventCreateKeyboardEvent" in source
    assert "find_enabled_pressable_text_candidate" in source


def test_password_wakeup_handles_system_events_consent_prompt() -> None:
    source = GUEST.read_text(encoding="utf-8")
    assert "handle_system_events_consent_prompt" in source
    assert "Terminal" in source
    assert "System Events" in source
    assert "subprocess.Popen" in source


def test_password_change_keeps_other_devices_signed_in() -> None:
    source = GUEST.read_text(encoding="utf-8")
    assert "handle_sign_out_other_devices_prompt" in source
    assert "Don't Sign Out" in source
    assert "Sign out other devices using your Apple Account?" in source
    assert 'tree_contains_text(roots, "Sign out other devices")' in source
    assert 'tree_contains_text(roots, "Apple Account")' in source
