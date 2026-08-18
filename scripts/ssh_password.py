#!/usr/bin/env python3
"""Shared configured-password SSH/SCP command construction.

OpenSSH obtains the guest password from a one-use Unix-socket broker through a
mode-700 SSH_ASKPASS helper.  The child receives only a socket path and random
capability, leaving stdin available for remote scripts and sudo input without
placing the password in its environment.  Public-key authentication is
explicitly disabled for every connection.
"""

from __future__ import annotations

import os
import secrets
import socket
import stat
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Mapping

from services.host_config import guest_password


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_DIR = PROJECT_ROOT / "runtime" / "ssh-password"
_ASKPASS_SCRIPT = (
    "#!/usr/bin/env python3\n"
    "import os\n"
    "import socket\n"
    "import sys\n"
    "path = os.environ.pop('SUBMISSION_SSH_ASKPASS_SOCKET', '')\n"
    "capability = os.environ.pop('SUBMISSION_SSH_ASKPASS_CAPABILITY', '')\n"
    "if not path or not capability:\n"
    "    raise SystemExit(1)\n"
    "response = bytearray()\n"
    "with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:\n"
    "    client.settimeout(10)\n"
    "    client.connect(path)\n"
    "    client.sendall(capability.encode('ascii') + b'\\n')\n"
    "    while len(response) <= 64:\n"
    "        chunk = client.recv(64 - len(response))\n"
    "        if not chunk:\n"
    "            break\n"
    "        response.extend(chunk)\n"
    "if not response or len(response) > 64:\n"
    "    raise SystemExit(1)\n"
    "os.write(sys.stdout.fileno(), bytes(response) + b'\\n')\n"
)
_BROKER_TIMEOUT_SECONDS = 30.0
_SOCKET_ENV = "SUBMISSION_SSH_ASKPASS_SOCKET"
_CAPABILITY_ENV = "SUBMISSION_SSH_ASKPASS_CAPABILITY"
_UNIX_SOCKET_PATH_LIMIT = 100
_CWD_LOCK = threading.Lock()


class SSHPasswordError(RuntimeError):
    """Raised when the local password-authentication channel is unsafe."""


class _SecureRuntimeDirectory:
    """An open, identity-checked runtime directory used for relative I/O."""

    def __init__(self, path: Path, descriptor: int, identity: tuple[int, int]) -> None:
        self.path = path
        self.descriptor = descriptor
        self.identity = identity

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1


def _secure_runtime_dir(path: Path) -> _SecureRuntimeDirectory:
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            path_stat = path.lstat()
        except OSError as error:
            raise SSHPasswordError("SSH password runtime directory is unsafe") from error
    except OSError as error:
        raise SSHPasswordError("SSH password runtime directory is unsafe") from error
    if not stat.S_ISDIR(path_stat.st_mode):
        raise SSHPasswordError("SSH password runtime directory is unsafe")

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        opened_stat = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened_stat.st_mode)
            or opened_stat.st_dev != path_stat.st_dev
            or opened_stat.st_ino != path_stat.st_ino
        ):
            raise SSHPasswordError("SSH password runtime directory changed")
        os.fchmod(descriptor, 0o700)
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise SSHPasswordError("SSH password runtime directory is unsafe") from error
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    return _SecureRuntimeDirectory(
        path,
        descriptor,
        (opened_stat.st_dev, opened_stat.st_ino),
    )


def _assert_runtime_identity(directory: _SecureRuntimeDirectory) -> None:
    if directory.descriptor < 0:
        raise SSHPasswordError("SSH password runtime directory is closed")
    try:
        path_stat = directory.path.lstat()
        opened_stat = os.fstat(directory.descriptor)
    except OSError as error:
        raise SSHPasswordError("SSH password runtime directory changed") from error
    if (
        not stat.S_ISDIR(path_stat.st_mode)
        or not stat.S_ISDIR(opened_stat.st_mode)
        or (path_stat.st_dev, path_stat.st_ino) != directory.identity
        or (opened_stat.st_dev, opened_stat.st_ino) != directory.identity
    ):
        raise SSHPasswordError("SSH password runtime directory changed")


