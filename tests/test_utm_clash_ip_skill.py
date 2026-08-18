from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "utm-clash-ip" / "SKILL.md"


def test_utm_clash_ip_unifies_notion_proxy_clash_egress_and_environment() -> None:
    text = SKILL.read_text(encoding="utf-8")

    assert "name: utm-clash-ip" in text
    assert "utm-login" in text
    assert "UTM_CLASH_IP=verified" in text
    assert "$PROJECT_ROOT/scripts/utm_clash_ip.py" in text
    assert "--application-name '<app-name>'" in text
    assert "不读取或解释脚本内容" in text


def test_utm_clash_ip_skill_and_doc_exclude_script_implementation() -> None:
    skill = SKILL.read_text(encoding="utf-8")
    docs = (ROOT / "docs" / "utm-clash-ip.md").read_text(encoding="utf-8")

    for text in (skill, docs):
        assert "不读取或解释脚本内容" in text
        assert "socks-port: 7891" not in text
        assert "DOMAIN-SUFFIX" not in text


def test_utm_clash_ip_docs_publish_mainline_and_explicit_test_entries() -> None:
    skill = SKILL.read_text(encoding="utf-8")
    docs = (ROOT / "docs" / "utm-clash-ip.md").read_text(encoding="utf-8")

    for text in (skill, docs):
        assert "$PROJECT_ROOT/scripts/utm_clash_ip.py" in text
        assert "--application-name '<app-name>'" in text
        assert "$PROJECT_ROOT/scripts/utm_clash_ip_explicit_test.py" not in text
        assert "UTM_CLASH_IP=verified" in text


def test_utm_clash_ip_documents_do_not_describe_internal_recovery() -> None:
    skill = SKILL.read_text(encoding="utf-8")
    docs = (ROOT / "docs" / "utm-clash-ip.md").read_text(encoding="utf-8")

    for text in (skill, docs):
        for forbidden in (
            "CalledProcessError",
            "CONFIG_ROLLBACK=verified",
            "CONFIG_RECOVERY_DIAGNOSTICS=verified",
            "CONFIG_REPAIR_RETRY=verified",
            "CONFIG_REPAIR_RETRY_LIMIT=exhausted",
            "mode-600",
            "5 秒",
            "脚本自动",
            "同一目标只允许一次修复重试",
            "再次失败",
        ):
            assert forbidden not in text
