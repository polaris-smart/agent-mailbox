"""Real Python wake -> Node hook -> Unix socket -> receipt; no model invocation."""

import json
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from agent_mailbox import workbench_wake as wake

NODE = shutil.which("node")
ADAPTER = Path(__file__).resolve().parents[1] / "integrations" / "dsh-mailbox-host"
pytestmark = pytest.mark.skipif(
    not NODE or sys.platform == "win32", reason="Node/Unix socket required"
)


@pytest.mark.parametrize("online", [False, True])
def test_wake_uses_real_adapter_receipt(monkeypatch, online):
    # AF_UNIX paths have a short OS limit; don't use pytest's deeply nested path.
    with tempfile.TemporaryDirectory(prefix="am-dsh-", dir="/tmp") as directory:
        home = Path(directory)
        os.chmod(home, 0o700)
        socket = home / "s"
        binding = {
            "id": "binding_test",
            "employee_id": "employee_test",
            "project_id": "project_test",
            "host_kind": "deepseek",
            "host_session_id": "existing_test_session",
        }
        fixture = home / "serve.mjs"
        fixture.write_text(
            f"import {{startReceiver,createReceiver}} from {json.dumps((ADAPTER / 'index.mjs').as_uri())};\n"
            "import fs from 'node:fs/promises';\n"
            f"const binding={json.dumps(binding)};\n"
            "const receive=createReceiver({loadBindings:async()=>[binding],sessionController:{\n"
            "prompt:async r=>{await fs.appendFile(process.argv[3],JSON.stringify(r)+'\\n');return {accepted:true}}}});\n"
            "await startReceiver({socketPath:process.argv[2],receive});console.log('ready');\n"
        )
        process = None
        try:
            if online:
                process = subprocess.Popen(
                    [NODE, str(fixture), str(socket), str(home / "admissions.jsonl")],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    assert selector.select(timeout=10), "receiver did not start"
                    assert process.stdout.readline().strip() == "ready"
            monkeypatch.setattr(
                wake,
                "hook_command",
                lambda _: [NODE, str(ADAPTER / "client.mjs"), str(home), str(socket)],
            )
            send = wake.hook_deliver(home, require_receipt=True, expected_binding=binding)
            for _ in range(2):
                assert (
                    send("employee_test", {"fresh": ["message_test"], "reason": "test"}) is online
                )
            claims = home / "wake" / "claims"
            assert (claims / "message_test.ok").exists() is online
            assert not (claims / "message_test.unknown").exists()
            if online:
                calls = [
                    json.loads(line)
                    for line in (home / "admissions.jsonl").read_text().splitlines()
                ]
                assert len(calls) == 1
                assert calls[0]["sessionId"] == binding["host_session_id"]
                assert calls[0]["mode"] == "queue"
            else:
                assert (claims / "message_test.pending").exists()
        finally:
            if process is not None:
                process.terminate()
                process.communicate(timeout=10)
