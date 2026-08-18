from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_proxy_work_is_one_clash_ip_stage_without_development_environment() -> None:
    from scripts.preflight import ORDERED, STANDALONE

    assert ORDERED[:4] == ("utm-vm-clone", "utm-notion", "utm-clash-ip", "utm-login")
    assert not {"utm-5", "files", "utm-clash", "utm-6"}.intersection(ORDERED)
    assert STANDALONE == ()

    text = (ROOT / "skills" / "utm-clash-ip" / "SKILL.md").read_text(encoding="utf-8")
    for forbidden in (
        "ZSHRC=verified",
        "/usr/local/opt/ruby/bin",
    ):
        assert forbidden not in text
    assert "$PROJECT_ROOT/scripts/utm_clash_ip.py" in text
    assert "不读取或解释脚本内容" in text
    assert "UTM_CLASH_IP=verified" in text
