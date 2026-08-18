from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import utm_p8


def _payload() -> dict[str, str]:
    return {
        "issuer_id": "11111111-2222-3333-4444-555555555555",
        "key_id": "ABC123DEFG",
        "private_key_path": "/Users/example/Downloads/apple-store-bm/AuthKey_ABC123DEFG.p8",
        "private_key": "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----\n",
    }


def test_main_preserves_the_complete_actual_error(monkeypatch, capsys) -> None:
    detail = "p8 readback failed: " + "z" * 400
    monkeypatch.setattr(
        utm_p8,
        "run",
        lambda args: (_ for _ in ()).throw(utm_p8.UTMP8Error(detail)),
    )
    monkeypatch.setattr(sys, "argv", [
        "utm_p8.py",
        "--page-title", "Demo-abcd",
        "--vm-name", "abcd",
        "--vm-ip", "192.0.2.10",
        "--vm-user", "abcd",
    ])
    assert utm_p8.main() == 1
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [f"执行报错：utm-p8；{detail}"]


def test_guest_command_reads_only_the_fixed_prod_yml_and_p8() -> None:
    command = utm_p8.guest_read_command("abcd")
    assert "/Users/example/Downloads/apple-store-bm/config/prod.yml" in command
    assert "AuthKey_" in command
    assert "Downloads/apple-store-bm" in command
    for mutating in ("os.O_WRONLY", "os.unlink", "os.replace", "os.chmod", "os.mkdir", "rm -"):
        assert mutating not in command


def test_guest_payload_is_validated_without_printing_secret() -> None:
    parsed = utm_p8.parse_guest_payload(json.dumps(_payload()))
    assert parsed["key_id"] == "ABC123DEFG"
    broken = _payload()
    broken["private_key_path"] = "/Users/example/Downloads/other/AuthKey_ABC123DEFG.p8"
    with pytest.raises(utm_p8.UTMP8Error, match="GUEST_P8_PATH_INVALID"):
        utm_p8.parse_guest_payload(json.dumps(broken))


class FakeNotionRunner:
    def __init__(self, initial: str, *, corrupt_after_write: bool = False) -> None:
        self.value = initial
        self.corrupt_after_write = corrupt_after_write
        self.write_count = 0
        self.read_count = 0

    def __call__(self, command, *, input=None, **kwargs):
        if "verify-parent" in command:
            return subprocess.CompletedProcess(command, 0, stdout="{}\n", stderr="")
        if "read-toggle-code" in command:
            output = Path(command[command.index("--out") + 1])
            value = self.value
            if self.corrupt_after_write and self.write_count == 1 and self.read_count == 1:
                value = "corrupt"
            output.write_text(value, encoding="utf-8")
            output.chmod(0o600)
            self.read_count += 1
            return subprocess.CompletedProcess(command, 0, stdout="{}\n", stderr="")
        if "write-toggle-code" in command:
            self.write_count += 1
            if "--stdin" in command:
                self.value = input
            else:
                source = Path(command[command.index("--file") + 1])
                self.value = source.read_text(encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="{}\n", stderr="")
        raise AssertionError(command)


def test_notion_write_uses_before_write_after_and_independent_readback(tmp_path) -> None:
    expected = utm_p8.notion_payload(_payload())
    runner = FakeNotionRunner("old")
    result = utm_p8.update_notion(
        parent_title="host",
        page_title="App-abcd",
        text=expected,
        runner=runner,
        temporary_root=tmp_path,
    )
    assert result == "written"
    assert runner.value == expected
    assert runner.write_count == 1
    assert runner.read_count == 2
    assert not list(tmp_path.iterdir())


def test_notion_mismatch_rolls_back_and_independently_verifies(tmp_path) -> None:
    runner = FakeNotionRunner("before", corrupt_after_write=True)
    with pytest.raises(utm_p8.UTMP8Error, match="NOTION_AFTER_READBACK_MISMATCH"):
        utm_p8.update_notion(
            parent_title="host",
            page_title="App-abcd",
            text=utm_p8.notion_payload(_payload()),
            runner=runner,
            temporary_root=tmp_path,
        )
    assert runner.value == "before"
    assert runner.write_count == 2
    assert runner.read_count == 3
    assert not list(tmp_path.iterdir())


