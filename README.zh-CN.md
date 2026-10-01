# agent-mailbox

**把已有 AI Agent 组成项目团队的本地工作台。**

让你已经在用的 Codex、Claude Code 围绕同一个项目共享资料、邮件交接、分工协作。你看交付、做验收，再决定是否把代码变更合入。项目记录留在自己的电脑上，工作台不需要额外的管理 LLM 账号。

[English](https://github.com/polaris-smart/agent-mailbox/blob/main/README.md) · [上手指南](https://github.com/polaris-smart/agent-mailbox/blob/main/docs/GITHUB-QUICKSTART.md) · [发行渠道](https://github.com/polaris-smart/agent-mailbox/blob/main/docs/RELEASE-CHANNELS.md) · [PRD 正典](https://github.com/polaris-smart/agent-mailbox/blob/main/docs/PRD.md)

## 发现 → 入组 → 接入 → 协作 → 验收

**v0.8.0 通过项目邮箱 MCP 接入既有员工会话。下载可用性、文件和校验值以实际发行页为准。**

1. **发现已有员工。** 登记桌面 App、CLI，再创建项目、加入成员并填写职责。职责是分工说明，不额外授予权限。
2. **接入正在使用的会话。** 在工作台为员工生成项目邮箱 MCP 配置，导入该 Agent 的 MCP 设置。每份接入绑定员工、项目和会话，不要求另外配置 provider/key，也不用工作台另开 CLI。宿主需要支持导出的 stdio MCP 配置。
3. **共享资料、邮件交流。** 员工在原来的环境读取已批准的 PRD、todo、daily update、架构资料，收发项目邮件并回复。需要让员工主动查信：支持 MCP 不代表闲置会话会自动收到通知或被唤醒；读信不等于接单。
4. **分配邮件任务、验收交付。** 邮件任务是默认路径。员工明确接受时固定已批准的资料版本，完成后提交结果，进入 Human 验收。提交不等于完成或验收通过；Human 可以通过或退回为关联任务。这个流程不启动 CLI、不自动修改原项目。

项目接入由主 Agent 保管；子代理向负责人汇报。若把同一接入交给子代理，它的调用会归到同一登记会话，系统不能独立核验宿主内部实际作者。

**受管 CLI 执行保留为独立选项。** Codex、Claude Code CLI 可以由工作台启动任务，运行器提供项目工具；本机修改任务使用独立 Git worktree、保存固定 diff。应用到原仓库需要另行确认，工作台不自动 commit 或 push。这与接入原有 App/CLI 的邮箱是两条不同路径。

既有会话邮箱目前只支持登记在本机的员工。可选远端执行节点继续使用现有协议，不代表已打通远端 App 的新邮箱接入。

## 安装 v0.8.0

**版本：`0.8.0`。** 在 [GitHub 发行页](https://github.com/polaris-smart/agent-mailbox/releases/tag/v0.8.0)核对文件和校验值。只有检查通过、文件实际上传后才有可下载的发行版；[渠道与门槛](https://github.com/polaris-smart/agent-mailbox/blob/main/docs/RELEASE-CHANNELS.md)区分发行准备和已发布。

| 你的电脑 | 原生下载 |
| --- | --- |
| Apple Silicon Mac | [Agent-Mailbox-0.8.0-darwin-arm64.zip](https://github.com/polaris-smart/agent-mailbox/releases/download/v0.8.0/Agent-Mailbox-0.8.0-darwin-arm64.zip) |
| Windows x64 | [Agent-Mailbox-0.8.0-win32-x64.zip](https://github.com/polaris-smart/agent-mailbox/releases/download/v0.8.0/Agent-Mailbox-0.8.0-win32-x64.zip) |
| Linux x64 | [Agent-Mailbox-0.8.0-linux-x64.tar.gz](https://github.com/polaris-smart/agent-mailbox/releases/download/v0.8.0/Agent-Mailbox-0.8.0-linux-x64.tar.gz) |

完整解压后启动 **Agent Mailbox**，不要只移动其中的可执行文件。原生包自带 Python、Node 和锁定的任务运行组件。macOS 包没有 Developer ID 签名和公证，Windows 下载可能显示信誉提示，详见[安装与升级步骤](https://github.com/polaris-smart/agent-mailbox/blob/main/docs/BETA-INSTALL.md)。本版不提供 Intel Mac 或 Windows ARM 原生包。

喜欢 Python 安装？使用 Python 3.10+ 和独立虚拟环境。使用下方精确版本安装，包是否可用以索引为准：

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install 'agent-mailbox==0.8.0'
agent-mailbox --home ~/.agent-mailbox-v08
```

Windows 改用 `py -m venv .venv` 和 `.venv\Scripts\Activate.ps1`。下方精确版本命令须待公开后使用，不能以旧稳定版安装替代。Python/源码执行任务需要 Node.js 22.13+，并在页面点“准备运行环境”。已有入口为 [PyPI agent-mailbox](https://pypi.org/project/agent-mailbox/)，没有 TestPyPI 或 Homebrew 发行。已有 npm 包 [dsh-agent-mailbox](https://www.npmjs.com/package/dsh-agent-mailbox) 是独立的 DeepSeek Harness 插件，配套发布目标为 `0.8.0`，公开状态另行核验，不是工作台安装入口；无 scope 的 `agent-mailbox` 属于其他项目。

Codex、Claude Code 使用各自原生登录。查看接入检查不会调用模型，点击接入测试会消耗原生模型额度。如果默认模型不可用，可以明确选择执行服务提供的模型。[上手指南](https://github.com/polaris-smart/agent-mailbox/blob/main/docs/GITHUB-QUICKSTART.md)包含源码安装和第一次协作步骤。

## 与你已有的工具一起工作

- **本地项目记录**：私有数据库、员工信箱、项目笔记、资料提案和批准版本，工作日志来自真实事件。
- **Human 掌握决定权**：操作审批、项目权限、暂停和退役，验收与代码合入分开，失败显示原因。
- **代码交接**：本机修改任务要求干净、有提交的 Git 仓库根目录；使用独立 worktree 和固定交付。工作区隔离改动，但不是操作系统沙箱。
- **可选跨设备**：显式配对的 HTTPS 节点支持只读协作；每台设备保留自己的 agent 登录和项目 checkout，配对不自动同步文件。
- **可选资料工具**：CodeGraph 已有索引检索、archify 单文件 HTML 隔离预览；结构化本地记忆使用文本搜索，无需独立向量数据库。

同种工具的多个员工名称默认共享该设备原生登录。发现桌面 app 不代表能自动控制它。v0.8 替代的是软件发行，不会自动迁移 v0.7 数据库、后台服务或配置，旧 MCP 配置也不兼容新的项目工具入口；保留旧数据备份，不要让 v0.8 直接使用 v0.7 数据目录。

## 继续了解

[完整上手指南](https://github.com/polaris-smart/agent-mailbox/blob/main/docs/GITHUB-QUICKSTART.md) · [Beta 验收与平台边界](https://github.com/polaris-smart/agent-mailbox/blob/main/docs/BETA-ACCEPTANCE.md) · [Beta 3 验证证据](https://github.com/polaris-smart/agent-mailbox/blob/main/docs/evidence/v080/beta3-collaboration.md) · [可选设备接入](https://github.com/polaris-smart/agent-mailbox/blob/main/docs/NODES.md) · [安全说明](https://github.com/polaris-smart/agent-mailbox/blob/main/SECURITY.md)

更新目前提供显式检查、暂停接单和私有备份，程序替换及恢复仍手动完成。数据库备份之外，还需保留项目仓库和独立工作区。跨设备修改隔离、自动更新、万能 app 控制、拖拽 workflow 编辑器和 AI ERP 尚未实现；完整限制集中在验收文档中。

© 2026 NoFox 与贡献者 · [Apache-2.0](https://github.com/polaris-smart/agent-mailbox/blob/main/LICENSE) · [NOTICE](https://github.com/polaris-smart/agent-mailbox/blob/main/NOTICE) · [原有 MIT 声明](https://github.com/polaris-smart/agent-mailbox/blob/main/LICENSES/MIT-Legacy.txt)
