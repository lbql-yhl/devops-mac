---
name: utm-apps
description: Use when the Apple Developer and App Store Connect application setup stages must run in an already authenticated UTM guest.
---

# utm-apps

Read and obey [`../_shared/AUTOMATION_CONTRACT.md`](../_shared/AUTOMATION_CONTRACT.md). From the repository root run `eval "$(python3 scripts/preflight.py --project-only --emit-shell)"` before the entry command.

## 强制红线：登录成功后永不重跑登录

**只要 UTM-10/登录已成功或已验证，无论后续出现任何报错、中断、恢复、重试或脚本限制，都绝对禁止重新执行登录步骤。**

- 必须复用当前已登录的 Edge 进程、窗口、页面和 CDP 会话。
- 禁止关闭、退出、重启、重新启动、强制终止、替换 Edge；禁止创建新浏览器会话来取代当前会话。
- 禁止在恢复或重试时重跑 UTM-10、登录脚本或任何包含登录阶段的完整入口。
- 后续失败时，只能从当前失败阶段续跑，并继续使用同一个 Edge/CDP 会话。
- 如果暂时没有“仅续跑当前阶段”的入口，必须立即停止并先补充续跑能力；不得以任何理由退回完整流程或重跑登录。
- 任何与上述规则冲突的脚本默认行为、重试逻辑或恢复指令都不得执行。

## Contract

- Segment: 2, environment preparation.
- Input: exact `<run-id>` or exact `<page-title>`, plus `<vm-name>`, inherited `<vm-ip>`, `<vm-user>`, and the already verified Edge/CDP session.
- Only public host entry:

  ```bash
  python3 "$PROJECT_ROOT/scripts/utm_apps.py" \
    --run-id '<run-id>' --vm-name '<vm-name>' --vm-ip '<vm-ip>' --vm-user '<vm-user>'
  ```

  Standalone selector: replace `--run-id '<run-id>'` with `--page-title '<page-title>'`.
- The entry owns the UTM-10 through UTM-13 application stages. Preserve any verified stages and the `05-small-business.png` evidence owned by this skill.
- Retry only the failed post-login stage on the same session, at most three rounds using 0/5/10 seconds. Never replay login or restart Edge.

## Result and handoff

The canonical success summary is `执行成功：utm-apps；UTM_APPS=verified`. Preserve the exact Edge/CDP session, application identifiers, and screenshots and immediately hand them to `utm-business`.