def _create_shared_app_assets(shared_dir: Path, app_name: str) -> None:
    (shared_dir / app_name).mkdir()
    (shared_dir / f"{app_name}-git").mkdir()
    (shared_dir / f"{app_name}.png").write_bytes(b"png")
    (shared_dir / f"{app_name}.xlsx").write_bytes(b"xlsx")


def test_cleanup_shared_app_assets_moves_only_four_exact_application_targets(tmp_path) -> None:
    shared_dir = tmp_path / "shared"
    trash_root = tmp_path / "trash"
    shared_dir.mkdir()
    trash_root.mkdir()
    app_name = "PanSwap-CakeBaking"
    _create_shared_app_assets(shared_dir, app_name)
    preserved = (
        shared_dir / "PanSwap-CakeBaking-copy",
        shared_dir / "PanSwap-CakeBaking-git-backup",
        shared_dir / "AppleAccountScriptsBackup",
        shared_dir / "Fire_One_en1.3",
        shared_dir / "utm-image",
    )
    for path in preserved:
        path.mkdir()

    result = utm_p8.cleanup_shared_app_assets(
        app_name=app_name,
        shared_dir=shared_dir,
        trash_root=trash_root,
    )

    assert result == "verified_4"
    for name in utm_p8.shared_app_asset_names(app_name):
        assert not (shared_dir / name).exists()
    assert all(path.is_dir() for path in preserved)
    trash_attempts = list(trash_root.iterdir())
    assert len(trash_attempts) == 1
    assert {path.name for path in trash_attempts[0].iterdir()} == set(
        utm_p8.shared_app_asset_names(app_name)
    )


def test_cleanup_shared_app_assets_rejects_unsafe_target_before_moving_anything(tmp_path) -> None:
    shared_dir = tmp_path / "shared"
    trash_root = tmp_path / "trash"
    outside = tmp_path / "outside"
    shared_dir.mkdir()
    trash_root.mkdir()
    outside.mkdir()
    app_name = "PanSwap-CakeBaking"
    (shared_dir / app_name).mkdir()
    (shared_dir / f"{app_name}-git").mkdir()
    (shared_dir / f"{app_name}.png").write_bytes(b"png")
    (shared_dir / f"{app_name}.xlsx").symlink_to(outside, target_is_directory=True)

    with pytest.raises(utm_p8.UTMP8Error, match="SHARED_APP_ASSET_UNSAFE"):
        utm_p8.cleanup_shared_app_assets(
            app_name=app_name,
            shared_dir=shared_dir,
            trash_root=trash_root,
        )

    assert (shared_dir / app_name).is_dir()
    assert (shared_dir / f"{app_name}-git").is_dir()
    assert (shared_dir / f"{app_name}.png").is_file()
    assert (shared_dir / f"{app_name}.xlsx").is_symlink()
    assert outside.is_dir()
    assert not list(trash_root.iterdir())


def test_cleanup_shared_app_assets_rejects_path_escape_and_is_idempotent(tmp_path) -> None:
    shared_dir = tmp_path / "shared"
    trash_root = tmp_path / "trash"
    shared_dir.mkdir()
    trash_root.mkdir()
    preserved = tmp_path / "Other"
    preserved.mkdir()

    with pytest.raises(utm_p8.UTMP8Error, match="SHARED_APP_NAME_INVALID"):
        utm_p8.cleanup_shared_app_assets(
            app_name="../Other",
            shared_dir=shared_dir,
            trash_root=trash_root,
        )
    assert preserved.is_dir()

    assert utm_p8.cleanup_shared_app_assets(
        app_name="PanSwap-CakeBaking",
        shared_dir=shared_dir,
        trash_root=trash_root,
    ) == "already_clean"
    assert not list(trash_root.iterdir())


