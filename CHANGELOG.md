# Changelog

## 0.8.2 — 2026-10-10

- Add native CI and extracted-package HTTP/MCP checks for macOS arm64, macOS Intel, Windows x64 and Linux x64 on Python 3.10 and 3.13. Additional platforms are preview support; see [scope and limits](docs/RELEASE-082.md).
- Fix Windows wake delivery locking, process-exit recovery, private ACLs, PowerShell hooks, binary reads and path handling.
- Avoid unsupported service removal commands on Linux and unsupported optional CodeGraph execution on Windows.
- Statically link cryptography OpenSSL in Intel macOS builds to prevent bundled library conflicts.
- Preserve mailbox-task acceptance, pinned resources and separate Human review. No new agent-host restriction.
- Align App/Web/package versions at 0.8.2 and refresh bilingual onboarding and the narrated 60-second introduction.

## 0.8.1 — 2026-10-10

This release targets **macOS Apple Silicon App + local Web on macOS**. Windows and Linux native packages are not included. The project owner confirmed Human acceptance on 2026-10-10. Release artifacts are checked against the tagged source.

- Fix project switching and new-project actions in the desktop shell, sidebar layout, narrow-window mail reading, and employee search/filter layout.
- Restore the new mailbox-task action and align dashboard counts with the lists they open; correct global/project view scope.
- Distinguish pending notifications from successful host delivery; preserve controlled redelivery and manual handling of unknown delivery outcomes.
- Fix native application shutdown so a subsequent launch can reuse the same data home without manual instance-file removal.
- Add frontend syntax and CSS parsing checks, and verify nonempty official 0.8.0 upgrade plus backup restoration on an isolated candidate.
- Verify a bounded, same-machine workflow with two real agent hosts: receive, accept, read pinned resources, submit, and resume after a service interruption. Human acceptance remains a separate action.
- Align App/Web/package version metadata and refresh English/Chinese onboarding.

See [0.8.1 scope and validation](docs/RELEASE-081.md). The following alpha entries are historical development notes, not a list of guarantees for every host or platform.

## 0.8.1a1 — 2026-10-05

内部 **alpha** · 本地 tag `v0.8.1a1`（**未推远端，未发布 PyPI / npm / GitHub**）。
自 `0.8.0` 起为**纯增量**：反风暴加固 · 检索/团队墙/审计链 · 交付证明 · 记账视图 ·
能力与工具契约 · 审核机制（升级预算/同行校验）· 文件认领 · 探测入伙 · 单向桥接 ·
产物按预算读取 · 通讯录白名单 · 唤醒阶梯 · 四原语 · 观察窗 · 一键自检。

> npm 伴生插件 `dsh-agent-mailbox` 仍标 `0.8.0`：它的版本描述的是**已发布的东西**，
> alpha 未发布故不跟随；待 `0.8.1` 正式发布时三渠道（GitHub/PyPI/npm）一并同步。

### 可靠性（反风暴）
- 自启单元熔断：速率熔断 + 「已死但重试过千次」只告警不杀；周期单元无 `KeepAlive`（`storm_breaker.py`）
- 回声闸门：同往返 ≥3 轮且无状态变化 ⇒ 判回声；冻结线程拒发（`echo_guard.py` + `send_message` 执行点）
- 单向刷屏闸门 + 通知折叠（`detect_flood` / `fold_notice`）
- 背压折叠：双窗分层（短窗 10/300s · 长窗 30/6h），标题保留、正文丢弃、条数有界；
  工作请求 / 幂等消息 / 人工消息永不折叠（`backpressure.py`）
- 失败与取消**不自动回信**（回归锁）

### 检索（T5）
- `workbench_search.py`：FTS5(trigram) 索引 + 三条触发器随 messages 增删改同步；索引按需自建、无 schema 迁移
- **短查询回退**：trigram 需 ≥3 字符（实测「风暴」命中 0）⇒ <3 字走 LIKE 扫描，并在结果里标明 `mode`
- 结果默认**只给标题+片段**，`--bodies` 才带正文（省 token）；支持项目/员工/收件箱-发件箱过滤
- CLI：`agent-mailbox search <词> [--project …] [--limit …] [--json]`（暂不新增 MCP 工具面，留给 T29/T30 契约定版）

### 团队墙 + 审计链（U7 + 第四成败手）
- `workbench_wall.py`：`wall()` 一屏三问（在跑/等谁/卡住）+ 冻结线程 + 折叠计数 + 健康行；
  `audit_chain()` 还原单封信全生命周期（发→收→claim→回执→折叠/冻结→终态）
- **三约束已写成测试**：pull-only（无推送）· **不引入新写者**（零写 + 不许顺手建索引）· 聚合去噪（不搬消息流水）
- 无项目归属的治理事件按"全局可见"处理（实测：冻结事件不带 project_id 时，按项目看墙会漏）
- CLI：`agent-mailbox wall [--project …] [--json]` / `agent-mailbox audit <消息ID>`
- 真实数据验证：墙上直接显示 T8 那单在"等人验收"；审计链还原 8 步（含 actor 与时间戳）

### 协议 v2 草案（仓内，不发布）
- `docs/protocol-v2-draft.md`：接收侧七闸（S1–S7，每条附 2026-10-04 实测反例）
  + **发送侧对称三条（S-send-1 滑动窗口上限 / S-send-2 语义重复查重 / S-send-3 止损后静默等待）**
  —— 三条来自 HS 对 353 封根因的第一手供认
- 一致性用例表（狗粮）：熔断 10 / 背压 12 / 回声 13 / 墙 6，**发送侧三条尚无实现**（HS 侧先做）
- 状态：未发布、未对外；发布时机是老板三裁决点之一

### 水位线第一步（背压治慢滴 · 与 HS 共识版）
- `workbench_views.py`：`mail_view_marks(员工,对端,last_viewed_at)` 表，**惰性幂等创建、无 schema 迁移**
- **只喂折叠、不进 API 语义**：无已读回执、消息无 read 状态、`viewing_acknowledges` 仍为 False
- **单调不减**（HS 补的不变量）：更早/相同时刻不回退
- **kill-switch**：`AGENT_MAILBOX_BACKLOG_FOLD=0` 整体关闭积压档（水位线随之失效）
- 背压新增第三档「按未看过的积压折叠」（默认上限 20，`AGENT_MAILBOX_BACKLOG_LIMIT` 可调）；
  与速率双档并列，任一命中即折叠
- 读信（`employee_messages` 的 inbox）记水位线：**不变量测试断言"查看永不改任何状态"**

### 交付证明（T12 · 补齐 mailbox 任务那一半）
- `workbench_proof.py`：`build_proof()` 交接单（任务/交付人/验收项+自检+证据引用/产出物/自检/补丁哈希）
  · `artifact_ref()` 给 **`art:<sha256[:16]>` 引用 + 哈希 + 字节数**，**绝不内联正文**（waggle 教训）
  · `verify_proof()` **重算哈希**：`verified` / `mismatch` / `incomplete` / `no_proof`；可留痕，**不推进终态**
  · `render_verdict(proof, verification)` **只吃 proof**（不接 store、不接聊天）⇒ 判据形状由签名保证
- 交付证明落 `governance_events`（`delivery_proof` / `delivery_verified`）⇒ **自动进审计链**，不新增表、不新增写者
- CLI：`agent-mailbox proof <task_id> [--verify]` · `agent-mailbox proof-verify <task_id> [--record]`
- 实测发现：产品**已有** workspace 交付结构（含 `patch_sha256` 与「员工报告不等于系统测试通过」），
  但 **mailbox 任务（主路径）是空的**（`task_delivery` 返回 `files: []`）—— T12 补的就是这一半
- 真实数据：T8 那单已记录证明（1 产出物 / 12 验收项）→ 系统校验 **verified** → 一屏可判

### 按任务记账（T11 · 派生视图 · 零写）
- `workbench_ledger.py`：`ledger()` 按任务（时长/事件数/事件类型/关联信数/证明与校验结论/失败码）
  + 按员工**上游健康**（单量/在跑/待人/失败/成功率/最近失败/能力口径）
- **零写、无新表**（有测试）；**默认去噪**：不列零活动员工（`include_idle=True` 或 `--all` 才全列）
- 能力字段复用 `store._employee` 的同一计算 ⇒ 账本与产品口径不可能漂移
- 与交付证明联动：任务行直接显示 `has_proof` / `proof_verdict`
- CLI：`agent-mailbox ledger [--project …] [--all] [--json]`
- **实测发现（对 T29/T30 有价值）**：受管路径**已经**拒绝无适配器员工（`ADAPTER_UNSUPPORTED`），
  而 mailbox 路径按设计允许（人派活、员工走自己的 app/session）—— 修正"能力声明是空话"的说法：
  真正缺的是**建单时的能力提示/路由**，不是拒绝逻辑

### 能力契约单源化（T29）
- 新增 `workbench_contract.py`：**唯一事实来源** —— `EXECUTION_KINDS` / `execution_supported()` /
  `execution_sql()`（参数化 SQL，无字符串字面量）/ `use_kinds()`（就地替换，供运行时与测试）
- 收口 **7 处**写死的可执行集合：`store._employee` · `store.create_task` ·
  **store 认领 SQL 内那处 `IN ('codex','claude')`** · `onboarding` 探针 · `runtime` 发现（×3）
- `workbench_runtime.SUPPORTED` 改为契约**别名**（不复制字典）⇒ 改契约全仓同步
- 测试 6 例：改契约后**员工视图 / 建单 / 认领 SQL** 行为必须跟着翻转；
  另加**防回归守卫**（源码里再出现写死的可执行集合即失败）
- `execution_verified` 语义保持：**契约放行 ≠ 已验证**（仍要求真实证据）

### 工具契约与两套面收敛（T30）
- 新增 `workbench_tools.py`：**声明式工具契约**（14 个工具 × 作用域 + 用途）
  · 9 个共用（`mailbox` 与 `workspace/remote` 同名同参）
  · 3 个仅 mailbox：`project_tasks` / `project_task_accept` / `project_task_submit`（员工自己的接单-交付路径）
  · 2 个仅 workspace/remote：`project_code_search` · **`team_message`**
- **测试直接建真实服务器并 `list_tools()` 比对**（进程内，非源码正则）：
  两套面的工具集必须与契约完全一致；**9 个共用工具的参数签名必须逐一致**
- 测试当场抓出**我此前漏掉的第 14 个工具 `team_message`**（会建任务卡的同事派活）
- 记录一处**刻意的不对称**：`team_message` 不发给绑定邮箱会话的员工 —— 防 agent 间派活级联
  （今日风暴的同源风险）；若要放开须先过反风暴评审

### 审核机制（notify_policy / escalation_budget / peer_review）
- 新增 `workbench_policy.py`：策略**派生自治理事件**（`policy_updated`），**不新增表**；
  解析顺序 = 默认 ← 全局覆盖 ← 项目覆盖（最新胜）
