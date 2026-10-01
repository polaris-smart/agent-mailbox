# agent-mailbox 统一发行渠道与发布门槛

2026-10-01。Human 已决定：**v0.8 统一替代 v0.7；各平台、各发行渠道保持同一产品版本，发布前逐项测试通过。** 产品、仓库、Python 包与 CLI 均保持 `agent-mailbox`，不另设 workbench 发行名。本轮发行范围为 **GitHub Beta + PyPI**。

定位：**Local-first AI agent team workbench for Codex and Claude Code / 把已有 AI Agent 组成项目团队的本地工作台**。软件替代不等于数据迁移：v0.7 数据库、后台服务与配置没有自动迁移，旧 MCP 配置不兼容新的项目工具入口，不应复用旧数据目录。兼容 v0.8 的保留数据升级是另一条已验证路径，不能推广成跨架构自动继承旧配置。

## 本轮统一版本

| 产物或渠道 | 版本表示 | 规则 |
| --- | --- | --- |
| Python / PyPI / wheel | `0.8.0b4` | PEP 440 预发布版本 |
| GitHub Release / tag | `v0.8.0b4` | 标记为 prerelease |
| macOS App 与平台程序显示 | `0.8.0b4` | 同一 Beta 4，不各自递增 |

GitHub Beta、PyPI 与平台产物必须来自**同一提交**。正式稳定版再统一为 Python `0.8.0`、GitHub `v0.8.0`，在通过稳定发行门槛后更新稳定渠道。

## 已发布的渠道事实（2026-10-01）

- **PyPI**：[0.8.0b4 已发布](https://pypi.org/project/agent-mailbox/0.8.0b4/)，账户 `coolmax`，作者 `polaris-smart`。独立环境从正式索引安装、HTTP/MCP 检查通过；预发布必须指定版本或 `--pre`，普通稳定安装仍可能选择 0.7.6，不撤销旧稳定版。
- **GitHub**：[v0.8.0b4 Beta 已发布](https://github.com/polaris-smart/agent-mailbox/releases/tag/v0.8.0b4)，default main 已切换独立 v0.8。三个原生包、wheel、sdist、SHA256SUMS 和来源记录共七个文件上传完成，公开 asset digest 与本地文件全匹配。所有发行物源于 `420d136651ff8209b5a3fdc43f4b4bb985fa3c39`；后续文档提交不替换这些不可变发行物。
- **TestPyPI**：从未发布本项目包，当前包查询返回 404，不是丢失的旧入口。以后可先上传并验证，当前不列入发行渠道。
- **Homebrew**：从未发布自建 tap，属于以后可选新增渠道，不宣称已有配方。
- **npm 配套插件**：[dsh-agent-mailbox](https://www.npmjs.com/package/dsh-agent-mailbox) 已有 `0.1.0`–`0.1.2`，维护者 `polaris-smart`，源码为 [polaris-smart/dsh-agent-mailbox](https://github.com/polaris-smart/dsh-agent-mailbox)。它是 DeepSeek Harness 插件，提供 v0.7 的八个 `mailbox_*` 工具，不是工作台安装包；尚未适配或发布 v0.8。继续使用原包名即可，无需新建 scope。无 scope 的 `agent-mailbox` 属于 `NoizceEra/agent-mailbox`，不能作为本项目安装来源。

安装请使用[上手指南](GITHUB-QUICKSTART.md)中的专用 venv 与独立 `--home`，或下载对应原生包。不提供 `brew` 或 `npm` Beta 4 安装命令。

## 发布门槛与准备记录

本轮以**六组 CI 与 macOS/Windows/Linux 原生包检查**为发布门禁。发行提交 `420d136` 的[预发布 CI](https://github.com/polaris-smart/agent-mailbox/actions/runs/36849107349)、[main CI](https://github.com/polaris-smart/agent-mailbox/actions/runs/36849909110)及[PyPI 发布流程](https://github.com/polaris-smart/agent-mailbox/actions/runs/36849910427)均通过。Mac/Linux 每组 365 passed、4 skipped；Windows 每组 321 passed、48 skipped。原生包均实际解压运行；跳过项及真实模型边界单独列明，不能当作全部功能实机验收。

1. **版本和来源一致**：代码元数据、包版本、App 显示、Git tag、Release 与 PyPI 对应同一提交和同一 Beta 4；发行产物 SHA-256 可核对。
2. **平台 CI 无失败**：按实际发行架构执行回归与构建。跳过项必须有明确理由，不把跳过功能当作已验收；任何必要项失败先修复并重测。
3. **安装测试**：独立环境实际安装、检查版本、启动工作台、准备依赖与退出；wheel/App、平台原生包、PyPI 各自验证，不以源码启动替代包安装测试。
4. **首次协作测试**：支持的 Codex/Claude Code 员工完成上下文读取、开发、固定交付审查与 Human 验收；无模型 CI 测试与真实原生模型证据分别记录。
5. **替代和升级测试**：旧程序/后台服务的停止及处理方法明确，旧数据保留；兼容 v0.8 升级保留身份与业务记录。任务保护、备份、恢复和节点兼容性按实际范围验证；没有 v0.7 自动迁移承诺。
6. **发行身份**：macOS App 尚无 Apple Developer ID 签名、公证。明确实际发行方式、系统提示与产物来源；校验和不能代替发行身份验证。
7. **文档同步**：实际发布后默认分支和 PyPI 安装入口切换到同一版本。中英文步骤能复现，不残留旧命令、不要求删除数据升级。

TestPyPI、Homebrew 若未来启用，再加入对应门禁。已有 npm 插件升级必须先适配 v0.8 项目身份和工具接口，通过独立安装与协作测试；不能仅修改版本号。npm 的 Beta 4 对照为 `0.8.0-beta.4`，发布时使用 `--tag beta --registry=https://registry.npmjs.org`，不覆盖稳定 `latest`。本机默认 registry 为 npmmirror；2026-10-01 官方 registry 的 `npm whoami` 返回 401，现有发布凭据尚未验证可用，不改登录、不输出 token。

历史失败和整改按原始提交保留；不删除失败证据，也不重建或覆盖同一版本已上传文件。

历史证据和功能边界见 [Beta 验收](BETA-ACCEPTANCE.md)、[Beta 3 证据](evidence/v080/beta3-collaboration.md)。Beta 4 的结果应单独记录，不能复制历史通过数字当作本轮验证。
