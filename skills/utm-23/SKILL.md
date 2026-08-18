---
name: utm-23
description: Use when utm-22 has uploaded a valid build and the existing App Store Connect session must prepare its review draft.
---

# utm-23

Read and obey [`../_shared/AUTOMATION_CONTRACT.md`](../_shared/AUTOMATION_CONTRACT.md). From the repository root run `eval "$(python3 scripts/preflight.py --project-only --emit-shell)"` before the entry helper.

## Contract

- Segment: 4, code processing.
- Input: `UTM_22=verified`, `BUILD_UPLOAD_FINAL_STATE=COMPLETE`, `BUILD_PROCESSING_STATE=VALID`, exact run/chat/app/App ID/Bundle ID/version/build/VM, and the already authenticated Edge/CDP session.
- Only browser-session entry helper:

  ```bash
  node "$PROJECT_ROOT/scripts/session.mjs" inspect
  ```

`session.mjs` only attaches to and inspects the existing browser session. There is no complete `utm-23` wrapper. The skill body must perform and verify the App Store Connect GUI workflow below without starting or restarting Edge.

## Required workflow

1. Bind the exact current run, existing Edge PID/CDP identity, App Store Connect application, version, and build. Reuse an existing approved tab when possible; do not log in again or choose a newest run/build.
2. Classify the live preparation state and resume from the first incomplete item in this fixed order: attach the exact build, clear Missing Compliance with `None of the algorithms mentioned above`, leave Game Center unchecked, save the version, maintain exactly one existing Draft Submission, link 14 IAP items, link the App Version, capture screenshot 02, clear the two scoped App Information areas, then capture screenshot 03.
3. If `Add Build` is temporarily absent, poll page/API read-only at 15/30/60/120 seconds. Never rebuild, re-Archive, repackage, or re-upload to probe visibility.
4. The single draft must contain the current App Version and exactly 14 IAP items, all `Ready for Review`. Expand with `See More`; add only missing items to that existing draft. Never select `Create New Submission`.
5. Save `$PROJECT_ROOT/runtime/review-screenshots/<run-id>/02-iap-drafts.png` only after all 14 items and the single draft are visible. Save `03-app-information.png` only on the matching App Information page with the current Ready for Review version visible. Files must be mode 600, valid non-empty PNGs, and have recorded SHA-256 values.
6. Clear only actual configured records in `App Store Regulations & Permits` and actual Production/Sandbox Server URLs. Placeholder actions such as `Get Started`, `Add`, `Declare Regulated Medical Device`, and `Set Up URL` mean empty and must not be clicked. Do not touch the app-specific shared secret.
7. Re-read the build, compliance, unchecked Game Center, saved version, one draft, 14 IAPs, linked App Version, empty scoped App Information areas, screenshots, run, VM, and browser session. This skill must not click `Submit for Review`.

For reversible GUI failures, return to the last verified page and retry the exact action at most three rounds using 0/5/10 seconds. Discard stale coordinates each round. Multiple drafts, ambiguous ownership, or missing unique destructive controls get three read-only checks and then stop.

## Result and handoff

The canonical success summary is `执行成功：utm-23；UTM_23=verified`. It also requires `REVIEW_SCREENSHOT_02=verified`, `REVIEW_SCREENSHOT_03=verified`, `DRAFT_SUBMISSIONS=1`, `IAP_READY_FOR_REVIEW=14`, and `SUBMIT_FOR_REVIEW=not_clicked`. Keep the Ready for Review tab and exact Edge/CDP session open and immediately hand them to `utm-24`.
