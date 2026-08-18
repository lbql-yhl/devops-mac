#!/usr/bin/env python3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/utm-p8/SKILL.md"
DOC = ROOT / "docs/utm-p8.md"


def test_utm_p8_uses_only_the_host_entry_in_documentation() -> None:
    skill = SKILL.read_text(encoding="utf-8")
    doc = DOC.read_text(encoding="utf-8")
    combined = "\n".join((skill, doc))

    for required in (
        "$PROJECT_ROOT/scripts/utm_p8.py",
        "UTM_P8=verified",
        "SHARED_APP_ASSETS_CLEANED=verified",
        "--cleanup-only",
        "不要求 VM 运行",
        "交接 `utm-21`",
        "不读取或解释脚本内容",
    ):
        assert required in combined, required

    for forbidden in (
        "Team Keys",
        "appstoreconnect.apple.com/access/integrations/api",
        "ACTIVE_API_KEY_COUNT",
        "notify-review-success",
        "REVIEW_SUCCESS_NOTIFICATION",
        "随机哨兵",
        "完整 Downloads",
        "第一条",
    ):
        assert forbidden not in combined, forbidden


def test_utm_p8_documentation_excludes_internal_write_details() -> None:
    combined = "\n".join(
        (
            SKILL.read_text(encoding="utf-8"),
            DOC.read_text(encoding="utf-8"),
        )
    )
    for forbidden in (
        "$PROJECT_ROOT/scripts/notion_api.py",
        "verify-parent",
        "更新信息",
        "退款回调及p8",
        "issuer id: ",
        "key id:",
        "p8文件内容：",
        "before",
        "write-toggle-code",
        "after",
        "read-toggle-code",
        "NOTION_ROLLBACK=verified",
        "独立回读",
    ):
        assert forbidden not in combined, forbidden


def test_utm_p8_is_a_single_script_entry() -> None:
    skill = SKILL.read_text(encoding="utf-8")
    assert skill.count("$PROJECT_ROOT/scripts/utm_p8.py") == 2
    assert "python3 -c" not in skill
    assert "ssh " not in skill
