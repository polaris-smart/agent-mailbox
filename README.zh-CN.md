# agent-mailbox

在本机围绕项目管理 AI 员工：派工、共享资料、查看进度、审批操作、验收结果。

**v0.8 从实现开始完全独立**：继续叫 agent-mailbox、继续版本线，但使用自己的入口、数据库、运行组件和项目工具。v0.7 的邮箱命令和后台服务不再随包发行。当前为 **0.8.0a3 本地 alpha**，尚未发布到 GitHub Release 或 PyPI。

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

会打开本机浏览器工作台。alpha 使用独立数据目录，避免与旧安装共用。macOS ARM64 可选 app 包自带 Python、Node、执行组件；使用源码不需要 app。目前公共仓库的 pip 版本不是本 alpha，不能用 `pip install agent-mailbox` 取得这一版。

1. 选已有项目目录。
2. 添加员工。当前能自动执行任务的是 **Codex、Claude 的 CLI 适配器**；发现一个桌面 app 不代表可以自动控制它。
3. 源码安装需 Node.js 22.13+，在页面点“准备运行环境”；agent 登录仍在本机使用原生登录。管理工作台不用额外 LLM 或管理 API key。
4. 写任务及交付要求，指派员工。有操作权限请求时由人审批。员工执行结束进入“待验收”，由人通过或退回。

同种 agent 可起多个员工名称，但默认共用本机原生登录，不代表多个独立账号。运行器给受管任务提供项目范围内的 MCP 工具，用户不用为每个员工再配置一次 MCP；不自动接管用户原有 app 对话。

## 已有能力与边界

项目、员工、任务、进度、显式失败、权限审批、人工验收；项目笔记、资源链接、工作历史；暂停和退役员工；可选设备配对。记忆使用私有 SQLite 和文本检索，不需要独立 memory 服务或向量数据库。

默认一个人、一台电脑即可使用。少量用户可通过私网或 SSH 隧道，把 LAN/Ubuntu 服务器作为执行节点接入。主控必须开着且能连通；节点自己的项目目录、agent 登录和代码同步要分别准备。协议已做隔离回环验证，不能据此声称香港、硅谷、真实 LAN 或 Ubuntu 已验收。

应用文件与数据目录分开。同一数据目录的兼容 v0.8 更新保留身份和项目；停机后备份数据。自动更新/回滚、拖拽 workflow、独立编辑工作区、任意 app 自动唤醒、完整成本治理和 AI ERP 尚未实现。Windows/Linux 实机发行验收也仍待完成。

MIT 开源。GitHub 用来发现、讨论和下载；macOS 的签名、公证是发行身份验证，当前可选 app 尚无 Developer ID 签名。
