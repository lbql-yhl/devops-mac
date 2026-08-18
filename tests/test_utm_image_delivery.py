from __future__ import annotations

import hashlib
from pathlib import Path

from scripts import utm_image_delivery


ROOT = Path(__file__).resolve().parents[1]


def test_clone_stage_places_exact_guest_script_in_shared_backup(tmp_path: Path) -> None:
    result = utm_image_delivery.stage_utm_image_guest_file(tmp_path)
    target = tmp_path / "AppleAccountScriptsBackup" / "utm_19_one.mjs"
    source = ROOT / "skills" / "utm-image" / "scripts" / "utm_19_one.mjs"

    assert result == {"utm_19_one.mjs": hashlib.sha256(source.read_bytes()).hexdigest()}
    assert target.read_bytes() == source.read_bytes()
    assert not target.is_symlink()


def test_post_clone_stages_image_before_shared_copy() -> None:
    source = (ROOT / "scripts" / "utm_vm_clone_steps.py").read_text(encoding="utf-8")
    stage = source.index("stage_utm_image_guest_file(args.shared_dir)")
    copy = source.index("legacy.copy_shared(vm_name, ip)")
    assert stage < copy


def test_clone_skill_has_no_duplicate_vm_clone_docs() -> None:
    assert not (ROOT / "docs" / "utm-post-clone.md").exists()
    assert not (ROOT / "docs" / "utm-vm-clone.md").exists()