- **升级预算**：每小时自动升级数超预算即**抑制并留痕**（`escalation_suppressed`）——
  把反风暴 S4 用到"升级"这条最容易变风暴的路径上（今日实测：日推 157 条）
  ⚠️ **如实标注（工具+独立审查发现）**：`record_escalation()` 当前**生产零调用者** ——
  机制与测试都在，但 v0.8 尚无"推送/升级"执行路径（通知归飞书接入层，未接）。
  即：**预算目前不会真的被花掉**；接入层落地时必须在真实升级点调用它，否则形同虚设。
- **同行校验**：策略 `peer_review=required_for_write` 时，写能力交付**必须由另一名员工**校验通过
  才可合入（`apply_delivery` 门）；**自己验自己不算**；`verify_proof(verifier_id=…)` 可归因到人
- **notify_policy** 只声明不推送（`manual_check` 默认）——推送属飞书接入层（后续做）
- `mailbox_policy` 响应由策略视图生成（历史键名保留，响应不回归）
- CLI：`agent-mailbox policy [--project …] [--set k=v]`（修改写 governance 事件，可审计）
- 测试 7 例：默认值/校验/全局与项目覆盖/预算抑制+留痕/默认 off 不改行为/换人校验/策略不碰任务

### 文件认领 lease + pre-commit 拦截（T4）
- 新增 `workbench_lease.py`：路径认领（独占/共享、TTL、显式释放）**全部派生自治理事件**
  （`path_claimed` / `path_released`），**不新增表**；TTL 到期自动失效，无需后台回收器
- **最小 gitwildmatch 子集**自实现（`**` / `*` / `?` / 尾斜杠），**不引新依赖**；
  冲突判定**保守**（静态前缀 + 双向匹配 ⇒ 宁可多报一次，不可两个 agent 互相覆盖）
- `scripts/lease-guard.py`：**pre-commit 拦截**——暂存文件落在他人活跃认领内即拒绝提交，
  并给三条解法（等到期 / 请对方释放 / `--no-verify` 人工越权且责任在人）
- CLI：`agent-mailbox lease list|claim|release|check`（`--print-hook` 输出钩子片段）
- 测试 9 例：匹配语义 / 保守冲突 / 独占 vs 共享 / 释放 / TTL 过期 /
  安全（不碰任务与消息）/ **拦截器端到端**（被拦、认领者自己不被拦、缺配置退出码 2）

### 自动探测与一键入伙（T2）
- 新增 `workbench_enroll.py`：把已有探测机制做成**产品路径**
  · `discover()` 归一化（已安装优先、可执行优先，带 auth 状态）
  · `plan()` **只读**：谁可入伙、谁已在项目、给出确切命令
  · `enroll()` **幂等**：复用已有员工、签发新会话、写 0600 会话文件，返回**一个粘贴块**
  · `onboard(apply=False)` 默认**演练**：看一眼绝不动机器（有零写测试）
- **one paste 达竞品标准**：按宿主生成其自身配置形状
  （Hermes YAML / WorkBuddy JSON / Codex TOML / Claude CLI / generic JSON），
  格式对齐本机真实配置；实测产出可直接粘贴的 Hermes 块
- CLI：`agent-mailbox discover | plan | enroll <kind> | onboard`（`--apply` 才动手）
- 测试 11 例：归一化排序 / 计划零写 / 演练零写 / 幂等（复用员工+签发新会话）/
  会话文件 0600 且不含令牌于员工视图 / 5 种宿主粘贴块 / 非法输入
- 真机实测：发现 17 个（Claude Code 与 Codex 已认证且可执行）

### 单向桥接（T7 · 旧信 → 任务卡）
- 新增 `workbench_bridge.py`：v0.7 信件 **单向投影**为 v0.8 任务卡
  · **保守判定**：只认派工标记（【任务/任务书/派工/工单），并排除
    `Re:` 回复与**明确写着"非派工/思路参考/仅供/通报"**的信 —— **每次跳过都带原因**
  · **单向**：旧信箱只读，绝不写回（测试用文件指纹断言"禁止双写"）
  · **幂等**：映射记在治理事件 `letter_projected`（不新增表），重跑只报 `already_projected`
  · **默认演练**（`apply=False`）：看一眼绝不建卡
- CLI：`agent-mailbox bridge scan | project | status`（`--apply` 才动手）
- 测试 6 例：保守判定 / 跳过原因 / 演练零写 / 建卡+幂等 / **单向指纹** / 显式映射
- **真机结论（重要）**：`~/.agent-mail` 467 封里**仅 1 封**通过严格判定
  （其余 466 封为回复/回报/非派工参考）⇒ **旧档不是未消化的任务积压**，
  桥接的价值在"可追溯"而非"批量迁移"

### 产出物引用按字节预算读取（T6）
- 新增 `workbench_artifact.py`：`art:<sha256[:16]>` **引用可解析、可按预算取回**
  · 索引**派生自交付证明事件**（不新增表、不新增写者）
  · `read(budget=…)`：**只返回预算内的切片**，给 `truncated` 与 `next_offset`（可续读）
  · **单次硬上限 64KB**：防一次性灌爆上下文（引用永远不自动展开）
  · **投影**：`text` / `head`（只首行）/ `json_keys`（只给键与形状，不给值 —— 审计场景）
  · **安全**：哈希不符 ⇒ **一个字都不给**（返回被篡改的证据比返回空更危险）
- CLI：`agent-mailbox artifact list | read <ref> --budget N [--offset N] [--projection …]`
- 测试 6 例：索引与解析 / 预算与截断续读 / 硬上限与校验 / **篡改拒绝给内容** /
  投影（head、json_keys 不给值）/ 只读断言
- 真机演示：T8 证明产出物 1446B → `--budget 200` 只回 200B（截断标记 + 哈希校验通过）

### 通讯录 / 握手白名单（T33）
- 新增 `workbench_contacts.py`：谁是可达的（地址簿 + 请求/接受）
  · **默认开放**：收件人只要**没有**通讯录，任何人可发 —— 严格性是**显式开启**的，
    因此开启本能力**不会**打断既有项目（有测试保证）
  · **请求不授权**：`contact_requested` 只是意向，只有 `contact_added` 授予访问，
    `contact_removed` 撤销；握手用既有治理事件表达，**不新增表**
  · **执行点**：`send_message` 在收件人已设列表时拦截，错误码 `CONTACT_REQUIRED` 并写明如何解
- CLI：`agent-mailbox contacts list|add|remove|request|check`
- 测试 6 例：默认开放 / 一旦有列表即绑定 / 请求不授权 / 移除恢复开放 /
  自己与人工消息不受限 / **发送路径拦截** / 列表只读

### 唤醒阶梯与钩子简报（T31）
- 新增 `workbench_brief.py`：**档 1 钩子轻通道** —— 宿主会话起始调一次，把
  **有字节预算**的简报贴进上下文；**零常驻、只读、去噪**
  · 三问：未读 / 等人验收 / 在跑 + 策略（通知模式、同行校验、通讯录）
  · **把"安静但异常"顶上来**：已折叠数、升级被抑制数（安静不等于无事）
  · **预算真守住**：截断时先给提示语留位置（实现里修过：原先截断后反而超预算）
- `docs/wake-ladder.md`：唤醒阶梯规格（0 主动查 → 1 钩子 → 2 可选单内核；
  **永不 per-letter**），并写明档 2 启用时必须满足的护栏（S1/S3/S4/S5/S7）
- CLI：`agent-mailbox brief [--employee …] [--project …] [--budget N]`
- 测试 5 例：三问正确 / 只读 / 预算（含硬上限校验）/ 安静异常上浮 / 空范围可渲染

### 四原语 run / schedule / watch / inspect（T32）
- 新增 `workbench_schedule.py`：吸收竞品"它工作时你在睡"的方法，守自家纪律：
  · `schedule` = 治理事件（`task_scheduled`），**最新排期胜**；到期集合**派生**（只认 queued）
  · `watch` = **拉取式循环 + 超时**，不是监听器 ⇒ **零常驻**；时钟可注入（测试不用真等）
  · `inspect` = 既有历史（任务事件 + 治理事件 + 排期轨迹）
  · `run` = **对 mailbox 任务说实话**：平台不替员工接单，返回 `needs_employee_accept` 与提示
- 修一处真缺陷：`due_tasks` 原先依赖 `governance_events()` 的返回顺序取"最新排期"
  —— 实测该顺序**不保证时序**，改为**显式按 `created_at` 取最新**
- CLI：`agent-mailbox run <task> | schedule <task> --at ISO | watch [--timeout S] | inspect <task>`
- 测试 6 例：排期与到期派生 / 改期最新胜 / 项目隔离 / watch 立即返回与超时 /
  inspect 历史 / run 的诚实语义 / 到期与查看**零写**

### 观察窗开始计时（完成判据 = 事件计数）
- 新增 `workbench_observe.py`：把共识判据做成**可执行**的评估
  · `start()`：写一条 `observation_started` 治理事件，**幂等**（重复调用不覆盖起点，
    防止"重新计时"把窗口洗白）
  · `evaluate()`：四项判据从账本实时算出 ——
    **0 风暴**（折叠+冻结+升级抑制计数）· **0 丢失**（抽样信件必须端到端可还原）
    · **≥10 次真验收**（`review → done` 由人推进）· **审计抽样 20/20**（比例必须 1.0）
  · **只统计窗口内**（按起点过滤）；`evaluate/window` **只读**（有测试）
- 口径说明：不定义模糊的"丢失"，而是要求**每封信都能被还原** —— 那才是我们真正怕的失败模式
- CLI：`agent-mailbox observe [--start] [--sample N] [--json]`
- 文档：`docs/observation-window.md`
- 测试 5 例：起点幂等 / 闸门与验收计数 / 审计可还原性 / 未开始时诚实报"未开始" / 只读
- **实测已开启**：起点 `2026-10-04T18:41:15Z`（本地 2026-10-05 02:41）· 判定"观察中"

### 端到端自检（我们自己的功能必须"合在一起"能跑）
- 新增 `tests/test_integration_e2e.py`：在干净 home 上走完整条产品链 ——
  **开观察窗 → 探测/入伙 → 派单（mailbox 任务）→ 排期与到期 → 会话简报 → 文件认领
  → 接单 → 提交 → 交付证明 → 同行校验（自己验不算）→ 产物按预算读回
  → 团队墙/账本/审计链 → 检索 → 通讯录白名单 → 单向投影 → 观察窗判定**
- 自检当场抓出两处**我们自己的功能缺陷**（均已修）：
  · `claim_paths` 不校验参数 ⇒ 传员工字典会崩在深处；现改为**清楚报错并提示传 `employee['id']`**
  · `add_contact/remove_contact/request_contact` 同样不校验 ⇒ 统一加 `_require_ids`
- 同时确认：mailbox 任务的接单/提交只走员工自己的会话（`submit_mail_task` 拒绝非本人员工任务）

### 一键自检 `agent-mailbox selfcheck`（我们自己的功能，自己就能验）
- 新增 `workbench_selfcheck.py`：**临时 home** 上把整条产品链跑一遍并逐步报结果
  （建项目 → **开观察窗** → 会话绑定 → 派单 → 排期/到期 → 简报 → 认领 → 接单 → 提交
  → 交付证明 → 同行校验 → 产物按预算读回 → 墙/账本/审计 → 检索 → 通讯录 → 单向桥接 → 观察窗判定）
