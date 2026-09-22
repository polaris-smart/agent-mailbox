"""End-to-end test: MCP stdio handshake -> register -> send -> check -> broadcast,
plus the v0.5 delivery-side dedup contract on the MCP tool surface.

Each run gets a FRESH temporary mail root: the run always sends the same
content, and v0.5's semantic-hash dedup would (correctly) suppress the
repeat against a leftover acked letter from a previous run — a persistent
scratch root made the E2E order-dependent once dedup shipped.
"""

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Use the interpreter running the tests (CI has no .venv); fall back to repo venv locally.
PY = os.path.join(ROOT, ".venv", "bin", "python")
if not os.path.exists(PY):
    PY = sys.executable


def main():
    mailroot = tempfile.mkdtemp(prefix="agent-mailbox-e2e-")
    ENV = {**os.environ, "AGENT_MAIL_HOME": mailroot}
    proc = subprocess.Popen(
        [PY, "-m", "agent_mailbox.server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, env=ENV,
    )

    def send(m):
        proc.stdin.write((json.dumps(m) + "\n").encode())
        proc.stdin.flush()

    # Cross-platform reader: select() on Windows cannot poll pipes, so a
    # background reader thread + queue is used on all platforms.
    q: queue.Queue = queue.Queue()
    threading.Thread(target=lambda: [q.put(proc.stdout.readline()) for _ in iter(int, 1)], daemon=True).start()

    def read_resp(timeout=10):
        try:
            line = q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("no MCP response in time")
        if line:
            return json.loads(line)
        raise TimeoutError("empty MCP response")

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "e2e", "version": "0"}}})
        resp = read_resp()
        assert resp["result"]["serverInfo"]["name"] == "agent-mailbox"
        print("initialize ok")
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "mailbox_register", "arguments": {"agent_id": "WB", "owner": "Workbuddy"}}})
        read_resp()
        print("register ok")

        send({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "mailbox_send", "arguments": {
                "from_id": "HS", "to": "WB", "subject": "fuel ready", "body": "1856"}}})
        read_resp()
        print("send ok")

        send({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
            "name": "mailbox_check", "arguments": {"agent_id": "WB"}}})
        resp = read_resp()
        assert "fuel ready" in resp["result"]["content"][0]["text"]
        print("check ok — message delivered over MCP stdio")

        # v0.5 design A on the tool surface: the same content again is
        # deduped for that recipient — zero side effects, count excludes it.
        send({"jsonrpc": "2.0", "id": 15, "method": "tools/call", "params": {
            "name": "mailbox_send", "arguments": {
                "from_id": "HS", "to": "WB", "subject": "fuel ready", "body": "1856"}}})
        resp = json.loads(read_resp()["result"]["content"][0]["text"])
        assert resp["count"] == 0 and resp["delivered"][0]["deduped"] is True
        assert resp["delivered"][0]["existing_id"].endswith("-wb")
        print("dedupe ok — repeat suppressed with deduped/existing_id, count 0")

        send({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {
            "name": "mailbox_broadcast", "arguments": {"from_id": "boss", "subject": "hi", "body": "all"}}})
        read_resp()
        print("broadcast ok")

        send({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
            "name": "task_create", "arguments": {
                "from_id": "HS", "title": "ship v0.3.0", "assignee": "WB",
                "due": "2026-09-10"}}})
        resp = read_resp()
        task = json.loads(resp["result"]["content"][0]["text"])["task"]
        assert task["status"] == "todo"
        tid = task["id"]
        print(f"task_create ok ({tid})")

        send({"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {
            "name": "task_move", "arguments": {"from_id": "HS", "task_id": tid, "status": "review"}}})
        resp = read_resp()
        assert resp["result"].get("isError") is True
        print("task_move illegal skip rejected ok")

        send({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {
            "name": "task_move", "arguments": {"from_id": "HS", "task_id": tid, "status": "doing"}}})
        read_resp()
        send({"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {
            "name": "task_move", "arguments": {"from_id": "HS", "task_id": tid, "status": "review", "note": "please verify"}}})
        read_resp()
        print("task_move todo→doing→review ok")

        send({"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {
            "name": "mailbox_check", "arguments": {"agent_id": "WB"}}})
        resp = read_resp()
        text = resp["result"]["content"][0]["text"]
        assert f"[task#{tid} → review] ship v0.3.0" in text
        assert "please verify" in text
        print("auto-notification delivered over MCP stdio ok")

        send({"jsonrpc": "2.0", "id": 12, "method": "tools/call", "params": {
            "name": "task_list", "arguments": {"assignee": "WB", "status": "review"}}})
        resp = read_resp()
        assert tid in resp["result"]["content"][0]["text"]
        print("task_list filter ok")

        # B4 self-echo (2026-09-09 requirement): WB sends to itself — the
        # letter lands (mailbox_list sees it) but the wake-up notification is
        # suppressed by default and audited in sent.log as echo_suppressed.
        send({"jsonrpc": "2.0", "id": 13, "method": "tools/call", "params": {
            "name": "mailbox_send", "arguments": {
                "from_id": "WB", "to": "WB", "subject": "self echo probe", "body": "b4"}}})
        read_resp()
        send({"jsonrpc": "2.0", "id": 14, "method": "tools/call", "params": {
            "name": "mailbox_list", "arguments": {"agent_id": "WB"}}})
        resp = read_resp()
        assert "self echo probe" in resp["result"]["content"][0]["text"]
        sent_log = os.path.join(mailroot, "sent.log")
        last = None
        with open(sent_log, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last = line
        entry = json.loads(last)
        assert entry["subject"] == "self echo probe"
        assert entry.get("echo_suppressed") is True
        print("self-echo suppressed ok (letter on disk, wake-up dropped, audited)")
        print("E2E PASS")
    finally:
        proc.kill()
        proc.wait()
        import shutil
        shutil.rmtree(mailroot, ignore_errors=True)


if __name__ == "__main__":
    main()
