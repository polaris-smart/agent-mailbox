# v0.8.0 正式版发行记录

记录日期：2026-10-01。工作台最终发行源提交：`4413fbad7332501590919ed37b378fdb6c666eb8`。本记录区分测试、安装与公开渠道；不能由一个渠道成功推断所有渠道已同步。

## 当前渠道状态

| 渠道 | 版本 | 已确认状态 | 仍待确认 |
| --- | --- | --- | --- |
| PyPI 工作台 | `agent-mailbox==0.8.0` | 已公开；独立环境从官方索引安装，`agent-mailbox --version` 返回 `0.8.0` | 两份发行文件官方 digest 已与本地匹配，最终源为 4413fbad7332501590919ed37b378fdb6c666eb8 |
| GitHub 工作台 | `v0.8.0` | 已公开，非 draft、非 prerelease，Latest 为 v0.8.0；8 资产均 uploaded，官方 digest 与本地逐一匹配 | 下载时对照 SHA256SUMS；本次公开状态与 digest 核验已完成 |
| 本机 Mac App | `0.8.0` | 最终 CI ZIP 制成 DMG 后安装升级，bootstrap 返回 `0.8.0` | 该本机验证不等于所有电脑首次下载体验 |
| npm 配套插件 | `dsh-agent-mailbox@0.8.0` | 独立三平台 CI 通过；发布命令返回 `EOTP`，未公开 | 首次 EOTP 后浏览器认证流程等待返回 E404、失效；待用户二次验证、实际发布与官方安装核验 |

