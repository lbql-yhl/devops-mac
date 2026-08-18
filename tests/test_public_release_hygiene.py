import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

from scripts.public_release_audit import (
    REQUIRED_SENSITIVE_EXAMPLE_KEYS,
    audit_repository,
)


ROOT = Path(__file__).resolve().parents[1]
AUDIT_SCRIPT = ROOT / "scripts/public_release_audit.py"
FIXTURE_FIXED_PASSWORD_KEY = "FIXED_" + "PASSWORD"
FIXTURE_DEFAULT_APP_TOKEN_KEY = "DEFAULT_APP_" + "TOKEN"
FIXTURE_DAILY_REPORT_CHAT_KEY = "DAILY_REPORT_" + "CHAT_ID"
FIXTURE_API_TOKEN_KEY = "API_" + "TOKEN"
FIXTURE_NOTION_TOKEN_KEY = "NOTION_" + "TOKEN"
FIXTURE_REPORT_DESTINATION_KEY = "REPORT_" + "DESTINATION"
FIXTURE_BYTE_PASSWORD_KEY = "BYTE_" + "PASSWORD"
FIXTURE_RAW_TOKEN_KEY = "RAW_" + "TOKEN"
FIXTURE_UNICODE_SECRET_KEY = "UNICODE_" + "SECRET"
FIXTURE_FORMAT_APP_ID_KEY = "FORMAT_APP_" + "ID"
FIXTURE_TARGET_CHAT_ID_KEY = "TARGET_CHAT_" + "ID"

KNOWN_RULES = {
    "EXAMPLE_DUPLICATE_KEY",
    "EXAMPLE_FILE_IGNORED",
    "EXAMPLE_FILE_NOT_TRACKED",
    "EXAMPLE_SENSITIVE_KEY_MISSING",
    "EXAMPLE_SENSITIVE_VALUE",
    "FIXED_REPORT_DESTINATION",
    "HISTORICAL_PLAN",
    "IGNORE_RULE_MISSING",
    "PERSONAL_HOME_PATH",
    "SENSITIVE_ASSIGNMENT",
}


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )


def _public_release_gitignore() -> str:
    return """\
.env
*.env
.env.*
!.env.example
!*.env.example
config/workflow.env
runtime/
*.p8
*.p12
*.pem
*.key
*.cer
*.mobileprovision
*.utm/
*.vmwarevm/
*.qcow2
*.vmdk
*.vdi
*.vhd
*.vhdx
*.raw
*.iso
*.ova
*.ovf
*.vmsn
"""


def _copy_audit_cli(root: Path) -> Path:
    destination = root / "scripts/public_release_audit.py"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(AUDIT_SCRIPT, destination)
    return destination


def _tracked_fixture_repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert _git(root, "init", "-q").returncode == 0

    personal_home = "/" + "Users" + "/audit-fixture/private"
    secret_value = "fixture-value-that-must-not-be-reported"
    (root / "unsafe.py").write_text(
        f'HOME_PATH = "{personal_home}"\n'
        f'{FIXTURE_FIXED_PASSWORD_KEY} = "{secret_value}"\n'
        f'{FIXTURE_DEFAULT_APP_TOKEN_KEY} = "{secret_value}"\n'
        f'{FIXTURE_DAILY_REPORT_CHAT_KEY} = "{secret_value}"\n',
        encoding="utf-8",
    )
    historical = root / "docs/superpowers/plans/old.md"
    historical.parent.mkdir(parents=True)
    historical.write_text("historical\n", encoding="utf-8")
    (root / "binary.dat").write_bytes(
        personal_home.encode() + b"\0" + secret_value.encode()
    )
    (root / ".env").write_text(
        f'{FIXTURE_FIXED_PASSWORD_KEY}="{secret_value}"\n', encoding="utf-8"
    )
    (root / ".env.example").write_text(
        "".join(f"{key}=\n" for key in REQUIRED_SENSITIVE_EXAMPLE_KEYS),
        encoding="utf-8",
    )
    (root / "untracked.py").write_text(
        f'{FIXTURE_FIXED_PASSWORD_KEY} = "{secret_value}"\n', encoding="utf-8"
    )
    tracked_link = root / "tracked-link.py"
    try:
        os.symlink("unsafe.py", tracked_link)
    except (NotImplementedError, OSError):
        tracked_link.write_text("unsafe.py\n", encoding="utf-8")
    assert (
        _git(
            root,
            "add",
            "unsafe.py",
            "docs/superpowers/plans/old.md",
            "binary.dat",
            ".env",
            ".env.example",
            "tracked-link.py",
        ).returncode
        == 0
    )
    return root


def test_audit_repository_returns_rule_and_relative_path_pairs() -> None:
    findings = audit_repository(ROOT)

    assert isinstance(findings, list)
    assert all(
        isinstance(rule, str) and isinstance(relative_path, str)
        for rule, relative_path in findings
    )


