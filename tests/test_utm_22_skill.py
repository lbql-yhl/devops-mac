#!/usr/bin/env python3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/utm-22/SKILL.md"
SCRIPT = ROOT / "scripts/utm_22_distribute.mjs"
UPLOAD_SCRIPT = ROOT / "scripts/utm_22_upload.py"
DOC = ROOT / "docs/utm-22.md"


def main() -> None:
    skill = SKILL.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")
    upload_script = UPLOAD_SCRIPT.read_text(encoding="utf-8")
    doc = DOC.read_text(encoding="utf-8")
    combined = "\n".join((skill, doc, script, upload_script))

    for value in (
        "Runner.xcworkspace", "Xcode GUI", "Product", "Archive",
        "ARCHIVE_ATTEMPT_ID", "XCODE_GUI_RECOVERY=verified",
        "UPLOAD_ATTEMPT_ID", "--attempt-file", "prepare", "distribute",
        "buildUploads", "buildUploadFiles", "uploaded=true",
        "5/10/20 秒", "15/30/60/120 秒",
        "BUILD_UPLOAD_FINAL_STATE=COMPLETE", "BUILD_PROCESSING_STATE=VALID",
        "You must add the com.apple.developer.game-center key in Xcode.",
        "SIGNED_GAME_CENTER=verified", "PROFILE_GAME_CENTER=verified",
        "CFBundleShortVersionString", "validateEmbeddedFrameworkPlists",
        "UTM_22=verified", "立即继续 `utm-23`",
        "$PROJECT_ROOT/scripts/utm_22_upload.py --run-id '<run-id>'",
        "$PROJECT_ROOT/scripts/utm_22_upload.py --target '<应用名>-<VM名>'",
        "--archive-path '<精确 xcarchive 绝对路径>'",
        "同一 upload attempt 复用一个 JWT",
        "不读取项目源码", "SUBMISSION_GUEST_PASSWORD", "不启动、重启、恢复或切换 VM",
    ):
        assert value in combined, value

    for value in (
        "classifyMatchingUploads", "resumePackagedIpa", "loadOrCreateAttempt",
        "ambiguous_existing_upload", "create_result_ambiguous",
        "recovered_after_create_result_unknown", "same-attempt IPA hash mismatch",
        "attemptRecoveryAction", "classifyExistingBuilds", "readUploadConfiguration",
        "selectArchiveCandidate", "exactAppId", "upload-config", "discover",
        "apiAuthorization",
    ):
        assert value in script, value

    assert "archive_path=args.archive_path" in upload_script

    for forbidden in (
        "upload-existing", "xcodebuild archive", "xcodebuild -exportArchive",
        "git commit", "git push",
    ):
        assert forbidden not in combined, forbidden

    print("UTM_22_STABLE_ATTEMPTS=verified")


if __name__ == "__main__":
    main()
