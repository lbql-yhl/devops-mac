"""Read host-local settings without mutating the process environment."""

from __future__ import annotations

import os
import re
import secrets
import stat
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_PATH = ROOT / ".env"


class ConfigurationError(RuntimeError):
    """Raised when a required host-only setting is unavailable or invalid."""


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _runtime_open_flags(*, directory: bool) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    else:
        flags |= getattr(os, "O_NONBLOCK", 0)
    return flags


def _runtime_stat_at(directory_fd: int, name: str) -> os.stat_result:
    return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)


def secure_runtime_root(path: Path, *, create: bool = False) -> None:
    """Secure one shared directory without traversing its foreign entries."""
    target = Path(path)
    try:
        initial = target.lstat()
    except FileNotFoundError:
        if not create:
            raise ConfigurationError("unsafe runtime directory") from None
        try:
            os.mkdir(target, 0o700)
            initial = target.lstat()
        except OSError as error:
            raise ConfigurationError("unable to create runtime directory safely") from error
    except OSError as error:
        raise ConfigurationError("unable to inspect runtime directory safely") from error
    if not stat.S_ISDIR(initial.st_mode):
        raise ConfigurationError("unsafe runtime directory")

    descriptor = -1
    try:
        descriptor = os.open(target, _runtime_open_flags(directory=True))
        opened = os.fstat(descriptor)
        current = target.lstat()
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or not _same_file(initial, opened)
            or not _same_file(opened, current)
        ):
            raise ConfigurationError("unsafe runtime directory")
        os.fchmod(descriptor, 0o700)
    except ConfigurationError:
        raise
    except OSError as error:
        raise ConfigurationError("unable to secure runtime directory safely") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def secure_runtime_file(path: Path) -> None:
    """Validate and secure one known metadata file without following links."""
    target = Path(path)
    try:
        initial = target.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise ConfigurationError("unable to inspect runtime file safely") from error
    if not stat.S_ISREG(initial.st_mode):
        raise ConfigurationError("unsafe runtime file")

    descriptor = -1
    try:
        descriptor = os.open(target, _runtime_open_flags(directory=False))
        opened = os.fstat(descriptor)
        current = target.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or not _same_file(initial, opened)
            or not _same_file(opened, current)
        ):
            raise ConfigurationError("unsafe runtime file")
        os.fchmod(descriptor, 0o600)
    except ConfigurationError:
        raise
    except OSError as error:
        raise ConfigurationError("unable to secure runtime file safely") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def secure_runtime_tree(path: Path) -> None:
    """Validate an owned metadata tree completely, then apply user-only modes."""
    root = Path(path)
    try:
        initial_root = root.lstat()
    except FileNotFoundError:
        try:
            os.mkdir(root, 0o700)
            initial_root = root.lstat()
        except OSError as error:
            raise ConfigurationError("unable to create runtime tree safely") from error
    except OSError as error:
        raise ConfigurationError("unable to inspect runtime tree safely") from error
    if not stat.S_ISDIR(initial_root.st_mode):
        raise ConfigurationError("unsafe runtime tree")

    records: list[tuple[int, os.stat_result, int, int | None, str | None]] = []
    try:
        root_fd = os.open(root, _runtime_open_flags(directory=True))
        root_opened = os.fstat(root_fd)
        if not stat.S_ISDIR(root_opened.st_mode) or not _same_file(
            initial_root, root_opened
        ):
            os.close(root_fd)
            raise ConfigurationError("unsafe runtime tree")
        records.append((root_fd, root_opened, 0o700, None, None))

        def validate_directory(directory_fd: int) -> None:
            with os.scandir(directory_fd) as entries:
                for entry in entries:
                    entry_stat = _runtime_stat_at(directory_fd, entry.name)
                    if stat.S_ISDIR(entry_stat.st_mode):
                        desired_mode = 0o700
                        is_directory = True
                    elif stat.S_ISREG(entry_stat.st_mode):
                        desired_mode = 0o600
                        is_directory = False
                    else:
                        raise ConfigurationError("unsafe runtime tree")
                    child_fd = os.open(
                        entry.name,
                        _runtime_open_flags(directory=is_directory),
                        dir_fd=directory_fd,
                    )
                    opened_stat = os.fstat(child_fd)
                    if (
                        (is_directory and not stat.S_ISDIR(opened_stat.st_mode))
                        or (not is_directory and not stat.S_ISREG(opened_stat.st_mode))
                        or not _same_file(entry_stat, opened_stat)
                    ):
                        os.close(child_fd)
                        raise ConfigurationError("unsafe runtime tree")
                    records.append(
                        (child_fd, opened_stat, desired_mode, directory_fd, entry.name)
                    )
                    if is_directory:
                        validate_directory(child_fd)

        validate_directory(root_fd)

        current_root = root.lstat()
        if not stat.S_ISDIR(current_root.st_mode) or not _same_file(
            root_opened, current_root
        ):
            raise ConfigurationError("unsafe runtime tree")
        for descriptor, expected, _mode, parent_fd, name in records[1:]:
            assert parent_fd is not None and name is not None
            current = _runtime_stat_at(parent_fd, name)
            opened = os.fstat(descriptor)
            if not _same_file(expected, current) or not _same_file(expected, opened):
                raise ConfigurationError("unsafe runtime tree")

        for descriptor, _expected, desired_mode, _parent_fd, _name in records:
            os.fchmod(descriptor, desired_mode)
    except ConfigurationError:
        raise
    except OSError as error:
        raise ConfigurationError("unable to secure runtime tree safely") from error
    finally:
        for descriptor, _expected, _mode, _parent_fd, _name in reversed(records):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _open_runtime_parent(path: Path) -> tuple[int, Path]:
    parent = Path(path).parent
    secure_runtime_root(parent, create=True)
    try:
        initial = parent.lstat()
        descriptor = os.open(parent, _runtime_open_flags(directory=True))
        opened = os.fstat(descriptor)
        if not _same_file(initial, opened):
            os.close(descriptor)
            raise ConfigurationError("unsafe runtime directory")
        return descriptor, parent
    except ConfigurationError:
        raise
    except OSError as error:
        raise ConfigurationError("unable to open runtime directory safely") from error


