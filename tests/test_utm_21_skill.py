#!/usr/bin/env python3
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/utm-21/SKILL.md"
HELPER = ROOT / "scripts/utm_21_clone.py"
DOC = ROOT / "docs/utm-21.md"
AUTHORITATIVE_SURFACES = (
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    ROOT / "docs/utm-feishu-bot.md",
    ROOT / "shared-files/README.md",
    ROOT / ".gitignore",
)


def main() -> None:
    skill = SKILL.read_text(encoding="utf-8")
    helper = HELPER.read_text(encoding="utf-8")
    doc = DOC.read_text(encoding="utf-8")
    combined = "\n".join((skill, doc, helper))
    authoritative = "\n".join(
        path.read_text(encoding="utf-8") for path in AUTHORITATIVE_SURFACES
    )

    for value in (
        "scripts/utm_21_clone.py", "CODEUP_USERNAME", "CODEUP_PASSWORD",
        "stdin_memory_only", "REPO_STATE=existing_pristine_verified",
        "resumable", "unrepairable", "origin/main", "git fsck --full",
        "PLACEHOLDERS_ALREADY_REPLACED=verified", "com.example.test.demok1",
        "com.example.<app_name>", "5372311233", "jltest.test.test",
        "git grep -I -i", "git grep -Ilz", "git diff --check",
        "RUNNER_WORKSPACE_SOURCE=existing_verified",
        "DEPENDENCY_INSTALL_COMMANDS=not_run",
        "直接交接 `utm-22`", "UTM_21=verified",
    ):
        assert value in combined, value

    forbidden_residue = (
        "tools/" + "flutter",
        "Flutter " + "SDK",
        "Flutter " + "cache",
        "flutter pub" + " get",
        "command -v " + "flutter",
        "command -v " + "dart",
        "PUB_HOSTED" + "_URL",
        "FLUTTER_STORAGE" + "_BASE_URL",
        "pod " + "install",
        "Cocoa" + "Pods",
        "PUB_GET" + "_EXIT",
        "PUB_GET" + "_OUTPUT",
        "POD_INSTALL" + "_EXIT",
        "POD_INSTALL" + "_OUTPUT",
        ".dart_tool/" + "package_config.json",
        "ios/" + "Pods",
        "Podfile" + ".lock",
    )
    for value in forbidden_residue:
        assert value not in combined, value
        assert value not in authoritative, value

    assert not (ROOT / "shared-files/tools/README.md").exists()

    for value in (
        "clone --branch main --single-branch", "credential.helper",
        "git remote get-url origin", "git rev-parse refs/remotes/origin/main",
        "git fsck --full", "unset CODEUP_USERNAME CODEUP_PASSWORD",
    ):
        assert value in helper, value

    assert not re.search(r"CODEUP_(?:USERNAME|PASSWORD)\s*=\s*['\"][^'\"]+['\"]", skill)
    assert not re.search(r"https://[^\s/]+:[^\s@]+@", combined)
    assert "runtime/feishu-runs.json" not in skill
    assert "docs/superpowers" not in combined
    assert "目标已存在时不删除、不覆盖、不换目录；向原" not in doc
    assert "四项总命中为 0 时禁止替换和继续，向原" not in doc

    print("UTM_21_SECURE_RECOVERY=verified")


if __name__ == "__main__":
    main()
