# Project agent contract

## Code discovery

This repository uses `codebase-memory-mcp` as its code knowledge graph. For code discovery, always use this order:

1. `search_graph` — find functions, classes, routes, and variables by pattern.
2. `trace_path` — inspect inbound or outbound call paths.
3. `get_code_snippet` — read the selected function or class.
4. `query_graph` — run a focused Cypher query for relationships the preceding tools cannot answer.
5. `get_architecture` — request a high-level project summary.

Fall back to `rg` only for string literals, error messages, configuration values, non-code files, or when graph results are insufficient. Use `rg --files` rather than filesystem-wide traversal for file discovery.

## Workflow authority

项目内 `skills/` 是 16 个主线技能的唯一正文源。外部 Codex 技能目录只能保留指向本目录的发现 symlink，不得复制第二份技能正文。

- 单个技能的可执行事实以其 [`SKILL.md`](skills/) 为准。
- 共享操作、恢复、授权与安全边界以 [`skills/_shared/AUTOMATION_CONTRACT.md`](skills/_shared/AUTOMATION_CONTRACT.md) 为准。
- [`README.md`](README.md) 只负责介绍与快速启动；[`docs/configuration.md`](docs/configuration.md) 只负责配置；[`docs/project-workflow.md`](docs/project-workflow.md) 只负责阶段与链接；[`docs/utm-feishu-bot.md`](docs/utm-feishu-bot.md) 只负责飞书部署与运行。
- 文档与 `SKILL.md` 冲突时，执行细节以 `SKILL.md` 为准，共享安全约束以 `AUTOMATION_CONTRACT.md` 为准；不得在 `docs/` 重建技能 runbook。

## Exact mainline

1. 虚拟机准备：utm-vm-clone
2. 环境准备：utm-notion → utm-clash-ip → utm-login → utm-edit → utm-key → utm-apps → utm-business
3. 脚本准备、执行：utm-env → utm-script → utm-image → utm-p8
4. 代码处理：utm-21 → utm-22 → utm-23 → utm-24

- Current skills in exact order: `utm-vm-clone`, `utm-notion`, `utm-clash-ip`, `utm-login`, `utm-edit`, `utm-key`, `utm-apps`, `utm-business`, `utm-env`, `utm-script`, `utm-image`, `utm-p8`, `utm-21`, `utm-22`, `utm-23`, `utm-24`.
- 四段均可单独运行，每个技能也可人工单独指定执行。只执行用户指定的范围；范围内保持上述顺序。
- 每段结束时可停止；需继续时才交接下一段入口。单技能失败并完成修复后，重新运行该技能的唯一宿主入口。
- 连续交接必须保持同一精确 run、应用、VM、网络身份、SSH 身份、浏览器会话和工作目录；禁止按“最新”模糊重选。

## API routing

- Feishu 硬规则：消息、图片、Base 与 Wiki 数据只通过 Feishu Bot/OpenAPI 和项目封装访问；不读飞书桌面客户端，不用浏览器、坐标、剪贴板或页面 DOM 抓取数据。Wiki URL 路由和 Base 三元组路由必须二选一且不得混用。
- Notion API 硬规则：`utm-notion`、`utm-login`、`utm-edit`、`utm-key`、`utm-apps`、`utm-business`、`utm-env`、`utm-script`、`utm-image`、`utm-p8`、`utm-21`、`utm-24` 所有 Notion 读写都必须通过项目 `scripts/notion_api.py` 或其内部直接封装；先验证精确父页/目标页，再字段级读写并独立回读。不得用宿主 Chrome、Notion 插件、CUA、坐标或浏览器剪贴板读写 Notion。
- 只有 Feishu OpenAPI 接口可读取 Feishu 资源；只有 Notion API 接口可读写 Notion 资源。两者的 token、key、ID 和 URL 均由当前宿主本地配置提供。

## Public and safety boundary

- 不读取、不提交、不输出根 `.env`、`runtime/`、签名私钥/证书/profile、VM 包/镜像/配置、真实资源 ID、真实主机路径或密码。不在报错、日志、卡片、diff 或聊天中回显它们。
- 只允许读取无值的 `.env.example`。每台宿主自行填写 token、key、ID、path 和 password；禁止复制其他宿主的配置或运行历史。
- 保留工作树内所有既有 dirty 变更。不 checkout、reset、clean、stash、覆盖或改写与当前任务无关的文件。
- 删除、覆盖、重命名、提交、推送、发布、启停 VM、改系统/网络/浏览器/签名配置等有状态或破坏性动作，只能在用户对精确对象与范围明确授权后执行。授权不得从“继续”“完成”或其他模糊用语扩张。
- 正常运行自动继续；只在自动诊断、实际修复和独立复验恢复穷尽后才发送最后故障卡，具体门禁以共享合同为准。
