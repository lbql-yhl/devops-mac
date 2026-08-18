#!/usr/bin/env python3

from pathlib import Path
import re
import subprocess
import tempfile


REQUIRED_SENSITIVE_EXAMPLE_KEYS = (
    "SUBMISSION_GUEST_PASSWORD",
    "FEISHU_DAILY_REPORT_CHAT_ID",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_VERIFICATION_TOKEN",
    "FEISHU_ENCRYPT_KEY",
    "NOTION_TOKEN",
    "NOTION_ROOT_PAGE_ID",
    "UTM_NOTION_FEISHU_WIKI_URL",
    "UTM_NOTION_FEISHU_APP_TOKEN",
    "UTM_NOTION_FEISHU_TABLE_ID",
    "UTM_NOTION_FEISHU_VIEW_ID",
    "CODEUP_USERNAME",
    "CODEUP_PASSWORD",
    "OPENAI_API_KEY",
    "SUBMISSION_PROJECT_ROOT",
    "SUBMISSION_VM_IMAGES_DIR",
    "SUBMISSION_VM_TEMPLATE",
    "SUBMISSION_SHARED_DIR",
    "PROJECT_SKILLS_DIR",
    "SUBMISSION_HOST_MACHINE",
)

_REGULAR_FILE_MODES = {b"100644", b"100755"}
_SENSITIVE_KEY_SUFFIXES = (
    "PASSWORD",
    "TOKEN",
    "SECRET",
    "APP_ID",
    "CHAT_ID",
    "TABLE_ID",
    "VIEW_ID",
    "PAGE_ID",
    "RESOURCE_ID",
    "RECEIVE_ID",
)
_REPORT_DESTINATION_SUFFIXES = (
    "DAILY_REPORT_CHAT_ID",
    "REPORT_CHAT_ID",
    "REPORT_DESTINATION",
    "REPORT_RECEIVE_ID",
)
_QUOTED_ASSIGNMENT = re.compile(
    r"(?m)^[ \t]*(?:(?:export|const|let|var)\s+)?"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?:\s*:\s*[^=\r\n]+)?\s*=\s*"
    r"(?P<prefix>[bBrRuUfF]{0,2})"
    r"(?P<quote>['\"`])(?P<value>[^\r\n]*?)(?P=quote)"
)
_POSIX_HOME = re.compile(
    r"/(?:Users|home)/(?P<owner>[^/\s'\"<>${}]+)(?:/|$)"
)
_ROOT_HOME = re.compile(r"(?<![A-Za-z0-9_])/root(?:/|$)")
_WINDOWS_HOME = re.compile(
    r"[A-Za-z]:(?:\\+|/)Users(?:\\+|/)"
    r"(?P<owner>[^\\/\s'\"<>%]+)(?:(?:\\+|/)|$)",
    re.IGNORECASE,
)
_NON_PERSONAL_HOME_NAMES = {
    "example",
    "shared",
    "user",
    "username",
    "yourname",
}
_IGNORE_PROBES = (
    ".env",
    "config/workflow.env",
    "nested/service.env",
    "runtime/public-release-audit.json",
    "signing/public-release-audit.p8",
    "signing/public-release-audit.p12",
    "signing/public-release-audit.pem",
    "signing/public-release-audit.key",
    "signing/public-release-audit.cer",
    "signing/public-release-audit.mobileprovision",
    "vm/public-release-audit.utm/config.plist",
    "vm/public-release-audit.vmwarevm/config.vmx",
    "vm/public-release-audit.qcow2",
    "vm/public-release-audit.vmdk",
    "vm/public-release-audit.vdi",
    "vm/public-release-audit.vhd",
    "vm/public-release-audit.vhdx",
    "vm/public-release-audit.raw",
    "vm/public-release-audit.iso",
    "vm/public-release-audit.ova",
    "vm/public-release-audit.ovf",
    "vm/public-release-audit.vmsn",
)
_ALLOWED_EXAMPLE_PROBES = (
    ".env.example",
    "config/workflow.env.example",
    "nested/service.env.example",
)


def _tracked_regular_blobs(root: Path) -> dict[str, str]:
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--stage"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    blobs: dict[str, str] = {}
    for record in completed.stdout.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.split()
        if (
            not separator
            or len(fields) != 3
            or fields[0] not in _REGULAR_FILE_MODES
            or fields[2] != b"0"
        ):
            continue
        relative_path = raw_path.decode("utf-8", errors="surrogateescape")
        blobs[relative_path] = fields[1].decode("ascii")
    return dict(sorted(blobs.items()))


