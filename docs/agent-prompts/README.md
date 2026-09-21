# Agent 提示词索引

v3.0 的每个 Agent 都有独立提示词，提示词只描述角色、边界、输入输出和交接要求；固定操作仍以对应 Skill 的 `SKILL.md` 为准。

| Agent | 提示词 |
| --- | --- |
| 飞书主控 Agent | [feishu-orchestrator.md](feishu-orchestrator.md) |
| 提审登记与资料专家 Agent | [submission-registration.md](submission-registration.md) |
| 虚拟机准备专家 Agent | [vm-preparation.md](vm-preparation.md) |
| 环境准备专家 Agent | [environment-preparation.md](environment-preparation.md) |
| 脚本准备与执行专家 Agent | [script-execution.md](script-execution.md) |
| 生图专家 Agent | [visual-assets.md](visual-assets.md) |
| P8 与签名材料专家 Agent | [p8-materials.md](p8-materials.md) |
| App 应用 A 面代码专家 Agent | [app-a-code.md](app-a-code.md) |
| 构建与分发专家 Agent | [build-distribution.md](build-distribution.md) |
| 审核提交流程专家 Agent | [review-preparation.md](review-preparation.md) |
| 最终校验专家 Agent | [final-validation.md](final-validation.md) |
| 故障专家 Agent | [fault-recovery.md](fault-recovery.md) |

机器可读注册表见 [`../../config/agents.json`](../../config/agents.json)。
