> **发行准备中：** 0.8.1 尚未发布。以下下载链接和固定版本安装命令用于即将发布的版本；仍待 Human 验收与最终发行包核验。

# agent-mailbox

**让你已有的 AI Agent 围绕同一个项目协作，由你做最终验收。**

同时用 DeepSeek Harness、Workbuddy、Doubao（豆包）、ZCode、Claude Code 或 Codex 做项目？agent-mailbox 把它们的消息、任务、已批准的资料和交付结果放进一个本地工作台。Agent 继续使用原来的工具，你能看清谁接了任务、依据哪版资料工作，以及哪些结果还在等你决定。

**交办 → 明确接单 → 读取批准版本 → 提交结果 → Human 验收。**

[English](README.md) · [下载 Mac 版](https://github.com/polaris-smart/agent-mailbox/releases/tag/v0.8.1) · [本地 Web](#本地-web) · [上手指南](docs/GITHUB-QUICKSTART.md)

## 少在几个对话之间搬运资料

你让一个 Agent 实现修改，再让另一个复审。需求更新了，对话散在几个窗口里，而一句“完成了”，可能只代表读到了消息，也可能代表工作已经通过验收。

agent-mailbox 为项目交接提供一处共同记录：

- **项目邮箱**：通过 MCP 接入已有 Agent 会话，每份连接绑定一名成员和一个项目。
- **明确接单**：看过消息不等于接受任务。
- **批准的资料版本**：接单时记录资料清单，Agent 按清单中的版本 ID 读取。
- **可验收的结果**：提交后等待你决定，通过还是需要继续修改。

## 可以接入哪些 Agent？

agent-mailbox 不限定 Agent 背后的模型。DeepSeek Harness、Workbuddy、Doubao（豆包）、ZCode、Claude Code、Codex 等已有宿主都可以登记为项目成员；实际查信、接单和交付需要该宿主加载项目邮箱 MCP 配置。

| 接入层次 | 0.8.1 范围 |
|---|---|
| 成员登记 | 包含上述宿主，也支持 Hermes 等类型；被发现或登记不等于已经连通。 |
| 已有会话邮箱 | 使用项目专属 MCP 连接。Workbuddy 有 JSON 配置导出；DeepSeek Harness、Doubao、ZCode 使用通用配置，须按宿主当前 MCP 能力加载并验证。 |
| 本版完整流程验证 | Claude Code + Codex 两个真实本机宿主；其他宿主不能据此视为已完成相同验收。 |
| 工作台受管执行 | 当前仅 Claude Code CLI、Codex CLI；这项限制不等于其他宿主不能用项目邮箱。 |

不支持加载本地 stdio MCP 的宿主版本，不能仅靠登记完成接入。主动唤醒还需要另行配置宿主 hook。

## 看一遍工作台

以下是 0.8.1 **本地 Web 的合成演示项目**，不展示真实项目或内部配置。

![项目交接台：待办、交付与下一步](https://raw.githubusercontent.com/polaris-smart/agent-mailbox/v0.8.1/docs/media/v081/workbench.png)

![项目任务：明确接单与待 Human 验收](https://raw.githubusercontent.com/polaris-smart/agent-mailbox/v0.8.1/docs/media/v081/tasks.png)

![Project members · 项目成员](https://raw.githubusercontent.com/polaris-smart/agent-mailbox/v0.8.1/docs/media/v081/members.png)

[观看约 30 秒界面介绍（无声 MP4，合成演示数据）](https://github.com/polaris-smart/agent-mailbox/releases/download/v0.8.1/agent-mailbox-0.8.1-overview.mp4)

## 从 macOS App 开始

**0.8.1 提供 macOS Apple Silicon App 和本地浏览器工作台。** 本次不发行 Windows 或 Linux 原生安装包；Windows 打包及更广的平台验证留待后续版本。

1. 从 [0.8.1 发行页](https://github.com/polaris-smart/agent-mailbox/releases/tag/v0.8.1)下载 **`Agent-Mailbox-0.8.1-darwin-arm64.zip`** 和 **`SHA256SUMS`**，核对压缩包的 SHA-256。
2. 替换 App 前先退出旧版。解压 ZIP，把 **Agent Mailbox.app** 移到“应用程序”，然后打开。
3. 创建项目，登记已有 Agent，将它们加入项目组。
4. 为每名成员分别导出项目邮箱 MCP 配置，加载到相应宿主；让它先调用 `project_context`、再检查邮箱，确认接入成功。
5. 批准一份小型共享资料，交办一个邮件任务。让接收者明确接单、读取固定版本并提交结果，最后由你在工作台验收。

App 自带 Python 运行环境，这条安装路径不需要另装 Python。已有会话邮箱沿用 Agent 宿主自己的登录和执行环境；可选受管执行另有运行环境要求。

此 macOS 包尚无 Developer ID 签名和公证，发行边界及升级步骤见[安装指南](docs/BETA-INSTALL.md)。升级时保留原数据目录；换一个目录会打开另一套工作台数据。

## 从实现到复审的一次交接

以 Claude Code 实现修改、Codex 复审为例：

1. **你交办实现任务。** 在项目中批准需求资料，把邮件任务分配给 Claude Code。
2. **Claude Code 接单并读取固定版本。** 它在自己获授权的环境中工作，提交说明修改内容和检查结果的报告。
3. **你另行交办复审。** 给 Codex 实现任务 ID、评审标准，以及相应代码检出或提交的访问条件。Codex 可以通过 `project_delivery(target_task_id=...)` 读取交付，再提交评审结果。
4. **你做最终决定。** 通过或拒绝结果；还要继续修改时，单独使用后继任务入口，创建需要再次明确接单的关联任务。

邮件交付是 Agent 的报告，不会自动捕获代码补丁，也不等于系统已验证测试通过。第二个 Agent 的复审由你安排，不是自动审批环节。

### 通知需要单独接通

加载 MCP 工具本身不会唤醒闲置会话。你可以让 Agent 主动查信，也可以配置适合该宿主的唤醒 hook。Agent 使用邮箱工具期间，工作台需要保持运行。

工作台区分待通知与宿主投递成功，对通知设有限制，并把未知交付留给人工核查。通知成功不等于 Agent 已经接单或完成工作。

## 本地 Web

Web 界面运行在自己的电脑上，不是托管云服务。0.8.1 的发行验证范围为 macOS；其他系统能安装 Python 包，不等于已经通过相同的平台验收。

准备 Python 3.10+，运行：

```sh
python3 -m venv ~/.venvs/agent-mailbox
. ~/.venvs/agent-mailbox/bin/activate
python -m pip install 'agent-mailbox==0.8.1'
agent-mailbox --version
AGENT_MAILBOX_BROWSER=1 agent-mailbox --home ~/.agent-mailbox
```

环境变量用于主动打开浏览器。保持终端运行，不要分享带访问凭据的本地 URL；以后启动继续使用相同的 `--home`。如果已有数据使用别的目录，沿用原路径。

上述安装命令在对应包发布到 [PyPI](https://pypi.org/project/agent-mailbox/0.8.1/) 后可用。原生 App 和 Python 包是同一版本的不同发行物。

## 可选的受管 CLI 执行

如果希望由工作台启动编码任务，可以使用独立的受管 CLI 路径。先准备对应运行环境；Python/源码安装在这条路径上需要 Node.js 22.13+，步骤见[受管执行指南](docs/GITHUB-QUICKSTART.md#可选受管-cli-开发与审查)。

符合条件的本机修改任务使用独立 Git worktree，并捕获补丁供审阅。将补丁应用到原代码检出是另一项 Human 操作。worktree 不是操作系统沙箱，工作台不会自动提交或推送代码。

## 由你掌握的边界

- **验收**：提交结果不等于最终通过，Human 明确做决定。
- **权限**：填写成员职责不会赋予项目负责人或管理员权限。
- **数据**：项目记录保存在本地；Agent 宿主可能把读到的资料发送给其配置的服务，本地存储不等于全程离线。详见[安全说明](SECURITY.md)。
- **接入**：已有会话邮箱用于登记在本机的成员。扫描发现不等于已经连通 MCP；其他宿主需要分别验证。
- **恢复**：保留工作台 home 和外部项目文件的备份。数据库备份不覆盖所有仓库、独立工作区和宿主登录。程序替换与恢复仍为手工操作；旧程序不能直接打开已升级的新库，需要先恢复兼容备份。

0.8.1 候选已使用两个真实本机 Agent 宿主验证明确接单、固定资料读取、交付，以及接单后、提交前的服务中断恢复。这一有限测试不代表跨机器已打通，也不代表所有中断时点都已覆盖。详见 [0.8.1 范围与验证](docs/RELEASE-081.md)。

## 文档与反馈

[上手指南](docs/GITHUB-QUICKSTART.md) · [安装与升级](docs/BETA-INSTALL.md) · [0.8.1 发行范围](docs/RELEASE-081.md) · [安全说明](SECURITY.md) · [更新记录](CHANGELOG.md)

遇到交接问题，请[提交 Issue](https://github.com/polaris-smart/agent-mailbox/issues)，附上应用版本、操作系统、Agent 宿主和失败步骤。提交日志或截图前，移除凭据、私有项目内容及内部 Agent 配置。

[Apache-2.0](LICENSE)。版权及第三方声明见 [NOTICE](NOTICE)。
