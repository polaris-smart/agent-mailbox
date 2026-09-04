# agent-mailbox

**给每一个本地 AI Agent 一个专属信箱。**

一个 MCP server。注册一次，即可与本机任意 Agent 互发消息。不用 cron。不用轮询守护进程。不用共享 markdown 文件。不上云。

📖 **文档**: [English](README.md) · [中文](README.zh-CN.md) · [日本語](README.ja.md) · [Español](README.es.md) — [架构图](docs/architecture.html)

![agent-mailbox 架构图](docs/architecture.png)

```bash
uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox          # stdio transport，任何 MCP 宿主即插即用
uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox --http 8642   # 或走 HTTP，供远程 Agent 使用
```

---

## 问题

你的编码 Agent、运维 Agent、评审 Agent —— 全部跑在同一台机器上，个个身怀绝技 —— 却互相说不上话。于是**你**成了传话筒：从一个终端复制结论，往另一个终端粘贴指令，手工转发状态更新。

文件式变通（共享 markdown「日志」、`dropped-notes/` 文件夹）最终腐烂成一堆读不下去的流水账。cron 轮询式变通在空轮询上白白烧 token。云端中继则把你的工作流数据放到别人的 API 后面。

## 解法

一个**本身就是普通工具**的信箱：

| 工具 | 功能 |
|---|---|
| `mailbox_register` | 认领你的信箱。幂等。 |
| `mailbox_send` | 发给一个 Agent、一组 Agent，或 `"all"` 全员广播。 |
| `mailbox_check` | 拉取待读消息 —— 读取即自动 ack。 |
| `mailbox_reply` | 线程内回信，自动路由给原发件人。 |
| `mailbox_list` | 按状态浏览（`pending` / `acked` / `done`）。 |
| `mailbox_done` | 标记办结；done 消息自动归档。 |
| `mailbox_broadcast` | 一次调用，送达所有已注册 Agent。 |
| `mailbox_whoami` | 谁已注册、邮件根目录在哪。 |

消息就是普通 JSON，生命周期极简：`pending → acked → done`。收件人离线时消息静静等着 —— 邮件就该像邮件。

## 快速开始

**Hermes** (`~/.hermes/config.yaml`)：

```yaml
mcp:
  servers:
    agent-mailbox:
      command: uvx
      args: ["git+https://github.com/polaris-smart/agent-mailbox"]
```

**Claude Code** (`~/.claude/settings.json`)：

```json
{ "mcpServers": { "agent-mailbox": { "command": "uvx", "args": ["--from", "git+https://github.com/polaris-smart/agent-mailbox", "agent-mailbox"] } } }
```

**任何 MCP 客户端** (stdio)：

```bash
uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox
```

**远程 Agent**（比如另一台服务器上的 Agent）：

```bash
uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox --http 8642   # 在邮件主机上
```

```json
{ "mcpServers": { "agent-mailbox": { "url": "http://your-host:8642/mcp" } } }
```

## 等信（不轮询）

Agent 不需要轮询。`mailbox_wait` 阻塞长轮询直到消息到达 —— 在一轮的最后调用它，下一条消息立刻唤醒你的 Agent：

```json
{ "tool": "mailbox_wait", "arguments": { "timeout_seconds": 25 } }
```

对人类和仪表盘，配套的 watcher 把每条新消息按 JSON 行打印，还能给指定 Agent 发 macOS 通知：

```bash
uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox-watch --notify boss      # macOS 通知中心
agent-mailbox-watch --once                     # 单次扫描（适合 cron）
```

## 设计

- **Local-first** —— 普通 JSON 文件存放在 `~/.agent-mail/`。不用 SMTP、不用 IMAP、不用域名、不用云中继、不联网（除非你主动开启 HTTP transport）。
- **零依赖** —— 纯 Python 标准库。一个约 500 行的代码库，读一遍就懂。
- **MCP-native** —— 不是又一个 CLI 插件：任何 MCP 宿主都能注册。
- **注册制身份** —— `mailbox_register` 一次，永久持久身份；不会因会话结束而消失。

本地信箱解决同机与可信局域网内的协作。当需要跨组织的真实邮件基础设施投递时，把协议语义升级到 [AAMP](https://github.com/larksuite/aamp)（Agent Asynchronous Messaging Protocol）—— agent-mailbox 的消息生命周期就是按能干净映射到它而设计的。

## 与邻居们的对比

| | agent-mailbox | [cc2cc](https://github.com/non4me/cc2cc) | [agent-talk](https://github.com/xhluca/agent-talk) | 邮件系工具 |
|---|---|---|---|---|
| 外部服务 | **无** | Claude Code channels | retalk relay | SMTP / IMAP / 云 |
| 离线可用 | **是** | 是 | 需要 relay | 否 |
| 持久身份 | **注册一次** | 会话绑定 | 邀请码 | 按 provider |
| 任意 MCP 客户端 | **是** | 仅 Claude Code | 六个 CLI、仅插件 | 是 |
| 人类可读存储 | **纯 JSON** | JSON | relay 侧 | 邮箱导出 |

## 安全须知

- 邮件根目录在你的家目录下；除非你主动在可信网络开启 HTTP transport，消息永不离开本机。
- Agent id 严格校验（`[A-Za-z0-9_-]`，≤64 字符）—— 无路径穿越。
- 存储面向追加、原子写入加文件锁；写入方崩溃不会损坏注册表。
- 防篡改回执（ed25519 签名）在路线图上 —— 模式参考 [agenttransfer](https://github.com/shehryarsaroya/agenttransfer)。

## 路线图

- **v0.1.0**（当前）—— 同机 Agent 信箱，stdio MCP，零基础设施。含长轮询 `mailbox_wait` 与配套 watcher —— 无需轮询守护进程。
- **v0.2.0** —— 联邦化：为其他机器上的 Agent 提供 streamable HTTP transport（Tailscale/局域网友好）。
- **v0.3.0** —— 签名回执（ed25519），投递防篡改。
- **v1.0.0** —— AAMP 桥：本地线程升级为跨组织邮件，走 [AAMP 协议](https://github.com/larksuite/aamp)。

姊妹项目：[dsh-devices](https://github.com/polaris-smart/dsh-devices) 管理你的设备；agent-mailbox 管理设备上 Agent 之间的对话。

## 开发

```bash
git clone https://github.com/polaris-smart/agent-mailbox && cd agent-mailbox
pip install -e ".[dev]"
pytest
```

## 许可证

MIT — 见 [LICENSE](LICENSE)。
