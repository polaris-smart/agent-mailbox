"""Isolated Git task workspaces and immutable, explicitly applied deliveries."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from . import workbench_policy, workbench_proof
from .workbench_private import private_mode
from .workbench_resources import scrub_credentials
from .workbench_store import WorkbenchError, _now

WORKSPACE_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS task_workspaces (
        task_id TEXT PRIMARY KEY REFERENCES tasks(id), source_path TEXT NOT NULL,
        path TEXT NOT NULL, base_commit TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS task_deliveries (
        task_id TEXT PRIMARY KEY REFERENCES tasks(id), payload TEXT NOT NULL,
        patch TEXT NOT NULL, patch_sha256 TEXT NOT NULL, captured_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS task_delivery_applications (
        task_id TEXT PRIMARY KEY REFERENCES tasks(id), applied_at TEXT NOT NULL)""",
)
LIMIT = 1024 * 1024


def _payload_hash(payload):
    frozen = {k: v for k, v in payload.items() if k != "payload_sha256"}
    return hashlib.sha256(
        json.dumps(frozen, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _intact(row, payload):
    return hashlib.sha256(row["patch"].encode()).hexdigest() == row["patch_sha256"] and payload.get(
        "payload_sha256"
    ) == _payload_hash(payload)


def _git(path, *args, input_text=None, allowed=(0,)):
    # Files bound output memory even if Git emits a large patch.
    with tempfile.TemporaryFile() as output:
        try:
            proc = subprocess.run(
                [
                    "git",
                    "-c",
                    f"core.hooksPath={os.devnull}",
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "diff.external=",
                    "-C",
                    str(path),
                    *args,
                ],
                input=input_text.encode() if input_text is not None else None,
                stdout=output,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=20,
                env={
                    **{
                        k: v
                        for k, v in os.environ.items()
                        if k
                        in {
                            "PATH",
                            "SYSTEMROOT",
                            "WINDIR",
                            "TEMP",
                            "TMP",
                            "TMPDIR",
                            "LANG",
                            "LC_ALL",
                        }
                    },
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_TERMINAL_PROMPT": "0",
                    "GIT_OPTIONAL_LOCKS": "0",
                },
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkbenchError(
                "workspace_git_failed", "Git 操作未完成，请检查仓库和 Git 安装。"
            ) from exc
        output.seek(0)
        raw = output.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise WorkbenchError("delivery_too_large", "交付内容超过 1 MiB，请缩小修改范围。")
    if proc.returncode not in allowed:
        raise WorkbenchError("workspace_git_failed", "Git 校验失败，原目录未被自动覆盖。")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkbenchError(
            "delivery_unsafe", "交付包含非 UTF-8 内容，暂不支持自动合入。"
        ) from exc


def _root(path):
    path = Path(path)
    if path.is_symlink() or (path / ".git").is_symlink():
        raise WorkbenchError("delivery_unsafe", "Git 根目录或元数据包含符号链接。")
    path = path.resolve()
    if Path(_git(path, "rev-parse", "--show-toplevel").strip()).resolve() != path:
        raise WorkbenchError("workspace_root_required", "修改任务必须选择 Git 仓库根目录。")
    return path


def _baseline(path, commit=None):
    head = _git(path, "rev-parse", "HEAD").strip()
    if commit and head != commit:
        raise WorkbenchError("workspace_stale", "原仓库起始版本已改变，请重新创建任务。")
    if _git(path, "status", "--porcelain=v1", "--untracked-files=all"):
        raise WorkbenchError("workspace_dirty", "原仓库有未提交文件，请先保存或提交后再继续。")
    return head


def _safe_path(root, name):
    part = PurePosixPath(name)
    if not name or part.is_absolute() or any(p in ("..", ".git") for p in part.parts):
        raise WorkbenchError("delivery_unsafe", "交付路径不安全，不能自动合入。")
    if any(
        p.startswith(".env")
        or p.lower() in ("credentials", "credentials.json", "id_rsa", "id_ed25519")
        or p.lower().endswith((".pem", ".key", ".p12", ".pfx"))
        for p in part.parts
    ):
        raise WorkbenchError("delivery_unsafe", "交付包含凭据文件，请移除后重新提交。")
    target = root
    for component in part.parts:
        target = target / component
        if target.is_symlink():
            raise WorkbenchError("delivery_unsafe", "交付包含符号链接，不能自动合入。")
    return target


def prepare_workspace(store, task, project):
    if task.get("permission_mode") != "workspace-write":
        return dict(project)
    source = _root(project["path"])
    base = _baseline(source)
    task_id = task["id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
        raise WorkbenchError("delivery_unsafe", "任务标识不合法。")
    directory = store.root / "task-workspaces"
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink():
        raise WorkbenchError("delivery_unsafe", "工作区目录不安全。")
    private_mode(directory, 0o700)
    destination = directory / task_id
    with store._transaction() as db:
        existing = db.execute(
            "SELECT * FROM task_workspaces WHERE task_id=?", (task_id,)
        ).fetchone()
        if existing:
            if existing["source_path"] != str(source) or existing["base_commit"] != base:
                raise WorkbenchError("workspace_stale", "任务工作区基线已改变。")
            _root(existing["path"])
            return {**project, "path": existing["path"]}
        seed = None
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "task_links" in tables:
            link = db.execute(
                "SELECT parent_task_id FROM task_links WHERE task_id=?", (task_id,)
            ).fetchone()
            if link:
                seed = db.execute(
                    "SELECT * FROM task_deliveries WHERE task_id=?", (link[0],)
                ).fetchone()
                old_workspace = db.execute(
                    "SELECT * FROM task_workspaces WHERE task_id=?", (link[0],)
                ).fetchone()
                if (
                    not seed
                    or not old_workspace
                    or old_workspace["source_path"] != str(source)
                    or old_workspace["base_commit"] != base
                ):
                    raise WorkbenchError("workspace_stale", "上一轮交付基线不匹配，无法继承修改。")
                if not _intact(seed, json.loads(seed["payload"])) or not json.loads(
                    seed["payload"]
                ).get("safe_patch"):
                    raise WorkbenchError("delivery_unsafe", "上一轮交付不能安全继承。")
        if destination.exists() or destination.is_symlink():
            raise WorkbenchError("workspace_exists", "工作区路径已存在，请检查保留的任务目录。")
        _git(source, "worktree", "add", "--detach", str(destination), base)
        if seed and seed["patch"]:
            _git(destination, "apply", "--check", "-", input_text=seed["patch"])
            _git(destination, "apply", "-", input_text=seed["patch"])
        db.execute(
            "INSERT INTO task_workspaces VALUES(?,?,?,?)",
            (task_id, str(source), str(destination), base),
        )
    return {**project, "path": str(destination)}


def capture_delivery(store, task, project, result):
    with store._transaction() as db:
        old = db.execute(
            "SELECT payload FROM task_deliveries WHERE task_id=?", (task["id"],)
        ).fetchone()
        if old:
            return json.loads(old[0])
        workspace = db.execute(
            "SELECT * FROM task_workspaces WHERE task_id=?", (task["id"],)
        ).fetchone()
        payload = {
            "task_id": task["id"],
            "workspace": dict(workspace) if workspace else None,
            "files": [],
            "diff": "",
            "summary": str(result.get("output_text", ""))[:65536],
            "verification": {
                "status": "not_verified",
                "notes": "员工报告不等于系统测试通过。",
                "employee_reported": str(result.get("output_text", ""))[:65536],
            },
            "system": {"execution_status": result.get("status"), "tests_run": False},
            "safe_patch": False,
            "capture_error": None,
            "captured_at": _now(),
        }
        patch = ""
        try:
            if workspace:
                path = _root(workspace["path"])
                common = Path(
                    _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
                ).resolve()
                source_common = Path(
                    _git(
                        workspace["source_path"],
                        "rev-parse",
                        "--path-format=absolute",
                        "--git-common-dir",
                    ).strip()
                ).resolve()
                if common != source_common:
                    raise WorkbenchError("delivery_unsafe", "任务工作区 Git 元数据已改变。")
                tracked = _git(path, "diff", "--name-only", "-z", workspace["base_commit"]).split(
                    "\0"
                )
                untracked = _git(path, "ls-files", "--others", "--exclude-standard", "-z").split(
                    "\0"
                )
                names = sorted({n for n in tracked + untracked if n})
                for name in names:
                    _safe_path(path, name)
                    _safe_path(Path(workspace["source_path"]), name)
                payload["files"] = [
                    {"path": name, "status": "untracked" if name in untracked else "modified"}
                    for name in names
                ]
                patch = _git(
                    path, "diff", "--no-ext-diff", "--no-textconv", workspace["base_commit"], "--"
                )
                for name in untracked:
                    if name:
                        patch += _git(
                            path,
                            "diff",
                            "--no-index",
                            "--no-ext-diff",
                            "--no-textconv",
                            "--",
                            "/dev/null",
                            name,
                            allowed=(0, 1),
                        )
                if (
                    len(patch.encode()) > LIMIT
                    or "GIT binary patch" in patch
                    or "Binary files " in patch
                    or "mode 120000" in patch
                ):
                    raise WorkbenchError(
                        "delivery_unsafe", "二进制、符号链接或超大修改需人工检查，不能自动合入。"
                    )
                clean_patch = scrub_credentials(store._scrub(db, patch))
                if clean_patch != patch:
                    raise WorkbenchError(
                        "delivery_unsafe", "修改中检测到凭据，已隐藏，不能自动合入。"
                    )
                payload["safe_patch"] = True
        except WorkbenchError as exc:
            payload["capture_error"] = {"code": exc.code, "message": str(exc)}
        patch = scrub_credentials(store._scrub(db, patch))
        payload["diff"] = patch
        payload = store._scrub(db, payload)
        payload["summary"] = scrub_credentials(payload["summary"])
        payload["verification"]["employee_reported"] = scrub_credentials(
            payload["verification"]["employee_reported"]
        )
        payload["payload_sha256"] = _payload_hash(payload)
        db.execute(
            "INSERT INTO task_deliveries VALUES(?,?,?,?,?)",
            (
                task["id"],
                json.dumps(payload, ensure_ascii=False),
                patch,
                hashlib.sha256(patch.encode()).hexdigest(),
                payload["captured_at"],
            ),
        )
        return payload


def _apply_reason(task, row, payload, applied):
    if applied:
        return "already_applied"
    if task["status"] != "done":
        return "human_acceptance_required"
    if not row or not payload.get("workspace") or not payload.get("safe_patch"):
        return "unsafe_or_no_workspace"
    if not row["patch"]:
        return "no_changes"
    if not _intact(row, payload):
        return "delivery_integrity_error"
    try:
        source = _root(payload["workspace"]["source_path"])
        _baseline(source, payload["workspace"]["base_commit"])
        for file in payload["files"]:
            _safe_path(source, file["path"])
        _git(source, "apply", "--check", "-", input_text=row["patch"])
    except WorkbenchError as exc:
        return exc.code
    return None


def task_delivery(store, taskid):
    with store._transaction(readonly=True) as db:
        task = store._required(db, "tasks", taskid)
        if task["execution_mode"] == "mailbox":
            return store._scrub(
                db,
                {
                    "task_id": taskid,
                    "workspace": None,
                    "files": [],
                    "diff": "",
                    "summary": task["result"],
                    "verification": {
                        "status": "employee_report",
                        "notes": "员工提交的结果；系统未独立捕获文件修改或验证测试。",
                    },
                    "capture_error": None,
                    "applied": False,
                    "can_apply": False,
                    "apply_blocked_reason": "mailbox_report_only",
                },
            )
        row = db.execute("SELECT * FROM task_deliveries WHERE task_id=?", (taskid,)).fetchone()
        payload = (
            json.loads(row["payload"])
            if row
            else {
                "task_id": taskid,
                "workspace": None,
                "files": [],
                "diff": "",
                "summary": "",
                "verification": {"status": "not_verified", "notes": "尚未捕获交付。"},
                "capture_error": None,
            }
        )
        applied = bool(
            db.execute(
                "SELECT 1 FROM task_delivery_applications WHERE task_id=?", (taskid,)
            ).fetchone()
        )
        reason = _apply_reason(task, row, payload, applied)
        payload.update(applied=applied, can_apply=reason is None, apply_blocked_reason=reason)
        return store._scrub(db, payload)


def apply_delivery(store, taskid):
    # HTTP owner confirmation is handled by the caller. This transaction serializes applies.
    with store._transaction() as db:
        task = store._required(db, "tasks", taskid)
        project_id = task["project_id"]
    # 审核机制（默认 off ⇒ 行为不变）：门禁放在写事务之前，且**重算哈希**
    # —— 同行校验过后再替换产出物（TOCTOU）不得仍然放行
    policy = workbench_policy.effective_policy(store, project_id)
    if policy.get("peer_review") == "required_for_write":
        verdict = workbench_proof.verify_proof(store, taskid)
        if verdict.get("verdict") != "verified":
            raise WorkbenchError(
                "PROOF_NOT_VERIFIED",
                f"交付产出物当前校验不通过（{verdict.get('verdict')}）："
                f"不符 {verdict.get('mismatch', 0)} · 缺失 {verdict.get('missing', 0)}；"
                "请重新校验后再合入。",
            )
        if not workbench_policy.peer_verified(store, taskid, task["assignee_id"]):
            raise WorkbenchError(
                "PEER_REVIEW_REQUIRED",
                "本项目策略要求：写能力交付须由另一名员工校验通过（同行校验）后才能合入。",
            )
    with store._transaction() as db:
        # 门在事务外算过一次；进入**写事务**后用同一连接复检 ——
        # 覆盖"门通过后有人重录证明"或"策略翻成 required"这两种竞态（第三轮复查指出）
        policy_in = workbench_policy.effective_policy_in(db, project_id)
        if policy_in.get(
            "peer_review"
        ) == "required_for_write" and not workbench_policy.peer_verified_in(
            db, taskid, task["assignee_id"]
        ):
            raise WorkbenchError(
                "PEER_REVIEW_REQUIRED",
                "本项目策略要求：写能力交付须由另一名员工校验通过（同行校验）后才能合入。",
            )
        row = db.execute("SELECT * FROM task_deliveries WHERE task_id=?", (taskid,)).fetchone()
        payload = json.loads(row["payload"]) if row else {}
        applied = bool(
            db.execute(
                "SELECT 1 FROM task_delivery_applications WHERE task_id=?", (taskid,)
            ).fetchone()
        )
        reason = _apply_reason(task, row, payload, applied)
        if reason:
            raise WorkbenchError(reason, "当前交付不能合入，请检查验收、仓库基线和修改内容。")
        _git(payload["workspace"]["source_path"], "apply", "-", input_text=row["patch"])
        db.execute("INSERT INTO task_delivery_applications VALUES(?,?)", (taskid, _now()))
    return task_delivery(store, taskid)
