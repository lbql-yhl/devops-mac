---
name: utm-business
description: Use when the prepared App Store Connect application needs its business and banking setup verified in the same UTM guest.
---

# utm-business

Read and obey [`../_shared/AUTOMATION_CONTRACT.md`](../_shared/AUTOMATION_CONTRACT.md). From the repository root run `eval "$(python3 scripts/preflight.py --project-only --emit-shell)"` before the entry command.

## Contract

- Segment: 2, environment preparation; this is the segment tail.
- Input: exact `<run-id>` or exact `<page-title>`, plus `<vm-name>`, inherited `<vm-ip>`, `<vm-user>`, and the existing authenticated Edge/CDP session.
- Only public host entry:

  ```bash
  python3 "$PROJECT_ROOT/scripts/utm_business.py" \
    --run-id '<run-id>' --vm-name '<vm-name>' --vm-ip '<vm-ip>' --vm-user '<vm-user>'
  ```

  Standalone selector: replace `--run-id '<run-id>'` with `--page-title '<page-title>'`.
- Require one matching business/account context and independently re-read the final banking/business status. Do not start a new login or browser.
- Retry the same failed stage at most three rounds using 0/5/10 seconds while preserving the inherited session.

## Result and handoff

The canonical success summary is `执行成功：utm-business；UTM_BUSINESS=verified`. This ends segment 2. If the request includes segment 3, hand the same run, VM, application, and authenticated browser session to `utm-env`; otherwise stop at the segment boundary.
