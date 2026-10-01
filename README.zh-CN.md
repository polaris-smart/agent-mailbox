# agent-mailbox

在本机围绕项目管理 AI 员工：派工、共享资料、查看进度、审批操作、验收结果。

**v0.8 从实现开始完全独立**：继续叫 agent-mailbox、继续版本线，但使用自己的入口、数据库、运行组件和项目工具。v0.7 的邮箱命令和后台服务不再随包发行。当前为 **0.8.0b3 本地 Beta 3**，尚未发布到 GitHub Release 或 PyPI。

[English](README.md) · [PRD 正典](docs/PRD.md) · [上手指南](docs/GITHUB-QUICKSTART.md) · [可选设备](docs/NODES.md) · [维护入口](docs/HANDOVER-CODEX.md)

## 单机开始

在这份 v0.8 源码中，用 Python 3.10+：

```sh
python -m venv .venv
# macOS/Linux：
. .venv/bin/activate
# Windows PowerShell 改用：.venv\Scripts\Activate.ps1
python -m pip install .
agent-mailbox --home ~/.agent-mailbox-v08
```

会打开本机浏览器工作台。Beta 使用独立数据目录，避免与旧安装共用。macOS ARM64 可选 app 包自带 Python、Node、执行组件；使用源码不需要 app。目前公共仓库的 pip 版本不是本 Beta，不能用 `pip install agent-mailbox` 取得这一版。

1. 在员工页发现并登记本机已有 app/CLI。
2. 创建项目、选择已有项目目录，从名册添加项目成员。当前能自动执行任务的是 **Codex、Claude 的 CLI 适配器**；发现一个桌面 app 不代表可以自动控制它。
3. 源码安装需 Node.js 22.13+，在页面点“准备运行环境”；agent 登录仍在本机使用原生登录。管理工作台不用额外 LLM 或管理 API key。
4. 入组后在员工详情显式运行接入验证：实际读取项目上下文并写入随机标记笔记；会使用原生模型额度，可选择可用模型。再写任务及交付要求，指派员工。有操作权限请求时由人审批。员工执行结束进入“待验收”，由人通过或退回。

同种 agent 可起多个员工名称，但默认共用本机原生登录，不代表多个独立账号。运行器给受管任务提供项目范围内的 MCP 工具，用户不用为每个员工再配置一次 MCP；不自动接管用户原有 app 对话。

## 已有能力与边界

全局员工名册、项目成员、消息线程、协作请求、任务、进度、显式失败、权限审批、人工验收；项目笔记、实时资料与固定版本、工作日志；暂停和退役员工；可选设备配对。记忆使用私有 SQLite 和文本检索，不需要独立 memory 服务或向量数据库。

默认一个人、一台电脑即可使用。少量用户可通过私网或 SSH 隧道，把 LAN/Ubuntu 服务器作为执行节点接入。主控必须开着且能连通；节点自己的项目目录、agent 登录和代码同步要分别准备。Beta 1 已验证 Mac→Ubuntu 24.04 ARM64 Docker 的真实 HTTPS 节点协议、资料版本一致性、断线恢复及撤销；节点执行测试使用确定性 fixture。Beta 1 的 Codex→Claude 真实模型邮件交接与固定资料读取在 Mac 上通过；Beta 3 原生接入验证、独立修改、同事固定交付审查和显式合入也已通过。香港、硅谷和真实 LAN 实体设备仍需各自部署验收。

应用文件与数据目录分开。同一数据目录的兼容 v0.8 更新保留身份和项目。“关于与更新”提供稳定/Beta 渠道、显式检查、持久暂停接单、空闲检查和私有备份；按识别的安装方式手动替换程序，验证后恢复接单。自动更新/回滚、拖拽 workflow、任意 app 自动唤醒、完整成本治理和 AI ERP 尚未实现。更新备份包含 SQLite 与工作台身份/配置，不含 `home/task-workspaces/`、外部 Git 仓库或供应商原生登录；代码和保留的工作区需另行备份。Beta 1 已验证 Mac ARM64 app 与 Ubuntu ARM64 源码/节点，本轮结果另列证据；Windows 仍属未实测入口，CodeGraph 可选适配器在 Windows 明确不可用。

Apache-2.0 开源，由 NoFox 与贡献者维护；原有 MIT 声明保留在 LICENSES/MIT-Legacy.txt。GitHub 用来发现、讨论和下载；macOS 的签名、公证是发行身份验证，当前可选 app 尚无 Developer ID 签名。

## 资料与协作

登记 PRD、todo、daily update、架构图等文本后，可以查看实时文件或冻结一个团队确认版本。任务启动时固定该时刻已批准的资料清单，更新文件不改变正在执行的任务。员工提交版本是提案，需要 Human 确认；远端员工可显式提交最多 256 KiB 文本，不能覆盖协调端文件。新版从之后启动的任务使用，不自动同步 Git 工作区。

CodeGraph 是可选的已有索引符号检索；archify 单文件 HTML 可隔离预览。AOCI-Code/Graft 没有宣称完整集成。左侧区分团队、当前项目和管理，工作日志记录真实派工、结果、邮件、笔记和版本确认。详见 [Beta 验收](docs/BETA-ACCEPTANCE.md)。

## Beta 3：接入验证、独立工作区、交付验收

查看员工接入检查不启动模型；点击验证创建项目绑定的只读任务，可选模型。成功需实际 `project_context` 与随机标记 `project_note` 工具回执及任务正常结束，不凭员工口头声称。原生登录与执行验证分开；远端接入验证暂不支持，发现 App 不代表已有自动执行适配器。同种员工名称仍默认共享该设备原生账号。

本机修改任务必须选择干净、有提交的 Git 仓库根目录。系统在 `home/task-workspaces/<任务ID>` 建独立 worktree，原目录不直接修改；这不是操作系统沙箱。交付固定文本修改、基线、员工报告和系统事实；未运行的测试不会显示通过。**验收不等于合入**：验收后另行确认合入，校验原仓库基线与干净状态，不自动 commit/push。退回修改创建关联任务，在基线仍一致时继承上一轮固定 patch。

员工可通过本项目的 `project_delivery(task_id)` 读取同事固定交付；受管任务现有 11 个项目 MCP 工具，不任意读取工作区文件。远端修改任务暂时拒绝，远端只读协作继续保留。二进制、符号链接、含凭据、不安全或过大修改不自动合入。工作区保留，不自动清理，也不包含在 SQLite 更新备份中。验证进度和限制见 [Beta 3 证据](docs/evidence/v080/beta3-collaboration.md)。
