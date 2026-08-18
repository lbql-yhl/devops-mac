---
name: utm-image
description: Use when the executed application stage needs its exact submission image assets prepared and verified.
---

# utm-image

Read and obey [`../_shared/AUTOMATION_CONTRACT.md`](../_shared/AUTOMATION_CONTRACT.md). From the repository root run `eval "$(python3 scripts/preflight.py --project-only --emit-shell)"` before the entry command.

## Contract

- Segment: 3, script preparation and execution.
- Input: exact `<run-id>` or `<page-title>`, plus `<vm-name>`, inherited `<vm-ip>`, `<vm-user>`, and optional distinct `<asset-app-name>`.
- Only public host entry:

  ```bash
  python3 "$PROJECT_ROOT/scripts/utm_image.py" \
    --run-id '<run-id>' --vm-name '<vm-name>' --vm-ip '<vm-ip>' --vm-user '<vm-user>'
  ```

  Standalone selector: replace `--run-id '<run-id>'` with `--page-title '<page-title>'`. Add `--asset-app-name '<asset-app-name>'` only for an explicitly different source.
- Require the exact application/source binding and independently verify the expected image set and guest delivery. Do not print source credentials or unrelated asset contents.
- Retry the same source/file checkpoint at most three rounds using 0/5/10 seconds; never substitute another application's images.

## Result and handoff

The canonical success summary is `执行成功：utm-image；UTM_19=verified`. Immediately hand the same run, VM, application, source binding, and verified assets to `utm-p8`.