def _askpass_helper(directory: _SecureRuntimeDirectory) -> Path:
    _assert_runtime_identity(directory)
    helper_name = "askpass"
    existing_source: str | None = None
    try:
        helper_stat = os.stat(
            helper_name,
            dir_fd=directory.descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        helper_stat = None
    except OSError as error:
        raise SSHPasswordError("SSH_ASKPASS helper is unsafe") from error
    if helper_stat is not None:
        if not stat.S_ISREG(helper_stat.st_mode):
            raise SSHPasswordError("SSH_ASKPASS helper is unsafe")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(helper_name, flags, dir_fd=directory.descriptor)
            try:
                opened_stat = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(opened_stat.st_mode)
                    or opened_stat.st_dev != helper_stat.st_dev
                    or opened_stat.st_ino != helper_stat.st_ino
                ):
                    raise SSHPasswordError("SSH_ASKPASS helper changed")
                with os.fdopen(descriptor, encoding="utf-8") as handle:
                    descriptor = -1
                    existing_source = handle.read()
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        except (OSError, UnicodeError) as error:
            raise SSHPasswordError("SSH_ASKPASS helper is unsafe") from error
    if existing_source != _ASKPASS_SCRIPT:
        temporary_name = f".askpass-{uuid.uuid4().hex}.tmp"
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=directory.descriptor,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(_ASKPASS_SCRIPT)
                handle.flush()
                os.fsync(handle.fileno())
                os.fchmod(handle.fileno(), 0o700)
            os.replace(
                temporary_name,
                helper_name,
                src_dir_fd=directory.descriptor,
                dst_dir_fd=directory.descriptor,
            )
        finally:
            try:
                os.unlink(temporary_name, dir_fd=directory.descriptor)
            except FileNotFoundError:
                pass
    else:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(helper_name, flags, dir_fd=directory.descriptor)
        try:
            os.fchmod(descriptor, 0o700)
        finally:
            os.close(descriptor)
    _assert_runtime_identity(directory)
    return directory.path / helper_name


def _socket_directory(
    runtime_dir: _SecureRuntimeDirectory,
) -> _SecureRuntimeDirectory:
    _assert_runtime_identity(runtime_dir)
    candidate = runtime_dir.path / f".b-{secrets.token_hex(8)}"
    if len(os.fsencode(candidate)) < _UNIX_SOCKET_PATH_LIMIT:
        return runtime_dir
    short_root = Path(tempfile.gettempdir()) / f"submission-ssh-{os.getuid()}"
    return _secure_runtime_dir(short_root)


