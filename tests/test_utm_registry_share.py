#!/usr/bin/env python3
import importlib.util
import plistlib
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "utm_registry_share.py"


def load_module():
    spec = importlib.util.spec_from_file_location("utm_registry_share", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def registry(source_uuid: str, target_uuid: str, share: str):
    return {
        "Registry": {
            source_uuid: {"SharedDirectories": [{"Path": share, "ReadOnly": True, "Bookmark": b"source-bookmark"}]},
            target_uuid: {"SharedDirectories": [{"Path": "/other", "ReadOnly": False, "Bookmark": b"other-bookmark"}]},
        }
    }


def main() -> None:
    module = load_module()
    source_uuid = "11111111-1111-1111-1111-111111111111"
    target_uuid = "22222222-2222-2222-2222-222222222222"
    share = "/Volumes/External/shared"
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "prefs.plist"
        path.write_bytes(plistlib.dumps(registry(source_uuid, target_uuid, share), fmt=plistlib.FMT_BINARY))
        changed = module.sync_shared_directory(path, source_uuid, target_uuid, share)
        assert changed is True
        result = module.verify_shared_directory(path, target_uuid, share)
        assert result["count"] == 1
        assert result["read_only"] is True
        assert result["bookmark_bytes"] == len(b"source-bookmark")
        assert module.sync_shared_directory(path, source_uuid, target_uuid, share) is False

        broken = plistlib.loads(path.read_bytes())
        target_entries = broken["Registry"][target_uuid]["SharedDirectories"]
        next(item for item in target_entries if item["Path"] == share)["ReadOnly"] = False
        path.write_bytes(plistlib.dumps(broken, fmt=plistlib.FMT_BINARY))
        try:
            module.verify_shared_directory(path, target_uuid, share)
        except ValueError as exc:
            assert "read-only" in str(exc)
        else:
            raise AssertionError("writable directory accepted")
    print("UTM_REGISTRY_SHARE=verified")


if __name__ == "__main__":
    main()
