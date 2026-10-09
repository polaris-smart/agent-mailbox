"""Task execution joins durable product state with a replaceable ACP runtime."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .workbench_execution_resources import execution_project_context
from .workbench_runtime import BridgeExecution, context_prompt, workspace_server_command
from .workbench_store import WorkbenchError, WorkbenchStore
from .workbench_workspaces import capture_delivery, prepare_workspace

# 只读工具：不值得占用人的一次批准（写操作仍然必须人批）。
# **只看工具名的精确末段**——员工侧 ACP 元数据（kind/title）可被自定义工具伪造：
# 实测 `{'name':'shell','kind':'read'}` 与 `{'name':'project_resource_read_write'}` 都曾命中
# 子串判据而被静默放行（第 10 轮审查 [高]）。因此：不做子串、不折叠大小写、不看 title。
MCP_SERVER_PREFIX = "agent-mailbox"
MCP_READ_TOOLS = frozenset(
    {
        "project_context",
        "project_messages",
        "project_tasks",
        "project_delivery",
        "project_memory_search",
        "project_resource_read",
        "project_resource_versions",
    }
)
BUILTIN_READ_TOOLS = frozenset({"Read", "Glob", "Grep", "LS"})


def read_only_tool_call(tool_call: dict) -> bool:
    """这个工具调用是否只读（只读 ⇒ 引擎可直接放行，不必打断人）。

    判据刻意保守，**只看工具名的完整形状**（不看员工自报的 kind/title ✗）：
    * 我们自己的只读 MCP 工具 ⇒ 名字必须是 ``mcp__<agent-mailbox 前缀>__<白名单工具>``
      **恰好三段** —— 第 10 轮修复只取末段，异命名空间 ``mcp__x__project_context``
      仍被放行（第二轮评审 eng-verify 实测 ✗），这里补上；
    * 内置读工具 ⇒ **精确名**（大小写敏感）+ ``kind == "read"`` 双命中，且名字**不含** ``__``
      （带命名空间的 ``mcp__x__Read`` 不允许走这条 ✗）。
    """
    if not isinstance(tool_call, dict):
        return False
    raw = tool_call.get("name")
    if not isinstance(raw, str) or not raw:
        return False
    if raw.startswith("mcp__"):
        parts = raw.split("__")
        if len(parts) != 3:  # mcp__server__tool 恰好三段
            return False
        server, tool = parts[1], parts[2]
        return server.startswith(MCP_SERVER_PREFIX) and tool in MCP_READ_TOOLS
    return raw in BUILTIN_READ_TOOLS and str(tool_call.get("kind") or "") == "read"


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

    def _record_delivery_proof(
        self, task_id: str, candidates: set[str], run_started: float
    ) -> None:
        """T12：执行成功后，把本次**真的写出来的**文件记成交付证明。

        此前引擎只 `capture_delivery`（落 task_deliveries），不记 `delivery_proof` 事件，
        于是账本永远「证明=无」——验收只能靠肉眼，PRD §5「不看聊天记录，只看交接单+证据」不成立。
        识别口径：运行期工具调用里出现过的绝对路径 + 文件在本次运行期间被写过（mtime 判据），
        并排除 agent 自己的内部目录（plan/config）。证明失败**不**把已完成的活判失败。
        """
        from . import workbench_proof

        # git 级交付清单优先：capture_delivery 已算出文件列表，能覆盖 shell 写出的产出物；
        # tool_call 只带 file_path/path，shell 的 rawInput 只有 command（第 10 轮 [中] ①-3）。
        try:
            with self.store._transaction(readonly=True) as db:
                row = db.execute(
                    "SELECT payload FROM task_deliveries WHERE task_id=?", (task_id,)
                ).fetchone()
            raw_payload = row["payload"] if row is not None else None
            payload = (
                json.loads(raw_payload) if isinstance(raw_payload, str) else (raw_payload or {})
            )
            delivery_files = [
                item.get("path")
                for item in (payload or {}).get("files") or ()
                if isinstance(item, dict) and isinstance(item.get("path"), str)
            ]
        except Exception:  # noqa: BLE001 — 读不到就退回 tool_call 口径
            delivery_files = []
        for name in delivery_files:
            if name.startswith("/"):
                candidates.add(name)

        try:
            existing = workbench_proof.latest_proof(self.store, task_id)
        except Exception:  # noqa: BLE001 — 读失败按"未知"处理
            existing = None
        if existing is not None:
            return  # 已证过就不重复记
        ignored = [
            (Path.home() / name).resolve()
            for name in (
                ".claude",
                ".codex",
                ".zcode",
                ".qoder",
                ".hermes",
                ".config",
                ".cache",
                ".npm",
            )
        ]
        artifacts: list[str] = []
        for raw in sorted(candidates):
            try:
                resolved = Path(raw).resolve()  # 先归一化：`xx/../.claude/...` 才挡得住
                info = resolved.stat()
            except OSError:
                continue
            if not resolved.is_file():
                continue
            if any(base == resolved or base in resolved.parents for base in ignored):
                continue
            if info.st_mtime < run_started - 1:  # 只认本次运行期间写出的
                continue
            artifacts.append(str(resolved))
        if not artifacts:
            return  # 没有产出物就不记空证明（空证明=incomplete）
        try:
            workbench_proof.build_proof(self.store, task_id, artifacts=artifacts)
            self.store.add_event(
                task_id, "proof_recorded", f"交付证明已记录：{len(artifacts)} 个产出物。"
            )
        except Exception as exc:  # noqa: BLE001
            self.store.add_event(task_id, "proof_failed", f"交付证明未能记录：{exc}")

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
            project = prepare_workspace(self.store, task, project)
            if self.execution.command is None:
                task["prompt"] = context_prompt(
                    task,
                    execution_project_context(
                        self.store, project["id"], task["assignee_id"], task["id"], task["run_id"]
                    ),
                )
            creds = self.store.execution_credentials(task["id"])
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
                        {"name": "AGENT_MAIL_TASK", "value": task["id"]},
                        {"name": "AGENT_MAIL_RUN", "value": task["run_id"]},
                    ],
                }
            ]

            def cancelled():
                return self.stopping.is_set() or self.store.get_task(task["id"])["cancel_requested"]

            run_started = time.time()
            candidates: set[str] = set()

            def event(record):
                if record["type"] == "started":
                    self.store.set_status(task["id"], "running")
                if record["type"] == "event":
                    inner = record.get("event") or {}
                    if str(inner.get("tag", "")).startswith("tool_call"):
                        raw = inner.get("rawInput")
                        if isinstance(raw, dict):
                            for key in ("file_path", "filePath", "path", "filename"):
                                value = raw.get(key)
                                if isinstance(value, str) and value.startswith("/"):
                                    candidates.add(value)
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
                tool_call = record.get("tool_call") or {}
                options = list(record.get("options") or [])
                # 补一个「本任务内允许」：一次批准覆盖同一 run 的后续请求，
                # 否则一个小活能打断人六七次（真机实测）。
                if not any(
                    isinstance(option, dict) and option.get("kind") == "allow_run"
                    for option in options
                ):
                    options = [
                        *options,
                        {"optionId": "allow-run", "name": "本任务内允许", "kind": "allow_run"},
                    ]
                self.store.request_permission(task["id"], record["request_id"], options, tool_call)
                if self.on_change:
                    self.on_change()
                # 只读工具、或本 run 已授权「本任务内允许」⇒ 直接放行。
                # 仍然留痕：request_permission + resolve_permission 各写一条事件。
                if read_only_tool_call(tool_call) or self.store.run_has_allow_run(
                    task["id"], task["run_id"]
                ):
                    try:
                        self.store.resolve_permission(
                            task["id"], record["request_id"], "allow_once"
                        )
                    except WorkbenchError:
                        return "deny"
                    if self.on_change:
                        self.on_change()
                    return "allow_once"
                timeout_ms = record.get("timeout_ms", 120000)
                if (
                    not isinstance(timeout_ms, int)
                    or isinstance(timeout_ms, bool)
                    or timeout_ms <= 0
                ):
                    self.store.expire_permission(task["id"], record["request_id"], task["run_id"])
                    return "deny"
                duration = min(120, timeout_ms / 1000)
                # Return the default denial before the native request expires,
                # leaving a small transport allowance without extending consent.
                deadline = min(run_deadline, time.monotonic() + duration - min(0.2, duration / 10))
                # Hold the condition across the DB read so a human decision cannot
                # be lost between observing pending and registering the waiter.
                with self.permission_changed:
                    while not cancelled():
                        value = self.store.permission_decision(
                            task["id"], record["request_id"], task["run_id"]
                        )
                        if value["status"] != "pending":
                            decision = value.get("decision") or "deny"
                            # 「本任务内允许」对桥只表现为一次允许；后续请求由引擎按库自动放行
                            return "allow_once" if decision == "allow_run" else decision
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
            delivery = capture_delivery(self.store, task, project, result)
            if delivery.get("capture_error") and status == "review":
                status = "failed"
                result["error"] = {
                    "code": "DELIVERY_CAPTURE_FAILED",
                    "message": "交付内容未能完整捕获，请查看工作区和交付详情。",
                }
            saved = self.store.finish_task(
                task["id"], status, result.get("output_text", ""), result.get("error")
            )
            status = saved["status"]
            if status == "review":
                self._record_delivery_proof(task["id"], candidates, run_started)
            self.store.update_employee(
                employee["id"],
                "available"
                if status == "review"
                else "auth_required"
                if (result.get("error") or {}).get("code") == "AUTH_REQUIRED"
                else "unknown",
                "Execution verified."
                if status == "review"
                else "The managed execution adapter requires authentication; native sign-in alone is not execution verification."
                if (result.get("error") or {}).get("code") == "AUTH_REQUIRED"
                else "Inspect the task for execution details.",
            )
        except Exception as exc:  # noqa: BLE001 - execution must leave a terminal state
            self.store.finish_task(
                task["id"],
                "failed",
                error={
                    "code": exc.code if isinstance(exc, WorkbenchError) else "EXECUTION_ERROR",
                    "message": str(exc),
                },
            )
