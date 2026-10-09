"""Human workbench: authenticated loopback UI and durable task execution."""

from __future__ import annotations

import argparse
import hmac
import importlib
import ipaddress
import json
import os
import pathlib
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .workbench_activity import project_activity
from .workbench_compatibility import compatibility_nodes
from .workbench_engine import WorkbenchEngine
from .workbench_lock import WorkbenchLock
from .workbench_onboarding import create_probe, onboarding_status
from .workbench_private import private_mode
from .workbench_runtime import available_models, discover_employees, runtime_status
from .workbench_store import WorkbenchError, WorkbenchStore
from .workbench_updates import UpdateService
from .workbench_workspaces import apply_delivery, task_delivery

ASSETS = Path(__file__).parent / "workbench_assets"
PREFIX = "/api/workbench"
VERSION = __version__


def build_info(store) -> dict:
    """构建溯源 ✓（Codex AM-06：修复**到不了用户**的根因是"安装的东西 ≠ 源码"✗）。

    绑定四件事 ✓，让"关于页/证据"能说清**你打开的到底是哪份代码** ✗：
      · `version` + `revision`（仓库里取 git 短 sha ✓ 打包后若有 `build-info.json` 则以它为准 ✓）
      · `assets`：界面资源的 SHA256（前 16 位 ✓）—— 与源码/安装包**逐字节**可比 ✓
      · `home` + `schema`：数据目录与库结构版本 ✓（升级验收要靠这两个 ✓）
    """
    import hashlib

    assets_dir = pathlib.Path(__file__).with_name("workbench_assets")
    info: dict = {"version": VERSION, "revision": None, "built_at": None, "assets": {}}
    # 闸门②（规范 §三）✓：我跑的是**成品**还是**工作树**✗ —— 今晚就是这一点坑了一整晚 ✓
    shipped = assets_dir / "build-info.json"
    if shipped.is_file():
        try:
            info.update(json.loads(shipped.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    if not info.get("revision"):
        try:
            done = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=pathlib.Path(__file__).parents[2],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            info["revision"] = done.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            info["revision"] = None
    hashes = {}
    for name in ("workbench.js", "index.html", "workbench.css"):
        candidate = assets_dir / name
        if candidate.is_file():
            hashes[name] = hashlib.sha256(candidate.read_bytes()).hexdigest()[:16]
    info["assets"] = {**info.get("assets", {}), **hashes}
    try:
        from .workbench_store import SCHEMA_VERSION

        info["schema"] = SCHEMA_VERSION
    except ImportError:
        info["schema"] = None
    info["home"] = str(store.root)
    # **AM-06 运行来源（三事实 ✓ Codex 口径）** ✗：`build-info.json` **只说明构建来源** ✓
    # 绝不单独拿它决定"运行形态" ✗（它进了源码树后，"文件存在"就没有区分力了 ✓）
    code_path = str(pathlib.Path(__file__).resolve())
    info["runtime"] = {
        "executable": sys.executable,
        "argv": list(sys.argv),
        "code_path": code_path,
        "frozen": bool(getattr(sys, "frozen", False)),
    }
    # 形态**只在代码路径能判定时**给出 ✓ 否则「未确认」✗（证据不足不做二分 ✗）
    if getattr(sys, "frozen", False) or ".app/Contents/" in code_path:
        info["mode"] = "packaged"
    elif "/src/agent_mailbox/" in code_path:
        info["mode"] = "source"
    else:
        info["mode"] = "unknown"  # 未确认 ✓
    info.setdefault("dirty", None)  # 成品包由构建写入 ✓ 开发态为 None ✓
    return info


def pick_project():
    if sys.platform != "darwin":
        raise WorkbenchError(
            "PICKER_UNAVAILABLE", "Enter the project folder path on this platform."
        )
    try:
        result = subprocess.run(
            [
                "osascript",
                "-e",
                'POSIX path of (choose folder with prompt "Choose your project folder")',
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkbenchError("PICKER_FAILED", "The folder picker was unavailable.") from exc
    if result.returncode:
        raise WorkbenchError("PICKER_CANCELLED", "No project folder was selected.")
    return {"path": result.stdout.strip()}


def t_manual_detail(entrypoint: str) -> str:
    """手工登记时的说明文案（**如实** ✓：未被自动发现 ⇒ 执行未验证 ✗ 不假装已验证 ✓）。"""
    return f"手工登记（未被自动发现）· 入口：{entrypoint} · 是否可自动执行未验证"


def wake_redeem_loop(store, home, *, interval: float = 5.0, stop: object | None = None) -> None:
    """应用侧**兑现**：任何来源的新信 ⇒ 发现 ⇒ 叫醒 ✓（这就是"开着应用就自动"✓ 零配置 ✓）。

    · 低频（默认 5s ✓）· 只读判定 ✓ · 闸门全在 `run_once` 里（认领/水位线/冷却/上限 ✓）
    · 安静失败 ✓：任何异常都不许把工作台带崩 ✗（下一轮再来 ✓）
    """
    import time as _time

    from .workbench_wake import (
        active_employee_ids,
        hook_deliver,
        run_once,
        state_lock,
        unread_ids_for_employee,
    )

    while stop is None or not getattr(stop, "is_set", lambda: False)():
        try:
            state_home = home
            with state_lock(state_home):
                pass
            result = run_once(
                store,
                state_home,
                unread_provider=unread_ids_for_employee,
                deliver=hook_deliver(state_home),
                employee_ids=active_employee_ids(store),
                # **AM-03 修复** ✗：**不替持久状态做决定** ✓
                # （原来这里固定 `cold_started=True` ⇒ 空状态首启会**跳过冷启动归零** ✗
                #   把历史未读当新信投递 ✓ Codex 实测 `cold_done=null` 且投递一次 ✓
                #   ⇒ 现在交给 `_run_once_locked`：state 里没有 `cold_done` ⇒ 这一轮就是冷启动 ✓
                #     先记「已见」不叫人 ✓ 并把 `cold_done=True` 持久化 ✓ 之后各轮才真正兑现 ✓）
                cold_started=False,
            )
            if result.get("woke"):
                print(f"[wake] 已叫醒 {result['woke']}")
        except Exception as exc:  # noqa: BLE001 —— 后台线程必须**绝不**带崩工作台 ✓
            print(f"[wake] 本轮跳过（{type(exc).__name__}: {str(exc)[:80]}）")
        if stop is not None and getattr(stop, "wait", None):
            stop.wait(interval)
        else:
            _time.sleep(interval)


class WorkbenchHTTP(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, store, port=0, engine=None, token=None):
        self.store = store
        self.ui_condition = threading.Condition()
        self.ui_revision = 0
        self.instance_lock = WorkbenchLock(store.root)
        self.engine = engine or WorkbenchEngine(store)
        self.engine.on_change = self.ui_notify
        self.token = token or secrets.token_urlsafe(32)
        self.preview_lock = threading.Lock()
        self.previews = {}
        self.install_lock = threading.Lock()
        self.install_status = None
        self.fleet = None
        self.remote_client = None
        self.remote_worker = None
        self.remote_projects = []
        self.fleet_error = None
        self.updates = UpdateService(self)
        self.closing = threading.Event()
        self.listener_path = store.root / "workbench/fleet-listener.json"
        try:
            super().__init__(("127.0.0.1", port), Handler)
        except OSError:
            self.instance_lock.close()
            raise
        self.endpoint = f"http://127.0.0.1:{self.server_port}"
        self.engine.endpoint = self.endpoint
        self.instance_path = store.root / "workbench/instance.json"
        self.instance_path.write_text(json.dumps({"endpoint": self.endpoint, "token": self.token}))
        private_mode(self.instance_path, 0o600)
        self.restore_connections()

    def server_bind(self):
        # This is a literal loopback listener. HTTPServer's reverse DNS lookup
        # can block startup on machines without a working PTR resolver.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def resource_preview(self, content: str) -> str:
        # Mint a short-lived content capability only after owner-authenticated
        # resource reading. This capability cannot access any other API/data.
        now = time.monotonic()
        with self.preview_lock:
            self.previews = {key: value for key, value in self.previews.items() if value[0] > now}
            if len(self.previews) >= 32:
                self.previews.pop(next(iter(self.previews)))
            nonce = secrets.token_urlsafe(32)
            self.previews[nonce] = (now + 300, content)
        return "/workbench-preview/" + nonce

    def preview_content(self, nonce: str) -> str:
        with self.preview_lock:
            item = self.previews.get(nonce)
            if item is None or item[0] <= time.monotonic():
                self.previews.pop(nonce, None)
                raise WorkbenchError("NOT_FOUND", "Preview expired. Read the resource again.")
            return item[1]

    def close(self):
        self.closing.set()
        self.ui_notify()
        self.engine.close()
        if self.remote_worker:
            self.remote_worker.close()
        if self.fleet:
            self.fleet.stop()
        self.server_close()
        self.instance_path.unlink(missing_ok=True)
        self.instance_lock.close()

    def ui_notify(self):
        with self.ui_condition:
            self.ui_revision += 1
            self.ui_condition.notify_all()

    def notify(self):
        self.engine.notify()
        if self.fleet:
            self.fleet.notify()

    def fleet_status(self):
        return {
            "listener": {"base_url": self.fleet.base_url, "fingerprint": self.fleet.fingerprint}
            if self.fleet
            else None,
            "error": self.fleet_error,
            "remote": {
                "paired": True,
                "projects": self.remote_projects,
                "mappings": dict(self.remote_client.mappings),
                "worker": self.remote_worker.status() if self.remote_worker else None,
            }
            if self.remote_client
            else None,
        }

    def start_fleet(self, address="127.0.0.1", port=0):
        from .workbench_fleet import FleetCoordinator

        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise WorkbenchError(
                "INVALID_ADDRESS", "Enter this device's local network IP address."
            ) from exc
        private_networks = (
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
            ipaddress.ip_network("100.64.0.0/10"),
        )
        if ip.version != 4 or not (
            ip.is_loopback or any(ip in network for network in private_networks)
        ):
            raise WorkbenchError(
                "INVALID_ADDRESS",
                "Choose this device's LAN, private overlay or loopback IPv4 address.",
            )
        if self.fleet:
            raise WorkbenchError("ALREADY_STARTED", "The device listener is already running.")
        fleet = FleetCoordinator(self.store)
        fleet.on_change = self.engine.notify
        result = fleet.start(host=str(ip), port=port, advertised_host=str(ip))
        self.fleet = fleet
        config = {"enabled": True, "address": address, "port": fleet.server.server_port}
        self.listener_path.write_text(json.dumps(config))
        private_mode(self.listener_path, 0o600)
        self.fleet_error = None
        return result

    def restore_connections(self):
        # Restore durable connection settings, never regenerate paired identities.
        if self.listener_path.exists():
            try:
                config = json.loads(self.listener_path.read_text())
                if config.get("enabled"):
                    self.start_fleet(config["address"], config["port"])
            except (OSError, ValueError, KeyError, WorkbenchError) as exc:
                self.fleet_error = {"code": "LISTENER_RESTORE_FAILED", "message": str(exc)}
        credentials = self.store.root / "workbench/fleet/client.json"
        if credentials.exists():

            def restore():
                while not self.closing.is_set():
                    try:
                        self.join_fleet(json.loads(credentials.read_text()))
                        return
                    except (OSError, ValueError, WorkbenchError) as exc:
                        self.fleet_error = {"code": "DEVICE_RESTORE_FAILED", "message": str(exc)}
                        self.closing.wait(5)

            threading.Thread(target=restore, daemon=True).start()

    def join_fleet(self, invite):
        from .workbench_fleet import FleetClient
        from .workbench_remote import RemoteWorker

        client = FleetClient(self.store.root, invite)
        projects = client.projects()
        if self.closing.is_set():
            raise WorkbenchError("WORKBENCH_CLOSED", "The workbench is closing.")
        if self.remote_worker:
            self.remote_worker.close()
        self.remote_client = client
        self.remote_projects = projects
        self.remote_worker = RemoteWorker(client)
        for project_id in client.mappings:
            if project_id in {p["id"] for p in projects}:
                self.remote_worker.start_project(project_id)
        self.fleet_error = None
        return self.fleet_status()

    def install_runtime(self):
        with self.install_lock:
            if self.install_status and self.install_status["status"] == "installing":
                return self.install_status
            self.install_status = {"status": "installing"}

        def install():
            try:
                from .runtime_bridge.install_runtime import install_runtime

                install_runtime(self.store.root / "workbench/runtime/deps")
                self.install_status = {"status": "ready"}
                self.ui_notify()
            except Exception as exc:  # noqa: BLE001 - background boundary reports terminal install failure
                self.install_status = {
                    "status": "failed",
                    "error": {"code": "RUNTIME_INSTALL_FAILED", "message": str(exc)},
                }
                self.ui_notify()

        threading.Thread(target=install, daemon=True).start()
        return {"status": "installing"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # Never log bearer credentials or prompts.

    def send_value(
        self, status, value, content_type="application/json; charset=utf-8", *, csp=None
    ):
        data = (
            json.dumps(value, ensure_ascii=False).encode()
            if isinstance(value, (dict, list))
            else value
        )
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            csp
            or "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        self.end_headers()
        self.wfile.write(data)

    def changes(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        revision = -1
        try:
            while not self.server.closing.is_set():
                with self.server.ui_condition:
                    if revision == self.server.ui_revision:
                        self.server.ui_condition.wait(timeout=15)
                    revision = self.server.ui_revision
                data = (json.dumps({"revision": revision}) + "\n").encode()
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        self.close_connection = True

    def safe_origin(self):
        expected = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if self.headers.get("Host") not in expected:
            return False
        origin = self.headers.get("Origin")
        return not origin or origin in {f"http://{host}" for host in expected}

    def body(self):
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise WorkbenchError("INVALID_REQUEST", "Chunked requests are not supported.")
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 0 or size > 1024 * 1024:
                self.close_connection = True
                raise WorkbenchError("BODY_TOO_LARGE", "Requests must be smaller than 1 MiB.")
            value = json.loads(self.rfile.read(size) or b"{}")
            if not isinstance(value, dict):
                raise TypeError()
            return value
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            self.close_connection = True
            raise WorkbenchError("INVALID_REQUEST", "Expected a JSON object.") from exc

    def handle_request(self):
        if not self.safe_origin():
            self.close_connection = True
            self.send_value(
                403,
                {
                    "error": {
                        "code": "ORIGIN_REJECTED",
                        "message": "Open the local workbench address.",
                    }
                },
            )
            return
        url = urlsplit(self.path)
        path = url.path
        if self.command == "GET" and path.startswith("/workbench-preview/"):
            content = self.server.preview_content(path.removeprefix("/workbench-preview/"))
            self.send_value(
                200,
                content.encode("utf-8"),
                "text/html; charset=utf-8",
                csp="sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                "img-src data:; font-src data:; connect-src 'none'; frame-src 'none'; "
                "form-action 'none'; base-uri 'none'; frame-ancestors 'self'",
            )
            return
        if self.command == "GET" and not path.startswith(PREFIX):
            name = (
                "index.html"
                if path in {"/", "/workbench"}
                else path.removeprefix("/workbench-assets/").lstrip("/")
            )
            allowed = {
                "index.html": "text/html; charset=utf-8",
                "LICENSE.txt": "text/plain; charset=utf-8",
                "NOTICE.txt": "text/plain; charset=utf-8",
                "MIT-Legacy.txt": "text/plain; charset=utf-8",
                "workbench.js": "text/javascript; charset=utf-8",
                "api.js": "text/javascript; charset=utf-8",
                "workbench.css": "text/css; charset=utf-8",
            }
            if name not in allowed:
                self.send_value(404, {"error": {"code": "NOT_FOUND", "message": "Page not found."}})
                return
            self.send_value(200, (ASSETS / name).read_bytes(), allowed[name])
            return
        bearer = self.headers.get("Authorization", "")
        token = bearer[7:] if bearer.startswith("Bearer ") else ""
        data = self.body() if self.command in {"POST", "DELETE"} else {}
        if path == PREFIX + "/notify" and self.command == "POST":
            if not self.server.store.validate_execution(
                token,
                data.get("employee_id", ""),
                data.get("project_id", ""),
                data.get("task_id", ""),
                data.get("run_id", ""),
            ):
                raise WorkbenchError("UNAUTHORIZED", "This employee cannot notify this project.")
            self.server.notify()
            return self.send_value(200, {"status": "acknowledged"})
        if self.command == "POST" and path == PREFIX + "/mailbox/tools":
            from .workbench_mail_sessions import invoke

            if set(data) != {"tool", "args"}:
                raise WorkbenchError("INVALID_REQUEST", "Expected a project tool and arguments.")
            result = invoke(self.server.store, token, data["tool"], data["args"])
            self.server.ui_notify()
            return self.send_value(200, result)
        if not token or not hmac.compare_digest(token, self.server.token):
            self.send_value(
                401,
                {
                    "error": {
                        "code": "UNAUTHORIZED",
                        "message": "Open the workbench from the application.",
                    }
                },
            )
            return
        store = self.server.store
        route = path.removeprefix(PREFIX).strip("/").split("/")
        method = self.command
        result = None
        if method == "POST" and route == ["mail-tasks"]:
            from .workbench_mail_tasks import create_mail_task

            result = create_mail_task(
                store, data["project_id"], data["title"], data["prompt"], data["assignee_id"]
            )
            self.server.ui_notify()
            return self.send_value(200, result)
        if (
            method == "POST"
            and len(route) == 5
            and route[0] == "projects"
            and route[2] == "members"
            and route[4] == "role"
        ):
            from .workbench_mail_tasks import set_project_role

            return self.send_value(200, set_project_role(store, route[1], route[3], data["role"]))
        if route == ["mailbox", "sessions"]:
            from .workbench_mail_config import export_session
            from .workbench_mail_sessions import list_sessions

            if method == "POST":
                result = export_session(
                    store, data["employee_id"], data["project_id"], data.get("label", "")
                )
            elif method == "GET":
                query = parse_qs(url.query)
                result = {
                    "sessions": list_sessions(
                        store, query.get("employee_id", [""])[0], query.get("project_id", [""])[0]
                    )
                }
            else:
                raise WorkbenchError("NOT_FOUND", "Unknown mailbox session action.")
            return self.send_value(200, result)
        if len(route) == 3 and route[:2] == ["mailbox", "sessions"] and method == "DELETE":
            from .workbench_mail_sessions import revoke_session

            return self.send_value(200, revoke_session(store, route[2]))
        if method == "GET" and route == ["changes"]:
            return self.changes()
        if (
            method in {"POST", "DELETE"}
            and route
            and route[0] == "fleet"
            and store.update_maintenance()["paused"]
        ):
            raise WorkbenchError("UPDATE_PAUSED", "更新准备期间暂停设备配置变更，请先恢复接单。")
        if method == "POST" and route == ["application", "quit"]:
            self.server.updates.assert_can_quit()
            # Respond before shutting down this instance; main's finally owns
            # worker cleanup. Browser tabs and unrelated agents remain external.
            self.send_value(200, {"stopping": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if route and route[0] == "updates":
            action = route[1] if len(route) == 2 else ""
            if method == "GET" and action == "status":
                result = self.server.updates.status()
            elif method == "POST" and action == "check":
                result = self.server.updates.check_releases()
            elif method == "POST" and action == "channel":
                result = self.server.updates.channel(data.get("channel"))
            elif method == "POST" and action == "prepare":
                result = self.server.updates.prepare()
            elif method == "POST" and action == "resume":
                result = self.server.updates.resume()
            else:
                raise WorkbenchError("NOT_FOUND", "Unknown update action.")
            return self.send_value(200, result)
        if method == "GET" and route == ["bootstrap"]:
            runtime = runtime_status(store.root)
            runtime["install"] = self.server.install_status
            snapshot = store.snapshot()
            if self.server.fleet:
                revoked = {d["id"]: d["revoked"] for d in self.server.fleet.public_devices()}
                snapshot["devices"] = [
                    {**d, "revoked": revoked.get(d["id"], False)} for d in snapshot["devices"]
                ]
            _project_query = parse_qs(url.query).get("project_id", [None])[0]
            _proofs = store.proof_index(_project_query)
            for _task in snapshot["tasks"]:
                if _task["id"] in _proofs:
                    _task["proof"] = _proofs[_task["id"]]  # 列表行证据标记的数据源
            result = {
                **snapshot,
                # 未决权限请求：一屏三问要回答"等在谁身上、在等什么"（FL-a）
                "pending_permissions": store.pending_permissions(
                    parse_qs(url.query).get("project_id", [None])[0]
                ),
                "version": VERSION,
                # AM-06 ✓：把"你打开的是哪份代码"暴露给界面与证据 ✓
                "build": build_info(store),
                "node": store.local_node(),
                "runtime": runtime,
                "fleet": self.server.fleet_status(),
                "update_maintenance": store.update_maintenance(),
                "update_nodes": compatibility_nodes(store, self.server.fleet),
            }
        elif method == "POST" and route == ["projects"]:
            result = store.create_project(data["name"], data["path"])
        elif len(route) >= 3 and route[0] == "projects" and route[2] == "members":
            if method == "POST" and len(route) == 3:
                from .workbench_mail_sessions import ensure_member_session

                result = ensure_member_session(store, route[1], data["employee_id"])
            elif method == "DELETE" and len(route) == 4:
                result = store.remove_project_member(route[1], route[3])
            else:
                raise WorkbenchError("NOT_FOUND", "Unknown membership action.")
        elif len(route) == 3 and route[0] == "projects" and route[2] == "messages":
            if method == "GET":
                result = {"messages": store.list_messages(route[1])}
            elif method == "POST":
                if "sender_id" in data or "source_task_id" in data:
                    raise WorkbenchError(
                        "IDENTITY_FORBIDDEN", "Human messages cannot impersonate an employee."
                    )
                result = store.send_message(
                    route[1],
                    data["title"],
                    data["body"],
                    recipient_id=data.get("recipient_id"),
                    reply_to=data.get("reply_to"),
                    request_work=data.get("request_work", False),
                    request_id=data.get("request_id"),
                )
                if result.get("task_id"):
                    self.server.notify()
            else:
                raise WorkbenchError("NOT_FOUND", "Unknown message action.")
        elif method == "POST" and route == ["pick-project"]:
            result = pick_project()
        elif method == "GET" and route == ["models"]:
            query = parse_qs(url.query)
            result = {"models": available_models(query.get("kind", [""])[0], store.root)}
        elif method == "GET" and route == ["discover"]:
            from .workbench_runtime import discover_suspects

            result = {"employees": discover_employees(), "suspects": discover_suspects()}
        elif method == "POST" and route == ["employees"]:
            kind = data["kind"]
            connection_type = data.get("connection_type", "cli")
            entrypoint = data.get("entrypoint")
            discovered = next(
                (
                    e
                    for e in discover_employees()
                    if e["kind"] == kind
                    and e.get("connection_type", "cli") == connection_type
                    and (entrypoint is None or e.get("entrypoint", e.get("binary")) == entrypoint)
                ),
                None,
            )
            # 手工登记通道（HS 第 10b 条 ✓ 老板点名的用户自救路径 ✓）：扫描只是**加速器** ✗
            # 手工登记：**入口必须真实存在且可执行** ✓（否则与"垃圾登记"无异 ✗
            #  —— 旧测试正是防这个：`{"name":"Fake","entrypoint":"/unknown"}` 必须仍被拒 ✓）
            manual = (
                bool(str(data.get("name") or "").strip())
                and bool(entrypoint)
                and os.path.isfile(str(entrypoint))
                and os.access(str(entrypoint), os.X_OK)
            )
            if discovered is None and not manual:
                raise WorkbenchError(
                    "AGENT_NOT_DISCOVERED",
                    "Discover this agent entry point again before registering it.",
                )
            if discovered is None:
                discovered = {
                    "status": "unknown",
                    "detail": t_manual_detail(entrypoint),
                    "entrypoint": entrypoint,
                    "auth_status": "unknown",
                }
            if data.get("node_id") not in {None, store.local_node()["id"]}:
                raise WorkbenchError(
                    "REMOTE_REGISTRATION", "Connect the remote node before adding its employees."
                )
            result = store.create_employee(
                data["name"],
                kind,
                data.get("project_id"),
                status=discovered["status"],
                detail=discovered["detail"],
                connection_type=connection_type,
                entrypoint=discovered.get("entrypoint", discovered.get("binary")) or "",
                auth_status=discovered.get("auth_status", "unknown"),
            )
        elif method == "POST" and route == ["tasks"]:
            result = store.create_task(
                data["project_id"],
                data["title"],
                data["prompt"],
                data["assignee_id"],
                data.get("permission_mode", "read-only"),
                data.get("model"),
            )
            self.server.notify()
        elif (
            method == "POST" and len(route) == 3 and route[0] == "employees" and route[2] == "check"
        ):
            employee = next((e for e in store.snapshot()["employees"] if e["id"] == route[1]), None)
            if employee is None:
                raise WorkbenchError("NOT_FOUND", "Employee not found.")
            if employee["node_id"] != store.local_node()["id"]:
                raise WorkbenchError(
                    "REMOTE_CHECK_UNAVAILABLE", "Check agent authentication on its own device."
                )
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
            result = store.update_employee(
                employee["id"],
                candidate["status"] if candidate else "unavailable",
                candidate["detail"]
                if candidate
                else "This registered entry point was not found. Execution is not verified by discovery.",
                auth_status=candidate.get("auth_status", "unknown") if candidate else "unknown",
            )
        elif (
            method == "POST"
            and len(route) == 3
            and route[0] == "employees"
            and route[2] == "lifecycle"
        ):
            result = store.set_employee_lifecycle(route[1], data["status"], data.get("reason", ""))
            self.server.notify()
        elif method == "GET" and route == ["governance"]:
            query = parse_qs(url.query)
            result = {"events": store.governance_events(query.get("project_id", [None])[0])}
        elif (
            len(route) == 3
            and route[0] == "employees"
            and route[2] == "onboarding"
            and method == "GET"
        ):
            result = onboarding_status(
                store, route[1], parse_qs(url.query).get("project_id", [None])[0]
            )
        elif (
            len(route) == 3
            and route[0] == "employees"
            and route[2] == "verify"
            and method == "POST"
        ):
            result = create_probe(store, route[1], data["project_id"], model=data.get("model"))
            self.server.notify()
        elif len(route) == 3 and route[0] == "tasks" and route[2] == "delivery" and method == "GET":
            result = task_delivery(store, route[1])
        elif method == "GET" and len(route) == 2 and route[0] == "tasks":
            result = store.task_detail(route[1])
        elif method == "POST" and len(route) == 3 and route[0] == "tasks":
            if route[2] == "cancel":
                result = store.cancel_task(route[1])
            elif route[2] == "follow-up":
                result = store.follow_up_task(route[1], data["note"])
            elif route[2] == "apply":
                if data.get("confirm") is not True:
                    raise WorkbenchError("APPLY_CONFIRM_REQUIRED", "请明确确认应用已验收修改。")
                if store.update_maintenance()["paused"]:
                    raise WorkbenchError("UPDATE_PAUSED", "更新准备期间暂停合入，请先恢复接单。")
                result = apply_delivery(store, route[1])
            elif route[2] == "review":
                result = store.review_task(route[1], data["decision"], data.get("note", ""))
            else:
                raise WorkbenchError("NOT_FOUND", "Unknown task action.")
            self.server.notify()
        elif (
            method == "POST"
            and len(route) == 4
            and route[0] == "tasks"
            and route[2] == "permissions"
        ):
            result = store.resolve_permission(route[1], route[3], data["decision"])
            self.server.notify()
        elif (
            method == "GET"
            and len(route) == 5
            and route[0] == "projects"
            and route[2] == "resources"
            and route[4] == "read"
        ):
            result = store.read_resource(route[1], route[3])
            resource = result["resource"]
            if str(resource.get("path", "")).lower().endswith((".html", ".htm")) or resource.get(
                "kind"
            ) in {"html", "archify"}:
                result["preview_url"] = self.server.resource_preview(result["content"])
        elif (
            len(route) >= 5
            and route[0] == "projects"
            and route[2] == "resources"
            and route[4] == "versions"
        ):
            if len(route) == 5 and method == "GET":
                result = store.resource_versions(route[1], route[3])
            elif len(route) == 5 and method == "POST":
                result = store.capture_resource_version(route[1], route[3], data.get("summary", ""))
            elif len(route) == 7 and route[6] == "read" and method == "GET":
                result = store.read_resource_version(route[1], route[3], route[5])
                resource = result["resource"]
                if str(resource.get("path", "")).lower().endswith(
                    (".html", ".htm")
                ) or resource.get("kind") in {"html", "archify"}:
                    result["preview_url"] = self.server.resource_preview(result["content"])
            elif len(route) == 7 and route[6] == "approve" and method == "POST":
                result = store.approve_resource_version(route[1], route[3], route[5])
            else:
                raise WorkbenchError("NOT_FOUND", "Unknown resource version action.")
        elif (
            method == "GET"
            and len(route) == 3
            and route[0] == "projects"
            and route[2] == "activity"
        ):
            result = project_activity(store, route[1])
        elif (
            method == "GET"
            and len(route) == 3
            and route[0] == "projects"
            and route[2] == "knowledge"
        ):
            result = store.knowledge_status(route[1])
        elif (
            method == "POST"
            and len(route) == 4
            and route[0] == "projects"
            and route[2:] == ["knowledge", "query"]
        ):
            result = store.query_knowledge(route[1], data.get("query"))
        elif method == "POST" and route == ["resources"]:
            result = store.add_resource(
                data["project_id"], data["name"], data["kind"], data["path"]
            )
        elif method == "POST" and route == ["memories"]:
            result = store.add_memory(
                data["project_id"], data["title"], data["body"], data.get("source") or "human"
            )
        elif method == "DELETE" and len(route) == 2 and route[0] == "memories":
            store.delete_memory(route[1])
            result = {"deleted": True}
        elif method == "GET" and route == ["memory", "search"]:
            query = parse_qs(url.query)
            result = {
                "memories": store.search_memory(
                    query.get("project_id", [""])[0], query.get("q", [""])[0]
                )
            }
        elif method == "POST" and route == ["fleet", "start"]:
            result = self.server.start_fleet(data.get("address", "127.0.0.1"), data.get("port", 0))
        elif method == "POST" and route == ["fleet", "stop"]:
            if self.server.fleet:
                self.server.fleet.stop()
                self.server.fleet = None
            self.server.listener_path.write_text(json.dumps({"enabled": False}))
            private_mode(self.server.listener_path, 0o600)
            result = {"stopped": True}
        elif method == "POST" and route == ["fleet", "invite"]:
            if not self.server.fleet:
                raise WorkbenchError(
                    "LISTENER_DISABLED", "Enable this device's connection listener first."
                )
            result = self.server.fleet.issue_invite(data["project_ids"])
        elif method == "POST" and route == ["fleet", "join"]:
            result = self.server.join_fleet(data["invite"])
        elif method == "POST" and route == ["fleet", "leave"]:
            if self.server.remote_worker:
                self.server.remote_worker.close()
            self.server.remote_worker = None
            self.server.remote_client = None
            self.server.remote_projects = []
            (store.root / "workbench/fleet/client.json").unlink(missing_ok=True)
            result = {"disconnected": True}
        elif method == "POST" and route == ["fleet", "map"]:
            if not self.server.remote_client:
                raise WorkbenchError(
                    "DEVICE_UNPAIRED", "Pair this device with its coordinator first."
                )
            result = self.server.remote_client.map_project(data["project_id"], data["path"])
            self.server.remote_worker.start_project(data["project_id"])
        elif method == "POST" and route == ["fleet", "employee"]:
            if not self.server.remote_client:
                raise WorkbenchError(
                    "DEVICE_UNPAIRED", "Pair this device with its coordinator first."
                )
            kind = data["kind"]
            discovery = next((e for e in discover_employees() if e["kind"] == kind), None)
            if not discovery or discovery["status"] == "unavailable":
                raise WorkbenchError(
                    "AGENT_UNAVAILABLE", "Install the supported employee on this device first."
                )
            result = self.server.remote_client.register_employee(
                data["project_id"], data["name"], kind
            )
        elif method == "POST" and route == ["fleet", "revoke"]:
            if not self.server.fleet:
                raise WorkbenchError(
                    "LISTENER_DISABLED", "Enable the device listener to manage pairing."
                )
            self.server.fleet.revoke_device(data["device_id"])
            result = {"revoked": True}
        elif method == "POST" and route == ["runtime", "install"]:
            return self.send_value(202, self.server.install_runtime())
        else:
            raise WorkbenchError("NOT_FOUND", "Unknown workbench action.")
        if method in {"POST", "DELETE"} and not (
            len(route) == 4 and route[0] == "projects" and route[2:] == ["knowledge", "query"]
        ):
            self.server.ui_notify()
        self.send_value(200, result)

    def dispatch(self):
        try:
            self.handle_request()
        except WorkbenchError as exc:
            status = (
                404
                if exc.code.lower() == "not_found"
                else 401
                if exc.code == "UNAUTHORIZED"
                else 400
            )
            self.send_value(status, {"error": {"code": exc.code, "message": exc.message}})
        except (KeyError, TypeError, ValueError):
            self.send_value(
                400,
                {
                    "error": {
                        "code": "INVALID_REQUEST",
                        "message": "Required fields are missing or invalid.",
                    }
                },
            )
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # noqa: BLE001 - HTTP boundary never leaks exception text
            self.send_value(
                500,
                {
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": "The workbench could not complete this action. Your task status remains visible.",
                    }
                },
            )

    do_GET = dispatch
    do_POST = dispatch
    do_DELETE = dispatch


DESKTOP_BUNDLE_ID = "com.polaris-smart.agent-mailbox"


def _desktop_wanted(args) -> bool:
    """Native macOS app builds show the workbench in an app-owned window.

    Frozen macOS launches default to the desktop shell; other platforms and
    plain CLI launches keep the browser behaviour. Both directions can be
    overridden explicitly (AGENT_MAILBOX_DESKTOP=1 / AGENT_MAILBOX_NO_DESKTOP=1).
    """
    if sys.platform != "darwin" or args.no_browser:
        return False
    if os.environ.get("AGENT_MAILBOX_NO_DESKTOP"):
        return False
    if os.environ.get("AGENT_MAILBOX_DESKTOP") == "1":
        return True
    if not getattr(sys, "frozen", False):
        return False
    try:
        importlib.import_module(".workbench_desktop", __package__)
    except ImportError:
        return False
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Open the independent agent-mailbox project workbench.",
        epilog="Optional commands: agent-mailbox prepare --help; agent-mailbox node --help.",
    )
    parser.add_argument("--home", type=Path, default=Path.home() / ".agent-mailbox")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    # 2026-10-03 事故修复：默认不开浏览器标签（有人在反复调用 agent-mailbox ⇒ 每次都弹新标签）。
    # 2026-10-04 修正：**不得据此掐掉桌面壳路径**。显式 AGENT_MAILBOX_DESKTOP=1、
    #   或冻结版 macOS app（sys.frozen）都依赖"起窗口 / 聚焦已开窗口"这条路；
    #   一并禁用会让桌面版第二实例退回弹浏览器标签，且打断
    #   tests/test_workbench_desktop.py 两个用例锁定的契约。
    _explicit_desktop = os.environ.get("AGENT_MAILBOX_DESKTOP") == "1"
    _frozen_desktop = sys.platform == "darwin" and getattr(sys, "frozen", False)
    if os.environ.get("AGENT_MAILBOX_BROWSER") != "1" and not (
        _explicit_desktop or _frozen_desktop
    ):
        args.no_browser = True
    store = WorkbenchStore(args.home)
    try:
        server = WorkbenchHTTP(store, args.port)
        # ⑤ 应用侧**兑现**（老板判据：开着应用就该自动叫 ✓ 零配置 ✓ 默认开 ✓）
        threading.Thread(target=wake_redeem_loop, args=(store, store.root), daemon=True).start()
    except WorkbenchError as exc:
        if exc.code != "ALREADY_RUNNING":
            raise
        saved = json.loads((store.root / "workbench/instance.json").read_text())
        request = urllib.request.Request(
            saved["endpoint"] + "/api/workbench/bootstrap",
            headers={"Authorization": "Bearer " + saved["token"]},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            if response.status != 200:
                raise
        url = saved["endpoint"] + "/#token=" + saved["token"]
        if sys.stdout is not None:
            print(url, flush=True)
        if not args.no_browser:
            # A second launch of the native app focuses the existing in-app
            # window instead of stacking another browser window.
            if (
                _desktop_wanted(args)
                and subprocess.run(["open", "-b", DESKTOP_BUNDLE_ID], check=False).returncode == 0
            ):
                return
            webbrowser.open(url)
        return

    def stop_on_signal(_signal, _frame):
        raise KeyboardInterrupt

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, stop_on_signal)
    server.engine.start()
    url = server.endpoint + "/#token=" + server.token
    if sys.stdout is not None:
        print(url, flush=True)
    if _desktop_wanted(args):
        # The AppKit event loop owns the main thread; the HTTP service keeps
        # serving from its own background threads until the desktop UI quits.
        from .workbench_desktop import run_desktop

        service_stopped = threading.Event()

        def serve_desktop():
            try:
                server.serve_forever()
            finally:
                service_stopped.set()

        server_thread = threading.Thread(target=serve_desktop, daemon=True)
        server_thread.start()
        try:
            run_desktop(url, service_stopped=service_stopped)
        except KeyboardInterrupt:
            # SIGTERM uses the KeyboardInterrupt shim in non-desktop mode too.
            pass
        finally:
            # Ignore further SIGTERM while shutting down: the KeyboardInterrupt
            # shim must not fire inside server.shutdown() and corrupt teardown.
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            server.shutdown()
            server_thread.join(timeout=5)
    else:
        if not args.no_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    server.close()


if __name__ == "__main__":
    main()
