#!/usr/bin/env python3
"""Keep the Feishu bot and public tunnel running.

The supervisor restarts the local bot service and the Cloudflare tunnel when
either exits. It also writes the current public callback URL into runtime files.
"""

from __future__ import annotations

import os
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import error, request

try:
    from host_config import (
        secure_append_text,
        secure_runtime_file,
        secure_runtime_root,
        secure_write_text,
    )
except ModuleNotFoundError:
    from services.host_config import (
        secure_append_text,
        secure_runtime_file,
        secure_runtime_root,
        secure_write_text,
    )


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = ROOT / "runtime"
SUPERVISOR_LOG = RUNTIME_DIR / "feishu_supervisor.log"
PUBLIC_URL_FILE = RUNTIME_DIR / "public_url.txt"
CALLBACK_URL_FILE = RUNTIME_DIR / "feishu_callback_url.txt"
BOT_PID_FILE = RUNTIME_DIR / "feishu_bot.pid"
WS_PID_FILE = RUNTIME_DIR / "feishu_ws.pid"
POLLER_PID_FILE = RUNTIME_DIR / "feishu_poller.pid"
CLOUDFLARED_PID_FILE = RUNTIME_DIR / "cloudflared.pid"
LOCAL_CLOUDFLARED = ROOT / "tools" / "bin" / ("cloudflared.exe" if os.name == "nt" else "cloudflared")

TRY_CLOUDFLARE_RE = re.compile(r"https://(?!api\.)[a-zA-Z0-9-]+\.trycloudflare\.com")


def harden_runtime_metadata() -> None:
    """Secure known supervisor metadata without traversing shared runtime."""
    secure_runtime_root(ROOT)
    secure_runtime_root(RUNTIME_DIR, create=True)
    for metadata_file in (
        SUPERVISOR_LOG,
        PUBLIC_URL_FILE,
        CALLBACK_URL_FILE,
        BOT_PID_FILE,
        WS_PID_FILE,
        POLLER_PID_FILE,
        CLOUDFLARED_PID_FILE,
    ):
        secure_runtime_file(metadata_file)


def build_no_proxy_opener() -> request.OpenerDirector:
    return request.build_opener(request.ProxyHandler({}))


def process_group_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def resolve_cloudflared() -> str:
    configured = os.getenv("CLOUDFLARED_BIN", "").strip()
    if configured:
        return configured
    if LOCAL_CLOUDFLARED.exists():
        return str(LOCAL_CLOUDFLARED)
    found = shutil.which("cloudflared")
    if found:
        return found
    return "cloudflared"


