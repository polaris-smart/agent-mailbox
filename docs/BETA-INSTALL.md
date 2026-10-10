# 0.8.2 安装与升级 / Install and upgrade

安装版本：**0.8.2**。macOS Apple Silicon 为正式 App 路径；Intel、Windows x64、Linux x64 为新增预览平台，具体范围见 [0.8.2 说明](RELEASE-082.md)。

## macOS Apple Silicon App

1. 从 [v0.8.2 Release](https://github.com/polaris-smart/agent-mailbox/releases/tag/v0.8.2) 下载 `Agent-Mailbox-0.8.2-darwin-arm64.zip` 与 `SHA256SUMS`。
2. 核对文件名及 SHA-256，再解压。升级前先退出旧 App 和使用同一 home 的服务，保留旧程序以便回滚。
3. 将完整 `Agent Mailbox.app` 移到 Applications 后打开。界面在 App 自身窗口中显示。

```sh
shasum -a 256 Agent-Mailbox-0.8.2-darwin-arm64.zip
```

Download the Mac ZIP and checksums from the release page. Quit the old instance before replacing it, verify the archive, and move the complete App to Applications. The interface opens in the App's own window; its Python runtime is included.

本版没有 Developer ID 签名或 Apple 公证。校验值只能核对字节，不能替代发行者签名；核实来源后按系统安全提示处理，不要全局关闭系统保护。Intel Mac、Windows 和 Linux 包为预览支持，完整 GUI、历史升级与真实 Agent 验收尚未在这些新平台全部完成。

The Mac package is not Developer ID signed or notarized. Follow your operating system's security prompts after verifying the source; do not disable system protection globally. Intel Mac, Windows and Linux packages are preview support; native CI does not establish full GUI, historical upgrade or real-agent acceptance on those platforms.

## 新增预览包 / Additional preview packages

- **Intel Mac**：下载 `Agent-Mailbox-0.8.2-darwin-x64.zip`，按上面的 App 步骤安装。
- **Windows x64**：下载 `Agent-Mailbox-0.8.2-win32-x64.zip`，完整解压，运行文件夹内的 `Agent Mailbox.exe`。
- **Linux x64**：下载 `Agent-Mailbox-0.8.2-linux-x64.tar.gz`，完整解压，在解压后的文件夹运行 `./Agent\ Mailbox`。构建基线为 Ubuntu 24.04，未验证更老发行版。

Windows/Linux 为本地服务＋浏览器工作台，不包含 macOS 同款内嵌外壳。程序输出的访问链接带有本机访问凭据，不能公开分享。请保留整个解压目录，不能只复制可执行文件。

## npm 启动入口 / npm launcher

Node.js 22.13+：

```sh
npx @polaris-smart/agent-mailbox@0.8.2 --version
npx @polaris-smart/agent-mailbox@0.8.2
```

首次启动从 GitHub Release 下载匹配平台的完整程序，校验固定 SHA-256 后缓存；成品包含 Python。上文的平台和预览边界仍然适用。切换前退出旧实例，自定义数据目录继续使用原来的 `--home`。不需要覆盖已有 Python 安装提供的同名命令。

The official [scoped npm package](https://www.npmjs.com/package/@polaris-smart/agent-mailbox) downloads and verifies the matching native bundle on first launch, including its Python runtime. GitHub downloads must be reachable. The platform and preview boundaries above still apply. Quit the old instance and keep your existing custom `--home` when switching versions. The unscoped npm name belongs to an unrelated package; `dsh-agent-mailbox` is a separate integration.

## 本地 Web / Local Web

使用 Python 3.10+ 和专用虚拟环境。以下为 macOS/Linux 的 shell 命令；Windows 用户可先使用预览原生包。不宣称各平台已经完成相同程度的人工验收。

```sh
python3 -m venv ~/.venvs/agent-mailbox
. ~/.venvs/agent-mailbox/bin/activate
python -m pip install 'agent-mailbox==0.8.2'
agent-mailbox --version
AGENT_MAILBOX_BROWSER=1 agent-mailbox --home ~/.agent-mailbox
```

`AGENT_MAILBOX_BROWSER=1` 明确请求打开浏览器；不设置时，CLI 通常只打印本地访问 URL。保持终端运行，不要分享 URL 中的访问凭据。该 Web 服务运行在本机，不是云托管服务。

The environment flag explicitly requests a browser window. Keep the terminal running and the access URL private. Reuse the same `--home`; if your existing workbench uses a different path, keep it.

已有会话邮箱沿用宿主自身登录。可选受管执行需要另外准备运行组件；Python/源码方式在受管路径上要求 Node.js 22.13+，不应把这项要求混为普通邮箱接入要求。本次没有 Homebrew 或 TestPyPI 发行。npm 工作台入口 `@polaris-smart/agent-mailbox@0.8.2` 已公开；独立的 `dsh-agent-mailbox` 插件不是工作台安装器，不要混用不带 scope 的同名第三方包。

## 保留数据升级 / Upgrade without changing the home

1. 记录当前 home，备份该目录及外部项目仓库/文件。任务 worktree、项目文件和宿主登录并不都在数据库备份内。
2. 退出当前工作台；同一个 home 只启动一个实例。
3. 替换 App，或在原虚拟环境中更新 Python 包：`python -m pip install --upgrade 'agent-mailbox==0.8.2'`。
4. 沿用原 home 启动，核对界面版本、项目、成员、任务和资料；不要为了升级创建一个空 home 再误以为数据丢失。

The official 0.8.0 → 0.8.1 path and backup restoration were checked for the prior release. For 0.8.2, a nonempty synthetic home created by the actual 0.8.1 binary was reopened by the candidate with projects, members, tasks and memories unchanged. There is no new schema migration in this patch. These are bounded checks, not proof for every user database. See [scope and evidence boundaries](RELEASE-082.md).

## 回滚 / Rollback

停止新版后，再恢复与旧程序兼容的升级前数据库及配套 home 文件。外部项目文件独立恢复。旧程序会拒绝直接打开较新的数据库，不能通过手工降低 schema 版本号绕过。

Stop the new instance before restoring. Restore the compatible pre-upgrade database together with its supporting home files, and restore external project files separately as needed. Do not lower the schema number to force an older binary to open a newer database.

v0.7 数据库、服务和 MCP 配置没有自动迁移路径；保留它们的备份，为 v0.8 使用单独目录。更新检查与备份不等于自动安装更新，程序替换和恢复仍需显式操作。
