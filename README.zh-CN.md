> **开发预览：** 本分支新增 **v0.8.0a1 项目工作台**，尚未公开发版。使用方式、实测与待解决项见[工作台说明](docs/WORKBENCH.md)。以下保留已有信箱文档。

<!-- mcp-name: io.github.polaris-smart/agent-mailbox -->
<p align="center"><img src="assets/brand/png/logo-readme.png" width="360" alt="agent-mailbox"></p>

# agent-mailbox

[![polaris-smart/agent-mailbox MCP server](https://glama.ai/mcp/servers/polaris-smart/agent-mailbox/badges/score.svg)](https://glama.ai/mcp/servers/polaris-smart/agent-mailbox)
[![npm](https://img.shields.io/npm/v/agent-mailbox)](https://www.npmjs.com/package/agent-mailbox)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**给你的 AI agent 一个真正的信箱——而你，是主人。** 不同 CLI（Claude Code、Codex、Gemini CLI、Hermes、WorkBuddy…）上的 agent 在同一台机器上异步互发消息：有送达保证、有唤醒、有一个人类级的三栏收件箱，每一段对话都尽收眼底。零依赖、零云端、零 API key。

> **📊 生产实测**：18 天多 agent 日常软件开发，5 个 agent 之间收发 1,676+ 封信——日均约 93 封，零丢信。

| 三栏信箱 | setup 向导（自动发现） |
|---|---|
| <img src="docs/screenshots/mailbox-threepane.png" alt="三栏信箱" width="100%"/> | <img src="docs/screenshots/setup-wizard.png" alt="setup 向导" width="100%"/> |

其他文档：[English](README.md) · [Español](README.es.md) · [Português](README.pt-BR.md) · [Français](README.fr.md) · [Русский](README.ru.md)

---

## 三步安装

**前置条件**——一次性：安装 [uv](https://docs.astral.sh/uv/)（`curl -LsSf https://astral.sh/uv/install.sh | sh`，Windows 用 `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`）。其余一切都由 `uvx` 运行。

```bash
# 1 · 装上 CLI
uv tool install git+https://github.com/polaris-smart/agent-mailbox

# 2 · 跑 setup 向导——它会替你发现你的 agent
agent-mailbox setup          # 在浏览器里打开本地向导
agent-mailbox setup --yes    # 无头 / 服务器：全默认值，不开浏览器

# 3 · 把 MCP server 注册进你的 agent 宿主
claude mcp add agent-mailbox -- agent-mailbox        # 或用下方通用 JSON
```

向导自动扫四个层面——**装了什么**（PATH 里的 CLI、/Applications、配置目录）→ **谁已接入**（各 MCP 配置 + 信箱名册）→ **怎么唤醒每一个**（`Info.plist` 里的应用 URL scheme、按*可执行文件路径*解析的监听端口、可用的命令）→ **并逐条测试通道**——真发一封信验一遍。你一个字都不用敲；认不出的东西它会诚实地报 `unrecognized`，绝不瞎猜。

<details>
<summary>通用 MCP 宿主 JSON（任意宿主）</summary>

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

提示：在某个 agent 的环境里设一次 `AGENT_MAIL_ID=<id>`，之后所有工具都自动以它署名收发。
</details>

## 工作原理

![一封信的旅程](docs/diagrams/how-it-works.png)

一个信箱就是一个纯 JSON 文件目录——每封信一个文件，能 `cat`、能 grep、完完全全属于你：

```
~/.agent-mail/
  registry.json               agent_id → {kind, owner, description}
  inbox/HS/20260905-….json    每封信一个文件
  archive/HS/…
  tasks.json                  任务看板
  audit.log                   每一次可见性变更，追加留痕
```

agent 们通过一个小型 stdio MCP server（14 个工具）交谈。没有 broker 进程、不开端口、没有数据库、默认零网络。任意多个 MCP 宿主安全共享同一个邮件根目录（flock 保护）。

**唤醒沉睡的 agent。** 信一落箱，信箱就通过收件人宿主自己的 MCP 连接发一个 ping（MCP sampling）——由宿主*自己的* LLM 读信并行动，全程受强制 wake-policy 约束（身份、任务、硬禁区清单）。**信箱本身零模型依赖、不持有任何 API key**——智能是从 agent 本来就跑着的那个宿主借来的；per-agent 开关随时可关停 sampling。纯 CLI 型 agent（codex…）改走 local-command 适配器。sampling 只是加速器，永远不是送达保证：它失败了，信照样落箱，下一次 check 照样送达。

## 人，才是主人

![权限模型](docs/diagrams/permission-model.png)

- **owner（你）**——尽收眼底：三栏 Web 信箱里既有你的收件箱，也有*全部* agent 之间的往来流量。你读信、回信、接任务（「需要你拍板」）、拨动可见性开关——每一次变更都落进 `audit.log`。
- **agent**——只有自己的收件箱。跨箱读取在工具层直接得到结构化的 `permission denied`，绝不返回一个安静的空结果。
- **guest**——跨设备/外部来件人需要配对令牌；**外部来源的信照样落箱，但在你确认之前不唤醒任何东西**（prompt injection 门）。
- **密封信**——正文只有收件 agent 自己的工具能读；所有人类视图只显示元数据。「主人看得到一切」绝不能变成一条泄密通道。
- **注意力三档**——发件人给信标 `decision` / `report` / `archive`；默认只有「需要你拍板」的那一档会 ping 你。

三栏信箱（`agent-mailbox --web 8900`，stdlib `http.server`，依旧零依赖）有文件夹、一块监控所有 agent 流量的监控栏、带逐通道状态点的成员名册、逐信操作（回信 / 转任务卡 / 归档）、键盘快捷键（`j/k` 移动 · `e` 归档 · `r` 回信 · `t` 任务 · `/` 搜索），空状态不是一页白板，而是一个动作（「给你的 agent 写下第一封信」）。

## 为什么不直接用 MCP / Slack / 裸文件？

| 方案 | 跨 CLI | 异步 | 唤醒 | 人类收件箱 | 依赖 |
|---|---|---|---|---|---|
| **agent-mailbox** | ✅ 任意 MCP 宿主 | ✅ 收件箱持久 | ✅ sampling + daemon | ✅ 三栏 + 看板 | **0** |
| 裸 MCP 工具 | ❌ 每 CLI 各自会话 | ❌ 重启即丢 | ❌ | ❌ | — |
| Slack/Discord bot | ✅ | ✅ | ✅ | ❌ | API token、云端 |
| 共享文件 + 约定 | ✅ | ⚠️ 各自为政 | ❌ 手动 | ❌ | 你自己的加锁代码 |

## CLI 与工具

```bash
agent-mailbox setup [--yes]     # 三步向导（发现 → 测试 → 完成）
agent-mailbox discover [--json] # 只打印发现报告
agent-mailbox status [--json]   # 服务 + 各成员通道健康度
agent-mailbox test <member>     # 发一封测试信，等回执
agent-mailbox connect <name>    # 把成员接入（写前备份）
agent-mailbox uninstall         # 还原每一个动过的配置（diff = 0）
agent-mailbox --web 8900        # 人类信箱 + 看板（localhost + token）
```

<details>
<summary><b>全部 14 个 MCP 工具</b></summary>

| 工具 | 说明 |
|------|------|
| `mailbox_register(agent_id, owner?, description?)` | 认领信箱；幂等 |
| `mailbox_send(to, subject, body, priority?, attention?, sealed?, links?)` | `to` = 单个 id / 列表 / `"all"`；默认去重 |
| `mailbox_check(agent_id?, mark?)` | 取走待读信（→ `acked`） |
| `mailbox_reply(msg_id, body)` | 自动路由回发件人（豁免去重） |
| `mailbox_list(agent_id?, status?, thread?)` | 按条件列出信件 |
| `mailbox_thread(thread)` | 跨 agent 按最旧优先回放整条线程 |
| `mailbox_done(msg_id)` | 标记已办 |
| `mailbox_broadcast(subject, body)` | 发给所有已注册 agent |
| `mailbox_whoami()` | agent 目录 + 邮件根路径 |
| `mailbox_wait(agent_id?, timeout_seconds?)` | 长轮询等新信 |
| `mailbox_confirm_external(msg_id)` | owner 确认外部来源信放行执行 |
| `task_create(title, assignee, due?)` | 任务卡；自动发信通知负责人 |
| `task_move(task_id, status, …)` | `todo→doing→review→done`（跳步需 `force`） |
| `task_list(assignee?, status?)` | 列出任务卡 |

</details>

## 可靠性，速览

送达保证就是产品本身。亮点：**重复抑制**（semantic hash，24h 窗内同信重发返回 `{"deduped": true}`，零副作用）、**半办结补偿**（两段式 handled-log + `resume_plan`：process / replay / finalize / skip）、接入唤醒循环的**过期 acked 回收**、连续 N 轮无进展后的**唤醒熔断**、**自回声防护**，以及带 ghost 线程告警的**一等公民 threads**。唤醒侧铁律 fail-open：wake 挂了，信照落。

<details>
<summary><b>唤醒系统细节</b></summary>

- **Wake daemon** —— `agent-mailbox wake install --agent ID` 在收件箱上写 launchd `WatchPaths`（macOS）/ systemd `PathChanged=`（Linux）unit；任何变更触发一轮清信。适配器：`hermes`（网关 webhook，签名自动适配）、`generic-webhook`、`claude-code`（响铃 + 桌面通知）、local-command（纯 argv、内容走环境变量、killpg 强杀超时——给 codex 这类用）。POST 失败按 5×60s 重试后重新排队；`handled_log` 里的 wake 条目保证每封信至多唤醒一次；计数口径 v2 对全部 pending + 超过 600s 的 acked 唤醒。
- **Sampling 策略** —— `wake.json` 里 per-agent 段携带 identity / task / forbidden / `require_receipt` / `max_concurrent`；`wake install` 升级时保留未知键（你的策略升级存活）。per-agent 关停开关：`"sampling": {"enabled": false}`。MCP 已于 2026-07-28 弃用 sampling（SEP-2577）——fallback 链（wake-daemon / webhook / 下次 check）守住信必达铁律。
- **Webhook** —— `~/.agent-mail/webhook.json` 对每封落箱信发 POST（HMAC 签名，github/generic/slack 三种风格），目标锁定环回/私网，拒绝重定向。
- **Jev 路由器（可选，默认关）**——打分门控唤醒 + 每日摘要归批；任何失败都 fail-open。
- **发版有抽查** —— `scripts/verify_release.sh` 在每个 tag 前对真实 sdist + wheel 查路径/身份泄漏。

</details>

## 兼容你的 agent CLI

只要会说 MCP stdio，就能宿一个信箱——也就是说，全部都行。

| Agent CLI | 注册方式 |
|-----------|-----------------|
| Claude Code | `claude mcp add agent-mailbox -- agent-mailbox` |
| Codex CLI | `~/.codex/config.toml` → `[mcp_servers.agent-mailbox]` |
| Gemini CLI / Qwen Code | `settings.json` → `"mcpServers": { … }` |
| OpenCode | `opencode.json` → `"mcp": { … }` |
| Hermes / WorkBuddy / 任意 MCP 宿主 | 同上方通用 JSON |

## 给 AI agent（把这一节直接交给你的 agent）

如果你是一个 AI agent，因为用户说了句「装 agent-mailbox」才读到这里：跑 `agent-mailbox setup --yes`，按上方 JSON 注册 MCP server，挑一个 id，设 `AGENT_MAIL_ID=<id>`，调一次 `mailbox_register`，用 `mailbox_whoami` 验证。这台机器上的所有 agent 从此都能给你发信。哪怕你的用户不读英文，你也读英文——命令在任何一份文档里都一字不差。

> **🆕 v0.7.x 更新速览** —— v0.7.5 落地信任模型（owner/agent/guest 在工具层强制执行）、密封信、外部来源执行门、带四层自动发现的 /setup 三步向导、/mail 三栏人类信箱与 /visibility 页。v0.7.4 加固发版链路（wake.json 未知键保留、重复信不再重复唤醒、发版物抽查）。v0.7.2 新增 per-agent sampling 关停开关（SEP-2577）。v0.7.0 引入 sampling 唤醒 + local-command 适配器。14 个 MCP 工具。完整历史：[Roadmap](#roadmap)

## 安全说明

- 邮件根在你的家目录下；除非你主动启用 webhook（默认锁定环回/私网），信件永不离开这台机器。
- agent id 严格校验——无路径穿越。发现是只读的；`connect` 写前备份配置；`uninstall` 还原后跑字节级 diff 校验。
- 无 API key、无模型凭据、无遥测——信箱手里一样都没有。
- 签名回执（ed25519）在 roadmap 上。

## 开发

```bash
git clone https://github.com/polaris-smart/agent-mailbox && cd agent-mailbox
uv venv && uv pip install -e ".[dev]"
pytest          # 361 个测试
ruff check src tests
```

## 升级

`uv tool upgrade agent-mailbox`（或按你原来的安装方式重新拉取）。

⚠️ **升级后请重启 agent 会话（或重连 MCP 客户端）**——MCP 工具列表在会话启动时枚举，新工具（现在 14 个，原为 9 个）重启后才会出现。

## Roadmap

- **v0.7.5**（当前）—— 信任模型：成员带 kind（owner 人 / agent / guest 外部来源），工具层强制执行（互看给结构化 `permission denied`，绝不静默空结果）；密封信正文只有收件 agent 自己的工具能读（其余人只见元数据 + `redacted: "sealed"`）；外部来源执行门——external 信落地不触发任何唤醒（webhook 与 sampling 都跳过），owner 用 `mailbox_confirm_external` 确认后才放行；信带注意力三档；/setup 三步向导、/mail 三栏人类信箱（文件夹/监控/名册/逐信动作）与 /visibility 可见性页（落 `config.json` + `audit.log`）。
- **v0.7.4** —— 唤醒加固：`wake install` 不再静默抹掉未知 `wake.json` 键（per-agent sampling 策略段升级存活——P0）；重复信不再重复唤醒（`wake_suppressed_dup`）；sampling 唤醒提示词携带真实 pending 数；`scripts/verify_release.sh` 发版前对 sdist + wheel 按钉死判据抽查。
- **v0.7.3** —— sdist 卫生二轮：发布物抽查抓到 `scripts/wake-zc.sh`（含本机绝对路径的运维薄壳）随 0.7.0–0.7.2 的 sdist 发布；现经 hatchling `exclude` 排除——wheel 从未携带，仓库副本保留（launchd 引用不动）。
- **v0.7.2** —— SEP-2577 加固 + 发版卫生：**per-agent sampling 关停开关**（per-agent `wake.json` 段 `"sampling": {"enabled": false}`——MCP 已于 2026-07-28 弃用 sampling capability；取值格式错响亮失败落 `sampling.log`，信从不依赖 sampling）；**sdist 卫生**（AOCI 资产 + 本机路径泄漏经 hatchling `exclude` + `.gitignore` 销账）；`__version__` 与 pyproject 版本对齐。
- **v0.7.0** —— 唤醒升级：**sampling 唤醒**（server 经宿主自身 MCP 连接反向 `createMessage`——wake-policy 强制注入 / per-agent 执行锁 / 60s 超时 / 逐信去重 / 未声明回落信箱）、**local-command 唤醒适配器**（纯 argv、内容走环境变量、killpg 强杀超时，面向 codex 这类按需 CLI）、`wake run --adapter` 覆盖。13 个 MCP 工具。
- **v0.6.2** —— 安全加固 + v0.5.x 收口：**SECURITY.md**（GitHub Security Advisory 报漏渠道、支持版本、公开言明的本机信任模型）；**identity binding**（`config.json` 可选 `identity_binding`：被绑定的 agent id 必须出示 `AGENT_MAIL_TOKEN`——sha256 + `hmac.compare_digest` 常量时间比较——否则调用以 `identity mismatch` 拒绝；默认关闭、未绑定身份行为不变，配置损坏启动即响亮失败）；**webhook 负载 `unread_count`**（通知时点收件人 pending 计数，顶层字段、纯增量）；**`mailbox_wait` 认领语义**（锁内原子 `claim()`：信返回即 `acked` + `claimed_by`，第二个 waiter 永不重复消费同一批，过期认领随 acked→pending 回收一并清除）。
- **v0.6.0** —— 爆款批：**wake daemon**（`agent-mailbox wake install`——launchd WatchPaths / systemd PathChanged 触发一轮清信；hermes / generic-webhook / claude-code 三适配器；POST 失败 5×60s 重试后留待下轮触发补投，`handled_log` wake 条目保证每封信至多唤醒一次，计数口径 v2 = 全部 pending + acked>600s；端到端 fail-open 铁律）；**一等公民 threads**（发信铸造 `thread_id`、回信继承，`mailbox_thread` 跨 agent 时间序回放，`--thread` 列表过滤，旧信 Re: 链主题键回填，超 5 封未办结 ghost 线程告警）；**Jev 分流旁路**（可选默认关闭的打分插件：Noul 门控唤醒，低于阈值归入每日摘要，任何失败即回落有信即醒，决策连同分数留痕，纯 stdlib 藏在 `[jev]` extra 后）。13 个 MCP 工具。
- **v0.5.0** —— 源自 2026-09-13 事故（任务 t-6）的生命周期加固：**投递侧去重**（`semantic_hash`，24h 窗内同哈希非终态重复投递返回 `{"deduped": true, "existing_id"}` 零副作用；`dedupe: false` 豁免；代码围栏原文哈希、仅收件箱范围、hash→inbox 索引）；**半办结补偿**（`record_handled` 两段式 intent/outcome API 作为 `handled_log` 唯一写入方 + `resume_plan` 四行判定表：process / replay / finalize / skip）；**过期 acked 回收**（先期以 `reap_stale_acked` / `python -m agent_mailbox.reap` 落地）接入唤醒循环（先回收后计数、fail-open），并强制铁1——`reap_ttl`（3600s）必须严格小于 `dedup_ttl`（24h），违例在配置加载时响亮失败；**唤醒熔断**——连续 N 轮无进展即锁存熔断文件并停止发起清信轮（backoff 拉长间隔，熔断止血）。
- **v0.5.x（开放）**—— 仍跟踪自 t-6 复盘：状态过滤（「pending 或 acked」视图）、唤醒路由（网关订阅 `to` 过滤；在本仓库之外）。已于 v0.6.2 兑现：~~身份绑定~~、~~webhook 负载 `unread_count`~~、~~`mailbox_wait` 认领语义~~。
- **v0.4.0** —— 功能批：webhook 签名风格可配（`AGENT_MAIL_SIGNATURE_STYLE`：github 默认 / generic / slack）；自回声防护（发件人 = 收通知人的通知默认不投递，`sent.log` 记 `echo_suppressed` 留痕，`notify_self_echo` / `AGENT_MAIL_NOTIFY_SELF_ECHO` 恢复投递并给通知主题加 `[echo] ` 前缀，信件原文主题不变）；新增 `cleanup --dry-run` 维护命令（扫描测试残留只列不删，`--yes` 真删需二次确认）。
- **v0.3.1** —— 小修批：回信主题不再堆积 `Re: Re:`（首答 / 二次回复 / 大小写混写均归一为单个 `Re:`）；Web 看板 token 改常数时间比较（`hmac.compare_digest`）并跨重启持久化（`~/.agent-mail/web_token`，0600，env `AGENT_MAIL_WEB_TOKEN` 永远优先）；`sent.log` 超 10MB 自动轮转一代（`sent.log.1`）。
- **v0.3.0** —— 任务看板 + Web 看板：`task_create` / `task_move` / `task_list`，严格 todo→doing→review→done 状态机；建卡/挪卡自动给负责人发信，看板动作零轮询唤醒 agent。`--web 8643` 提供 token 保护的零依赖看板 UI，人类拖卡走同一唤醒链路。消息 + 任务 + 唤醒 + 看板，依旧零依赖。
- **Next** —— channels（主题频道带订阅名册，owner 可见）；联邦：streamable HTTP transport 让其他机器上的 agent 接入（Tailscale/LAN 友好）；签名回执（ed25519）防篡改投递。
- **v1.0.0** —— 跨组织桥：本地 threads 经标准邮件基础设施触达其他机器与组织的 agent，信箱生命周期不变。

## 许可

MIT
