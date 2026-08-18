#!/usr/bin/env python3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import utm_21_clone
from scripts.utm_21_clone import build_payload, clone_target, validate_repo_url


def test_direct_run_uses_exact_inventory_context_instead_of_feishu_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "direct-v1-gvby-5fca26ec45bc2902632cef71"
    observed: dict[str, object] = {}

    def resolver(**kwargs):
        observed.update(kwargs)
        return SimpleNamespace(
            context_id=run_id,
            parent_title="海淋",
            page_title="SlopeRight-gvby",
            vm_name="gvby",
            vm_ip="192.168.64.68",
            vm_user="gvby",
        )

    monkeypatch.setattr(
        utm_21_clone,
        "exact_owned_run",
        lambda *_args, **_kwargs: pytest.fail("direct run must not read Feishu runs"),
    )

    utm_21_clone.verify_execution_context(
        run_id=run_id,
        parent_title="海淋",
        page_title="SlopeRight-gvby",
        vm_name="gvby",
        vm_ip="192.168.64.68",
        vm_user="gvby",
        direct_resolver=resolver,
    )

    assert observed == {
        "parent_title": "海淋",
        "page_title": "SlopeRight-gvby",
        "vm_name": "gvby",
        "vm_ip": "192.168.64.68",
        "vm_user": "gvby",
    }


def main() -> None:
    url = "https://codeup.aliyun.com/team/example.git"
    assert validate_repo_url(url) == url
    for invalid in (
        "http://codeup.aliyun.com/team/example.git",
        "https://user:secret@codeup.aliyun.com/team/example.git",
        "https://example.com/team/example.git",
        "https://codeup.aliyun.com/team/example",
    ):
        try:
            validate_repo_url(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(invalid)

    assert clone_target("abcd", url) == Path("/Users/example/StudioProjects/example")
    payload = build_payload("user", "secret", url, clone_target("abcd", url))
    assert payload.count(b"\0") == 4
    assert payload.endswith(b"\0")

    source = Path("scripts/utm_21_clone.py").read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "stdout=subprocess.PIPE" not in source
    assert "CODEUP_USERNAME" in source and "CODEUP_PASSWORD" in source
    assert "RUN_HOST_OWNERSHIP_MISMATCH" in source
    print("UTM_21_CLONE_HELPER=verified")


if __name__ == "__main__":
    main()
