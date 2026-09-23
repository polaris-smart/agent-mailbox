# 发版 checklist（release checklist）

> 目的：根治「每次发版只更英文 README、多语言/CHANGELOG/SKILL.md 全部掉队」——v0.6.0 实测漏项，v0.6.1 被迫纯文档热修补齐。
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
- [ ] push main → push `vx.y.z` tag（Actions 自动发 PyPI，**勿手动上传**）
- [ ] 回报：commit SHA + Actions run 链接 + 六 README 锚点 grep 输出 + CHANGELOG 段标题
