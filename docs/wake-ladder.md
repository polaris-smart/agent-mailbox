# 唤醒阶梯（T31 规格 · 仓内）

## 0.8.3 开发候选：通知与定向邮件一起落库

定向邮件写入 `messages` 时，在同一事务内登记 `notifications` 的
`mailbox_wake` 待投递项；任一步失败一起回滚。事务提交后通知本进程的工作线程，
无需另外配置 Codex 心跳。产品自身仍保留恢复扫描，用于其他进程的写入、重启、
暂时失败和冷却后的补投。普通邮件仍不自动授予任务执行权限。

首次启动仅忽略没有待投递记录的历史消息；有持久通知的新消息不会被当成历史吞掉。
无 hook 时保留待投递，关闭、冷却、频率上限及未知交付的人核规则仍生效。
同一消息复用现有认领和成功标记，不因为进程重启重新开工。
已有的背压摘要继续按同一摘要消息合并，不为摘要里的每一封信创建一次唤醒。

Human 创建邮件任务时可选择 `review_recipient_id`，只允许另一位本机在岗项目成员。
交付将定向通知该技术复核者；员工不能在提交参数中更换复核者，复核者也不因此
获得 Human 验收权。未选择复核者的交付留在任务页供 Human 验收，不广播私有结果。
复核者被撤销或其他发送校验失败时，保存交付并记录 `review_notification_blocked`，
由 Human 处理，不能伪称已通知。补充任务继承同一复核者。

数据库升级到 schema 13，迁移前按既有流程备份；旧任务的复核者默认为空。
本次不推断旧任务的复核者、不重播历史交付，也不扩大成员权限。

通知表的 `host_received` **仅表示已核对宿主适配器的结构化接收回执**，不证明真实 Agent
已读、接单或开始复核。正确的宿主适配必须把请求送入所绑定的会话，未支持的
员工不得静默 `exit 0`。桌面气泡通知不能代替这一步。没有真实宿主证据时不能
宣称原生闭环通过；安装 MCP 读取工具本身也不等于安装了宿主唤醒适配。

### 0.8.3 宿主回执契约（开发候选）

工作台内置投递器与 `wake --once` 均要求回执，单纯退出码 0 不再算接收成功。
适配器从 outbox 读取 `delivery_id`、`employee_id`、`fresh`，把通知提交到该员工
预先绑定的会话；**只有宿主确认持久接收后**，原子写入
`<store home>/wake/receipts/<delivery_id>`（无扩展名，临时文件加 rename，权限 0600）：

```json
{
  "protocol": "agent-mailbox.host-receipt.v1",
  "delivery_id": "wake-employee-example-delivery",
  "employee_id": "employee-example",
  "message_ids": ["message-example"],
  "status": "received",
  "binding_id": "binding-example",
  "project_id": "project-example",
  "host_kind": "the-configured-host-kind",
  "host_session_id": "the-prebound-host-session"
}
```

员工、投递 ID 和完整有序消息列表必须与对应 outbox 一致；`binding_id`、项目、
宿主类型、会话 ID 必须逐项匹配 outbox 的 `binding`。任意非空会话名不再足够。
适配器必须复用投递 ID 做宿主侧去重。此 JSON 是受信任本地适配器的声明，
不能独立证明宿主内部已处理消息，也不是 Agent 接单回执；绑定是否正确必须真机验收。
不可将邮件内容作为 shell 命令、不可为通知创建替代聊天来冒充原会话接收。

宿主明确拒绝且保证没有接收时，可写同样身份字段、`status: "not_received"`
的回执，保留待办以便恢复。不能把超时或断连猜成未接收。
hook 返回、异常退出或超时后，有合法 `received` 回执即确认接收；有明确
`not_received` 才允许重投；回执缺失、损坏或身份不符则标为未知交付，交人核实，
不消耗成功叫醒计数、不自动重投。进程启动失败仍可重试。

