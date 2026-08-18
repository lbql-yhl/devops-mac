#!/usr/bin/env python3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"


def read(name: str) -> str:
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


def main() -> None:
    clash_ip = read("utm-clash-ip")
    assert "expected_proxy_ip='<proxy-ip" not in clash_ip
    for required in (
        "ipaddress.IPv4Address",
        "SOCKS5_SHARED_READBACK=exact",
        "PROXY_EGRESS=verified",
    ):
        assert required in clash_ip, required

    utm_8 = read("utm-edit")
    for required in (
        "scripts/utm_8_change_password.py",
        "APPLE_ACCOUNT_NEW_PASSWORD",
        "MODIFIED_PASSWORD_SOURCE=verified",
        "Sign-In & Security",
        "Change Password",
    ):
        assert required in utm_8, required
    for stale in ("secrets.choice", "初始密码：", "用户名：", "生日："):
        assert stale not in utm_8, stale

    utm_9 = read("utm-key")
    docs_9 = (ROOT / "docs" / "utm-key.md").read_text(encoding="utf-8")
    for required in (
        "$PROJECT_ROOT/scripts/utm_9.py",
        "CSR_ATTEMPT_ID",
        "CSR_PATH=/Users/example/Desktop/CertificateSigningRequest.certSigningRequest",
        "CSR_PRIVATE_KEY=verified",
        "CSR_DISK=verified",
        "/usr/bin/certtool",
    ):
        assert required in utm_9, required
    for stale in ("Computer Use", 'open -a "Keychain Access"', "SUBMISSION_SSH_PRIVATE_KEY"):
        assert stale not in utm_9
        assert stale not in docs_9

    utm_apps = read("utm-apps")
    body = [line.strip() for line in utm_apps.split("---", 2)[-1].splitlines() if line.strip()]
    assert body == [
        "1. 运行 utm-10 技能脚本",
        "2. 运行 utm-11 技能脚本",
        "3. 运行 utm-12 技能脚本",
        "4. 运行 utm-13 技能脚本",
    ]

    utm_business = read("utm-business")
    for required in (
        "$PROJECT_ROOT/scripts/utm_business.py",
        "/Users/example/Downloads/AppleAccountScriptsBackup/utm_business_one.mjs",
        "SSH stdin JSON",
        "FOREIGN_STATUS_FORM=verified",
        "W8BEN_SUBMIT=verified",
        "BANK_ACCOUNT_PROCESSING=verified",
        "DAC7_READBACK=No_saved",
        "UTM_BUSINESS=verified",
    ):
        assert required in utm_business, required

    print("MIDDLE_SKILL_REGRESSIONS=verified")


if __name__ == "__main__":
    main()
