---
name: utm-key
description: Use when the password-updated UTM guest needs Apple Developer keys, identifiers, certificates, and profiles prepared.
---

# utm-key

Read and obey [`../_shared/AUTOMATION_CONTRACT.md`](../_shared/AUTOMATION_CONTRACT.md). From the repository root run `eval "$(python3 scripts/preflight.py --project-only --emit-shell)"` before the entry command.

## Contract

- Segment: 2, environment preparation.
- Input: exact `<run-id>` or exact `<page-title>`, plus `<vm-name>`, inherited `<vm-ip>`, and `<vm-user>`.
- Only public host entry:

  ```bash
  python3 "$PROJECT_ROOT/scripts/utm_9.py" \
    --run-id '<run-id>' --vm-name '<vm-name>' --vm-ip '<vm-ip>' --vm-user '<vm-user>'
  ```

  Standalone selector: replace `--run-id '<run-id>'` with `--page-title '<page-title>'`.
- Accept only the unique App ID/Bundle ID/account context bound to the inherited run. Do not expose certificate material, P8 data, or passwords.
- Retry the same entry and exact identifier at most three rounds using 0/5/10 seconds; reconcile existing Apple resources before attempting any creation.

## Result and handoff

The canonical success summary is `执行成功：utm-key；UTM_9=verified`. Preserve the same browser login and identifier context and immediately hand them to `utm-apps`.