认领文件保存具体投递 ID；崩溃恢复按该 ID 核验，不能拿别批回执或旧空 `.ok`
标记证明本次投递成功。旧适配器须升级后才能接入本候选；历史内部调用的退出码
兼容分支不用于工作台和 CLI 的正式入口。没有回执的旧成功标记也不会被升级成新通知的证明。

### 项目范围的宿主绑定

本机 Human 管理者用下列命令登记**已经存在**的宿主会话。绑定只是路由配置，
不建立宿主连接、不创建聊天、不证明该宿主已具备接收能力：

```sh
agent-mailbox wake --home <store-home> --bind <employee-id> --project <project-id> --host <host-kind> --session <existing-session-id>
agent-mailbox wake --home <store-home> --bindings
agent-mailbox wake --home <store-home> --unbind <employee-id> --project <project-id>
```

一位员工在两个项目中可以绑定两个不同会话；投递按项目分批，不能把多个项目的
消息混进一个会话。一批接收成功、另一批缺绑定时，只确认成功批次；其余保持待办。
只有当前在岗、本机、项目内的成员可建立绑定。投递时重新检查成员资格；成员被撤销
后，遗留绑定不能继续用于发送。已经进入宿主的通知无法用解绑远程撤回。

配置存在 `<store-home>/wake/host-bindings.json`，仅本机所有者可访问（Windows 使用
私有 DACL），用临时文件、fsync 和原子替换保存，不存令牌、任意命令或宿主密钥。
配置损坏/重复不猜测路由、不覆盖原文件。`--bindings` 不创建 home 或数据库。
换绑生成新的绑定 ID；旧绑定的回执不能为新会话作证。已发出但未完成数据库确认的
旧投递遇到换绑时进入未知交付，需核实，不自动重发以免重复外部效果。

未绑定或明确未接收的通知保持待办。旧 `wake --once` 保留原有冷却/频率/关闭闸门；
0.8.3 工作台事件通知的不同限频规则见下节。
`--state-dir` 不用于改写绑定位置。该 CLI 是本地维护入口，尚不是完整的 App/Web
连接向导；真实宿主适配、会话真实性和端到端触发仍须单独验证。

## 0.8.3 开发候选：随信事件通知

邮件与通知意图在同一事务保存，提交后立即唤起工作台内的唯一通知工作线程。
独立 stdio MCP 进程经本机 HTTP 调用工作台，由 HTTP 服务持有的 store 提交事务，
因此这条路径不依赖不同进程共享 Python Event，也不需要额外的系统轮询器。
默认 5 秒超时仅作离线重试、漏信号及直接写库入口的恢复；隔离集成测试用
`interval=None` 完全禁用超时扫描，验证 A→B→A→B 连续事件。

这条通知流只处理持久化的待通知消息，历史邮件不因被读取而重放。它不使用旧提醒
模式的 15 分钟冷却，否则第二封正常回复也会被延迟。保留总关闭开关、消息 ID
去重、严格宿主回执、未知交付不盲重投与发送侧回声/背压约束；单员工每小时最多
60 次宿主确认接收，单次可合并多条消息。到达上限保留待通知，由恢复路径后续重试。
旧 `wake --once` 行为未改；以下阶梯描述保留为旧模式背景。

事件进入宿主不等于 Agent 已读、接单或交付。此候选通过了真实 MCP 子进程、HTTP、
数据库及事件工作线程的合成宿主接缝测试；真实原会话触发和双向 Agent 协作仍待验。

> 背景：2026-10-04 的 353 封风暴里，**v0.7 的"逐信唤醒"是四条风暴源之一**
> （`WatchPaths` 挂在收件箱 ⇒ 每来一封起一个进程）。所以唤醒这件事只有一条铁律：
> **绝不做 per-letter 进程**。下面是替代表，按"便宜 → 贵"排序。

## 阶梯

