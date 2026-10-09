"""aoci 只读诊断包装（**受管面** ✓ 依据 HS《几件套集成方案》§三 + dsh 评估）。

三个只读诊断（照规范 ✓）：
* `aoci_doctor()`  —— 认知层健康检查（`aoci doctor`）
* `aoci_status()`  —— 条目数/基线/漂移（`aoci status`）
* `aoci_check()`   —— 治理 findings 明细（`aoci check`）

**只读边界**：`aoci index` / `aoci onboard` / `aoci baseline` 等**写操作不集成** ✗（归仓主权方手工 ✓）。
**实测事实**：aoci 输出是**文本** ✗（`--json` 未生效 ✓）⇒ 用底座 `run_text()` ✓ 并显式标
`structured: False` ✓ —— **不假装是结构化数据** ✗（评审纪律：宣称与事实分离 ✓）。
"""

from __future__ import annotations

from pathlib import Path

from . import workbench_cli_query as cq

AOCI_TIMEOUT = 30.0
_WRITE_SUBCOMMANDS = ("index", "onboard", "baseline", "cognition")  # 一律不集成 ✗


def _guard(
    subcommand: str, cwd: Path, index_path: Path | None, timeout: float = AOCI_TIMEOUT
) -> dict:
    if subcommand in _WRITE_SUBCOMMANDS:
        return {
            "ok": False,
            "text": None,
            "summary": None,
            "structured": False,
            "provenance": {},
            "error": {
                "code": "WRITE_NOT_INTEGRATED",
                "message": f"aoci {subcommand} 是写操作，按规范不集成 ✗；请由仓主权方手工执行 ✓",
            },
        }
    try:
        root = cq.resolve_project_root(cwd)
    except cq.ProjectPathError as exc:
        return {
            "ok": False,
            "text": None,
            "summary": None,
            "structured": False,
            "provenance": {},
            "error": {"code": "BAD_PROJECT_PATH", "message": str(exc)},
        }
    return cq.run_text(
        "aoci",
        [subcommand],
        cwd=root,
        timeout=timeout,
        index_path=index_path or cq.default_index_path("aoci", root),
    )


def aoci_doctor(project_path: str | Path, index_path: Path | None = None) -> dict:
    """认知层健康检查（只读 ✓）。"""
    return _guard("doctor", Path(project_path), index_path)


def aoci_status(project_path: str | Path, index_path: Path | None = None) -> dict:
    """条目数 / 基线 / 漂移（只读 ✓）。"""
    return _guard("status", Path(project_path), index_path)


def aoci_check(project_path: str | Path, index_path: Path | None = None) -> dict:
    """治理 findings 明细（只读 ✓）。"""
    return _guard("check", Path(project_path), index_path)


def aoci_available() -> bool:
    """aoci 是否可用（UI/工具面据此决定是否展示入口 ✓ 缺则隐藏 ✗ 不报错 ✗）。"""
    return cq.resolve_binary("aoci") is not None
