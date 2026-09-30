"""An explicitly paired device executes only tasks for mapped local projects."""

from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime, timezone

from .workbench_fleet import permission_summary
from .workbench_runtime import BridgeExecution, context_prompt, workspace_server_command
from .workbench_store import WorkbenchError


class RemoteWorker:
    def __init__(self, client, bridge_command=None, task_timeout=600):
        self.client = client
        self.execution = BridgeExecution(client.store.root, bridge_command)
        self.task_timeout = task_timeout
        self.stopping = threading.Event()
        self.threads = []
        self.workers = set()
        self.lock = threading.Lock()
        self.errors = {}
        self.active_path = client.directory / "active-runs.json"
        self.active = {}

    def _save(self):
        data = json.dumps(self.active).encode()
        temporary = self.active_path.with_suffix(".tmp")
        temporary.write_bytes(data)
        temporary.chmod(0o600)
        temporary.replace(self.active_path)

    def start_project(self, project_id):
        self.client.local_project(project_id)
        if any(getattr(t, "project_id", None) == project_id for t in self.threads):
            return
        if not self.threads:
            if self.active_path.exists():
                self.active.update(json.loads(self.active_path.read_text()))
            try:
                for task in self.client.active_runs():
                    self.active.setdefault(task["id"], task["run_id"])
            except WorkbenchError as exc:
                self.errors["recovery"] = exc.message
            previous = dict(self.active)
            for task_id, run_id in previous.items():
                try:
                    self.client.receipt(
                        task_id,
                        run_id,
                        "interrupted",
                        error={
                            "code": "RUN_INTERRUPTED",
                            "message": "The device restarted. This task will not run again automatically.",
                        },
                    )
                    self.active.pop(task_id, None)
                except WorkbenchError as exc:
                    if exc.code == "invalid_state":
                        self.active.pop(task_id, None)
                    else:
                        self.errors["recovery"] = exc.message
            self._save()
        thread = threading.Thread(target=self._loop, args=(project_id,), daemon=True)
        thread.project_id = project_id
        self.threads.append(thread)
        thread.start()

    def _loop(self, project_id):
        while not self.stopping.is_set():
            try:
                task = self.client.claim(project_id, wait=30)
                self.errors.pop(project_id, None)
                if task and self.stopping.is_set():
                    self.client.receipt(
                        task["id"],
                        task["run_id"],
                        "interrupted",
                        error={
                            "code": "DEVICE_STOPPED",
                            "message": "This device stopped before execution.",
                        },
                    )
                    break
                if task:
                    with self.lock:
                        self.active[task["id"]] = task["run_id"]
                        self._save()
                        worker = threading.Thread(target=self._execute, args=(task,), daemon=True)
                        self.workers.add(worker)
                    worker.start()
            except (WorkbenchError, OSError, ValueError) as exc:
                self.errors[project_id] = str(exc)
                # Recover a lost claim response without repeating uncertain work.
                try:
                    for pending in self.client.active_runs():
                        with self.lock:
                            owned = pending["id"] in self.active
                        if (
                            not owned
                            and pending["status"] == "starting"
                            and pending["project_id"] == project_id
                        ):
                            self.client.receipt(
                                pending["id"],
                                pending["run_id"],
                                "interrupted",
                                error={
                                    "code": "CLAIM_UNCONFIRMED",
                                    "message": "This device could not confirm task delivery; execution was not repeated.",
                                },
                            )
                except WorkbenchError:
                    pass
                self.stopping.wait(5)  # Connection recovery, not a mailbox scan.

    def _execute(self, task):
        cancelled = threading.Event()
        finished = threading.Event()
        run_deadline = time.monotonic() + self.task_timeout

        def control():
            while not finished.is_set() and not self.stopping.is_set():
                try:
                    state = self.client.control(task["id"], task["run_id"], wait=30)
                    if state["cancel_requested"] or state["status"] not in {
                        "starting",
                        "running",
                        "waiting_approval",
                    }:
                        cancelled.set()
                        return
                except WorkbenchError:
                    cancelled.set()
                    return

        thread = threading.Thread(target=control, daemon=True)
        thread.start()
        try:
            state = self.client.control(task["id"], task["run_id"], wait=0)
            if state["cancel_requested"] or state["status"] not in {
                "starting",
                "running",
                "waiting_approval",
            }:
                self.client.receipt(task["id"], task["run_id"], "cancelled")
                return
            project = self.client.local_project(task["project_id"])
            if self.execution.command is None:
                task = {
                    **task,
                    "prompt": context_prompt(task, self.client.context(task["project_id"])),
                }
            command, args = workspace_server_command()
            servers = [
                {
                    "name": "agent-mailbox-project",
                    "command": command,
                    "args": args,
                    "env": [
                        {"name": "AGENT_MAIL_HOME", "value": str(self.client.store.root)},
                        {"name": "AGENT_MAIL_EMPLOYEE", "value": task["assignee_id"]},
                        {"name": "AGENT_MAIL_PROJECT", "value": task["project_id"]},
                        {"name": "AGENT_MAIL_FLEET", "value": "1"},
                    ],
                }
            ]

            def event(record):
                self.client.event(
                    task["id"], task["run_id"], record["type"], "Remote employee activity.", record
                )

            permission_ids = set()

            def permission(record):
                # Never forward the bridge's full ACP request/rawInput/content.
                permission_started = time.monotonic()
                try:
                    options, tool_call = permission_summary(
                        record.get("options"), record.get("tool_call")
                    )
                    request_id = record["request_id"]
                    if request_id in permission_ids:
                        return "deny"
                    permission_ids.add(request_id)
                    if cancelled.is_set() or self.stopping.is_set():
                        return "deny"
                    timeout_ms = record.get("timeout_ms", 120000)
                    if (
                        not isinstance(timeout_ms, int)
                        or isinstance(timeout_ms, bool)
                        or timeout_ms <= 0
                    ):
                        return "deny"
                    wait = max(0, min(120, math.ceil(timeout_ms / 1000)))
                    requested = self.client.request_permission(
                        task["id"],
                        task["run_id"],
                        task["assignee_id"],
                        request_id,
                        options,
                        tool_call,
                    )
                    expires = datetime.fromisoformat(requested["expires_at"].replace("Z", "+00:00"))
                    deadline = min(
                        permission_started + min(120, timeout_ms / 1000),
                        run_deadline,
                        time.monotonic() + (expires - datetime.now(timezone.utc)).total_seconds(),
                    )
                    wait = max(0, min(wait, math.ceil(deadline - time.monotonic())))
                    answered = threading.Event()
                    answer = {}

                    def expire():
                        try:
                            self.client.expire_permission(
                                task["id"], task["run_id"], task["assignee_id"], request_id
                            )
                        except WorkbenchError:
                            pass  # No connection can ever grant permission.

                    def await_decision():
                        try:
                            answer.update(
                                self.client.permission_decision(
                                    task["id"],
                                    task["run_id"],
                                    task["assignee_id"],
                                    request_id,
                                    wait,
                                )
                            )
                        except WorkbenchError:
                            answer["decision"] = "deny"
                        finally:
                            if answer.get("status") != "resolved":
                                expire()
                            answered.set()

                    threading.Thread(target=await_decision, daemon=True).start()
                    # Only local cancellation events are checked here; a single
                    # HTTPS request waits on the owner's condition, not DB polling.
                    while not answered.wait(0.1):
                        if (
                            cancelled.is_set()
                            or self.stopping.is_set()
                            or time.monotonic() >= deadline
                        ):
                            threading.Thread(target=expire, daemon=True).start()
                            return "deny"
                    decision = answer.get("decision", "deny")
                    if (
                        cancelled.is_set()
                        or self.stopping.is_set()
                        or time.monotonic() >= deadline
                        or answer.get("status") != "resolved"
                        or not any(option["kind"] == "allow_once" for option in options)
                    ):
                        if time.monotonic() >= deadline:
                            threading.Thread(target=expire, daemon=True).start()
                        return "deny"
                    return "allow_once" if decision == "allow_once" else "deny"
                except (WorkbenchError, KeyError, TypeError, ValueError):
                    return "deny"

            result = self.execution.run(
                task,
                project,
                servers,
                event,
                permission,
                lambda: cancelled.is_set() or self.stopping.is_set(),
                self.task_timeout,
            )
            status = {"completed": "review", "cancelled": "cancelled"}.get(
                result.get("status"), "failed"
            )
            state = self.client.control(task["id"], task["run_id"], wait=0)
            if state["cancel_requested"] or cancelled.is_set() or self.stopping.is_set():
                status = "cancelled"
            self.client.receipt(
                task["id"],
                task["run_id"],
                status,
                result.get("output_text", ""),
                result.get("error"),
            )
        except Exception as exc:  # noqa: BLE001 - a remote execution must produce an explicit failure
            try:
                self.client.receipt(
                    task["id"],
                    task["run_id"],
                    "failed",
                    error={"code": "REMOTE_EXECUTION_ERROR", "message": str(exc)},
                )
            except WorkbenchError:
                self.errors[task["id"]] = "The coordinator did not acknowledge the final result."
        finally:
            finished.set()
            with self.lock:
                self.workers.discard(threading.current_thread())
                # Keep unacknowledged runs for explicit restart recovery.
                if task["id"] not in self.errors:
                    self.active.pop(task["id"], None)
                    self._save()

    def close(self):
        self.stopping.set()
        with self.lock:
            workers = list(self.workers)
        for worker in workers:
            worker.join(timeout=8)
        for thread in self.threads:
            thread.join(timeout=0.2)

    def status(self):
        with self.lock:
            return {
                "projects": list(self.client.mappings),
                "active": len(self.workers),
                "errors": dict(self.errors),
            }
