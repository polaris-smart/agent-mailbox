# agent-mailbox

[![polaris-smart/agent-mailbox MCP server](https://glama.ai/mcp/servers/polaris-smart/agent-mailbox/badges/score.svg)](https://glama.ai/mcp/servers/polaris-smart/agent-mailbox)

**给每一个本地 AI Agent 一个专属信箱。** 一个 stdio MCP server，零守护进程，每封信一个 JSON 文件。外加内建任务看板：卡片移动即唤醒负责人，还有给人类用的零依赖 Web 看板。

📖 **文档**: [English](README.md) · [中文](README.zh-CN.md) · [Español](README.es.md) · [Português](README.pt-BR.md) · [Français](README.fr.md) · [Русский](README.ru.md)

> 🆕 **v0.7.x —— 唤醒升级**：**sampling 唤醒**——信一落箱，server 就经宿主自己的 MCP 连接发 `sampling/createMessage` 调起收件人，附带 per-agent 唤醒策略强制注入（身份模板/禁区硬约束/执行锁——同一 agent 同时只有一个分身在途）；CLI 型 agent（codex 等）走 **local-command 唤醒适配器**（纯 argv、内容走环境变量、killpg 强杀超时），多 agent 共享 wake.json 时可用 `wake run --adapter` 各取所需。sampling 只是加速通道不是送达保证：MCP 已于 2026-07-28 弃用（SEP-2577），v0.7.2 加 per-agent 关停开关（wake.json 里 `"sampling": {"enabled": false}`），信必达铁律始终由 fallback 链（wake-daemon / webhook / 下次 check）兜底。13 个 MCP 工具。上一版：[Wake daemon](#wake-daemon信必达新信落盘即唤醒)

---

## 问题

在同一台机器上跑多个 AI agent——Claude Code、Hermes、你自己的脚本——它们之间没有互相留言的办法。要么互相等待，要么你本人在各个窗口之间复制粘贴，充当人肉交换机。

## 方案

一个信箱就是一个普通 JSON 文件目录：

```
~/.agent-mail/
  registry.json                agent_id → {owner, description, created_at}
  inbox/HS/20260905-….json     每封信一个文件
  archive/HS/…
  tasks.json                   任务看板（{"next_id", "tasks": {id: 卡片}}）
```

Agent 通过一个小型 stdio MCP server 读写它。没有 broker 进程、不开端口、没有数据库、默认零网络。任意多个 MCP 宿主进程共享同一个邮件根目录（文件锁保护）。

![agent-mailbox 架构图](docs/architecture.png)

## 快速开始

**前置条件** —— 一次性：安装 [uv](https://docs.astral.sh/uv/)（macOS/Linux：`curl -LsSf https://astral.sh/uv/install.sh | sh`；Windows：`powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`）。其余全部由 `uvx` 运行，无需再装任何东西。

### 1 · 在你的 MCP 宿主里注册 server

Claude Code：

```bash
claude mcp add agent-mailbox -- uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox
```

通用 MCP 宿主（JSON 配置）：

```json
{
  "mcpServers": {
    "agent-mailbox": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/polaris-smart/agent-mailbox", "agent-mailbox"]
    }
  }
}
```

提示：在该 agent 的环境里设一次 `AGENT_MAIL_ID=HS`（或任意 id），之后所有工具自动以它署名收发，无需每次传 `agent_id`。

### 2 · Agent 注册一次即可

```json
{ "tool": "mailbox_register", "arguments": { "agent_id": "HS", "owner": "Hermes", "description": "PM & QA" } }
```

注册幂等。注册后即可被所有人寻址——包括一个叫 `boss`、由你亲自翻看的人类信箱。

### 3 · 发信、收信、回信

```json
{ "tool": "mailbox_send", "arguments": { "to": "HS", "subject": "deploy ready", "body": "v0.1.0 已就绪，请验收。" } }
{ "tool": "mailbox_check", "arguments": {} }
{ "tool": "mailbox_reply", "arguments": { "msg_id": "20260905-…-hs", "body": "验收通过，标记 done。" } }
```

`mailbox_check` 取走待读信件并标记 `acked`。生命周期：`pending → acked → done`，之后可归档。每封信都是一个可以 `cat` 的 JSON 文件——老板直接看收件箱。

### 4 · 等信而不是轮询

`mailbox_wait` 长轮询阻塞直到有新信——作为回 合的最后一个动作调用：

```json
{ "tool": "mailbox_wait", "arguments": { "timeout_seconds": 25 } }
```

## 任务看板

任务卡存于 `<邮件根>/tasks.json`（纯 JSON，与信件共用同一把文件锁）。状态机严格：`todo→doing→review→done`，非相邻移动被拒（除非 `force=True`）；`done` 为终态。建卡或挪卡都会给负责人发一封普通信箱消息（`[task#t-12 → review] …`）——看板动作经由既有信箱唤醒对应 agent，零轮询、零 webhook。自己挪自己的卡不发信，`notify=False` 可关闭。

**Web 看板（给人类）。** `agent-mailbox --web 8643` 在 `127.0.0.1` 上提供一个零依赖看板（stdlib `http.server` + 单个内嵌 HTML，无框架）：四条泳道对应状态机，拖卡到相邻列即移动，表单可直接建卡；一键切换深浅主题。鉴权用 bearer token——设 `AGENT_MAIL_WEB_TOKEN` 固定之，否则每次启动生成新 token 并打印（打开 `http://127.0.0.1:8643/?token=…`）。看板身份为 `boss`：人类拖卡/建卡同样自动给负责人发信唤醒。页面每 5 秒自动刷新。

## 唤醒离线的 agent（一行配置）

如果收信 agent 根本没在运行，`mailbox_send` 可以在新信落盘的瞬间 POST 一个 webhook——无需守护进程、无需轮询、无需额外进程：

```json
// ~/.agent-mail/webhook.json   (chmod 600)
{ "url": "http://localhost:8644/webhooks/agent-mailbox", "secret": "…" }
```

密钥自己生成一次：`openssl rand -hex 32`。省略则发未签名 POST（本地测试足够；是否强制验签由接收方决定）。

宿主的 webhook 处理器收到：

```json
{ "event": "agent_mailbox_new_message", "event_type": "agent_mailbox_new_message", "message": { "id": "…", "from": "ZC", "to": "HS", "subject": "…", "body": "…" } }
```

……然后唤醒该 agent，agent 到达后调用 `mailbox_check`。集成到此为止。

- 签名 `X-Hub-Signature-256: sha256=<hmac>`（GitHub 格式——Hermes gateway 和多数 webhook 消费方都认）。
- 签名风格可用环境变量 `AGENT_MAIL_SIGNATURE_STYLE` 切换：`github`（默认，`X-Hub-Signature-256: sha256=<hex>`）/ `generic`（`X-Webhook-Signature: <hex>`，无前缀）/ `slack`（`X-Slack-Signature: v0=<hex>`；本实现不发送 ts 字段，接收方请勿按 Slack 完整基串 `v0:ts:body` 校验）。
- 目标被锁定：仅 http/https、默认只允许环回/私网地址、拒绝重定向、绕过系统代理。
- 环境变量 `AGENT_MAIL_WEBHOOK_URL` / `AGENT_MAIL_WEBHOOK_SECRET` 优先于配置文件。不配置 = 完全离线。

## Wake daemon（信必达）：新信落盘即唤醒

上面的 webhook 需要一个已经在监听的网关。wake daemon 补上另一半：**收件侧**——当信落盘时，把 agent 的宿主真正拉进一个会话。一条命令安装：

```bash
agent-mailbox wake install --agent ZC            # 用 webhook.json 作为 POST 目标
agent-mailbox wake install --agent ALICE --adapter claude-code    # 终端响铃 + 桌面通知
agent-mailbox wake install --agent BOB --adapter generic-webhook --webhook-url https://… --webhook-secret …
agent-mailbox wake status / uninstall ZC
```

- **触发方式**：launchd `WatchPaths`（macOS）/ systemd `PathChanged=` path unit（Linux）监听 `~/.agent-mail/inbox/<agent>/`；任何变更都会启动一轮清信（`python -m agent_mailbox.wake run --once`）。
- **适配器**：`hermes`（POST 网关 webhook；签名风格自动适配 github/generic/slack）、`generic-webhook`（你的 URL + 密钥）、`claude-code`（终端响铃 + 桌面通知；更丰富的 hooks 预留中）。未知取值回落 hermes——唤醒胜过沉默。
- **可靠性三件套**（2026-09-22 事故复盘产品化）：POST 失败在一轮内按 60s×5 重试，仍失败则**不标记**该信，由下一轮文件监听触发补投——唤醒侧永不丢信；信件 `handled_log` 中的 `wake` 条目保证每封信**至多唤醒一次**（幂等）；计数口径 v2 对全部 `pending` 加上超过 600s 的 `acked` 信件唤醒（处理方半途死掉的情形），刚 acked 的新信保持安静。
- **fail-open 铁律**：daemon 是独立进程，只读信件文件、经 store 加锁 API 追加。wake 挂了，信照常落盘，下一次 `mailbox_check` 照常送达——发送路径上没有任何东西依赖它。
- **Jev 路由器（可选，默认关闭）**：在 `<邮件根>/wake.json` 设 `"jev": {"enabled": true, "api_key": …, "endpoint": …}`（或 `wake install --jev --jev-api-key …`）。信件异步打分（Noul 门控：现在有人需要行动吗？分数：紧急度 0–10，低于阈值归入每日摘要），不阻塞唤醒主路径；Jev 超时/出错立即回落「有信即醒」。决策连同分数记入 `<邮件根>/wake-jev.log`；`api_key` 绝不进日志。纯 stdlib——`pip install agent-mailbox[jev]` 现在即可安装，extra 为将来的原生客户端预留。

> ⚠️ macOS 安装会写 `~/Library/LaunchAgents/com.polaris-smart.agent-mailbox-wake-<agent>.plist` 并执行 `launchctl load`；Linux 在 `~/.config/systemd/user` 下写用户 unit 并启用 path unit。用 `--no-activate` 只生成文件不加载。

### Threads（v0.6.0，一等公民）

每封信现在都带 `thread_id`：新发的信铸造一个，回信继承原信的；`mailbox_thread(thread)` 按**跨所有 agent**（inbox + archive）的时间序回放整段对话。`mailbox_list` 支持 `thread` 过滤。旧信按归一化主题启发式归组（剥掉堆叠的 `Re:`/`Fwd:`）——12 层深的 "Re: Re: …" 链归并为一个 thread；无法匹配的孤信保持 `null`，缺口可见。当一个 thread 累积超过 5 封未办结（非 done）信件时，`mailbox_check` 返回 `ghost_warning`——在「acked 沉底」这种失效模式吃掉整条线程之前把它暴露出来。

### 自回声防护（默认开启）

`send` 产生「发件人 = 收通知人」的通知（自回声）**默认不投递**：信件照常落盘，`mailbox_list` / `mailbox_check` 不受影响，只是不再唤醒发件人自己；丢弃动作在 `sent.log` 里记 `echo_suppressed: true` 留痕，可一行 grep 定谳。设 `notify_self_echo: true`（邮件根 `~/.agent-mail/config.json`）或环境变量 `AGENT_MAIL_NOTIFY_SELF_ECHO` 恢复投递——恢复后自回声**通知**的主题带 `[echo] ` 前缀（信件原文主题不变，便于正则剥离）。

## 工具一览

| 工具 | 说明 |
|------|------|
| `mailbox_register(agent_id, owner?, description?)` | 认领信箱；幂等 |
| `mailbox_send(to, subject, body, priority?)` | `to` = 单个 id、列表或 `"all"`；`dedupe?`（默认 true）抑制同哈希重复投递 |
| `mailbox_check(agent_id?, mark?)` | 取走待读信（→ `acked`） |
| `mailbox_reply(msg_id, body)` | 自动路由回原发件人（豁免去重） |
| `mailbox_list(agent_id?, status?, thread?)` | 列出信件，可按状态 / thread 过滤 |
| `mailbox_thread(thread)` | 跨 agent 按时间序回放整条线程（thread_id 或任意 msg id） |
| `mailbox_done(msg_id)` | 标记已办 |
| `mailbox_broadcast(subject, body)` | 发给所有已注册 agent；`dedupe?` 按收件人生效 |
| `mailbox_whoami()` | agent 目录 + 邮件根路径 |
| `mailbox_wait(agent_id?, timeout_seconds?)` | 长轮询等新信 |
| `task_create(title, assignee, due?)` | 建任务卡（初始 `todo`）；自动发信通知负责人 |
| `task_move(task_id, status, assignee?, note?, force?)` | 沿 `todo→doing→review→done` 移卡（跳步需 `force`）；挪卡即自动给负责人发信 |
| `task_list(assignee?, status?)` | 列任务卡，可过滤 |

身份：显式传 `agent_id`，或每个 agent 设一次 `AGENT_MAIL_ID`。

## 可选：给人类看的桌面通知

配套 watcher 把每封新信打印成 JSON 行并弹出桌面通知（macOS / Linux / Windows）。它从不在 agent 唤醒路径上——agent 不需要它：

```bash
uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox-watch --notify boss
```

以服务方式常驻：

| 平台 | 安装 | 验证 |
|------|------|------|
| macOS (launchd) | `scripts/install-watch-macos.sh --notify boss` | `tail -f ~/.agent-mail/watch.log` |
| Linux (systemd user) | `scripts/install-watch-linux.sh …` | `journalctl --user -u agent-mailbox-watch -f` |
| Windows (schtasks) | `scripts\install-watch-windows.ps1` | `schtasks /Query /TN AgentMailboxWatch /V` |

## 维护：清理测试残留（cleanup）

```bash
python -m agent_mailbox.cleanup --dry-run              # 只列不删（默认行为即是 dry-run）
python -m agent_mailbox.cleanup --dry-run --root ~/.agent-mail
python -m agent_mailbox.cleanup --yes                  # 真删：需显式 --yes，且要求交互二次确认
```

扫描邮件根，列出**疑似测试残留**：registry 外 agent 的 `inbox/` / `archive/` 目录、`NEWBIE` / `WBTEST` 等测试命名目录、孤儿信件（散落在 `inbox/` / `archive/` 顶层的文件、解析失败的 JSON、中断原子写遗留的 `*.tmp`）。每条输出路径 + 大小 + 判定理由；`--dry-run`（及不带旗标的默认行为）零删除；`--yes` 才真删并要求输入 `yes` 确认。已注册但测试命名的目录仅提示（review only），不随 `--yes` 删除。

## 设计原则

- **本地优先** —— `~/.agent-mail/` 下的纯 JSON 文件。无 SMTP、无 IMAP、无域名、无云端中继、默认零网络。
- **注册即寻址** —— `mailbox_register("HS")` 一步完成；注册后即被所有人可见可达。
- **零外部依赖** —— 只有 `mcp`。存储是单个 Python 文件，`flock` 原子写保护；多个 MCP 宿主进程安全共享一个邮件根。
- **人类可读** —— 每封信都是一个小 JSON 文件，`cat` 即全文。老板直接看收件箱。
- **尊重既有身份** —— 每个 agent 的环境里设一次 `AGENT_MAIL_ID`，工具自动署名收发。

## 安全说明

- 邮件根在你的家目录下；除非你主动启用 webhook（默认锁定环回/私网目标），消息永不离开这台机器。
- agent id 严格校验（`[A-Za-z0-9_-]`，≤64 字符）——无路径穿越。
- 存储为追加式原子写 + 文件锁；崩溃的写入者不会损坏注册表。
- webhook 负载带 HMAC 签名；校验方应使用常数时间比较。
- 防篡改回执（ed25519）在 roadmap 上。

## 开发

```bash
git clone https://github.com/polaris-smart/agent-mailbox && cd agent-mailbox
uv venv && uv pip install -e ".[dev]"
pytest
```

## 升级

用 `uv tool upgrade agent-mailbox` 升级（或按你原有的安装方式重新拉取）。

⚠️ **升级后请重启 agent 会话（或重连 MCP 客户端）**——MCP 工具列表在会话启动时枚举，因此新工具（现在是 13 个，原来是 9 个）只有在重启后才会出现。无需改任何配置；`tasks.json` 在首次使用时自动创建。

## Roadmap

- **v0.7.2**（当前）—— SEP-2577 加固 + 发版卫生：**per-agent sampling 关停开关**（per-agent `wake.json` 段 `"sampling": {"enabled": false}`——MCP 已于 2026-07-28 弃用 sampling capability；取值格式错响亮失败落 `sampling.log` error 审计，信从不依赖 sampling）；**sdist 卫生**（AOCI 资产 + 本机路径泄漏经 hatchling `exclude` + `.gitignore` 销账）；`__version__` 与 pyproject 版本对齐。
- **v0.7.0** —— 唤醒升级：**sampling 唤醒**（server 经宿主自身 MCP 连接反向 createMessage——policy 强制注入/per-agent 执行锁/60s 超时/逐信去重/未声明回落信箱）、**local-command 唤醒适配器**（纯 argv、内容走环境变量、killpg 强杀超时，面向 codex 这类按需 CLI）、`wake run --adapter` 覆盖。13 个 MCP 工具。
- **v0.6.2** —— 安全加固 + v0.5.x 收口：**SECURITY.md**（GitHub Security Advisory 报漏渠道、支持版本、公开言明的本机信任安全模型）；**identity binding**（`config.json` 可选 `identity_binding`：被绑定的 agent id 必须出示 `AGENT_MAIL_TOKEN`——sha256 + `hmac.compare_digest` 常量时间比较——否则调用以 `identity mismatch` 拒绝；默认关闭、未绑定身份行为不变，配置损坏启动即响亮失败）；**webhook 负载 `unread_count`**（通知时点收件人 pending 计数，顶层字段、纯增量）；**`mailbox_wait` 认领语义**（锁内原子 `claim()`：信返回即 `acked` + `claimed_by`，第二个 waiter 永不重复消费同一批，过期认领随 acked→pending 回收一并清除）。
- **v0.6.0** —— 爆款批：**wake daemon**（`agent-mailbox wake install`——launchd WatchPaths / systemd PathChanged 触发一轮清信；hermes / generic-webhook / claude-code 三适配器；POST 失败 5×60s 重试后留待下轮触发补投，`handled_log` wake 条目保证每封信至多唤醒一次，计数口径 v2 = 全部 pending + acked>600s；端到端 fail-open 铁律）；**一等公民 threads**（发信铸造 `thread_id`、回信继承，`mailbox_thread` 跨 agent 时间序回放，`--thread` 列表过滤，旧信 Re: 链主题键回填，超 5 封未办结 ghost 线程告警）；**Jev 分流旁路**（可选默认关闭的打分插件：Noul 门控唤醒，低于阈值归入每日摘要，任何失败即回落有信即醒，决策连同分数留痕，纯 stdlib 藏在 `[jev]` extra 后）。13 个 MCP 工具。
- **v0.5.0** —— 源自 2026-09-13 事故（任务 t-6）的生命周期加固：**投递侧去重**（`semantic_hash`，24h 窗内同哈希非终态重复投递返回 `{"deduped": true, "existing_id"}` 零副作用；`dedupe: false` 豁免；代码围栏原文哈希、仅收件箱范围、hash→inbox 索引）；**半办结补偿**（`record_handled` 两段式 intent/outcome API 作为 `handled_log` 唯一写入方 + `resume_plan` 四行判定表：process / replay / finalize / skip）；**过期 acked 回收**（`reap_stale_acked` / `python -m agent_mailbox.reap`）接入唤醒循环（先回收后计数、fail-open），并强制铁1——`reap_ttl`（3600s）必须严格小于 `dedup_ttl`（24h），违例在配置加载时响亮失败；**唤醒熔断**——连续 N 轮无进展即锁存熔断文件并停止发起清信轮（backoff 拉长间隔，熔断止血）。
- **v0.5.x（开放）**—— 仍跟踪自 t-6 复盘：状态过滤（"pending 或 acked" 视图）、唤醒路由（网关订阅 `to` 过滤；在本仓库之外）。已于 v0.6.2 兑现：~~身份绑定~~、~~webhook 负载 `unread_count`~~、~~`mailbox_wait` 认领语义~~。
- **v0.4.0** —— 功能批：webhook 签名风格可配（`AGENT_MAIL_SIGNATURE_STYLE`：github 默认 / generic / slack）；自回声防护（`from == 收通知人` 的通知默认不投递，`sent.log` 记 `echo_suppressed` 留痕，`notify_self_echo` / `AGENT_MAIL_NOTIFY_SELF_ECHO` 恢复投递并给通知主题加 `[echo] ` 前缀，信件原文主题不变）；新增 `cleanup --dry-run` 维护命令（扫描测试残留只列不删，`--yes` 真删需二次确认）。
- **v0.3.1** —— 小修批：回信主题不再堆积 `Re: Re:`（首答/二次回复/大小写混写均归一为单个 `Re:`）；Web 看板 token 改常数时间比较（`hmac.compare_digest`）并跨重启持久化（`~/.agent-mail/web_token`，0600，env `AGENT_MAIL_WEB_TOKEN` 永远优先）；`sent.log` 超 10MB 自动轮转一代（`sent.log.1`）。
- **v0.3.0**—— 任务看板 + Web 看板：`task_create` / `task_move` / `task_list`，严格 todo→doing→review→done 状态机；建卡/挪卡自动给负责人发信，看板动作零轮询唤醒 agent。`--web 8643` 提供 token 保护的零依赖看板 UI，人类拖卡走同一唤醒链路。消息 + 任务 + 唤醒 + 看板，依旧零依赖。
- **v0.5.0+** —— 可能：更深的看板集成（Kaneo 作为参考/竞品）。届时再议。
- **后续** —— 联邦：streamable HTTP transport 让其他机器上的 agent 接入（Tailscale/LAN 友好）；签名回执（ed25519）防篡改投递。
- **v1.0.0** —— 跨组织桥：本地会话经标准邮件基础设施触达其他机器与组织的 agent，信箱生命周期不变。

## 许可

MIT
