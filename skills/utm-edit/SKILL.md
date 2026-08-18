---
name: utm-edit
description: Use when the signed-in UTM guest needs the current Apple Account password-change stage completed for its exact application.
---

# utm-edit

Read and obey [`../_shared/AUTOMATION_CONTRACT.md`](../_shared/AUTOMATION_CONTRACT.md). From the repository root run `eval "$(python3 scripts/preflight.py --project-only --emit-shell)"` before the entry command.

## Contract

- Segment: 2, environment preparation.
- Input: exact `<page-title>`, `<vm-name>`, inherited `<vm-ip>`, and `<vm-user>`.
- Only public host entry:

  ```bash
  python3 "$PROJECT_ROOT/scripts/utm_8_change_password.py" \
    --page-title '<page-title>' --vm-name '<vm-name>' \
    --vm-ip '<vm-ip>' --vm-user '<vm-user>'
  ```

- Keep old/new passwords and recovery data inside the secured entry. Verify the changed account state and authoritative page update without printing a secret.
- Retry the same account checkpoint at most three rounds using 0/5/10 seconds. Never change a different account or manufacture recovery answers.

## Result and handoff

The canonical success summary is `执行成功：utm-edit；UTM_8=verified`. Preserve the same account, VM, and session and immediately hand them to `utm-key`.
