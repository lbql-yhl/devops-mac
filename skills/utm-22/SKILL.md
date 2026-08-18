---
name: utm-22
description: Use when utm-21 has verified the workspace and the same UTM guest is ready to archive and upload its build.
---

# utm-22

Read and obey [`../_shared/AUTOMATION_CONTRACT.md`](../_shared/AUTOMATION_CONTRACT.md). From the repository root run `eval "$(python3 scripts/preflight.py --project-only --emit-shell)"` before the entry command.

## Contract

- Segment: 4, code processing.
- Input: `UTM_21=verified`, the same run/VM/IP/user, repository, existing `Runner.xcworkspace`, App ID, Bundle ID, application, account, and current Edge session.
- Existing automated sub-stage entry:

  ```bash
  python3 "$PROJECT_ROOT/scripts/utm_22_upload.py" --run-id '<run-id>'
  ```

  Standalone use may replace the selector with `--target '<app-name>-<vm-name>'`; `--archive-path '<exact-archive>'` is allowed only with that standalone selector.

`utm_22_upload.py` covers only exact Archive discovery, validation, IPA preparation, and App Store Connect API upload. It does not open Xcode, configure signing, click Archive, or complete the full skill. The skill body owns those GUI stages and must not present the helper as a full wrapper.

## Required workflow

1. Open the exact verified workspace in the unique Xcode instance. Confirm the Runner target, `Any iOS Device (arm64)`, Team, Apple Distribution certificate, App Store profile, Bundle ID, and warning-free signing state.
2. Reconcile marketing version and build number across the authoritative project declaration, generated configuration, Xcode build settings, and GUI. All four must agree before Archive.
3. Create a stable archive attempt ledger. In Xcode select `Product` -> `Archive` exactly once. If the result is unknown, reconcile the same attempt read-only; never click again while a build may have started.
4. Select the unique new Archive matching the same app, Bundle ID, version, build, Team, attempt window, and pre-Archive manifest. Verify its recursively generated manifest and actual app metadata. Never choose merely the newest Archive, modify it, re-sign it, or click Xcode `Distribute App`.
5. Run the automated sub-stage entry only after the new Archive is verified. It must validate signing/profile/framework metadata, create the IPA, bind one upload attempt, and obtain `UTM_22_UPLOAD=verified` with `BUILD_UPLOAD_FINAL_STATE=COMPLETE` and `BUILD_PROCESSING_STATE=VALID` (or reconcile one already-valid identical build). Never create a second upload for an ambiguous attempt.
6. Use the Game Center recovery branch only for Apple's exact missing Game Center entitlement error: increment the authoritative build number once, add the capability/profile in Xcode, create a new attempt and Archive, then upload that new build. No other error authorizes rebuilding.

Retry reversible GUI/signing checks at most three rounds using 0/5/10 seconds. Build processing uses bounded read-only polling and never triggers a duplicate Archive or upload.

## Result and handoff

Only after the GUI Archive and automated upload sub-stage both pass, emit `执行成功：utm-22；UTM_22=verified`. Preserve the same VM, current build, upload identity, and Edge session and immediately hand them to `utm-23`.
