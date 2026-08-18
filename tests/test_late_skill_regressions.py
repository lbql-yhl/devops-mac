#!/usr/bin/env python3
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
sys.path.insert(0, str(ROOT))

from scripts.utm_env_generate import write_env  # noqa: E402


def read(name: str) -> str:
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


def sample_env() -> dict[str, str]:
    return {
        "APP_ID": "1234567890",
        "CONTACT_PHONE": "+15551234567",
        "CONTACT_EMAIL": "person@example.com",
        "VM_NAME": "abcd",
        "CONTACT_FIRST_NAME": "Ada",
        "CONTACT_LAST_NAME": "Lovelace",
        "COPYRIGHT": "Example",
        "BUNDLE_ID": "com.example.app",
        "PRIMARY_CATEGORY": "GRAPHICS_AND_DESIGN",
        "DESCRIPTION": "First paragraph.",
        "KEYWORDS": "photo,design",
        "TOP_LEVEL_DOMAIN": "example.com",
        "SUPPORT_URL": "https://example.com/support",
        "PRIVACY_POLICY_URL": "https://example.com/privacy",
        "PRIVACY_CHOICES_URL": "https://example.com/terms",
    }


def main() -> None:
    generator_source = (ROOT / "scripts" / "utm_env_generate.py").read_text(encoding="utf-8")
    for required in ("os.fsync", "os.replace", "ENV_WRITE", "ENV_READBACK"):
        assert required in generator_source, required

    with tempfile.TemporaryDirectory() as tmp:
        output = write_env(sample_env(), Path(tmp))
        inode = output.stat().st_ino
        assert output.stat().st_mode & 0o777 == 0o600
        output_again = write_env(sample_env(), Path(tmp))
        assert output_again.stat().st_ino == inode
        assert output_again.read_bytes() == output.read_bytes()

    utm_env = read("utm-env")
    for required in (
        "${SUBMISSION_SHARED_DIR}/Fire_One_en1.3/.env",
        "ENV_TARGET_DIR=verified",
        "ENV_READBACK=exact",
        "HOST_ENV=GENERATED_AND_VERIFIED",
    ):
        assert required in utm_env, required
    for forbidden in (
        "GUEST_ENV_WRITE=atomic_verified",
        "SSH_PRIVATE_KEY=verified",
        "UTM_ENV=SSH_COPIED",
    ):
        assert forbidden not in utm_env, forbidden

    for required in (
        "scripts/utm_env.py",
        "scripts/utm_env_download_assets.py",
        "scripts/utm_env_guest_finalize.py",
        "qv0zc1dq6qy.feishu.cn/base/LNFdbb2cxaauuvsVGuGcebMhnxm",
        "table=tblcENvSwMO6lhSH&view=vewfxNVEGj",
        "研发截图",
        "金币表格文件",
        "金币商店截图",
        "代码 URL",
        "code_commit_sha",
        "code_download_method",
        "asset_count=4",
        "<应用名>.xlsx",
        "<应用名>.png",
        "asset_filename_case=exact",
        "${SUBMISSION_SHARED_DIR}",
        "/Volumes/My Shared Files/共享文件",
        "Fire_One_en1.3",
        "HOST_GUEST_ASSET_SHA256=exact",
        "GUEST_DOWNLOAD_ASSET_COPY=three_assets_verified_or_identical_existing",
        "GUEST_CODE_PATH=/Users/<vm_name>/StudioProjects/<应用名>",
        "GUEST_CODE_COPY=verified_or_identical_existing",
        "FIRE_ONE_ENV_SOURCE=Fire_One_en1.3/.env",
        "FIRE_ONE_ENV_SHA256=exact",
        "FIRE_ONE_ENV_MODE=600",
    ):
        assert required in utm_env, required
    for forbidden in (
        "研发金币图链接",
        "read-field --heading '应用信息'",
        "pbpaste | python3 scripts/shared_operations.py browser-url",
        "Fire_One_en1.2",
        "tccutil reset SystemPolicyAppBundles",
        "/Library/Application Support/com.apple.TCC/TCC.db",
        "<应用名>.<图片扩展名>",
        "金币商店截图为 `<应用名><检测到的图片扩展名>`",
        "Computer Use",
        "app-management-ensure",
        "app-management-verify",
        "Privacy_AppBundles",
        "TERMINAL_APP_MANAGEMENT=verified",
        "`$HOME/Downloads/` 下的四项资产",
    ):
        assert forbidden not in utm_env, forbidden

    for relative in ("README.md", "AGENTS.md", "docs/utm-env.md"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "/Users/<vm_name>/StudioProjects/<应用名>" in text, relative
        assert "<应用名>-git" in text, relative

    for name in ("utm-env", "utm-script"):
        skill = read(name)
        assert "Fire_One_en1.3" in skill, name
        assert "Fire_One_en1.2" not in skill, name

    utm_18 = read("utm-script")
    assert "`pkill` 必须返回 `0`" not in utm_18
    assert "幂等，可修复后执行一次新 attempt" not in utm_18
    for required in (
        "UTM_18_ATTEMPT_ID",
        "UTM_18_LOG_PATH=precommitted",
        "EDGE_CDP_HTTP=verified",
        "ZERO_BUSINESS_SIDE_EFFECTS=verified",
    ):
        assert required in utm_18, required

    utm_19 = read("utm-image")
    assert "/usr/bin/zipinfo -1" not in utm_19
    assert "partial_upload" not in utm_19
    for required in (
        "scripts/utm_image_source.py",
        "美女截图 链接",
        "研发截图",
        "ZIP/JPEG/PNG",
        "SCREENSHOT_SOURCE_FIELD=beauty_link|development_attachment",
        "SCREENSHOT_PREUPLOAD_CLASSIFICATION=empty|complete",
    ):
        assert required in utm_19, required

    utm_20 = read("utm-business")
    for required in (
        "mode-600",
        "SSH stdin JSON",
        "NOTION_BUSINESS_SAVED=written|verified_equal",
        "BANK_ACCOUNT_PROCESSING=verified",
        "utm-business-bank-info-missing",
    ):
        assert required in utm_20, required

    utm_21 = read("utm-21")
    assert "perl -pi" not in utm_21
    for required in (
        "repo_name not in {'.', '..'}",
        "CLONE_RESULT=created|existing_pristine|resumed",
        "PLACEHOLDER_STATE=needs_replacement|already_replaced",
        "REPLACEMENT_LEDGER=verified",
    ):
        assert required in utm_21, required

    utm_22 = read("utm-22")
    for required in (
        "ARCHIVE_LEDGER_MODE=600",
        "ARCHIVE_MANIFEST_SHA256",
        "VERSION_BUILD_SOURCE=verified",
        "GAME_CENTER_ARCHIVE_ATTEMPT_ID",
        "UPLOAD_ATTEMPT_MODE=600",
    ):
        assert required in utm_22, required

    utm_23 = read("utm-23")
    assert "/^[A-Za-z0-9._-]+$/" not in utm_23
    assert "var reviewRoot = `${PROJECT_ROOT}" not in utm_23
    for required in (
        "BROWSER_SESSION_RECHECKS=2",
        "PREPARATION_LEDGER_MODE=600",
        "ADD_BUILD_ATTEMPT_ID",
        "IAP_BATCH_ATTEMPT_ID",
        "APP_VERSION_LINK_ATTEMPT_ID",
        "FINAL_STATE_LEDGER=verified",
    ):
        assert required in utm_23, required

    utm_24 = read("utm-24")
    assert "/^[A-Za-z0-9._-]+$/" not in utm_24
    assert "var reviewRoot = `${PROJECT_ROOT}" not in utm_24
    for required in (
        "PRIVACY_CLIPBOARD=cleared",
        "REVIEW_SUBMIT_ATTEMPT_ID",
        "APPROVAL_DECISION_ID=bound",
        "EXPEDITE_SUBMIT_ATTEMPT_ID",
        "SCREENSHOT_RECOVERY=handoff_to_owner",
    ):
        assert required in utm_24, required

    utm_p8 = read("utm-p8")
    for required in (
        "scripts/utm_p8.py",
        "PROD_YML_API_CREDENTIALS=verified",
        "P8_FILE=verified",
        "NOTION_REFUND_CALLBACK_P8=verified",
    ):
        assert required in utm_p8, required

    print("LATE_SKILL_REGRESSIONS=verified")


if __name__ == "__main__":
    main()
