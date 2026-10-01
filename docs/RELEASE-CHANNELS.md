# agent-mailbox 统一发行渠道与发布门槛

2026-10-01。Human 已决定：**v0.8 统一替代 v0.7；各平台、各发行渠道保持同一产品版本，发布前逐项测试通过。** 产品、仓库、Python 包与 CLI 均保持 `agent-mailbox`，不另设 workbench 发行名。正式版目标范围为 **GitHub + PyPI 工作台，以及独立 npm 配套插件**。

定位：**Local-first AI agent team workbench for Codex and Claude Code / 把已有 AI Agent 组成项目团队的本地工作台**。软件替代不等于数据迁移：v0.7 数据库、后台服务与配置没有自动迁移，旧 MCP 配置不兼容新的项目工具入口，不应复用旧数据目录。兼容 v0.8 的保留数据升级是另一条已验证路径，不能推广成跨架构自动继承旧配置。

## 本轮统一版本

| 产物或渠道 | 目标版本 | 规则 |
| --- | --- | --- |
| Python / PyPI / wheel | `0.8.0` | 已公开；独立官方索引安装返回 0.8.0 |
| GitHub Release / tag | `v0.8.0` | 已公开，非 prerelease，Latest 为 v0.8.0；8 份资产 digest 核验通过 |
| macOS App 与平台程序显示 | `0.8.0` | 同一发行源提交，原生安装包分别验收 |
| npm 配套插件 | `dsh-agent-mailbox@0.8.0` | 独立插件仓库；不是工作台安装包，单独安装/接口测试 |

