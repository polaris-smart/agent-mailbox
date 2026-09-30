# agent-mailbox 产品需求文档（PRD）

> 版本：v1.0（2026-09-30）· 维护人：交接后归 Codex · 判定人：HS / 老板
> 维护规则见 §9。本文件是产品意图的唯一正典；**CHANGELOG.md 是已交付事实的唯一
> 事实源**——PRD 写「要什么/为什么」，CHANGELOG 写「发了什么」，两者不互相重复。

---

## 1 · 产品定位

**agent-mailbox** = 给 AI agent 用的信箱服务器（MCP）。

一句话（老板口径）：**多身份同机是唯一价值场景**——一台机器上跑着 N 个 agent
（Claude Code、Codex、WorkBuddy、ZCode…），它们互相派活、报备、回执，需要
一个不丢信、不误投、坏了看得见、失败不静默的**邮政系统**。

三条产品铁律（全部来自真实事故，不是设想）：
1. **装完配好零障碍**：用户只配一次模型面（provider/model/key）；agent 侧零配置，
   名字·入口·配置家·模型·唤醒单元全由产品探测/推导（G-7）
2. **各回各家**：每个身份的信只叫醒它自己；装第二第三个不覆盖第一个（G-6 per-agent 路由）
3. **不许数字骗人、不许失败静默**：unread=真待办；rc=0 伪装成功是最大敌；
   告警必须回读确认送达（G-5）

## 2 · 用户与场景

**用户**：在自己 Mac/服务器上同时运行多个 AI agent CLI/app 的个人开发者与
多 agent 工作室（含我们自己的四条生产线 HS/ZC/WB/codex——**吃自己的狗粮**）。

**核心用户路径**（= 验收场景，`scripts/acceptance-user-path.sh` 23 判定项可复跑）：
- S1 单身份：装 → 发测试信 → 被叫醒 → 处理 → done 归零
- S2 三身份同机：互不覆盖、各自只被自己的信叫醒（触发日志+pending 下降双证）
- S3 故障注入：错路径/未登录/不在名单 → doctor 逐条点名+人话修法+非零退出
- S4 LLM 缺席：机器上没有任何 CLI 登录态，信件仍流转（digest 纯本地：读信→摘要→
  标 done→建议回复清单），不卡看门狗、不烧额度
- S5 doctor 每个故障给**可直接粘贴执行**的修复命令

## 3 · 能力矩阵（0.7.6 现状）

| 域 | 能力 | 落点 |
|---|---|---|
| 信件 | send/check/list/thread/reply/done、links 附件指针（file:// allowed-roots 安全模型）、sealed 密封信、attention 三档、MAX_BODY_BYTES | store/server、docs/links.md |
| 权限 | owner/agent/guest 三身份、identity binding（token 恒时比对）、四 visibility 开关、pairing token、外部来源执行门 | server/store、/visibility 页 |
| 唤醒 | claim-first 认领投递（多窗不重复）、belt/daemon/webhook 三路由、per-agent 入口档（app/cli·配置家·模型·看门狗）、LLM 缺席 digest 降级 | wake.py/installer.py/watchdog.py |
| 运维 | doctor 十检（根可读/装/加载/最近唤醒/路由/积压/认证/断路器/脚本一致/告警可达+入口体检）、失败必响非零退出、告警回读确认、upgrade 点击更新（禁 self-update） | cli.py/alerts.py/version_check.py |
| 人面 | web 三栏邮箱、任务卡看板、setup 三步向导、可见性页、配置页成员卡（入口档可看可改） | web.py/webpages.py |
| 错误面 | MailboxError 带 MBE 码、slug 白名单、1MiB 上限、错误信息人话化（不得裸甩 CLI 内部错误） | server/store/wake |

## 4 · 版本线

| 版本 | 主题 | 状态 |
|---|---|---|
| 0.7.5 | 信任模型（身份/权限/密封信/三栏邮箱/向导） | **已发布**（PyPI/tag/Release/Registry 四处一致） |
| 0.7.6 | 权限基座+状态可信+投递修复+产品化（装完即用/LLM 可选） | **栈就绪**（feat/v076-a7，23 commit），候发版门真机五条 + HS 亲跑复验 |
| 0.7.7 | UI 线批一（线程视图+归并+搜索）；挂账：滞后投递/dedup 盲区两项优化、J1-J4 立正式门 | 规划（首单候选） |
| v0.8 | C 级单值守窗（防多窗同身份）+ 跨设备联邦 + claim-first 重投环披露闭环 | 设计稿就绪（单值守窗设计稿.md） |