- **15 步**，退出码 = 是否全过；**绝不碰真实 home**（临时目录自动清理，有测试）
- 自检自身也进测试套件（它一旦坏了会红）
- CLI：`agent-mailbox selfcheck [--json]`

### 独立审查修复（2026-10-05 · 第一批：4 处真缺陷）
> 来源：**独立审查者**（不同 agent、只审不改、实跑复现）审 v0.8.1a1 批次，
> 结论"3 高 / 5 中 / 4 低；作者『全绿』成立、『零写/不新增表』部分不成立"。本批修高危与高危相邻项：

- **H1 账本截断让派生视图静默失真**（`governance_events()` 固定 `LIMIT 200`，被 6 个派生视图当全量）：
  超过 200 条事件后，通讯录结论会与执行点**相反**、观察窗"忘记起点"并**再写一个新起点**、
  桥接**重复投影建卡**、产出物引用与证明"查不到"。修：`limit=None` 取全量 + 派生视图一律全量
  （真实 store 当前 44 条 ⇒ 尚未失真，属定时炸弹）
- **H3 策略不可逆**（`peer_review` 一旦开启就关不掉）：`_parse` 曾把字符串键的 `"off"` 归一成 `False`，
  再被字符串校验拒绝。修：**按键类型归一**（布尔键才做布尔归一）+ 字符串键小写容错（`"Off"` 可关）
- **M 简报 SQL 注入**：`waiting_on_human` 曾把 `employee_id` 拼进 SQL（撇号崩成"磁盘错误"误导、
  `x' OR '1'='1` 能读到**别人的**待验收任务）。修：参数化
- **M `json_keys` 投影无视预算**：曾整份结构吐出（实测 20000 键 → 420,000B）且谎报 `returned_bytes=0`。
  修：有界结构遍历 + 诚实字节数 + 兜底摘要
- 新增 `tests/test_review_regressions.py`（6 例，逐一锁定上述缺陷）
- 其余未修项（H2 白名单可被群发绕过 · lease-guard 三处绕过 · `search` 实为写 · CLI 裸 traceback ·
  `record_escalation` 无生产调用者）**明列为 OPEN**，下一批处理

### 独立审查修复（第二批：安全与语义）
- **白名单可被群发绕过**（高）：员工只要**不填收件人**就能全项目广播，目标员工照样收到 ⇒
  白名单形同虚设；且员工侧 MCP `project_message` 的 `recipient_id` 默认空串 ⇒ **默认就是群发**。
  修：员工群发在"本项目已有员工设通讯录"时**拒发**（`CONTACT_REQUIRED`）；人工群发不受影响
- **lease-guard 三处绕过**（中）：
  · 非 ASCII 路径被 git 转义成 `"src/\344\270\255…"` ⇒ 匹配不上、直接放行
  · `--diff-filter=ACMR` **漏掉 D** ⇒ **删除**别人认领的文件不拦（最容易毁掉对方工作的动作）
  · 提示里"`--no-verify` 会留痕"**不实**（仓里没有任何留痕机制）⇒ 改为如实说明"会跳过闸门且不自动留痕"
- **匹配器语义纠正**（中）：原先并非 gitignore 语义 —— `tests/` 匹配不到 `pkg/tests/x.py`，
  而 `src/**/x.py` 反而匹配 `src/abx.py`。重写：**未锚定模式任意深度生效**、**`**/` = 零个或多个目录**、
  含斜杠即锚定根（新增 8 条语义断言 + 两条绕过端到端断言）

### 独立审查修复（第三批：CLI 自解释 / 认领副作用 / 同秒时序）
- **CLI 裸 traceback**（中）：`schedule`/`lease`/`contacts`/`bridge` 等只捕 `ValueError`，
  `WorkbenchError` 直穿成 Python 栈。修：`main()` 变薄壳，统一自解释 + 带命令名 + **退出码 2**
  （实测三条命令：rc=2、traceback 0）
- **`run_now` 会顺手认领别的任务**（中）：盲目 `claim_task` 按排队顺序取第一张，
  且无 `starting → queued` 回退路径。修：新增**只读** `store.peek_claim()`（与写入共用同一段 SQL），
  先窥视再认领；若不是这一张 ⇒ `not_eligible` + `would_claim` + 提示，**零副作用**
- **同秒平局不确定**（中）：多个视图用 uuid 或扫描顺序在**同秒**事件间"取最新" ⇒ 结果随机。
  修：新增叶子模块 `workbench_events.event_order = (created_at, 插入序 _seq)`，
  事件行带 `_seq`（rowid）；`contacts` / `schedule` / `bridge` / `proof` / `observe` / `echo_guard`
  统一改用（2 例同秒回归测试：排期"最新胜"、通讯录"先加后删=删"）

### 独立审查修复（第四批：检索读写分离）
- **`search()` 名为只读、实为建表**（中）：一次"只读检索"顺手建 `fts_messages` + 5 张影子表
  + 3 个触发器并回填 ⇒ 与"零写/不新增表"的承诺矛盾（同批的 `wall` 都知道要避开）
- 修：**检索只读、建索引显式**
  · `search()` 只读探测索引；未建则走 LIKE 并回 `mode="like_no_index"` + `hint`（仍查得到，不静默降级）
  · `index_stats()` 只读（新增 `index_built`，`in_sync` 仅在已建索引时为真）
  · 新增**显式写入口** `build_index()`；CLI `agent-mailbox search --index`
- 测试：新增"检索不建表"回归（`sqlite_master` 前后对比），并把"索引已建"设为该文件用例的前提
- 自检与端到端用例改为显式建索引

### 代码认知层修复（AOCI 索引重建为 v0.8）
- **问题（工具实测）**：本仓沿用了 v0.7 worktree 的 AOCI 索引 —— **42 条目 / 60 处指向不存在的文件**
  （`server.py` / `wake.py` / `web.py` / `ci.yml`…，条目原文还写着 `agent-mailbox 0.6.2`、13 工具），
  `aoci check` 与 `aoci index inventory` 直接报 `code_object_path_unresolved` ⇒ **认知层实际没生效**
- **处置**：旧索引**归档**到 `~/tools/_archive-v07/aoci-heritage-20261005/`（含原因说明），
  随后为 v0.8 `aoci init --locale zh-CN`（v1 正式认知不可覆盖，须先移出旧件）
- **现状**：索引可解析（2 区段 / 0 条目 / 0 警告）· 基线已建（**291 个文件**，`.venv` 已排除）·
  `aoci check` 可跑并如实报告 **241 项待撰写**（骨架有效但为空）
- 下一步（需决策）：配置 `aoci ai` 端点后由 `aoci index build` 批量起草条目，或人工撰写

### 独立审查修复（第五批：同行校验绑定版本 + 空证明不得放行）
- **橡皮图章**（中）：`verify_proof` 对**没有任何产出物**的证明也判 `verified` ⇒
  可用来"满足门禁"。修：空证明 ⇒ `incomplete`（无需验证 ≠ 已验证）
- **同行校验未绑版本**（中）：原先只要存在一条 `verified` 即放行 ⇒ **交付内容整批换新（新
  `proof_sha256`）后旧校验依旧有效**。修：`peer_verified_in` 必须命中**当前最新一版**证明的
  `proof_sha256`；换版即失效，需重新换人校验（2 例回归：换版失效 / 重验恢复）

### 第二次对抗性复查修复（审查者判定"已全部修复"不成立）

> **更正说明（2026-10-05 · 第二次对抗性复查后）**：本文件此前多处"已修复"的声称经独立复查
> **实测不成立**，已逐条修正并补回归测试。具体：
> ① "白名单可被群发绕过（高）…修" —— **第一版不成立**：`_whitelisted_in` 按"最后一个 add/remove 事件"
>    判断，正常删掉一人即关闸（复查者用真实 CLI 复现，消息确实到达白名单收件人）；已在
>    "第二次对抗性复查修复"里按"与通讯录视图同源"重做。
> ② "cli.main：不再吞掉子命令退出码" —— 对 `python -m agent_mailbox` **不成立**（`__main__` 未传播返回码）。
> ③ "CLI 裸 traceback（中）…修" —— **未修全**：`proof-verify` 遇"无证明"仍吐栈。
> ④ "必须由另一名员工校验通过" —— **未强制**：CLI 唯一留痕入口写 `verifier_id=None`（开策略即死锁），
>    API 又接受任意字符串当"员工"。
> ⑤ "json_keys…有界" —— 非硬界：小预算（1–50B）兜底摘要仍超预算、深嵌套抛 RecursionError。
> ⑥ "ruff 全过" —— 只跑了 `ruff check`；CI 同时要求 `ruff format --check`，当时 45 个文件未格式化。
>
> 以上 6 条现均已真修（见下文各条与 `tests/test_review2_regressions.py` 11 例）。

- **H2 真修**：群发闸门与通讯录视图同源（删掉名单里任何一人不再把关卡整体关掉）
- **同行校验真实可用且被强制**：
  · CLI `proof-verify --record` 新增 **`--verifier`**（必填）——原先写 `verifier_id=None`，
    开策略后 `apply_delivery` **恒被拒**（功能性死锁）
  · `verify_proof(record=True, verifier_id=…)` 现在**校验身份**：必须是本项目在职员工且非交付人
    （原先任意字符串如 `whoever_i_say` 都被接受）
  · **TOCTOU**：合入前**重算哈希**，同行验过后再替换产出物不再放行（`PROOF_NOT_VERIFIED`）
- **`python -m agent_mailbox` 退出码**：`__main__` 传播返回码（`-m` 与控制台脚本一致）
- **`proof-verify` 无证明不再裸 traceback**：`no_proof` 返回键集统一（含 `mismatch`/`missing`）
- **MCP `project_message` 收件人必填**（三处）：默认空串 = 默认全项目群发，已禁止
- 两处低危：短查询 LIKE 转义 `%`/`_`（原先查 `%` 命中全部）· `head` 投影 `returned_bytes` 报真实内容字节
- **只读路径不再拿写锁**（复查者中危 #2）：`_transaction(readonly=True)` ⇒ 只读连接 +
  `PRAGMA query_only` + 延迟 `BEGIN`；13 处只读视图切换（search/wall/ledger/brief/observe/
  schedule/enroll/policy/lease）。**锁竞争不再谎报磁盘故障**：`busy`（稍后重试）·
  `internal_error`（参数/类型）· 真磁盘错才 `storage_error`；`busy_timeout` 可用环境变量覆盖
- **未知 `--project` 不再静默**：新增 `store.ensure_project()`，`wall/ledger/contacts/brief/
  plan/active_claims` 一律校验；坏 project ⇒ `not_found` + CLI rc=2（原先 rc=0 当"全局/空"）
- **artifact 同内容多副本取新**：索引保留最新（原先留最旧、归属错）；`read` 依次尝试候选 ⇒
  最旧副本被删后仍有等价副本可用
- **深嵌套/大文件不再出栈/爆内存**：`json_keys` 捕获 `RecursionError` ⇒ `too_deep`；
  新增 `MAX_ARTIFACT_BYTES`（64MB）⇒ `too_large`，不整份读进内存
