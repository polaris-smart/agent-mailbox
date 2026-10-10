# 从 GitHub 上手 v0.8.2


把已有 AI Agent 组成项目团队的本地工作台。默认从单机开始：发现 → 入组 → 协作 → 验收。

目标版本为 `0.8.2`，[GitHub 下载入口](https://github.com/polaris-smart/agent-mailbox/releases/tag/v0.8.2) 与 [PyPI 安装入口](https://pypi.org/project/agent-mailbox/0.8.2/) 可用性以实际发行页和索引为准；以下 clone/install 命令要求对应 tag/包存在。独立配套 npm 插件不是工作台安装器，不随本轮自动改号或发布。TestPyPI、Homebrew 不列入本轮发行。

原生包和 PyPI 的安装、来源校验及保留数据升级见[安装与升级](BETA-INSTALL.md)。以下为源码入口。

## 单机

Python 3.10+，执行工作需要 Node.js 22.13+。在 v0.8 checkout 建独立 venv，安装本目录：

```sh
git clone --branch v0.8.2 --single-branch https://github.com/polaris-smart/agent-mailbox.git agent-mailbox-v08
cd agent-mailbox-v08
python3 -m venv .venv
. .venv/bin/activate
python -c "import runpy; runpy.run_path('scripts/build-workbench.py')['write_build_info']()"
python -m pip install .
AGENT_MAILBOX_BROWSER=1 agent-mailbox --home ~/.agent-mailbox-v08
```

产品、仓库、Python 包与 CLI 都保持 `agent-mailbox` 名称，v0.8 将替代旧发行线。当前源码验证使用专用 venv 和独立 v0.8 数据目录；v0.7 数据库、后台服务与配置没有自动迁移，旧 MCP 配置不兼容新的项目工具入口，不要把旧数据目录直接交给新程序。使用自带运行组件的 macOS app 则双击启动；未公证 App 可能受 Gatekeeper 阻挡，GitHub 下载不消除系统校验。

在“员工”页发现并登记本机 app/CLI，再创建项目、从名册添加项目成员。普通消息用于同步；选择接收者并点“请求协作”才触发只读任务。必要时点“准备运行环境”。Codex/Claude 在这台电脑完成原生登录后，在员工详情选择项目，显式运行接入验证，可按实际模型列表选择模型；此任务会消耗原生模型额度。查看检查不调用模型，验证成功依赖实际上下文和随机标记笔记回执，不是员工口头报告。远端 probe 暂不支持。管理工作台不要求额外模型账号。准备组件会下载 package-lock 固定依赖，不修改全局 CLI 或登录。

### 先完成一次协作

默认在员工详情导出项目邮箱 MCP，导入已有 App/CLI。入组和导出不代表真实接入成功；让员工通过工具读取上下文、邮件和批准资料。MCP 配置本身不自动唤醒闲置会话；主动查信或显式接通宿主通知 hook。邮件任务明确 accept→submit→Human review；接受返回并保存 resource_manifest，使用清单 version_id 读取固定资料。受管 CLI 开发/审查是下列独立可选流程。

1. 登记已有 App/CLI，创建项目并加入成员、填写职责。
2. 导出该员工的项目邮箱 MCP 配置，导入宿主；先实际调用上下文和收信工具确认接入。
3. 登记并批准 PRD、todo 等共享资料，发送一封普通邮件并要求回复；普通读信不接单。
4. 创建默认邮件任务，让指定员工明确接受、读取清单的固定资料版本，再提交结果。
5. 查看结果与工作日志，由 Human 验收或退回。员工仍在原环境工作，工作台不替它启动 CLI。

### 可选：受管 CLI 开发与审查

1. 在员工页发现工具，创建项目并加入 Codex、Claude Code CLI 员工。
2. 在“资料与记忆”登记 PRD、todo 等背景，确认团队要使用的版本。
3. 给 Codex 派一个小任务。修改类任务使用干净、有提交的 Git 根目录；只读分析可以先上手。
4. 再给 Claude Code 一个审查任务，让它通过 `project_delivery(task_id)` 读取 Codex 的固定交付并提出意见。
5. 查看 diff 和员工证据。需要修改就写明要求、退回补充；验收通过后，再单独确认是否合入原仓库。

普通邮件不自动触发模型；“请求协作”会创建只读任务。当前是明确派工和交接，不是拖拽 workflow 编排器。运行器为这些受管任务提供项目 MCP 工具；原有桌面 app 对话不会被自动接管。

CLI 可以在关闭工作台后显式准备同一个 home：

```sh
agent-mailbox prepare --home ~/.agent-mailbox-v08
AGENT_MAILBOX_BROWSER=1 agent-mailbox --home ~/.agent-mailbox-v08
```

同一 home 只允许一个工作台或执行节点 owner。退出按钮会停止应用，继续工作时重新运行原命令。`python -m agent_mailbox` 同样进入工作台。`agent-mailbox --version` 显示版本。

## 模型与认证检查

Codex 若返回 `MODEL_UNSUPPORTED`，新建任务时从服务实际提供的模型列表选择一个模型。工作台不会自动修改全局模型设置或换模型重跑。Claude 的原生 `auth status` 与受管 ACP 会话认证是不同检查；会话返回 `AUTH_REQUIRED` 时，需要完成该运行入口支持的认证，不能把原生已登录当成执行已验证。

## 更新与备份

打开“关于与更新”，按需选择稳定版/Beta 并点击“检查更新”；公开发行版可能仍比本地 Beta 旧。点击“准备升级”，等待已有任务及未确认领取/回执结束，再次准备生成私有备份。只有显示准备完成才退出。按页面识别的 App/wheel/源码步骤替换程序，用原 home 启动，核对员工/项目/历史，再点击“恢复接单”。备份位于 home/update-backups，不含项目源码、home/task-workspaces 独立工作区或供应商外部登录，请另行保留。没有自动下载安装、数据库降级或自动回滚。不要删除运行数据来“升级”。

可选服务器用 [无界面节点指南](NODES.md)。单机用户无需部署服务器、配置私网或为每位员工手动安装 MCP。

## 共享资料与工作日志

在当前项目的“资料与记忆”登记 PRD、todo、daily update 等文本。“实时文件”看当前修改，“固定版本”保留确认内容。Human 冻结当前文件即确认该版本；员工提交的版本标为待确认提案，需你查看再确认。任务启动时固定批准版本，已启动任务继续使用旧版；新任务使用新的批准版本。没有批准版本会明确报错，员工选择实时读取时会标记覆盖。

远端员工可通过项目工具显式提交小于 256 KiB 的资料文本作为提案，不能直接覆盖协调端文件。资料 API 读取共享版本，不自动拷贝整个项目或同步 Git。工作日志记录派工、结果、消息、笔记及资料确认；查看不会自动 ACK 或验收。

Beta 1 的 Mac/Ubuntu 记录是历史基线。Beta 4 原生包范围为 macOS ARM64、Windows x64、Linux x64；构建和无模型 HTTP/MCP 检查与实体设备真实模型验收是不同证据。Windows CodeGraph 适配器明确不支持。具体提交的 CI、跳过项和功能边界见[验收记录](BETA-ACCEPTANCE.md)，最终下载状态以对应 GitHub Release 为准。

## 修改、退回和合入

选择“允许修改”前，确认项目路径是有提交、无未提交/未跟踪文件的 Git 仓库根目录。员工在独立 worktree 修改，原目录保持不动；工作区并非 OS 沙箱，原生账号仍共享。远端修改任务暂不支持，不能当作已有隔离能力使用。

任务完成后查看固定交付与差异。员工声称测试通过和系统已验证分开显示。验收通过不会自动改原目录；另行点合入并确认，系统要求原仓库仍干净、起始 HEAD 未变，并且 patch 校验通过，不自动 commit/push。需要修改就填写退回说明，建立关联任务，在相同基线继承上一轮 patch。文件保留在 home/task-workspaces/<task-id>，更新备份不包含这些工作区，需自行保留。受管员工可用 `project_delivery(task_id)` 查看本项目同事固定交付。
