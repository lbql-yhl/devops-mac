#!/usr/bin/env python3
"""Create and verify one Apple Developer CSR without GUI automation.

The host sends the Notion-derived subject through stdin JSON.  Sensitive
subject values are captured in memory and are never written to argv, stdout,
stderr, logs, or marker files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pty
import pwd
import re
import select
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable


UTM9_GUEST_HELPER_ID = "mac-submission-utm-9-v1"
CSR_FILENAME = "CertificateSigningRequest.certSigningRequest"
KEY_LABEL_RE = re.compile(r"utm-9-[0-9a-f]{16}")
EMAIL_RE = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
RunCommand = Callable[..., subprocess.CompletedProcess[bytes]]
PromptRunner = Callable[[list[str], str, bytes], int]


class GuestError(RuntimeError):
    """Raised when the guest cannot safely create or verify the CSR."""


def _one_line(value: object, field: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str):
        raise GuestError(f"{field.upper()}_INVALID")
    text = value.strip()
    if not text or len(text) > maximum or any(ord(char) < 32 for char in text):
        raise GuestError(f"{field.upper()}_INVALID")
    return text


def validate_payload(payload: object, *, require_subject: bool) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise GuestError("PAYLOAD_INVALID")
    expected_user = _one_line(payload.get("expected_user"), "expected_user", maximum=32)
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", expected_user):
        raise GuestError("EXPECTED_USER_INVALID")
    expected_path = f"/Users/{expected_user}/Desktop/{CSR_FILENAME}"
    csr_path = _one_line(payload.get("csr_path"), "csr_path", maximum=512)
    if csr_path != expected_path:
        raise GuestError("CSR_PATH_INVALID")
    key_label = _one_line(payload.get("key_label"), "key_label", maximum=64)
    if not KEY_LABEL_RE.fullmatch(key_label):
        raise GuestError("KEY_LABEL_INVALID")
    challenge = _one_line(payload.get("challenge"), "challenge", maximum=64)
    if not KEY_LABEL_RE.fullmatch(challenge):
        raise GuestError("CHALLENGE_INVALID")
    keychain_password = _one_line(
        payload.get("keychain_password"), "keychain_password", maximum=64
    )
    if re.fullmatch(r"[0-9]{4}", keychain_password) is None:
        raise GuestError("KEYCHAIN_PASSWORD_INVALID")
    normalized: dict[str, Any] = {
        "expected_user": expected_user,
        "csr_path": csr_path,
        "key_label": key_label,
        "challenge": challenge,
        "keychain_password": keychain_password,
    }
    if require_subject:
        common_name = _one_line(payload.get("common_name"), "common_name")
        email = _one_line(payload.get("email"), "email", maximum=320)
        if not EMAIL_RE.fullmatch(email):
            raise GuestError("EMAIL_INVALID")
        normalized.update({"common_name": common_name, "email": email})
    before = payload.get("desktop_before")
    if before is not None:
        if not isinstance(before, dict):
            raise GuestError("DESKTOP_BEFORE_INVALID")
        count = before.get("desktop_count")
        digest = before.get("desktop_digest")
        if not isinstance(count, int) or count < 0 or not isinstance(digest, str):
            raise GuestError("DESKTOP_BEFORE_INVALID")
        digest = digest.lower()
        if not SHA256_RE.fullmatch(digest):
            raise GuestError("DESKTOP_BEFORE_INVALID")
        normalized["desktop_before"] = {
            "desktop_count": count,
            "desktop_digest": digest,
        }
    return normalized


def build_certtool_input(
    *, key_label: str, challenge: str, common_name: str, email: str
) -> bytes:
    values = (
        key_label,
        "r",
        "2048",
        "y",
        "s",
        "2",
        "y",
        challenge,
        common_name,
        "",
        "",
        "",
        "",
        email,
        "y",
    )
    if any("\n" in value or "\r" in value or "\0" in value for value in values):
        raise GuestError("CERTTOOL_INPUT_INVALID")
    return ("\n".join(values) + "\n").encode("utf-8")


def parse_application_label(output: str) -> str:
    matches = re.findall(r"0x00000006\s+<blob>=0x([0-9A-Fa-f]{40})(?:\s|$)", output)
    if len(matches) != 1:
        raise GuestError(f"KEY_APPLICATION_LABEL_MATCH_COUNT={len(matches)}")
    return matches[0].lower()


def _run(
    command: list[str],
    *,
    runner: RunCommand,
    input_bytes: bytes | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return runner(
            command,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise GuestError(f"COMMAND_TIMEOUT={Path(command[0]).name}") from error


def _identity(expected_user: str, *, runner: RunCommand) -> tuple[Path, Path]:
    account = pwd.getpwuid(os.getuid())
    if account.pw_name != expected_user or account.pw_dir != f"/Users/{expected_user}":
        raise GuestError("GUEST_IDENTITY_MISMATCH")
    console = _run(
        ["/usr/bin/stat", "-f", "%Su", "/dev/console"], runner=runner
    )
    if console.returncode != 0 or console.stdout.decode("utf-8", errors="replace").strip() != expected_user:
        raise GuestError("CONSOLE_USER_MISMATCH")
    home = Path(account.pw_dir)
    desktop = home / "Desktop"
    keychain = home / "Library" / "Keychains" / "login.keychain-db"
    if not desktop.is_dir() or desktop.is_symlink():
        raise GuestError("DESKTOP_INVALID")
    if not keychain.is_file() or keychain.is_symlink():
        raise GuestError("LOGIN_KEYCHAIN_INVALID")
    return desktop, keychain


def desktop_snapshot(desktop: Path) -> dict[str, Any]:
    entries: list[tuple[str, str]] = []
    for entry in os.scandir(desktop):
        if entry.name in {CSR_FILENAME, ".DS_Store"}:
            continue
        if entry.is_symlink():
            kind = "symlink"
        elif entry.is_file(follow_symlinks=False):
            kind = "file"
        elif entry.is_dir(follow_symlinks=False):
            kind = "directory"
        else:
            kind = "other"
        entries.append((entry.name, kind))
    encoded = json.dumps(sorted(entries), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return {
        "desktop_count": len(entries),
        "desktop_digest": hashlib.sha256(encoded).hexdigest(),
    }


def _key_application_label(
    keychain: Path, key_label: str, *, runner: RunCommand
) -> str | None:
    result = _run(
        [
            "/usr/bin/security",
            "find-key",
            "-t",
            "private",
            "-l",
            key_label,
            "-s",
            str(keychain),
        ],
        runner=runner,
    )
    if result.returncode != 0:
        return None
    return parse_application_label(result.stdout.decode("utf-8", errors="replace"))


def _keychain_unlocked(keychain: Path, *, runner: RunCommand) -> bool:
    result = _run(
        ["/usr/bin/security", "show-keychain-info", str(keychain)], runner=runner
    )
    return result.returncode == 0


def _pty_password_prompt(command: list[str], password: str, prompt: bytes) -> int:
    master_fd, slave_fd = pty.openpty()
    process = None
    sent = False
    transcript = bytearray()
    try:
        process = subprocess.Popen(
            command,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
        )
        os.close(slave_fd)
        slave_fd = -1
        deadline = time.monotonic() + 15
        while process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.wait()
                raise GuestError("KEYCHAIN_UNLOCK_TIMEOUT")
            ready, _, _ = select.select([master_fd], [], [], min(remaining, 0.25))
            if not ready:
                continue
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                if process.poll() is not None:
                    break
                raise
            if not chunk:
                break
            transcript.extend(chunk)
            if len(transcript) > 8192:
                del transcript[:-8192]
            if not sent and prompt.lower() in bytes(transcript).lower():
                os.write(master_fd, password.encode("utf-8") + b"\n")
                sent = True
        returncode = process.wait()
        if not sent:
            raise GuestError("KEYCHAIN_UNLOCK_PROMPT_MISSING")
        return returncode
    finally:
        if slave_fd >= 0:
            os.close(slave_fd)
        os.close(master_fd)
        del password


def unlock_keychain(
    keychain: Path,
    password: str,
    *,
    prompt_runner: PromptRunner = _pty_password_prompt,
) -> None:
    command = ["/usr/bin/security", "unlock-keychain", str(keychain)]
    if prompt_runner(command, password, b"password to unlock") != 0:
        raise GuestError("KEYCHAIN_UNLOCK_FAILED")


def _ensure_keychain_unlocked(
    keychain: Path, values: dict[str, Any], *, runner: RunCommand
) -> None:
    if _keychain_unlocked(keychain, runner=runner):
        return
    password = values["keychain_password"]
    unlock_keychain(keychain, password)
    del password
    if not _keychain_unlocked(keychain, runner=runner):
        raise GuestError("LOGIN_KEYCHAIN_LOCKED")


def probe(payload: object, *, runner: RunCommand = subprocess.run) -> dict[str, Any]:
    values = validate_payload(payload, require_subject=False)
    desktop, keychain = _identity(values["expected_user"], runner=runner)
    _ensure_keychain_unlocked(keychain, values, runner=runner)
    csr = Path(values["csr_path"])
    csr_exists = csr.exists() or csr.is_symlink()
    if csr_exists and (csr.is_symlink() or not csr.is_file()):
        raise GuestError("CSR_PATH_CONFLICT")
    return {
        "status": "probed",
        "identity_verified": True,
        "keychain_unlocked": _keychain_unlocked(keychain, runner=runner),
        "csr_exists": csr_exists,
        "key_exists": _key_application_label(keychain, values["key_label"], runner=runner)
        is not None,
        **desktop_snapshot(desktop),
    }


def _parse_subject(output: bytes) -> dict[str, str]:
    fields: dict[str, str] = {}
    for raw_line in output.decode("utf-8", errors="strict").splitlines():
        line = raw_line.strip()
        if not line or line == "subject=" or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in fields:
            raise GuestError(f"CSR_SUBJECT_DUPLICATE={key}")
        fields[key] = value
    return fields


def verify(payload: object, *, runner: RunCommand = subprocess.run) -> dict[str, Any]:
    values = validate_payload(payload, require_subject=True)
    desktop, keychain = _identity(values["expected_user"], runner=runner)
    _ensure_keychain_unlocked(keychain, values, runner=runner)
    csr = Path(values["csr_path"])
    if csr.is_symlink() or not csr.is_file() or csr.stat().st_size <= 0:
        raise GuestError("CSR_FILE_INVALID")
    if csr.stat().st_mode & 0o777 != 0o600:
        raise GuestError("CSR_MODE_INVALID")
    expected_before = values.get("desktop_before")
    current_snapshot = desktop_snapshot(desktop)
    if expected_before is not None and current_snapshot != expected_before:
        raise GuestError("DESKTOP_NON_TARGET_DRIFT")

    certtool_verify = _run(["/usr/bin/certtool", "V", str(csr)], runner=runner)
    if certtool_verify.returncode != 0:
        raise GuestError("CERTTOOL_VERIFY_FAILED")
    openssl_verify = _run(
        ["/usr/bin/openssl", "req", "-in", str(csr), "-noout", "-verify"],
        runner=runner,
    )
    if openssl_verify.returncode != 0:
        raise GuestError("OPENSSL_VERIFY_FAILED")
    subject = _run(
        [
            "/usr/bin/openssl",
            "req",
            "-in",
            str(csr),
            "-noout",
            "-subject",
            "-nameopt",
            "sep_multiline,utf8",
        ],
        runner=runner,
    )
    if subject.returncode != 0:
        raise GuestError("CSR_SUBJECT_READ_FAILED")
    fields = _parse_subject(subject.stdout)
    if fields != {"CN": values["common_name"], "emailAddress": values["email"]}:
        raise GuestError("CSR_SUBJECT_MISMATCH")

    key_hash = _key_application_label(keychain, values["key_label"], runner=runner)
    if key_hash is None:
        raise GuestError("CSR_PRIVATE_KEY_MISSING")
    public_key = _run(
        ["/usr/bin/openssl", "req", "-in", str(csr), "-pubkey", "-noout"],
        runner=runner,
    )
    if public_key.returncode != 0:
        raise GuestError("CSR_PUBLIC_KEY_READ_FAILED")
    pkcs1 = _run(
        [
            "/usr/bin/openssl",
            "rsa",
            "-pubin",
            "-RSAPublicKey_out",
            "-outform",
            "DER",
        ],
        runner=runner,
        input_bytes=public_key.stdout,
    )
    if pkcs1.returncode != 0:
        raise GuestError("CSR_PUBLIC_KEY_CONVERSION_FAILED")
    csr_key_hash = hashlib.sha1(pkcs1.stdout).hexdigest()
    if csr_key_hash != key_hash:
        raise GuestError("CSR_PRIVATE_KEY_MISMATCH")
    content = csr.read_bytes()
    return {
        "status": "verified",
        "csr_bytes": len(content),
        "csr_sha256": hashlib.sha256(content).hexdigest(),
        "key_application_label": key_hash,
        **current_snapshot,
    }


def create(payload: object, *, runner: RunCommand = subprocess.run) -> dict[str, Any]:
    values = validate_payload(payload, require_subject=True)
    desktop, keychain = _identity(values["expected_user"], runner=runner)
    _ensure_keychain_unlocked(keychain, values, runner=runner)
    expected_before = values.get("desktop_before")
    if expected_before is None or desktop_snapshot(desktop) != expected_before:
        raise GuestError("DESKTOP_BEFORE_MISMATCH")
    csr = Path(values["csr_path"])
    if csr.exists() or csr.is_symlink():
        raise GuestError("CSR_ALREADY_EXISTS")
    if _key_application_label(keychain, values["key_label"], runner=runner) is not None:
        raise GuestError("KEY_LABEL_ALREADY_EXISTS")
    certtool_input = build_certtool_input(
        key_label=values["key_label"],
        challenge=values["challenge"],
        common_name=values["common_name"],
        email=values["email"],
    )
    result = _run(
        ["/usr/bin/certtool", "r", str(csr), f"k={keychain}", "a"],
        runner=runner,
        input_bytes=certtool_input,
        timeout=120,
    )
    del certtool_input
    if result.returncode != 0:
        raise GuestError(f"CERTTOOL_EXIT={result.returncode}")
    if csr.is_symlink() or not csr.is_file() or csr.stat().st_size <= 0:
        raise GuestError("CERTTOOL_RESULT_MISSING")
    os.chmod(csr, 0o600)
    return verify(values, runner=runner)


def main() -> int:
    parser = argparse.ArgumentParser(description="UTM-9 command-only guest CSR helper")
    parser.add_argument("--mode", required=True, choices=("probe", "create", "verify"))
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
        if args.mode == "probe":
            result = probe(payload)
        elif args.mode == "create":
            result = create(payload)
        else:
            result = verify(payload)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except (GuestError, OSError, ValueError, json.JSONDecodeError) as error:
        code = str(error) if isinstance(error, GuestError) else type(error).__name__.upper()
        print(f"UTM_9_GUEST=blocked:{code}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