- **lease 路径规范化**：`src/../etc/passwd` 不再绕过 `etc/**` 认领
- **补上 CI 的格式门槛**：`ruff format --check src tests` 原先 45 文件未格式化（我只跑了
  `ruff check` 就宣称"ruff 全过"）⇒ 全仓格式化，现 101 文件全部合规
- 新增 `tests/test_review2_regressions.py`（**13 例**，逐条锁死；含并发只读、退出码一致性、
  深嵌套/大文件、路径规范化）

### 第三轮对抗性复查修复（0 高 / 4 中 / 8 低）
> 第三轮复查结论：这批修复**可用**，但有两处"声称修好实际没修全"、一处产品级承诺漏网。
> 本批逐条修完（`611b82f` 起），并新增 `tests/test_review3_regressions.py`（11 例）。

- **收件人必填只做在 schema 层**（中）：`invoke()`（宿主 MCP → HTTP → invoke 共用）与 fleet
  远端路由现对**空串/缺参**一律 `MAILBOX_ARGUMENT_DENIED`（原先 `or None` 又翻译成全项目群发）
- **未知 `--project` 覆盖不全**（中）：`effective_policy` / `search` / `due_tasks` 下沉项目存在校验
  —— 原先 `policy --project 打错` 会 rc=0 打印**默认值**（查"这项目开没开同行校验"得到相反答案）
- **错误分流没做完**（中）：新增 `_classify_operational_error`：`readonly_violation` /
  `internal_error` / `constraint_error` / `busy` / `storage_error` 按 SQLite 报错分流，
  **只有真磁盘类**才是 storage_error（原先只读路径偷写、缺表缺列、语法错全报"请检查磁盘"）
- **64MB 上限只挡首选候选**（中）：大小闸对**每个候选**生效；`artifact_ref` 改**分块哈希**
  （200MB 产出物不再整份进内存）；非普通文件（FIFO/目录）直接拒绝——原先 FIFO 会**永久挂死**
- **IntegrityError 也分流**（中3 补）：NOT NULL/UNIQUE/外键违约走的是 `IntegrityError`
  （不是 OperationalError），原先落到 catch-all 报「请检查磁盘」；现归 `constraint_error`
- 低危：只读连接不再执行 `PRAGMA journal_mode=WAL`（非 WAL 库上"只读"曾真写库头）·
  `too_large` 不再谎报 `hash_verified=False`（新增 `hash_checked`）· 门在**写事务内复检**
  （原先门后重录证明/策略翻转仍会合入）· 账本新增 `peer_verified`，把「系统校验」与「同行校验」分开 ·
  busy 文案点明"嵌套在已有事务内"这条真因 · 强化 rv2_6（改成真调用）与 rv2_14（断言确切回落值）

### 第九轮对抗性复查修复（R9b：核心修复成立 + 1 中 / 1 低 + 自查 1 项）
> R9b 审查者做完主项、并用**变异实验确认** rv8_1/rv8_2/rv8_3 三条测试"改坏即红"（真测试），
> 随后崩溃；结论仍由其增量落盘的 `/tmp/amr9b/report.md` 保住。

- **核心复验成立**：`read()` 的无条件二次哈希对三类改写（等长就地改写 / 变长追加 / 截断）
  **全部拒绝**且 `content is None`；小文件、空文件、正好等于 budget 的文件、正常内容均未被弄坏
- **`budget=1` 遇多字节字符静默丢字**（中）：9 轮续读每轮都返回空文本、3 个字符全丢且**毫无信号**
  （前瞻条件漏了"本轮没解出任何字符"这一支）。修：条件补 `or not text`。新增 `rv9_1`
  （budget=1 逐轮续读拼回 `中中中`）
- **拒绝时报 `hash_verified=True`**（低）：对**从未交出**的字节声称已校验。修：拒绝分支改
  `hash_verified=False` + `hash_checked=True`（说明"查过、且没通过"）。新增 `rv9_2`
- **自查（升级路径隐患）**：`_ready()` 只校验 6 张表的列 ⇒ 缺**辅助表**（`task_links`
  `task_workspaces` `task_deliveries` `resource_*` `employee_probes` `mail_view_marks` 等 9 张，
  由各模块按需创建）既不检测也不修复，**读路径才炸**（`task_detail` 报"结构或语句不匹配"）。
  修：辅助表纳入开库检查与幂等修复（FTS 表除外——它是派生物）。新增 `rv9_3`/`rv9_4`
- **性能诚实记录**：一次 `read()` = 2 遍全量哈希 + 1 次窗口读（8MB 实测 7ms、峰值内存增量 0，
  哈希是 1MiB 分块流式）。R9b 建议改为"单遍流式边哈希边截窗口"（省 1 遍 I/O，安全等价）——
  **本轮不采纳**：最近两次高危均是作者自身的"优化"引入，正确性优先；该优化记入 backlog，
  待有压测需求再评估。

### 第八轮对抗性复查修复（范围收窄的复验：1 高 / 3 中 / 5 低；判定「部分成立」）
> 第八轮复查（SHA 冻结 `80ceb04`）**跑完了**（169 行报告落盘 `/tmp/amr8/report.md`），
> 并确认第七轮 8/9 个回归测试是真测试（变异实验：改坏即红）。
> 但它抓出**我在第七轮引入的一个高危反向门控**，以及三处第七轮没做完的面。

- **`read()` 的二次哈希守卫被写反**（高，我第七轮引入）：本意"整份读完 ⇒ 无需复核"，
  实际成了"**只有整份读完才复核**" ⇒ 大于 `budget` 的文件被就地改写时，
  返回**从未通过哈希校验**的字节却自称 `hash_verified=True`（sha256 溯源可被绕过）。
  第七轮的回归 `rv6_7` 用的是 48 字节文件 + `budget=200`（整份读完）⇒ **新旧实现都绿，零覆盖**。
  修：删掉门控，恢复**无条件**二次哈希；docstring/CHANGELOG 措辞同步纠正。
  新增 `rv8_1`（4000 字节文件 + `budget=100` + 等长就地改写 ⇒ 必须拒绝）
- **版本最新但缺表：不补表、不报错，只把库头翻成 WAL**（中）：`_ensure_required_columns`
  对整张缺失的表 `continue`（注释说"交给建表路径"，而那条路径只在版本落后时才走），
  任何命令（含纯读）都会 `_ready()`→False → 零修复 → 无条件切 WAL。
  修：新增 `_ensure_schema()`（逐条 `CREATE TABLE/INDEX IF NOT EXISTS`，可放进事务），
  缺表时真的重建。复验 `probe_migration` P8：`permissions exists=table`
- **补列非原子 ⇒ 半修状态 + 不可行动错误**（中）：连接是 autocommit，逐条 `ALTER` 立即提交，
  中途失败（如某张表被换成视图）会把前半修**持久化**。
  修：整个"建表 + 补列"包进 `BEGIN IMMEDIATE`/`COMMIT`，失败 `rollback` 并把
  `view`/`locked` 映射为可行动错误（指向 `state-v*-before-v*-*.sqlite` 备份）。
  复验 P10：两次尝试后 `tasks.model present=False`、`jm=delete`、库头 `1/1`（不再半修）
- **`rv7_9` 是空测试**（中）：声称"缺列时只读命令不写库"，却从不 `DROP COLUMN`、也不重开 store
  （变异 M9 下仍绿）。修：真造缺列 + 真重开 + 再验读 API 不切 journal（并另加 `rv8_4`）
- 低危：`rv7_4` 不再依赖宿主是否装有 agent CLI（无 suggestions 时不再 `IndexError` 假红）·
  lossy 记账自洽（`returned_bytes` 恒等于返回内容字节，新增 `lossy_dropped_bytes`
  报出"未被文本呈现的源字节数"，不予静默）· 性质测试的检测器现在**认 `raw_connect()`**
  （此前它被整条当原语免检，读语义函数可用它绕过只读性质）· `private_mode` 注释与实现对齐
  （放宽的权限仍会被修回，包括 setuid/setgid/sticky，安全性未削弱）·
  `bridge --limit` 负值改为 `invalid_field`（不再静默当 0；`--limit 0` = 不处理任何候选）
- 新增 `tests/test_review8_regressions.py`（4 例）+ `rv7_9` 改为真测试

### 第七轮对抗性复查修复（已完成攻击面 1–6：1 高 / 5 中 / 6 低）
> 第七轮复查（SHA 冻结 `37747a2`）同样崩于报告中途，但增量落盘的 `/tmp/amr7/report.md`
> （369 行）保住了攻击面 1–6 的全部结论；它另做了 **13 组变异实验**验证测试真伪。
> 攻击面 7–8（新风险 / CHANGELOG 一致性）未完成，需下一轮补。

- **迁移失败前就已把库翻成 WAL**（高）：`PRAGMA journal_mode=WAL` 在 `_connection()` 的通用前置里，
  早于任何迁移校验；而切 WAL **不是事务性操作**，`rollback()` 撤不回库头 ⇒
  "迁移失败"这条失败路径本身就把库永久改成 WAL（库文件格式字节 `1/1` → `2/2`，此后在只读介质上不可读）。
  修：迁移连接 `switch_journal=False`，**迁移与补写都成功之后**才切 WAL。
  复验（复查者 `probe1.py` 1.3/1.3b，含代码自己的 `migration_failed` 分支）：`jm_before/after` 均为 `delete`、库头仍 `1/1`
- **缺列既不修也不报，直到公开 API 抛裸 `IndexError`**（中）：`_ready()` 只看 `user_version` + 本地节点行，
  版本号已最新的缺列库被判"就绪"，迁移块里的补列逻辑永远够不到；`project_context()` 报误导性
  `internal_error`，`snapshot()` 直接抛 `sqlite3.Row` 缺键的 `IndexError`（yield 之后发生，逃过 except）。
  修：`_ready()` 增加 `REQUIRED_COLUMNS` 校验 + 幂等 `_ensure_required_columns()`（版本号一致也补），
  `_employee` 用 `row.keys()` 容错。复验 `probe1d`：`role` 被补回、`snapshot`/`project_context` 恢复正常
- **墙上分不清"等交付人交证明"与"等同行复验"**（中）：两件要催不同人的事，此前都渲染成 `同行校验=否`。
  修：`wall` 的三类行（等验收/在跑/卡住）都带 `has_proof`/`proof_verdict`/`peer_verified`，
  人读改三态 `证明=无` / `证明=有·同行=否`（已交证明、等同行复验）/ `证明=verified·同行=是`
- **干跑承诺真跑做不到的事**（中）：`onboard(None/"")` 返回 `would_enroll`，`apply=True` 立刻 `invalid_field`；
  `plan(None)` 给出的建议命令是 `enroll claude --project None`（复制粘贴必失败）。
  修：`onboard` 空项目直接 `invalid_field`；`plan` 无项目时 `command=None` + `needs_project=True`
- **打错的 `--mail-root` 静默报成功**（中）：不存在/是普通文件都返回"共 0 封"且 rc=0（`--apply` 也一样），
  操作者据此以为迁移跑过了。修：入口 `is_dir()` 校验 ⇒ `invalid_path`、rc≠0
