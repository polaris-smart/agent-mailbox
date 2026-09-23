# agent-mailbox v0.2 预研 · Git-Native Memory 三件套

> ZC · 2026-09-20 · Task 5 交付 · 方案 + 可运行原型（已实测闭环）
> 对标：teamai-cli (4,795⭐) / okf-agent-memory (704⭐) / RSIAgent 论文路线

## 一、结论先行

**采用「自研三件套 + 借鉴 okf 架构」混合路线**，不直接采用 okf-agent-memory 做 Memory 层。决定性判据来自实弹测试：

| 判据 | okf-agent-memory 实测 | 我们的原型实测 |
|---|---|---|
| 中文词中子串检索 | ❌ "符号链接"可命中，但 **"链接"/"回滚"/"穿透" 全部零命中**（tokenizer 把连续汉字整段当一个 token）| ✅ bigram 分词，"链接"/"静默" 全命中 |
| 中文场景适配 | 英文 BM25 假设（Go `unicode.IsLetter` 整段切）| ✅ 原生 bigram（英文词 + 中文 2-gram）|
| 依赖 | Go 1.26 单二进制（9.8MB）| Python 标准库零依赖 |
| MCP 工具面 | 6 个（search/show/validate/create/update/relate）| 待接（v0.2 主任务）|
| 中文文档支持 | 弱（无 CJK 分词文档）| 场景即中文 |

okf 值得**借鉴**的三点：① DMAA 双记忆分层（Push ~150 token 行为准则 + Pull 零基线按需检索）；② Search-Before-Write 纪律；③ OKF v0.2 格式（Google 标准，provenance/trust tier 字段设计）。

## 二、三件套设计

### 1. learnings/ — Git 仓库式经验条目

```
~/.agent-mail/learnings/
├── entries/
│   └── 20260920-put-grants.md      # <日期>-<slug>.md
├── votes/
│   └── ZC.yaml                      # 每 agent 一个投票文件
└── index.json                       # BM25 索引（可重建）
```

条目格式（markdown + frontmatter，Git 友好、可 diff）：
```markdown
---
title: PUT grants 外层字段被静默忽略
agent: ZC
date: 2026-09-20
signal: PUT grants 外层字段被静默忽略
solution: 必须写 inner grant 对象，写完读回验证
tags: dt,api
---
## 摩擦信号
...
## 解决方案
...
```

### 2. recall — BM25 + 中文 bigram 分词

**核心创新点**（vs okf）：中文按 **overlapping bigram** 索引——"符号链接" → `符号/号链/链接`，因此任意词中子串都能命中（实测"链接"✅）。无需 jieba 等重依赖。

评分：标准 BM25（k1=1.5, b=0.75），再乘投票权重。

### 3. 投票飞轮 — votes/<agent>.yaml

```yaml
# votes/HS.yaml
20260920-dt-dev-main: +1
```
排序公式：`final_score = bm25_score × max(0.1, 1 + 0.1 × net_votes)`
（净 2 票 → ×1.2，实测 0.7952 → 0.9542）

## 三、实测验收记录（可复现）

```
① 三 agent 写入（ZC/HS/dsh-agent-01 各一条）        ✅
② 中文词中子串检索 "链接" → 命中 skill-hub 条目      ✅（okf 做不到）
③ 投票前基线：dt-dev-main 0.7952 / put-grants 0.7952 ✅
④ HS + ZC 两票投给 dt-dev-main                       ✅
⑤ 投票后：dt-dev-main 0.9542（+20%）/ put-grants 不变 ✅
⑥ 重跑复现：结果完全一致                              ✅
```

复现命令：
```bash
cd ~/tools/agent-mailbox/v02-proposals/git-native-memory
export LEARNINGS_ROOT=/tmp/learnings-test && rm -rf $LEARNINGS_ROOT
python3 learn.py add --agent ZC --signal "..." --solution "..."
python3 learn.py recall "链接"
python3 learn.py vote <entry> --agent HS --up
```

## 四、与现有 inbox 的关系

| 维度 | inbox（信件）| learnings（经验）|
|---|---|---|
| 生命周期 | 处理完即归档 | 长期沉淀、持续投票 |
| 寻址 | 按收件人 | 按语义检索 |
| 位置 | `~/.agent-mail/inbox/` | `~/.agent-mail/learnings/`（**共用根，独立子目录**）|
| 交集 | 信件里的"教训"可由 agent 显式转存为 learning（人工触发，不自动）| — |

**不自动转存**的原因：信件含大量过程性内容，自动转存会污染经验库（违反 Search-Before-Write 的"质量优先"精神）。

## 五、v0.2 落地路径（建议）

| 阶段 | 内容 | 预估 |
|---|---|---|
| P1 | 三件套并入 agent-mailbox 主干（`learn` 子命令 + 2 个 MCP 工具 `mailbox_learn` / `mailbox_recall`）| 0.5 天 |
| P2 | 接 okf 借鉴项：Search-Before-Write 检查（写入前 recall 查重）| 0.5 天 |
| P3 | 接 WorkBuddy 连接器（v0.2 发布时同步更新 Task 2 的 connector 包）| 0.5 天 |

## 六、学术印证（HS 提供的 CausalWM/RSIAgent 材料）

RSIAgent（OSWorld 2.0 超 GPT-6 Astra）证明：**不更新模型参数、纯靠外部 Memory 沉淀即可产生跨任务能力提升**。我们的 learnings/recall/投票正是这条路线的工程化实现。论文：openreview.net/pdf?id=3pf4d0EEqm（写正式方案时引用）。

另：CausalWM 的 stage-ordered attention mask（时序隔离）与本项目无直接关系，但已记档为通用设计模式（滞后感知/延迟决策场景必须在数据流层做时序隔离）。

## 七、文件清单

- `learn.py` — 可运行原型（纯标准库，~230 行）
- 本方案文档