GitHub/PyPI/平台工作台产物来自同一最终发行提交；插件来自独立源码提交，版本一致不表示二进制来源相同。[PyPI 精确版本](https://pypi.org/project/agent-mailbox/0.8.0/) 已公开并完成独立官方索引安装与版本检查；[GitHub 正式版](https://github.com/polaris-smart/agent-mailbox/releases/tag/v0.8.0) 已公开，非 draft、非 prerelease，Latest 为 v0.8.0；8 份资产官方 digest 与本地逐一匹配。最终 CI 已通过，实证见下；公开发行与 asset hash 核验单独进行。

默认现有会话 MCP 邮箱只支持本机员工，主动查信，不自动唤醒；员工角色不增加权限；邮件任务由员工明确接受/提交，再由 Human 验收。受管 CLI 是独立选项。

## 已发布的历史渠道事实（Beta 4；不代表正式版已发布）

- **PyPI**：[0.8.0b4 已发布](https://pypi.org/project/agent-mailbox/0.8.0b4/)，账户 `coolmax`，作者 `polaris-smart`。独立环境从正式索引安装、HTTP/MCP 检查通过；预发布必须指定版本或 `--pre`，普通稳定安装仍可能选择 0.7.6，不撤销旧稳定版。
- **GitHub**：[v0.8.0b4 Beta 已发布](https://github.com/polaris-smart/agent-mailbox/releases/tag/v0.8.0b4)，default main 已切换独立 v0.8。三个原生包、wheel、sdist、SHA256SUMS 和来源记录共七个文件上传完成，公开 asset digest 与本地文件全匹配。所有发行物源于 `420d136651ff8209b5a3fdc43f4b4bb985fa3c39`；后续文档提交不替换这些不可变发行物。
- **TestPyPI**：从未发布本项目包，当前包查询返回 404，不是丢失的旧入口。以后可先上传并验证，当前不列入发行渠道。
- **Homebrew**：从未发布自建 tap，属于以后可选新增渠道，不宣称已有配方。
- **npm 配套插件**：[dsh-agent-mailbox](https://www.npmjs.com/package/dsh-agent-mailbox) 已有 `0.1.0`–`0.1.2`，维护者 `polaris-smart`，源码为 [polaris-smart/dsh-agent-mailbox](https://github.com/polaris-smart/dsh-agent-mailbox)。它是 DeepSeek Harness 插件，提供 v0.7 的八个 `mailbox_*` 工具，不是工作台安装包；Beta 4 发行时未适配 v0.8；当前 `0.8.0` 项目 MCP 适配已实现、通过独立三平台 CI 并在官方 npm registry 公开，独立安装与插件导入验证通过。继续使用原包名即可，无需新建 scope。无 scope 的 `agent-mailbox` 属于 `NoizceEra/agent-mailbox`，不能作为本项目安装来源。

安装请使用[上手指南](GITHUB-QUICKSTART.md)中的专用 venv 与独立 `--home`，或下载对应原生包。不提供 `brew` 或 `npm` Beta 4 安装命令。

## Beta 4 历史门禁与现行发布要求

Beta 4 以**六组 CI 与 macOS/Windows/Linux 原生包检查**为发布门禁。发行提交 `420d136` 的[预发布 CI](https://github.com/polaris-smart/agent-mailbox/actions/runs/36849107349)、[main CI](https://github.com/polaris-smart/agent-mailbox/actions/runs/36849909110)及[PyPI 发布流程](https://github.com/polaris-smart/agent-mailbox/actions/runs/36849910427)均通过。Mac/Linux 每组 365 passed、4 skipped；Windows 每组 321 passed、48 skipped。原生包均实际解压运行；跳过项及真实模型边界单独列明，不能当作全部功能实机验收。

1. **版本和来源一致**：代码元数据、包版本、App 显示、Git tag、Release 与 PyPI 对应同一工作台提交和产品版本；发行产物 SHA-256 可核对。
2. **平台 CI 无失败**：按实际发行架构执行回归与构建。跳过项必须有明确理由，不把跳过功能当作已验收；任何必要项失败先修复并重测。
3. **安装测试**：独立环境实际安装、检查版本、启动工作台、准备依赖与退出；wheel/App、平台原生包、PyPI 各自验证，不以源码启动替代包安装测试。
4. **首次协作测试**：支持的 Codex/Claude Code 员工完成上下文读取、开发、固定交付审查与 Human 验收；无模型 CI 测试与真实原生模型证据分别记录。
5. **替代和升级测试**：旧程序/后台服务的停止及处理方法明确，旧数据保留；兼容 v0.8 升级保留身份与业务记录。任务保护、备份、恢复和节点兼容性按实际范围验证；没有 v0.7 自动迁移承诺。
6. **发行身份**：macOS App 尚无 Apple Developer ID 签名、公证。明确实际发行方式、系统提示与产物来源；校验和不能代替发行身份验证。
7. **文档同步**：实际发布后默认分支和 PyPI 安装入口切换到同一版本。中英文步骤能复现，不残留旧命令、不要求删除数据升级。

TestPyPI、Homebrew 若未来启用，再加入对应门禁。已有 npm 插件升级必须先适配 v0.8 项目身份和工具接口，通过独立安装与协作测试；不能仅修改版本号。npm 的 Beta 4 对照为 `0.8.0-beta.4`，发布时使用 `--tag beta --registry=https://registry.npmjs.org`，不覆盖稳定 `latest`。本机默认 registry 为 npmmirror；前期官方 registry 的 `npm whoami` 返回 401，随后用户恢复登录；登录成功不代称实际发布权限或新版本已公开，不输出 token。

历史失败和整改按原始提交保留；不删除失败证据，也不重建或覆盖同一版本已上传文件。

历史证据和功能边界见 [Beta 验收](BETA-ACCEPTANCE.md)、[Beta 3 证据](evidence/v080/beta3-collaboration.md)。Beta 4 的结果应单独记录，不能复制历史通过数字当作本轮验证。

正式版 npm 发布必须显式使用 `--registry=https://registry.npmjs.org`，插件稳定版使用 `latest`。登录成功只证明身份；实际发布权限、版本、插件 tarball 与公开安装需要分别核验，不输出 token、不把镜像源发布当官方成功。


### v0.8.0 发行准备实证（不代称公开发行）

源码提交 `4201b34dad503529d113b904ae67a23be7cc5644` 的 [CI](https://github.com/polaris-smart/agent-mailbox/actions/runs/36865817096) 完成 9/9 门禁：六组回归和三平台原生包检查。Mac/Linux 各组 404 passed、4 skipped；Windows 各组 360 passed、48 skipped。后台线程异常提升为测试失败；本次无后台警告。原生包实际解压、启动，并分别验证 12 个既有会话邮箱 MCP 工具和 11 个受管项目 MCP 工具；协议 fixture 不调用供应商模型。Mac 新原生包在真实数据副本上验证 schema 9→11，20 名员工、2 个项目及历史记录保留。

这些测试本身不能代称渠道公开；当前 GitHub/PyPI/npm 均已公开 0.8.0，分别以渠道核验记录为准。ZCode 隔离 CLI、DeepSeek Harness 无模型接口验证也不能代表所有正在运行的真实 App 会话已热加载 MCP 或自动唤醒。真实宿主、物理设备和跨设备验收按实际范围单列。Beta 4 历史证据不覆盖本轮结果。


## v0.8.0 当前发行状态

最终工作台源 `4413fbad7332501590919ed37b378fdb6c666eb8`。[发布流程 36868116924](https://github.com/polaris-smart/agent-mailbox/actions/runs/36868116924) 首次 Win 3.10 超时，重跑失败任务后全部成功；PyPI `0.8.0` 已公开，独立官方索引安装 `--version` 返回 `0.8.0`。GitHub 已公开，非 draft、非 prerelease，Latest 为 v0.8.0，8 份资产官方 digest 与本地逐一匹配；PyPI 两份资产官方 digest 同样匹配。

Mac 最终 CI ZIP 制成 DMG 后实际升级 `/Applications/Agent Mailbox.app`，bootstrap 为 `0.8.0`；真实 home schema 9→11 保留 20 员工、2 项目、1 邮件、0 任务，私有一致性备份已存。此前 4201b34 的 404/4 与 360/48 回归统计保留，不假写为最终重跑的独立统计。

独立 npm 插件发行源 `ae43aa7582ed6dcb57458475a9fde7fe37e4d8f0` 的[三平台 CI](https://github.com/polaris-smart/dsh-agent-mailbox/actions/runs/36868122488) 通过；发布早期返回 `EOTP`，一次浏览器认证流程等待后返回 `E404`、失效；之后用户浏览器二次验证成功，命令返回 `+ dsh-agent-mailbox@0.8.0`。等待 registry processing 数分钟后，官方版本已可见；独立官方 registry 安装与 dist/plugin.js 的 name/apply 导入验证通过。GitHub/PyPI/npm 三个已启用渠道已统一 0.8.0；最后 npm tarball SHA 核对单独记录。详细来源与失败/重跑记录见[正式版证据](evidence/v080/stable-release.md)。


正式版公开核验：tag 指向 `4413fbad7332501590919ed37b378fdb6c666eb8`；GitHub 8 资产及 PyPI 两份发行文件官方 digest 匹配，具体文件清单见[正式版证据](evidence/v080/stable-release.md)。Mac 真实 UI 展示 20 张员工卡，无 JavaScript 错误，390px 宽视图无横向溢出。npm 0.8.0 已公开并完成独立官方安装，三个已启用渠道版本已对齐。

公开 GitHub 下载 URL 的 `SHA256SUMS` 已实际下载，字节与本地完全匹配。npm 官方 registry 当前已可见 `0.8.0`；独立官方 registry 安装成功，dist/plugin.js 导入的 name/apply 正确。三个已启用渠道均为 0.8.0；npm 官方 tarball SHA-1、SHA-512 integrity 已匹配，gitHead 对应独立插件源提交。

独立 npm 插件发行源保持 `ae43aa7582ed6dcb57458475a9fde7fe37e4d8f0`，沿用独立源码的 MIT 协议；不是 agent-mailbox 工作台安装器。工作台 Apache-2.0 与插件 MIT 分别保留，TestPyPI/Homebrew 未启用。
