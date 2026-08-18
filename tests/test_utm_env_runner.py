#!/usr/bin/env python3
"""Tests for the single-entry UTM-ENV host/guest runner."""

from __future__ import annotations

import hashlib
import inspect
import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))

import scripts.utm_env as runner
import scripts.utm_env_guest_finalize as guest


class FakeTransport:
    def __init__(self, *, verify_hash: str = "exact") -> None:
        self.calls: list[tuple[str, object]] = []
        self.verify_hash = verify_hash

    def probe_identity(self) -> None:
        self.calls.append(("probe_identity", None))

    def install_helpers(self) -> None:
        self.calls.append(("install_helpers", None))

    def run_guest(self, mode: str, payload: dict) -> dict:
        self.calls.append(("run_guest", mode))
        return {
            "status": "verified",
            "host_guest_asset_sha256": self.verify_hash,
            "fire_one_env_sha256": self.verify_hash,
            "fire_one_env_mode": "600",
            "asset_count": 4,
            "guest_download_asset_count": 3,
            "guest_code_path": "/Users/example/StudioProjects/FlagCue",
            "guest_code_copy": "verified_or_identical_existing",
        }

    def cleanup_helpers(self) -> None:
        self.calls.append(("cleanup_helpers", None))


def make_assets(root: Path, app_name: str = "FlagCue") -> None:
    fire_one = root / "Fire_One_en1.3"
    fire_one.mkdir()
    (fire_one / ".env").write_text("APP_NAME=FlagCue\n", encoding="utf-8")
    images = root / app_name
    images.mkdir()
    (images / f"{app_name}1.png").write_bytes(b"\x89PNG\r\n\x1a\nimage")
    code = root / f"{app_name}-git"
    code.mkdir()
    (code / "README.md").write_text("source", encoding="utf-8")
    (root / f"{app_name}.xlsx").write_bytes(b"xlsx")
    (root / f"{app_name}.png").write_bytes(b"\x89PNG\r\n\x1a\ncoin")


def make_guest_home(base: Path) -> tuple[Path, Path, Path]:
    home = base / "testvm"
    downloads = home / "Downloads"
    downloads.mkdir(parents=True)
    fire_one = downloads / "Fire_One_en1.3"
    fire_one.mkdir()
    return home, downloads, fire_one