def _read_index_blob(root: Path, object_id: str) -> bytes:
    completed = subprocess.run(
        ["git", "cat-file", "blob", object_id],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout


def _is_personal_home_path(text: str) -> bool:
    if _ROOT_HOME.search(text):
        return True
    for pattern in (_POSIX_HOME, _WINDOWS_HOME):
        for match in pattern.finditer(text):
            if match.group("owner").casefold() not in _NON_PERSONAL_HOME_NAMES:
                return True
    return False


def _is_sensitive_key(key: str) -> bool:
    return key.upper().endswith(_SENSITIVE_KEY_SUFFIXES)


def _env_assignments(text: str) -> tuple[dict[str, list[str]], set[str]]:
    assignments: dict[str, list[str]] = {}
    duplicate_keys: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        normalized_value = value.strip()
        if normalized_value in {"''", '\"\"'}:
            normalized_value = ""
        if key in assignments:
            duplicate_keys.add(key)
        assignments.setdefault(key, []).append(normalized_value)
    return assignments, duplicate_keys


def _is_dotenv_path(relative_path: str) -> bool:
    name = relative_path.rsplit("/", 1)[-1]
    return name == ".env" or ".env." in name or name.endswith(".env")


def _ignored_paths_with_git(
    ignore_content: bytes, relative_paths: tuple[str, ...]
) -> set[str]:
    if b"\0" in ignore_content:
        ignore_content = b""
    with tempfile.TemporaryDirectory(prefix="public-release-audit-") as temp_dir:
        temp_root = Path(temp_dir)
        subprocess.run(
            ["git", "init", "-q"],
            cwd=temp_root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        (temp_root / ".gitignore").write_bytes(ignore_content)
        subprocess.run(
            ["git", "add", "-f", "--", ".gitignore"],
            cwd=temp_root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        completed = subprocess.run(
            [
                "git",
                "-c",
                "core.excludesFile=/dev/null",
                "check-ignore",
                "--no-index",
                "-z",
                "--stdin",
            ],
            cwd=temp_root,
            check=False,
            input=b"\0".join(path.encode() for path in relative_paths) + b"\0",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if completed.returncode not in {0, 1}:
            raise subprocess.CalledProcessError(
                completed.returncode,
                completed.args,
                output=completed.stdout,
                stderr=completed.stderr,
            )
    return {
        path.decode("utf-8", errors="surrogateescape")
        for path in completed.stdout.split(b"\0")
        if path
    }


def audit_repository(root: Path) -> list[tuple[str, str]]:
    """Return sorted ``(rule, relative_path)`` public-release findings."""
    root = root.resolve()
    tracked_blobs = _tracked_regular_blobs(root)
    blob_cache: dict[str, bytes] = {}
    findings: set[tuple[str, str]] = set()

    def blob_content(object_id: str) -> bytes:
        if object_id not in blob_cache:
            blob_cache[object_id] = _read_index_blob(root, object_id)
        return blob_cache[object_id]

    for relative_path, object_id in tracked_blobs.items():
        if relative_path.startswith("docs/superpowers/"):
            findings.add(("HISTORICAL_PLAN", relative_path))
        if relative_path == ".env":
            continue

        content = blob_content(object_id)
        if b"\0" in content:
            continue
        text = content.decode("utf-8", errors="replace")

        if _is_personal_home_path(text):
            findings.add(("PERSONAL_HOME_PATH", relative_path))

        is_example = relative_path == ".env.example" or relative_path.endswith(
            ".env.example"
        )
        if is_example:
            assignments, duplicate_keys = _env_assignments(text)
            if duplicate_keys:
                findings.add(("EXAMPLE_DUPLICATE_KEY", relative_path))
            if any(
                (key in REQUIRED_SENSITIVE_EXAMPLE_KEYS or _is_sensitive_key(key))
                and any(bool(value) for value in values)
                for key, values in assignments.items()
            ):
                findings.add(("EXAMPLE_SENSITIVE_VALUE", relative_path))
            if relative_path == ".env.example" and any(
                key not in assignments for key in REQUIRED_SENSITIVE_EXAMPLE_KEYS
            ):
                findings.add(("EXAMPLE_SENSITIVE_KEY_MISSING", relative_path))
            continue

        if _is_dotenv_path(relative_path):
            assignments, _ = _env_assignments(text)
            for key, values in assignments.items():
                upper_key = key.upper()
                if not any(bool(value) for value in values):
                    continue
                if upper_key.endswith(_REPORT_DESTINATION_SUFFIXES):
                    findings.add(("FIXED_REPORT_DESTINATION", relative_path))
                if _is_sensitive_key(upper_key):
                    findings.add(("SENSITIVE_ASSIGNMENT", relative_path))

        for match in _QUOTED_ASSIGNMENT.finditer(text):
            key = match.group("key").upper()
            if not match.group("value"):
                continue
            if key.endswith(_REPORT_DESTINATION_SUFFIXES):
                findings.add(("FIXED_REPORT_DESTINATION", relative_path))
            if _is_sensitive_key(key):
                findings.add(("SENSITIVE_ASSIGNMENT", relative_path))

    ignore_content = b""
    ignore_object_id = tracked_blobs.get(".gitignore")
    if ignore_object_id is not None:
        ignore_content = blob_content(ignore_object_id)
    probe_paths = _IGNORE_PROBES + _ALLOWED_EXAMPLE_PROBES
    ignored_paths = _ignored_paths_with_git(
        ignore_content,
        probe_paths,
    )
    for relative_path in _IGNORE_PROBES:
        if relative_path not in ignored_paths:
            findings.add(("IGNORE_RULE_MISSING", relative_path))
    for relative_path in _ALLOWED_EXAMPLE_PROBES:
        if relative_path in ignored_paths:
            findings.add(("EXAMPLE_FILE_IGNORED", relative_path))
    if ".env.example" not in tracked_blobs:
        findings.add(("EXAMPLE_FILE_NOT_TRACKED", ".env.example"))

    return sorted(findings)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    try:
        findings = audit_repository(root)
    except (OSError, subprocess.SubprocessError):
        print("AUDIT_ERROR .")
        return 2

    if not findings:
        print("PUBLIC_RELEASE_AUDIT=ok")
        return 0
    for rule, relative_path in findings:
        print(f"{rule} {relative_path}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
