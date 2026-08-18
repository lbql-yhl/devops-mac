#!/usr/bin/env python3
"""Apple Account 登录入口。

账号和密码只从环境变量或隐藏输入读取，不写入命令行参数、源码或日志。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time


def stop_previous_login_processes() -> None:
    """每次启动前结束同一脚本的旧实例，确保只运行最新进程。"""
    script_path = os.path.realpath(__file__)
    current_pid = os.getpid()
    try:
        output = subprocess.check_output(
            ["ps", "-axo", "pid=,command="],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return

    previous_pids = []
    for line in output.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2:
            continue
        try:
            pid = int(fields[0])
        except ValueError:
            continue
        if pid != current_pid and script_path in fields[1]:
            previous_pids.append(pid)

    for pid in previous_pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError:
            continue

    deadline = time.monotonic() + 2.0
    remaining = set(previous_pids)
    while remaining and time.monotonic() < deadline:
        for pid in tuple(remaining):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                remaining.remove(pid)
            except PermissionError:
                remaining.remove(pid)
        if remaining:
            time.sleep(0.1)

    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    if previous_pids:
        print(
            "已结束旧登录脚本进程，正在启动新流程。",
            flush=True,
        )


def main() -> int:
    stop_previous_login_processes()
    import find_system_settings_general as implementation

    parser = argparse.ArgumentParser(description="Sign in to Apple Account")
    parser.add_argument(
        "--stdin-json",
        action="store_true",
        help="从标准输入读取环境变量 JSON；不在命令行显示账号值",
    )
    parser.add_argument("--handle-mac-password", action="store_true")
    parser.add_argument("--email-var", default="APPLE_ACCOUNT_EMAIL")
    parser.add_argument("--password-var", default="APPLE_ACCOUNT_PASSWORD")
    parser.add_argument("--phone-var", default="APPLE_ACCOUNT_PHONE")
    parser.add_argument("--sms-url-var", default="APPLE_ACCOUNT_SMS_URL")
    args = parser.parse_args()
    if args.stdin_json:
        try:
            payload = json.load(sys.stdin)
        except (ValueError, TypeError) as error:
            print("stdin JSON 无效：{}".format(error), file=sys.stderr)
            return 6
        if not isinstance(payload, dict):
            print("stdin JSON 必须是对象。", file=sys.stderr)
            return 6
        for variable in (
            args.email_var,
            args.password_var,
            args.phone_var,
            args.sms_url_var,
        ):
            value = payload.get(variable)
            if not isinstance(value, str) or not value:
                print("stdin JSON 缺少必需账号变量。", file=sys.stderr)
                return 6
            os.environ[variable] = value
    command = []
    if args.handle_mac_password:
        command.append("--handle-mac-password")
    else:
        command.extend([
            "--login",
            "--email-var", args.email_var,
            "--password-var", args.password_var,
            "--phone-var", args.phone_var,
            "--sms-url-var", args.sms_url_var,
        ])
    return int(implementation.main(command))


if __name__ == "__main__":
    raise SystemExit(main())
