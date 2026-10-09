# 0.8.1 安装与升级 / Install and upgrade

**发行准备中，尚未发布 / Release preparation; not published.** 下载与固定版本安装命令在正式文件上传后可用。当前范围为 macOS Apple Silicon App + macOS 本地 Web。

## macOS Apple Silicon App

1. 从 [v0.8.1 Release](https://github.com/polaris-smart/agent-mailbox/releases/tag/v0.8.1) 下载 `Agent-Mailbox-0.8.1-darwin-arm64.zip` 与 `SHA256SUMS`。
2. 核对文件名及 SHA-256，再解压。升级前先退出旧 App 和使用同一 home 的服务，保留旧程序以便回滚。
3. 将完整 `Agent Mailbox.app` 移到 Applications 后打开。界面在 App 自身窗口中显示。

```sh
shasum -a 256 Agent-Mailbox-0.8.1-darwin-arm64.zip
```

Download the Mac ZIP and checksums from the release page. Quit the old instance before replacing it, verify the archive, and move the complete App to Applications. The interface opens in the App's own window; its Python runtime is included.

本版没有 Developer ID 签名或 Apple 公证。校验值只能核对字节，不能替代发行者签名；核实来源后按系统安全提示处理，不要全局关闭系统保护。Intel Mac、Windows 和 Linux 原生包不在本次发行范围。

The Mac package is not Developer ID signed or notarized. Follow your operating system's security prompts after verifying the source; do not disable system protection globally. Intel Mac, Windows and Linux native packages are not included in this release.

## 本地 Web / Local Web

使用 Python 3.10+ 和专用虚拟环境。以下命令用于 macOS；本版不宣称其他系统已经完成相同验收。

```sh
python3 -m venv ~/.venvs/agent-mailbox
. ~/.venvs/agent-mailbox/bin/activate
python -m pip install 'agent-mailbox==0.8.1'
agent-mailbox --version
AGENT_MAILBOX_BROWSER=1 agent-mailbox --home ~/.agent-mailbox
```

`AGENT_MAILBOX_BROWSER=1` 明确请求打开浏览器；不设置时，CLI 通常只打印本地访问 URL。保持终端运行，不要分享 URL 中的访问凭据。该 Web 服务运行在本机，不是云托管服务。

The environment flag explicitly requests a browser window. Keep the terminal running and the access URL private. Reuse the same `--home`; if your existing workbench uses a different path, keep it.

已有会话邮箱沿用宿主自身登录。可选受管执行需要另外准备运行组件；Python/源码方式在受管路径上要求 Node.js 22.13+，不应把这项要求混为普通邮箱接入要求。没有 Homebrew、TestPyPI 或 npm 工作台安装命令；独立的 `dsh-agent-mailbox` 插件不是工作台安装器。

## 保留数据升级 / Upgrade without changing the home

1. 记录当前 home，备份该目录及外部项目仓库/文件。任务 worktree、项目文件和宿主登录并不都在数据库备份内。
2. 退出当前工作台；同一个 home 只启动一个实例。
3. 替换 App，或在原虚拟环境中更新 Python 包：`python -m pip install --upgrade 'agent-mailbox==0.8.1'`。
4. 沿用原 home 启动，核对界面版本、项目、成员、任务和资料；不要为了升级创建一个空 home 再误以为数据丢失。

The candidate's official 0.8.0 → 0.8.1 upgrade and backup restoration were checked with a populated synthetic home. That validates a bounded migration path, not every possible user database. See [scope and evidence boundaries](RELEASE-081.md).

## 回滚 / Rollback

停止新版后，再恢复与旧程序兼容的升级前数据库及配套 home 文件。外部项目文件独立恢复。旧程序会拒绝直接打开较新的数据库，不能通过手工降低 schema 版本号绕过。

Stop the new instance before restoring. Restore the compatible pre-upgrade database together with its supporting home files, and restore external project files separately as needed. Do not lower the schema number to force an older binary to open a newer database.

v0.7 数据库、服务和 MCP 配置没有自动迁移路径；保留它们的备份，为 v0.8 使用单独目录。更新检查与备份不等于自动安装更新，程序替换和恢复仍需显式操作。
