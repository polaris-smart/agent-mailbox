"""Explicit, certificate-pinned HTTPS between workbench device nodes.

The listener serves only device-scoped routes. Pairing grants project access,
never human APIs, arbitrary shell commands or blanket host access. Invitations
are one-use secrets; their certificate fingerprint must be delivered through a
trusted channel alongside the invitation.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import secrets
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit

from .workbench_store import ACTIVE, WorkbenchError, WorkbenchStore

MAX_BODY = 1024 * 1024
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
FINGERPRINT_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")
REMOTE_EVENTS = frozenset({"started", "session", "event", "output", "error", "permission_denied"})


def _identifier(value, label="编号"):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise WorkbenchError("invalid_field", f"{label}无效，请刷新后重试。")
    return value


def _string(value, label, limit=300):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise WorkbenchError("invalid_field", f"请填写有效的{label}。")
    return value


def _digest(secret):
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _write_private(path, data):
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    except OSError as exc:
        raise WorkbenchError("storage_error", "无法保存设备凭据，请检查磁盘和目录权限。") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _directory(root):
    directory = Path(root) / "workbench" / "fleet"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    return directory


def _load(path, fallback):
    if not path.exists():
        return fallback
    try:
        path.chmod(0o600)
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkbenchError("storage_error", "设备凭据无法读取，请恢复备份，勿重新覆盖。") from exc


class FleetCoordinator:
    def __init__(self, store: WorkbenchStore, on_change=None):
        self.store = store
        self.directory = _directory(store.root)
        self.state_path = self.directory / "server.json"
        self.state = _load(self.state_path, {"invites": {}, "devices": {}})
        if not isinstance(self.state, dict) or not all(
            isinstance(self.state.get(key), dict) for key in ("invites", "devices")
        ):
            raise WorkbenchError("storage_error", "设备配对记录格式无效，请恢复备份。")
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.server = None
        self.thread = None
        self.base_url = ""
        self.fingerprint = ""
        self.on_change = on_change

    def _save(self):
        _write_private(self.state_path, json.dumps(self.state, ensure_ascii=False).encode("utf-8"))

    def _tls(self, hostname):
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import ec
            from cryptography.x509.oid import NameOID
        except ImportError as exc:
            raise WorkbenchError(
                "runtime_missing", "请安装包含 TLS 组件的完整工作台版本。"
            ) from exc
        key_path = self.directory / "tls-key.pem"
        cert_path = self.directory / "tls-cert.pem"
        if key_path.exists() != cert_path.exists():
            raise WorkbenchError("storage_error", "TLS 身份文件不完整，请恢复备份。")
        if not key_path.exists():
            key = ec.generate_private_key(ec.SECP256R1())
            subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Agent Mailbox Fleet")])
            now = datetime.now(timezone.utc)
            try:
                name = x509.IPAddress(ipaddress.ip_address(hostname))
            except ValueError:
                name = x509.DNSName(hostname)
            cert = (
                x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(subject)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5))
                .not_valid_after(now + timedelta(days=3650))
                .add_extension(x509.SubjectAlternativeName([name]), critical=False)
                .sign(key, hashes.SHA256())
            )
            _write_private(
                key_path,
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ),
            )
            _write_private(cert_path, cert.public_bytes(serialization.Encoding.PEM))
        key_path.chmod(0o600)
        cert_path.chmod(0o600)
        try:
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            self.fingerprint = "sha256:" + cert.fingerprint(hashes.SHA256()).hex()
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(cert_path, key_path)
            return context
        except (OSError, ValueError, ssl.SSLError) as exc:
            raise WorkbenchError("storage_error", "TLS 身份无法加载，请恢复备份。") from exc

    def start(self, host="127.0.0.1", port=0, advertised_host=None):
        _string(host, "监听地址", 255)
        if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
            raise WorkbenchError("invalid_field", "监听端口需要在 0 到 65535 之间。")
        with self.lock:
            if self.server is not None:
                raise WorkbenchError("invalid_state", "设备监听已经启动。")
            visible_host = advertised_host or ("127.0.0.1" if host == "0.0.0.0" else host)
            _string(visible_host, "设备地址", 255)
            context = self._tls(visible_host)
            coordinator = self

            class Handler(BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def log_message(self, format, *args):
                    # Invitation and bearer credentials never enter access logs.
                    return

                def do_GET(self):
                    self.respond("GET")

                def do_POST(self):
                    self.respond("POST")

                def respond(self, method):
                    try:
                        body = {}
                        if method == "POST":
                            if self.headers.get("Transfer-Encoding"):
                                raise WorkbenchError("invalid_field", "不支持此请求编码。")
                            length = self.headers.get("Content-Length", "")
                            if not length.isdecimal() or not 0 < int(length) <= MAX_BODY:
                                raise WorkbenchError(
                                    "payload_too_large", "请求大小无效或超过 1 MiB。"
                                )
                            try:
                                body = json.loads(self.rfile.read(int(length)))
                            except (ValueError, UnicodeDecodeError) as exc:
                                raise WorkbenchError(
                                    "invalid_field", "请求需要是有效的 JSON。"
                                ) from exc
                            if not isinstance(body, dict):
                                raise WorkbenchError("invalid_field", "请求内容需要是 JSON 对象。")
                        result = coordinator._route(method, self.path, dict(self.headers), body)
                        self.send_json(200, result)
                    except WorkbenchError as exc:
                        code = (
                            403
                            if exc.code in {"permission_denied", "invite_expired", "invite_used"}
                            else 400
                        )
                        if exc.code == "not_found":
                            code = 404
                        self.send_json(code, {"error": {"code": exc.code, "message": exc.message}})
                    except Exception:  # noqa: BLE001 - HTTP boundary never exposes internals.
                        self.send_json(
                            500,
                            {
                                "error": {
                                    "code": "internal_error",
                                    "message": "设备请求未完成，请查看主控状态。",
                                }
                            },
                        )

                def send_json(self, code, value):
                    content = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(content)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(content)
                    self.close_connection = True

            class Server(ThreadingHTTPServer):
                daemon_threads = True

                def get_request(self):
                    connection, address = super().get_request()
                    connection.settimeout(10)
                    connection = context.wrap_socket(
                        connection, server_side=True, do_handshake_on_connect=False
                    )
                    return connection, address

            try:
                server = Server((host, port), Handler)
            except (OSError, ssl.SSLError) as exc:
                raise WorkbenchError(
                    "network_error", "无法监听设备连接，请检查地址和端口。"
                ) from exc
            self.server = server
            display_host = f"[{visible_host}]" if ":" in visible_host else visible_host
            self.base_url = f"https://{display_host}:{server.server_address[1]}"
            self.thread = threading.Thread(
                target=server.serve_forever, name="mailbox-fleet-tls", daemon=True
            )
            self.thread.start()
            return {
                "base_url": self.base_url,
                "fingerprint": self.fingerprint,
                "device_id": self.store.local_node()["id"],
            }

    def stop(self):
        with self.lock:
            server, thread = self.server, self.thread
            self.server = self.thread = None
            self.condition.notify_all()
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)

    def notify(self):
        """Wake pending device claims after a task or execution-state change."""
        with self.condition:
            self.condition.notify_all()
        if self.on_change is not None:
            self.on_change()

    def issue_invite(self, project_ids, ttl=300):
        if (
            not isinstance(project_ids, list)
            or not project_ids
            or len(project_ids) > 100
            or not all(isinstance(value, str) for value in project_ids)
        ):
            raise WorkbenchError("invalid_field", "请选择要共享的项目。")
        if not isinstance(ttl, int) or isinstance(ttl, bool) or not 1 <= ttl <= 3600:
            raise WorkbenchError("invalid_field", "邀请码有效期需要在 1 到 3600 秒之间。")
        with self.lock:
            if self.server is None:
                raise WorkbenchError("invalid_state", "请先启动设备 TLS 监听。")
            available = {project["id"] for project in self.store.snapshot()["projects"]}
            if not set(project_ids) <= available:
                raise WorkbenchError("not_found", "有项目已不存在，请刷新后重试。")
            invite_id, secret = secrets.token_hex(16), secrets.token_urlsafe(32)
            expires = time.time() + ttl
            scope = list(dict.fromkeys(project_ids))
            self.state["invites"][invite_id] = {
                "secret_hash": _digest(secret),
                "project_ids": scope,
                "expires_at": expires,
                "used": False,
            }
            self._save()
            return {
                "version": 1,
                "base_url": self.base_url,
                "fingerprint": self.fingerprint,
                "invite_id": invite_id,
                "invite_secret": secret,
                "expires_at": expires,
                "project_ids": scope,
            }

    def _pair(self, body):
        invite_id = _identifier(body.get("invite_id"), "邀请码编号")
        secret = _string(body.get("invite_secret"), "邀请码", 200)
        device_id = _identifier(body.get("device_id"), "设备编号")
        name = _string(body.get("name"), "设备名称", 200)
        with self.lock:
            invitation = self.state["invites"].get(invite_id)
            expected = invitation["secret_hash"] if invitation else "0" * 64
            valid = hmac.compare_digest(expected, _digest(secret))
            if not invitation or not valid:
                raise WorkbenchError("permission_denied", "邀请码验证失败。")
            if invitation["used"]:
                raise WorkbenchError("invite_used", "邀请码已使用，请生成新的邀请码。")
            if invitation["expires_at"] <= time.time():
                raise WorkbenchError("invite_expired", "邀请码已过期，请生成新的邀请码。")
            if device_id == self.store.local_node()["id"] or device_id in self.state["devices"]:
                raise WorkbenchError("permission_denied", "该设备已有身份，请使用现有凭据。")
            token = secrets.token_urlsafe(32)
            self.store.upsert_device(
                device_id, name, "online", datetime.now(timezone.utc).isoformat()
            )
            self.state["devices"][device_id] = {
                "device_id": device_id,
                "name": name,
                "token_hash": _digest(token),
                "project_ids": invitation["project_ids"],
                "paired_at": time.time(),
                "revoked": False,
            }
            invitation["used"] = True
            self._save()
            return {
                "device_id": device_id,
                "token": token,
                "project_ids": invitation["project_ids"],
                "coordinator_id": self.store.local_node()["id"],
            }

    def _authenticate(self, headers):
        # Header names are case insensitive on the wire.
        headers = {key.lower(): value for key, value in headers.items()}
        device_id = headers.get("x-device-id", "")
        authorization = headers.get("authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
        if not isinstance(device_id, str) or len(device_id) > 128 or len(token) > 200:
            raise WorkbenchError("permission_denied", "设备身份验证失败。")
        with self.lock:
            device = self.state["devices"].get(device_id)
            expected = device["token_hash"] if device else "0" * 64
            valid = hmac.compare_digest(expected, _digest(token))
            if not device or not valid or device.get("revoked"):
                raise WorkbenchError("permission_denied", "设备身份验证失败。")
            return {**device, "project_ids": list(device["project_ids"]), "_request_token": token}

    @staticmethod
    def _scope(device, project_id):
        _identifier(project_id, "项目编号")
        if project_id not in device["project_ids"]:
            raise WorkbenchError("permission_denied", "这个设备没有该项目的访问权限。")

    def _task_scope(self, device, task_id, run_id):
        _identifier(task_id, "任务编号")
        task = self.store.get_task(task_id)
        self._scope(device, task["project_id"])
        if task["node_id"] != device["device_id"] or task["run_id"] != run_id:
            raise WorkbenchError("permission_denied", "任务不属于这个设备或执行已过期。")
        return task

    def _context(self, project_id):
        result = self.store.project_context(project_id)
        result["project"].pop("path", None)
        for resource in result["resources"]:
            resource.pop("path", None)
        result["employees"] = [
            {key: row[key] for key in ("id", "name", "kind", "node_id", "status")}
            for row in self.store.snapshot()["employees"]
            if project_id in row["project_ids"]
        ]
        return result

    def _project_tool(self, device, project_id, body):
        self._scope(device, project_id)
        employee_id = _identifier(body.get("employee_id"), "员工编号")
        employee = next(
            (row for row in self.store.snapshot()["employees"] if row["id"] == employee_id), None
        )
        if (
            employee is None
            or employee["node_id"] != device["device_id"]
            or project_id not in employee["project_ids"]
        ):
            raise WorkbenchError("permission_denied", "员工不属于这个设备或授权项目。")
        tool, args = body.get("tool"), body.get("args", {})
        if not isinstance(args, dict):
            raise WorkbenchError("invalid_field", "工具参数需要是 JSON 对象。")
        if (
            "project_id" in args
            and args["project_id"] != project_id
            or "employee_id" in args
            and args["employee_id"] != employee_id
        ):
            raise WorkbenchError("permission_denied", "工具不能切换到其他项目或员工身份。")
        if tool == "context":
            return self._context(project_id)
        if tool == "memory_search":
            return {"memories": self.store.search_memory(project_id, args.get("query", ""))}
        if tool == "resource_read":
            result = self.store.read_resource(project_id, args.get("resource_id"))
            result["resource"].pop("path", None)
            result["source"] = f"resource:{result['resource']['id']}:{result['resource']['name']}"
            return result
        if tool == "note":
            return self.store.add_memory(
                project_id, args.get("title"), args.get("body"), source=f"employee:{employee_id}"
            )
        if tool == "team_message":
            if args.get("recipient_id") == employee_id:
                raise WorkbenchError("permission_denied", "请选择另一位项目员工。")
            task = self.store.create_task(
                project_id,
                args.get("title"),
                args.get("message"),
                args.get("recipient_id"),
                "read-only",
            )
            self.store.add_event(
                task["id"], "team_message", "项目同事请求了这项工作。", {"from_id": employee_id}
            )
            self.notify()
            return {"task_id": task["id"], "status": "queued"}
        raise WorkbenchError("permission_denied", "远端员工不能调用这个工具。")

    def _route(self, method, path, headers, body):
        if "?" in path or "%" in path or ".." in path:
            raise WorkbenchError("not_found", "找不到这个设备接口。")
        if method == "POST" and path == "/v1/pair":
            return self._pair(body)
        device = self._authenticate(headers)

        def redact(value):
            if isinstance(value, dict):
                return {
                    key: "[redacted]"
                    if str(key).lower()
                    in {"token", "authorization", "password", "api_key", "access_token"}
                    else redact(item)
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [redact(item) for item in value]
            if isinstance(value, str):
                return value.replace(device["_request_token"], "[redacted]")
            return value

        body = redact(body)
        if method == "GET" and path == "/v1/device":
            return {
                "device": {
                    key: device[key] for key in ("device_id", "name", "project_ids", "paired_at")
                },
                "coordinator": self.store.local_node(),
            }
        if method == "GET" and path == "/v1/projects":
            return {
                "projects": [
                    {"id": row["id"], "name": row["name"]}
                    for row in self.store.snapshot()["projects"]
                    if row["id"] in device["project_ids"]
                ]
            }
        match = re.fullmatch(r"/v1/projects/([A-Za-z0-9_-]+)/context", path)
        if method == "GET" and match:
            project_id = match[1]
            self._scope(device, project_id)
            return self._context(project_id)
        match = re.fullmatch(r"/v1/projects/([A-Za-z0-9_-]+)/tools", path)
        if method == "POST" and match:
            return self._project_tool(device, match[1], body)
        if method == "POST" and path == "/v1/employees":
            project_id = body.get("project_id")
            self._scope(device, project_id)
            employee = self.store.create_employee(
                body.get("name"), body.get("kind"), project_id, node_id=device["device_id"]
            )
            return {"employee": employee}
        if method == "POST" and path == "/v1/tasks/claim":
            project_id = body.get("project_id")
            self._scope(device, project_id)
            wait = body.get("wait", 0)
            if not isinstance(wait, int) or isinstance(wait, bool) or not 0 <= wait <= 45:
                raise WorkbenchError("invalid_field", "领取等待时间需要在 0 到 45 秒之间。")
            # Hold the condition lock through the first DB claim. notify cannot
            # be lost between observing an empty queue and registering the wait.
            with self.condition:
                task = self.store.claim_task(device["device_id"], project_ids=[project_id])
                if task is None and wait and self.server is not None:
                    self.condition.wait(timeout=wait)
                    if self.server is not None:
                        task = self.store.claim_task(device["device_id"], project_ids=[project_id])
            if task:
                employee = next(
                    row
                    for row in self.store.snapshot()["employees"]
                    if row["id"] == task["assignee_id"]
                )
                task = {**task, "kind": employee["kind"], "employee_name": employee["name"]}
            return {"task": task}
        match = re.fullmatch(r"/v1/tasks/([A-Za-z0-9_-]+)/(events|receipt|control)", path)
        if method == "POST" and match:
            task_id, action = match.groups()
            task = self._task_scope(device, task_id, body.get("run_id"))
            if action == "control":
                wait = body.get("wait", 30)
                if not isinstance(wait, int) or isinstance(wait, bool) or not 0 <= wait <= 45:
                    raise WorkbenchError("invalid_field", "控制等待时间需要在 0 到 45 秒之间。")
                deadline = time.monotonic() + wait
                with self.condition:
                    while True:
                        self._authenticate(headers)
                        task = self._task_scope(device, task_id, body.get("run_id"))
                        remaining = deadline - time.monotonic()
                        if (
                            task["cancel_requested"]
                            or task["status"] not in ACTIVE
                            or remaining <= 0
                            or self.server is None
                        ):
                            return {
                                "status": task["status"],
                                "cancel_requested": task["cancel_requested"],
                            }
                        self.condition.wait(timeout=remaining)
            if task["status"] not in ACTIVE:
                raise WorkbenchError("invalid_state", "这次执行已结束，不能再提交进度。")
            if action == "events":
                event_type = body.get("type")
                if not isinstance(event_type, str) or event_type not in REMOTE_EVENTS:
                    raise WorkbenchError("invalid_field", "这个事件不能由远端设备提交。")
                if event_type == "started":
                    self.store.set_status(task_id, "running")
                event = self.store.add_event(
                    task_id, event_type, body.get("message", ""), body.get("payload")
                )
                return {"event": event}
            if body.get("status") == "review" and task["status"] != "running":
                raise WorkbenchError("invalid_state", "尚未确认员工开始执行，不能提交完成回执。")
            receipt = {
                "task": self.store.finish_task(
                    task_id, body.get("status"), body.get("result", ""), body.get("error")
                )
            }
            self.notify()
            return receipt
        raise WorkbenchError("not_found", "找不到这个设备接口。")

    def revoke_device(self, device_id):
        with self.lock:
            if device_id not in self.state["devices"]:
                raise WorkbenchError("not_found", "找不到这个设备。")
            self.state["devices"][device_id]["revoked"] = True
            self._save()
            record = self.state["devices"][device_id]
            self.store.upsert_device(device_id, record["name"], "offline")
        self.notify()


class FleetClient:
    def __init__(self, root: Path, invite: dict):
        self.store = WorkbenchStore(root)
        self.directory = _directory(self.store.root)
        self.credentials_path = self.directory / "client.json"
        self.mapping_path = self.directory / "projects.json"
        if not isinstance(invite, dict):
            raise WorkbenchError("invalid_field", "请提供有效的设备邀请码。")
        self.base_url = _string(invite.get("base_url"), "主控地址", 1024).rstrip("/")
        parsed = urlsplit(self.base_url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise WorkbenchError("invalid_field", "主控地址端口无效。") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or not port
        ):
            raise WorkbenchError("invalid_field", "主控地址需要是包含端口的 HTTPS 地址。")
        self.host, self.port = parsed.hostname, port
        self.fingerprint = invite.get("fingerprint")
        if not isinstance(self.fingerprint, str) or not FINGERPRINT_PATTERN.fullmatch(
            self.fingerprint
        ):
            raise WorkbenchError("invalid_field", "邀请码需要包含有效的 TLS 指纹。")
        self.credentials = _load(self.credentials_path, None)
        if self.credentials is not None and not isinstance(self.credentials, dict):
            raise WorkbenchError("storage_error", "设备凭据格式无效，请恢复备份。")
        if self.credentials is not None and (
            self.credentials.get("base_url") != self.base_url
            or self.credentials.get("fingerprint") != self.fingerprint
        ):
            raise WorkbenchError(
                "permission_denied", "该数据目录已连接其他主控，请使用独立设备目录。"
            )
        if self.credentials is None:
            node = self.store.local_node()
            paired = self._request(
                "POST",
                "/v1/pair",
                {
                    "invite_id": invite.get("invite_id"),
                    "invite_secret": invite.get("invite_secret"),
                    "device_id": node["id"],
                    "name": node["name"],
                },
                authenticated=False,
            )
            self.credentials = {
                **paired,
                "base_url": self.base_url,
                "fingerprint": self.fingerprint,
            }
            _write_private(
                self.credentials_path,
                json.dumps(self.credentials, ensure_ascii=False).encode("utf-8"),
            )
        self.mappings = _load(self.mapping_path, {})
        if not isinstance(self.mappings, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in self.mappings.items()
        ):
            raise WorkbenchError("storage_error", "本设备项目目录记录格式无效，请恢复备份。")

    def _request(self, method, path, body=None, authenticated=True, timeout=10):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection(
            self.host, self.port, context=context, timeout=timeout
        )
        try:
            connection.connect()
            actual = (
                "sha256:"
                + hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
            )
            if not hmac.compare_digest(actual, self.fingerprint):
                raise WorkbenchError("tls_pin_mismatch", "主控 TLS 身份不匹配，连接已停止。")
            # Send no invitation, token or project data until the pin has matched.
            headers = {"Content-Type": "application/json"}
            if authenticated:
                headers["Authorization"] = "Bearer " + self.credentials["token"]
                headers["X-Device-ID"] = self.credentials["device_id"]
            data = (
                None
                if body is None
                else json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            )
            if data is not None and len(data) > MAX_BODY:
                raise WorkbenchError("payload_too_large", "请求超过 1 MiB，请缩小内容。")
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            payload = response.read(MAX_BODY * 4 + 1)
            if len(payload) > MAX_BODY * 4:
                raise WorkbenchError("payload_too_large", "主控响应过大，请缩小项目资料。")
            result = json.loads(payload)
            if response.status >= 400:
                error = result.get("error", {})
                raise WorkbenchError(
                    error.get("code", "network_error"), error.get("message", "设备请求失败。")
                )
            return result
        except WorkbenchError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException, ValueError, UnicodeError) as exc:
            raise WorkbenchError(
                "network_error", "无法连接主控设备，请检查地址、网络和 TLS 监听。"
            ) from exc
        finally:
            connection.close()

    def device(self):
        return self._request("GET", "/v1/device")

    def projects(self):
        return self._request("GET", "/v1/projects")["projects"]

    def project_tool(self, project_id, employee_id, tool, args):
        project_id = _identifier(project_id, "项目编号")
        return self._request(
            "POST",
            f"/v1/projects/{project_id}/tools",
            {"employee_id": employee_id, "tool": tool, "args": args},
        )

    def context(self, project_id):
        project_id = _identifier(project_id, "项目编号")
        return self._request("GET", f"/v1/projects/{quote(project_id, safe='')}/context")

    def register_employee(self, project_id, name, kind):
        return self._request(
            "POST", "/v1/employees", {"project_id": project_id, "name": name, "kind": kind}
        )["employee"]

    def map_project(self, project_id, path):
        self.context(project_id)  # Validate authorization before persisting a local mapping.
        try:
            directory = Path(path).expanduser().resolve(strict=True)
        except (OSError, ValueError, TypeError) as exc:
            raise WorkbenchError("invalid_path", "请选择本设备已有的项目目录。") from exc
        if not directory.is_dir():
            raise WorkbenchError("invalid_path", "本设备项目路径需要是目录。")
        self.mappings[project_id] = str(directory)
        _write_private(self.mapping_path, json.dumps(self.mappings).encode("utf-8"))
        return {"project_id": project_id, "path": str(directory)}

    def local_project(self, project_id):
        context = self.context(project_id)
        path = self.mappings.get(project_id)
        if path is None or not Path(path).is_dir():
            raise WorkbenchError("project_unmapped", "请先为该项目选择本设备工作目录。")
        return {**context["project"], "path": path}

    def claim(self, project_id, wait=30):
        if not isinstance(wait, int) or isinstance(wait, bool) or not 0 <= wait <= 45:
            raise WorkbenchError("invalid_field", "领取等待时间需要在 0 到 45 秒之间。")
        self.local_project(project_id)  # Never claim work without an explicit local directory.
        return self._request(
            "POST", "/v1/tasks/claim", {"project_id": project_id, "wait": wait}, timeout=wait + 10
        )["task"]

    def event(self, task_id, run_id, type, message="", payload=None):
        task_id = _identifier(task_id, "任务编号")
        return self._request(
            "POST",
            f"/v1/tasks/{task_id}/events",
            {"run_id": run_id, "type": type, "message": message, "payload": payload},
        )["event"]

    def receipt(self, task_id, run_id, status, result="", error=None):
        task_id = _identifier(task_id, "任务编号")
        return self._request(
            "POST",
            f"/v1/tasks/{task_id}/receipt",
            {"run_id": run_id, "status": status, "result": result, "error": error},
        )["task"]

    def control(self, task_id, run_id, wait=30):
        task_id = _identifier(task_id, "任务编号")
        if not isinstance(wait, int) or isinstance(wait, bool) or not 0 <= wait <= 45:
            raise WorkbenchError("invalid_field", "控制等待时间需要在 0 到 45 秒之间。")
        return self._request(
            "POST",
            f"/v1/tasks/{task_id}/control",
            {"run_id": run_id, "wait": wait},
            timeout=wait + 10,
        )