def test_cleanup_shared_app_assets_never_treats_public_directories_as_an_app(tmp_path) -> None:
    shared_dir = tmp_path / "shared"
    trash_root = tmp_path / "trash"
    shared_dir.mkdir()
    trash_root.mkdir()
    public = shared_dir / "AppleAccountScriptsBackup"
    public.mkdir()

    with pytest.raises(utm_p8.UTMP8Error, match="SHARED_APP_NAME_RESERVED"):
        utm_p8.cleanup_shared_app_assets(
            app_name="AppleAccountScriptsBackup",
            shared_dir=shared_dir,
            trash_root=trash_root,
        )

    assert public.is_dir()
    assert not list(trash_root.iterdir())


def test_run_cleans_shared_assets_only_after_notion_registration_succeeds(monkeypatch, tmp_path) -> None:
    context = SimpleNamespace(
        parent_title="host",
        page_title="PanSwap-CakeBaking-abcd",
        app_name="PanSwap-CakeBaking",
    )
    monkeypatch.setattr(utm_p8, "resolve_direct_context", lambda **kwargs: context)
    monkeypatch.setattr(utm_p8, "parse_guest_payload", lambda stdout: _payload())
    events: list[str] = []
    monkeypatch.setattr(
        utm_p8,
        "update_notion",
        lambda **kwargs: events.append("notion") or "written",
    )
    monkeypatch.setattr(
        utm_p8,
        "cleanup_shared_app_assets",
        lambda **kwargs: events.append(f"cleanup:{kwargs['app_name']}") or "verified_4",
    )
    runner = lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")
    args = SimpleNamespace(
        run_id=None,
        page_title=context.page_title,
        vm_name="abcd",
        vm_ip="192.0.2.10",
        vm_user="abcd",
    )

    assert utm_p8.run(args, runner=runner, password_env={}) == 0
    assert events == ["notion", "cleanup:PanSwap-CakeBaking"]

    events.clear()
    monkeypatch.setattr(
        utm_p8,
        "update_notion",
        lambda **kwargs: (_ for _ in ()).throw(utm_p8.UTMP8Error("NOTION_FAILED")),
    )
    with pytest.raises(utm_p8.UTMP8Error, match="NOTION_FAILED"):
        utm_p8.run(args, runner=runner, password_env={})
    assert events == []


def test_cleanup_only_uses_page_application_name_without_any_vm_operation(monkeypatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        utm_p8,
        "validate_target",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("VM validation called")),
    )
    monkeypatch.setattr(
        utm_p8,
        "resolve_direct_context",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("VM context called")),
    )
    monkeypatch.setattr(
        utm_p8,
        "cleanup_shared_app_assets",
        lambda **kwargs: events.append(kwargs["app_name"]) or "already_clean",
    )
    args = SimpleNamespace(
        cleanup_only=True,
        run_id=None,
        page_title="PanSwap-CakeBaking-stgf",
        vm_name="stgf",
        vm_ip=None,
        vm_user=None,
    )

    def forbidden_runner(*args, **kwargs):
        raise AssertionError("SSH or Notion runner called")

    assert utm_p8.run(args, runner=forbidden_runner, password_env={}) == 0
    assert events == ["PanSwap-CakeBaking"]


def test_cleanup_only_cli_does_not_require_vm_ip_or_vm_user(monkeypatch, capsys) -> None:
    observed = []
    monkeypatch.setattr(
        utm_p8,
        "run",
        lambda args: observed.append(args) or print("SHARED_APP_ASSETS_CLEANED=verified") or 0,
    )
    monkeypatch.setattr(sys, "argv", [
        "utm_p8.py",
        "--cleanup-only",
        "--page-title", "PanSwap-CakeBaking-stgf",
        "--vm-name", "stgf",
    ])

    assert utm_p8.main() == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "开始执行：utm-p8",
        "执行成功：utm-p8；SHARED_APP_ASSETS_CLEANED=verified",
    ]
    assert observed[0].cleanup_only is True
    assert observed[0].vm_ip is None
    assert observed[0].vm_user is None