def test_audit_scans_only_tracked_regular_text_files_and_never_reads_env(
    tmp_path: Path, monkeypatch,
) -> None:
    root = _tracked_fixture_repository(tmp_path)
    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(path: Path) -> bytes:
        if path == root / ".env":
            raise AssertionError("the host .env must never be read")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    findings = audit_repository(root)

    assert ("PERSONAL_HOME_PATH", "unsafe.py") in findings
    assert ("SENSITIVE_ASSIGNMENT", "unsafe.py") in findings
    assert ("FIXED_REPORT_DESTINATION", "unsafe.py") in findings
    assert ("HISTORICAL_PLAN", "docs/superpowers/plans/old.md") in findings
    content_rules = {
        "PERSONAL_HOME_PATH",
        "SENSITIVE_ASSIGNMENT",
        "FIXED_REPORT_DESTINATION",
        "EXAMPLE_SENSITIVE_VALUE",
    }
    assert all(
        rule not in content_rules
        for rule, path in findings
        if path in {".env", "binary.dat", "tracked-link.py", "untracked.py"}
    )
    assert "fixture-value-that-must-not-be-reported" not in repr(findings)
    assert findings == sorted(set(findings))


def test_index_stage_zero_blobs_are_the_only_content_source(
    tmp_path: Path, monkeypatch,
) -> None:
    root = _tracked_fixture_repository(tmp_path)
    fixture_value = "index-only-fixture-secret"
    external_fixture_value = "external-symlink-fixture-secret"

    staged_unsafe = root / "staged-unsafe.py"
    staged_unsafe.write_text(
        f'{FIXTURE_FIXED_PASSWORD_KEY} = "{fixture_value}"\n', encoding="utf-8"
    )
    deleted = root / "deleted.py"
    deleted.write_text(
        f'{FIXTURE_API_TOKEN_KEY} = "{fixture_value}"\n', encoding="utf-8"
    )
    staged_safe = root / "staged-safe.py"
    staged_safe.write_text("VALUE = 1\n", encoding="utf-8")
    worktree_symlink = root / "worktree-symlink.py"
    worktree_symlink.write_text("VALUE = 1\n", encoding="utf-8")
    (root / ".gitignore").write_text(
        _public_release_gitignore(), encoding="utf-8"
    )
    assert (
        _git(
            root,
            "add",
            "staged-unsafe.py",
            "deleted.py",
            "staged-safe.py",
            "worktree-symlink.py",
            ".gitignore",
        ).returncode
        == 0
    )

    staged_unsafe.write_text("VALUE = 1\n", encoding="utf-8")
    deleted.unlink()
    staged_safe.write_text(
        f'{FIXTURE_API_TOKEN_KEY} = "{fixture_value}"\n', encoding="utf-8"
    )
    (root / ".gitignore").write_text("", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text(
        f'{FIXTURE_FIXED_PASSWORD_KEY} = "{external_fixture_value}"\n',
        encoding="utf-8",
    )
    worktree_symlink.unlink()
    try:
        os.symlink(outside, worktree_symlink)
    except (NotImplementedError, OSError):
        pass

    def reject_worktree_reads(path: Path) -> bytes:
        raise AssertionError(f"worktree content read: {path.name}")

    monkeypatch.setattr(Path, "read_bytes", reject_worktree_reads)
    findings = audit_repository(root)

    assert ("SENSITIVE_ASSIGNMENT", "staged-unsafe.py") in findings
    assert ("SENSITIVE_ASSIGNMENT", "deleted.py") in findings
    assert ("SENSITIVE_ASSIGNMENT", "staged-safe.py") not in findings
    assert ("SENSITIVE_ASSIGNMENT", "worktree-symlink.py") not in findings
    assert not any(rule == "IGNORE_RULE_MISSING" for rule, _ in findings)
    assert fixture_value not in repr(findings)
    assert external_fixture_value not in repr(findings)


def test_index_symlink_to_external_file_is_not_scanned(tmp_path: Path) -> None:
    root = _tracked_fixture_repository(tmp_path)
    external_fixture_value = "external-index-symlink-secret"
    outside = tmp_path / "external.py"
    outside.write_text(
        f'{FIXTURE_FIXED_PASSWORD_KEY} = "{external_fixture_value}"\n',
        encoding="utf-8",
    )
    link = root / "external-link.py"
    try:
        os.symlink(outside, link)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    assert _git(root, "add", "external-link.py").returncode == 0

    findings = audit_repository(root)

    assert not any(path == "external-link.py" for _, path in findings)
    assert external_fixture_value not in repr(findings)


def test_sensitive_example_keys_are_required_and_must_be_empty(tmp_path: Path) -> None:
    root = _tracked_fixture_repository(tmp_path)
    nonempty_key = "SUBMISSION_PROJECT_ROOT"
    missing_key = "SUBMISSION_HOST_MACHINE"
    (root / ".env.example").write_text(
        "".join(
            f"{key}={'nonempty-fixture-value' if key == nonempty_key else ''}\n"
            for key in REQUIRED_SENSITIVE_EXAMPLE_KEYS
            if key != missing_key
        ),
        encoding="utf-8",
    )
    assert _git(root, "add", ".env.example").returncode == 0

    findings = audit_repository(root)

    assert ("EXAMPLE_SENSITIVE_VALUE", ".env.example") in findings
    assert ("EXAMPLE_SENSITIVE_KEY_MISSING", ".env.example") in findings
    assert "nonempty-fixture-value" not in repr(findings)


def test_duplicate_example_key_cannot_hide_an_earlier_nonempty_value(
    tmp_path: Path,
) -> None:
    root = _tracked_fixture_repository(tmp_path)
    lines = [f"{key}=\n" for key in REQUIRED_SENSITIVE_EXAMPLE_KEYS]
    lines.extend(
        (
            f"{FIXTURE_NOTION_TOKEN_KEY}=nonempty-fixture-value\n",
            f"{FIXTURE_NOTION_TOKEN_KEY}=\n",
        )
    )
    (root / ".env.example").write_text("".join(lines), encoding="utf-8")
    assert _git(root, "add", ".env.example").returncode == 0

    findings = audit_repository(root)

    assert ("EXAMPLE_DUPLICATE_KEY", ".env.example") in findings
    assert ("EXAMPLE_SENSITIVE_VALUE", ".env.example") in findings
    assert "nonempty-fixture-value" not in repr(findings)


def test_fixed_report_destination_does_not_require_a_sensitive_key_suffix(
    tmp_path: Path,
) -> None:
    root = _tracked_fixture_repository(tmp_path)
    value = "fixture-destination-that-must-not-be-reported"
    (root / "report.py").write_text(
        f'{FIXTURE_REPORT_DESTINATION_KEY} = "{value}"\n', encoding="utf-8"
    )
    assert _git(root, "add", "report.py").returncode == 0

    findings = audit_repository(root)

    assert ("FIXED_REPORT_DESTINATION", "report.py") in findings
    assert value not in repr(findings)


def test_sensitive_assignment_syntaxes_are_detected_without_reporting_values(
    tmp_path: Path,
) -> None:
    root = _tracked_fixture_repository(tmp_path)
    value = "syntax-fixture-secret-value"
    sources = {
        "dotenv.env.sample": f"{FIXTURE_API_TOKEN_KEY}={value}\n",
        "bytes.py": f'{FIXTURE_BYTE_PASSWORD_KEY} = b"{value}"\n',
        "raw.py": f'{FIXTURE_RAW_TOKEN_KEY} = r"{value}"\n',
        "unicode.py": f'{FIXTURE_UNICODE_SECRET_KEY} = u"{value}"\n',
        "format.py": f'{FIXTURE_FORMAT_APP_ID_KEY} = f"{value}"\n',
        "javascript.js": f"const {FIXTURE_TARGET_CHAT_ID_KEY} = `{value}`;\n",
    }
    for relative_path, content in sources.items():
        (root / relative_path).write_text(content, encoding="utf-8")
    assert _git(root, "add", *sources).returncode == 0

    findings = audit_repository(root)

    for relative_path in sources:
        assert ("SENSITIVE_ASSIGNMENT", relative_path) in findings
    assert value not in repr(findings)


def test_personal_home_paths_include_root_and_escaped_windows_source(
    tmp_path: Path,
) -> None:
    root = _tracked_fixture_repository(tmp_path)
    escaped_windows_home = "C:" + "\\\\" + "Users" + "\\\\alice\\\\project"
    sources = {
        "root-home.txt": "/" + "root" + "/private/project\n",
        "windows-source.py": f'PATH = "{escaped_windows_home}"\n',
        "windows.json": f'{{"path": "{escaped_windows_home}"}}\n',
    }
    for relative_path, content in sources.items():
        (root / relative_path).write_text(content, encoding="utf-8")
    assert _git(root, "add", *sources).returncode == 0

    findings = audit_repository(root)

    for relative_path in sources:
        assert ("PERSONAL_HOME_PATH", relative_path) in findings


def test_cli_audits_index_and_never_prints_matching_values(tmp_path: Path) -> None:
    root = _tracked_fixture_repository(tmp_path)
    fixture_value = "cli-fixture-secret-that-must-not-leak"
    indexed = root / "indexed.py"
    indexed.write_text(
        f'{FIXTURE_FIXED_PASSWORD_KEY} = "{fixture_value}"\n', encoding="utf-8"
    )
    assert _git(root, "add", "indexed.py").returncode == 0
    indexed.write_text("VALUE = 1\n", encoding="utf-8")
    cli = _copy_audit_cli(root)

    completed = subprocess.run(
        [sys.executable, str(cli)],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "SENSITIVE_ASSIGNMENT indexed.py" in completed.stdout.splitlines()
    assert all(
        re.fullmatch(r"[A-Z_]+ [^\r\n]+", line)
        for line in completed.stdout.splitlines()
    )
    assert fixture_value not in completed.stdout
    assert fixture_value not in completed.stderr


def test_cli_prints_exact_success_marker_for_a_clean_index(tmp_path: Path) -> None:
    root = tmp_path / "clean-repo"
    root.mkdir()
    assert _git(root, "init", "-q").returncode == 0
    (root / ".env.example").write_text(
        "".join(f"{key}=\n" for key in REQUIRED_SENSITIVE_EXAMPLE_KEYS),
        encoding="utf-8",
    )
    (root / ".gitignore").write_text(
        _public_release_gitignore(), encoding="utf-8"
    )
    (root / "README.md").write_text("Portable project.\n", encoding="utf-8")
    assert (
        _git(root, "add", ".env.example", ".gitignore", "README.md").returncode
        == 0
    )
    cli = _copy_audit_cli(root)

    completed = subprocess.run(
        [sys.executable, str(cli)],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stdout == "PUBLIC_RELEASE_AUDIT=ok\n"
    assert completed.stderr == ""


def test_private_runtime_signing_and_vm_files_are_ignored_but_examples_are_allowed() -> None:
    ignored = (
        ".env",
        "config/workflow.env",
        "nested/service.env",
        "runtime/public-release-audit.json",
        "signing/public-release-audit.p8",
        "signing/public-release-audit.p12",
        "signing/public-release-audit.mobileprovision",
        "vm/public-release-audit.qcow2",
        "vm/public-release-audit.vmdk",
        "vm/public-release-audit.utm/config.plist",
    )
    allowed = (
        ".env.example",
        "config/workflow.env.example",
        "nested/service.env.example",
    )

    missing = [
        path
        for path in ignored
        if _git(ROOT, "check-ignore", "-q", "--no-index", "--", path).returncode
        != 0
    ]
    wrongly_ignored = [
        path
        for path in allowed
        if _git(ROOT, "check-ignore", "-q", "--no-index", "--", path).returncode
        == 0
    ]

    assert not missing, f"missing ignore rules: {missing}"
    assert not wrongly_ignored, f"example files must remain allowed: {wrongly_ignored}"
    assert _git(ROOT, "ls-files", "--error-unmatch", ".env.example").returncode == 0


def test_audit_probes_nested_env_and_nested_example_from_index(tmp_path: Path) -> None:
    root = _tracked_fixture_repository(tmp_path)
    without_env_glob = _public_release_gitignore().replace("*.env\n", "")
    (root / ".gitignore").write_text(without_env_glob, encoding="utf-8")
    assert _git(root, "add", ".gitignore").returncode == 0

    findings = audit_repository(root)

    assert ("IGNORE_RULE_MISSING", "nested/service.env") in findings

    (root / ".gitignore").write_text(
        _public_release_gitignore() + "nested/service.env.example\n",
        encoding="utf-8",
    )
    assert _git(root, "add", ".gitignore").returncode == 0

    findings = audit_repository(root)

    assert ("EXAMPLE_FILE_IGNORED", "nested/service.env.example") in findings


def test_gitignore_wildcard_does_not_cross_directory_boundaries(
    tmp_path: Path,
) -> None:
    root = _tracked_fixture_repository(tmp_path)
    ignore_text = _public_release_gitignore().replace("*.utm/\n", "")
    (root / ".gitignore").write_text(
        ignore_text + "vm/*.plist\n", encoding="utf-8"
    )
    assert _git(root, "add", ".gitignore").returncode == 0

    findings = audit_repository(root)

    assert (
        "IGNORE_RULE_MISSING",
        "vm/public-release-audit.utm/config.plist",
    ) in findings


def test_current_repository_has_representative_baseline_findings() -> None:
    findings = set(audit_repository(ROOT))
    representative_findings = {
        ("PERSONAL_HOME_PATH", "README.md"),
        ("SENSITIVE_ASSIGNMENT", "scripts/ssh_password.py"),
        ("FIXED_REPORT_DESTINATION", "services/feishu_bot.py"),
        (
            "HISTORICAL_PLAN",
            "docs/superpowers/plans/2026-08-18-public-release-hardening.md",
        ),
    }

    assert findings
    assert {rule for rule, _ in findings} <= KNOWN_RULES
    assert representative_findings <= findings
