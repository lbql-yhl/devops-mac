---
name: utm-env
description: Use when the prepared UTM guest needs the current application's submission environment and assets materialized.
---

# utm-env

Read and obey [`../_shared/AUTOMATION_CONTRACT.md`](../_shared/AUTOMATION_CONTRACT.md). From the repository root run `eval "$(python3 scripts/preflight.py --project-only --emit-shell)"` before the entry command.

## Contract

- Segment: 3, script preparation and execution; this is the segment entry.
- Input: exact `<parent-title>`, `<app-name>`, optional distinct `<asset-app-name>`, inherited `<vm-ip>`, and `<vm-user>`.
- Only public host entry:

  ```bash
  python3 "$PROJECT_ROOT/scripts/utm_env.py" \
    --parent-title '<parent-title>' --app-name '<app-name>' \
    --vm-ip '<vm-ip>' --vm-user '<vm-user>'
  ```

  Add `--asset-app-name '<asset-app-name>'` only when the Feishu asset source differs from the application name.
- Require unique source rows and application identity; verify the guest environment and copied assets without printing `.env` values or asset credentials.
- Retry the same source/guest checkpoint at most three rounds using 0/5/10 seconds. Do not switch source application unless the inherited input explicitly names it.

## Result and handoff

The canonical success summary is `执行成功：utm-env；UTM_ENV=verified`. Immediately hand the same run, VM, application, asset source, and browser session to `utm-script`.