- **"预算守恒"只在回报字段上成立**（中）：非法字节经 `errors="replace"` 可把 1 字节变 3 字节 ⇒
  `returned_bytes=22` 而 `content` 仍是 62 字节；`test_rv6_3` 只断言回报字段，变异体全绿。
  修：lossy 内容按字符边界真裁到 ≤ 预算；测试改断言 `len(content.encode())`
- 低危：ledger 人读在 `同行=False` 时整段省略 ⇒ 改 `同行=是/否`；`ledger --limit 0` 人读 0 行而 `--json` 1 行 ⇒
  打印端改用已钳位列表；`bridge limit=0` 曾等于"不限量"、负值截尾 ⇒ 显式处理；
  `AGENT_MAIL_BUSY_TIMEOUT_MS` 只覆盖 `_connection()` ⇒ 新增 `raw_connect()` 供迁移备份/updates 快照的
  **活库第二连接**使用；只读库文件的报错曾指向"目录" ⇒ 现在是"文件不可写"；
  `private_mode` 模式已正确时不再 `chmod`（读路径不必改元数据）；
  追加与就地改写现在分别报 `size_changed_while_reading` / `changed_while_reading`，
  **部分窗口取完后必须复核二次哈希**（第八轮证明第七轮此处写反：门控一度让 >budget 的文件跳过复核），
  且**仅在确实切在字符中间时**才前瞻（纯 ASCII 不再恒超发 3 字节）
- **测试加固**：性质测试的检测器现在认得 `sqlite3.connect`（并区分 HTTP 的 `.connect()`）、
  只读 URI（`mode=ro`）、`getattr` 间接调用与 `async def`；两张免检名单改为**反向校验**
  （条目必须存在且真的开写连接，死条目即红）；`offset_ignored` 补反例。
  该加固当场又抓出 `_request`（HTTP connect 误报）与 `_index`（只读 URI 误报）两处**检测器自身**的问题
- 新增 `tests/test_review7_regressions.py`（9 例：迁移失败不改库头 / 缺列自愈 / 坏 mail_root /
  空项目入伙 / 墙三态 / lossy 预算 / ledger limit 口径 / bridge limit=0 / 只读命令不写库）

### 第六轮对抗性复查修复（2 高 / 3 中 / 4 低；复查者崩于报告中途，但报告已落盘）
> 第六轮复查（SHA 冻结 `92382d6`）在写「攻击面 3」时崩溃；因要求**增量落盘**，
> 其结论完整保存在 `/tmp/amr6/report.md`（247 行）——落盘要求救了这一轮。
> 已覆盖并修完攻击面 1–2；**攻击面 3–9（初始化/超时/CLI/`plan("")`/测试有效性/CHANGELOG）
> 本轮未被审查**，需下一轮补。

- **`bridge project` 干跑也拿写锁**（高）：私有助手 `_resolve_assignee` 用写事务做纯 SELECT，
  且它在 `if not apply:` **之前**被调用 ⇒ 干跑路径也会把库 `delete→WAL`。
  修：改 `readonly=True`。复验（复查者 `proof_resolve_assignee`）：`journal after dry-run: delete`、
  `-wal` 侧车文件不存在
- **`read()` 是 TOCTOU，且能被 FIFO 挂死**（高）：原实现「先 `stat`/哈希、后**重新打开路径**取窗口」——
  三次独立打开之间可被换掉文件（返回**从未校验**的字节却自称 `hash_verified=True`），
  或把路径换成具名管道 ⇒ `open()` **永久阻塞**（正是第三轮专门修过的挂死，重写时被请了回来）。
  修：**单 fd** —— `os.open(O_RDONLY|O_NONBLOCK)` + `os.fstat` 复核 `S_ISREG` + 大小闸，
  哈希/取窗口复用同一 fd；取窗口后**再哈希一次**，同 inode 就地改写即拒（`changed_while_reading`）。
  复验：`probe_swap_content`「未击穿」；`probe_toctou`/`probe_toctou2` 均在 10s 内结束（不再挂）
- **`budget` 对非法 UTF-8 不成立**（中）：`errors="replace"` 让 1 个非法字节变 3 字节 ⇒
  22 字节预算返回 62 字节且 `truncated=False`、`refused=None`。修：非法字节走 `lossy` 分支并
  按**原始窗口**计消耗。复验：22 → 22（`超了吗？False`）
- **续读拼不回原文（含死循环）**（中）：`budget=2` 遇 CJK（3 字节/字）时 `consumed=0` ⇒
  `next_offset` 不前进 ⇒ **调用方死循环**（复查者探针即挂在此）。修：
  ①「窗口 + 至多 3 字节前瞻」再对齐，保证解出完整字符；
  ② `consumed` 前进保证；③ 只跳**续字节**（`0b10xxxxxx`）——原先用 try-decode 会把"尾部截断"
  误判成头部问题而白跳真内容（`head` 投影曾因此丢掉首字）。
  复验：budget 2/3/5/7/50 **全部逐字还原**原文
- **性质测试三条绕过口（变异已证明）**（中）：私有函数免检（`_resolve_assignee` 正躲在此）、
  `_connection(readonly=False)` 不被字面量匹配、写标记词（如 `probe_`）被当免检。
  修：性质测试改用 **AST 检测任意参数形态**、**含私有函数**（去下划线再匹配）、
  **只用无歧义写标记**（移除 `resolve`/`probe`/`start`… 等歧义词），
  并新增**检测器自检**（用三条已知绕过做样本，防"测试静默失效"）；
  删除死条目 `plan_update`
- 低危：真·尾部 U+FFFD 不再被静默删除（按字符边界解码，不再 `rstrip`）·
  0 字节产出物如实报 `total_bytes: 0`（与"没读到"区分）·
  `json_keys` 回显 `offset` 却不使用时标注 `offset_ignored`
- 新增 `tests/test_review6_regressions.py`（7 例：干跑不写库 / 续读还原 / 预算守恒 /
  0 字节可区分 / offset_ignored / FIFO 不挂 / 就地改写被拒）

### 第五轮对抗性复查修复（2 高 / 5 中 / 7 低；判定「部分成立」）
> 第五轮复查（SHA 冻结 `fb4d66e`，报告先落盘 `/tmp/amr5/report.md`）指出：
> 我上一轮修的是**自己已知的 12 条**，不是**这一类** —— 另有 30+ 条只读语义 API 与
> 10 条只读 HTTP GET 路由仍开写连接（把库 `delete→WAL`，并发写者持锁时**立刻 busy**）。
> 本批按「类」修完，并把「只读不写库」从**枚举**改成**性质测试**。

- **只读语义 API 全面收敛**（高）：按 AST 判定而非人工列举，把 28 处改走 `_transaction(readonly=True)`
  （`local_node`/`get_task`/`task_detail`/`project_context`/`list_messages`/`peek_claim`/`search_memory`/
  `read_resource`/`resource_manifest`/`knowledge_status`/`query_knowledge`/`validate_*`/`*_credentials`/
  `resources.read`/`execution_manifests`/`project_activity`/`onboarding_status`/`views.*` 等）。
  复验 `probe_read_journal2`：FLIP 从 30+ 降到 **0**（仅剩两处**按设计就是写**的路径，见下）；
  `probe_http_journal`：**10 条 GET 路由全部 `delete -> delete`**，其中 `updates/status` 靠
  `update_channel` 读写分离修好（读分支只读、写分支才写）
- **只读超时与写者隔离**（中）：只读连接 `busy_timeout` 上限 2s（原沿用写路径 10 分钟）；
  写者持 `RESERVED` 锁时只读路径不再"立刻 busy"
- **`_ready()` 不再吞掉真错误**（中）：凡探测期异常（锁/损坏/权限）**原样抛出**，
  不再"回退写路径"（原实现会**先把 `delete` 翻成 WAL 再报错**）；`probe_ready3` 复验
- **构造期错误可自解释**（中）：库文件只读 / 父目录不可写 ⇒ `storage_error` 明确文案
  （原先抛裸 `PermissionError`）
- **候选回退的归属语义不再静默**（中）：`read()` 返回 `is_newest` / `used_candidate_index` /
  `candidate_count` / `skipped`（含每个被跳过副本的原因），"读到旧副本"从此**显式可见**
- **修回 `plan("")`/`onboard("")` 回归**（中）：我上一批给 `enrolled()` 加强校验时误伤了空项目
  （修前可用、修后 `invalid_field`）；现空项目=「只看本机能装什么」，坏项目仍 `not_found`
- **读路径不再整份进内存**（低）：`read()` 改「分块哈希校验 + `seek` 取窗口」，
  60MB 产出物只取 100B 切片时 RSS 增量从 ~60MB 降到 **~2MB**（`probe` 实测）
- **CLI 人读终于能看出同行校验**（低）：`wall` 的等人验收行加 `· 同行校验=是/否`，
  `ledger` 的按任务行加 `· 同行=是`（此前只有 `--json` 能看出）
- **错误分流补洞**（低）：`IntegrityError`（NOT NULL/UNIQUE/FK）→ `constraint_error`；
  `file is not a database`（`DatabaseError`，原先绕过分区）纳入分流；真磁盘写满文案可用
  （marker 由 `disk full` 改 `disk is full`，匹配 SQLite 原文 `database or disk is full`）
- **`_guard` 不再谎报**（低）：stat 失败按真实异常类型报告（链接环 `E62` 不再说成"文件不存在"）
- **移除死代码**（低）：`read()` 里不可达的第二段回退循环已删（重写时一并去掉）
- **性质测试再加一层「反向」检查**：除「只读语义不得开写连接」外，新增
  「凡开写连接者，要么名字像写、要么在 `DECLARED_WRITERS` 显式声明」；
  **它当场又抓出一处**：`escalation_allowed`（升级预算"检查"）竟在拿写锁 ⇒ 已改只读
- **测试从枚举变性质**（中）：新增 `tests/test_readonly_property.py` —— AST 扫描全仓，
  凡「只读语义」函数开写连接即失败；**它当场又抓出三份复审都漏掉的 2 处**
  （`list_mail_tasks` 已改只读；`resolve_permission` 实为写路径，列入**带理由的例外表**）。
  `rv3_7②` 由假断言改为**真构造**（同 ref 的超限副本 + 可读旧副本 ⇒ 必须跳过超限件读旧件）；
  `rv4_3` 的 CLI 断言由「rc=2」加强为「stderr 含 `not_found`」并覆盖空项目

> **按设计就是写路径**（已在性质测试里显式列出并注明理由）：
> `employee_messages`（记已读标记）· `resolve_permission`（落审批决定）·
> `update_channel(channel=…)`（写渠道）· `private_backup`（备份）。

### 第四轮对抗性复查修复（复查者崩了，探针复跑抓出 6 类问题）
> 第四轮复查者在出报告前崩溃（未落结论），但它留在 `/tmp/amr4` 的 22 个探针全部保留。
> **用实时源码逐个复跑**，抓出并在本批修完（新增 `tests/test_review4_regressions.py` 6 例）。

