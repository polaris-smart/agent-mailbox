"""Existing employee sessions use HTTP mailbox tools, never the workbench database."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .workbench_private import private_access


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _private_json(path: Path) -> dict:
    try:
        if path.is_symlink() or not path.is_file() or not private_access(path, 0o600):
            raise ValueError
        if path.stat().st_size > 65536:
            raise ValueError
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise TypeError
        return value
    except (OSError, ValueError, TypeError):
        raise ToolError(
            "MAILBOX_CONFIG_INVALID: Check the private mailbox connection file."
        ) from None


def build_server(session_file: str | Path) -> MCPServer:
    manifest = _private_json(Path(session_file))
    required = ("token", "home", "employee_id", "project_id", "session_id")
    if any(not isinstance(manifest.get(key), str) or not manifest[key] for key in required):
        raise ToolError("MAILBOX_CONFIG_INVALID: Incomplete mailbox connection file.")
    token = manifest["token"]
    if any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise ToolError("MAILBOX_CONFIG_INVALID: Invalid mailbox credential.")
    home = Path(manifest["home"])
    if not home.is_absolute():
        raise ToolError("MAILBOX_CONFIG_INVALID: Mailbox home must be absolute.")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def call(tool: str, args: dict) -> dict:
        instance = _private_json(home / "workbench" / "instance.json")
        endpoint = instance.get("endpoint", "")
        match = (
            re.fullmatch(r"http://127\.0\.0\.1:([0-9]{1,5})", endpoint)
            if isinstance(endpoint, str)
            else None
        )
        if not match or not 1 <= int(match[1]) <= 65535:
            raise ToolError("MAILBOX_CONFIG_INVALID: Invalid local workbench address.")
        request = urllib.request.Request(
            endpoint + "/api/workbench/mailbox/tools",
            data=json.dumps({"tool": tool, "args": args}).encode("utf-8"),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with opener.open(request, timeout=10) as response:
                payload = response.read(2 * 1024 * 1024 + 1)
                if len(payload) > 2 * 1024 * 1024:
                    raise ValueError
                result = json.loads(payload)
                if not isinstance(result, dict):
                    raise TypeError
                return result
        except urllib.error.HTTPError as exc:
            known = {
                "RESOURCE_NOT_APPROVED": "Ask the human to approve a shared resource revision.",
                "MAILBOX_ARGUMENT_DENIED": "This argument is not allowed for an employee mailbox.",
                "MAILBOX_TOOL_DENIED": "This tool is not available in an employee mailbox.",
                "EXPLICIT_CONTENT_REQUIRED": "Provide proposal content explicitly.",
                "invalid_field": "Check the mailbox tool arguments.",
            }
            try:
                failure = json.loads(exc.read(65537))
                error = failure.get("error", {}) if isinstance(failure, dict) else {}
                code = error.get("code") if isinstance(error, dict) else None
            except (OSError, ValueError, TypeError):
                code = None
            finally:
                exc.close()
            if isinstance(code, str) and code in known:
                raise ToolError(f"{code}: {known[code]}") from None
            if exc.code in (401, 403):
                raise ToolError("UNAUTHORIZED: Reconnect this employee mailbox session.") from None
            raise ToolError(
                "MAILBOX_UNAVAILABLE: Workbench rejected the mailbox request."
            ) from None
        except (OSError, ValueError, TypeError):
            raise ToolError("MAILBOX_UNAVAILABLE: Open the workbench and retry.") from None

    server = MCPServer(
        "agent-mailbox-project",
        instructions=(
            "Use project_context first. This connection belongs to one employee session and project. "
            "After accepting a task, read its resource_manifest with explicit version_id to use the pinned instructions. Read approved shared resources by default. Ordinary mail and replies never start tasks. "
            "Viewing mail is not acknowledgment or completion. Human acceptance is separate. "
            "Never share this session file or tools with subagents; subagents report to you. "
            "Authorship identifies the registered session, not independently verified internal authorship."
        ),
    )

    @server.tool()
    def project_context() -> dict:
        """Read project members, responsibilities and shared context."""
        return call("context", {})

    @server.tool()
    def project_messages(folder: str = "inbox", limit: int = 100) -> dict:
        """Read my mailbox without marking messages read or starting work."""
        return call("messages", {"folder": folder, "limit": limit})

    @server.tool()
    def project_message(
        title: str, body: str, recipient_id: str, reply_to: str = "", request_id: str = ""
    ) -> dict:
        """Send or reply as this employee; request_id makes repeated sends identifiable."""
        return call(
            "message",
            {
                "title": title,
                "body": body,
                "recipient_id": recipient_id,
                "reply_to": reply_to,
                "request_id": request_id,
            },
        )

    @server.tool()
    def project_note(title: str, body: str) -> dict:
        """Submit a project note; it does not become a human decision."""
        return call("note", {"title": title, "body": body})

    @server.tool()
    def project_graft_ask(task: str) -> dict:
        """只读：问仓库结构（graft ask）。**不触发受管执行**、不改任何状态。"""
        return call("graft_ask", {"task": task})

    @server.tool()
    def project_graft_callers(symbol: str) -> dict:
        """只读：谁调用/被谁调用（graft callers）。**不触发受管执行**。"""
        return call("graft_callers", {"symbol": symbol})

    @server.tool()
    def project_aoci_doctor() -> dict:
        """只读：认知层健康检查（aoci doctor）。**不触发受管执行**。"""
        return call("aoci_doctor", {})

    @server.tool()
    def project_aoci_status() -> dict:
        """只读：条目数/基线/漂移（aoci status）。**不触发受管执行**。"""
        return call("aoci_status", {})

    @server.tool()
    def project_aoci_check() -> dict:
        """只读：治理 findings 明细（aoci check）。**不触发受管执行**。"""
        return call("aoci_check", {})

    @server.tool()
    def project_memory_search(query: str) -> dict:
        """Search this project's shared notes."""
        return call("memory_search", {"query": query})

    @server.tool()
    def project_resource_read(resource_id: str, version_id: str = "", live: bool = False) -> dict:
        """Read approved resource content; unapproved live source is unavailable here."""
        if live:
            raise ToolError(
                "MAILBOX_ARGUMENT_DENIED: Existing sessions read approved resource revisions only."
            )
        return call("resource_read", {"resource_id": resource_id, "version_id": version_id})

    @server.tool()
    def project_resource_versions(resource_id: str) -> dict:
        """List resource revisions and approval status."""
        return call("resource_versions", {"resource_id": resource_id})

    @server.tool()
    def project_resource_propose(resource_id: str, content: str, summary: str = "") -> dict:
        """Propose a resource revision for human approval."""
        return call(
            "resource_propose", {"resource_id": resource_id, "summary": summary, "content": content}
        )

    @server.tool()
    def project_tasks() -> dict:
        """Read tasks assigned to me for handling in my existing App/CLI."""
        return call("tasks", {})

    @server.tool()
    def project_task_accept(task_id: str) -> dict:
        """Explicitly accept my mailbox task; reading alone never accepts it."""
        return call("task_accept", {"task_id": task_id})

    @server.tool()
    def project_task_submit(task_id: str, result: str) -> dict:
        """Submit my mailbox task result for Human review, without acceptance or code apply."""
        return call("task_submit", {"task_id": task_id, "result": result})

    @server.tool()
    def project_delivery(task_id: str) -> dict:
        """Read a colleague's fixed project delivery without accepting it."""
        return call("delivery", {"target_task_id": task_id})

    return server


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Connect an existing employee to its project mailbox"
    )
    parser.add_argument("--session-file", type=Path, required=True)
    args = parser.parse_args(argv)
    # 规则 U5「错误自解释」：连不上时给"是什么 + 去哪修 + 一条命令"，不要抛裸 traceback。
    # 2026-10-04 实测：无效/缺失会话文件时打印栈回溯，宿主只看到一坨 traceback。
    try:
        server = build_server(args.session_file)
    except ToolError as exc:
        print(f"agent-mailbox: {exc}", file=sys.stderr)
        print(f"  · 会话文件：{args.session_file}", file=sys.stderr)
        print(
            "  · 它在哪来：工作台 → 员工详情 → 导出该员工的私有接入配置（会话 30 天有效）",
            file=sys.stderr,
        )
        print(
            "  · 自查命令：agent-mailbox mailbox-mcp --session-file <导出的文件>", file=sys.stderr
        )
        return 2
    server.run()
    return 0


if __name__ == "__main__":
    main()
