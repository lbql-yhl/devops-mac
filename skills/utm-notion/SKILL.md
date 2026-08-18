---
name: utm-notion
description: Use when an initialized UTM guest needs its unique application page prepared or verified in Notion.
---

# utm-notion

Read and obey [`../_shared/AUTOMATION_CONTRACT.md`](../_shared/AUTOMATION_CONTRACT.md). From the repository root run `eval "$(python3 scripts/preflight.py --project-only --emit-shell)"` before the entry command.

## Contract

- Segment: 2, environment preparation; this is the segment entry.
- Input: either one exact `<run-id>`, or the exact `<app-name>` plus four-letter `<vm-name>` for a standalone invocation. The selectors are mutually exclusive.
- Only public host entry:

  ```bash
  python3 "$PROJECT_ROOT/scripts/utm_notion.py" --run-id '<run-id>'
  ```

  Standalone selector: `python3 "$PROJECT_ROOT/scripts/utm_notion.py" --app-name '<app-name>' --vm-name '<vm-name>'`.
- Require one matching parent and one `<app-name>-<vm-name>` page. Re-read all written fields and preserve unrelated page content.
- Retry the same entry and identity at most three rounds using 0/5/10 seconds; never select a nearby or newest page.

## Result and handoff

The canonical success summary is `执行成功：utm-notion；status=verified`. Immediately hand the same run, application, VM, and page identity to `utm-clash-ip`.