| 档 | 机制 | 成本 | 何时用 | 状态 |
|---|---|---|---|---|
| **0** | **不唤醒**：员工**主动查**（`project_messages` / `project_tasks`） | 0 | 默认。v0.8 的 `notification_mode=manual_check` 就是它 | ✅ 已有 |
| **1** | **钩子轻通道**：宿主在会话起始调一次 `agent-mailbox brief`，把**有预算的简报**贴进上下文 | 一次进程、几十毫秒；**无常驻** | 装了 hook 的宿主（Claude Code / Codex / Hermes…） | ✅ 本轮实现 |
| **2** | **单内核**（可选）：一个常驻进程轮询并"叫醒 idle agent" | 1 个进程、持续开销 | 只在"必须叫醒睡着的 agent"时启用 | ⏸ 可选；启用必须满足下方护栏 |
| ❌ | **逐信唤醒**（v0.7 形态） | 每信一进程 | **永不** | 🚫 已退役 |

## 档 2 若启用，必须满足（护栏 = 反风暴七闸的子集）

- **S1** 绝不裸 `KeepAlive`（要么 `SuccessfulExit:false`，要么带重启熔断 = `storm_breaker.py`）；
- **S7** 常驻 ≤1 个进程，且有状态文件 + 一条命令可停；
- **S4** 通知折叠 + 每小时上限；**S5** 日志只在状态变化时写；
- **S3** 回声冻结：同一往返 ≥3 轮且状态无变化 ⇒ 冻结（`echo_guard.py`）；
- 触发前先算**升级预算**（`escalation_budget`），超了就抑制并留痕（`workbench_policy.py`）。

## 钩子怎么装（示例：宿主支持 SessionStart 钩子时）

```sh
# 会话起始把简报注入上下文（输出已被预算约束，默认 1200 字节）
agent-mailbox brief --employee <employee_id> --project <project_id> --budget 1200
```

要点：
- **只读**：简报不写任何东西（有测试），因此钩子失败也只是"少一段提示"，不会坏数据；
- **有预算**：`render()` 先给截断提示留位置再截内容（否则"截断后反而超预算"）；
- **去噪**：只给计数与前几条标题，不给消息流水；且会把
  **被折叠/被抑制**的信号顶上来（**安静不等于无事**）。

## 可选宿主叫醒 hook

`wake --once` 只在宿主明确接入后交付叫醒请求。macOS/Linux 继续读取
`<store home>/wake/wake-hook.sh`，要求文件可执行；Windows 读取同一目录的
`wake-hook.ps1`，使用 Windows 系统目录中的 WindowsPowerShell，以
`-NoProfile -NonInteractive -File` 启动。两种 hook 都收到两个独立字符串参数：
员工 ID 和投递 ID。投递 ID 对应 `wake-outbox/<投递 ID>.json`，不把消息正文拼入命令。
hook 与 outbox 始终钉在 store home；`--state-dir` 不改变宿主入口。

Windows 遵守机器现有执行策略，不传 ExecutionPolicy 覆盖参数、不修改系统策略。
在 Restricted 策略下，PS1 会被拒绝；AllSigned 要求受信任的签名，RemoteSigned
仍可能拒绝带互联网来源标记的未签名脚本。被拒绝或返回非零都不计成功叫醒、不写
`.ok`；缺脚本或解释器则保留待办。[微软执行策略说明](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_execution_policies?view=powershell-5.1)

PS1 宿主 hook 应先声明参数并设置 `$ErrorActionPreference = 'Stop'`，只有实际宿主接口
确认接收请求后才 `exit 0`。若调用原生宿主程序，检查并传播 `$LASTEXITCODE`；出错时
返回非零。PowerShell 非终止错误可能不影响退出码，不能只打印错误后正常结束。
旧版本仅依据退出码判断；0.8.3 开发候选还强制核验上面的结构化回执。
即使回执有效，也不能证明员工已开始处理或人类已接受结果。
[微软 PowerShell 启动与退出码说明](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_powershell_exe?view=powershell-5.1)

`wake --install` 仅在 macOS 写 launchd 单元，仍需用户显式加载。Linux/Windows
返回不支持且不创建 plist；`--once` 可由宿主已有调度器调用，此处不安装其它系统服务。