[PyPI 0.8.0](https://pypi.org/project/agent-mailbox/0.8.0/) 是已确认入口；[GitHub v0.8.0](https://github.com/polaris-smart/agent-mailbox/releases/tag/v0.8.0) 同样是已确认的公开入口，tag 指向 `4413fbad7332501590919ed37b378fdb6c666eb8`。插件不是工作台安装包。没有新增 TestPyPI 或 Homebrew 渠道。

## CI 与原生包

源码 `4201b34dad503529d113b904ae67a23be7cc5644` 的 [CI 36865817096](https://github.com/polaris-smart/agent-mailbox/actions/runs/36865817096) 完成 9/9 门禁：六组回归和三平台原生包检查。Mac/Linux 各组 404 passed、4 skipped；Windows 各组 360 passed、48 skipped。跳过项不计为通过。后台线程异常已提升为测试失败，本次无后台警告。

最终发行源 `4413fbad7332501590919ed37b378fdb6c666eb8` 的 [发布工作流 36868116924](https://github.com/polaris-smart/agent-mailbox/actions/runs/36868116924) 首次遇到 Windows Python 3.10 超时，保留该失败历史；随后执行 `rerun --failed`，重跑失败任务后工作流全部成功。PyPI 正式版由该发行流程公开。本记录不把首次超时隐藏成首次全部通过，也不将前一源提交的通过数量伪写为最终重跑的独立统计。

原生包实际解压、启动，验证 12 个既有会话邮箱 MCP 工具和 11 个受管项目 MCP 工具。邮箱 fixture 覆盖身份、批准资料、普通邮件、明确接单、提交与独立 Human 验收；不启动供应商模型。协议检查不能替代真实 ZCode/Hermes/WorkBuddy 等所有 App 的宿主接入、热加载或自动唤醒验收。

## Mac 实际升级

由最终 CI 的 `4413fbad7332501590919ed37b378fdb6c666eb8` ZIP 制成 DMG，升级 `/Applications/Agent Mailbox.app` 后实际启动。bootstrap 返回 `0.8.0`，真实使用数据目录迁移 schema 9→11，保留 20 名员工、2 个项目、1 封邮件、0 个任务。升级前已保存私有一致性 SQLite 备份；不公开备份凭据或私人资料。

这是实际 Mac 保留数据升级证据，与此前同规模数据副本验证区分。应用使用原数据目录，供应商原生登录及其他 App 在用会话没有因此变成已接入邮箱。程序仍通过本机浏览器展示；端口不是员工身份或固定配置。DMG 不等于 Developer ID 签名/公证；本版不提供该发行者签名。

## 独立 npm 插件

插件候选源提交为 `ae43aa7582ed6dcb57458475a9fde7fe37e4d8f0`，独立仓库 [polaris-smart/dsh-agent-mailbox](https://github.com/polaris-smart/dsh-agent-mailbox)。[三平台 CI 36868122488](https://github.com/polaris-smart/dsh-agent-mailbox/actions/runs/36868122488) 通过。插件版本目标为 `0.8.0`，保持 `dsh-agent-mailbox` 包名，不使用其他项目的无 scope `agent-mailbox` 包。

官方 registry 发布返回 `EOTP`，后续 TTY 浏览器认证流程等待后返回 `E404`、认证流程失效，仍需用户二次验证；这是认证流程失败，不表示现有 npm 包不存在。登录成功、CI 通过和本地 pack 均不能代称新版本公开。必须在实际发布后核对官方 registry 的版本、来源、tarball 和独立安装。显式使用 `--registry=https://registry.npmjs.org`，不输出 token；不把 npmmirror 当发布目标。

## 保留边界

默认为现有 App/CLI 导入绑定员工/项目/session 的 stdio MCP，主动查信，不自动唤醒。邮件任务明确接受→提交→Human 验收；职责不增加权限，接受返回/保存批准资料清单，用显式 `version_id` 读取固定版本。受管 Codex/Claude CLI 是独立选项。

v0.7 数据库、后台服务、MCP 配置没有自动迁移；兼容 v0.8 数据升级不能扩展为旧架构自动迁移。真实 HK/US 节点、其他平台原生账号和所有 App 热加载未由上述 fixture 证明。ZCode 隔离 CLI、DeepSeek Harness 无模型检查也不是全部真实 App 协作验收。

[Beta 4 历史发行记录](beta4-release.md) 与 `420d136` 不可变发行物保留；新发行记录不删除旧证据，不覆盖旧 tag 或文件。


## 公开 GitHub 文件与 digest

最终 8 份资产全部上传，官方 asset digest 与本地文件逐一匹配，包括 `SHA256SUMS`。校验下载文件时使用发行页的 [SHA256SUMS](https://github.com/polaris-smart/agent-mailbox/releases/download/v0.8.0/SHA256SUMS)，不将校验和当作发行者签名。

- `Agent-Mailbox-0.8.0-darwin-arm64.dmg`
- `Agent-Mailbox-0.8.0-darwin-arm64.zip`
- `Agent-Mailbox-0.8.0-linux-x64.tar.gz`
- `Agent-Mailbox-0.8.0-win32-x64.zip`
- `agent_mailbox-0.8.0-py3-none-any.whl`
- `agent_mailbox-0.8.0.tar.gz`
- `native-provenance.json`
- `SHA256SUMS`

PyPI 的 wheel 与 sdist 两份官方 digest 亦与本地文件匹配。GitHub Release 非 draft、非 prerelease；公开 `/releases/latest` 指向 `v0.8.0`。npm 仍未发布，不能声称全部渠道同步。

## Mac 浏览器实际检查

真实工作台显示 20 张员工卡，没有 JavaScript 错误；390px 宽视图无横向溢出。此项是本机 UI 证据，不能扩展为所有系统或所有 Agent 宿主热加载验收。

公开 GitHub 下载 URL 的 `SHA256SUMS` 已实际下载，字节与本地完全匹配。npm 官方 registry 当前查询仍为 `0.1.2`，`0.8.0` 未发布，渠道尚未全部对齐。

AOCI rules/overview 仍返回 `index_invalid` / `code_object_path_unresolved`；不能声称索引维护已完成。此项沿用既有诊断，未用公开发行成功掩盖索引错误。
