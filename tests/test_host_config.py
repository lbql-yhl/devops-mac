from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import socket
import stat

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "services" / "host_config.py"
TEST_SETTING = "HOST_CONFIG_TEST_SETTING"


def load_module():
    assert MODULE.is_file(), "host configuration accessors are missing"
    spec = importlib.util.spec_from_file_location("host_config_under_test", MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_process_environment_precedes_dotenv_without_mutating_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_module()
    env_path = tmp_path / ".env"
    env_path.write_text(f"{TEST_SETTING}=from-file\n", encoding="utf-8")
    monkeypatch.setenv(TEST_SETTING, "  from-process  ")
    before = dict(os.environ)

    assert module.optional_setting(TEST_SETTING, env_path=env_path) == "from-process"
    assert dict(os.environ) == before


def test_explicit_environment_is_the_only_configuration_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_module()
    env_path = tmp_path / ".env"
    env_path.write_text(f"{TEST_SETTING}=from-file\n", encoding="utf-8")
    monkeypatch.setenv(TEST_SETTING, "from-process")
    supplied = {TEST_SETTING: "  from-mapping  "}

    assert (
        module.optional_setting(TEST_SETTING, environ=supplied, env_path=env_path)
        == "from-mapping"
    )
    assert module.optional_setting(TEST_SETTING, environ={}, env_path=env_path) == ""
    assert supplied == {TEST_SETTING: "  from-mapping  "}


def test_dotenv_is_a_read_only_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_module()
    env_path = tmp_path / ".env"
    env_path.write_text(
        f"# host-only settings\n{TEST_SETTING}=  from-file  \n",
        encoding="utf-8",
    )
    monkeypatch.delenv(TEST_SETTING, raising=False)
    before = dict(os.environ)

    assert module.optional_setting(TEST_SETTING, env_path=env_path) == "from-file"
    assert dict(os.environ) == before


def test_dotenv_symlink_is_rejected_without_reading_or_modifying_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_module()
    synthetic_value = "9072"
    external = tmp_path / "external.env"
    external.write_text(f"{TEST_SETTING}={synthetic_value}\n", encoding="utf-8")
    external.chmod(0o640)
    env_path = tmp_path / ".env"
    env_path.symlink_to(external)
    monkeypatch.delenv(TEST_SETTING, raising=False)
    before_mode = stat.S_IMODE(external.stat().st_mode)

    with pytest.raises(module.ConfigurationError) as captured:
        module.optional_setting(TEST_SETTING, env_path=env_path)

    assert synthetic_value not in str(captured.value)
    assert stat.S_IMODE(external.stat().st_mode) == before_mode
    assert external.read_text(encoding="utf-8") == f"{TEST_SETTING}={synthetic_value}\n"


def test_required_setting_fails_with_only_the_setting_name() -> None:
    module = load_module()
    with pytest.raises(
        module.ConfigurationError,
        match=f"^missing required host setting: {TEST_SETTING}$",
    ) as captured:
        module.required_setting(TEST_SETTING, environ={TEST_SETTING: "  "})

    assert "secret" not in str(captured.value).casefold()


def test_guest_password_reads_the_required_host_setting() -> None:
    module = load_module()
    synthetic_value = "9072"

    assert (
        module.guest_password(
            environ={"SUBMISSION_GUEST_PASSWORD": f"  {synthetic_value}  "}
        )
        == synthetic_value
    )


@pytest.mark.parametrize("invalid", ("12", "12345", "12a4", "１２３４"))
def test_guest_password_rejects_non_four_ascii_digits_without_echo(
    invalid: str,
) -> None:
    module = load_module()

    with pytest.raises(module.ConfigurationError) as captured:
        module.guest_password(environ={"SUBMISSION_GUEST_PASSWORD": invalid})

    assert invalid not in str(captured.value)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_secure_runtime_tree_preserves_tree_and_fixes_modes(tmp_path: Path) -> None:
    module = load_module()
    runtime = tmp_path / "owned"
    nested = runtime / "nested"
    nested.mkdir(parents=True, mode=0o777)
    first = runtime / "first.json"
    second = nested / "second.log"
    first.write_text("first-payload", encoding="utf-8")
    second.write_text("second-payload", encoding="utf-8")
    runtime.chmod(0o755)
    nested.chmod(0o777)
    first.chmod(0o666)
    second.chmod(0o644)
    entries_before = sorted(path.relative_to(runtime) for path in runtime.rglob("*"))

    module.secure_runtime_tree(runtime)

    assert sorted(path.relative_to(runtime) for path in runtime.rglob("*")) == entries_before
    assert first.read_text(encoding="utf-8") == "first-payload"
    assert second.read_text(encoding="utf-8") == "second-payload"
    assert _mode(runtime) == _mode(nested) == 0o700
    assert _mode(first) == _mode(second) == 0o600


def test_secure_runtime_tree_creates_missing_root_with_user_only_mode(
    tmp_path: Path,
) -> None:
    module = load_module()
    runtime = tmp_path / "owned"

    module.secure_runtime_tree(runtime)

    assert runtime.is_dir()
    assert _mode(runtime) == 0o700


@pytest.mark.parametrize("target_kind", ("directory", "file", "broken"))
def test_secure_runtime_tree_rejects_symlinks_without_partial_chmod(
    tmp_path: Path,
    target_kind: str,
) -> None:
    module = load_module()
    runtime = tmp_path / "owned"
    runtime.mkdir(mode=0o755)
    prior = runtime / "prior.json"
    prior.write_text("keep", encoding="utf-8")
    prior.chmod(0o666)
    external_dir = tmp_path / "external"
    external_dir.mkdir(mode=0o755)
    external_file = external_dir / "payload.txt"
    external_file.write_text("outside", encoding="utf-8")
    external_file.chmod(0o644)
    external_dir.chmod(0o755)
    link = runtime / "unsafe"
    if target_kind == "directory":
        link.symlink_to(external_dir, target_is_directory=True)
    elif target_kind == "file":
        link.symlink_to(external_file)
    else:
        link.symlink_to(tmp_path / "missing")

    with pytest.raises(module.ConfigurationError, match="unsafe runtime tree") as captured:
        module.secure_runtime_tree(runtime)

    assert "outside" not in str(captured.value)
    assert _mode(runtime) == 0o755
    assert _mode(prior) == 0o666
    assert prior.read_text(encoding="utf-8") == "keep"
    assert _mode(external_dir) == 0o755
    assert _mode(external_file) == 0o644


def test_secure_runtime_tree_rejects_symlink_root_without_touching_target(
    tmp_path: Path,
) -> None:
    module = load_module()
    external = tmp_path / "external"
    external.mkdir(mode=0o755)
    marker = external / "marker.txt"
    marker.write_text("outside", encoding="utf-8")
    marker.chmod(0o644)
    external.chmod(0o755)
    runtime = tmp_path / "owned"
    runtime.symlink_to(external, target_is_directory=True)

    with pytest.raises(module.ConfigurationError, match="unsafe runtime tree"):
        module.secure_runtime_tree(runtime)

    assert _mode(external) == 0o755
    assert _mode(marker) == 0o644
    assert marker.read_text(encoding="utf-8") == "outside"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is not supported")
def test_secure_runtime_tree_rejects_fifo_without_partial_chmod(tmp_path: Path) -> None:
    module = load_module()
    runtime = tmp_path / "owned"
    runtime.mkdir(mode=0o755)
    prior = runtime / "prior.json"
    prior.write_text("keep", encoding="utf-8")
    prior.chmod(0o666)
    os.mkfifo(runtime / "pipe", 0o644)

    with pytest.raises(module.ConfigurationError, match="unsafe runtime tree"):
        module.secure_runtime_tree(runtime)

    assert _mode(runtime) == 0o755
    assert _mode(prior) == 0o666


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix socket is unavailable")
def test_secure_runtime_tree_rejects_owned_socket_without_partial_chmod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_module()
    runtime = tmp_path / "owned"
    runtime.mkdir(mode=0o755)
    prior = runtime / "prior.json"
    prior.write_text("keep", encoding="utf-8")
    prior.chmod(0o666)
    monkeypatch.chdir(runtime)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind("owned.sock")
    try:
        with pytest.raises(module.ConfigurationError, match="unsafe runtime tree"):
            module.secure_runtime_tree(runtime)
    finally:
        listener.close()

    assert _mode(runtime) == 0o755
    assert _mode(prior) == 0o666


def test_secure_runtime_file_fixes_regular_file_and_rejects_symlink(
    tmp_path: Path,
) -> None:
    module = load_module()
    metadata = tmp_path / "metadata.json"
    metadata.write_text("payload", encoding="utf-8")
    metadata.chmod(0o666)

    module.secure_runtime_file(metadata)

    assert _mode(metadata) == 0o600
    assert metadata.read_text(encoding="utf-8") == "payload"
    external = tmp_path / "external.json"
    external.write_text("outside", encoding="utf-8")
    external.chmod(0o644)
    metadata.unlink()
    metadata.symlink_to(external)
    with pytest.raises(module.ConfigurationError, match="unsafe runtime file"):
        module.secure_runtime_file(metadata)
    assert _mode(external) == 0o644
    assert external.read_text(encoding="utf-8") == "outside"
