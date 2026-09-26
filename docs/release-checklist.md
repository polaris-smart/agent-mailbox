# 发版 checklist（release checklist）

> 目的：根治「每次发版只更英文 README、多语言/CHANGELOG/SKILL.md 全部掉队」——v0.6.0 实测漏项，v0.6.1 被迫纯文档热修补齐。
> **0.7.x 追加铁律（老板 09-26 定）**：一个版本就该解决一个版本的问题——检查发生在**发布之前**，不在发布之后。0.7.1（aoci 泄漏）、0.7.3（wake-zc.sh 泄漏）两次追发是同一病根（产物检查放在发布后）复发两次，本节 §5.5 前移后此模式关闭。
> 用法：任何版本号变更，按下表逐项打勾后再 commit + tag。英文 README.md 是唯一全量真源，其余语言跟随它。

## 0 · 发版前

- [ ] pytest 全量绿（基线以当版测试数为准）
- [ ] `ruff check src tests` 绿
- [ ] 功能改动已写进英文 `README.md`（正文 + roadmap 条目 + 工具表）

## 1 · 版本号变更（两处，缺一不可）

- [ ] `pyproject.toml` → `version = "x.y.z"`
- [ ] `src/agent_mailbox/__init__.py` → `__version__ = "x.y.z"`

## 2 · 六份 README（逐份打勾）

| # | 文件 | 必改项 |
|---|------|--------|
| 1 | `README.md` | 头部 🆕 锚点行 → 新版本一句话概述；新功能章节；工具表加新工具行；`Upgrading` 段工具数；roadmap 顶部新版本条目 |
| 2 | `README.zh-CN.md` | 头部 🆕 锚点行翻译英文新版本段；新功能章节（对照英文翻译）；工具表同步；升级段工具数；roadmap 同步 |
| 3 | `README.es.md` | 头部 🆕 锚点行 → 新版本一句话概述 + 指向英文 README 的详情链接 |
| 4 | `README.pt-BR.md` | 同上 |
| 5 | `README.fr.md` | 同上 |
| 6 | `README.ru.md` | 同上 |

- [ ] 六份锚点一致：`grep -n '🆕 \*\*v' README*.md` 输出同一版本号
- [ ] 工具数三处一致：server 实际工具数 == 六份 README == `SKILL.md`

## 3 · CHANGELOG

- [ ] `CHANGELOG.md` 新增 `[x.y.z] — YYYY-MM-DD` 段（Keep a Changelog 格式）
- [ ] 文末 compare 链接补 `[x.y.z]: .../compare/v上一版...vx.y.z`，并把 `[Unreleased]` 指向新 tag

## 4 · SKILL.md（`skills/agent-mailbox/SKILL.md`，skills.sh 分发面）

- [ ] `## Tools (N)` 计数 == server 实际工具数；新增工具有表行
- [ ] 安装段保持现行 pip/pipx 路径（无超前 / 过期引用）
- [ ] 新能力（如 wake daemon / threads / jev）在 Setup 或 Ops notes 有最小说明

## 5 · 发版

- [ ] commit：精确路径 `git add <file>...`（禁 `git add -A`）
- [ ] **本地实包抽查（§5.5，tag 前必过，见下）**
- [ ] push main → push `vx.y.z` tag（Actions 自动发 PyPI，**勿手动上传**）
- [ ] 回报：commit SHA + Actions run 链接 + 六 README 锚点 grep 输出 + CHANGELOG 段标题

## 5.5 · 本地实包抽查（发布前，一次做穷尽）

> 0.7.1/0.7.3 两次泄漏追发的根治步骤：**在 build 出真实产物上扫，不是信配置**。

- [ ] `python -m build` 出 sdist **和 wheel** 两个产物
- [ ] 两个产物各自解包，逐项扫：
  - `grep -r inte[r]ia`（本机用户名/绝对路径）→ 零命中
  - 密钥/token 样式（`sk-`、`AKID`、长 hex 串抽目检）→ 零命中
  - 本机运维件（scripts/ 个人薄壳、`config.local*`、`.aoci`、`aoci*.txt`）→ 不存在
  - 版本号两处（`pyproject` + `__init__.__version__`）→ 与本次发版号一致
- [ ] `pip install` 该 wheel 冒烟（`agent_mailbox --help` 或 import）→ 正常
- [ ] **全绿才允许 `git tag`**——发布后的线上复扫只是验证，不是检查手段

## 6 · 发布后抽查（发布当天，§5.5 的线上验证面）

> 判据由 HS 0.7.4 派单写死（t-35③）；工具=`scripts/verify_release.sh`。
> 结论口径必须带「（**分发包面**）」限定——仓面是否清理是老板拍板项，勿对外夸大。

- [ ] 从 PyPI 下载**线上** sdist + wheel（不是本地 dist/）
- [ ] `scripts/verify_release.sh <线上wheel> <线上sdist>` → PASS
- [ ] 抽查新版前一版产物 → 应 FAIL（判据有效性自证，判据改动须 HS 复核）
- [ ] PyPI 页面核：latest 版本号 / yanked=false / digest 与 build 一致