- **一批读路径仍在开写连接**（中）：它们把库从 `delete` 改成 WAL，并可能与写者抢锁 ——
  实测命中 `proof.latest_proof`、`proof.verify_proof`、`artifact.list_refs`、
  `store.governance_events`、`store.snapshot`、`resources.versions`、`mail_sessions.list_sessions`、
  `workspaces.task_delivery`、`contacts.contacts`、`policy.peer_verified`，以及 CLI `artifact list` / `proof`。
  修：`governance_events`/`snapshot`/`versions`/`list_sessions`/`task_delivery`/`peer_verified`
  全部改只读事务；`snapshot` 里重复的 `BEGIN` 移除；`_bound_store` 代理兼容 `readonly` 参数。
  复验 `probe_journal`：**全部 `delete -> delete`**（含 CLI）；`probe_perf`：写者持锁时 `peer_verified ok=True`（原先 busy）
- **结构已就绪时构造 store 仍在写**（中）：`WorkbenchStore.__init__` 每次都走迁移+本地节点写路径 ⇒
  任何只读 CLI 命令都会写库。修：新增只读探测 `_ready()`（`user_version` 一致且本地节点已登记）⇒ 免写
- **候选回退被我上一批的大小闸打断**（中，我引入的回归）：最新副本缺失/超限时直接拒绝，
  即使仍有可读的等价副本。修：遍历候选**跳过不可用者**，用第一个可校验副本
  （复验 `probe_regress`：case A 恢复回退；case F 现在**跳过超限件读可用件**，比修前更好）
- **`enrolled()` / `bridge project` 不校验项目**（低）：坏 project 分别返回 `{}` 与 rc=0；
  现均 `not_found`（bridge 在 0 候选时也校验）
- **`wall` 完全看不出同行校验**（低）：等人验收行现带 `peer_verified`（系统校验 ≠ 同行校验）
- **两处错误分流缺口**（低）：`no such module`（如 fts5 缺失）/`out of memory` 曾报「检查磁盘」⇒
  归 `internal_error`；`AGENT_MAIL_BUSY_TIMEOUT_MS` 极大值曾直接透传 SQLite（等于可挂死）⇒ 钳到 10 分钟上限
- **假测试**（低）：`rv3_7` 只有 1 个候选、从没走回退分支（正好掩盖上述回归）⇒ 改为构造 ≥2 候选；
  `rv3_9` 断言过弱（修前实现也能过）⇒ 改为 monkeypatch `Path.read_bytes` 失败，证明真分块

### 修复
- 工作台桌面壳：默认不弹浏览器标签，但保留「聚焦原生 app 窗口」路径（修 2 个桌面用例）
- `mailbox-mcp`：会话文件无效时报错自解释（是什么/去哪修/一条命令）并返回退出码 2
- `cli.main`：不再吞掉子命令退出码

### 说明
- 版本号按「渠道分离 + 同版本内累积迭代」的发布纪律推进：**alpha 线内累加，不动稳定面**（当前 `0.8.1a1`）
- 测试：ruff 全过 · 全量 pytest 全过

## 0.8.0 — 2026-10-01

- Make existing App/CLI project mailbox MCP the primary connection path: employee/project/session binding, revocable private configuration, ordinary mail and approved shared resources. MCP does not automatically notify or wake an idle agent.
- Default to mailbox tasks with project responsibilities, explicit employee acceptance, saved approved-resource manifests and result submission for separate Human review. Read pinned document revisions with explicit version_id; ordinary unversioned reads remain different.
- Keep managed Codex/Claude CLI execution as a separate option. Mailbox tasks never grant managed execution credentials or automatically apply code.
- Cancel active mailbox tasks on cancellation, leave, pause or retirement; retain existing-session progress across workbench restarts.
- Correct SQLite private-permission handling races and make background-thread failures fail the test gate. The 4201b34 release gate passed six regression jobs and three extracted native-package checks; fixture protocols and native model execution remain separate evidence.
- Provide the matching npm companion plugin target dsh-agent-mailbox@0.8.0 in its separate repository; it is not a workbench installer. Registry publication remains a separate verified action.
- Preserve Beta 4 evidence, incompatible v0.7 data/configuration boundaries and manual compatible-v0.8 upgrades.

## 0.8.0b4 — Beta 4 release target

- Keep the agent-mailbox brand, Python distribution and CLI as the replacement for v0.7; document the breaking configuration/data boundary and verified registry ownership.
- Rewrite bilingual product introduction around Codex/Claude Code project collaboration, with a concrete review example and four-step onboarding.
- Remove reverse DNS from local HTTP and fleet listener startup. Preserve and close the actual long-poll socket during remote shutdown.
- Enforce protected Windows DACLs for workbench secrets and verify real ACLs instead of POSIX mode bits.
- Produce standalone private SQLite upgrade snapshots with no WAL sidecars; preserve v0.8 business data through manual program replacement.
- Provide bilingual native/Python install steps with registry ownership, checksums and explicit v0.7 migration boundaries.
- Gate release builds on six native regression jobs and extracted macOS/Windows/Linux archive HTTP/MCP checks. Tags build artifacts; registry publishing requires an explicit destination.


## 0.8.0b3 — local Beta 3 (2026-10-01, unpublished)

- Explicit project-bound onboarding probes with optional model selection; actual context and marker-note receipts distinguish execution proof from model claims. Viewing checks does not invoke a model. Remote probes and unadapted apps remain unsupported.
- Clean, committed local Git roots receive detached task worktrees; original files are not edited directly. This is not an OS sandbox or native-account isolation. Remote write tasks are refused.
- Freeze text deliveries with integrity checks and separate employee reports/system facts. Human acceptance and explicitly confirmed application are separate; guarded application never commits or pushes. Follow-up tasks inherit the previous patch only at the same baseline.
- Add project-scoped colleague delivery retrieval (`project_delivery`), bringing managed project MCP tools to 11. Reject unsafe, credential-bearing, binary, symlink or oversized patches.
- Schema 9 preserves business identities. Existing update backups cover SQLite and workbench identities/configuration, not retained task workspaces or external Git repositories. Platform/provider verification is recorded separately from prior Beta evidence.

## 0.8.0b2 — local prerelease (2026-10-01)

- Explicit stable/Beta GitHub release checks with numeric version ordering; no automatic polling, downloads or replacement. Public releases may be older than this local Beta.
- Persistent update maintenance pause blocks local/remote new claims while existing tasks finish. Queued work survives; uncertain claims and unconfirmed receipts block update readiness.
- Private, verified SQLite and identity/configuration backup before manual upgrade. Errors keep claims paused; explicit resume remains available. No automatic rollback.
- Installation-specific guidance and authenticated node version/protocol reports; incompatible protocols block new claims while preserving terminal receipts. Legacy unreported nodes remain unknown.
- About/update and device UI supports Chinese/English, mobile, waiting/ready/error states and a persistent pause banner. Schema 8 preserves existing business data.

## 0.8.0b1 — local Beta 1 (not publicly released)

- Separate global employees, current-project navigation and management in the sidebar; add a real project work log and accessible mobile navigation.
- Add immutable resource revisions, explicit human approval and task-run manifests. Default employee reads use pinned versions; explicit remote text proposals cannot overwrite coordinator files.
- Enforce active task identity on remote reads as well as writes. Finished, cancelled or revoked sessions are rejected.
- Verify real Codex-to-Claude mailbox handoff and approved resource retrieval on macOS; verify Mac-to-Ubuntu Docker pinned HTTPS, version consistency, recovery and revocation with a deterministic worker.
- Preserve existing data with private schema migration backups. Optional CodeGraph retrieval and isolated self-contained HTML previews remain supported; Windows CodeGraph is explicitly unavailable in this beta.
- Public GitHub/PyPI release, Windows physical validation, server deployment and automatic updates are separate work.


## 0.8.0a3 — independent foundation (unpublished)

- Remove v0.7 mail runtime, background services, installation scripts and legacy tests from the v0.8 distribution; default CLI and Python module open the workbench.
- Add explicit runtime preparation and optional headless device join/map/employee/run commands; private/CGNAT binding and SSH dial override.
- Persist terminal device receipts and acknowledge exact same-run replay without re-execution or overwriting human review; recover a lost pairing response safely.
- Align runtime/model discovery with the dependency manifest and managed Codex pair; synchronize permission summaries with same-run tool events.
- Preserve task drafts during setup, support named local employees, and separate local runtime readiness from remote assignment.
- Replace active PRD, quick start, security and handover with the independent product baseline; use one shared cross-platform CI test definition.

Earlier alpha and v0.7 notes below are historical, not capabilities of this build.


All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.6] — 2026-09-30

权限基座 + 状态可信 + 投递修复版（A 组 11 条基座 + E/F/PR2/claim-first 五件；四平台同版本发布：PyPI + tag `v0.7.6` + GitHub Release + MCP Registry republish）。

### Added

- **权限基座（A 组，11 条）**: tool-guard 全量 `ToolError` 化（`MailboxError` 统一出口带 `MBE|code` 前缀，mcp SDK 客户端不再只见 "Error executing tool"）；`MAX_BODY_BYTES` 1 MiB 落盘前三段报错（`BODY_TOO_LARGE`，不留半成品/死箱痕迹）；未注册收件人默认硬拒（`unregistered_recipients: reject|warn` 一键降级，warn 模式不再污染 `inbox/<NOSUCH>/`）；agent slug 白名单（`..` 显式拒，任何路径拼接前先过注册表解析）；identity binding 常时校验（`AGENT_MAIL_TOKEN` sha256 恒时比对）；读侧同形防存在性 oracle；成员三源归一化单函数（registry 优先、投递用原始形式）。
- **版本检查 + 点击更新（E）**: `fetch_latest_version()` 打 PyPI 版本级端点取 `info.version`；CLI `agent-mailbox upgrade`（uv tool/pipx/pip 自动探测，`--check` 只查、默认 dry-run 显完整命令+确认、`--yes` 直执行）+ web `/api/version`、`/api/upgrade` 与看板一键入口。安全三硬约束：owner/本机限定（token 门 + 回环对端核验）、执行前显示将运行的完整命令、`<root>/audit.log` 审计留痕（dry_run/start/result 含退出码）。**禁 self-update**——唯一形态是 spawn 外部命令。非强制查询走 `<root>/version_check.json` 24h 限流；断网/坏 JSON 一律 fail-open 静默跳过，绝不影响信件功能。
- **状态可信（F）**: `mailbox_check` 的 `unread` 改为「真待办」口径（仓内 pending 计数，acked 永不计入——修掉"18 封全 acked 仍报 unread=18"），新增 `total`（pending+acked 积压）；web 侧栏「待处理 N」可点下钻（`?folder=pending` 白名单深链）。**禁自动 ack/自动已读**：`mailbox_check` 默认变纯读（`mark=False`），消费正路 = `claim()`（恰好一次）或显式 `mark=True`（旧路径保留）——⚠️ 行为变更：依赖 check 默认消费的外部调用方需改走 claim 或传 `mark=True`。
- **信件 links 字段（PR2）**: `send()` 可选 `links=[{title, uri, kind?, sha256?, note?}]`；校验态 `ok/stale/denied`+拒因在读路径现算（check/list/thread/archived 四处），老信零影响；`file://` 走 allowed-roots 安全模型（config 显式列、不默认放行 `$HOME`、fail-closed、realpath/symlink 归一后前缀匹配、禁 `..` 穿越禁通配）；`http(s)/git` v1 只做 scheme 策略校验零网络请求；sealed 信 links 同权剥除；未知 scheme 发信即结构化拒（A7 同族码）。格式与样例见 `docs/links.md`。
- **claim-first 投递修复**: belt（`scripts/wake-zc.sh`）与 wake daemon（`agent-mailbox wake run`）三路由改「先认领再投」——投递前 `reap_stale_acked`(600s)→`claim()`，只投认领所得的信，认领 0 封不拉会话；投递全失败 `release_claim` 放回 pending（信不丢，带 label 归属校验防误放他人认领）；第二路撞在途信落 `claim_denied` 审计（每信去重一次）；`handled_log.by` 带 `belt:<pid>`/`wake:<runid>` 路由会话标识。多窗重复回信（一信双 done、一 thread 四回信）就此根治；J1–J4 活体判据 17 测入 CI。webhook 消费端（本地网关）对齐原语已备（`wake claim/release` + docstring 对齐点），网关侧接线随部署同步。

