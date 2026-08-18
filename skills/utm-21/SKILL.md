---
name: utm-21
description: Use when utm-p8 has finished and the same UTM guest needs its Codeup project prepared for Xcode.
---

# utm-21

Read and obey [`../_shared/AUTOMATION_CONTRACT.md`](../_shared/AUTOMATION_CONTRACT.md). From the repository root run `eval "$(python3 scripts/preflight.py --project-only --emit-shell)"` before the entry command.

## Contract

- Segment: 4, code processing; this is the segment entry.
- Input: `UTM_P8=verified`, exact `run_id`, original `chat_id`, host title, application, four-letter VM name, inherited VM IP/user, unique Notion page, App ID, Bundle ID, and the same guest/browser session.
- Existing automated sub-stage entry:

  ```bash
  python3 "$PROJECT_ROOT/scripts/utm_21_clone.py" \
    --run-id '<run-id>' --vm-name '<vm-name>' --vm-ip '<vm-ip>' \
    --parent-title '<parent-title>' --page-title '<app-name>-<vm-name>'
  ```

`utm_21_clone.py` covers only the safe Codeup clone sub-stage. It does not implement the full skill, identifier replacement, workspace verification, or any remaining Notion/guest GUI work. The skill body owns those steps and must not present the helper as a complete wrapper.

## Required workflow

1. Re-read the unique Notion page for the credential-free Codeup HTTPS URL, numeric App ID, and production Bundle ID. Keep credentials and identifiers out of logs and argv.
2. Classify the exact guest repository target before cloning. An absent target may use the entry helper. A pristine matching repository or a same-run partial replacement resumes after read-only verification. A different repository, unknown ownership, or unrelated changes must never be deleted, reset, moved, or overwritten.
3. Verify the helper's sub-stage evidence: `UTM_21_RUN_HOST=verified`, `UTM_21_CODEUP_CREDENTIAL_CHANNEL=stdin_memory_only`, and `UTM_21_CLONE_EXIT=0`. Credentials must come from the secure host snapshot and exist only in memory/stdin.
4. In Git-tracked regular text files only, replace the four established placeholders case-insensitively with the current App ID/Bundle ID. Use an atomic before/after ledger, prevent cascading replacements, preserve modes and the directory tree, and roll back on incomplete verification. If no placeholder remains, accept only independently verified final declarations.
5. Require `main` tracking the verified credential-free origin, expected tracked-file modifications only, no untracked/renamed/deleted paths, and `git diff --check` success. Do not install dependencies, commit, push, publish, or modify Notion.
6. Verify the existing `ios/Runner.xcworkspace` is unique, owned by the guest user, non-symlinked, non-empty, and contained in the same repository. Do not generate or repair a workspace.

Retry only the exact failed reversible checkpoint at most three rounds using 0/5/10 seconds. Repository ownership conflicts and ambiguous replacement declarations get three read-only checks, then stop without mutation.

## Result and handoff

Only after the clone sub-stage and all skill-owned checks pass, emit `执行成功：utm-21；UTM_21=verified`. Pass the same VM/IP/user, repository, verified workspace, identifiers, and live session immediately to `utm-22`.
