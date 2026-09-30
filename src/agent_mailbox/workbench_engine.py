"""Task execution joins durable product state with a replaceable ACP runtime."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from .workbench_runtime import BridgeExecution, context_prompt, workspace_server_command
from .workbench_store import WorkbenchStore


class WorkbenchEngine:
    def __init__(
        self,
        store: WorkbenchStore,
        bridge_command: list[str] | None = None,
        task_timeout: float = 600,
    ):
        self.store = store
        self.execution = BridgeExecution(Path(store.root), bridge_command)
        self.task_timeout = task_timeout
        self.changed = threading.Event()
        self.permission_changed = threading.Condition()
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None
        self.workers: set[threading.Thread] = set()
        self.lock = threading.Lock()
        self.endpoint = ""
        self.on_change = None

    def start(self):
        self.store.recover_runs(self.store.local_node()["id"])
        self.thread = threading.Thread(target=self._loop, name="mailbox-workbench", daemon=True)
        self.thread.start()

    def notify(self):
        self.changed.set()
        with self.permission_changed:
            self.permission_changed.notify_all()
        if self.on_change:
            self.on_change()

    def close(self):
        self.stopping.set()
        self.notify()
        if self.thread:
            self.thread.join(timeout=8)
        with self.lock:
            workers = list(self.workers)
        for worker in workers:
            worker.join(timeout=8)

    def _loop(self):
        while not self.stopping.is_set():
            task = self.store.claim_task(self.store.local_node()["id"])
            if task:
                worker = threading.Thread(target=self._work, args=(task,), daemon=True)
                with self.lock:
                    self.workers.add(worker)
                worker.start()
                continue
            self.changed.wait()
            self.changed.clear()

    def _work(self, task):
        try:
            self.execute(task)
        finally:
            with self.lock:
                self.workers.discard(threading.current_thread())
            self.notify()

    def execute(self, task: dict):
        run_deadline = time.monotonic() + self.task_timeout
        try:
            snapshot = self.store.snapshot()
            project = next(p for p in snapshot["projects"] if p["id"] == task["project_id"])
            employee = next(e for e in snapshot["employees"] if e["id"] == task["assignee_id"])
            task = {**task, "kind": employee["kind"]}
            if self.execution.command is None:
                task["prompt"] = context_prompt(task, self.store.project_context(project["id"]))
            creds = self.store.employee_credentials(employee["id"], project["id"])
            command, args = workspace_server_command()
            mcp_servers = [
                {
                    "name": "agent-mailbox-project",
                    "command": command,
                    "args": args,
                    "env": [
                        {"name": "AGENT_MAIL_HOME", "value": str(self.store.root)},
                        {"name": "AGENT_MAIL_EMPLOYEE", "value": employee["id"]},
                        {"name": "AGENT_MAIL_PROJECT", "value": project["id"]},
                        {"name": "AGENT_MAIL_WORKBENCH_URL", "value": self.endpoint},
                        {"name": "AGENT_MAIL_PROJECT_TOKEN", "value": creds["token"]},
                    ],
                }
            ]

            def cancelled():
                return self.stopping.is_set() or self.store.get_task(task["id"])["cancel_requested"]

            def event(record):
                if record["type"] == "started":
                    self.store.set_status(task["id"], "running")
                # Keep thoughts/tool transcripts out of the concise public event message.
                messages = {
                    "session": "Employee session connected.",
                    "started": "Employee started working.",
                    "event": "Employee activity recorded.",
                }
                self.store.add_event(
                    task["id"],
                    record["type"],
                    messages.get(record["type"], "Execution event."),
                    record,
                )

                if self.on_change and (
                    record["type"] != "event" or record.get("event", {}).get("type") != "text_delta"
                ):
                    self.on_change()

            def permission(record):
                self.store.request_permission(
                    task["id"],
                    record["request_id"],
                    record.get("options", []),
                    record.get("tool_call", {}),
                )
                if self.on_change:
                    self.on_change()
                timeout_ms = record.get("timeout_ms", 120000)
                if (
                    not isinstance(timeout_ms, int)
                    or isinstance(timeout_ms, bool)
                    or timeout_ms <= 0
                ):
                    self.store.expire_permission(task["id"], record["request_id"], task["run_id"])
                    return "deny"
                deadline = min(run_deadline, time.monotonic() + min(120, timeout_ms / 1000))
                # Hold the condition across the DB read so a human decision cannot
                # be lost between observing pending and registering the waiter.
                with self.permission_changed:
                    while not cancelled():
                        value = self.store.permission_decision(
                            task["id"], record["request_id"], task["run_id"]
                        )
                        if value["status"] != "pending":
                            return value.get("decision") or "deny"
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        self.permission_changed.wait(timeout=remaining)
                self.store.expire_permission(task["id"], record["request_id"], task["run_id"])
                return "deny"

            result = self.execution.run(
                task, project, mcp_servers, event, permission, cancelled, timeout=self.task_timeout
            )
            status = {"completed": "review", "cancelled": "cancelled"}.get(
                result.get("status"), "failed"
            )
            self.store.finish_task(
                task["id"], status, result.get("output_text", ""), result.get("error")
            )
            self.store.update_employee(
                employee["id"],
                "available" if status == "review" else "unknown",
                "Execution verified."
                if status == "review"
                else "Inspect the task for execution details.",
            )
        except Exception as exc:  # noqa: BLE001 - execution must leave a terminal state
            self.store.finish_task(
                task["id"], "failed", error={"code": "EXECUTION_ERROR", "message": str(exc)}
            )
