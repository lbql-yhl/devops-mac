#!/usr/bin/env python3
import hashlib
import importlib
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType, ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services import host_config  # noqa: E402
from services.host_config import ConfigurationError  # noqa: E402


@pytest.fixture
def vm_inventory_scan() -> ModuleType:
    return importlib.import_module("scripts.vm_inventory_scan")


def _write_delivery_settings(path: Path, *, version: str = "v1") -> None:
    path.write_text(
        f"FEISHU_APP_ID=app-{version}\n"
        f"FEISHU_APP_SECRET=secret-{version}\n"
        f"FEISHU_DAILY_REPORT_CHAT_ID=report-{version}\n",
        encoding="utf-8",
    )


def test_import_does_not_read_host_dotenv() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import services.host_config as host_config
host_config._dotenv_setting = lambda *_args, **_kwargs: (_ for _ in ()).throw(
    AssertionError('inventory scan import must not read host .env')
)
import scripts.vm_inventory_scan
print('IMPORT_OK')
""",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "IMPORT_OK"


def test_report_starts_with_date_and_hash_is_stable(
    vm_inventory_scan: ModuleType,
) -> None:
    report = vm_inventory_scan.build_report(
        {
            "scan_at": "2026-07-29T08:00:00+00:00",
            "newly_reusable": ["abcd"],
            "unregistered": [{"vm_name": "efgh", "bundle_path": "/Volumes/AutoA/images/efgh.utm"}],
        }
    )
    assert report.splitlines()[0] == "2026-07-29"
    assert "未登记虚拟机：efgh" in report
    assert vm_inventory_scan.report_sha256(report) == hashlib.sha256(
        report.encode("utf-8")
    ).hexdigest()


def test_send_report_requires_configured_daily_destination_before_send(
    vm_inventory_scan: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "FEISHU_APP_ID=app-id\nFEISHU_APP_SECRET=app-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        vm_inventory_scan,
        "send_text_message",
        lambda *_args: pytest.fail("delivery must not start without a destination"),
    )

    with pytest.raises(ConfigurationError) as captured:
        vm_inventory_scan._send_report(
            "report",
            "digest",
            "digest",
            environ={},
            env_path=env_path,
        )

    assert str(captured.value) == (
        "missing required host setting: FEISHU_DAILY_REPORT_CHAT_ID"
    )


def test_send_report_uses_configured_daily_destination(
    vm_inventory_scan: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sent: list[tuple[str, str, str, str]] = []
    env_path = tmp_path / ".env"
    env_path.write_text(
        "FEISHU_APP_ID=app-id\n"
        "FEISHU_APP_SECRET=app-secret\n"
        "FEISHU_DAILY_REPORT_CHAT_ID=configured-daily-chat\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        vm_inventory_scan,
        "send_text_message",
        lambda *args: sent.append(args),
    )

    vm_inventory_scan._send_report(
        "report",
        "digest",
        "digest",
        environ={},
        env_path=env_path,
    )

    assert sent == [
        ("app-id", "app-secret", "configured-daily-chat", "report")
    ]


def test_send_report_opens_and_parses_one_settings_snapshot(
    vm_inventory_scan: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    _write_delivery_settings(env_path)
    open_calls = 0
    sent: list[tuple[str, str, str, str]] = []
    real_open = host_config.os.open

    def counted_open(path: object, flags: int) -> int:
        nonlocal open_calls
        open_calls += 1
        return real_open(path, flags)

    monkeypatch.setattr(host_config.os, "open", counted_open)
    monkeypatch.setattr(
        vm_inventory_scan,
        "send_text_message",
        lambda *args: sent.append(args),
    )

    vm_inventory_scan._send_report(
        "report",
        "digest",
        "digest",
        environ={},
        env_path=env_path,
    )

    assert open_calls == 1
    assert sent == [("app-v1", "secret-v1", "report-v1", "report")]


def test_send_report_rejects_symlink_without_revealing_values(
    vm_inventory_scan: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "real.env"
    env_path = tmp_path / ".env"
    _write_delivery_settings(target, version="symlink-secret")
    env_path.symlink_to(target)
    monkeypatch.setattr(
        vm_inventory_scan,
        "send_text_message",
        lambda *_args: pytest.fail("unsafe settings must fail before network"),
    )

    with pytest.raises(ConfigurationError) as captured:
        vm_inventory_scan._send_report(
            "report",
            "digest",
            "digest",
            environ={},
            env_path=env_path,
        )

    assert "symlink-secret" not in str(captured.value)


def test_send_report_rejects_settings_replacement_during_read(
    vm_inventory_scan: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    old_path = tmp_path / "old.env"
    _write_delivery_settings(env_path, version="original-secret")
    real_fdopen = host_config.os.fdopen

    def replacing_fdopen(descriptor: int, *args: object, **kwargs: object):
        handle = real_fdopen(descriptor, *args, **kwargs)
        env_path.replace(old_path)
        _write_delivery_settings(env_path, version="replacement-secret")
        return handle

    monkeypatch.setattr(host_config.os, "fdopen", replacing_fdopen)
    monkeypatch.setattr(
        vm_inventory_scan,
        "send_text_message",
        lambda *_args: pytest.fail("replaced settings must fail before network"),
    )

    with pytest.raises(ConfigurationError) as captured:
        vm_inventory_scan._send_report(
            "report",
            "digest",
            "digest",
            environ={},
            env_path=env_path,
        )

    message = str(captured.value)
    assert "original-secret" not in message
    assert "replacement-secret" not in message


def test_send_report_does_not_mix_two_snapshot_versions(
    vm_inventory_scan: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots = iter(
        (
            MappingProxyType(
                {
                    "FEISHU_APP_ID": "app-v1",
                    "FEISHU_APP_SECRET": "secret-v1",
                    "FEISHU_DAILY_REPORT_CHAT_ID": "report-v1",
                }
            ),
            MappingProxyType(
                {
                    "FEISHU_APP_ID": "app-v2",
                    "FEISHU_APP_SECRET": "secret-v2",
                    "FEISHU_DAILY_REPORT_CHAT_ID": "report-v2",
                }
            ),
        )
    )
    calls = 0
    sent: list[tuple[str, str, str, str]] = []

    def next_snapshot(**_kwargs: object):
        nonlocal calls
        calls += 1
        return next(snapshots)

    monkeypatch.setattr(
        vm_inventory_scan,
        "host_settings_snapshot",
        next_snapshot,
        raising=False,
    )
    monkeypatch.setattr(
        vm_inventory_scan,
        "send_text_message",
        lambda *args: sent.append(args),
    )

    vm_inventory_scan._send_report(
        "report",
        "digest",
        "digest",
        environ={},
        env_path=Path("missing.env"),
    )

    assert calls == 1
    assert sent == [("app-v1", "secret-v1", "report-v1", "report")]


if __name__ == "__main__":
    raise SystemExit(0)
