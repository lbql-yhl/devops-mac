# Portable Automation Contract

This contract applies to all 16 UTM skills. A skill body supplies the business-specific inputs, entry point, evidence, and handoff; this file supplies only shared execution rules.

## Portable bootstrap

Before any skill command, change to the repository root and run exactly:

```bash
eval "$(python3 scripts/preflight.py --project-only --emit-shell)"
```

After bootstrap, resolve every repository file from `$PROJECT_ROOT`. Do not hard-code a host user name, home directory, virtual-environment path, plugin version, VM name, IP address, or credential. Use `python3` for Python entry points.

`--project-only --emit-shell` is the only bootstrap mode used by skills. It must not read host secrets. Never print the root `.env`, secret values, tokens, passwords, private keys, JWTs, authenticated URLs, clipboard contents, or raw internal helper output.

## Immutable run context

Keep the inherited `run_id`, original `chat_id`, host identity, application, VM name/IP/user, App ID, Bundle ID, browser process/session, repository/workspace, version/build, and stable attempt IDs bound to the same run. Never replace them with the newest, first, or closest candidate.

Read sensitive settings only through repository code that securely snapshots the root `.env`. Keep values in process memory, pass them through protected stdin or environment channels, and clear sensitive clipboard contents immediately after use.

## One public host entry

Run only the public host entry or entry helper named in the current skill. Internal modules, guest helpers, browser functions, and sub-stage scripts are implementation details unless the skill explicitly says that the public entry covers only one automated sub-stage.

Each invocation emits only a non-sensitive summary:

```text
开始执行：<skill>
执行成功：<skill>；<verified marker>
```

or:

```text
开始执行：<skill>
执行报错：<skill>；<non-sensitive reason>
```

Do not forward commands, tracebacks, helper logs, identifiers not required by the success marker, or secret-bearing diagnostics.

## Observe, act, verify

For every command or GUI side effect:

1. Re-read the current run, target identity, and live state.
2. Require exactly one enabled target matching the expected page, parent region, text, path, or API identity.
3. Perform one minimum action.
4. Poll the resulting state with the skill's bounded schedule.
5. Re-read fresh evidence and record the skill's marker only when all stated invariants hold.

A click, launched process, closed dialog, zero exit code, old screenshot, or absence of visible error is not sufficient evidence.

## Bounded recovery

Retry only the exact failed checkpoint while preserving the same run and target. Unless a skill defines a stricter schedule, use at most three rounds with delays of 0, 5, and 10 seconds. For each round: diagnose, apply one safe reversible repair, then independently reverify. Do not reset the budget by restarting the skill.

After an interrupted public entry, invoke that same public entry again and revalidate from its first checkpoint. A verified idempotent checkpoint may skip its side effect; an irreversible or result-unknown attempt may only be reconciled by read-only checks and must never be repeated speculatively.

For GUI recovery, discard old coordinates and screenshots, return with `Escape`, `Cancel`, or an explicitly verified tab/page, and locate the unique target again. For browser stages, preserve the inherited Edge PID, profile, CDP session, and successful tabs; never restart a browser after login has been verified.

Only after the bounded budget is exhausted, or three read-only checks prove an external state such as CAPTCHA, account lock, missing authority, ownership conflict, or ambiguous irreversible result, may the workflow use the repository fault-notification service. Fault evidence must be non-sensitive and include the attempt count, recovery actions, and last verified checkpoint.

## Continuous handoff

On success, immediately pass the same verified context and live session to the next skill inside the same segment. Do not wait for ordinary user confirmation. A segment-tail skill stops at the segment boundary unless the active request covers the following segment. `utm-24` is the only final workflow endpoint.

Never hand off a partial, blocked, ambiguous, or result-unknown state.