**发版门（老板拍，不可协商）**：本机所有 agent 配置面+app/cli 双入口各一条 → 真发测试信
自动处理 done 增长 → 负例改错配置家 doctor 点名+人话 → 改回复绿 → 全程 0 手写脚本/
0 env/0 手改文件 → 回执给 HS → **HS 亲跑复验过才发**（PyPI+tag+GitHub Release+MCP
Registry 四平台同版本）。

## 5 · 非目标（明确不做）

- ❌ self-update（静默自替换代码）——upgrade 只 spawn 外部命令+三重安全约束
- ❌ 常驻轮询/后台乱联网——显式命令触发或 ≥24h 限流静默查
- ❌ 自动 ack/自动已读——消费正路 = claim 恰好一次
- ❌ 跳版本/新增版本号——一切落已存在的号上
- ❌ 把「能装能跑」当验收——一切以可复跑命令的输出原文为准

## 6 · 架构一页纸

```
MCP 客户端(agent) ──stdio/HTTP── server.py(14 工具+guard) ── store.py(信箱/锁/审计)
                                      │
web 人面(web.py/webpages.py) ──token── ┤
                                      ├─ wake(认领/投递) ── 入口档 ── 宿主 CLI/app
belt(scripts/) ──轮询/认领────────────┘        │
                                    watchdog.py(进程组)  alerts.py(回读确认)
digest.py(LLM 缺席降级)    version_check.py(PyPI)    doctor(cli.py 十检)
```

上下文重建：`graft build --dir docs/context-graph`（1202 节点）+ `codegraph index .`；
架构图册 `docs/`（architecture-en/png、diagrams/）。

## 7 · 验收哲学（产品的一部分，不是流程负担）

本项目用血换来的判定纪律，**永久生效**：
1. 验收脚本先交、允许先红——实现对不上就 FAIL，不许写成永远绿
2. 回执 = 条目 → 命令输出原文 → 判定；禁「通过/全绿」空话
3. 判据不得读被测对象自身可改写的量（pending 会被离线分支清零的教训）
4. 现场真故障必须实机复现（codex spawn / WB auth 两条的教训），不许只跑单测
5. 报备区分「本机面/外部面」；版本锚点来自 git 与外部一手源，不来自记忆

## 8 · 质量门（每批回执必附原文）

```bash
uvx ruff@0.16.6 check src tests          # exit 0
uvx ruff@0.16.6 format --check src tests # exit 0
.venv/bin/pytest                          # 全仓；当前基线 557 passed, 1 skipped
bash scripts/acceptance-user-path.sh      # 用户路径 23/23
python3 -m agent_mailbox.cli doctor       # 十检
```

已知 flake：`test_sampling_stdio_e2e_wire`（stdio 子进程时敏，单跑即绿，与业务面无关）。

## 9 · 本文档的维护规则（交接给 Codex）

1. **触发时机**：每个版本发版后、或产品定位/非目标发生变化时更新；日常小功能不触动
2. **变更流程**：改 PRD ⇒ 在 mailbox 给 HS 回执说明改了哪节+为什么 ⇒ HS 无异议即生效
3. **事实边界**：能力矩阵只写「已合入主分支」的；规划只写「老板/HS 拍过」的——
   自己的想法进任务书提案，不进 PRD
4. **备份纪律**：PRD 随仓走（commit 即备份）+ wiki 镜像 + git bundle 定期全量
   （现势：`~/wiki/YueXue-Zone/backup/agent-mailbox-0.7.6-stack-0930.bundle`）

## 10 · 附：关键文档索引

- 接手包（工作方式/权限面/坑清单）：`docs/HANDOVER-CODEX.md`
- 唯一口径任务书（收口批 7 缺口，滚动更新）：`~/wiki/YueXue-Zone/任务书/2026-09-29-任务书-ZC-mailbox-0.7.6收口批-5缺口.md`
- 收口过程版本线：`CHANGELOG.md`（[0.7.6] 段）
- roadmap：`~/wiki/YueXue-Zone/任务书/2026-09-29-agent-mailbox-roadmap-0.7.5起.md`
