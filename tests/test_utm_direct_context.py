from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Callable

import pytest

from scripts.utm_clash_ip_target import BoundVM, TargetVMError


CONFIG_UUID = "0190EBDD-A7EE-48E0-BE01-D9070F649E4C"
REGISTERED_MAC = "d2:77:bd:d2:01:b6"


def _resolver_api() -> tuple[Callable[..., Any], type[Exception]]:
    try:
        module = importlib.import_module("scripts.utm_direct_context")
    except ModuleNotFoundError as error:
        pytest.fail(
            "direct-v1 shared resolver is not implemented: "
            "scripts.utm_direct_context",
            pytrace=False,
        )
    return module.resolve_direct_context, module.DirectContextError


def _bound_vm(images_dir: Path) -> BoundVM:
    return BoundVM(
        vm_name="igec",
        bundle_path=(images_dir / "igec.utm").resolve(),
        config_uuid=CONFIG_UUID,
        mac_address=REGISTERED_MAC,
    )


def _resolve(
    tmp_path: Path,
    *,
    vm_ip: str = "192.168.64.52",
    bound_loader: Callable[..., BoundVM] | None = None,
    status_loader: Callable[[str], str] | None = None,
    ip_loader: Callable[[BoundVM], str] | None = None,
) -> Any:
    resolve_direct_context, _error_type = _resolver_api()
    images_dir = tmp_path / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    database = tmp_path / "runtime" / "vm-inventory.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    database.touch()
    target = _bound_vm(images_dir)
    return resolve_direct_context(
        parent_title="海淋",
        page_title="SowSheet-igec",
        vm_name="igec",
        vm_ip=vm_ip,
        vm_user="igec",
        database=database,
        images_dir=images_dir,
        bound_loader=bound_loader or (
            lambda _database, _images_dir, _vm_name, _app_name: target
        ),
        status_loader=status_loader or (lambda _vm_name: "started"),
        ip_loader=ip_loader or (lambda _target: vm_ip),
    )


def test_direct_v1_resolves_only_the_exact_bound_running_vm_and_registered_mac_ip(
    tmp_path: Path,
) -> None:
    calls: list[tuple[Any, ...]] = []
    images_dir = tmp_path / "images"
    target = _bound_vm(images_dir)

    def load_bound(
        database: Path,
        requested_images_dir: Path,
        vm_name: str,
        app_name: str,
    ) -> BoundVM:
        calls.append((database, requested_images_dir, vm_name, app_name))
        return target

    def load_status(vm_name: str) -> str:
        calls.append(("status", vm_name))
        return "running"

    def load_ip(bound: BoundVM) -> str:
        calls.append(("ip", bound.config_uuid, bound.mac_address))
        return "192.168.64.52"

    context = _resolve(
        tmp_path,
        bound_loader=load_bound,
        status_loader=load_status,
        ip_loader=load_ip,
    )

    assert context.parent_title == "海淋"
    assert context.page_title == "SowSheet-igec"
    assert context.app_name == "SowSheet"
    assert context.vm_name == "igec"
    assert context.vm_ip == "192.168.64.52"
    assert context.vm_user == "igec"
    assert context.context_id.startswith("direct-v1-igec-")
    assert len(context.context_id.removeprefix("direct-v1-igec-")) == 24
    assert calls[0][2:] == ("igec", "SowSheet")
    assert calls[1:] == [
        ("status", "igec"),
        ("ip", CONFIG_UUID, REGISTERED_MAC),
    ]


def test_direct_v1_id_is_stable_across_ip_and_absolute_bundle_root_changes(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first-host"
    second_root = tmp_path / "migrated-host"

    first = _resolve(
        first_root,
        vm_ip="192.168.64.52",
        bound_loader=lambda *_args: _bound_vm(first_root / "images"),
    )
    second = _resolve(
        second_root,
        vm_ip="192.168.64.99",
        bound_loader=lambda *_args: _bound_vm(second_root / "images"),
    )

    assert first.context_id == second.context_id
    assert "192.168.64" not in first.context_id
    assert str(first_root) not in first.context_id
    assert str(second_root) not in second.context_id


@pytest.mark.parametrize("binding_problem", ["missing", "ambiguous"])
def test_direct_v1_rejects_missing_or_ambiguous_inventory_binding(
    tmp_path: Path,
    binding_problem: str,
) -> None:
    _resolve_direct_context, error_type = _resolver_api()

    def blocked_binding(*_args: Any) -> BoundVM:
        raise TargetVMError(f"{binding_problem} exact application binding")

    with pytest.raises(error_type, match="DIRECT_CONTEXT_BINDING_BLOCKED"):
        _resolve(tmp_path, bound_loader=blocked_binding)


@pytest.mark.parametrize(
    ("status", "registered_ip", "expected_error"),
    [
        ("stopped", "192.168.64.52", "DIRECT_CONTEXT_VM_NOT_RUNNING"),
        ("started", "192.168.64.99", "DIRECT_CONTEXT_IP_MISMATCH"),
    ],
)
def test_direct_v1_rejects_non_running_vm_or_registered_mac_ip_mismatch(
    tmp_path: Path,
    status: str,
    registered_ip: str,
    expected_error: str,
) -> None:
    _resolve_direct_context, error_type = _resolver_api()

    with pytest.raises(error_type, match=expected_error):
        _resolve(
            tmp_path,
            status_loader=lambda _vm_name: status,
            ip_loader=lambda _target: registered_ip,
        )
