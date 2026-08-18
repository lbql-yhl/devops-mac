---
name: utm-p8
description: Use when the current application needs its App Store Connect API key configuration finalized in the prepared UTM guest.
---

# utm-p8

Read and obey [`../_shared/AUTOMATION_CONTRACT.md`](../_shared/AUTOMATION_CONTRACT.md). From the repository root run `eval "$(python3 scripts/preflight.py --project-only --emit-shell)"` before the entry command.

## Contract

- Segment: 3, script preparation and execution; this is the segment tail.
- Input: exact `<run-id>` or `<page-title>`, plus `<vm-name>`, inherited `<vm-ip>`, and `<vm-user>`.
- Only public host entry:

  ```bash
  python3 "$PROJECT_ROOT/scripts/utm_p8.py" \
    --run-id '<run-id>' --vm-name '<vm-name>' --vm-ip '<vm-ip>' --vm-user '<vm-user>'
  ```

  Standalone selector: replace `--run-id '<run-id>'` with `--page-title '<page-title>'`. For explicitly requested local cleanup only, add `--cleanup-only` and omit `--vm-ip`/`--vm-user`.
- Require one matching key ID, issuer, App ID, and non-empty P8 file; never print the key or token. Normal completion also verifies cleanup of only the current application's four shared temporary asset targets.
- Retry the same key/configuration checkpoint at most three rounds using 0/5/10 seconds. Do not choose a different P8 or delete unrelated shared content.

## Result and handoff

Normal success is `执行成功：utm-p8；UTM_P8=verified`; cleanup-only success is `执行成功：utm-p8；SHARED_APP_ASSETS_CLEANED=verified`. This ends segment 3. If the request includes segment 4, hand the same verified context to `utm-21`; otherwise stop at the segment boundary.
