#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.project_paths import PROJECT_ROOT


RUNS_FILE = PROJECT_ROOT / "runtime" / "feishu-runs.json"


def normalize_us_phone(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    if not digits:
        return ""
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return f"+1{digits}"


def format_account_block(data: dict[str, Any]) -> str:
    dev = data.get("developer_account") or {}
    proxy = data.get("proxy") or {}
    bank = data.get("bank_info") or {}
    rows = [
        ("用户名：", ""),
        ("邮箱：", dev.get("email", "")),
        ("初始密码：", dev.get("password", "")),
        ("修改后的密码：", ""),
        ("电话：", normalize_us_phone(dev.get("phone", ""))),
        ("电话短信接收平台：", dev.get("sms_url", "")),
        ("生日：", ""),
        ("team ID:", ""),
        ("APP_ID：", ""),
        ("Renewal date：", ""),
        ("代理ip:", proxy.get("host", "")),
        ("代理端口:", proxy.get("port", "")),
        ("代理用户名：", proxy.get("username", "")),
        ("代理用户密码：", proxy.get("password", "")),
        ("代码链接：", data.get("code_link", "")),
        ("ABA Routing Number：", bank.get("aba_routing_number", "")),
        ("Account Number：", bank.get("account_number", "")),
    ]
    return "\n\n".join(f"{label}{value}" for label, value in rows)


def validate_account_block(text: str) -> None:
    labels = [
        "用户名：",
        "邮箱：",
        "初始密码：",
        "修改后的密码：",
        "电话：",
        "电话短信接收平台：",
        "生日：",
        "team ID:",
        "APP_ID：",
        "Renewal date：",
        "代理ip:",
        "代理端口:",
        "代理用户名：",
        "代理用户密码：",
        "代码链接：",
        "ABA Routing Number：",
        "Account Number：",
    ]
    lines = text.splitlines()
    if len(lines) != 33:
        raise RuntimeError("Account block must keep the 33-line template spacing")
    if any(lines[index] for index in range(1, 33, 2)):
        raise RuntimeError("Account block must keep one blank line between fields")
    mismatched = [label for label, line in zip(labels, lines[::2]) if not line.startswith(label)]
    if mismatched:
        raise RuntimeError(f"Account block label/order mismatch: {', '.join(mismatched)}")


def load_submission(run_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not RUNS_FILE.is_file():
        raise RuntimeError("Feishu runs file is missing")
    payload = json.loads(RUNS_FILE.read_text(encoding="utf-8"))
    matches = [run for run in payload.get("runs", []) if str(run.get("id") or "") == run_id]
    if len(matches) != 1:
        raise RuntimeError(f"Run match count must be one, got {len(matches)}")

    run = matches[0]
    data = run.get("submission_data") or {}
    local_host = os.getenv("SUBMISSION_HOST_MACHINE", "").strip()
    run_host = str(data.get("host_machine") or run.get("host_machine") or "").strip()
    if not local_host or run_host != local_host:
        raise RuntimeError("Run host ownership mismatch")

    vm_name = str(run.get("vm_name") or "")
    if not re.fullmatch(r"[a-z]{4}", vm_name):
        raise RuntimeError("Run vm_name must be four lowercase letters")
    app_name = str(run.get("app_name") or "")
    if not app_name or str(data.get("app_name") or "") != app_name:
        raise RuntimeError("Run application identity mismatch")
    return data, run


def atomic_write_text(path: Path, text: str) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError(f"Unsafe output path: {path}")
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            tmp = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        tmp = None
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("Output is not a regular file")
        if path.stat().st_mode & 0o777 != 0o600:
            raise RuntimeError("Output mode is not 600")
        if path.read_text(encoding="utf-8") != text:
            raise RuntimeError("Output readback mismatch")
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def copy_to_clipboard(text: str) -> None:
    subprocess.run(["pbcopy"], input=text, text=True, check=True)
    copied = subprocess.check_output(["pbpaste"], text=True)
    if copied != text:
        raise RuntimeError("Clipboard verification failed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--vm-name", default="")
    parser.add_argument("--copy", action="store_true")
    parser.add_argument("--out", default="/tmp/notion_utm_account_block.txt")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    data, run = load_submission(args.run_id)
    vm_name = str(run["vm_name"])
    if args.vm_name and args.vm_name != vm_name:
        raise RuntimeError("CLI vm_name does not match the run")
    account_block = format_account_block(data)
    validate_account_block(account_block)
    page_title = f"{data['app_name']}-{vm_name}"
    payload = {
        "run_id": args.run_id,
        "page_title": page_title,
        "submission_data": data,
        "account_block": account_block,
    }

    atomic_write_text(Path(args.out), account_block)
    if args.json_out:
        atomic_write_text(Path(args.json_out), json.dumps(payload, ensure_ascii=False, indent=2))
    if args.copy:
        copy_to_clipboard(account_block)

    print(json.dumps({
        "run_id": args.run_id,
        "run_host": "verified",
        "vm_name": vm_name,
        "page_title": page_title,
        "out": args.out,
        "bytes": len(account_block.encode("utf-8")),
        "lines": len(account_block.splitlines()),
        "json_out": bool(args.json_out),
        "copied": args.copy,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
