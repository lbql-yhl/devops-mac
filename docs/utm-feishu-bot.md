# UTM Feishu Bot 部署与运行

Bot 负责 Feishu 消息、日报、普通 assistant 对话和已有 run 的状态/卡片能力。Feishu 数据只通过 Bot/OpenAPI 访问，不读 Feishu 桌面客户端、浏览器 DOM 或剪贴板。

## 当前功能边界

Feishu 提审启动当前禁用。完整登记消息和 `/提审 开始` 静默返回，不创建 run、VM 名称、prompt 或后台 runner。普通 Codex 对话、日报以及已有 run 的状态、故障卡和回调能力保留。

如果后续由代码变更恢复新 run 入口，主线仍必须遵守 [精确四段](project-workflow.md) 与项目内 16 个 `SKILL.md`；本文不授权绕过当前禁用开关。

## Feishu Open Platform 配置

1. 创建当前宿主专用的自建应用并启用 Bot。
2. 从开放平台取得当前应用的 App ID/App Secret，只写入本机 `.env`。
3. 申请并发布以下最小权限：

   - `im:message:send_as_bot`
   - `im:message.group_at_msg:readonly`
   - `im:message.p2p_msg:readonly`
   - `im:message:readonly`
   - `im:resource`
   - `bitable:app`
   - `drive:drive:readonly`
   - `wiki:wiki:readonly`

4. 订阅事件 `im.message.receive_v1` 和卡片动作 `card.action.trigger`。
5. 将 Bot 添加到当前宿主明确授权的会话。Bot 应用、会话路由、Feishu Base/Wiki 与 Notion 根页必须都属于同一宿主配置，不按“最新”或模糊名称路由。

## 23 个 `FEISHU_*` 宿主键

值只能存在当前宿主的 `.env`。完整 47 键清单、必需性与互斥路由见 [配置参考](configuration.md)。

| 键名 | 来源 | 用途 |
|---|---|---|
| `FEISHU_APP_ID` | Feishu Open Platform 自建应用 | Bot/OpenAPI 应用身份 |
| `FEISHU_APP_SECRET` | Feishu Open Platform 自建应用 | 换取 tenant access token |
| `FEISHU_VERIFICATION_TOKEN` | Feishu 事件订阅 | URL verification 检查和 WS 事件分发器 |
| `FEISHU_ENCRYPT_KEY` | Feishu 事件订阅 | WS 事件安全参数和 HTTP 原始请求签名校验开关 |
| `FEISHU_DAILY_REPORT_CHAT_ID` | 当前宿主管理的日报会话 | 唯一日报投递目标 |
| `FEISHU_BOT_HOST` | 本机服务网络配置 | HTTP/health 监听地址 |
| `FEISHU_BOT_PORT` | 本机服务网络配置 | HTTP/health 监听端口 |
| `FEISHU_SEND_RETRIES` | 当前宿主运维策略 | OpenAPI 发送的有界重试次数 |
| `FEISHU_SEND_TIMEOUT_SECONDS` | 当前宿主运维策略 | 单次 OpenAPI 发送超时 |
| `FEISHU_VERIFY_DELIVERY` | 当前宿主运维策略 | 是否通过 OpenAPI 回读验证送达 |
| `FEISHU_ALLOWED_CHAT_ID` | 当前宿主管理的会话 | 将命令与回复限定到一个授权会话 |
| `FEISHU_POLL_CHAT_IDS` | 当前宿主管理的会话 | 可选轮询模式的会话白名单 |
| `FEISHU_POLL_INTERVAL_SECONDS` | 当前宿主运维策略 | 轮询间隔 |
| `FEISHU_WS_ENABLED` | 部署策略 | 启用推荐的 WebSocket 长连接 |
| `FEISHU_TUNNEL_ENABLED` | 部署策略 | HTTP 模式下启用 tunnel |
| `FEISHU_PUBLIC_HEALTH_CHECK` | 部署策略 | 是否允许 supervisor 探测公网 health URL |
| `FEISHU_ASSISTANT_PROVIDER` | assistant 策略 | 选择普通对话 provider |
| `FEISHU_ASSISTANT_ENABLED` | assistant 策略 | 启用或禁用普通 assistant 回复 |
| `FEISHU_ASSISTANT_REQUIRE_MENTION` | 群聊策略 | 群聊中仅在 @bot 时回复 |
| `FEISHU_ASSISTANT_MAX_OUTPUT_TOKENS` | assistant 策略 | 限制单次回复长度 |
| `FEISHU_CODEX_COMMAND` | 当前宿主 PATH | 从 PATH 解析 Codex CLI，不使用他人绝对路径 |
| `FEISHU_CODEX_MODEL` | assistant 策略 | Codex provider 的模型名 |
| `FEISHU_CODEX_TIMEOUT_SECONDS` | 当前宿主运维策略 | 单次 Codex 调用超时 |

