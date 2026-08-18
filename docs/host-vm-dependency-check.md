# 宿主与 UTM guest 依赖检查

## 用途

[`scripts/check_host_vm_dependencies.py`](../scripts/check_host_vm_dependencies.py) 从 macOS 宿主发起一次只读检查，同时报告宿主与一台精确 UTM macOS guest 是否具备项目依赖。它不安装软件，不启动、停止、重启或切换 VM。

## 来源

检查项与身份校验由项目跟踪脚本定义。VM 库位置、guest 认证和宿主路径只从当前机器的 [`.env` 配置](configuration.md) 与当次精确参数解析，不使用他人路径或历史 run。

## 运行与校验

先确保目标 guest 已由操作者启动，并且当前宿主配置中的 inventory 身份与 guest 一致。在项目根目录运行：

```bash
python3 scripts/check_host_vm_dependencies.py --vm-name '<exact-vm-name>'
```

脚本固定输出 26 个检查项，每行仅有“已安装”或“未安装”状态。只有输出数量完整且 26 项全部为“已安装”才表示校验成功，不得只看进程退出状态。

“guest 连接未安装”表示精确 VM 身份、运行状态、网络或 SSH 校验未完成；此时后续 guest 项目可能连带报“未安装”，不得直接推断软件确实缺失。

## 安全边界

- 命令只接收当次精确 VM 选择器，不扫描或选择“最新” VM。
- guest 密码只通过项目一次性 SSH askpass broker 使用，不放入 argv、日志或输出。
- 不输出真实 IP、UUID、MAC、宿主路径、密码或 VM 配置。
- 发现缺失项后，先在其所属的宿主或 guest 上完成明确授权的修复，再重跑同一只读检查。
