#!/usr/bin/env python3
from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import utm_env_download_assets as utm_env
from scripts.utm_env_download_assets import (
    BaseLocation,
    download_assets,
    parse_github_repo_url,
    parse_base_url,
    select_exact_record,
)


FIXTURE_APP_IDENTIFIER = "LNFdbb2cxaauuvsVGuGcebMhnxm"
FIXTURE_TABLE_IDENTIFIER = "tblcENvSwMO6lhSH"
FIXTURE_VIEW_IDENTIFIER = "vewfxNVEGj"
FIXTURE_TENANT_VALUE = "tenant-token"
BASE_URL = (
    f"https://qv0zc1dq6qy.feishu.cn/base/{FIXTURE_APP_IDENTIFIER}"
    f"?table={FIXTURE_TABLE_IDENTIFIER}&view={FIXTURE_VIEW_IDENTIFIER}"
)


def xlsx_bytes() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("_rels/.rels", "<Relationships/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
    return output.getvalue()


def zip_bytes() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("source-a.png", b"\x89PNG\r\n\x1a\nimage-a")
        archive.writestr("nested/source-b.jpg", b"\xff\xd8\xffimage-b")
        archive.writestr("__MACOSX/._source-a.png", b"metadata")
    return output.getvalue()


class FakeClient:
    def __init__(self, records: list[dict], payloads: dict[str, bytes]) -> None:
        self.records = records
        self.payloads = payloads
        self.downloaded: list[str] = []

    def list_records(self, location: BaseLocation) -> list[dict]:
        assert location == BaseLocation(
            app_token=FIXTURE_APP_IDENTIFIER,
            table_id=FIXTURE_TABLE_IDENTIFIER,
            view_id=FIXTURE_VIEW_IDENTIFIER,
        )
        return self.records

    def download_media(self, file_token: str) -> bytes:
        self.downloaded.append(file_token)
        return self.payloads[file_token]


class FakeGitHubClient:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def retrieve_repository(self, repo_url: str, destination: Path) -> tuple[str, str]:
        self.urls.append(repo_url)
        destination.mkdir(parents=True)
        (destination / "README.md").write_text("FlagCue source")
        (destination / "src").mkdir()
        (destination / "src" / "main.dart").write_text("void main() {}")
        return "d6dde346f55006a648967ee4aa7229282f7d7f89", "github_zip"


class FakeGitRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.environments: list[dict[str, str]] = []

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        self.environments.append(dict(kwargs["env"]))
        if "ls-remote" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="d6dde346f55006a648967ee4aa7229282f7d7f89\tHEAD\n",
                stderr="",
            )
        raise AssertionError(command)


def code_zip_bytes() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("repo-sha/README.md", "FlagCue source")
        archive.writestr("repo-sha/src/main.dart", "void main() {}")
    return output.getvalue()


def record(app_name: str = "FlagCue", *, development_name: str = "shots.zip") -> dict:
    return {
        "record_id": "rec_flagcue",
        "fields": {
            "应用名": app_name,
            "研发截图": [{"file_token": "dev-token", "name": development_name}],
            "金币表格文件": [{"file_token": "sheet-token", "name": "coins.XLSX"}],
            "金币商店截图": [{"file_token": "coin-token", "name": "store.PNG"}],
            "代码 URL": {
                "link": "https://github.com/wangrenzhu-ola/appfactory-claude-kf-20260730T013030Z-f8e9a202",
                "text": "https://github.com/wangrenzhu-ola/appfactory-claude-kf-20260730T013030Z-f8e9a202",
            },
        },
    }


def record_with_store_image_name(source_name: str) -> dict:
    value = record()
    value["fields"]["金币商店截图"] = [
        {"file_token": "coin-token", "name": source_name}
    ]
    return value


