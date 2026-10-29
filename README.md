# macOS 提审自动化 · v3.0

![Version](https://img.shields.io/badge/version-3.0-2563eb) ![Skills](https://img.shields.io/badge/mainline%20skills-16-16a34a)

这是从 macOS 宿主协调 UTM macOS guest、Feishu OpenAPI、Notion API 与 App Store Connect 的提审自动化项目。v3.0 以**飞书机器人作为主控 Agent**，统一调度多个专家 Agent；固定流程由项目内 16 个 Skills（含 Playwright 自动化能力）封装，按需调用并保留可复核证据。

> 提审执行使用 LangGraph 管理计划校验与运行生命周期，LangChain/OpenAI API 校验固定阶段顺序，Codex CLI 继续执行现有 Skills。仅当模型 Key、Feishu 应用凭据、授权会话和宿主身份齐备时，完整登记消息才会启动运行。

## 技术栈

| 领域 | 技术 |
| --- | --- |
| Agent 编排 | LangGraph（计划校验、执行状态、失败转人工） |
| LLM 与提示词 | LangChain OpenAI、ChatPromptTemplate、OpenAI API Key |
| Skills 执行器 | Codex CLI、项目内 16 个 Skills、Python、JavaScript、Playwright |
| 系统集成 | Feishu Bot/OpenAPI、Notion API、App Store Connect API、UTM |
| macOS 环境 | UTM macOS guest、SSH、分阶段执行与证据交接 |

## 架构图

```mermaid
flowchart LR
 U[用户/飞书群]-->F[Feishu Bot]-->G[LangGraph 运行编排]
 G-->P[LangChain/OpenAI 固定计划校验]
 P-->C[Codex CLI 执行器]-->E[专家 Agent / 16 个 Skills]
 E-->K[Playwright 与项目脚本]-->H[macOS / UTM / Notion / ASC]
 G-->N[运行状态与结果通知]
 C-.失败或阶段结果.->G
 G-.无法安全恢复.->X[人工兜底]
```

LangGraph 校验固定阶段计划、执行 Codex runner 并记录状态；LangChain/OpenAI 只接收应用名和固定阶段/Skill ID，不接收原始登记消息或凭据。Codex CLI 继续逐段执行现有 Skills，Skills 仍是唯一操作正文源；遇到无法安全恢复的状态停止并转人工。

## 提审流程图

```mermaid
flowchart TD
 A[飞书提审请求]-->B[主控校验 run/应用/版本/构建]
 B-->C[1 虚拟机准备：utm-vm-clone]
 C-->D[2 环境准备：utm-notion → utm-clash-ip → utm-login → utm-edit → utm-key → utm-apps → utm-business]
 D-->E[3 脚本准备、执行：utm-env → utm-script → utm-image → utm-p8]
 E-->G[4 代码处理：utm-21 → utm-22 → utm-23 → utm-24]
 G-->Q[最终校验与飞书通知]
 B-.信息不完整.->M[人工补充]-.->B
```

## 故障闭环图

```mermaid
flowchart LR
 F[Skill/Agent 失败]-->C[分类并记录脱敏证据]-->R{限定次数可重试?}
 R--是-->S[重跑原 Skill 唯一入口]-->O{恢复?}
 O--是-->N[继续交接/通知]
 O--否-->R
 R--否-->X[故障专家：隔离测试环境修复]-->V[限定次数回归]
 V-->P{通过?}
 P--是-->N
 P--否/超限-->H[人工兜底：故障卡+动作清单]
```

## 四段顺序与技能

1. 虚拟机准备：utm-vm-clone
2. 环境准备：utm-notion → utm-clash-ip → utm-login → utm-edit → utm-key → utm-apps → utm-business
3. 脚本准备、执行：utm-env → utm-script → utm-image → utm-p8
4. 代码处理：utm-21 → utm-22 → utm-23 → utm-24

每段可单独运行，每个技能可人工单独指定执行；只运行用户指定范围，范围内保持精确顺序。图示存在旧名称时，执行仍以真实技能目录 **`utm-clash-ip`** 为准。

完整技能链接：[`utm-vm-clone`](skills/utm-vm-clone/SKILL.md)、[`utm-notion`](skills/utm-notion/SKILL.md)、[`utm-clash-ip`](skills/utm-clash-ip/SKILL.md)、[`utm-login`](skills/utm-login/SKILL.md)、[`utm-edit`](skills/utm-edit/SKILL.md)、[`utm-key`](skills/utm-key/SKILL.md)、[`utm-apps`](skills/utm-apps/SKILL.md)、[`utm-business`](skills/utm-business/SKILL.md)、[`utm-env`](skills/utm-env/SKILL.md)、[`utm-script`](skills/utm-script/SKILL.md)、[`utm-image`](skills/utm-image/SKILL.md)、[`utm-p8`](skills/utm-p8/SKILL.md)、[`utm-21`](skills/utm-21/SKILL.md)、[`utm-22`](skills/utm-22/SKILL.md)、[`utm-23`](skills/utm-23/SKILL.md)、[`utm-24`](skills/utm-24/SKILL.md)。

## Agent 注册表与提示词

机器可读注册表：[`config/agents.json`](config/agents.json)；提示词索引：[`docs/agent-prompts/README.md`](docs/agent-prompts/README.md)。每个 Agent 都有可复制提示词，包含角色、输入输出、权限边界和交接要求；包括飞书主控、提审登记、VM、环境、脚本、生图、P8、App 应用 A 面代码、构建分发、审核准备、最终校验、故障专家。

交接字段固定为：`status / run_id / agent_id / skill_id / evidence / artifacts / next_step`。故障策略固定为：重试失败后进入隔离测试环境修复，修复后回归；超出次数或涉及敏感权限时人工兜底。

## 快速启动

```bash
git clone https://github.com/lbql-yhl/devops-mac.git && cd devops-mac
python3 -m pip install -r requirements-agent.txt
cp .env.example .env && chmod 600 .env && git check-ignore -q .env
python3 scripts/preflight.py --project-only
/bin/zsh scripts/install_project_skills.sh --install
python3 -u services/feishu_supervisor.py
```

按 [配置参考](docs/configuration.md) 填写当前宿主自己的 token、key、ID、path 和 password；不要复制其他机器的 `.env`。宿主/guest 依赖见 [依赖检查](docs/host-vm-dependency-check.md)，Feishu 部署见 [Bot 指南](docs/utm-feishu-bot.md)。完整登记消息只会在模型 Key、Feishu 应用凭据、授权会话和宿主身份齐备后创建 run 并启动 LangGraph runner。

## 执行边界与安全

单技能执行以 `skills/<skill>/SKILL.md` 为唯一真值；文档只做索引，不读取或解释脚本内容。Feishu 只通过 Bot/OpenAPI，Notion 只通过 Notion API 和 `scripts/notion_api.py`。严禁提交或输出 `.env`、runtime、签名私钥/证书/profile、VM 包/镜像/配置、真实资源 ID、主机绝对路径或密码。

## 维护检查

```bash
python3 -m pytest -q
python3 -m pytest -q tests/test_v3_agent_registry.py
python3 scripts/public_release_audit.py
git diff --check
```

更多阶段与交接说明见 [项目流程](docs/project-workflow.md)。
