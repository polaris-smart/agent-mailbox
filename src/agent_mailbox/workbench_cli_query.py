"""几件套只读查询的**通用底座**（graft / aoci / codegraph 共用）。

依据：HS《几件套集成方案-workspace_mcp扩展》+ dsh 评估（**别复制三遍** ✗）。
本模块只负责四件事，供各 provider 复用：

1. **解析可执行文件**：env 覆盖优先（`AGENT_MAIL_<PROVIDER>_BIN`）⇒ PATH 查找；
2. **带超时/降级地跑 CLI**：缺二进制、超时、非 JSON 一律**返回结构化错误** ✗ 不抛异常中断；
3. **附 provenance**：provider · 工具版本 · 二进制路径 · git revision/dirty · **陈旧门**
   （索引时间 vs HEAD 提交时间 ⇒ `stale_days`）—— 评审教训：没有陈旧门就会**拿过期索引当证据** ✗；
4. **统一错误码**：`CLI_MISSING` / `CLI_TIMEOUT` / `CLI_BAD_OUTPUT` / `CLI_FAILED` / `BAD_PROVIDER`。

只读 ✓：本模块**不执行任何写操作**（build/index/onboard 归仓主权方手工 ✗）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path


class ProjectPathError(RuntimeError):
    """项目路径非法 ⇒ 拒绝查询（邮箱会话不得拿只读工具当「读任意目录」的口子）。"""


DEFAULT_TIMEOUT = 15.0
MAX_OUTPUT_BYTES = 4 * 1024 * 1024

# provider 注册表：env 覆盖键 → PATH 候选名
PROVIDERS: dict[str, dict[str, object]] = {
    "codegraph": {
        "env": "AGENT_MAIL_CODEGRAPH_BIN",
        "names": ("codegraph",),
        "version_args": ("--version",),
    },
    "graft": {"env": "AGENT_MAIL_GRAFT_BIN", "names": ("graft",), "version_args": ("--version",)},
    "aoci": {"env": "AGENT_MAIL_AOCI_BIN", "names": ("aoci",), "version_args": ("--version",)},
}


def resolve_binary(provider: str) -> str | None:
    """env 覆盖优先，其次 PATH。找不到返回 None（调用方降级 ✓ 不抛 ✗）。"""
    spec = PROVIDERS.get(provider)
    if spec is None:
        return None
    override = os.environ.get(str(spec["env"]), "").strip()
    if override:
        return override if Path(override).exists() else None
    for name in spec["names"]:  # type: ignore[union-attr]
        found = shutil.which(name)
        if found:
            return found
    return None


def tool_version(binary: str, provider: str, timeout: float = 5.0) -> str | None:
    """尽力取版本（失败返回 None ✓ 不阻塞查询 ✗）。"""
    spec = PROVIDERS.get(provider) or {}
    args = list(spec.get("version_args") or ("--version",))  # type: ignore[arg-type]
    try:
        done = subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=clean_git_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (done.stdout or done.stderr or "").strip().splitlines()
    return text[0][:120] if text else None


def git_facts(root: Path) -> tuple[str | None, bool | None]:
    """(revision, working_tree_dirty)。非 git 仓返回 (None, None) ✓。"""
    try:
        rev = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=clean_git_env(),
        )
        if rev.returncode != 0:
            return None, None
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=clean_git_env(),
        )
        return rev.stdout.strip(), bool(status.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return None, None


def index_staleness(root: Path, index_path: Path | None = None) -> dict:
    """陈旧门：索引文件时间 vs HEAD 提交时间 ⇒ 落后天数（拿不到就给 None ✓ 不猜 ✗）。"""
    facts: dict[str, object] = {
        "index_path": str(index_path) if index_path else None,
        "index_mtime": None,
        "head_time": None,
        "stale_days": None,
    }
    if index_path is None or not index_path.exists():
        return facts
    try:
        facts["index_mtime"] = int(index_path.stat().st_mtime)
    except OSError:
        return facts
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "log", "-1", "--format=%ct"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=clean_git_env(),
        )
        if out.returncode == 0 and out.stdout.strip().isdigit():
            head_time = int(out.stdout.strip())
            facts["head_time"] = head_time
            if head_time > int(facts["index_mtime"]):  # type: ignore[arg-type]
                facts["stale_days"] = round((head_time - int(facts["index_mtime"])) / 86400, 2)
            else:
                facts["stale_days"] = 0
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return facts


def run(
    provider: str,
    args: list[str],
    *,
    cwd: Path,
    timeout: float = DEFAULT_TIMEOUT,
    index_path: Path | None = None,
) -> dict:
    """跑一次只读查询，**永不抛**（除参数错）✓ 返回 {ok, data, provenance, error}。"""
    spec = PROVIDERS.get(provider)
    if spec is None:
        return {
            "ok": False,
            "data": None,
            "provenance": {},
            "error": {"code": "BAD_PROVIDER", "message": f"未知 provider：{provider}"},
        }
    binary = resolve_binary(provider)
    if not binary:
        return {
            "ok": False,
            "data": None,
            "provenance": {"provider": provider, "binary": None},
            "error": {
                "code": "CLI_MISSING",
                "message": f"未找到 {provider} 可执行文件；设置 {spec['env']} 或安装后重试。",
            },
        }
    revision, dirty = git_facts(cwd)
    provenance: dict[str, object] = {
        "provider": provider,
        "binary": binary,
        "version": tool_version(binary, provider),
        "revision": revision,
        "working_tree_dirty": dirty,
        "mode": "cli-read-only",
    }
    provenance["staleness"] = index_staleness(cwd, index_path)
    started = time.monotonic()
    try:
        done = subprocess.run(
            [binary, *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=min(timeout, DEFAULT_TIMEOUT),
            check=False,
            env=clean_git_env(),
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "data": None,
            "provenance": provenance,
            "error": {"code": "CLI_TIMEOUT", "message": f"{provider} 查询超时（>{timeout:.0f}s）"},
        }
    except OSError as exc:
        return {
            "ok": False,
            "data": None,
            "provenance": provenance,
            "error": {"code": "CLI_FAILED", "message": f"{provider} 无法启动：{exc}"},
        }
    provenance["duration_ms"] = int((time.monotonic() - started) * 1000)
    raw = (done.stdout or "")[:MAX_OUTPUT_BYTES]
    if not raw.strip():
        return {
            "ok": False,
            "data": None,
            "provenance": provenance,
            "error": {
                "code": "CLI_FAILED",
                "message": f"{provider} 没有输出（stderr: {(done.stderr or '')[:160]}）",
            },
        }
    try:
        return {"ok": True, "data": json.loads(raw), "provenance": provenance, "error": None}
    except ValueError:
        return {
            "ok": False,
            "data": None,
            "provenance": provenance,
            "error": {
                "code": "CLI_BAD_OUTPUT",
                "message": f"{provider} 输出不是 JSON（前 120 字：{raw[:120]}）",
            },
        }


def run_text(
    provider: str,
    args: list[str],
    *,
    cwd: Path,
    timeout: float = DEFAULT_TIMEOUT,
    index_path: Path | None = None,
) -> dict:
    """同 `run()`，但上游**输出是文本而非 JSON** 时用这个 ✓（aoci 实测如此 ✗）。

    返回 {ok, text, summary, structured: False, provenance, error}；
    与 `run()` 共享 resolve/超时/降级/provenance/陈旧门 ✓ 只差解析方式 ✓。
    """
    spec = PROVIDERS.get(provider)
    if spec is None:
        return {
            "ok": False,
            "text": None,
            "summary": None,
            "structured": False,
            "provenance": {},
            "error": {"code": "BAD_PROVIDER", "message": f"未知 provider：{provider}"},
        }
    binary = resolve_binary(provider)
    if not binary:
        return {
            "ok": False,
            "text": None,
            "summary": None,
            "structured": False,
            "provenance": {"provider": provider, "binary": None},
            "error": {
                "code": "CLI_MISSING",
                "message": f"未找到 {provider} 可执行文件；设置 {spec['env']} 或安装后重试。",
            },
        }
    revision, dirty = git_facts(cwd)
    provenance: dict[str, object] = {
        "provider": provider,
        "binary": binary,
        "version": tool_version(binary, provider),
        "revision": revision,
        "working_tree_dirty": dirty,
        "mode": "cli-read-only",
    }
    provenance["staleness"] = index_staleness(cwd, index_path)
    started = time.monotonic()
    try:
        done = subprocess.run(
            [binary, *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=min(timeout, DEFAULT_TIMEOUT),
            check=False,
            env=clean_git_env(),
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "text": None,
            "summary": None,
            "structured": False,
            "provenance": provenance,
            "error": {"code": "CLI_TIMEOUT", "message": f"{provider} 查询超时（>{timeout:.0f}s）"},
        }
    except OSError as exc:
        return {
            "ok": False,
            "text": None,
            "summary": None,
            "structured": False,
            "provenance": provenance,
            "error": {"code": "CLI_FAILED", "message": f"{provider} 无法启动：{exc}"},
        }
    provenance["duration_ms"] = int((time.monotonic() - started) * 1000)
    text = ((done.stdout or "") + (("\n" + done.stderr) if done.stderr else ""))[:MAX_OUTPUT_BYTES]
    if not text.strip():
        return {
            "ok": False,
            "text": None,
            "summary": None,
            "structured": False,
            "provenance": provenance,
            "error": {"code": "CLI_FAILED", "message": f"{provider} 没有输出"},
        }
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return {
        "ok": done.returncode == 0,
        "text": text,
        "summary": lines[0][:200] if lines else None,
        "structured": False,
        "provenance": provenance,
        "error": None
        if done.returncode == 0
        else {
            "code": "CLI_FAILED",
            "message": f"{provider} 退出码 {done.returncode}：{(done.stderr or text)[:160]}",
        },
    }


def clean_git_env():
    """Clean inherited env vars that can hijack git (eng-verify B4).

    GIT_DIR / GIT_WORK_TREE inherited from the parent make `git -C <dir>` ignore -C,
    so read-only queries would run against another repository.
    """
    env = dict(os.environ)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"):
        env.pop(key, None)
    return env


def resolve_project_root(path: str | Path) -> Path:
    """把调用方给的路径收敛成**合法项目根**，否则抛 `ProjectPathError`。

    HS 2026-10-07 裁定：graft/aoci 子进程必须限制在本项目 checkout 内，禁止任意路径。
    规则：存在且是目录 · 不许是文件系统根/主目录本身 · 必须是 git 工作树 · 归一化消 `..`。
    注意：**工具层不接受 agent 传来的路径** —— 服务端按项目解析后再传入，这里是第二道防线。
    """
    raw = Path(path).expanduser()
    try:
        resolved = raw.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectPathError(f"项目路径不存在或不可解析：{raw}（{exc}）") from exc
    if not resolved.is_dir():
        raise ProjectPathError(f"项目路径不是目录：{resolved}")
    home = Path.home().resolve()
    if resolved == Path(resolved.anchor) or resolved == home:
        raise ProjectPathError(f"拒绝把 {resolved} 当项目根（文件系统根或主目录本身）")
    top = subprocess.run(
        ["git", "-C", str(resolved), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        env=clean_git_env(),
        check=False,
    )
    if top.returncode != 0:
        raise ProjectPathError(
            f"不是 git 工作树：{resolved}（git 说：{(top.stderr or '').strip()[:120]}）"
        )
    try:
        top_path = Path(top.stdout.strip()).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectPathError(f"git 工作树路径不可解析：{resolved}（{exc}）") from exc
    if top_path != resolved:
        raise ProjectPathError(
            f"拒绝：{resolved} 不是独立工作树根（git 说实际根是 {top_path}）：worktree/gitdir 重定向/嵌套仓库一律拒绝"
        )
    git_dir = subprocess.run(
        ["git", "-C", str(resolved), "rev-parse", "--absolute-git-dir"],
        capture_output=True,
        text=True,
        env=clean_git_env(),
        check=False,
    )
    if git_dir.returncode != 0:
        raise ProjectPathError(f"读不到 git-dir：{resolved}")
    try:
        git_dir_path = Path(git_dir.stdout.strip()).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectPathError(f"git-dir 不可解析：{resolved}（{exc}）") from exc
    outside = git_dir_path != resolved and resolved not in git_dir_path.parents
    if outside:
        # 合法 linked worktree / submodule 的 gitdir **天生在根外** ✓ ⇒ 判别物是它的 `commondir` 文件 ✓
        # （攻击样例指向的**主 .git 没有** commondir ✗ ⇒ 仍拒 ✓ 这条是拦 B1/B2 的承重墙 ✓ 不能撤 ✗）
        # ⚠️ 只判 commondir **存在** 会被伪造 ✗（flow-product：假 .git 指向**受害者的合法 worktree** ⇒ 它天然有
        # commondir ⇒ 越界读复活 ✓ —— 我 B1 那条"存在性≠有效性"的教训在**下一层**复发了 ✗）
        # ⇒ 真正的判别物是**回指针**：`<git-dir>/gitdir` 必须指回本目录的 .git ✓（伪造里它指受害者 ⇒ 一读就挡 ✓）
        back = None
        try:
            raw_back = (
                (git_dir_path / "gitdir").read_text(encoding="utf-8", errors="replace").strip()
            )
            if raw_back:
                candidate = Path(raw_back)
                back = (candidate if candidate.is_absolute() else git_dir_path / candidate).resolve(
                    strict=True
                )
        except (OSError, RuntimeError):
            back = None
        legit_worktree = (git_dir_path / "commondir").is_file() and back == (resolved / ".git")
        if not legit_worktree:
            raise ProjectPathError(
                f"拒绝：git-dir 不在项目根内（{git_dir_path} 在 {resolved} 之外）"
                "—— 疑似 gitdir 重定向/软链（合法 worktree 应有 commondir ✓）"
            )
    return resolved


# 各 provider 的**默认索引位置**（陈旧门靠它把"索引时间 vs HEAD"算出来 ✓；都不存在则 None ✓）
INDEX_CANDIDATES: dict[str, tuple[str, ...]] = {
    "aoci": (".aoci/baseline.json",),
    "codegraph": (".codegraph/codegraph.db",),
    "graft": ("graft/index.json", "graft"),  # graft 索引形态未实测 ⇒ 存在即用 ✓ 否则 None（不猜 ✗）
}


def default_index_path(provider: str, root: str | Path) -> Path | None:
    """返回该项目里该 provider 的默认索引路径（不存在 ⇒ None ⇒ 陈旧门如实说"不可判定" ✓）。"""
    for relative in INDEX_CANDIDATES.get(provider, ()):
        candidate = Path(root) / relative
        if candidate.exists():
            return candidate
    return None
