"""An explicitly paired device executes only tasks for mapped local projects."""

from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime, timezone

from .workbench_fleet import _load, _write_private, permission_summary
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
        self.lock = getattr(client, "journal_lock", threading.RLock())
        self.errors = {}
        self.active_path = client.directory / "active-runs.json"
        self.active = {}
        client.claimed_runs = self.active
        self.outbox_path = client.directory / "terminal-receipts.json"
        self.outbox = _load(self.outbox_path, {})
        if not isinstance(self.outbox, dict):
            raise WorkbenchError("storage_error", "任务回执记录格式无效，请恢复备份。")
        self.receipt_lock = threading.Lock()
        self.receipt_ready = threading.Event()
        self.receipt_thread = None

    def _queue_receipt(self, task, status, result="", error=None):
        # Write the result before any network operation. Reconnect/restart must
        # resend this exact receipt, never replay the model or downgrade output.
        with self.lock:
            self.outbox[task["id"]] = {
                "run_id": task["run_id"],
                "status": status,
                "result": result,
                "error": error,
            }
            try:
                _write_private(self.outbox_path, json.dumps(self.outbox).encode())
            except WorkbenchError as exc:
                self.errors[task["id"]] = "最终结果尚未落盘：" + exc.message
                self.receipt_ready.set()
                return
        self.receipt_ready.set()
        self._flush_receipts()

    def _flush_receipts(self):
        with self.receipt_lock:
            with self.lock:
                pending = dict(self.outbox)
                if pending:
                    try:
                        # A previous queue write may have failed. Preserve the
                        # actual result in memory and retry storage before HTTPS.
                        _write_private(self.outbox_path, json.dumps(pending).encode())
                    except WorkbenchError as exc:
                        for task_id in pending:
                            self.errors[task_id] = "最终结果尚未落盘：" + exc.message
                        return
            for task_id, receipt in pending.items():
                try:
                    self.client.receipt(task_id, **receipt)
                except WorkbenchError as exc:
                    # Keep even rejected receipts locally for diagnosis. Scope
                    # revocation or a changed run must not discard a deliverable.
                    self.errors[task_id] = "最终回执尚未确认：" + exc.message
                    continue
                with self.lock:
                    if self.outbox.get(task_id) != receipt:
                        continue
                    next_active = {
                        key: value for key, value in self.active.items() if key != task_id
                    }
                    next_outbox = {
                        key: value for key, value in self.outbox.items() if key != task_id
                    }
                    try:
                        # Commit cleanup to disk before mutating memory. If the
                        # second write fails the durable outbox still contains
                        # the result, even if the active journal is already clear.
                        _write_private(self.active_path, json.dumps(next_active).encode())
                        _write_private(self.outbox_path, json.dumps(next_outbox).encode())
                    except WorkbenchError as exc:
                        self.errors[task_id] = "主控已确认，本机回执清理未完成：" + exc.message
                        continue
                    self.active.clear()
                    self.active.update(next_active)
                    self.outbox = next_outbox
                    self.errors.pop(task_id, None)

    def _retry_receipts(self):
        while not self.stopping.is_set():
            self.receipt_ready.wait(5)
            self.receipt_ready.clear()
            if not self.stopping.is_set():
                self._flush_receipts()
            if self.outbox:
                self.stopping.wait(5)  # Retry a final acknowledgement, no model replay.

    def _save(self):
        _write_private(self.active_path, json.dumps(self.active).encode())

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
            self._flush_receipts()
            previous = dict(self.active)
            for task_id, run_id in previous.items():
                if task_id in self.outbox:
                    continue
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
            self.receipt_thread = threading.Thread(target=self._retry_receipts, daemon=True)
            self.receipt_thread.start()
        thread = threading.Thread(target=self._loop, args=(project_id,), daemon=True)
        thread.project_id = project_id
        self.threads.append(thread)
        thread.start()

    def _loop(self, project_id):
        if hasattr(self.client, "claim_cancellation"):
            self.client.claim_cancellation.event = self.stopping
        while not self.stopping.is_set():
            try:
                task = self.client.claim(project_id, wait=30)
                self.errors.pop(project_id, None)
                if task is None and self.client.store.update_maintenance()["paused"]:
                    if self.client.store.update_maintenance()["pending_claims"]:
                        raise WorkbenchError(
                            "claim_unconfirmed", "更新暂停期间正在确认此前未收到的任务领取。"
                        )
                    self.stopping.wait(1)
                if task and self.stopping.is_set():
                    self._queue_receipt(
                        task,
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
                self._queue_receipt(task, "cancelled")
                return
            project = self.client.local_project(task["project_id"])
            if self.execution.command is None:
                task = {
                    **task,
                    "prompt": context_prompt(
                        task,
                        self.client.project_tool(
                            task["project_id"],
                            task["assignee_id"],
                            "context",
                            {},
                            task_id=task["id"],
                            run_id=task["run_id"],
                        ),
                    ),
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
                        {"name": "AGENT_MAIL_TASK", "value": task["id"]},
                        {"name": "AGENT_MAIL_RUN", "value": task["run_id"]},
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
                    duration = min(120, timeout_ms / 1000)
                    deadline = min(
                        permission_started + duration - min(0.2, duration / 10),
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
            self._queue_receipt(task, status, result.get("output_text", ""), result.get("error"))
        except Exception as exc:  # noqa: BLE001 - a remote execution must produce an explicit failure
            with self.lock:
                recorded = task["id"] in self.outbox
            if recorded:
                self.errors[task["id"]] = "最终回执尚未确认。"
            else:
                try:
                    self._queue_receipt(
                        task,
                        "failed",
                        error={"code": "REMOTE_EXECUTION_ERROR", "message": str(exc)},
                    )
                except (WorkbenchError, OSError):
                    self.errors[task["id"]] = "无法保存最终回执，请检查本地磁盘。"
        finally:
            finished.set()
            with self.lock:
                self.workers.discard(threading.current_thread())
                # Keep unacknowledged runs for explicit restart recovery.
                if task["id"] not in self.errors and task["id"] not in self.outbox:
                    self.active.pop(task["id"], None)
                    self._save()

    def close(self):
        self.stopping.set()
        if hasattr(self.client, "interrupt_claim"):
            self.client.interrupt_claim()
        self.receipt_ready.set()
        if self.receipt_thread:
            self.receipt_thread.join(timeout=0.2)
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
                "pending_receipts": len(self.outbox),
                "errors": dict(self.errors),
            }
