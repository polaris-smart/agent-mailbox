# Codex 接手包 · agent-mailbox 0.7.6 收口后开发

> 生成：2026-09-30 · 交接方：ZC（栈主权）· 接手方：Codex（最快模型档）
> 本文件 = 唯一入口。读完这一篇 + 跑通 §6 验收命令，你就可以开工。
> 判定人永远是 HS（复验）与老板（发布/推支/舰队切换）——**不看任何人的自报**。

---

## 1 · 这是什么项目

**agent-mailbox** = 给 AI agent 用的 MCP 信箱服务器（Python 3.10+ / hatchling / src 布局，
依赖仅 `mcp>=2.1`，dev 面 pytest+ruff）。agent 之间互发信件、任务卡、看板/三栏邮箱 web 面、
唤醒链（launchd WatchPaths → claim 认领 → 投递给宿主 CLI）。

- 已发布：**PyPI 0.7.5**（tag v0.7.5 / Release Latest / MCP Registry 四处一致）
- 待发布：**0.7.6**（本地栈 22 commit，见 §3——**未 push/未 tag/未发版**，全候发版门）
- 产品定位（老板口径）：多身份同机 = 唯一价值场景。装完配好、所有 agent 零折腾直接能用。

## 2 · 权限面与人员（越权=事故）

| 角色 | 权限 | 备注 |
|---|---|---|
| 老板 | 发布门 / 推支 / launchd 舰队切换 / 参数化裁决 | 一个字 = 执行 |
| HS | 派工唯一通道（mailbox）/ 亲跑复验 / 发版执行 | 无壳信箱窗：只信命令输出原文 |
| ZC（交接方） | 0.7.6 栈主权 | 栈在本机 worktree，未上远端 |
| WB | 主权仓 ~/.workbuddy——**不碰、不派、不打断**（正带 codex 开发） | 4 个 codebuddy 孤儿进程是它的，只观察不杀 |
| **Codex（你）** | 工程窗：独立 worktree 内施工+commit | 禁 push/tag/发版/碰他人主权仓 |

**铁律（违反即事故）**：
1. 不新开版本号（一切落 0.7.6；发版 = HS 亲跑复验过才动）
2. 不 push / 不打 tag / 不上传 PyPI / 不 republish MCP Registry
3. 不动 `release/v0.7.5`、不改 tag 指向、不改已发布产物
4. 一单元一 commit，可独立回退，禁混提交
5. 遇意外停手报告，不自行降级、不自想办法
6. 派工/回执走 mailbox，信里只放指针（任务书路径），不塞全文

## 3 · 栈地图（22 commit，b387513 → HEAD）

基线 b387513 = main tip = 0.7.5 已发内容（tag v0.7.5=4408f19，差异两笔的书面判读见 Release v0.7.5 页「版本判读」节）。

```
0e8b50a/5708812/70a0233/6d99684  A组·11条权限基座（HS 已亲验通过）
ab6c7ae                          0.7.6 定版（CHANGELOG/双写版本号）
09880a4  t-53  E 版本检查+upgrade 点击更新
3c8fd3b  t-54  F unread=真待办 + web下钻（行为变更：check 默认纯读，消费走 claim）
b855b2b  t-55  PR2 links 字段 + file:// allowed-roots 安全模型
87a68dc  t-56  claim-first 投递修复（多窗重复回信根治）+ J1-J4 活体判据
24868f7  t-57  CHANGELOG [0.7.6] 五件条目
78bad78  t-58  A-1 per-agent 唤醒路由（agents.<ID> 三键）
18c5075  t-59  A-2 失败必响 + doctor 六检
e48c34f  t-60  belt provider 解析落仓（resolve-provider-config.sh）
5bb1346  t-61  判据1/6 一条命令装完（installer.py 入口档/plist/belt 生成物化）
60ffd15  t-62  判据7/S4 LLM 可选（digest.py 纯本地降级流转）
ac85a0b  t-63  S1-S5 干净HOME端到端场景测试
d260d58  t-64  收口批1诊断面（doctor 装/加载分行+breaker+sha校验）
90e764d  t-65  收口批2运行面（watchdog.py 进程组+alerts.py 回读确认）
6aecf6f/5b7aee2/7ed316d  t-66/67/68 收口批3用户面（入口档+配置页+人话报错+验收脚本）
```

## 4 · 工作方式（「ZC 方式」四条，已验证可复制）

1. **任务书唯一口径**：判据写死+验收脚本先交（允许先红——红了逐项修，绿了交复验）+
   回执必须「条目 → 命令输出原文 → 判定」，禁「通过/全绿」空话
2. **独立 worktree 车间**：`git worktree add ../agent-mailbox-wtX -b feat/v07xx-x <基点>`，
   一单元一 commit，文件面互斥可并行，完工后 ff/cherry-pick 回主分支
3. **验收不信自报**：判定面=可复跑命令的输出原文；已有验收脚本
   `scripts/acceptance-user-path.sh`（一行复跑，自造干净环境，23/23 判定项）
4. **每批一次回执**（不等全部做完）；派工/回执走 mailbox（正式通道）

## 5 · 已知坑（本栈实战攒的，前人踩过你别再踩）