## [0.7.5] — 2026-09-26

The trust-model release (PR A brand/CLI + PR B identity & permissions + PR C wizard/mailbox/visibility), merged with the 0.7.4 wake-hardening line (`v07-sampling-wake`) onto `release/v0.7.5`. **Ships together with [0.7.4] below** — 0.7.4 was built, gated and verified locally but never uploaded, so PyPI jumps 0.7.3 → 0.7.5 (legal skip; both entries kept with their own anchors). Tool count: 13 → 14 registered MCP tools (`mailbox_confirm_external` joins).

### Added

- **Identity & permission model (v0.7.5 §3.3)**: members now carry a `kind` (`owner` 人 / `agent` 本机 agent / `guest` 外部来源) on their registry card — legacy cards resolve to the historical behavior (`boss` → owner, everything else → agent). Tool-layer enforcement: `agent`/`guest` callers reading another member's mailbox get a structured `permission denied` (never a silent empty result); owners read all traffic; an unset `AGENT_MAIL_ID` keeps local trust so existing scripts are unchanged.
- **Sealed letters (密封信)**: `mailbox_send(..., sealed=True)` marks a letter whose body is readable **only** through the recipient agent's own tools. Every other reader — owner tools, other agents, the new human-view web endpoints — gets metadata (who/when/status/subject) with `redacted: "sealed"`, so "owner sees all" can never double as a credential leak channel.
- **External-origin gate (外部来源执行门)**: letters may carry `origin` (`local`/`external`). External mail lands normally but triggers nothing — the wake drain skips it (`skipped_external`) and the webhook POST is dropped — until an owner flips it back with the new `mailbox_confirm_external` tool (appends to `handled_log` and the audit log, greppable by name).
- **Human mail view**: `GET /api/messages` (list) and `GET /api/messages/<id>` (detail) on the web board expose every letter under the root to the token holder as the owner — sealed bodies metadata-only.
- **Audit trail**: visibility-relevant decisions append JSONL to `<root>/audit.log` (`member_kind` changes, `confirm_external`) with who/when/what.
- **Pairing-token validation point**: `guest` senders must present a `pairing_token` whose sha256 matches the `pairing_tokens` block of `config.json` (constant-time compare; no block configured → guests cannot send). Pairing/rotation flows remain 0.7.6 scope.
- **Attention tiers (打扰三档 §3.4)**: letters carry `attention` (`decision` 需你拍板 / `report` 报备 / `archive` 存档), default `decision`; reminder presentation lands with the mailbox UI.
- **3-step setup wizard (`/setup`, v0.7.5 §4.1)**: ① discover — the member table (member / kind / wake channels with auditable sources / live status) straight off the discover engine, with the §5⑩ support-list fallback and §5⑨ fingerprint-staleness notes; ② test — a real test letter per member with **result + next step always paired** (a broken member shows ❌ *and* the way out); ③ done — service status, connected count, pending fixes, and the model line ("不自动处理来信（可选）" — the model is an optional feature, never a required question).
- **Three-pane mailbox (`/mail`, §4.2)**: the human mailbox — folders (收件箱/星标/已发送/草稿/已归档), a read-only **monitor** section (agent↔agent traffic, 未送达/失败 via the cached discovery report, sealed letters as metadata), the roster with channel-status dots, and per-letter actions (回复 / 转发 / 派成任务卡 / 归档 / 标未读 / 确认执行外部信) with thread history and sender-channel warnings. Keyboard shortcuts `j/k e r t /`. The §3.6 empty-mailbox call-to-action ("给你的 agent 写第一封信") posts a real letter — no blank pages. 人 vs agent 的界线在界面上讲清：信是往来，任务卡是要人动手的活 (§5⑬).
- **Visibility page (`/visibility`, §4.3)**: the four §3.3 switches with their defaults rendered as-is (`owner_sees_all` on, `agent_cross_read` / `sealed_in_human_view` / `external_auto_execute` off), 发信权限 note, and the audit trail shown inline. Changes persist to `config.json` (`visibility` block), append a `visibility_change` audit line, and are wired for real: `agent_cross_read` opens the tool-layer cross-read, `external_auto_execute` opens the wake gate (fail-closed on any config error); `sealed_in_human_view` is hard-locked off.
- **Owner mailbox APIs**: `GET /api/mail` (decorated letters + roster + counts), `POST /api/mail/send|drafts|drafts/delete`, `POST /api/mail/<id>/read|unread|archive|star|unstar|task|confirm-external`, `POST /api/members`, plus the wizard endpoints `GET /api/status`, `POST /api/discover|test`, `GET /api/setup-summary` — the discover engine calls are injectable so tests stay hermetic. Star/read/draft state lives in `<root>/web_state.json` so the human never edits other members' letters.
- **Wake-attempts anchor + failure alerts (锚A + U2)**: every wake attempt — belt (`scripts/wake-zc.sh`) and daemon (`agent_mailbox.wake`) alike — appends one 8-field JSON row (`ts`/`agent`/`route`/`attempt`/`outcome`/`error_class`/`executor`/`latency_ms`) to `<root>/wake-attempts.jsonl` (0600, append-only, success and failure both written), so wake health is judged from data, not process logs. When every delivery attempt for a letter fails, a `[wake-fail]` alert letter goes to the registered sender plus the boss box (once per letter via a `handled_log` marker; belt alerts dedup through the 24h semantic window) — a broken wake is visible from the mailbox face alone, no shell needed.

## [0.7.4] — 2026-09-26

HS dispatch (0.7.4 repo-write window): close t-37 remnant + same-root-cause sweep + t-38, then gate. **Built and verified locally; upload held pending HS re-review + boss go.** 随 0.7.5 首发合入（merged into `release/v0.7.5` — this entry ships to PyPI as part of the 0.7.5 release, not as a standalone 0.7.4 upload）.

### Fixed
- **`wake install` no longer silently wipes unknown `wake.json` keys (t-37, P0)** — `WakeConfig` round-trips only the keys it knows; the sampling per-agent `agents` policy section (identity / forbidden / `max_concurrent` / `sampling.enabled`) and any future top-level key were being dropped on every `load→save` (install/uninstall paths). Unknown top-level keys are now preserved verbatim. Same root cause at the nested level: unknown keys inside the `webhook` / `jev` sections survive too.
- **`install()` double `cfg.save()`** collapsed to a single write (t-37 remnant).
- **Duplicate letters no longer re-wake (t-38②)** — a `dedupe=False` re-send that lands while a same-`semantic_hash` non-terminal letter is already in the recipient's inbox is delivered and audited, but marked `wake_suppressed_dup` and excluded from the wake face (webhook notification AND sampling). Behavior change: `dedupe=False` same-content re-sends each got their own sampling wake before; now only the first does. Fresh content (different hash) always wakes.
- **Zero-notification sends post no webhook** — when every landed letter is a duplicate, no empty webhook round-trip happens.
- **Sampling wake prompt carries the real pending count (t-38③)** — `unread` in the wake prompt is counted at wake time under the store, not the send-time snapshot (parallel windows under-reported: notification said 1, recipient found 2).

### Added
- **`scripts/verify_release.sh`** (t-35③) — post-release artifact sweep with HS-pinned criteria (sdist content: `/U[s]ers/|inte[r]ia|/h[o]me/` = 0 and machine-specific wrappers = 0; wheel: no `scripts/` at manifest or content level; self-test: the 0.7.2 artifacts must FAIL and 0.7.3 must PASS). §6 of the release checklist wires it in.

## [0.7.3] — 2026-09-26

### Fixed
- **sdist hygiene, round two** — the post-release artifact sweep (every release re-checks the published sdist from now on) caught `scripts/wake-zc.sh`, a machine-specific ops wrapper with hardcoded local paths, shipping in the 0.7.0–0.7.2 sdists (the 0.7.1 sweep only covered the AOCI assets it was written for). Now excluded via hatchling `exclude` (the wheel was never affected — it never carried `scripts/`); the repo copy stays, launchd wiring untouched.

## [0.7.2] — 2026-09-26

Close out every open 0.7 finding (HS review P2 SKILL + P4 items) before any version bump.

### Added
- **Per-agent sampling kill switch (SEP-2577 hardening)** — MCP deprecated the sampling capability on 2026-07-28; a per-agent `"sampling": {"enabled": false}` section in `wake.json` now disables the path outright (no `createMessage` attempts, letters land via the fallback chain). Malformed values fail loudly into a `sampling.log` error audit — the letter still lands, the misconfiguration is visible. Sampling remains an accelerator, never a delivery guarantee.

### Fixed
- **`__version__` tracks the release** — `src/agent_mailbox/__init__.py` had stayed at 0.6.2 through the 0.7.0/0.7.1 releases; both version locations now match (release-checklist §1 "two places, no exceptions").
- **Tool-count consistency** — the six READMEs claimed "17 MCP tools" (counting four built-in MCP primitives `get_prompt`/`list_prompts`/`list_resources`/`read_resource` that `list_tools` never returns); the verified count is 13 registered tools, now stated consistently across README ×6, roadmap, and SKILL.md.
- **SKILL.md brought current** — in-repo `skills/agent-mailbox/SKILL.md` now documents v0.7: sampling wake, the wake-policy schema (`identity` / `forbidden` / `max_concurrent` / `sampling.enabled`), the local-command adapter, `wake run --adapter`, and the wake delivery chain (sampling → wake-daemon / webhook / next check).

## [0.7.1] — 2026-09-26

### Fixed
- **sdist hygiene** — AOCI cognition assets (`.aoci/`, `aoci*.txt`) and local runtime config no longer ship in the sdist (hatchling exclude + `.gitignore`); in-repo `aoci.code.txt` entries normalized to repo-relative paths (was: absolute local paths).

## [0.7.0] — 2026-09-26