## 传输模式：二选一

### WebSocket 长连接（推荐）

WebSocket 不需要公网 callback URL，降低了入站暴露面。在 `.env` 启用 `FEISHU_WS_ENABLED`，关闭 `FEISHU_TUNNEL_ENABLED`，然后启动：

```bash
python3 -u services/feishu_supervisor.py
curl --fail http://127.0.0.1:8787/health
```

WebSocket 启动要求 `lark-oapi`，并使用 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_VERIFICATION_TOKEN` 与 `FEISHU_ENCRYPT_KEY` 创建事件分发器。

### HTTP callback

只有在宿主已有 TLS 公网入口和来源认证策略时才选用 HTTP。在 Feishu 开放平台配置的唯一回调路径是：

```text
https://<public-host>/feishu/events
```

禁止将本地端口直接裸露到公网。可使用自管理反向代理，或在经过安全评审后启用 supervisor 的 tunnel 选项。本地 health 端点仍为 `/health`。

## HTTP 回调安全边界

当前 HTTP handler 的实际边界如下：

- `FEISHU_ENCRYPT_KEY` 非空时，handler 对原始 POST body 与 `x-lark-request-timestamp`、`x-lark-request-nonce`、`x-lark-signature` 执行签名比较。
- URL verification 请求在 `FEISHU_VERIFICATION_TOKEN` 非空时比较 token。普通明文事件不会再独立比较该 token。
- 当前 HTTP handler **不解密**带 `encrypt` 字段的回调，会返回 `encrypted_callbacks_not_enabled`。不得将“配置 Encrypt Key”误当成已支持加密 payload。
- 如果因 Feishu 配置而无法同时得到可验签的明文事件，不得绕过验签。改用推荐的 WebSocket，或先在受信网关实现完整的来源认证、时间窗/重放防护、请求大小与速率限制，再转发到本地 handler。
- TLS 只保护传输，不等于 Feishu 来源认证。在上述边界未满足时，不得对公网开放 HTTP 回调。

## 宿主路由与日报群隔离

- 每台宿主在自己的 `.env` 中配置唯一宿主身份、Feishu 应用、Notion 根页和 Feishu Base/Wiki 路由。
- run 只能由 `SUBMISSION_HOST_MACHINE` 与原 `chat_id` 精确归属；不得使用其他宿主的 VM 绑定、凭据、页面或运行历史。
- `FEISHU_DAILY_REPORT_CHAT_ID` 必须是专用日报会话。该会话只接收用户确认的日报，不接收故障卡、成功卡、状态卡、测试消息、health 消息或普通 assistant 回复。
- 故障卡只能发回当前 run 的原非日报 `chat_id`。自动诊断、实际修复和独立复验恢复穷尽后才可发送最后故障卡。

## Health 与无敏排障

先从宿主本地检查：

```bash
curl --fail http://127.0.0.1:8787/health
```

排障只记录键名和非敏状态：

1. health 失败：检查 supervisor/Bot 进程是否运行、监听端口是否冲突，不粘贴 `.env` 或 runtime 文件。
2. WS 不收事件：检查应用已发布、Bot 已加入授权会话、权限/事件名存在，只报告“已配置/缺失”。
3. HTTP 返回 `invalid_signature`：核对开放平台与本机的签名配置来源，不打印 header、body 或 key。
4. HTTP 返回 `encrypted_callbacks_not_enabled`：当前 handler 不支持加密 body，不关闭安全校验强行继续；转用 WS 或完成受信网关适配。
5. 发送失败：只核对所需 scope、Bot 成员关系、目标是否为授权会话和非敏 HTTP 错误类型，不输出 chat ID 或 API 响应原文。
6. Base/Wiki 读取失败：检查 Wiki URL 或 Base 三元组二选一、不混用，只报告缺失键名。

禁止把 token、secret、key、chat ID、Base/Wiki/Notion ID、回调 URL、消息原文、runtime 记录或主机路径粘贴到 issue、聊天、日志或卡片。

## 主线技能范围

下列名称仅用于确认 Bot 交接范围，操作真值仍是各自 `SKILL.md`：

→ utm-vm-clone
→ utm-notion
→ utm-clash-ip
→ utm-login
→ utm-edit
→ utm-key
→ utm-apps
→ utm-business
→ utm-env
→ utm-script
→ utm-image
→ utm-p8
→ utm-21
→ utm-22
→ utm-23
→ utm-24
