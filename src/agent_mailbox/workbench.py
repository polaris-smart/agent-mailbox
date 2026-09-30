"""Human workbench: authenticated loopback UI and durable task execution."""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import secrets
import signal
import subprocess
import sys
import threading
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .workbench_engine import WorkbenchEngine
from .workbench_lock import WorkbenchLock
from .workbench_runtime import available_models, discover_employees, runtime_status
from .workbench_store import WorkbenchError, WorkbenchStore

ASSETS = Path(__file__).parent / "workbench_assets"
PREFIX = "/api/workbench"
VERSION = __version__


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
        self.install_lock = threading.Lock()
        self.install_status = None
        self.fleet = None
        self.remote_client = None
        self.remote_worker = None
        self.remote_projects = []
        self.fleet_error = None
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
        self.instance_path.chmod(0o600)
        self.restore_connections()

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
        self.listener_path.chmod(0o600)
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

    def send_value(self, status, value, content_type="application/json; charset=utf-8"):
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
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
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
        if self.command == "GET" and not path.startswith(PREFIX):
            name = (
                "index.html"
                if path in {"/", "/workbench"}
                else path.removeprefix("/workbench-assets/").lstrip("/")
            )
            allowed = {
                "index.html": "text/html; charset=utf-8",
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
        if method == "GET" and route == ["changes"]:
            return self.changes()
        if method == "POST" and route == ["application", "quit"]:
            # Respond before shutting down this instance; main's finally owns
            # worker cleanup. Browser tabs and unrelated agents remain external.
            self.send_value(200, {"stopping": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if method == "GET" and route == ["bootstrap"]:
            runtime = runtime_status(store.root)
            runtime["install"] = self.server.install_status
            snapshot = store.snapshot()
            if self.server.fleet:
                revoked = {d["id"]: d["revoked"] for d in self.server.fleet.public_devices()}
                snapshot["devices"] = [
                    {**d, "revoked": revoked.get(d["id"], False)} for d in snapshot["devices"]
                ]
            result = {
                **snapshot,
                "version": VERSION,
                "node": store.local_node(),
                "runtime": runtime,
                "fleet": self.server.fleet_status(),
            }
        elif method == "POST" and route == ["projects"]:
            result = store.create_project(data["name"], data["path"])
        elif len(route) >= 3 and route[0] == "projects" and route[2] == "members":
            if method == "POST" and len(route) == 3:
                result = store.add_project_member(route[1], data["employee_id"])
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
            result = {"employees": discover_employees()}
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
            if discovered is None:
                raise WorkbenchError(
                    "AGENT_NOT_DISCOVERED",
                    "Discover this agent entry point again before registering it.",
                )
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
        elif method == "GET" and len(route) == 2 and route[0] == "tasks":
            result = store.task_detail(route[1])
        elif method == "POST" and len(route) == 3 and route[0] == "tasks":
            if route[2] == "cancel":
                result = store.cancel_task(route[1])
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
            self.server.listener_path.chmod(0o600)
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
        if method in {"POST", "DELETE"}:
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


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Open the independent agent-mailbox project workbench.",
        epilog="Optional commands: agent-mailbox prepare --help; agent-mailbox node --help.",
    )
    parser.add_argument("--home", type=Path, default=Path.home() / ".agent-mailbox")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    store = WorkbenchStore(args.home)
    try:
        server = WorkbenchHTTP(store, args.port)
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
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()


if __name__ == "__main__":
    main()