### Added
- **Sampling wake (MCP `createMessage` reverse call)** — letters sent through the MCP server now wake the recipient host over its existing stdio connection when the host declares `capabilities.sampling`; per-connection capability negotiation, wake-policy injection (identity / task / forbidden actions / require-receipt, force-injected into every sampling request and audited to `sampling.log`), 60s configurable timeout, per-`msg_id` dedup (in-process + `handled_log` persisted), fail-open to the mailbox when the host is offline or undeclared.
- **Per-agent execution lock** — one in-flight sampling per agent (`max_concurrent`, default 1), FIFO by arrival, released on success/failure/timeout, restart degrades the queued requests to the mailbox fallback (nothing stranded in memory).
- **`local-command` wake adapter** — for on-demand CLI agents (no resident process): wake triggers a configured argv command (e.g. `codex exec`), argv-array only (no shell), letter content passed via `AGENT_MAIL_*` env vars, 300s timeout with process-group kill.
- **`wake run --adapter` CLI override** — multi-agent installs share one `wake.json`; each plist picks its own wake path (`--agent codex --adapter local-command`).
- **AOCI cognition layer** — repository cognition index (baseline / drift / governed entries) established and maintained under AOCI governance.

### Fixed
- **`mailbox_list` inbox-only blindness** — handled letters live in `archive/`; the unified `list_all_messages` view now spans both (HS: five status queries returned 0 against 877 on-disk letters).
- **Cross-agent reply thread inheritance** — `reply_to` resolution fell back to a whole-root scan; previously every cross-agent reply minted a fresh thread (v0.6 F2 regression).
- **Wake drain legacy-letter poisoning** — a stale-format letter (no `id`) raised on the bare `m["id"]` and the fail-open handler skipped every letter after it; the filename stem is now back-filled on read.
- **`ServerSession` anchoring** — the SDK rebuilds the session per request; the sampling registry now anchors the stable `Connection` object so identity binding survives across requests.

## [0.6.2] — 2026-09-23

Security hardening + closing three long-open v0.5.x items. Default behavior is unchanged: every new capability is opt-in or additive until configured.

### Security

- **SECURITY.md**: vulnerability reporting channel (GitHub Security Advisories), supported versions, and the security model stated in the open — local trust by default, `identity_binding` as the opt-in hardening mode.
- **Identity binding (opt-in)**: `identity_binding` in `<mail-root>/config.json` (`{"enabled": true, "<agent_id>": "<sha256(token) hex>"}`) pins agent ids to tokens. Semantics pinned down: **not enabled → allow (local trust remains the default); token mismatch → reject with `identity mismatch`; malformed config → fail loudly at startup**. Bound callers present `AGENT_MAIL_TOKEN` — sha256, compared in constant time (`hmac.compare_digest`, per the web.py precedent). Unbound agents and the disabled default keep today's local-trust behavior; a malformed block fails loudly at server startup, never silently degrading to "disabled".

### Added

- **`unread_count` in webhook payloads**: the recipient's pending-letter count at notification time, top-level on the POSTed event (pure addition — callers that pass no count, like the wake adapters, emit payloads without the field).
- **Claim semantics for `mailbox_wait`** (P1–P4 from the 2026-09-13 forensics): a new locked single-pass `MailStore.claim` returns pending letters already flipped to `acked`, stamped `claimed_by` with a `claimed` handled_log entry, so two waiters on one mailbox can never consume the same batch (the old list-then-check gap let them race and wake empty). Fail-open unchanged: a claimed-but-unhandled letter stays ordinary acked mail for the stale-acked reap round, which now also drops the stale `claimed_by`.

## [0.6.1] — 2026-09-23

Docs-only hotfix: v0.6.0 updated only the English README; every other doc surface was still up to three versions behind.

- **Added** — this `CHANGELOG.md` (Keep a Changelog format, back-filled 0.3.1 → 0.6.0).
- **Fixed** — `README.zh-CN.md` header anchor raised v0.3.0 → v0.6.0, with new Wake daemon and Threads sections, the 13-tool table, and a synced roadmap.
- **Fixed** — `README.es.md` / `README.pt-BR.md` / `README.fr.md` / `README.ru.md` header anchors raised to v0.6.0 (one-line summary + link to the English README for details).
- **Added** — `docs/release-checklist.md`: a per-file version-bump checklist (pyproject / six READMEs / CHANGELOG / SKILL.md) so multilingual docs stop lagging releases.

## [0.6.0] — 2026-09-23

### Added

- **Wake daemon (信必达)**: `agent-mailbox wake install / uninstall / status / run` installs an OS file-watcher (launchd `WatchPaths` on macOS / systemd `PathChanged=` path units on Linux) over `~/.agent-mail/inbox/<agent>/`; every change fires one drain round (`python -m agent_mailbox.wake run --once`). Adapters: `hermes` (POST the gateway webhook; signature style auto-adapts github/generic/slack), `generic-webhook` (your URL + secret), `claude-code` (terminal bell + desktop toast; unknown values fall back to hermes — waking beats silence).
- **Wake reliability trio** (productized from the 2026-09-22 incident review): a failed POST retries 5× at 60s inside one round, then leaves the letter unmarked so the next file-watcher trigger re-drains it — the wake side never drops mail; a `wake` entry in the letter's `handled_log` makes every letter wake **at most once**; count semantics v2 wakes on all `pending` plus `acked` letters older than 600s while freshly-acked mail stays quiet.
- **Fail-open iron law**: the daemon is a separate process that only reads letter files and appends through the store's locked APIs — if wake dies, mail still lands and the next `mailbox_check` still delivers.
- **First-class threads**: every letter carries a `thread_id` (minted on send, inherited on reply); new `mailbox_thread` MCP tool replays a whole conversation oldest-first across every agent (inbox + archive); `mailbox_list` accepts a `thread` filter; legacy letters are grouped by normalized subject with stacked `Re:`/`Fwd:` stripped (a 12-deep chain resolves to one thread, unmatchable singles stay `null`); `mailbox_check` returns a `ghost_warning` past 5 open letters in one thread.
- **Jev routing (optional, default OFF)**: an async scoring router gates what is worth waking the agent for (Noul gate; urgency 0–10; sub-threshold scores batch into a daily digest) without blocking the wake path. Any Jev timeout/error falls back to wake-on-any-mail. Decisions are logged with scores to `<mail-root>/wake-jev.log`; the `api_key` never appears in logs. Pure stdlib behind the `jev` optional extra (reserved for a native client later).
- Server: `agent-mailbox wake` subcommand dispatch (`--home` passthrough), `mailbox_thread` tool (12 → 13 tools), `mailbox_list` `thread` parameter, `mailbox_check` ghost reminder. PyPI package renamed `agent-mailbox-mcp` → `agent-mailbox` with a Trusted Publisher workflow.

## [0.5.0] — 2026-09-22

### Added

- **Duplicate suppression (delivery-side)**: every letter stores a `semantic_hash` — sha256 over normalized subject + prose + code-fence regions hashed raw in original order. `mailbox_send` / `mailbox_broadcast` dedupe by default: a same-hash non-terminal (`pending`/`acked`) letter in the target inbox within the 24h window blocks the send with zero side effects, and the caller sees `{"deduped": true, "existing_id"}`; `count` only counts letters that actually landed; `dedupe: false` exempts; replies are exempt by design; scope is inbox-only, never archives; legacy letters without the field never match.
- **Two-phase `handled_log`**: `MailStore.record_handled(agent_id, msg_id, action)` records `intent` / `outcome`; the store is the single writer (mail-root flock), keeping `handled_log` append-only and auditable across sessions.
- **Compensation table**: `MailStore.resume_plan(agent_id, msg_id)` reads only the log and returns `process` / `replay` / `finalize` / `skip` — a half-done claim is recoverable without guessing.
- **Stale-`acked` reclaim**: `python -m agent_mailbox.reap --agent ID --ttl 7200` flips `acked` mail older than the TTL back to `pending` with a `reclaimed` entry (acked → reclaimed → done stays auditable end to end); letters whose newest `intent` is fresher than 30 minutes are deferred; manual maintenance only.
- **Wake circuit breaker**: N consecutive no-progress drain rounds latch a breaker file (auto-expires after 6h) and stop launching turns — backoff stretches the interval, the breaker stops the bleeding.

### Changed

- Wake wiring (`scripts/wake-zc.sh`) reaps **before** counting pending on every loop iteration (flock-idempotent, fail-open with an explicit log line).

### Hard rules

- 铁1: `reap_ttl` (default 3600s) must stay strictly below `dedup_ttl` (default 86400s); violating configs fail loudly (`MailboxError`) at load time.
- 铁2: deduped perception contract — `deduped` / `existing_id` in the per-recipient result, `count` counts only real landings, deduped sends append nothing anywhere.
- 铁3: periodic letters whose bodies embed timestamps naturally hash differently — dedup does **not** stop them (by design; replay protection relies on the B compensation flow plus `dedupe: false`).

## [0.4.0] — 2026-09-13

### Added

- Configurable webhook signature style via `AGENT_MAIL_SIGNATURE_STYLE`: `github` (default, `X-Hub-Signature-256: sha256=<hex>`) / `generic` (`X-Webhook-Signature: <hex>`, bare hex) / `slack` (`X-Slack-Signature: v0=<hex>`; emits no `ts` field, so receivers must not verify against the full Slack base string).
- Self-echo protection: notifications whose sender equals the notify target are dropped by default (the letter still lands); the drop is audited as `echo_suppressed: true` in `sent.log`; `notify_self_echo` in `<mail-root>/config.json` or env `AGENT_MAIL_NOTIFY_SELF_ECHO` restores delivery with an `[echo] ` subject prefix on the notification (the stored letter keeps its original subject).
- `python -m agent_mailbox.cleanup --dry-run` maintenance command: scans the mail root for suspected test residue (unregistered `inbox/`/`archive/` directories, `NEWBIE`/`WBTEST`-style test-named directories, orphan letters, `*.tmp` leftovers), lists path + size + reason without deleting; `--yes` deletes for real but requires typing `yes` to confirm; registered-but-test-named directories are review-only.

## [0.3.1] — 2026-09-13

### Fixed

- Reply subjects no longer pile up `Re: Re:` — first replies, re-replies, and mixed-case prefixes all normalize to a single `Re:` (reply path only).
- Web board tokens use constant-time comparison (`hmac.compare_digest`, both sites) and persist across reboots (`~/.agent-mail/web_token`, mode 0600; `AGENT_MAIL_WEB_TOKEN` env always wins).
- `sent.log` auto-rotates one generation past 10 MB (`os.replace` → `sent.log.1`).

[Unreleased]: https://github.com/polaris-smart/agent-mailbox/compare/v0.7.6...HEAD
[0.7.6]: https://github.com/polaris-smart/agent-mailbox/compare/v0.7.5...v0.7.6
[0.7.4]: https://github.com/polaris-smart/agent-mailbox/compare/v0.7.3...v0.7.4
[0.7.3]: https://github.com/polaris-smart/agent-mailbox/compare/v0.7.2...v0.7.3
[0.7.2]: https://github.com/polaris-smart/agent-mailbox/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/polaris-smart/agent-mailbox/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/polaris-smart/agent-mailbox/compare/v0.6.2...v0.7.0
[0.6.2]: https://github.com/polaris-smart/agent-mailbox/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/polaris-smart/agent-mailbox/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/polaris-smart/agent-mailbox/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/polaris-smart/agent-mailbox/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/polaris-smart/agent-mailbox/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/polaris-smart/agent-mailbox/compare/v0.3.0...v0.3.1
