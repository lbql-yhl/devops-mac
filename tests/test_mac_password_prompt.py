from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import mac_password_prompt as prompt  # noqa: E402


def test_flat_guest_deployment_imports_without_services(
    tmp_path: Path,
) -> None:
    deployed_script = tmp_path / "mac_password_prompt.py"
    shutil.copy2(ROOT / "scripts" / "mac_password_prompt.py", deployed_script)
    guest_fixture_value = "6194"
    probe = """
import runpy
import sys
import types

class FlatDependency(types.ModuleType):
    def __getattr__(self, name):
        return None

sys.modules["find_system_settings_general"] = FlatDependency(
    "find_system_settings_general"
)
namespace = runpy.run_path(sys.argv[1])
print(namespace["guest_password"]())
"""
    child_environment = os.environ.copy()
    child_environment["SUBMISSION_GUEST_PASSWORD"] = guest_fixture_value

    result = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(deployed_script)],
        cwd=tmp_path,
        env=child_environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == guest_fixture_value
    assert "ModuleNotFoundError" not in result.stderr
    assert not (tmp_path / "services").exists()


def test_guest_password_fails_closed_when_runtime_value_is_missing(
    monkeypatch,
    capsys,
) -> None:
    unrelated_fixture_value = "must-not-be-echoed"
    monkeypatch.delenv("SUBMISSION_GUEST_PASSWORD", raising=False)
    monkeypatch.setenv("UNRELATED_SECRET", unrelated_fixture_value)

    with pytest.raises(RuntimeError) as raised:
        prompt.guest_password()

    captured = capsys.readouterr()
    assert "SUBMISSION_GUEST_PASSWORD" in str(raised.value)
    assert unrelated_fixture_value not in str(raised.value)
    assert unrelated_fixture_value not in captured.out
    assert unrelated_fixture_value not in captured.err


def test_secure_field_accepts_exact_length_when_macos_uses_private_mask_glyphs(
    monkeypatch,
) -> None:
    synthetic_value = "9072"
    monkeypatch.setattr(prompt, "guest_password", lambda: synthetic_value)
    secure_info = {"role": "AXTextField", "subrole": "AXSecureTextField"}

    assert prompt.mac_password_readback_matches("◦" * len(synthetic_value), secure_info)
    assert not prompt.mac_password_readback_matches("◦" * (len(synthetic_value) - 1), secure_info)


def test_nonsecure_field_uses_the_runtime_password_or_known_mask(monkeypatch) -> None:
    synthetic_value = "9072"
    monkeypatch.setattr(prompt, "guest_password", lambda: synthetic_value)
    plain_info = {"role": "AXTextField", "subrole": None}

    assert prompt.mac_password_readback_matches(synthetic_value, plain_info)
    assert prompt.mac_password_readback_matches("•" * len(synthetic_value), plain_info)
    assert not prompt.mac_password_readback_matches("◦" * len(synthetic_value), plain_info)


def test_prompt_source_has_no_import_time_password_capture() -> None:
    source = (ROOT / "scripts" / "mac_password_prompt.py").read_text(encoding="utf-8")

    assert "guest_password()" in source
    assert "MAC_PASSWORD_VALUE" not in source