- **解释器**：主仓 `.venv` editable 指向 main 分支 src——测 worktree 代码必须
  `uv sync --extra dev` 自建（uv.lock 是本地产物，**不 commit**，收工删）
- **/usr/bin/python3 = CLT 3.9**：requires-python>=3.10 的包装不上；用 .venv
- **jsonl 格式两态**：`"k":"v"` 与 `"k": "v"` 都存在——grep 断言一律格式无关（只搜值）
- **launchd 磁盘态≠运行态**：plist 改了不 bootout/bootstrap 就不生效；反过来
  重启/重载=按磁盘态静默换码。动服务前先对两态取证（`launchctl print gui/$(id -u)/<label>`）
- **部署副本=生成物**：`~/.agent-mail/wake-zc.sh`、`resolve-provider-config.sh` 是
  `scripts/` 的同步目标——改仓内源头，部署侧用 tmp+rename **原子写**（非原子写曾与
  launchd 轮询竞态出过语法错）
- **在役身份**：HS（hermes→localhost:8644 网关）/ WB（主权在 WB）/ ZC（belt 轮询）
  ——真机三身份的 plist 是老板权限面，工程窗只读探测
- **flake**：`test_sampling_stdio_e2e_wire` 真子进程时敏用例偶发超时（与业务面零共享，
  单跑/复跑即绿）

## 6 · 验收命令表（每批回执必附原文）

```bash
uvx ruff@0.16.6 check src tests          # 期望 All checks passed!（exit 0）
uvx ruff@0.16.6 format --check src tests # 期望 N files already formatted
.venv/bin/pytest                          # 全仓不带目录参数；当前基线 557 passed, 1 skipped
bash scripts/acceptance-user-path.sh      # 用户路径验收 23/23（自造干净环境，可重复）
python3 -m agent_mailbox.cli doctor       # 诊断十检（真机只读探测）
```

## 7 · 七件套索引（上下文重建入口）

| 件 | 位置 |
|---|---|
| AOCI-Code | 仓内 `aoci.txt` / `aoci.code.txt` / `aoci.meta.txt` |
| archify 图册 | 仓内 `docs/`（architecture-en.html/png、diagrams/、board-* 截图） |
| graft 上下文图 | `docs/context-graph/`（git-ignored 本地缓存；你自行 `graft build --dir docs/context-graph` 重建，1202 节点/3429 边） |
| codegraph | `.codegraph/codegraph.db`（`codegraph index .` 重建；查询用 `codegraph query`） |
| PRD/roadmap | `~/wiki/YueXue-Zone/任务书/2026-09-29-agent-mailbox-roadmap-0.7.5起.md` |
| 任务书（唯一口径） | `~/wiki/YueXue-Zone/任务书/2026-09-29-任务书-ZC-mailbox-0.7.6收口批-5缺口.md`（**v2.2+ 滚动更新，开工前重读 §变更日志**）+ `2026-09-29-agent-mailbox-版本收敛B+两缺口并入0.7.6A-任务书.md` |
| todo-list | 信箱任务卡看板（web board）+ 上述任务书 |
| daily-update | `~/wiki/YueXue-Zone/工作日志/2026-09-3*.md` |

## 8 · 当前在飞 / 待办（按优先级）

1. **发版门真机五条**（发 v0.7.6 的最后一关）：本机三身份（HS/ZC/WB）各配 app+cli
   双入口 → 每条真发测试信自动处理 done 增长 → 负例改错配置家 doctor 点名+人话 →
   改回复绿 → 全程 0 手写脚本/0 env/0 手改文件 → 回执给 HS 亲跑复验 → 过了才发四平台。
   ⚠️ 前置=launchd 舰队切换（命令组在案，**老板权限面，候一字**）。
2. **0.7.7 UI 线批一**：线程视图+归并+搜索（HS 任务书随批另发）
3. **C 级单值守窗**（治多窗同身份各说各话，v0.8 联邦车）：设计稿
   `~/wiki/YueXue-Zone/任务书/2026-09-29-单值守窗设计稿-防多窗重复回信.md`
4. **挂账**：政治课件泄题隔离判定 / HS 两条产品优化（滞后投递/dedup 盲区）入 0.7.7 候选
5. **J1-J4 立正式门**（老板待拍）

## 9 · 首单建议（已与老板对齐方向）

从 **0.7.7 UI 线批一** 或 **C 级单值守窗** 起手——判据清晰、有任务书骨架、不碰发版门。
第一单交付物照 §4：任务书回执（先交验收判据）→ 独立 worktree 施工 → 命令输出原文回执。

## 10 · 环境速查

- 仓：本机 `~/tools/agent-mailbox`（main=0.7.5）+ worktree `~/tools/agent-mailbox-wt76`
  （feat/v076-a7 = 0.7.6 栈，**本包所在**）
- 邮箱根：`~/.agent-mail`（真机在役——测试一律 tmp HOME，别碰真箱）
- CLI：`python3 -m agent_mailbox.cli <sub>`（setup/discover/status/doctor/digest/wake…）
- 模型面：火山方舟通道；Codex 档=老板配置的最快模型
