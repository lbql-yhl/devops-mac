# 项目流程

项目主线只有以下四段。四段均可单独运行，每个技能也可人工单独指定执行；只执行指定范围，范围内保持精确顺序。

1. 虚拟机准备：utm-vm-clone
2. 环境准备：utm-notion → utm-clash-ip → utm-login → utm-edit → utm-key → utm-apps → utm-business
3. 脚本准备、执行：utm-env → utm-script → utm-image → utm-p8
4. 代码处理：utm-21 → utm-22 → utm-23 → utm-24

## 技能链接

### 1. 虚拟机准备

1. [`utm-vm-clone`](../skills/utm-vm-clone/SKILL.md)

本段成功后可结束；需继续时从 `utm-notion` 进入第二段。

### 2. 环境准备

1. [`utm-notion`](../skills/utm-notion/SKILL.md)
2. [`utm-clash-ip`](../skills/utm-clash-ip/SKILL.md)
3. [`utm-login`](../skills/utm-login/SKILL.md)
4. [`utm-edit`](../skills/utm-edit/SKILL.md)
5. [`utm-key`](../skills/utm-key/SKILL.md)
6. [`utm-apps`](../skills/utm-apps/SKILL.md)
7. [`utm-business`](../skills/utm-business/SKILL.md)

本段成功后可结束；需继续时从 `utm-env` 进入第三段。

### 3. 脚本准备、执行

1. [`utm-env`](../skills/utm-env/SKILL.md)
2. [`utm-script`](../skills/utm-script/SKILL.md)
3. [`utm-image`](../skills/utm-image/SKILL.md)
4. [`utm-p8`](../skills/utm-p8/SKILL.md)

本段成功后可结束；需继续时从 `utm-21` 进入第四段。

### 4. 代码处理

1. [`utm-21`](../skills/utm-21/SKILL.md)
2. [`utm-22`](../skills/utm-22/SKILL.md)
3. [`utm-23`](../skills/utm-23/SKILL.md)
4. [`utm-24`](../skills/utm-24/SKILL.md)

`utm-24` 成功后主线结束。

## 执行与交接规则

- 单技能执行以链接的 `SKILL.md` 为唯一可执行真值；先遵守 [共享自动化合同](../skills/_shared/AUTOMATION_CONTRACT.md)。
- 只运行 `SKILL.md` 列出的唯一宿主入口，不绕过入口运行内部步骤。失败并修复后重新运行同一入口。
- 连续交接保持同一精确 run、应用、VM、网络/SSH 身份、浏览器会话和工作目录；不按“最新”或模糊名称重选。
- Feishu 数据只通过 Bot/OpenAPI 访问，Notion 数据只通过 `scripts/notion_api.py` 或项目内部的 Notion API 封装读写。
- 文档不复制技能命令或操作正文。配置见 [配置参考](configuration.md)，Feishu 部署见 [Feishu Bot 指南](utm-feishu-bot.md)。
