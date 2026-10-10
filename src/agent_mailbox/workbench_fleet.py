"""Explicit, certificate-pinned HTTPS between workbench device nodes.

The listener serves only device-scoped routes. Pairing grants project access,
never human APIs, arbitrary shell commands or blanket host access. Invitations
are one-use secrets; their certificate fingerprint must be delivered through a
trusted channel alongside the invitation.
"""

from __future__ import annotations

import _socket
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from urllib.parse import quote, urlsplit

from . import __version__
from .workbench_compatibility import FLEET_PROTOCOL, node_report
from .workbench_execution_resources import execution_project_context
from .workbench_private import private_mode
from .workbench_store import ACTIVE, WorkbenchError, WorkbenchStore

MAX_BODY = 1024 * 1024
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
FINGERPRINT_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")
REMOTE_EVENTS = frozenset({"started", "session", "event", "output", "error", "permission_denied"})


def permission_summary(options, tool_call):
    """Only ACP choice labels and a concise tool description cross devices."""
    if not isinstance(options, list) or not 1 <= len(options) <= 32:
        raise WorkbenchError("invalid_field", "权限请求需要有效的选项。")
    clean_options = []
    seen = set()
    for option in options:
        if not isinstance(option, dict):
            raise WorkbenchError("invalid_field", "权限选项格式无效。")
        option_id = _string(option.get("optionId"), "权限选项编号", 200)
        kind = option.get("kind")
        if (
            option_id in seen
            or not isinstance(kind, str)
            or kind
            not in {
                "allow_once",
                "allow_always",
                "reject_once",
                "reject_always",
            }
        ):
            raise WorkbenchError("invalid_field", "权限选项无效或重复。")
        seen.add(option_id)
        clean_options.append(
            {
                "optionId": option_id,
                "name": _string(option.get("name"), "权限选项说明", 300),
                "kind": kind,
            }
        )
    if not isinstance(tool_call, dict):
        raise WorkbenchError("invalid_field", "权限请求需要工具说明。")
    clean_tool = {
        key: _string(tool_call[key], "工具说明", 300)
        for key in ("title", "kind", "toolCallId")
        if key in tool_call
    }
    if not clean_tool.get("title"):
        raise WorkbenchError("invalid_field", "权限请求需要工具标题。")
    return clean_options, clean_tool


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
            private_mode(temporary, 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        private_mode(path, 0o600)
    except OSError as exc:
        raise WorkbenchError("storage_error", "无法保存设备凭据，请检查磁盘和目录权限。") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _directory(root):
    directory = Path(root) / "workbench" / "fleet"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_mode(directory, 0o700)
    return directory


def _load(path, fallback):
    if not path.exists():
        return fallback
    try:
        private_mode(path, 0o600)
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
        private_mode(key_path, 0o600)
        private_mode(cert_path, 0o600)
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

                def server_bind(self):
                    # Numeric endpoints do not need reverse DNS to start serving.
                    TCPServer.server_bind(self)
                    self.server_name, self.server_port = self.server_address[:2]

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
        import base64

        invite_id = _identifier(body.get("invite_id"), "邀请码编号")
        secret = _string(body.get("invite_secret"), "邀请码", 200)
        device_id = _identifier(body.get("device_id"), "设备编号")
        name = _string(body.get("name"), "设备名称", 200)
        pair_token = body.get("pair_token")
        if pair_token is not None:
            if not isinstance(pair_token, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{43}", pair_token
            ):
                raise WorkbenchError("invalid_field", "配对凭据需要是 256 位随机密钥。")
            raw_token = base64.urlsafe_b64decode(pair_token + "=")
            if (
                len(raw_token) != 32
                or base64.urlsafe_b64encode(raw_token).decode().rstrip("=") != pair_token
            ):
                raise WorkbenchError("invalid_field", "配对凭据格式无效。")
        with self.lock:
            invitation = self.state["invites"].get(invite_id)
            expected = invitation["secret_hash"] if invitation else "0" * 64
            valid = hmac.compare_digest(expected, _digest(secret))
            if not invitation or not valid:
                raise WorkbenchError("permission_denied", "邀请码验证失败。")
            existing = self.state["devices"].get(device_id)
            if invitation["used"]:
                if (
                    pair_token is not None
                    and existing is not None
                    and not existing.get("revoked", False)
                    and existing.get("pair_invite_id") == invite_id
                    and hmac.compare_digest(existing["token_hash"], _digest(pair_token))
                ):
                    return {
                        "device_id": device_id,
                        "token": pair_token,
                        "project_ids": list(existing["project_ids"]),
                        "coordinator_id": self.store.local_node()["id"],
                    }
                raise WorkbenchError("invite_used", "邀请码已使用，请生成新的邀请码。")
            if invitation["expires_at"] <= time.time():
                raise WorkbenchError("invite_expired", "邀请码已过期，请生成新的邀请码。")
            if device_id == self.store.local_node()["id"] or (
                existing is not None and not existing.get("revoked", False)
            ):
                raise WorkbenchError("permission_denied", "该设备已有身份，请使用现有凭据。")
            if self.store.update_maintenance()["paused"]:
                raise WorkbenchError("UPDATE_PAUSED", "主控正在准备更新，请恢复接单后再加入设备。")
            token = pair_token or secrets.token_urlsafe(32)
            self.store.upsert_device(
                device_id, name, "online", datetime.now(timezone.utc).isoformat()
            )
            self.state["devices"][device_id] = {
                "device_id": device_id,
                "name": name,
                "token_hash": _digest(token),
                "pair_invite_id": invite_id if pair_token is not None else None,
                "project_ids": invitation["project_ids"],
                "paired_at": time.time(),
                "revoked": False,
            }
            invitation["used"] = True
            self._save()
            self.notify()
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
            if "x-agent-mail-version" in headers or "x-agent-mail-protocol" in headers:
                report = node_report(
                    headers.get("x-agent-mail-version"), headers.get("x-agent-mail-protocol")
                )
                if (
                    device.get("version") != report["version"]
                    or device.get("protocol") != report["protocol"]
                ):
                    device.update(report)
                    self._save()
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

    def _permission_task(self, device, task_id, body):
        task = self._task_scope(device, task_id, body.get("run_id"))
        if body.get("employee_id") != task["assignee_id"]:
            raise WorkbenchError("permission_denied", "权限请求不属于这位员工。")
        self.store.employee_credentials(task["assignee_id"], task["project_id"])
        return task

    def _permission_decision(self, device, task_id, request_id, headers, body):
        wait = body.get("wait", 120)
        if not isinstance(wait, int) or isinstance(wait, bool) or not 0 <= wait <= 120:
            raise WorkbenchError("invalid_field", "审批等待时间需要在 0 到 120 秒之间。")
        deadline = time.monotonic() + wait
        with self.condition:
            while True:
                try:
                    self._authenticate(headers)
                    self._permission_task(device, task_id, body)
                    permission = self.store.permission_decision(
                        task_id, request_id, body.get("run_id")
                    )
                except WorkbenchError:
                    self.store.expire_permission(task_id, request_id, body.get("run_id"))
                    self.notify()
                    raise
                status = permission["status"]
                if status != "pending":
                    decision = permission.get("decision") if status == "resolved" else "deny"
                    return {"status": status, "decision": decision or "deny"}
                expires = datetime.fromisoformat(permission["expires_at"].replace("Z", "+00:00"))
                expires_in = (expires - datetime.now(timezone.utc)).total_seconds()
                remaining = min(
                    deadline - time.monotonic(),
                    expires_in,
                )
                if remaining <= 0 or self.server is None:
                    if expires_in <= 0 or self.server is None:
                        self.store.expire_permission(task_id, request_id, body.get("run_id"))
                        self.notify()
                        return {"status": "expired", "decision": "deny"}
                    return {"status": "pending", "decision": None}
                self.condition.wait(timeout=remaining)

    def _context(self, project_id, employee_id=None, task_id="", run_id=""):
        result = execution_project_context(self.store, project_id, employee_id, task_id, run_id)
        result["project"].pop("path", None)
        for resource in result["resources"]:
            resource.pop("path", None)
        result["employees"] = [
            {
                key: row[key]
                for key in (
                    "id",
                    "name",
                    "kind",
                    "node_id",
                    "status",
                    "connection_type",
                    "execution_supported",
                    "execution_verified",
                )
            }
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
        self.store.employee_credentials(employee_id, project_id)
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
        source_task_id = None
        # Once a session identifies a task, every read and write is scoped to
        # that active run. Ended runs cannot fall back to device-wide read access.
        if body.get("task_id") or body.get("run_id"):
            source_task_id = _identifier(body.get("task_id"), "来源任务编号")
            source = self._task_scope(device, source_task_id, body.get("run_id"))
            if (
                source["assignee_id"] != employee_id
                or source["project_id"] != project_id
                or source["status"] not in ACTIVE
                or source["cancel_requested"]
            ):
                raise WorkbenchError("permission_denied", "工具必须属于当前员工的有效执行会话。")
        if tool == "context":
            return self._context(
                project_id,
                employee_id=employee_id,
                task_id=source_task_id or "",
                run_id=body.get("run_id", ""),
            )
        if tool == "code_search":
            result = self.store.query_knowledge(project_id, args.get("query"))
            # A remote employee receives a coordinator snapshot, not proof that
            # its mapped checkout has the same files or revision.
            provenance = result.setdefault("provenance", {})
            provenance.pop("repository_root", None)
            result.pop("repository_root", None)
            result.pop("project_path", None)
            result["index_scope"] = "coordinator_project"
            return result
        if tool == "memory_search":
            return {"memories": self.store.search_memory(project_id, args.get("query", ""))}
        if tool == "resource_read":
            if source_task_id:
                result = self.store.read_execution_resource(
                    project_id,
                    args.get("resource_id"),
                    source_task_id,
                    body.get("run_id"),
                    args.get("version_id", ""),
                    args.get("live", False),
                )
            elif args.get("version_id") and not args.get("live", False):
                result = self.store.read_resource_version(
                    project_id, args.get("resource_id"), args["version_id"]
                )
            else:
                result = self.store.read_resource(project_id, args.get("resource_id"))
            result["resource"].pop("path", None)
            if "version" in result:
                result["version"].pop("source", None)
            result["source"] = f"resource:{result['resource']['id']}:{result['resource']['name']}"
            return result
        if tool == "task_delivery":
            from .workbench_execution_resources import project_task_delivery

            return project_task_delivery(self.store, project_id, args.get("target_task_id"))
        if tool == "resource_versions":
            result = self.store.resource_versions(project_id, args.get("resource_id"))
            for version in result["versions"]:
                version.pop("source", None)
            return result
        if tool == "messages":
            return self.store.employee_messages(
                project_id, employee_id, args.get("folder", "inbox"), args.get("limit", 100)
            )
        if tool in {"note", "message", "team_message", "resource_propose"}:
            if not body.get("task_id") or not body.get("run_id"):
                raise WorkbenchError("permission_denied", "写入工具需要当前执行会话。")
            source_task_id = _identifier(body.get("task_id"), "来源任务编号")
            source = self._task_scope(device, source_task_id, body.get("run_id"))
            if (
                source["assignee_id"] != employee_id
                or source["project_id"] != project_id
                or source["status"] not in ACTIVE
            ):
                raise WorkbenchError(
                    "permission_denied", "工具写入必须属于当前员工的有效执行会话。"
                )
        if tool == "resource_propose":
            result = self.store.capture_resource_version(
                project_id,
                args.get("resource_id"),
                args.get("summary", ""),
                employee_id,
                content=args.get("content"),
            )
            result.pop("source", None)
            self.notify()
            return result
        if tool == "note":
            return self.store.add_memory(
                project_id, args.get("title"), args.get("body"), source=f"employee:{employee_id}"
            )
        if tool in {"message", "team_message"}:
            if not str(args.get("recipient_id") or "").strip():
                raise WorkbenchError(
                    "invalid_field",
                    "发消息必须指定收件人（recipient_id 非空）；员工不能群发全项目。",
                )
            saved = self.store.send_message(
                project_id,
                args.get("title"),
                args.get("message") if tool == "team_message" else args.get("body"),
                recipient_id=args.get("recipient_id"),
                sender_id=employee_id,
                reply_to=args.get("reply_to"),
                request_work=tool == "team_message",
                request_id=args.get("request_id"),
                source_task_id=source_task_id,
            )
            self.notify()
            return {**saved, "status": "queued" if saved.get("task_id") else "delivered"}
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
        match = re.fullmatch(
            r"/v1/tasks/([A-Za-z0-9_-]+)/permissions(?:/([A-Za-z0-9_-]{1,200})/(decision|expire))?",
            path,
        )
        if method == "POST" and match:
            task_id, request_id, action = match.groups()
            self._permission_task(device, task_id, body)
            if request_id:
                if action == "expire":
                    self.store.expire_permission(task_id, request_id, body.get("run_id"))
                    self.notify()
                    return {"status": "expired", "decision": "deny"}
                return self._permission_decision(device, task_id, request_id, headers, body)
            request_id = _string(body.get("request_id"), "权限请求编号", 200)
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", request_id):
                raise WorkbenchError("invalid_field", "权限请求编号无效。")
            options, tool_call = permission_summary(body.get("options"), body.get("tool_call"))
            permission = self.store.request_permission(task_id, request_id, options, tool_call)
            self.notify()
            return {"status": permission["status"], "expires_at": permission["expires_at"]}
        if method == "GET" and path == "/v1/device":
            return {
                "device": {
                    key: device[key] for key in ("device_id", "name", "project_ids", "paired_at")
                },
                "coordinator": {
                    **self.store.local_node(),
                    "version": __version__,
                    "protocol": FLEET_PROTOCOL,
                },
            }
        if method == "GET" and path == "/v1/projects":
            return {
                "projects": [
                    {"id": row["id"], "name": row["name"]}
                    for row in self.store.snapshot()["projects"]
                    if row["id"] in device["project_ids"]
                ]
            }
        if method == "GET" and path == "/v1/tasks/active":
            return {
                "tasks": [
                    {
                        key: task[key]
                        for key in ("id", "run_id", "project_id", "status", "cancel_requested")
                    }
                    for task in self.store.snapshot()["tasks"]
                    if task["node_id"] == device["device_id"]
                    and task["project_id"] in device["project_ids"]
                    and task["status"] in ACTIVE
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
            self.notify()
            return {"employee": employee}
        if method == "POST" and path == "/v1/tasks/claim":
            self._compatible_claim(device)
            project_id = body.get("project_id")
            self._scope(device, project_id)
            wait = body.get("wait", 0)
            if not isinstance(wait, int) or isinstance(wait, bool) or not 0 <= wait <= 45:
                raise WorkbenchError("invalid_field", "领取等待时间需要在 0 到 45 秒之间。")
            # Hold the condition lock through the first DB claim. notify cannot
            # be lost between observing an empty queue and registering the wait.
            with self.condition:
                # Revocation may have happened after the route's initial authentication.
                # Revalidate under the same lock that protects revocation and claiming.
                device = self._authenticate(headers)
                self._scope(device, project_id)
                self._compatible_claim(device)
                task = self.store.claim_task(device["device_id"], project_ids=[project_id])
                if task is None and wait and self.server is not None:
                    self.condition.wait(timeout=wait)
                    if self.server is not None:
                        device = self._authenticate(headers)
                        self._scope(device, project_id)
                        self._compatible_claim(device)
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
            if action == "events":
                if task["status"] not in ACTIVE:
                    raise WorkbenchError("invalid_state", "这次执行已结束，不能再提交进度。")
                event_type = body.get("type")
                if not isinstance(event_type, str) or event_type not in REMOTE_EVENTS:
                    raise WorkbenchError("invalid_field", "这个事件不能由远端设备提交。")
                if event_type == "started":
                    self.store.set_status(task_id, "running")
                event = self.store.add_event(
                    task_id, event_type, body.get("message", ""), body.get("payload")
                )
                self.notify()
                return {"event": event}
            receipt = {
                "task": self.store.finish_remote_task(
                    task_id,
                    body.get("run_id"),
                    body.get("status"),
                    body.get("result", ""),
                    body.get("error"),
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

    @staticmethod
    def _compatible_claim(device):
        if device.get("protocol") not in {None, FLEET_PROTOCOL}:
            raise WorkbenchError(
                "protocol_incompatible", "节点协议不兼容，请更新后再领取新任务；已有任务仍可回执。"
            )

    def public_devices(self, include_reports=False):
        """Owner UI metadata; no token hashes, secrets or invitation records."""
        with self.lock:
            return [
                {
                    "id": device_id,
                    "revoked": bool(record.get("revoked", False)),
                    **(
                        {
                            key: value
                            for key, value in node_report(
                                record.get("version"), record.get("protocol")
                            ).items()
                            if include_reports and key in record
                        }
                    ),
                }
                for device_id, record in self.state["devices"].items()
            ]


class FleetClient:
    def __init__(self, root: Path, invite: dict):
        self.store = WorkbenchStore(root)
        self.claim_lock = threading.RLock()
        self.journal_lock = threading.RLock()
        self.connection_lock = threading.Lock()
        self.claim_connection = None
        self.claim_socket = None
        self.claim_cancellation = threading.local()
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
            attempt_path = self.directory / "pair-attempt.json"
            attempt = _load(attempt_path, None)
            binding = {
                "base_url": self.base_url,
                "fingerprint": self.fingerprint,
                "invite_id": _identifier(invite.get("invite_id"), "邀请码编号"),
                "device_id": node["id"],
            }
            if (
                isinstance(attempt, dict)
                and all(
                    attempt.get(key) == value for key, value in binding.items() if key != "base_url"
                )
                and attempt.get("base_url") != self.base_url
            ):
                # Changing an SSH tunnel address must preserve the pinned
                # coordinator, invitation and previously persisted proof.
                attempt = {**attempt, "base_url": self.base_url}
                _write_private(attempt_path, json.dumps(attempt).encode("utf-8"))
            if attempt is not None and (
                not isinstance(attempt, dict)
                or any(attempt.get(key) != value for key, value in binding.items())
                or not isinstance(attempt.get("pair_token"), str)
            ):
                raise WorkbenchError(
                    "storage_error", "还有未确认的配对，请使用原邀请码与地址重试，勿覆盖设备身份。"
                )
            created_attempt = attempt is None
            if created_attempt:
                attempt = {**binding, "pair_token": secrets.token_urlsafe(32)}
                _write_private(attempt_path, json.dumps(attempt).encode("utf-8"))
            try:
                paired = self._request(
                    "POST",
                    "/v1/pair",
                    {
                        "invite_id": invite.get("invite_id"),
                        "invite_secret": invite.get("invite_secret"),
                        "device_id": node["id"],
                        "name": node["name"],
                        "pair_token": attempt["pair_token"],
                    },
                    authenticated=False,
                )
            except WorkbenchError as exc:
                if (
                    created_attempt
                    and exc.code == "tls_pin_mismatch"
                    or exc.code in {"invite_expired", "invite_used"}
                ):
                    # The pin check precedes sending any secret, so this fresh
                    # attempt never granted an identity. Explicit invite expiry
                    # or refusal cannot be recovered with this proof either.
                    attempt_path.unlink(missing_ok=True)
                raise
            if (
                paired.get("device_id") != node["id"]
                or not isinstance(paired.get("token"), str)
                or not hmac.compare_digest(paired["token"], attempt["pair_token"])
            ):
                raise WorkbenchError("protocol_error", "主控未确认这次设备身份，请重试原配对。")
            self.credentials = {
                **paired,
                "base_url": self.base_url,
                "fingerprint": self.fingerprint,
            }
            _write_private(
                self.credentials_path,
                json.dumps(self.credentials, ensure_ascii=False).encode("utf-8"),
            )
            attempt_path.unlink(missing_ok=True)
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
        response = None
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
                headers["X-Agent-Mail-Version"] = __version__
                headers["X-Agent-Mail-Protocol"] = FLEET_PROTOCOL
            data = (
                None
                if body is None
                else json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            )
            if data is not None and len(data) > MAX_BODY:
                raise WorkbenchError("payload_too_large", "请求超过 1 MiB，请缩小内容。")
            if path == "/v1/tasks/claim":
                with self.connection_lock:
                    stop = getattr(self.claim_cancellation, "event", None)
                    if stop is not None and stop.is_set():
                        raise WorkbenchError("claim_stopped", "设备已停止领取任务。")
                    self.claim_connection = connection
                    # HTTPConnection clears .sock for Connection: close responses
                    # before the response body finishes. Keep the actual transport
                    # so stopping can interrupt that read on every platform.
                    self.claim_socket = connection.sock
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
            with self.connection_lock:
                if self.claim_connection is connection:
                    self.claim_connection = None
                    self.claim_socket = None
            if response is not None:
                # Connection: close transfers the live makefile to the response;
                # closing the connection alone cannot release that I/O reference.
                response.close()
            connection.close()

    def interrupt_claim(self):
        # Stop only the pending claim transport, never receipts or running tools.
        with self.connection_lock:
            transport = self.claim_socket
            if transport:
                try:
                    # HTTPResponse may keep a live fd on an SSLSocket that
                    # HTTPConnection has marked closed. The SSL override clears
                    # _sslobj; keep that state intact for the concurrent reader
                    # and shut down only the native transport here.
                    _socket.socket.shutdown(transport, socket.SHUT_RDWR)
                except OSError:
                    pass
                if os.name == "nt":
                    # Winsock needs an actual close to cancel a pending recv;
                    # makefile references otherwise defer socket.close(). This
                    # native close invalidates the object fd, making subsequent
                    # cleanup safe. POSIX keeps the shutdown fd alive until the
                    # reader unwinds: closing concurrently can race its SSL
                    # select/read and descriptor reuse instead of waking it.
                    _socket.socket.close(transport)

    def device(self):
        return self._request("GET", "/v1/device")

    def projects(self):
        return self._request("GET", "/v1/projects")["projects"]

    def active_runs(self):
        # Ordinary status reads must not wait behind a 30-second claim poll.
        # Recovery may clear markers only when no claim transport is in flight.
        if not self.claim_lock.acquire(blocking=False):
            return self._request("GET", "/v1/tasks/active")["tasks"]
        try:
            tasks = self._request("GET", "/v1/tasks/active")["tasks"]
            pending = self.store.update_maintenance()["pending_claims"]
            if pending:
                self._journal_claims(tasks)
                for marker in pending:
                    self.store.end_update_claim(marker["id"])
            return tasks
        finally:
            self.claim_lock.release()

    def _journal_claims(self, tasks, handed_off=False):
        with self.journal_lock:
            path = self.directory / "active-runs.json"
            active = _load(path, {})
            if not isinstance(active, dict):
                raise WorkbenchError("storage_error", "设备执行记录格式无效，请恢复备份。")
            active.update({task["id"]: task["run_id"] for task in tasks})
            _write_private(path, json.dumps(active).encode("utf-8"))
            if handed_off and hasattr(self, "claimed_runs"):
                self.claimed_runs.update({task["id"]: task["run_id"] for task in tasks})

    def project_tool(self, project_id, employee_id, tool, args, *, task_id="", run_id=""):
        project_id = _identifier(project_id, "项目编号")
        return self._request(
            "POST",
            f"/v1/projects/{project_id}/tools",
            {
                "employee_id": employee_id,
                "tool": tool,
                "args": args,
                "task_id": task_id,
                "run_id": run_id,
            },
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
        with self.claim_lock:
            if self.store.update_maintenance()["paused"]:
                return None
            if self.store.update_maintenance()["pending_claims"]:
                raise WorkbenchError(
                    "claim_unconfirmed", "尚有未确认的任务领取，请先恢复连接并确认回执。"
                )
            marker = "claim_" + secrets.token_hex(16)
            if not self.store.begin_update_claim(marker):
                return None  # Maintenance pauses network claims too.
            try:
                self.local_project(project_id)  # Explicit local mapping is mandatory.
                coordinator = self.device().get("coordinator", {})
                report = node_report(coordinator.get("version"), coordinator.get("protocol"))
                if report["protocol"] not in {None, FLEET_PROTOCOL}:
                    raise WorkbenchError(
                        "protocol_incompatible", "主控协议不兼容，请更新后再领取新任务。"
                    )
            except BaseException:
                # No claim request was sent, therefore no uncertain execution.
                self.store.end_update_claim(marker)
                raise
            stop = getattr(self.claim_cancellation, "event", None)
            if stop is not None and stop.is_set():
                self.store.end_update_claim(marker)
                return None
            try:
                task = self._request(
                    "POST",
                    "/v1/tasks/claim",
                    {"project_id": project_id, "wait": wait},
                    timeout=wait + 10,
                )["task"]
            except WorkbenchError as exc:
                if exc.code in {
                    "permission_denied",
                    "protocol_incompatible",
                    "invalid_field",
                    "tls_pin_mismatch",
                    "claim_stopped",
                }:
                    # Definite rejection/pre-transmission pin failure allocated no run.
                    self.store.end_update_claim(marker)
                raise
            if task:
                self._journal_claims([task], handed_off=True)
            self.store.end_update_claim(marker)
            return task

    def event(self, task_id, run_id, type, message="", payload=None):
        task_id = _identifier(task_id, "任务编号")
        return self._request(
            "POST",
            f"/v1/tasks/{task_id}/events",
            {"run_id": run_id, "type": type, "message": message, "payload": payload},
        )["event"]

    def receipt(self, task_id, run_id, status, result="", error=None):
        task_id = _identifier(task_id, "任务编号")
        task = self._request(
            "POST",
            f"/v1/tasks/{task_id}/receipt",
            {"run_id": run_id, "status": status, "result": result, "error": error},
        )["task"]
        with self.journal_lock:
            path = self.directory / "active-runs.json"
            active = _load(path, {})
            if not isinstance(active, dict):
                raise WorkbenchError("storage_error", "设备执行记录格式无效，请恢复备份。")
            if active.get(task_id) == run_id:
                del active[task_id]
                _write_private(path, json.dumps(active).encode("utf-8"))
        return task

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

    def request_permission(self, task_id, run_id, employee_id, request_id, options, tool_call):
        task_id = _identifier(task_id, "任务编号")
        options, tool_call = permission_summary(options, tool_call)
        return self._request(
            "POST",
            f"/v1/tasks/{task_id}/permissions",
            {
                "run_id": run_id,
                "employee_id": employee_id,
                "request_id": request_id,
                "options": options,
                "tool_call": tool_call,
            },
        )

    def permission_decision(self, task_id, run_id, employee_id, request_id, wait=120):
        task_id = _identifier(task_id, "任务编号")
        if not isinstance(wait, int) or isinstance(wait, bool) or not 0 <= wait <= 120:
            raise WorkbenchError("invalid_field", "审批等待时间需要在 0 到 120 秒之间。")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", request_id):
            raise WorkbenchError("invalid_field", "权限请求编号无效。")
        return self._request(
            "POST",
            f"/v1/tasks/{task_id}/permissions/{request_id}/decision",
            {"run_id": run_id, "employee_id": employee_id, "wait": wait},
            timeout=wait + 10,
        )

    def expire_permission(self, task_id, run_id, employee_id, request_id):
        task_id = _identifier(task_id, "任务编号")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", request_id):
            raise WorkbenchError("invalid_field", "权限请求编号无效。")
        return self._request(
            "POST",
            f"/v1/tasks/{task_id}/permissions/{request_id}/expire",
            {"run_id": run_id, "employee_id": employee_id},
        )
