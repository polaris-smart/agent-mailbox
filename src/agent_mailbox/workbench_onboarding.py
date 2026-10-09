"""Explicit project-bound connectivity probes; model claims are not proof."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from . import workbench_contract
from .workbench_runtime import discover_employees, runtime_status
from .workbench_store import ACTIVE, WorkbenchError, _text

ONBOARDING_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS employee_probes(task_id TEXT PRIMARY KEY REFERENCES tasks(id), employee_id TEXT NOT NULL REFERENCES employees(id), project_id TEXT NOT NULL REFERENCES projects(id), marker TEXT NOT NULL, context_seen INTEGER NOT NULL DEFAULT 0, note_seen INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS task_links(task_id TEXT PRIMARY KEY REFERENCES tasks(id), parent_task_id TEXT NOT NULL REFERENCES tasks(id), relation TEXT NOT NULL CHECK(relation='follow_up'))",
)


def probe_dict(db, row):
    if row is None:
        return None
    task = db.execute("SELECT status FROM tasks WHERE id=?", (row["task_id"],)).fetchone()
    return {
        "task_id": row["task_id"],
        "status": task["status"],
        "context_seen": bool(row["context_seen"]),
        "note_seen": bool(row["note_seen"]),
        "verified": bool(
            row["context_seen"] and row["note_seen"] and task["status"] in {"review", "done"}
        ),
    }


def onboarding_status(store, employee_id, project_id=None):
    snapshot = store.snapshot()
    employee = next((e for e in snapshot["employees"] if e["id"] == employee_id), None)
    if employee is None:
        raise WorkbenchError("NOT_FOUND", "找不到这位员工。")
    local = employee["node_id"] == store.local_node()["id"]
    checks = []

    def add(key, status, zh, en, action_zh, action_en):
        checks.append(
            {
                "id": key,
                "status": status,
                "message": {"zh": zh, "en": en},
                "action": {"zh": action_zh, "en": action_en},
            }
        )

    supported = employee["execution_supported"]
    member = bool(project_id and project_id in employee["project_ids"])
    active = employee["lifecycle"] == "active"
    add(
        "adapter",
        "passed" if supported else "blocked",
        "受支持的 CLI 执行适配器。" if supported else "已登记入口，但没有自动执行适配器。",
        "Supported CLI execution adapter."
        if supported
        else "Registered entry without an automatic execution adapter.",
        "选择 Codex 或 Claude CLI。",
        "Select a Codex or Claude CLI.",
    )
    add(
        "device",
        "passed" if local else "unknown",
        "员工在本机。" if local else "远端员工的原生状态需在节点验证。",
        "Employee is on this device." if local else "Verify native state on the remote node.",
        "远端接入验证将在节点侧单独支持。",
        "Remote connectivity probes will be supported separately on the node.",
    )
    candidate = None
    runtime = None
    if local:
        candidate = next(
            (
                e
                for e in discover_employees()
                if e["kind"] == employee["kind"]
                and e.get("connection_type", "cli") == employee["connection_type"]
                and (
                    not employee["entrypoint"]
                    or e.get("entrypoint", e.get("binary")) == employee["entrypoint"]
                )
            ),
            None,
        )
        runtime = runtime_status(store.root)
    found = bool(candidate)
    add(
        "entrypoint",
        "passed" if found else "blocked" if local else "unknown",
        "已发现原生入口。"
        if found
        else "本机未找到登记入口。"
        if local
        else "未在本机检查远端入口。",
        "Native entry discovered."
        if found
        else "Registered entry not found locally."
        if local
        else "Remote entry not checked locally.",
        "安装或重新发现员工入口。",
        "Install or rediscover the native entry.",
    )
    auth = candidate.get("auth_status", "unknown") if candidate else "unknown"
    add(
        "native_login",
        "passed"
        if auth == "authenticated"
        else "blocked"
        if auth == "auth_required"
        else "unknown",
        "原生登录已确认，不代表首次执行通过。"
        if auth == "authenticated"
        else "需要原生登录。"
        if auth == "auth_required"
        else "原生登录状态未确认，可由首次执行进一步验证。",
        "Native sign-in confirmed, but execution is not yet proved."
        if auth == "authenticated"
        else "Native sign-in is required."
        if auth == "auth_required"
        else "Native sign-in is unconfirmed; first execution can verify it.",
        "在对应 Agent 原生入口登录后再检查。",
        "Sign in through the native agent, then check again.",
    )
    ready = bool(runtime and runtime.get("installed") and runtime.get("node_available"))
    add(
        "runtime",
        "passed" if ready else "blocked" if local else "unknown",
        "执行组件可用。" if ready else "执行组件尚未就绪。",
        "Execution runtime ready." if ready else "Execution runtime is not ready.",
        "准备执行组件；App 发行包自带组件。",
        "Prepare the runtime; the App bundle includes it.",
    )
    add(
        "membership",
        "passed" if member else "blocked",
        "员工已加入所选项目。" if member else "先选择项目，并将员工加入项目组。",
        "Employee belongs to the selected project."
        if member
        else "Choose a project and add the employee first.",
        "从员工卡片选择项目或进入项目成员。",
        "Select a project from the employee card or project members.",
    )
    add(
        "lifecycle",
        "passed" if active else "blocked",
        "员工正常工作。" if active else "员工已暂停或退役。",
        "Employee is active." if active else "Employee is paused or retired.",
        "确认后恢复员工工作状态。",
        "Reactivate the employee when appropriate.",
    )
    with store._transaction(readonly=True) as db:
        latest = (
            db.execute(
                "SELECT * FROM employee_probes WHERE employee_id=? AND project_id=? ORDER BY created_at DESC LIMIT 1",
                (employee_id, project_id),
            ).fetchone()
            if project_id
            else None
        )
        probe = probe_dict(db, latest)
    verified = bool(probe and probe["verified"] and active and member)
    add(
        "project_tools",
        "passed" if verified else "unknown",
        "该项目的上下文读取和验证笔记已实际完成。"
        if verified
        else "尚未完成该项目的工具读取与回执验证。",
        "Project context and verification note completed."
        if verified
        else "Project tools and receipt have not been verified.",
        "显式运行只读接入验证，会使用模型额度并可能需要权限确认。",
        "Run the explicit read-only probe. It uses model quota and may require permission approval.",
    )
    return {
        "employee_id": employee_id,
        "project_id": project_id,
        "checks": checks,
        "probe": probe,
        "shared_native_identity": True,
        "can_verify": bool(
            local
            and supported
            and member
            and active
            and found
            and ready
            and auth != "auth_required"
            and not store.update_maintenance()["paused"]
        ),
    }


def create_probe(store, employee_id, project_id, model=None):
    model = _text(model, "model", 200) if model is not None else None
    with store._transaction() as db:
        employee = store._required(db, "employees", employee_id)
        if employee["node_id"] != store.local_node()["id"]:
            raise WorkbenchError("REMOTE_CHECK_UNAVAILABLE", "请在远端节点验证原生执行状态。")
        if db.execute("SELECT paused FROM update_settings WHERE id=1").fetchone()[0]:
            raise WorkbenchError("UPDATE_PAUSED", "更新准备期间暂停新验证，请先恢复接单。")
        store._required(db, "projects", project_id)
        if employee["lifecycle"] != "active":
            raise WorkbenchError("employee_inactive", "这位员工已暂停或退役。")
        if not store._membership(db, employee_id, project_id):
            raise WorkbenchError("permission_denied", "这位员工未加入该项目。")
        if not workbench_contract.execution_supported(
            employee["kind"], employee["connection_type"]
        ):
            raise WorkbenchError("ADAPTER_UNSUPPORTED", "此入口尚无执行适配器。")
        existing = db.execute(
            "SELECT p.task_id FROM employee_probes p JOIN tasks t ON t.id=p.task_id WHERE p.employee_id=? AND p.project_id=? AND t.status IN ('queued','starting','running','waiting_approval') ORDER BY p.created_at DESC LIMIT 1",
            (employee_id, project_id),
        ).fetchone()
        if existing:
            return {
                "task": store._entity(store._required(db, "tasks", existing["task_id"])),
                "reused": True,
            }
        marker = "MAILBOX-PROBE-" + secrets.token_hex(16)
        prompt = (
            "This is an explicit read-only connectivity probe. Do not modify files, use "
            "shell, delegate, or contact colleagues. Only invoke agent-mailbox-project "
            "MCP operations. Your client may expose them through tool discovery and an "
            "exec/code-mode transport wrapper; that discovery and wrapper are allowed "
            "solely to locate and call these project tools. If direct tools are absent, "
            "inspect the provided tool catalog (for example ALL_TOOLS in an exec "
            "wrapper); normalized names may contain agent_mailbox_project. Do not "
            "invoke shell, filesystem, or unrelated tools inside the wrapper. "
            "The first project operation must be project_context(), followed by "
            f"project_note(title='Agent connectivity verification', body='{marker}') "
            "with exactly that body. Finally report completion. The note is a test "
            "receipt, not a product decision. Ask for permission through the normal "
            "tool flow if required."
        )
        task = store._create_task(
            db, project_id, "接入验证 / Connectivity probe", prompt, employee_id, model=model
        )
        db.execute(
            "INSERT INTO employee_probes(task_id,employee_id,project_id,marker,created_at) VALUES(?,?,?,?,?)",
            (task["id"], employee_id, project_id, marker, datetime.now(timezone.utc).isoformat()),
        )
        return {"task": task, "reused": False}


def record_probe_step(store, task_id, run_id, step, body=None):
    if not task_id:
        return
    with store._transaction() as db:
        probe = db.execute("SELECT * FROM employee_probes WHERE task_id=?", (task_id,)).fetchone()
        if probe is None:
            return
        task = store._required(db, "tasks", task_id)
        employee = store._required(db, "employees", task["assignee_id"])
        if (
            task["run_id"] != run_id
            or task["status"] not in ACTIVE
            or task["cancel_requested"]
            or employee["lifecycle"] != "active"
            or not store._membership(db, employee["id"], task["project_id"])
        ):
            raise WorkbenchError("UNAUTHORIZED", "验证会话已结束或权限已撤销。")
        if step == "context":
            db.execute("UPDATE employee_probes SET context_seen=1 WHERE task_id=?", (task_id,))
        elif step == "note" and body == probe["marker"] and probe["context_seen"]:
            db.execute("UPDATE employee_probes SET note_seen=1 WHERE task_id=?", (task_id,))
