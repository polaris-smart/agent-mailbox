# Beta 4 替代版本与统一发布验收

2026-10-01 Human 决定：全部替代 v0.7，各平台、各渠道保持同一产品版本；发布前测试通过，不能带已知失败发行。

产品、仓库、Python 包和 CLI 名称继续 `agent-mailbox`。目标 Python/App 版本 `0.8.0b4`，GitHub tag `v0.8.0b4`，本轮只发 GitHub Beta 和 PyPI，均为同一 Beta 4、同一提交。GitHub 标为 prerelease。未来若新增 scoped npm，其 SemVer 可为 `0.8.0-beta.4`，须先确认账户和 scope；不触碰第三方无 scope 同名 npm 包。TestPyPI、Homebrew、npm 从未发布，均不属于本轮既有渠道。

软件替代不包含 v0.7 数据库、后台服务与配置自动迁移。旧 MCP 配置不兼容新的项目工具入口。旧数据应保留，旧后台服务应明确停止；新版本使用独立 v0.8 数据目录。已有兼容 v0.8 的保留数据升级证据不扩展为 v0.7 或跨架构迁移承诺。

父任务负责统一元数据、构建和 CI；文档描述真实阶段。当前 Beta 4 整改未发，Beta 3 历史本机通过不代表当前 macOS/Windows CI 或所有渠道通过。本轮以六组 CI 与 Mac/Windows/Linux 原生包检查门禁为准，当前未全部通过。发行前完成版本/提交校验、平台回归、GitHub/PyPI 隔离安装、真实协作、替代/升级、数据恢复与文档复现；任何必要项失败，先修复并重测。

详细渠道与发布门槛见 [RELEASE-CHANNELS](../RELEASE-CHANNELS.md)。公开发布后才同步默认分支及各安装渠道，并记录实际提交、产物校验、平台及通过/跳过项。
