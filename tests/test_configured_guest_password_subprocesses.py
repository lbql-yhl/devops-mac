from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

from scripts import utm_apps_delivery, utm_business_delivery, utm_image_delivery


@pytest.mark.parametrize(
    ("module", "sync", "stage_name", "staged", "expected_calls"),
    (
        (
            utm_apps_delivery,
            utm_apps_delivery.sync_utm_apps_playwright_files,
            "stage_utm_apps_playwright_files",
            {
                filename: f"digest-{filename}"
                for filename in utm_apps_delivery.PLAYWRIGHT_GUEST_FILES
            },
            7,
        ),
        (
            utm_business_delivery,
            utm_business_delivery.sync_business_guest_files,
            "stage_business_guest_files",
            {utm_business_delivery.BUSINESS_GUEST_FILES[0]: "business-digest"},
            3,
        ),
        (
            utm_image_delivery,
            utm_image_delivery.sync_utm_image_guest_file,
            "stage_utm_image_guest_file",
            {utm_image_delivery.GUEST_FILE: "image-digest"},
            3,
        ),
    ),
)
def test_each_ssh_or_scp_call_gets_a_fresh_password_broker_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module: ModuleType,
    sync: Callable[..., dict[str, str]],
    stage_name: str,
    staged: dict[str, str],
    expected_calls: int,
) -> None:
    environments: list[dict[str, str]] = []
    broker_calls = 0

    def password_environment() -> dict[str, str]:
        nonlocal broker_calls
        broker_calls += 1
        return {
            "SUBMISSION_SSH_ASKPASS_CAPABILITY": f"capability-{broker_calls}"
        }

    def run(
        args: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        assert args[0] in {"/usr/bin/ssh", "/usr/bin/scp"}
        environments.append(kwargs["env"])
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(module, stage_name, lambda shared_dir: staged)
    monkeypatch.setattr(module, "password_environment", password_environment)
    monkeypatch.setattr(module.subprocess, "run", run)

    assert sync(tmp_path, vm_user="submission", vm_ip="192.0.2.10") == staged
    assert broker_calls == expected_calls
    assert len(environments) == expected_calls
    assert [env["SUBMISSION_SSH_ASKPASS_CAPABILITY"] for env in environments] == [
        f"capability-{index}" for index in range(1, expected_calls + 1)
    ]
    assert len({id(environment) for environment in environments}) == expected_calls
