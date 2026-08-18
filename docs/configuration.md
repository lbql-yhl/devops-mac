# 配置参考

根目录 `.env.example` 是公开键清单，`.env` 是每台宿主自行创建的唯一私有配置文件。本文只列键名、必需性、来源和用途，不记录任何值。

```bash
cp .env.example .env
chmod 600 .env
git check-ignore -q .env
```

每台宿主必须独立填写自己的 token、key、ID、path 和 password。不得复制其他宿主的 `.env`、运行历史、VM 绑定或浏览器会话。

## 47 个宿主键

“必需”表示相关服务或技能运行前必须配置；“条件”表示仅启用该功能时必须配置；“可选”表示可使用程序的安全默认值或不启用该功能。

### 凭据、宿主身份与资源路由

| 键名 | 必需性 | 来源 | 用途 |
|---|---|---|---|
| `SUBMISSION_GUEST_PASSWORD` | 必需 | 当前 guest 账号 | 通过一次性 SSH askpass broker 进行 guest 认证；不进入子进程环境或日志 |
| `FEISHU_APP_ID` | 必需 | Feishu Open Platform 自建应用 | Bot/OpenAPI 应用身份 |
| `FEISHU_APP_SECRET` | 必需 | Feishu Open Platform 自建应用 | 换取 tenant access token |
| `FEISHU_VERIFICATION_TOKEN` | 条件 | Feishu 事件订阅 | 验证 URL challenge 并传给长连接事件分发器 |
| `FEISHU_ENCRYPT_KEY` | 条件 | Feishu 事件订阅 | 长连接事件安全参数；HTTP 模式另见 [Bot 安全边界](utm-feishu-bot.md#http-回调安全边界) |
| `FEISHU_DAILY_REPORT_CHAT_ID` | 必需 | 当前宿主管理的 Feishu 日报会话 | 唯一日报投递路由，与普通消息/故障卡隔离 |
| `NOTION_TOKEN` | 必需 | Notion integration | Notion API 认证 |
| `NOTION_ROOT_PAGE_ID` | 必需 | 当前宿主的 Notion 工作区 | 限定项目可操作的根页范围 |
| `NOTION_TEMPLATE_TITLE` | 可选 | 当前宿主的 Notion 工作区 | 指定当前根页下的模板标题 |
| `UTM_NOTION_FEISHU_WIKI_URL` | 二选一 | Feishu Wiki/Base 页面 | 从唯一 Wiki URL 解析 Base、表和视图 |
| `UTM_NOTION_FEISHU_APP_TOKEN` | 二选一三元组 | Feishu Base | 直接 Base 路由的 app token |
| `UTM_NOTION_FEISHU_TABLE_ID` | 二选一三元组 | Feishu Base | 直接 Base 路由的 table ID |
| `UTM_NOTION_FEISHU_VIEW_ID` | 二选一三元组 | Feishu Base | 直接 Base 路由的 view ID |
| `SUBMISSION_HOST_MACHINE` | 必需 | 当前宿主的管理员 | 绑定 Feishu run、Notion 父级与本机资源所有权 |
| `OPENAI_API_KEY` | 条件 | 当前宿主的 OpenAI 账号 | 仅在启用 OpenAI assistant provider 时认证 |
| `CODEUP_USERNAME` | 条件 | 当前宿主可用的 Codeup 账号 | `utm-21` 的临时 credential helper 用户名 |
| `CODEUP_PASSWORD` | 条件 | 当前宿主可用的 Codeup 账号 | `utm-21` 的临时 credential helper 密码 |

Feishu 源必须使用下列两种形式之一：

- 只填 `UTM_NOTION_FEISHU_WIKI_URL`；或
- 同时填完 `UTM_NOTION_FEISHU_APP_TOKEN`、`UTM_NOTION_FEISHU_TABLE_ID`、`UTM_NOTION_FEISHU_VIEW_ID`。

不得混用 Wiki URL 与三元组，三元组也不得部分填写。不完整或混用的路由必须在网络请求前失败。

### Feishu Bot、传输与对话

| 键名 | 必需性 | 来源 | 用途 |
|---|---|---|---|
| `FEISHU_BOT_HOST` | 可选 | 当前宿主网络配置 | HTTP 服务监听地址 |
| `FEISHU_BOT_PORT` | 可选 | 当前宿主网络配置 | HTTP 服务端口和 health 端口 |
| `FEISHU_SEND_RETRIES` | 可选 | 运维策略 | Feishu 发送的有界重试次数 |
| `FEISHU_SEND_TIMEOUT_SECONDS` | 可选 | 运维策略 | 单次 Feishu 发送超时 |
| `FEISHU_VERIFY_DELIVERY` | 可选 | 运维策略 | 是否通过 OpenAPI 复验消息送达 |
| `USER_CONFIRM_API_URL` | 条件 | 当前宿主的确认服务 | 需要外部确认时的 API 路由 |
| `USER_CONFIRM_API_TIMEOUT_SECONDS` | 可选 | 运维策略 | 外部确认 API 超时 |
| `FEISHU_ALLOWED_CHAT_ID` | 条件 | 当前宿主管理的 Feishu 会话 | 将命令/回复限定在单个授权会话 |
| `FEISHU_POLL_CHAT_IDS` | 条件 | 当前宿主管理的 Feishu 会话 | 仅轮询模式下的会话白名单 |
| `FEISHU_POLL_INTERVAL_SECONDS` | 可选 | 运维策略 | 轮询间隔 |
| `FEISHU_WS_ENABLED` | 可选 | 部署策略 | 启用推荐的 WebSocket 长连接 |
| `FEISHU_TUNNEL_ENABLED` | 条件 | 部署策略 | HTTP 模式下启用 tunnel 进程 |
| `FEISHU_PUBLIC_HEALTH_CHECK` | 条件 | 部署策略 | 是否允许 supervisor 检查公网 health URL |
| `CLOUDFLARED_BIN` | 条件 | 当前宿主安装 | 覆盖 `cloudflared` 可执行文件的可移植解析 |
| `CLOUDFLARED_PROTOCOL` | 条件 | 当前宿主网络策略 | tunnel 传输协议 |
| `OPENAI_MODEL` | 条件 | 当前宿主的 assistant 策略 | OpenAI provider 的模型名 |
| `FEISHU_ASSISTANT_PROVIDER` | 可选 | 当前宿主的 assistant 策略 | 选择 assistant provider |
| `FEISHU_ASSISTANT_ENABLED` | 可选 | 当前宿主的 assistant 策略 | 启用或禁用普通 assistant 回复 |
| `FEISHU_ASSISTANT_REQUIRE_MENTION` | 可选 | 当前宿主的群聊策略 | 群聊中仅在 @bot 时回复 |
| `FEISHU_ASSISTANT_MAX_OUTPUT_TOKENS` | 可选 | 当前宿主的 assistant 策略 | 限制 assistant 最大输出 |
| `FEISHU_CODEX_COMMAND` | 可选 | 当前宿主 PATH | 从 PATH 解析 Codex CLI，不复制他人绝对路径 |
| `FEISHU_CODEX_MODEL` | 可选 | 当前宿主的 assistant 策略 | Codex provider 的模型名 |
| `FEISHU_CODEX_TIMEOUT_SECONDS` | 可选 | 运维策略 | 单次 Codex assistant 调用超时 |

### Runner 与可移植路径

| 键名 | 必需性 | 来源 | 用途 |
|---|---|---|---|
| `SUBMISSION_RUNNER_COMMAND` | 可选 | 当前宿主 PATH/部署策略 | 保留的 runner 命令；当前 Feishu 提审启动禁用 |
| `SUBMISSION_RUNNER_TIMEOUT_SECONDS` | 可选 | 运维策略 | 保留 runner 的有界超时 |
| `SUBMISSION_PROJECT_ROOT` | 可选 | 当前 clone 位置 | 覆盖项目根目录；留空时从源码位置解析 |
| `SUBMISSION_VM_IMAGES_DIR` | 可选 | 当前宿主的 UTM 资产布局 | VM 库目录 |
| `SUBMISSION_VM_TEMPLATE` | 可选 | 当前宿主的 UTM 资产布局 | 克隆模板包 |
| `SUBMISSION_SHARED_DIR` | 可选 | 当前宿主的共享目录 | 宿主与 guest 间的运行资产交接 |
| `PROJECT_SKILLS_DIR` | 可选 | 当前宿主的 Codex 安装 | 技能发现 symlink 目录，不是技能正文源 |

## 8 个 runtime/session 键

这些键由协调器、技能入口或本地浏览器会话在当次运行中注入。它们不是宿主持久配置，不得写入 `.env`、文档或提交。

| 键名 | 必需性 | 来源 | 用途 |
|---|---|---|---|
| `SUBMISSION_RUN_ID` | 条件 | 当次 Feishu/run 协调器 | 将命令绑定到唯一 run |
| `SUBMISSION_PROMPT_PATH` | 条件 | 当次 session handoff | 将保留 runner 绑定到当次提示文件 |
| `SUBMISSION_CHAT_ID` | 条件 | 当次 Feishu 事件/命令 | 将卡片或状态操作绑定到原会话 |
| `VM_NAME` | 条件 | 当次 VM 克隆/技能交接 | 传递已验证的精确 VM 名称 |
| `LOCAL_BROWSER_CDP_PORT` | 可选 | 当次本地浏览器会话 | 指定本地 CDP 端口 |
| `LOCAL_BROWSER_CHANNEL` | 可选 | 当次本地浏览器会话 | 指定受控浏览器 channel |
| `LOCAL_BROWSER_PROFILE_DIR` | 可选 | 当次本地浏览器会话 | 指定临时 profile 目录 |
| `LOCAL_BROWSER_STATE_FILE` | 可选 | 当次本地浏览器会话 | 指定会话状态文件 |

## SSH 内部临时变量

`SSH_ASKPASS`、`SSH_ASKPASS_REQUIRE`、`DISPLAY`、`SUBMISSION_SSH_ASKPASS_SOCKET` 和 `SUBMISSION_SSH_ASKPASS_CAPABILITY` 由一次性 password broker 自动生成并注入子进程。它们只存活于当次连接，不得手工设置、写入 `.env`、记录或输出。

## 安全边界

- `.env` 必须被 Git 忽略且仅当前用户可读。
- 不将密码、token、secret、URL、ID 或主机路径放入 CLI 参数、日志、卡片或故障报告。
- `runtime/`、签名材料、VM 包/镜像/配置都是宿主私有状态，不得读取、提交或发布。
- 排障时只报告缺失/无效的键名与错误类型，不报告实际值。
