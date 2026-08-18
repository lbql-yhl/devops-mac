from __future__ import annotations

import importlib.util
import json
import plistlib
import sqlite3
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "utm_vm_clone_accessibility.py"
VISION = ROOT / "scripts" / "utm_vm_clone_accessibility_vision.swift"


def load_module():
    assert SCRIPT.is_file(), "clone accessibility implementation is missing"
    spec = importlib.util.spec_from_file_location("utm_vm_clone_accessibility", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_inventory(
    tmp_path: Path,
    *,
    vm_name: str = "ocww",
    directory_present: int = 1,
    registered_mac: str | None = None,
) -> tuple[Path, Path]:
    images = tmp_path / "images"
    bundle = images / f"{vm_name}.utm"
    bundle.mkdir(parents=True)
    config_uuid = "246622A7-2BA6-4B91-BCEA-8E0A38A66C89"
    mac = "46:3f:6d:57:f7:03"
    (bundle / "config.plist").write_bytes(
        plistlib.dumps(
            {
                "Information": {"UUID": config_uuid},
                "Network": [{"MacAddress": mac}],
            }
        )
    )
    database = tmp_path / "vm-inventory.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE vm_inventory (
               vm_name TEXT PRIMARY KEY, config_uuid TEXT, bundle_path TEXT,
               mac_address TEXT, status TEXT, directory_present INTEGER
            )"""
        )
        connection.execute(
            "INSERT INTO vm_inventory VALUES (?, ?, ?, ?, 'complete', ?)",
            (vm_name, config_uuid, str(bundle), registered_mac or mac, directory_present),
        )
    return database, images


def test_clone_accessibility_artifacts_exist() -> None:
    assert SCRIPT.is_file()
    assert VISION.is_file()


def test_vm_name_and_status_are_strict() -> None:
    module = load_module()
    assert module.validate_vm_name("ocww") == "ocww"
    for value in ("OCWW", "ocw", "ocwww", "latest", "oc-w"):
        with pytest.raises(module.UTMAError):
            module.validate_vm_name(value)
    assert module.parse_utm_status("started\n") == "started"
    assert module.parse_utm_status("running\n") == "running"
    for value in ("stopped\n", "suspended\n", "started\nrunning\n", ""):
        with pytest.raises(module.UTMAError):
            module.parse_utm_status(value)


def test_inventory_resolution_is_read_only_and_identity_locked(tmp_path: Path) -> None:
    module = load_module()
    database, images = make_inventory(tmp_path)
    before = database.read_bytes()
    target = module.resolve_inventory_target(database, images, "ocww")
    assert target.vm_name == "ocww"
    assert target.config_uuid == "246622A7-2BA6-4B91-BCEA-8E0A38A66C89"
    assert target.mac_address == "46:3f:6d:57:f7:03"
    assert target.bundle_path == (images / "ocww.utm").resolve()
    assert database.read_bytes() == before

    config = target.bundle_path / "config.plist"
    payload = plistlib.loads(config.read_bytes())
    payload["Network"][0]["MacAddress"] = "46:3f:6d:57:f7:04"
    config.write_bytes(plistlib.dumps(payload))
    with pytest.raises(module.UTMAError, match="MAC"):
        module.resolve_inventory_target(database, images, "ocww")


def test_clone_step_accepts_only_the_exact_active_incomplete_clone(tmp_path: Path) -> None:
    module = load_module()
    database, images = make_inventory(
        tmp_path,
        directory_present=0,
        registered_mac="46:3f:6d:57:f7:02",
    )
    bundle = (images / "ocww.utm").resolve()
    active = tmp_path / "utm-vm-clone-active.json"
    active.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "vm_name": "ocww",
                "config_uuid": "246622A7-2BA6-4B91-BCEA-8E0A38A66C89",
                "bundle_path": str(bundle),
            }
        ),
        encoding="utf-8",
    )
    state = tmp_path / "ocww.json"
    state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "vm_name": "ocww",
                "bundle": str(bundle),
                "config_uuid": "246622A7-2BA6-4B91-BCEA-8E0A38A66C89",
                "steps": [2, 3, 4, 5, 6],
            }
        ),
        encoding="utf-8",
    )
    state.chmod(0o600)

    target = module.resolve_inventory_target(
        database,
        images,
        "ocww",
        active_workflow_path=active,
        state_path=state,
    )
    assert target.mac_address == "46:3f:6d:57:f7:03"

    active.write_text(
        active.read_text(encoding="utf-8").replace("ocww", "zzzz"),
        encoding="utf-8",
    )
    with pytest.raises(module.UTMAError, match="active"):
        module.resolve_inventory_target(
            database,
            images,
            "ocww",
            active_workflow_path=active,
            state_path=state,
        )


def test_ip_candidates_require_registered_mac_intersection() -> None:
    module = load_module()
    utm_output = "192.168.64.48\n192.168.64.49\n"
    arp_output = "? (192.168.64.48) at 46:3f:6d:57:f7:03 on bridge100\n"
    assert module.match_registered_ip(
        utm_output, arp_output, "46:3f:6d:57:f7:03"
    ) == "192.168.64.48"
    with pytest.raises(module.UTMAError):
        module.match_registered_ip(utm_output, "", "46:3f:6d:57:f7:03")


def test_ip_candidate_falls_back_to_unique_registered_mac_when_utm_backend_has_no_ip() -> None:
    module = load_module()
    arp_output = "? (192.168.64.48) at 46:3f:6d:57:f7:03 on bridge100\n"
    assert module.match_registered_ip(
        "", arp_output, "46:3f:6d:57:f7:03"
    ) == "192.168.64.48"


def test_password_prompt_is_unique_and_redacted() -> None:
    module = load_module()
    synthetic_value = "9072"
    module.guest_password = lambda: synthetic_value
    count, delta = module.password_prompt_delta(b"ocww@host's password:", 0)
    assert (count, delta) == (1, 1)
    with pytest.raises(module.UTMAError):
        module.password_prompt_delta(b"password:\npassword:", 0)
    public = module.redact_ssh_transcript(
        b"ocww@host's password:\r\nIDENTITY=verified\r\n"
    )
    assert public == "IDENTITY=verified"
    assert "password" not in public.casefold()
    assert synthetic_value not in public
    assert module.has_marker(
        "Warning: host key accepted\nSETTINGS_OPENED=1", "SETTINGS_OPENED=1"
    )


def test_run_ssh_uses_configured_password_askpass_without_a_pty(monkeypatch) -> None:
    module = load_module()
    monkeypatch.setenv("SUBMISSION_GUEST_PASSWORD", "9072")
    command = f"{sys.executable} -c \"print('MARKER=verified')\""
    monkeypatch.setattr(
        module,
        "_ssh_arguments",
        lambda _user, _ip, _remote: ["/bin/zsh", "-lc", command],
    )

    assert module.run_ssh("ocww", "192.168.64.48", "ignored") == "MARKER=verified"
    source = (ROOT / "scripts" / "utm_vm_clone_accessibility.py").read_text(
        encoding="utf-8"
    )
    assert "password_environment" in source
    assert "pty.openpty" not in source


def test_accessibility_recovery_cancels_stale_file_picker_before_reopening_settings(
    monkeypatch,
) -> None:
    module = load_module()
    observations = [
        module.OCRObservation("Applications", 1.0, module.PixelBox(10, 10, 90, 30)),
        module.OCRObservation("Cancel", 1.0, module.PixelBox(100, 100, 150, 125)),
        module.OCRObservation("Open", 1.0, module.PixelBox(170, 100, 210, 125)),
    ]
    keys: list[int] = []
    monkeypatch.setattr(
        module,
        "_send_key",
        lambda _window, key_code: keys.append(key_code),
    )

    assert module.cancel_stale_file_picker(
        object(), Image.new("RGB", (300, 200)), observations
    ) is True
    assert keys == [53]


def test_file_picker_cancel_uses_guest_relative_coordinates() -> None:
    module = load_module()
    window = module.UTMWindow(42, 7, (99.0, 30.0, 1616.0, 1050.0))
    assert module._map_point((1129.0, 633.5), (1728, 1162), window) == pytest.approx(
        (1154.8241, 602.4398), abs=0.01
    )


def test_ssh_transcript_redacts_an_unexpected_password_echo() -> None:
    module = load_module()
    synthetic_value = "9072"
    module.guest_password = lambda: synthetic_value
    public = module.redact_ssh_transcript(
        b"ocww@host's password:\r\n" + synthetic_value.encode() + b"\r\nIDENTITY=verified\r\n"
    )
    assert public == "IDENTITY=verified"
    assert synthetic_value not in public


def test_password_input_maps_every_configured_ascii_digit(monkeypatch) -> None:
    module = load_module()
    synthetic_value = "9072"
    monkeypatch.setattr(module, "guest_password", lambda: synthetic_value)
    events: list[tuple[int, bool]] = []
    fake_quartz = type(
        "Quartz",
        (),
        {
            "CGEventCreateKeyboardEvent": staticmethod(
                lambda _source, key_code, pressed: (key_code, pressed)
            ),
            "CGEventPostToPid": staticmethod(lambda _pid, event: events.append(event)),
        },
    )
    monkeypatch.setitem(sys.modules, "Quartz", fake_quartz)

    module._send_password(module.UTMWindow(42, 7, (0.0, 0.0, 100.0, 100.0)))

    assert events == [
        (25, True), (25, False),
        (29, True), (29, False),
        (26, True), (26, False),
        (19, True), (19, False),
    ]


@pytest.mark.parametrize("invalid", ("12", "12345", "12a4", "１２３４"))
def test_password_input_rejects_non_four_ascii_digits_without_echoing_value(
    invalid: str, monkeypatch
) -> None:
    module = load_module()
    monkeypatch.setattr(module, "guest_password", lambda: invalid)

    with pytest.raises(module.ConfigurationError) as captured:
        module._send_password(module.UTMWindow(42, 7, (0.0, 0.0, 100.0, 100.0)))

    assert invalid not in str(captured.value)


def test_ocr_targets_and_authorization_are_unique() -> None:
    module = load_module()
    payload = json.dumps(
        [
            {"text": "Accessibility", "confidence": 0.99, "x": 0.1, "y": 0.85, "width": 0.2, "height": 0.05},
            {"text": "Privacy & Security", "confidence": 0.99, "x": 0.35, "y": 0.80, "width": 0.3, "height": 0.04},
            {"text": "ocww", "confidence": 0.99, "x": 0.02, "y": 0.93, "width": 0.08, "height": 0.04},
            {"text": "AEServer", "confidence": 0.99, "x": 0.2, "y": 0.65, "width": 0.1, "height": 0.04},
            {"text": "sshd-keygen-wrapper", "confidence": 0.99, "x": 0.2, "y": 0.55, "width": 0.2, "height": 0.04},
            {"text": "Terminal", "confidence": 0.99, "x": 0.2, "y": 0.45, "width": 0.1, "height": 0.04},
            {"text": "Enter your password to allow this.", "confidence": 0.99, "x": 0.30, "y": 0.71, "width": 0.4, "height": 0.04},
            {"text": "ocww", "confidence": 0.99, "x": 0.45, "y": 0.67, "width": 0.08, "height": 0.04},
            {"text": "Modify Settings", "confidence": 0.99, "x": 0.65, "y": 0.54, "width": 0.2, "height": 0.05},
        ]
    )
    observations = module.parse_ocr_output(payload, 1000, 800)
    for target in module.TARGETS:
        assert module.find_unique_target(observations, target).text
    dialog = module.locate_authorization_dialog(observations, "ocww", (1000, 800))
    assert dialog.username_box.center[1] > dialog.password_box.center[1]
    assert dialog.password_point[0] == pytest.approx(dialog.username_box.center[0])
    assert dialog.modify_point == dialog.modify_box.center
    aeserver = module.find_unique_target(observations, "AEServer")
    with pytest.raises(module.UTMAError):
        module.find_unique_target(observations + [aeserver], "AEServer")

    without_owner = [
        item for item in observations if "privacy & security" not in item.text.casefold()
    ]
    with pytest.raises(module.UTMAError, match="ownership"):
        module.locate_authorization_dialog(without_owner, "ocww", (1000, 800))


def test_password_field_content_detection_is_scoped_to_inferred_row() -> None:
    module = load_module()
    dialog = module.AuthorizationDialog(
        module.PixelBox(80, 150, 120, 170),
        module.PixelBox(70, 110, 230, 130),
        module.PixelBox(70, 250, 230, 280),
        (100.0, 205.0),
        (150.0, 265.0),
    )
    filled = module.OCRObservation(
        "....", 0.99, module.PixelBox(80, 197, 120, 213)
    )
    unrelated = module.OCRObservation(
        "Cancel", 0.99, module.PixelBox(120, 300, 180, 320)
    )
    assert module.password_field_has_content([filled, unrelated], dialog, (320, 400))
    assert not module.password_field_has_content([unrelated], dialog, (320, 400))


def test_ax_probe_has_no_guest_pyobjc_dependency() -> None:
    module = load_module()
    source = module.ssh_accessibility_probe_source()
    assert "ctypes" in source
    assert "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices" in source
    assert "import ApplicationServices" not in source


def test_switch_detection_uses_row_relative_components() -> None:
    module = load_module()
    image = Image.new("RGB", (800, 500), "white")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((650, 190, 730, 235), radius=22, fill=(10, 132, 255))
    draw.ellipse((690, 194, 728, 232), fill="white")
    label = module.PixelBox(120, 190, 300, 235)
    switch = module.detect_switch(image, label)
    assert switch.enabled is True
    assert 650 <= switch.center[0] <= 730

    image = Image.new("RGB", (800, 500), "white")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((650, 190, 730, 235), radius=22, fill=(145, 145, 150))
    draw.ellipse((653, 194, 691, 232), fill="white")
    switch = module.detect_switch(image, label)
    assert switch.enabled is False

    image = Image.new("RGB", (800, 500), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((650, 200, 672, 214), fill=(10, 132, 255))
    compact_label = module.PixelBox(120, 200, 300, 215)
    switch = module.detect_switch(image, compact_label)
    assert switch.enabled is True


def test_source_contract_forbids_scope_and_lifecycle_expansion() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'TARGETS = ("AEServer", "sshd-keygen-wrapper", "Terminal")' in source
    assert "WAIT_SECONDS = 3.0" in source
    for forbidden in (
        '"start"', '"stop"', '"restart"', "tccutil", "TCC.db",
        "NSPasteboard", "pbcopy", "pbpaste", "--latest", "--current",
    ):
        assert forbidden not in source
    assert "CGWindowListCopyWindowInfo" in source
    assert "Capture Input" in source
    assert "CGEventCreateKeyboardEvent" in source
    assert "FIXED_PASSWORD" not in source
    assert "PASSWORD_KEY_CODES" not in source
    assert "AXIsProcessTrusted" in source
    assert source.count("verify_all_targets") >= 3
    assert "_active_vm_name" not in source


def test_standalone_utm_a_skill_is_removed() -> None:
    assert not (ROOT / "skills" / "utm-a").exists()
    assert not (ROOT / "docs" / "utm-a.md").exists()