class Supervisor:
    def __init__(self) -> None:
        self.host = os.getenv("FEISHU_BOT_HOST", "0.0.0.0")
        self.port = int(os.getenv("FEISHU_BOT_PORT", "8787"))
        self.bot: subprocess.Popen[str] | None = None
        self.ws: subprocess.Popen[str] | None = None
        self.poller: subprocess.Popen[str] | None = None
        self.tunnel: subprocess.Popen[str] | None = None
        self.stopping = False
        self.last_public_url = ""
        self.bot_failed_checks = 0
        self.poller_enabled = bool(os.getenv("FEISHU_POLL_CHAT_IDS", "").strip())
        self.ws_enabled = os.getenv("FEISHU_WS_ENABLED", "1").lower() in {"1", "true", "yes"}
        self.tunnel_enabled = os.getenv("FEISHU_TUNNEL_ENABLED", "1").lower() in {"1", "true", "yes"}
        self.tunnel_failed_checks = 0
        self.cloudflared_bin = resolve_cloudflared()
        self.cloudflared_protocol = os.getenv("CLOUDFLARED_PROTOCOL", "").strip()
        self.public_health_check = os.getenv("FEISHU_PUBLIC_HEALTH_CHECK", "").lower() in {
            "1",
            "true",
            "yes",
        }

    def log(self, message: str) -> None:
        line = f"[{datetime.now().isoformat(timespec='seconds')}] {message}"
        print(line, flush=True)
        secure_append_text(SUPERVISOR_LOG, line + "\n")

    def start_bot(self) -> None:
        if self.bot and self.bot.poll() is None:
            return
        cmd = [
            sys.executable,
            "-u",
            str(ROOT / "services" / "feishu_bot.py"),
            "--host",
            self.host,
            "--port",
            str(self.port),
        ]
        self.bot = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **process_group_kwargs(),
        )
        secure_write_text(BOT_PID_FILE, str(self.bot.pid))
        self.log(f"started feishu bot pid={self.bot.pid}")
        threading.Thread(target=self.pipe_output, args=("bot", self.bot), daemon=True).start()

    def start_ws(self) -> None:
        if not self.ws_enabled:
            return
        if self.ws and self.ws.poll() is None:
            return
        cmd = [
            sys.executable,
            "-u",
            str(ROOT / "services" / "feishu_bot.py"),
            "ws",
        ]
        self.ws = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **process_group_kwargs(),
        )
        secure_write_text(WS_PID_FILE, str(self.ws.pid))
        self.log(f"started feishu ws pid={self.ws.pid}")
        threading.Thread(target=self.pipe_output, args=("ws", self.ws), daemon=True).start()

    def start_poller(self) -> None:
        if not self.poller_enabled:
            return
        if self.poller and self.poller.poll() is None:
            return
        cmd = [
            sys.executable,
            "-u",
            str(ROOT / "services" / "feishu_bot.py"),
            "poll",
        ]
        self.poller = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **process_group_kwargs(),
        )
        secure_write_text(POLLER_PID_FILE, str(self.poller.pid))
        self.log(f"started feishu poller pid={self.poller.pid}")
        threading.Thread(target=self.pipe_output, args=("poller", self.poller), daemon=True).start()

    def start_tunnel(self) -> None:
        if not self.tunnel_enabled:
            return
        if self.tunnel and self.tunnel.poll() is None:
            return
        cmd = [
            self.cloudflared_bin,
            "tunnel",
            "--url",
            f"http://127.0.0.1:{self.port}",
            "--no-autoupdate",
        ]
        if self.cloudflared_protocol:
            cmd.extend(["--protocol", self.cloudflared_protocol])
        self.tunnel = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **process_group_kwargs(),
        )
        secure_write_text(CLOUDFLARED_PID_FILE, str(self.tunnel.pid))
        self.log(f"started cloudflared pid={self.tunnel.pid}")
        threading.Thread(target=self.pipe_output, args=("cloudflared", self.tunnel), daemon=True).start()

    def pipe_output(self, name: str, proc: subprocess.Popen[str]) -> None:
        if proc.stdout is None:
            return
        for raw_line in proc.stdout:
            line = raw_line.rstrip()
            if not line:
                continue
            self.log(f"{name}: {line}")
            if name == "cloudflared":
                match = TRY_CLOUDFLARE_RE.search(line)
                if match:
                    self.set_public_url(match.group(0))

    def set_public_url(self, public_url: str) -> None:
        if public_url == self.last_public_url:
            return
        self.last_public_url = public_url
        callback_url = public_url.rstrip("/") + "/feishu/events"
        secure_write_text(PUBLIC_URL_FILE, public_url + "\n")
        secure_write_text(CALLBACK_URL_FILE, callback_url + "\n")
        self.log(f"public url: {public_url}")
        self.log(f"feishu callback url: {callback_url}")

    def health_ok(self) -> bool:
        try:
            with build_no_proxy_opener().open(f"http://127.0.0.1:{self.port}/health", timeout=5) as resp:
                return resp.status == 200
        except (error.URLError, TimeoutError, OSError):
            return False

    def public_health_ok(self) -> bool:
        if not self.last_public_url:
            return False
        try:
            with request.urlopen(f"{self.last_public_url.rstrip('/')}/health", timeout=8) as resp:
                return resp.status == 200
        except (error.URLError, TimeoutError, OSError):
            return False

    def terminate_proc(self, name: str, proc: subprocess.Popen[str] | None) -> None:
        if not proc or proc.poll() is not None:
            return
        self.log(f"stopping {name} pid={proc.pid}")
        if os.name == "nt":
            proc.terminate()
        else:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.log(f"killing {name} pid={proc.pid}")
            if os.name == "nt":
                proc.kill()
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def restart_bot(self) -> None:
        self.terminate_proc("bot", self.bot)
        self.bot = None
        self.bot_failed_checks = 0
        time.sleep(1)
        self.start_bot()

    def restart_tunnel(self) -> None:
        self.terminate_proc("cloudflared", self.tunnel)
        self.tunnel = None
        self.tunnel_failed_checks = 0
        self.last_public_url = ""
        time.sleep(1)
        self.start_tunnel()

    def stop(self, *_args: object) -> None:
        self.stopping = True

    def run(self) -> int:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        self.log("supervisor starting")
        if not self.tunnel_enabled:
            # Long-connection mode does not use an HTTP callback URL. Remove
            # stale quick-tunnel metadata so operators cannot follow a dead
            # address while diagnosing callback delivery.
            for stale_path in (PUBLIC_URL_FILE, CALLBACK_URL_FILE):
                try:
                    stale_path.unlink()
                except FileNotFoundError:
                    pass
        self.start_bot()
        self.start_ws()
        time.sleep(2)
        self.start_poller()
        self.start_tunnel()

        while not self.stopping:
            if self.bot and self.bot.poll() is not None:
                self.log(f"bot exited code={self.bot.returncode}; restarting")
                self.bot = None
                self.start_bot()

            if not self.health_ok():
                self.bot_failed_checks += 1
                self.log(f"bot health check failed count={self.bot_failed_checks}")
                if self.bot_failed_checks >= 3:
                    self.restart_bot()
            else:
                self.bot_failed_checks = 0

            if self.ws_enabled and self.ws and self.ws.poll() is not None:
                self.log(f"ws exited code={self.ws.returncode}; restarting")
                self.ws = None
                self.start_ws()

            if self.poller_enabled and self.poller and self.poller.poll() is not None:
                self.log(f"poller exited code={self.poller.returncode}; restarting")
                self.poller = None
                self.start_poller()

            if self.tunnel_enabled and self.tunnel and self.tunnel.poll() is not None:
                self.log(f"cloudflared exited code={self.tunnel.returncode}; restarting")
                self.tunnel = None
                self.start_tunnel()
            elif self.tunnel_enabled and self.public_health_check and self.last_public_url and not self.public_health_ok():
                self.tunnel_failed_checks += 1
                self.log(f"public tunnel health check failed count={self.tunnel_failed_checks}")
                if self.tunnel_failed_checks >= 2:
                    self.restart_tunnel()
            elif self.tunnel_enabled and self.public_health_check and self.last_public_url:
                self.tunnel_failed_checks = 0

            time.sleep(10)

        self.log("supervisor stopping")
        self.terminate_proc("cloudflared", self.tunnel)
        self.terminate_proc("poller", self.poller)
        self.terminate_proc("ws", self.ws)
        self.terminate_proc("bot", self.bot)
        return 0


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
    os.umask(0o077)
    harden_runtime_metadata()
    load_dotenv(ROOT / ".env")
    return Supervisor().run()


if __name__ == "__main__":
    raise SystemExit(main())
