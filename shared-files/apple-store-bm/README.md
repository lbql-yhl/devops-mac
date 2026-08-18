# apple-store-bm 公开运行包

## 用途

该目录提供 App Store 批处理工具的可公开运行包，由对应技能在已验证的 run、应用与签名上下文中调用。

## 来源与校验

- `apple_store_tools` 是面向 Apple Silicon macOS 的 Mach-O 64-bit `arm64` 可执行文件。
- `config/prod.example.yml` 只提供无值占位符结构。
- 可执行文件 SHA-256：`0e246e1dc86ea8f1ac0edced28d8abd4419104e896437457f5bb166de2202ab2`。

使用前必须独立校验文件类型、`arm64` 架构、可执行权限和上述 SHA-256。哈希或架构不匹配时立即停止，不运行未验证二进制。

## 安全边界

- 真实运行配置与 P8 私钥只能在本地运行时生成，不得进入 Git、日志、聊天或卡片。
- 只能在对应 `SKILL.md` 已绑定精确 run、App ID、Key ID、P8 归属与幂等检查后运行；不手工脱离流程执行批处理。
- 运行时保持二进制与 `config/` 同级，但不得把生成的私有文件回写到公开工作树。
