---
name: utm-login
description: Use when the proxy-ready UTM guest must sign in to the exact Apple Account recorded for its application page.
---

# utm-login

Read and obey [`../_shared/AUTOMATION_CONTRACT.md`](../_shared/AUTOMATION_CONTRACT.md). From the repository root run `eval "$(python3 scripts/preflight.py --project-only --emit-shell)"` before the entry command.

## Contract

- Segment: 2, environment preparation.
- Input: exact Notion `<parent-title>` and `<page-title>`, inherited `<vm-ip>`, and `<vm-user>`.
- Only public host entry:

  ```bash
  python3 "$PROJECT_ROOT/scripts/utm_7_login.py" \
    --parent-title '<parent-title>' --page-title '<page-title>' \
    --vm-ip '<vm-ip>' --vm-user '<vm-user>'
  ```

- Use only the unique account, phone, and current OTP resolved by the entry. Never log or pass credentials in argv.
- Retry the same account/session checkpoint at most three rounds using 0/5/10 seconds. CAPTCHA, lockout, ambiguous phone/OTP, or unknown security challenges remain external blockers.

## Result and handoff

The canonical success summary is `执行成功：utm-login；UTM_7=verified`. Preserve the verified Apple/Edge session and immediately hand it to `utm-edit`.