class UTMEnvRunnerTests(unittest.TestCase):
    def test_expected_assets_use_the_case_exact_fixed_names(self) -> None:
        self.assertEqual(
            runner.expected_asset_names("FlagCue"),
            ["FlagCue", "FlagCue.xlsx", "FlagCue.png", "FlagCue-git"],
        )
        self.assertNotEqual(
            runner.expected_asset_names("FlagCue"),
            runner.expected_asset_names("flagcue"),
        )

    def test_collect_host_manifest_covers_files_and_directory_trees(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_assets(root)
            manifest = runner.collect_host_manifest(root, "FlagCue")

        self.assertEqual(list(manifest), runner.expected_asset_names("FlagCue"))
        self.assertEqual(manifest["FlagCue.xlsx"]["type"], "file")
        self.assertEqual(manifest["FlagCue-git"]["type"], "directory")
        for evidence in manifest.values():
            self.assertRegex(evidence["sha256"], r"^[0-9a-f]{64}$")

    def test_collect_host_env_manifest_uses_fire_one_env_not_shared_root_env(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_assets(root)
            (root / ".env").write_text("WRONG=root-env\n", encoding="utf-8")
            evidence = runner.collect_host_env_manifest(root)

        self.assertEqual(evidence["type"], "file")
        self.assertEqual(
            evidence["sha256"],
            hashlib.sha256(b"APP_NAME=FlagCue\n").hexdigest(),
        )

    def test_finalize_guest_only_runs_copy_then_fresh_verify(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_assets(root)
            transport = FakeTransport()
            result = runner.finalize_guest(
                app_name="FlagCue",
                vm_user="abcd",
                shared_root=root,
                transport=transport,
            )

        self.assertEqual(
            transport.calls,
            [
                ("probe_identity", None),
                ("install_helpers", None),
                ("run_guest", "copy"),
                ("run_guest", "verify"),
                ("cleanup_helpers", None),
            ],
        )
        self.assertEqual(result["host_guest_asset_sha256"], "exact")
        self.assertEqual(result["fire_one_env_sha256"], "exact")
        self.assertEqual(result["fire_one_env_mode"], "600")
        self.assertEqual(result["guest_download_asset_count"], 3)
        self.assertEqual(result["guest_code_path"], "/Users/example/StudioProjects/FlagCue")
        self.assertEqual(result["guest_code_copy"], "verified_or_identical_existing")
        self.assertNotIn("terminal_app_management", result)
        self.assertEqual(result["utm_env"], "verified")

    def test_gui_is_blocked_until_fresh_three_way_hash_verification_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_assets(root)
            transport = FakeTransport(verify_hash="mismatch")
            with self.assertRaisesRegex(RuntimeError, "three-way asset verification"):
                runner.finalize_guest(
                    app_name="FlagCue",
                    vm_user="abcd",
                    shared_root=root,
                    transport=transport,
                )

        self.assertEqual(
            transport.calls,
            [
                ("probe_identity", None),
                ("install_helpers", None),
                ("run_guest", "copy"),
                ("cleanup_helpers", None),
            ],
        )

    def test_utm_env_helpers_do_not_contain_app_management_automation(self) -> None:
        combined = "\n".join(
            (ROOT / "scripts" / name).read_text(encoding="utf-8")
            for name in ("utm_env.py", "utm_env_guest_finalize.py")
        )
        for forbidden in (
            "Privacy_AppBundles",
            "TERMINAL_APP_MANAGEMENT",
            "app-management-ensure",
            "app-management-verify",
            "run_aqua",
        ):
            self.assertNotIn(forbidden, combined)

    def test_run_generates_env_before_guest_checks_and_downloading(self) -> None:
        source = inspect.getsource(runner.run)
        generate_index = source.index("generate_env_from_notion(")
        collect_index = source.index("collect_host_env_manifest(")
        probe_index = source.index("probe_identity()")
        download_index = source.index("download_assets(")
        self.assertLess(generate_index, collect_index)
        self.assertLess(generate_index, probe_index)
        self.assertLess(collect_index, download_index)
        self.assertLess(probe_index, download_index)

    def test_cli_requires_parent_title_and_derives_exact_notion_page_title(self) -> None:
        args = runner.build_parser().parse_args([
            "--parent-title",
            "Host A",
            "--app-name",
            "FlagCue",
            "--asset-app-name",
            "OriginalFlagCue",
            "--vm-ip",
            "192.0.2.10",
            "--vm-user",
            "abcd",
        ])
        self.assertEqual(args.parent_title, "Host A")
        self.assertEqual(args.asset_app_name, "OriginalFlagCue")
        self.assertEqual(runner.notion_page_title(args.app_name, args.vm_user), "FlagCue-abcd")

        with self.assertRaises(SystemExit):
            runner.build_parser().parse_args([
                "--app-name",
                "FlagCue",
                "--vm-ip",
                "192.0.2.10",
                "--vm-user",
                "abcd",
            ])

    def test_guest_preflight_checks_shared_and_target_env_safety(self) -> None:
        source = inspect.getsource(runner.SSHGuestTransport.probe_identity)
        self.assertIn("Fire_One_en1.3/.env", source)
        self.assertIn("test ! -L", source)

    def test_cli_prints_explicit_completion_markers(self) -> None:
        result = {
            "ENV_WRITE": "changed",
            "ENV_READBACK": "exact",
            "host_guest_asset_sha256": "exact",
            "fire_one_env_sha256": "exact",
            "fire_one_env_mode": "600",
            "guest_download_asset_count": 3,
            "guest_code_path": "/Users/example/StudioProjects/FlagCue",
            "guest_code_copy": "verified_or_identical_existing",
            "utm_env": "verified",
        }
        argv = [
            "utm_env.py",
            "--parent-title",
            "Host A",
            "--app-name",
            "FlagCue",
            "--vm-ip",
            "192.0.2.10",
            "--vm-user",
            "abcd",
        ]
        output = io.StringIO()
        with patch.object(sys, "argv", argv), patch.object(
            runner, "run", return_value=result
        ), redirect_stdout(output):
            self.assertEqual(runner.main(), 0)
        lines = output.getvalue().splitlines()
        self.assertEqual(
            lines,
            ["开始执行：utm-env", "执行成功：utm-env；UTM_ENV=verified"],
        )

    def test_cli_prints_clean_error_without_json_payload(self) -> None:
        argv = [
            "utm_env.py",
            "--parent-title",
            "Host A",
            "--app-name",
            "FlagCue",
            "--vm-ip",
            "192.0.2.10",
            "--vm-user",
            "abcd",
        ]
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(sys, "argv", argv), patch.object(
            runner, "run", side_effect=RuntimeError("asset failure")
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(runner.main(), 1)
        self.assertEqual(stdout.getvalue().splitlines(), ["开始执行：utm-env"])
        self.assertEqual(stderr.getvalue().splitlines(), ["执行报错：utm-env；asset failure"])
        self.assertNotIn('"status"', stdout.getvalue() + stderr.getvalue())

    def test_utm_env_runner_has_no_gui_or_fixed_password_pty_branch(self) -> None:
        source = (ROOT / "scripts" / "utm_env.py").read_text(encoding="utf-8")
        self.assertNotIn("SUDO_PROMPT", source)
        self.assertNotIn("pty.openpty", source)
        self.assertNotIn("sudo -S", source)
        self.assertNotIn("tccutil", source)

    def test_guest_helper_places_code_in_studio_projects_and_other_assets_in_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            shared = base / "shared"
            shared.mkdir()
            make_assets(shared)
            home, downloads, fire_one = make_guest_home(base)
            payload = {
                "app_name": "FlagCue",
                "assets": runner.collect_host_manifest(shared, "FlagCue"),
                "fire_one_env": runner.collect_host_env_manifest(shared),
            }
            with patch.object(guest, "GUEST_SHARED_ROOT", shared), patch.object(
                guest, "_verify_identity", return_value=(home, downloads, fire_one)
            ):
                copied = guest.run_assets("copy", "testvm", payload)
                verified = guest.run_assets("verify", "testvm", payload)

            self.assertEqual(copied["host_guest_asset_sha256"], "exact")
            self.assertEqual(copied["fire_one_env_sha256"], "exact")
            self.assertEqual(verified["host_guest_asset_sha256"], "exact")
            self.assertEqual(copied["guest_code_path"], str(home / "StudioProjects" / "FlagCue"))
            self.assertEqual(copied["guest_code_copy"], "verified_or_identical_existing")
            self.assertEqual(copied["guest_download_asset_count"], 3)
            self.assertEqual(
                sorted(path.name for path in downloads.iterdir()),
                ["Fire_One_en1.3", "FlagCue", "FlagCue.png", "FlagCue.xlsx"],
            )
            self.assertFalse((downloads / "FlagCue-git").exists())
            self.assertEqual(
                (home / "StudioProjects" / "FlagCue" / "README.md").read_text(encoding="utf-8"),
                "source",
            )
            self.assertFalse((home / "StudioProjects").is_symlink())

    def test_guest_helper_verify_does_not_create_missing_studio_projects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            shared = base / "shared"
            shared.mkdir()
            make_assets(shared)
            home, downloads, fire_one = make_guest_home(base)
            shutil.copy2(shared / "Fire_One_en1.3" / ".env", fire_one / ".env")
            (fire_one / ".env").chmod(0o600)
            shutil.copytree(shared / "FlagCue", downloads / "FlagCue")
            shutil.copy2(shared / "FlagCue.xlsx", downloads / "FlagCue.xlsx")
            shutil.copy2(shared / "FlagCue.png", downloads / "FlagCue.png")
            payload = {
                "app_name": "FlagCue",
                "assets": runner.collect_host_manifest(shared, "FlagCue"),
                "fire_one_env": runner.collect_host_env_manifest(shared),
            }

            with patch.object(guest, "GUEST_SHARED_ROOT", shared), patch.object(
                guest, "_verify_identity", return_value=(home, downloads, fire_one)
            ):
                with self.assertRaisesRegex(
                    guest.GuestFinalizeError,
                    "StudioProjects is missing during verification",
                ):
                    guest.run_assets("verify", "testvm", payload)

            self.assertFalse((home / "StudioProjects").exists())

    def test_guest_helper_rejects_a_symlinked_studio_projects_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            shared = base / "shared"
            shared.mkdir()
            make_assets(shared)
            home, downloads, fire_one = make_guest_home(base)
            outside = base / "outside"
            outside.mkdir()
            (home / "StudioProjects").symlink_to(outside, target_is_directory=True)
            payload = {
                "app_name": "FlagCue",
                "assets": runner.collect_host_manifest(shared, "FlagCue"),
                "fire_one_env": runner.collect_host_env_manifest(shared),
            }

            with patch.object(guest, "GUEST_SHARED_ROOT", shared), patch.object(
                guest, "_verify_identity", return_value=(home, downloads, fire_one)
            ):
                with self.assertRaisesRegex(guest.GuestFinalizeError, "StudioProjects.*unsafe"):
                    guest.run_assets("copy", "testvm", payload)

            self.assertEqual(list(outside.iterdir()), [])

    def test_guest_helper_never_overwrites_a_conflicting_studio_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            shared = base / "shared"
            shared.mkdir()
            make_assets(shared)
            home, downloads, fire_one = make_guest_home(base)
            conflict = home / "StudioProjects" / "FlagCue"
            conflict.mkdir(parents=True)
            readme = conflict / "README.md"
            readme.write_text("do-not-overwrite", encoding="utf-8")
            payload = {
                "app_name": "FlagCue",
                "assets": runner.collect_host_manifest(shared, "FlagCue"),
                "fire_one_env": runner.collect_host_env_manifest(shared),
            }

            with patch.object(guest, "GUEST_SHARED_ROOT", shared), patch.object(
                guest, "_verify_identity", return_value=(home, downloads, fire_one)
            ):
                with self.assertRaisesRegex(guest.GuestFinalizeError, "StudioProjects target conflicts"):
                    guest.run_assets("copy", "testvm", payload)

            self.assertEqual(readme.read_text(encoding="utf-8"), "do-not-overwrite")

    def test_guest_helper_refreshes_only_fire_one_env_from_shared_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            shared = base / "shared"
            shared.mkdir()
            make_assets(shared)
            home, downloads, fire_one = make_guest_home(base)
            target_env = fire_one / ".env"
            target_env.write_text("STALE=1\n", encoding="utf-8")
            untouched = fire_one / "package.json"
            untouched.write_text("keep", encoding="utf-8")
            payload = {
                "app_name": "FlagCue",
                "assets": runner.collect_host_manifest(shared, "FlagCue"),
                "fire_one_env": runner.collect_host_env_manifest(shared),
            }
            with patch.object(guest, "GUEST_SHARED_ROOT", shared), patch.object(
                guest, "_verify_identity", return_value=(home, downloads, fire_one)
            ):
                copied = guest.run_assets("copy", "testvm", payload)
                verified = guest.run_assets("verify", "testvm", payload)

            self.assertEqual(target_env.read_bytes(), (shared / "Fire_One_en1.3" / ".env").read_bytes())
            self.assertEqual(target_env.stat().st_mode & 0o777, 0o600)
            self.assertEqual(untouched.read_text(encoding="utf-8"), "keep")
            self.assertEqual(copied["fire_one_env_sha256"], "exact")
            self.assertEqual(verified["fire_one_env_sha256"], "exact")

            target_env.chmod(0o644)
            with patch.object(guest, "GUEST_SHARED_ROOT", shared), patch.object(
                guest, "_verify_identity", return_value=(home, downloads, fire_one)
            ):
                with self.assertRaisesRegex(guest.GuestFinalizeError, "mode is not 600"):
                    guest.run_assets("verify", "testvm", payload)

    def test_guest_helper_never_overwrites_a_conflicting_download(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            shared = base / "shared"
            shared.mkdir()
            make_assets(shared)
            home, downloads, fire_one = make_guest_home(base)
            conflict = downloads / "FlagCue.xlsx"
            conflict.write_bytes(b"do-not-overwrite")
            payload = {
                "app_name": "FlagCue",
                "assets": runner.collect_host_manifest(shared, "FlagCue"),
                "fire_one_env": runner.collect_host_env_manifest(shared),
            }
            with patch.object(guest, "GUEST_SHARED_ROOT", shared), patch.object(
                guest, "_verify_identity", return_value=(home, downloads, fire_one)
            ):
                with self.assertRaisesRegex(guest.GuestFinalizeError, "conflicts"):
                    guest.run_assets("copy", "testvm", payload)
            self.assertEqual(conflict.read_bytes(), b"do-not-overwrite")


if __name__ == "__main__":
    unittest.main()
