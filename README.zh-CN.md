# agent-mailbox

**把已有 AI Agent 组成项目团队的本地工作台。**

让你已经在用的 Codex、Claude Code 围绕同一个项目共享资料、邮件交接、分工协作。你看交付、做验收，再决定是否把代码变更合入。项目记录留在自己的电脑上，工作台不需要额外的管理 LLM 账号。

[English](README.md) · [上手指南](docs/GITHUB-QUICKSTART.md) · [发行渠道](docs/RELEASE-CHANNELS.md) · [PRD 正典](docs/PRD.md)

## 一个项目，两名员工，一次验收

让 **Codex** 在独立 Git 工作区修复一个 bug，再让 **Claude Code** 通过项目工具读取固定 diff、提出审查意见。你查看交付，需要修改就填写要求、退回为关联任务；满意后验收。把 patch 应用到原仓库需要另行确认，agent-mailbox 不会自动 commit 或 push。

这是一条可以直接使用的多 Agent 协作流程。你决定每件事交给谁，不必先学习或画一张 workflow 图。

## 发现 → 入组 → 协作 → 验收

1. **发现已有员工。** 登记本机 CLI 和桌面 app，分别查看入口、原生登录与实际执行验证。当前支持自动执行的是 Codex、Claude Code 的 CLI 适配器。
2. **组成项目团队。** 选择项目目录，加入员工，共享 PRD、todo、daily update、笔记和架构资料。任务启动时固定已批准的资料版本。
3. **交代工作。** 显式运行接入测试，再派任务、审批需要的操作。普通项目消息用于交流，明确的“请求协作”才启动工作。运行器提供项目范围内的 MCP 工具，不必给每个受管员工单独配置一次 MCP。
4. **查看和验收。** 查看固定文件、diff 与工作日志，通过验收或退回补充；安全的文本 patch 由你另外确认应用到原仓库。

## 从源码体验 v0.8 Beta

**发行目标：`0.8.0b4`（Beta 4，发行整改中）。** 源码分支已公开，Beta 4 正在发行整改，尚无 Beta 4 GitHub Release 或包渠道发行。我们的 PyPI 当前为 v0.7.6；没有发布过 TestPyPI、Homebrew 或 npm 包。同名无 scope npm 包属于其他项目，不是本工作台安装来源。本轮只向 GitHub Beta 和 PyPI 发布同一产品版本。详见[渠道状态与发行门槛](docs/RELEASE-CHANNELS.md)。

使用 Python 3.10+，并建立专用虚拟环境。产品、仓库、Python 包和 CLI 都保持 `agent-mailbox` 名称，v0.8 将统一替代各渠道旧发行。发行验证完成前，源码测试请与旧安装隔离：

```sh
git clone --branch feat/v080-workbench --single-branch https://github.com/polaris-smart/agent-mailbox.git agent-mailbox-v08
cd agent-mailbox-v08
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
agent-mailbox --home ~/.agent-mailbox-v08
```

命令会打开本机浏览器工作台。源码执行任务需 Node.js 22.13+，并在页面点“准备运行环境”。Codex、Claude Code 使用各自原生登录。查看接入检查不会调用模型；点击接入测试会消耗原生模型额度。如果默认模型不可用，可以明确选择执行服务提供的模型。

macOS ARM64 已有本机构建 App 验证。Windows PowerShell 的激活命令改为 `.venv\Scripts\Activate.ps1`，但 Windows 尚非验收通过的发行目标。GitHub CI 仍有平台失败待修复，本机 Mac 验证不代表全平台 CI 已通过。

## 与你已有的工具一起工作

- **本地项目记录**：私有数据库、员工信箱、项目笔记、资料提案和批准版本，工作日志来自真实事件。
- **Human 掌握决定权**：操作审批、项目权限、暂停和退役，验收与代码合入分开，失败显示原因。
- **代码交接**：本机修改任务要求干净、有提交的 Git 仓库根目录；使用独立 worktree 和固定交付。工作区隔离改动，但不是操作系统沙箱。
- **可选跨设备**：显式配对的 HTTPS 节点支持只读协作；每台设备保留自己的 agent 登录和项目 checkout，配对不自动同步文件。
- **可选资料工具**：CodeGraph 已有索引检索、archify 单文件 HTML 隔离预览；结构化本地记忆使用文本搜索，无需独立向量数据库。

同种工具的多个员工名称默认共享该设备原生登录。发现桌面 app 不代表能自动控制它。v0.8 替代的是软件发行，不会自动迁移 v0.7 数据库、后台服务或配置，旧 MCP 配置也不兼容新的项目工具入口；保留旧数据备份，不要让 v0.8 直接使用 v0.7 数据目录。

## 继续了解

[完整上手指南](docs/GITHUB-QUICKSTART.md) · [Beta 验收与平台边界](docs/BETA-ACCEPTANCE.md) · [Beta 3 验证证据](docs/evidence/v080/beta3-collaboration.md) · [可选设备接入](docs/NODES.md) · [安全说明](SECURITY.md)

更新目前提供显式检查、暂停接单和私有备份，程序替换及恢复仍手动完成。数据库备份之外，还需保留项目仓库和独立工作区。跨设备修改隔离、自动更新、万能 app 控制、拖拽 workflow 编辑器和 AI ERP 尚未实现；完整限制集中在验收文档中。

© 2026 NoFox 与贡献者 · [Apache-2.0](LICENSE) · [NOTICE](NOTICE) · [原有 MIT 声明](LICENSES/MIT-Legacy.txt)
