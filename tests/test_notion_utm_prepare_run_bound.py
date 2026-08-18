#!/usr/bin/env python3
import json
import os
import stat
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import notion_utm_prepare as prepare  # noqa: E402


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        prepare.RUNS_FILE = root / "feishu-runs.json"
        prepare.RUNS_FILE.write_text(
            json.dumps(
                {
                    "runs": [
                        {
                            "id": "run-a",
                            "app_name": "Alpha",
                            "vm_name": "abcd",
                            "submission_data": {
                                "app_name": "Alpha",
                                "host_machine": "海淋",
                                "developer_account": {},
                                "proxy": {},
                            },
                        },
                        {
                            "id": "run-b",
                            "app_name": "Beta",
                            "vm_name": "efgh",
                            "submission_data": {
                                "app_name": "Beta",
                                "host_machine": "other",
                                "developer_account": {},
                                "proxy": {},
                            },
                        },
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.environ["SUBMISSION_HOST_MACHINE"] = "海淋"

        data, run = prepare.load_submission("run-a")
        assert data["app_name"] == "Alpha"
        assert run["vm_name"] == "abcd"

        try:
            prepare.load_submission("run-b")
        except RuntimeError as exc:
            assert "host" in str(exc).lower()
        else:
            raise AssertionError("foreign-host run accepted")

        output = root / "account.txt"
        prepare.atomic_write_text(output, "secret")
        assert output.read_text(encoding="utf-8") == "secret"
        assert stat.S_IMODE(output.stat().st_mode) == 0o600

    print("NOTION_UTM_PREPARE_RUN_BOUND=verified")


if __name__ == "__main__":
    main()
