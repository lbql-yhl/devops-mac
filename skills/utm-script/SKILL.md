---
name: utm-script
description: Use when the prepared guest environment is ready to execute the application's fixed automation stage.
---

# utm-script

Read and obey [`../_shared/AUTOMATION_CONTRACT.md`](../_shared/AUTOMATION_CONTRACT.md). From the repository root run `eval "$(python3 scripts/preflight.py --project-only --emit-shell)"` before the host entry.

## VM

The following browser preparation is allowed only before UTM-10/login has ever succeeded. If login or the Edge/CDP identity is already verified, preserve that exact process and session and skip this block.

```bash
pkill "Microsoft Edge"

nohup "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" \
  --remote-debugging-port=9222 \
  --user-data-dir=/tmp/edge-debug-profile \
  --no-first-run \
  --start-maximized \
  >/dev/null 2>&1 &
```

## Contract

- Segment: 3, script preparation and execution.
- Input: exact `<run-id>` or exact `<page-title>`, plus `<vm-name>`, inherited `<vm-ip>`, `<vm-user>`, and the existing Edge/CDP session.
- Only public host entry:

  ```bash
  python3 "$PROJECT_ROOT/scripts/utm_18.py" \
    --run-id '<run-id>' --vm-name '<vm-name>' --vm-ip '<vm-ip>' --vm-user '<vm-user>'
  ```

  Standalone selector: replace `--run-id '<run-id>'` with `--page-title '<page-title>'`.
- Do not call its guest scripts or attempt helpers directly. Preserve the current attempt ledger and browser identity.
- Retry the same failed checkpoint at most three rounds using 0/5/10 seconds. Once login is verified, never run the VM restart block during recovery.

## Result and handoff

The canonical success summary is `执行成功：utm-script；UTM_18=verified`. Immediately hand the same run, VM, application, attempt, and Edge/CDP session to `utm-image`.
