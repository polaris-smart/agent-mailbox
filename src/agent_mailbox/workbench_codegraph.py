"""codegraph 重索引（**人侧写操作**，与只读严格分层）。

依据 HS《几件套集成方案》§四 + §七 自动化分层原则：
* **只读查询** ⇒ MCP wrapper 自动注入给员工（`project_code_search` / graft / aoci ✓）
* **写操作**（`graft build` / `aoci index` / **`codegraph index`**）⇒ **归仓主权方手工** ✗
  ⇒ 因此本模块**不进**任何员工工具面、**不进** `_READ_ONLY_KNOWLEDGE` ✓
  ⇒ 且必须**显式确认**（`confirmed=True`）才会执行 ✓ —— 防止"顺手调用就重建索引"✗

实测背景：本机 `.aoci/baseline.json` 比提交旧 1.49 天 ✓；codegraph 索引需显式重建 ✓。
"""

from __future__ import annotations

from pathlib import Path

from . import workbench_cli_query as cq

INDEX_TIMEOUT = 120.0
INDEX_ARGS = ("index",)


def reindex(project_path: str | Path, *, confirmed: bool) -> dict:
    """**只有人显式确认**才重建 codegraph 索引 ✓（写操作 ✓ 不自动、不授权员工 ✗）。

    返回 {ok, text, provenance, error}；未确认 ⇒ `CONFIRM_REQUIRED` 且**不调 CLI** ✗。
    """
    if confirmed is not True:
        return {
            "ok": False,
            "text": None,
            "provenance": {},
            "error": {
                "code": "CONFIRM_REQUIRED",
                "message": "重建索引是写操作：需人显式确认（confirmed=True）才执行 ✗",
            },
        }
    try:
        root = cq.resolve_project_root(project_path)
    except cq.ProjectPathError as exc:
        return {
            "ok": False,
            "text": None,
            "provenance": {},
            "error": {"code": "BAD_PROJECT_PATH", "message": str(exc)},
        }
    out = cq.run_text(
        "codegraph",
        list(INDEX_ARGS),
        cwd=root,
        timeout=INDEX_TIMEOUT,
        index_path=cq.default_index_path("codegraph", root),
    )
    return {
        "ok": out.get("ok", False),
        "text": out.get("text"),
        "provenance": out.get("provenance", {}),
        "error": out.get("error"),
    }


def codegraph_available() -> bool:
    """codegraph 是否可用（UI 据此决定是否展示「重建索引」按钮 ✓ 缺则隐藏 ✗ 不报错 ✗）。"""
    return cq.resolve_binary("codegraph") is not None
