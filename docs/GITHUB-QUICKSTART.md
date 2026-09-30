# 从 GitHub 开始使用 agent-mailbox

GitHub 是源码、问题反馈、文档和版本下载的主入口。本地浏览器工作台是默认使用界面；macOS `.app` 是可选安装包。使用工作台不需要 Apple Developer 账号，也不需要为管理界面配置 LLM。

当前分支是 **v0.8.0a2 开发预览**，尚未 push、打 tag 或公开 Release。下列仓内命令和本地产物用于预览验证，不代表 GitHub 默认分支或 PyPI 已提供 v0.8.0。

## 仓内启动

已有本分支源码与 [uv](https://docs.astral.sh/uv/) 时，在仓库根目录执行：

```sh
uv run agent-mailbox workbench
```

浏览器会打开本机工作台。选择已有项目目录，加入已安装、已登录的 Codex 或 Claude Code，交代工作及验收标准。受管会话的项目 MCP 由应用注入，无需逐个编辑全局配置。

首次实际执行需要 Node.js >=22.13、npm 和受管运行时依赖。源码方式需要先提供 Node/npm，再在工作台点击“准备执行环境”；它会下载固定版本依赖，不更改全局 agent 配置或登录态。当前依赖包含原生 agent 组件，下载体积较大；管理界面可以先打开。自包含 macOS 安装包自带运行时。

## 本地 Python 安装包

本轮构建的 wheel 可以安装到隔离工具环境。将路径替换为实际下载的文件：

```sh
uv tool install /absolute/path/agent_mailbox-0.8.0a2-py3-none-any.whl
agent-mailbox workbench
```

工作台默认数据目录为 `~/.agent-mailbox`。安装包和项目、员工、工作日志、记忆、设备凭据分开保存。升级应用或工具包时保留这个目录即可沿用配置。预览验证使用独立 `--home`，不访问旧版在役邮箱 `~/.agent-mail`。

## macOS 可选安装包

`Agent Mailbox.app` 双击后打开同一工作台，不依赖用户安装 Python/Node/npm。本轮仅构建 macOS ARM64，未做 Developer ID 签名或 Apple 公证；从互联网下载后的 Gatekeeper 首次打开体验仍未验证。Windows/Linux 的桌面包尚未构建。

GitHub Release 可以同时放源码、Python 包和 `.app`。它不要求通过 Mac App Store 发布；macOS 的代码签名与公证是另一条发行流程。

## 升级与退出

工作台提供“退出应用”，停止本实例并清理受管任务进程。关闭浏览器标签页不会自动停止后台服务。替换程序后重新打开，项目身份和配置从原数据目录读取；应用不会接管任意已打开的原生 agent 对话。

本轮验证应用替换和 SQLite 增量迁移保留数据，没有实现自动下载更新、后台自替换或公开回滚渠道。未来的一键更新仍需要真实发行渠道、产物验证和数据回滚验收。

已验证事实与剩余发行门见 [工作台说明](WORKBENCH.md) 和 [验证记录](evidence/v080/verification.md)。