def _unlink_owned_socket(
    directory: _SecureRuntimeDirectory,
    name: str,
    identity: tuple[int, int],
) -> None:
    try:
        path_stat = os.stat(
            name,
            dir_fd=directory.descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    if (
        stat.S_ISSOCK(path_stat.st_mode)
        and (path_stat.st_dev, path_stat.st_ino) == identity
    ):
        os.unlink(name, dir_fd=directory.descriptor)


def _bind_socket_at(
    server: socket.socket,
    directory: _SecureRuntimeDirectory,
    socket_name: str,
) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    with _CWD_LOCK:
        previous_directory = os.open(".", flags)
        try:
            os.fchdir(directory.descriptor)
            server.bind(socket_name)
        finally:
            os.fchdir(previous_directory)
            os.close(previous_directory)


def _start_password_broker(
    directory: _SecureRuntimeDirectory,
    configured_password: str,
) -> tuple[Path, str]:
    _assert_runtime_identity(directory)
    capability = secrets.token_urlsafe(32)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    socket_name = f".b-{secrets.token_hex(8)}"
    socket_path = directory.path / socket_name
    identity: tuple[int, int] | None = None
    try:
        _bind_socket_at(server, directory, socket_name)
        socket_stat = os.stat(
            socket_name,
            dir_fd=directory.descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISSOCK(socket_stat.st_mode):
            raise SSHPasswordError("SSH password broker socket is unsafe")
        identity = (socket_stat.st_dev, socket_stat.st_ino)
        _assert_runtime_identity(directory)
        server.listen(1)
        server.settimeout(_BROKER_TIMEOUT_SECONDS)
    except Exception:
        server.close()
        if identity is not None:
            _unlink_owned_socket(directory, socket_name, identity)
        directory.close()
        raise

    def serve_once() -> None:
        try:
            connection, _ = server.accept()
            with connection:
                connection.settimeout(_BROKER_TIMEOUT_SECONDS)
                request = bytearray()
                while len(request) <= 256 and b"\n" not in request:
                    chunk = connection.recv(257 - len(request))
                    if not chunk:
                        break
                    request.extend(chunk)
                supplied = bytes(request).partition(b"\n")[0]
                expected = capability.encode("ascii")
                if len(request) <= 256 and secrets.compare_digest(supplied, expected):
                    connection.sendall(configured_password.encode("utf-8"))
        except (OSError, TimeoutError):
            pass
        finally:
            server.close()
            try:
                _unlink_owned_socket(directory, socket_name, identity)
            finally:
                directory.close()

    thread = threading.Thread(
        target=serve_once,
        name="ssh-password-broker",
        daemon=True,
    )
    try:
        thread.start()
    except Exception:
        server.close()
        _unlink_owned_socket(directory, socket_name, identity)
        directory.close()
        raise
    return socket_path, capability


def password_environment(
    base: Mapping[str, str] | None = None, *, runtime_dir: Path = DEFAULT_RUNTIME_DIR
) -> dict[str, str]:
    """Return an environment that forces one configured-password prompt."""
    env = dict(os.environ if base is None else base)
    configured_password = guest_password(environ=base) if base is not None else guest_password()
    runtime_directory = _secure_runtime_dir(runtime_dir)
    try:
        helper = _askpass_helper(runtime_directory)
        broker_directory = _socket_directory(runtime_directory)
        if broker_directory is runtime_directory:
            runtime_directory = None
        else:
            runtime_directory.close()
            runtime_directory = None
        socket_path, capability = _start_password_broker(
            broker_directory,
            configured_password,
        )
    finally:
        if runtime_directory is not None:
            runtime_directory.close()
    env.pop("SUBMISSION_GUEST_PASSWORD", None)
    env.update(
        {
            "SSH_ASKPASS": str(helper),
            "SSH_ASKPASS_REQUIRE": "force",
            "DISPLAY": env.get("DISPLAY") or "submission-ssh:0",
            _SOCKET_ENV: str(socket_path),
            _CAPABILITY_ENV: capability,
        }
    )
    return env


def connection_options(connect_timeout: int = 5) -> list[str]:
    if connect_timeout <= 0:
        raise ValueError("connect timeout must be positive")
    return [
        "-o", "BatchMode=no",
        "-o", "PreferredAuthentications=password,keyboard-interactive",
        "-o", "PubkeyAuthentication=no",
        "-o", "KbdInteractiveAuthentication=yes",
        "-o", "NumberOfPasswordPrompts=1",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ConnectTimeout={connect_timeout}",
    ]


def ssh_args(user: str, ip: str, *, connect_timeout: int = 5, tty: bool = False) -> list[str]:
    if not user or not ip:
        raise ValueError("SSH user and IP are required")
    command = ["/usr/bin/ssh"]
    if tty:
        command.append("-tt")
    command.extend(["-q", *connection_options(connect_timeout), f"{user}@{ip}"])
    return command


def scp_args(
    user: str,
    ip: str,
    source: Path | str,
    target: str,
    *,
    connect_timeout: int = 8,
) -> list[str]:
    if not target:
        raise ValueError("SCP target is required")
    return [
        "/usr/bin/scp", "-q", *connection_options(connect_timeout),
        str(source), f"{user}@{ip}:{target}",
    ]
