from __future__ import annotations

from pathlib import Path

import pytest

from services import host_config


def test_host_settings_snapshot_allows_an_absent_file(
    tmp_path: Path,
) -> None:
    settings = host_config.host_settings_snapshot(
        environ={"PROCESS_ONLY": "from-process"},
        env_path=tmp_path / "missing.env",
    )

    assert settings == {"PROCESS_ONLY": "from-process"}


def test_host_settings_snapshot_opens_once_and_process_values_override_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "SNAPSHOT_SOURCE=file-version\nFILE_ONLY=from-file\n",
        encoding="utf-8",
    )
    open_calls = 0
    real_open = host_config.os.open

    def counted_open(path: object, flags: int) -> int:
        nonlocal open_calls
        open_calls += 1
        return real_open(path, flags)

    monkeypatch.setattr(host_config.os, "open", counted_open)

    settings = host_config.host_settings_snapshot(
        environ={
            "SNAPSHOT_SOURCE": "process-version",
            "PROCESS_ONLY": "from-process",
        },
        env_path=env_path,
    )

    assert settings == {
        "SNAPSHOT_SOURCE": "process-version",
        "FILE_ONLY": "from-file",
        "PROCESS_ONLY": "from-process",
    }
    assert open_calls == 1
    with pytest.raises(TypeError):
        settings["SNAPSHOT_SOURCE"] = "mutated"  # type: ignore[index]


def test_host_settings_snapshot_rejects_symlink_without_revealing_values(
    tmp_path: Path,
) -> None:
    target = tmp_path / "real.env"
    link = tmp_path / ".env"
    fixture_value = "symlink-secret-must-not-leak"
    target.write_text(f"SECRET={fixture_value}\n", encoding="utf-8")
    link.symlink_to(target)

    with pytest.raises(host_config.ConfigurationError) as captured:
        host_config.host_settings_snapshot(environ={}, env_path=link)

    assert fixture_value not in str(captured.value)


def test_host_settings_snapshot_rejects_replacement_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    original_fixture_value = "original-version-secret"
    replacement_fixture_value = "replacement-version-secret"
    env_path.write_text(f"VERSION={original_fixture_value}\n", encoding="utf-8")
    original_path = tmp_path / "original.env"
    real_fdopen = host_config.os.fdopen
    replaced = False

    def replacing_fdopen(descriptor: int, *args: object, **kwargs: object):
        nonlocal replaced
        handle = real_fdopen(descriptor, *args, **kwargs)
        if not replaced:
            replaced = True
            env_path.replace(original_path)
            env_path.write_text(
                f"VERSION={replacement_fixture_value}\n", encoding="utf-8"
            )
        return handle

    monkeypatch.setattr(host_config.os, "fdopen", replacing_fdopen)

    with pytest.raises(host_config.ConfigurationError) as captured:
        host_config.host_settings_snapshot(environ={}, env_path=env_path)

    message = str(captured.value)
    assert original_fixture_value not in message
    assert replacement_fixture_value not in message
