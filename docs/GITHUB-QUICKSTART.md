# 从 GitHub 上手 v0.8

当前是未公开发行的 0.8.0b3 Beta 3；获取这一版须用独立源码 checkout 或提供的本机构建产物。产品公开 PyPI 版本不代表本 Beta。

## 单机

Python 3.10+，执行工作需要 Node.js 22.13+。在 v0.8 checkout 建独立 venv，安装本目录：

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install .
agent-mailbox --home ~/.agent-mailbox-v08
```

Windows PowerShell 激活改用 `.venv\Scripts\Activate.ps1`。以上使用独立 v0.8 数据目录。使用自带运行组件的 macOS app 则双击启动；未公证 Beta 可能受 Gatekeeper 阻挡，GitHub 下载不消除系统校验。

在“员工”页发现并登记本机 app/CLI，再创建项目、从名册添加项目成员。普通消息用于同步；选择接收者并点“请求协作”才触发只读任务。必要时点“准备运行环境”。Codex/Claude 在这台电脑完成原生登录后，在员工详情选择项目，显式运行接入验证，可按实际模型列表选择模型；此任务会消耗原生模型额度。查看检查不调用模型，验证成功依赖实际上下文和随机标记笔记回执，不是员工口头报告。远端 probe 暂不支持。管理工作台不要求额外模型账号。准备组件会下载 package-lock 固定依赖，不修改全局 CLI 或登录。

CLI 可以在关闭工作台后显式准备同一个 home：

```sh
agent-mailbox prepare --home ~/.agent-mailbox-v08
agent-mailbox --home ~/.agent-mailbox-v08
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

Beta 1 验证平台为 macOS ARM64 app 与 Ubuntu 24.04 ARM64 源码节点。Windows 可以尝试源码入口，但未完成实机验收，CodeGraph 适配器明确不支持。

## 修改、退回和合入

选择“允许修改”前，确认项目路径是有提交、无未提交/未跟踪文件的 Git 仓库根目录。员工在独立 worktree 修改，原目录保持不动；工作区并非 OS 沙箱，原生账号仍共享。远端修改任务暂不支持，不能当作已有隔离能力使用。

任务完成后查看固定交付与差异。员工声称测试通过和系统已验证分开显示。验收通过不会自动改原目录；另行点合入并确认，系统要求原仓库仍干净、起始 HEAD 未变，并且 patch 校验通过，不自动 commit/push。需要修改就填写退回说明，建立关联任务，在相同基线继承上一轮 patch。文件保留在 home/task-workspaces/<task-id>，更新备份不包含这些工作区，需自行保留。受管员工可用 `project_delivery(task_id)` 查看本项目同事固定交付。
