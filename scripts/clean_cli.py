#!/usr/bin/env python3
"""Keep user-facing skill output to one start and one terminal result line."""

from __future__ import annotations

import contextlib
import os
import re
import sys
import tempfile
from collections.abc import Callable


PROGRESS_RE = re.compile(
    r"步骤(?:[1-9]|10)(?:已操作|已完成，跳过)"
)
_progress_fd: int | None = None


def emit_progress(line: str) -> None:
    """Emit one approved progress line through the quiet CLI wrapper."""
    if not PROGRESS_RE.fullmatch(line):
        return
    if _progress_fd is None:
        print(line, flush=True)
        return
    os.write(_progress_fd, f"{line}\n".encode())


def safe_reason(error: BaseException) -> str:
    detail = re.sub(r"[\r\n]+", " ", str(error)).strip()
    if re.fullmatch(r"[A-Z][A-Z0-9_:=.,;|/ -]{0,319}", detail):
        return detail
    return type(error).__name__


def full_reason(error: BaseException) -> str:
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        detail = re.sub(r"[\r\n]+", " ", str(current)).strip()
        if not detail:
            detail = type(current).__name__
        if parts:
            parts.append(f"{type(current).__name__}: {detail}")
        else:
            parts.append(detail)
        if current.__cause__ is not None:
            current = current.__cause__
        elif current.__context__ is not None and not current.__suppress_context__:
            current = current.__context__
        else:
            current = None
    return " | caused by ".join(parts)


def run_clean_cli(
    *,
    skill_name: str,
    success_marker: str,
    operation: Callable[[], int | None],
    progress_pattern: str | None = None,
    preserve_error_detail: bool = False,
) -> int:
    global _progress_fd
    print(f"开始执行：{skill_name}", flush=True)
    captured_reason = ""
    previous_progress_fd = _progress_fd
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        stdout_fd = os.dup(1)
        stderr_fd = os.dup(2)
        _progress_fd = stdout_fd
        with tempfile.TemporaryFile(mode="w+") as sink:
            try:
                os.dup2(sink.fileno(), 1)
                os.dup2(sink.fileno(), 2)
                with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                    result = operation()
            finally:
                sys.stdout.flush()
                sys.stderr.flush()
                sink.seek(0)
                captured = sink.read()
                os.dup2(stdout_fd, 1)
                os.dup2(stderr_fd, 2)
                os.close(stdout_fd)
                os.close(stderr_fd)
        if progress_pattern:
            for line in captured.splitlines():
                if re.fullmatch(progress_pattern, line.strip()):
                    print(line.strip(), flush=True)
        if preserve_error_detail:
            full_codes = re.findall(
                r"(?m)^(?:[A-Z][A-Z0-9_]*=(?:blocked|failed):\s*|"
                r"执行报错：[^；]+；)([^\r\n]+)$",
                captured,
            )
            if full_codes:
                captured_reason = full_codes[-1].strip()
        else:
            safe_codes = re.findall(
                r"(?m)^(?:[A-Z][A-Z0-9_]*=(?:blocked|failed):\s*|执行报错：[^；]+；)"
                r"([A-Z][A-Z0-9_:=.,;|/ -]{0,319})$",
                captured,
            )
            if len(safe_codes) == 1:
                captured_reason = safe_codes[0]
        status = int(result or 0)
        if status != 0:
            raise RuntimeError(captured_reason or f"EXIT_{status}")
    except BaseException as error:
        print(
            f"执行报错：{skill_name}；"
            f"{full_reason(error) if preserve_error_detail else safe_reason(error)}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    finally:
        _progress_fd = previous_progress_fd
    print(f"执行成功：{skill_name}；{success_marker}", flush=True)
    return 0
