# Beta 4 安装与升级 / Install and upgrade

目标版本 `0.8.0b4`。下载入口是 [polaris-smart/agent-mailbox 的 GitHub Release](https://github.com/polaris-smart/agent-mailbox/releases/tag/v0.8.0b4)，Python 入口是 [PyPI agent-mailbox](https://pypi.org/project/agent-mailbox/)。文件实际上传后才能下载；发布前请使用[源码指南](GITHUB-QUICKSTART.md)。没有 Homebrew、TestPyPI 或 npm 工作台安装入口；已有 `dsh-agent-mailbox` npm 包是 v0.7 的 DeepSeek Harness 插件，尚未适配 v0.8。

## 原生包 / Native package

选择与系统、CPU 一致的文件；完整解压，保留程序旁的文件。运行组件已包含，无需再装 Python 或 Node。

Choose the matching OS and CPU archive. Extract all files and keep the program's folder intact. Python, Node and the task runtime are included.

| 系统 / OS | 文件 / File | 启动 / Launch |
| --- | --- | --- |
| Apple Silicon Mac | `Agent-Mailbox-0.8.0b4-darwin-arm64.zip` | 解压后将 `Agent Mailbox.app` 放到 Applications，启动 / Move the extracted app to Applications and launch |
| Windows x64 | `Agent-Mailbox-0.8.0b4-win32-x64.zip` | 完整解压，运行 `Agent Mailbox/Agent Mailbox.exe` / Extract all files and run the executable |
| Linux x64 | `Agent-Mailbox-0.8.0b4-linux-x64.tar.gz` | 解压后运行 `./Agent Mailbox/Agent Mailbox`（路径含空格，需引号）/ Extract and run the quoted path |

Linux 示例 / Linux example:

```sh
tar -xzf Agent-Mailbox-0.8.0b4-linux-x64.tar.gz
'./Agent Mailbox/Agent Mailbox' --home ~/.agent-mailbox-v08
```

程序打开本机浏览器；服务器无桌面用途见[节点指南](NODES.md)。新用户从员工发现、创建项目和接入验证开始，见[第一次协作](GITHUB-QUICKSTART.md#先完成一次协作)。原生 agent 登录仍由本人在该设备完成。

The launcher opens your local browser. Start by discovering employees, creating a project and verifying execution. Agent sign-in stays native to each device; server-only use is covered by the node guide.

### 来源与校验 / Source and verification

发行页的 `SHA256SUMS` 必须与实际下载文件一致。校验值证明下载字节相同，不替代发行者签名。

Compare your download with the release's `SHA256SUMS`. A checksum verifies bytes; it is not a publisher signature.

```sh
# macOS
shasum -a 256 Agent-Mailbox-0.8.0b4-darwin-arm64.zip
# Linux
sha256sum Agent-Mailbox-0.8.0b4-linux-x64.tar.gz
```

Windows PowerShell:

```powershell
Get-FileHash .\Agent-Mailbox-0.8.0b4-win32-x64.zip -Algorithm SHA256
```

此 Beta 无 Apple Developer ID 签名/公证，也没有 Windows 发行者签名。系统可能阻止启动；核对来源和校验后，按系统自身的安全提示处理。不要全局关闭系统保护。CI 的实际压缩包启动检查不等于已通过所有实体电脑的首次下载体验。Intel Mac、Windows ARM 未提供原生包。

This beta has no Apple Developer ID signature/notarization or Windows publisher signature. Verify the source and checksum before following your operating system's security prompts. Native CI launch checks do not prove every physical computer's first-download experience. Intel Mac and Windows ARM native builds are not provided.

## Python / PyPI

Python 3.10+；任务执行需 Node.js 22.13+。在专用虚拟环境中安装对应预发布版本；它实际上传前，此命令不会成功。

Use Python 3.10+ and a dedicated environment; task execution needs Node.js 22.13+. Install the exact beta once it is available:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install 'agent-mailbox==0.8.0b4'
agent-mailbox --home ~/.agent-mailbox-v08
```

Windows 使用 `py -m venv .venv` 和 `.venv\Scripts\Activate.ps1`。页面“准备运行环境”下载锁定组件。普通 `pip install agent-mailbox` 只选择稳定版本，不能用它代替指定 Beta 的安装验证。

On Windows use `py -m venv .venv` and `.venv\Scripts\Activate.ps1`. Choose **Prepare runtime** in the UI to download locked components. A plain stable installation does not select the beta.

## 从已有 v0.8 升级 / Upgrade an existing v0.8

1. “关于与更新”中检查所选版本，点击“准备升级”，等待任务和未确认回执结束，确认私有备份已完成。
2. 记录当前数据目录；项目仓库、独立任务 worktree 和供应商登录不在该备份里，请另外保留。
3. 退出应用。原生包替换程序及其完整目录；Python 在原环境运行 `python -m pip install --upgrade 'agent-mailbox==0.8.0b4'`。
4. 用原数据目录启动，核对版本、员工、项目和历史，再恢复接单。失败时停止使用，恢复匹配的旧程序和数据备份；没有自动回滚。

Prepare the upgrade in **About & updates**, wait for tasks and receipts, and verify the private backup. Back up repositories and task worktrees separately. Quit, replace the entire native program folder or upgrade the original Python environment, then start with the same home. Verify version, employees, projects and history before resuming. Recovery is manual.

## v0.7 用户 / v0.7 users

v0.8 替代旧软件发行，但没有 v0.7 数据库、后台服务或 MCP 配置自动迁移。保留旧数据；停止旧服务，在新的 v0.8 home 创建团队，不要把旧目录直接传给新程序。旧项目 MCP 配置不适用于新项目工具入口。

v0.8 replaces the software distribution, with no automatic migration of v0.7 databases, background services or MCP configuration. Keep old data, stop the old service, and use a separate v0.8 home. Old project MCP configuration is incompatible with the new project tools.
