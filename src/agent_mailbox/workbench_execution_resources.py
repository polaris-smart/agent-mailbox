"""Bind project briefings to the resource versions frozen for a task run."""

from __future__ import annotations


def execution_project_context(store, project_id, employee_id, task_id="", run_id=""):
    context = store.project_context(project_id, employee_id=employee_id)
    if task_id:
        context["resource_manifest"] = store.execution_resource_manifest(task_id, run_id)
        context["resource_manifest"]["scope"] = "task_run"
        context["resource_manifest"]["task_id"] = task_id
        context["resource_manifest"]["run_id"] = run_id
        with store._transaction(readonly=True) as db:
            workspace = db.execute(
                "SELECT path,base_commit FROM task_workspaces WHERE task_id=?", (task_id,)
            ).fetchone()
        if workspace:
            context["project"]["path"] = workspace["path"]
            context["workspace"] = {"isolated": True, "base_commit": workspace["base_commit"]}
    return context


def project_task_delivery(store, project_id, target_task_id):
    """Project members may read frozen colleague delivery, never apply it."""
    from .workbench_store import WorkbenchError
    from .workbench_workspaces import task_delivery

    target = store.get_task(target_task_id)
    if target["project_id"] != project_id:
        raise WorkbenchError("UNAUTHORIZED", "交付记录不属于当前授权项目。")
    result = task_delivery(store, target_task_id)
    result.pop("can_apply", None)
    result.pop("apply_blocked_reason", None)
    if result.get("workspace"):
        result["workspace"] = {"base_commit": result["workspace"]["base_commit"], "isolated": True}
    return {"task_id": target_task_id, "task_status": target["status"], **result}