class UTMEnvDownloadAssetsTests(unittest.TestCase):
    def test_feishu_client_reuses_record_attachment_url_with_required_context(self) -> None:
        server_url = (
            "https://open.feishu.cn/open-apis/drive/v1/medias/dev-token/download"
            "?extra=required-context"
        )
        source_record = record()
        source_record["fields"]["研发截图"][0]["url"] = server_url
        client = utm_env.FeishuClient.__new__(utm_env.FeishuClient)
        client._token = FIXTURE_TENANT_VALUE
        client._media_download_urls = {}

        with mock.patch.object(
            utm_env,
            "_json_request",
            return_value={
                "data": {"items": [source_record], "has_more": False}
            },
        ), mock.patch.object(
            utm_env, "_request", return_value=b"downloaded"
        ) as request:
            client.list_records(parse_base_url(BASE_URL))
            self.assertEqual(client.download_media("dev-token"), b"downloaded")

        self.assertEqual(request.call_args.args[0], server_url)

    def test_parses_the_exact_base_location(self) -> None:
        self.assertEqual(
            parse_base_url(BASE_URL),
            BaseLocation(
                app_token=FIXTURE_APP_IDENTIFIER,
                table_id=FIXTURE_TABLE_IDENTIFIER,
                view_id=FIXTURE_VIEW_IDENTIFIER,
            ),
        )

    def test_application_name_match_is_case_sensitive_and_unique(self) -> None:
        records = [record("FlagCue"), record("flagcue")]
        self.assertEqual(select_exact_record(records, "FlagCue")["record_id"], "rec_flagcue")
        with self.assertRaisesRegex(ValueError, "found 0"):
            select_exact_record(records, "FLAGCUE")

    def test_accepts_only_a_canonical_github_repository_url(self) -> None:
        self.assertEqual(
            parse_github_repo_url("https://github.com/Owner/Repo.git"),
            ("Owner", "Repo"),
        )
        for invalid in (
            "http://github.com/Owner/Repo",
            "https://github.com/Owner/Repo/issues",
            "https://example.com/Owner/Repo",
            "https://github.com/Owner/Repo?token=secret",
        ):
            with self.assertRaises(ValueError):
                parse_github_repo_url(invalid)

    def test_github_repository_downloads_the_locked_head_zip_without_retaining_zip(self) -> None:
        self.assertTrue(
            hasattr(utm_env, "GitHubRepositoryClient"),
            "repository clone client is not implemented",
        )
        runner = FakeGitRunner()
        zip_requests: list[tuple[str, str]] = []

        def download_zip(repo_url: str, commit_sha: str) -> bytes:
            zip_requests.append((repo_url, commit_sha))
            return code_zip_bytes()

        client = utm_env.GitHubRepositoryClient(
            run_command=runner,
            download_zip=download_zip,
        )
        repo_url = record()["fields"]["代码 URL"]["link"]

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "FlagCue-git"
            commit_sha, method = client.retrieve_repository(repo_url, destination)
            self.assertEqual((destination / "README.md").read_text(), "FlagCue source")
            self.assertFalse((destination / ".git").exists())
            self.assertFalse(any(path.suffix == ".zip" for path in destination.parent.iterdir()))

        self.assertEqual(commit_sha, "d6dde346f55006a648967ee4aa7229282f7d7f89")
        self.assertEqual(method, "github_zip")
        self.assertEqual(len(runner.commands), 1)
        self.assertIn("ls-remote", runner.commands[0])
        self.assertTrue(all(env["GIT_TERMINAL_PROMPT"] == "0" for env in runner.environments))
        combined = " ".join(part for command in runner.commands for part in command)
        self.assertNotIn("icloud.com", combined)
        self.assertNotIn("archive", combined)
        self.assertEqual(zip_requests, [(repo_url, commit_sha)])

    def test_downloads_three_unique_attachments_to_the_shared_root(self) -> None:
        payloads = {
            "dev-token": zip_bytes(),
            "sheet-token": xlsx_bytes(),
            "coin-token": b"\x89PNG\r\n\x1a\ncoin image",
        }
        client = FakeClient([record()], payloads)
        github = FakeGitHubClient()
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary)
            with mock.patch(
                "tempfile.mkdtemp",
                side_effect=AssertionError("temporary staging directory is forbidden"),
            ):
                summary = download_assets(
                    base_url=BASE_URL,
                    app_name="FlagCue",
                    output_root=output_root,
                    client=client,
                    github_client=github,
                )

            self.assertEqual(client.downloaded, ["dev-token", "sheet-token", "coin-token"])
            self.assertEqual(
                sorted(path.name for path in output_root.iterdir()),
                ["FlagCue", "FlagCue-git", "FlagCue.png", "FlagCue.xlsx"],
            )
            self.assertEqual((output_root / "FlagCue.xlsx").read_bytes(), payloads["sheet-token"])
            self.assertEqual((output_root / "FlagCue.png").read_bytes(), payloads["coin-token"])
            self.assertEqual((output_root / "FlagCue" / "FlagCue1.png").read_bytes(), b"\x89PNG\r\n\x1a\nimage-a")
            self.assertEqual((output_root / "FlagCue" / "FlagCue2.jpg").read_bytes(), b"\xff\xd8\xffimage-b")
            self.assertEqual((output_root / "FlagCue-git" / "README.md").read_text(), "FlagCue source")
            self.assertFalse((output_root / "FlagCue-git" / ".git").exists())
            self.assertEqual(github.urls, [record()["fields"]["代码 URL"]["link"]])
            self.assertEqual(summary["app_name"], "FlagCue")
            self.assertEqual(summary["record_id"], "rec_flagcue")
            self.assertEqual(summary["attachment_count"], 3)
            self.assertEqual(summary["development_image_count"], 2)
            self.assertEqual(summary["code_repository_count"], 1)
            self.assertEqual(summary["code_download_method"], "github_zip")
            self.assertEqual(summary["code_commit_sha"], "d6dde346f55006a648967ee4aa7229282f7d7f89")
            self.assertEqual(summary["asset_count"], 4)
            self.assertEqual(summary["asset_filename_case"], "exact")
            self.assertEqual(
                sorted(item["name"] for item in summary["files"]),
                ["FlagCue", "FlagCue-git", "FlagCue.png", "FlagCue.xlsx"],
            )
            development = next(item for item in summary["files"] if item["name"] == "FlagCue")
            self.assertEqual(development["type"], "directory")
            self.assertRegex(development["sha256"], r"^[0-9a-f]{64}$")
            repository = next(item for item in summary["files"] if item["name"] == "FlagCue-git")
            self.assertEqual(repository["type"], "directory")
            self.assertRegex(repository["sha256"], r"^[0-9a-f]{64}$")

    def test_selects_the_original_base_record_but_writes_current_app_names(self) -> None:
        payloads = {
            "dev-token": zip_bytes(),
            "sheet-token": xlsx_bytes(),
            "coin-token": b"\x89PNG\r\n\x1a\ncoin image",
        }
        client = FakeClient([record("PanSwap")], payloads)
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary)
            summary = download_assets(
                base_url=BASE_URL,
                app_name="PanSwap-CakeBaking",
                record_app_name="PanSwap",
                output_root=output_root,
                client=client,
                github_client=FakeGitHubClient(),
            )

            self.assertEqual(summary["app_name"], "PanSwap-CakeBaking")
            self.assertEqual(summary["record_app_name"], "PanSwap")
            self.assertEqual(
                sorted(path.name for path in output_root.iterdir()),
                [
                    "PanSwap-CakeBaking",
                    "PanSwap-CakeBaking-git",
                    "PanSwap-CakeBaking.png",
                    "PanSwap-CakeBaking.xlsx",
                ],
            )

    def test_uses_case_exact_fixed_workbook_and_png_names(self) -> None:
        payloads = {
            "dev-token": zip_bytes(),
            "sheet-token": xlsx_bytes(),
            "coin-token": b"\x89PNG\r\n\x1a\ncoin image",
        }
        client = FakeClient([record_with_store_image_name("store.JPG")], payloads)
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary)
            summary = download_assets(
                base_url=BASE_URL,
                app_name="FlagCue",
                output_root=output_root,
                client=client,
                github_client=FakeGitHubClient(),
            )

            self.assertTrue((output_root / "FlagCue.xlsx").is_file())
            self.assertTrue((output_root / "FlagCue.png").is_file())
            self.assertFalse((output_root / "FlagCue.jpg").exists())
            self.assertEqual(
                sorted(item["name"] for item in summary["files"]),
                ["FlagCue", "FlagCue-git", "FlagCue.png", "FlagCue.xlsx"],
            )

    def test_rejects_non_png_store_image_instead_of_mislabeling_it(self) -> None:
        payloads = {
            "dev-token": zip_bytes(),
            "sheet-token": xlsx_bytes(),
            "coin-token": b"\xff\xd8\xffjpeg image",
        }
        client = FakeClient([record_with_store_image_name("store.JPG")], payloads)
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "金币商店截图.*PNG"):
                download_assets(
                    base_url=BASE_URL,
                    app_name="FlagCue",
                    output_root=output_root,
                    client=client,
                    github_client=FakeGitHubClient(),
                )
            self.assertEqual(list(output_root.iterdir()), [])

    def test_requires_exactly_one_attachment_in_each_required_field(self) -> None:
        invalid = record()
        invalid["fields"]["研发截图"] = []
        client = FakeClient([invalid], {})
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "研发截图.*exactly one"):
                download_assets(
                    base_url=BASE_URL,
                    app_name="FlagCue",
                    output_root=Path(temporary),
                    client=client,
                    github_client=FakeGitHubClient(),
                )
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_rejects_an_unsafe_development_archive_without_partial_outputs(self) -> None:
        unsafe = io.BytesIO()
        with zipfile.ZipFile(unsafe, "w") as archive:
            archive.writestr("../escape.png", b"\x89PNG\r\n\x1a\nimage")
        client = FakeClient(
            [record()],
            {"dev-token": unsafe.getvalue(), "sheet-token": xlsx_bytes(), "coin-token": b"\x89PNG\r\n\x1a\ncoin"},
        )
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "unsafe path"):
                download_assets(
                    base_url=BASE_URL,
                    app_name="FlagCue",
                    output_root=Path(temporary),
                    client=client,
                    github_client=FakeGitHubClient(),
                )
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_never_overwrites_an_existing_target(self) -> None:
        client = FakeClient(
            [record()],
            {
            "dev-token": zip_bytes(),
                "sheet-token": xlsx_bytes(),
                "coin-token": b"\x89PNG\r\n\x1a\ncoin image",
            },
        )
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary)
            existing = output_root / "FlagCue.xlsx"
            existing.write_bytes(b"existing")
            with self.assertRaisesRegex(FileExistsError, "FlagCue.xlsx"):
                download_assets(
                    base_url=BASE_URL,
                    app_name="FlagCue",
                    output_root=output_root,
                    client=client,
                    github_client=FakeGitHubClient(),
                )
            self.assertEqual(existing.read_bytes(), b"existing")
            self.assertEqual(client.downloaded, [])
            self.assertEqual(sorted(path.name for path in output_root.iterdir()), ["FlagCue.xlsx"])


if __name__ == "__main__":
    unittest.main()
