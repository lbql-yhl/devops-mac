# 项目流程与 Agent 交接

`devops-mac` v3.0 将飞书机器人作为主控 Agent，统一调度专家 Agent；专家 Agent 只通过项目内 16 个 Skills 的唯一入口执行固定流程。本文描述阶段、映射和交接协议，不复制任何 Skill 的操作 runbook。

> 图片中有时写作 utm-clash；当前仓库真实技能目录和主线名称是 `utm-clash-ip`，本文以仓库源代码为准，不新增虚构技能目录。

## 精确四段主线

1. 虚拟机准备：utm-vm-clone
2. 环境准备：utm-notion → utm-clash-ip → utm-login → utm-edit → utm-key → utm-apps → utm-business
3. 脚本准备、执行：utm-env → utm-script → utm-image → utm-p8
4. 代码处理：utm-21 → utm-22 → utm-23 → utm-24

四段均可单独运行，每个技能可人工单独指定执行；只执行用户指定范围，范围内保持上述顺序。

## Agent 与 Skill 映射

| Agent | 职责 | 绑定 Skills | 提示词 |
| --- | --- | --- | --- |
| 飞书主控 Agent | 接收请求、编排、汇总、通知 | 无 | [feishu-orchestrator](agent-prompts/feishu-orchestrator.md) |
| 提审登记与资料专家 Agent | 校验提审资料与精确 run 上下文 | `utm-notion` | [submission-registration](agent-prompts/submission-registration.md) |
| 虚拟机准备专家 Agent | 准备并验证 UTM guest | `utm-vm-clone` | [vm-preparation](agent-prompts/vm-preparation.md) |
| 环境准备专家 Agent | 代理、登录、编辑、密钥、应用、业务环境 | `utm-clash-ip`, `utm-login`, `utm-edit`, `utm-key`, `utm-apps`, `utm-business` | [environment-preparation](agent-prompts/environment-preparation.md) |
| 脚本准备与执行专家 Agent | 环境和固定脚本执行 | `utm-env`, `utm-script` | [script-execution](agent-prompts/script-execution.md) |
| 生图专家 Agent | 生产 app 截图、金币图、研发截图 | `utm-image` | [visual-assets](agent-prompts/visual-assets.md) |
| P8 与签名材料专家 Agent | P8 与提审材料安全准备 | `utm-p8` | [p8-materials](agent-prompts/p8-materials.md) |
| App 应用 A 面代码专家 Agent | A 面代码处理与前置校验 | `utm-21` | [app-a-code](agent-prompts/app-a-code.md) |
| 构建与分发专家 Agent | 归档、分发、上传与处理校验 | `utm-22` | [build-distribution](agent-prompts/build-distribution.md) |
| 审核提交流程专家 Agent | 审核资料准备，不点击最终提交 | `utm-23` | [review-preparation](agent-prompts/review-preparation.md) |
| 最终校验专家 Agent | 最终状态核验与报告 | `utm-24` | [final-validation](agent-prompts/final-validation.md) |
| 故障专家 Agent | 测试环境修复、回归、人工兜底 | 无（按失败技能回归） | [fault-recovery](agent-prompts/fault-recovery.md) |

完整可机器读取的注册表见 [`config/agents.json`](../config/agents.json)。

## 结构化交接格式

每个 Agent 必须返回以下字段，不得只返回自然语言“完成”：

```json
{
  "status": "success | retryable_failure | blocked | needs_human",
  "run_id": "当前精确运行标识",
  "agent_id": "当前 Agent",
  "skill_id": "当前 Skill 或 null",
  "evidence": ["脱敏证据索引"],
  "artifacts": ["产物元数据或安全路径索引"],
  "next_step": "下一入口、重试或人工动作"
}
```

连续交接必须保留同一精确 run、应用、VM、网络/SSH 身份、浏览器会话、版本/构建和工作目录。禁止按“最新”模糊重选。

## 失败处理闭环

1. **重试**：主控判断瞬态故障，按注册表中的限定次数重试原 Skill 唯一入口。
2. **自愈**：重试仍失败，故障专家在隔离测试环境中诊断并有限修复脚本或 Skill。
3. **回归**：修复后从原失败入口重跑，验证同一 run 上下文和相关阶段契约。
4. **人工**：超过修复次数、涉及敏感权限或无法形成可复核证据时，输出脱敏故障卡和人工动作清单。

## 执行边界

- 单技能执行以 `skills/<skill>/SKILL.md` 为唯一可执行真值；先遵守 [共享自动化合同](../skills/_shared/AUTOMATION_CONTRACT.md)。
- Notion 只通过 `scripts/notion_api.py` 或项目内部封装读写；Feishu 只通过 Bot/OpenAPI 访问。
- 文档只做索引，不读取或解释脚本内容；配置见 [配置参考](configuration.md)，部署见 [Feishu Bot 指南](utm-feishu-bot.md)。
- 当前公开版本保留 Feishu 提审启动禁用状态；v3.0 的编排协议和提示词先作为可审计设计资产落库。
