#!/usr/bin/env python3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    clash_ip = (ROOT / "skills" / "utm-clash-ip" / "SKILL.md").read_text(encoding="utf-8")
    for required in (
        "代理ip:", "代理端口:", "代理用户名：", "代理用户密码：",
        "SOCKS5_SHARED_READBACK=exact", "SOCKS5_GUEST_READBACK=exact",
        "PROXY_EGRESS=verified", "password_environment()", "scp_args(",
    ):
        assert required in clash_ip, required
    for forbidden in ("SUBMISSION_SSH_PRIVATE_KEY", '"-o", "BatchMode=yes"'):
        assert forbidden not in clash_ip, forbidden

    combined = (ROOT / "skills" / "utm-vm-clone" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "--vm-name" not in combined
    for required in (
        "UTM_CLONE_MACOS=verified",
        "DEMO_HOME=absent",
        "VM_SHUTDOWN=verified",
        "DATABASE_AVAILABLE=1",
        "UTM_CLONE_AND_INITIALIZE=verified",
    ):
        assert required in combined, required

    ssh_password = (ROOT / "scripts/ssh_password.py").read_text(encoding="utf-8")
    assert "IdentityFile" not in ssh_password
    assert "BatchMode=yes" not in ssh_password

    notion = (ROOT / "skills" / "utm-notion" / "SKILL.md").read_text(encoding="utf-8")
    assert "$PROJECT_ROOT/scripts/utm_notion.py" in notion
    assert "空值保留模板标签" in notion
    assert '"status": "verified"' in notion

    print("EARLY_RUNTIME_SAFETY=verified")


if __name__ == "__main__":
    main()