def secure_write_text(path: Path, contents: str, *, encoding: str = "utf-8") -> None:
    """Atomically replace a regular metadata file without following symlinks."""
    target = Path(path)
    parent_fd, _parent = _open_runtime_parent(target)
    temporary_name = f".{target.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    temporary_fd = -1
    temporary_exists = False
    try:
        try:
            destination = _runtime_stat_at(parent_fd, target.name)
        except FileNotFoundError:
            destination = None
        if destination is not None and not stat.S_ISREG(destination.st_mode):
            raise ConfigurationError("unsafe runtime file")

        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        temporary_fd = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
        temporary_exists = True
        os.fchmod(temporary_fd, 0o600)
        with os.fdopen(temporary_fd, "w", encoding=encoding) as handle:
            temporary_fd = -1
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())

        try:
            current = _runtime_stat_at(parent_fd, target.name)
        except FileNotFoundError:
            current = None
        if destination is None:
            if current is not None:
                raise ConfigurationError("unsafe runtime file")
        elif current is None or not stat.S_ISREG(current.st_mode) or not _same_file(
            destination, current
        ):
            raise ConfigurationError("unsafe runtime file")
        os.replace(
            temporary_name,
            target.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        temporary_exists = False
    except ConfigurationError:
        raise
    except (OSError, UnicodeError) as error:
        raise ConfigurationError("unable to write runtime file safely") from error
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if temporary_exists:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except OSError:
                pass
        os.close(parent_fd)


def secure_append_text(path: Path, contents: str, *, encoding: str = "utf-8") -> None:
    """Append to a regular metadata file without following symlinks."""
    target = Path(path)
    parent_fd, _parent = _open_runtime_parent(target)
    descriptor = -1
    try:
        try:
            initial = _runtime_stat_at(parent_fd, target.name)
        except FileNotFoundError:
            initial = None
        if initial is not None and not stat.S_ISREG(initial.st_mode):
            raise ConfigurationError("unsafe runtime file")
        flags = (
            os.O_WRONLY
            | os.O_APPEND
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(target.name, flags, 0o600, dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            initial is not None and not _same_file(initial, opened)
        ):
            raise ConfigurationError("unsafe runtime file")
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, contents.encode(encoding))
    except ConfigurationError:
        raise
    except (OSError, UnicodeError) as error:
        raise ConfigurationError("unable to append runtime file safely") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def secure_create_file(path: Path) -> None:
    """Create one empty user-only marker without following an existing entry."""
    target = Path(path)
    parent_fd, _parent = _open_runtime_parent(target)
    descriptor = -1
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(target.name, flags, 0o600, dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ConfigurationError("unsafe runtime file")
        os.fchmod(descriptor, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def _read_dotenv_snapshot(env_path: Path | None) -> dict[str, str]:
    if env_path is None:
        return {}
    path = Path(env_path)
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise ConfigurationError("unable to inspect host settings file") from error
    if not stat.S_ISREG(path_stat.st_mode):
        raise ConfigurationError("unsafe host settings file")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ConfigurationError("unable to open host settings file safely") from error
    try:
        opened_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or not _same_file(opened_stat, path_stat)
        ):
            raise ConfigurationError("host settings file changed during secure open")
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = -1
            contents = handle.read()
            read_stat = os.fstat(handle.fileno())
        final_path_stat = path.lstat()
        if (
            not stat.S_ISREG(read_stat.st_mode)
            or not stat.S_ISREG(final_path_stat.st_mode)
            or not _same_file(opened_stat, read_stat)
            or not _same_file(read_stat, final_path_stat)
            or opened_stat.st_size != read_stat.st_size
            or opened_stat.st_mtime_ns != read_stat.st_mtime_ns
        ):
            raise ConfigurationError("host settings file changed during secure read")
    except ConfigurationError:
        raise
    except (OSError, UnicodeError) as error:
        raise ConfigurationError("unable to read host settings file safely") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    values: dict[str, str] = {}
    for raw_line in contents.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value.strip()
    return values


def host_settings_snapshot(
    *,
    environ: Mapping[str, str] | None = None,
    env_path: Path | None = DEFAULT_ENV_PATH,
    file_override_keys: frozenset[str] = frozenset(),
) -> Mapping[str, str]:
    """Return one immutable, securely-read view of host configuration."""
    values = _read_dotenv_snapshot(env_path)
    process_values = os.environ if environ is None else environ
    for key, value in process_values.items():
        if key in file_override_keys and key in values:
            continue
        values[str(key)] = str(value).strip()
    return MappingProxyType(values)


def _dotenv_setting(name: str, env_path: Path) -> str:
    return str(
        host_settings_snapshot(environ={}, env_path=env_path).get(name, "")
    ).strip()


def optional_setting(
    name: str,
    *,
    environ: Mapping[str, str] | None = None,
    env_path: Path = DEFAULT_ENV_PATH,
) -> str:
    """Return a stripped host setting without changing ``os.environ``."""
    if environ is not None:
        return str(environ.get(name, "")).strip()
    if name in os.environ:
        return str(os.environ.get(name, "")).strip()
    return _dotenv_setting(name, env_path)


def required_setting(
    name: str,
    *,
    environ: Mapping[str, str] | None = None,
    env_path: Path = DEFAULT_ENV_PATH,
) -> str:
    """Return a required setting or raise without revealing its value."""
    value = optional_setting(name, environ=environ, env_path=env_path)
    if not value:
        raise ConfigurationError(f"missing required host setting: {name}")
    return value


def guest_password(
    *,
    environ: Mapping[str, str] | None = None,
    env_path: Path = DEFAULT_ENV_PATH,
) -> str:
    """Return the host-configured macOS guest password at call time."""
    value = required_setting(
        "SUBMISSION_GUEST_PASSWORD", environ=environ, env_path=env_path
    )
    if re.fullmatch(r"[0-9]{4}", value) is None:
        raise ConfigurationError(
            "invalid host setting: SUBMISSION_GUEST_PASSWORD must be exactly "
            "four ASCII digits"
        )
    return value
