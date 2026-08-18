# macOS 提审自动化

这是一个从 macOS 宿主协调 UTM macOS guest、Feishu OpenAPI、Notion API 与 App Store Connect 操作的自动化项目。项目采用 16 个主线技能，项目内 `skills/` 是唯一技能源，外部技能目录只作发现入口。

## 前置依赖

- macOS 宿主与 [UTM](https://mac.getutm.app/)
- [Python 3](https://www.python.org/downloads/)、[Node.js](https://nodejs.org/)、[Git](https://git-scm.com/)和 `curl`
- guest 中的 [Xcode](https://developer.apple.com/xcode/) 与项目要求的应用
- [Feishu Open Platform](https://open.feishu.cn/) 自建应用
- [Notion integration](https://developers.notion.com/docs/create-a-notion-integration)
- 可访问的 App Store Connect / Apple Developer 账号与当前宿主自有的资源

详细宿主与 guest 依赖见 [宿主/VM 依赖检查](docs/host-vm-dependency-check.md)。

## 可移植快速启动

1. 克隆仓库并进入项目根目录：

   ```bash
   git clone <repository-url>
   cd <repository-directory>
   ```

2. 创建当前宿主的私有配置：

   ```bash
   cp .env.example .env
   chmod 600 .env
   git check-ignore -q .env
   ```

   按 [配置参考](docs/configuration.md) 填写当前宿主自己的 token、key、ID、path 和 password。不要复制其他机器的 `.env`。

3. 先做不依赖宿主私有资源的项目检查，再做宿主检查：

   ```bash
   python3 scripts/preflight.py --project-only
   python3 scripts/preflight.py
   ```

4. 安装技能发现 symlink 并检查它们仍指向项目内唯一源：

   ```bash
   /bin/zsh scripts/install_project_skills.sh --install
   /bin/zsh scripts/install_project_skills.sh --check
   ```

5. 选择一种飞书接收模式，启动 supervisor：

   ```bash
   python3 -u services/feishu_supervisor.py
   curl --fail http://127.0.0.1:8787/health
   ```

   WebSocket 长连接是推荐模式；HTTP 回调仅在你已有 TLS 公网入口且完成来源认证时使用，回调路径是 `/feishu/events`。两种模式的配置与安全边界见 [Feishu Bot 指南](docs/utm-feishu-bot.md)。

## 四段顺序

1. 虚拟机准备：utm-vm-clone
2. 环境准备：utm-notion → utm-clash-ip → utm-login → utm-edit → utm-key → utm-apps → utm-business
3. 脚本准备、执行：utm-env → utm-script → utm-image → utm-p8
4. 代码处理：utm-21 → utm-22 → utm-23 → utm-24

每段可单独运行，每个技能也可人工单独指定执行。精确技能链接和交接边界见 [项目流程](docs/project-workflow.md)。

## Feishu 运行状态

飞书提审启动当前禁用：完整登记消息和 `/提审 开始` 不会创建新 run、VM、prompt 或后台 runner。普通 Codex 对话、日报和已有运行的状态/卡片能力保留。

流程中的 Feishu 数据只通过 Bot/OpenAPI 读取，Notion 数据只通过 Notion API 和 `scripts/notion_api.py` 读写。正常运行自动继续；自动诊断、修复和复验恢复穷尽后才能发送最后故障卡。

## 文档中列出的质量命令

以下命令供维护者在合适的环境中手动执行：

```bash
python3 -m pytest -q
node --test tests/test_utm_22_distribute.mjs
python3 scripts/public_release_audit.py
git diff --check
```

## 公开安全政策

- 永远不提交或输出 `.env`、`runtime/`、签名私钥/证书/profile、VM 包/镜像/配置、真实 Feishu/Notion/App Store 资源 ID、主机绝对路径或密码。
- 每台宿主都必须独立创建 `.env`，不复制运行历史、VM 绑定、浏览器会话、签名材料或凭据。
- 运行时目录只在本地生成，目录权限应为 `0700`，包含元数据的普通文件权限应为 `0600`，且不允许符号链接。
- 切勿在 issue、日志、卡片或故障报告中粘贴配置值；只报告键名、错误类型和脱敏状态。

## 文档层级

1. 本 README：项目介绍与快速启动。
2. [配置参考](docs/configuration.md)：所有宿主键与运行时边界。
3. [项目流程](docs/project-workflow.md)：精确四段和 16 个技能链接。
4. [Feishu Bot 指南](docs/utm-feishu-bot.md)：开放平台权限、事件、运行与排障。
5. `skills/<skill>/SKILL.md`：单技能可执行真值；[`skills/_shared/AUTOMATION_CONTRACT.md`](skills/_shared/AUTOMATION_CONTRACT.md)：共享合同。
6. [`shared-files/`](shared-files/README.md)：公开运行资产的用途、来源、校验和安全边界。
