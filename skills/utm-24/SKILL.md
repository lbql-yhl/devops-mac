---
name: utm-24
description: Use when utm-23 has verified the review draft and the same App Store Connect session is ready for final submission.
---

# utm-24

Read and obey [`../_shared/AUTOMATION_CONTRACT.md`](../_shared/AUTOMATION_CONTRACT.md). From the repository root run `eval "$(python3 scripts/preflight.py --project-only --emit-shell)"` before the entry helper.

## Contract

- Segment: 4, code processing; this is the segment and workflow tail.
- Input: `UTM_23=verified`, exact run/chat/app/App ID/version/build/VM, the unchanged Ready for Review tab and Edge/CDP identity, screenshot 05 from `utm-apps`, and screenshots 02/03 from `utm-23`.
- Only browser-session entry helper:

  ```bash
  node "$PROJECT_ROOT/scripts/session.mjs" inspect
  ```

`session.mjs` only attaches to and inspects the existing browser session. There is no complete `utm-24` wrapper. The skill body owns screenshot capture, self-check authorization, the one-time review submission, and expedited-review GUI work. Never start or restart Edge.

## Required workflow

1. Verify inherited screenshots 02, 03, and 05 belong to the same run, are mode 600 valid PNGs, and match their recorded SHA-256. A missing inherited image returns only to its owning skill; `utm-24` must not recreate it.
2. In the same Edge session, capture only `$PROJECT_ROOT/runtime/review-screenshots/<run-id>/01-media-manager.png` from the current app/version Media Manager and `04-privacy-agreement.png` from the exact current privacy URL. Verify both as mode 600 valid PNGs with recorded hashes. Do not overwrite 02, 03, or 05.
3. Recheck all five images in fixed 01-05 order, the exact Ready for Review version/build, one Draft Submission, `Items Ready to Submit (15)`, the current App Version, and 14 IAP items.
4. Use `python3 "$PROJECT_ROOT/services/feishu_bot.py" record-auto-review-approval` to bind the five current hashes and `REVIEW_SCREENSHOTS=verified_5;ITEMS_READY=15` to the current run. This is an automatic self-check record, not an interactive approval card.
5. Create or reuse one stable review-submit attempt with `python3 "$PROJECT_ROOT/services/feishu_bot.py" record-review-submit-attempt`. Re-read the live page, advance the same attempt through `prepared`, `clicking`, `result_unknown`, and `verified`, and click `Submit for Review` at most once. Accept only `15 Items Submitted` or `Waiting for Review` for the same app/version/build. Any ambiguous post-click state is read-only forever for that attempt.
6. Preserve the App Store success tab. In a new tab of the same Edge process, open Apple's expedited-review form. Bind one stable expedite attempt, select the exact application and `iOS`, and click `Send` at most once. Accept only `We’ll expedite review for <current-app>.`; an existing exact success page may be reconciled without another click.

Retry reversible pre-submit GUI failures at most three rounds using 0/5/10 seconds. A disabled submit control may be polled read-only at 5/10/20/40 seconds. After either irreversible click, never repeat it; only reconcile the same attempt.

## Result and endpoint

The canonical success summary is `执行成功：utm-24；UTM_24=verified`. It requires five verified screenshots, automatic approval, a verified one-click review attempt, an explicit App Store success state, and a verified one-click or already-successful expedite attempt. Preserve both success tabs and end the workflow; there is no next skill.
