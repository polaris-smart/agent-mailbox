"""End-to-end test: MCP stdio handshake -> register -> send -> check -> broadcast."""

import json
import os
import select
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = {**os.environ, "AGENT_MAIL_HOME": os.path.join(ROOT, ".test-mail")}
# Use the interpreter running the tests (CI has no .venv); fall back to repo venv locally.
PY = os.path.join(ROOT, ".venv", "bin", "python")
if not os.path.exists(PY):
    PY = sys.executable


def main():
    proc = subprocess.Popen(
        [PY, "-m", "agent_mailbox.server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, env=ENV,
    )

    def send(m):
        proc.stdin.write((json.dumps(m) + "\n").encode())
        proc.stdin.flush()

    def read_resp(timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            r, _, _ = select.select([proc.stdout], [], [], 0.3)
            if r:
                line = proc.stdout.readline()
                if line:
                    return json.loads(line)
        raise TimeoutError("no MCP response in time")

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

        send({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {
            "name": "mailbox_broadcast", "arguments": {"from_id": "boss", "subject": "hi", "body": "all"}}})
        read_resp()
        print("broadcast ok")
        print("E2E PASS")
    finally:
        proc.kill()


if __name__ == "__main__":
    main()
