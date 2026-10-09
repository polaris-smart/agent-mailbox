"""graft 只读查询包装（**受管面** ✓ 依据 HS《几件套集成方案》§二 + dsh 评估）。

* **只读**：只做 `graft ask` / `graft callers` 查询 ✓；`graft build`（写）**不集成** ✗（归仓主权方手工 ✓）。
* **共用底座**：走 `workbench_cli_query.run()` ✓ ⇒ provenance / 陈旧门 / 超时 / 降级只写一次 ✓。
* **降级不炸**：graft 未安装 ⇒ `CLI_MISSING` + 安装提示 ✓，绝不抛异常中断员工任务 ✗。
输出**最小透传** ✓（上游 JSON 形状可能随版本变 ⇒ 不猜字段 ✗）。
"""

from __future__ import annotations

from pathlib import Path

from . import workbench_cli_query as cq

GRAFT_ASK_TIMEOUT = 20.0
GRAFT_CALLERS_TIMEOUT = 20.0


def _normalize(raw: dict, provider_args: list[str]) -> dict:
    references: list = []
    data = raw.get("data")
    if isinstance(data, dict):
        for key in ("references", "cards", "files", "results"):
            value = data.get(key)
            if isinstance(value, list):
                references = value[:50]
                break
    return {
        "ok": True,
        "data": data,
        "references": references,
        "args": provider_args,
        "provenance": raw.get("provenance", {}),
        "error": None,
    }


def graft_ask(project_path: str | Path, task: str) -> dict:
    """仓认知问答：`graft ask "<task>" --json`（只读 ✓）。"""
    question = (task or "").strip()
    if not question:
        return {
            "ok": False,
            "data": None,
            "references": [],
            "args": [],
            "provenance": {},
            "error": {"code": "BAD_ARGUMENT", "message": "请给出要问的问题（非空）"},
        }
    args = ["ask", question, "--json"]
    root = cq.resolve_project_root(project_path)
    raw = cq.run(
        "graft",
        args,
        cwd=root,
        timeout=GRAFT_ASK_TIMEOUT,
        index_path=cq.default_index_path("graft", root),
    )
    return raw if not raw.get("ok") else _normalize(raw, args)


def graft_callers(project_path: str | Path, symbol: str) -> dict:
    """调用链追踪：`graft callers <symbol>`（只读 ✓）。"""
    name = (symbol or "").strip()
    if not name:
        return {
            "ok": False,
            "data": None,
            "references": [],
            "args": [],
            "provenance": {},
            "error": {"code": "BAD_ARGUMENT", "message": "请给出符号名（非空）"},
        }
    args = ["callers", name]
    root = cq.resolve_project_root(project_path)
    raw = cq.run(
        "graft",
        args,
        cwd=root,
        timeout=GRAFT_CALLERS_TIMEOUT,
        index_path=cq.default_index_path("graft", root),
    )
    return raw if not raw.get("ok") else _normalize(raw, args)


def graft_available() -> bool:
    """graft 是否可用（供 UI/工具面决定是否展示入口 ✓ 缺就隐藏 ✗ 不报错 ✗）。"""
    return cq.resolve_binary("graft") is not None
