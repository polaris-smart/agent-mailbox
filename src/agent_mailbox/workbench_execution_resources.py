"""Bind project briefings to the resource versions frozen for a task run."""

from __future__ import annotations


def execution_project_context(store, project_id, employee_id, task_id="", run_id=""):
    context = store.project_context(project_id, employee_id=employee_id)
    if task_id:
        context["resource_manifest"] = store.execution_resource_manifest(task_id, run_id)
        context["resource_manifest"]["scope"] = "task_run"
        context["resource_manifest"]["task_id"] = task_id
        context["resource_manifest"]["run_id"] = run_id
    return context
