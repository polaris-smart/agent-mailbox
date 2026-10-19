"""Local Unix transport contract for an opened-mail DSH queue retraction."""

import json
import selectors
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from agent_mailbox.workbench_host_transport import retract_dsh_queue

NODE = shutil.which("node")
ADAPTER = Path(__file__).resolve().parents[1] / "integrations" / "dsh-mailbox-host" / "index.mjs"


def test_retraction_transport_sends_distinct_protocol_and_checks_exact_reply():
    binding = {
        "id": "binding_synthetic",
        "employee_id": "employee_synthetic",
        "project_id": "project_synthetic",
        "host_kind": "deepseek",
        "host_session_id": "existing-session",
    }
    payload = {
        "operation": "retract_opened",
        "receipt_protocol": "agent-mailbox.host-retraction.v1",
        "delivery_id": "wake-synthetic",
        "employee_id": binding["employee_id"],
        "binding": binding,
        "fresh": ["message_synthetic"],
    }
    seen = []
    with tempfile.TemporaryDirectory(prefix="amr-", dir="/private/tmp") as directory:
        private = Path(directory)
        private.chmod(0o700)
        endpoint = private / "dsh.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(endpoint))
            endpoint.chmod(0o600)
            server.listen(1)

            def serve():
                with server.accept()[0] as channel:
                    request = bytearray()
                    while b"\n" not in request:
                        request.extend(channel.recv(4096))
                    value = json.loads(request.split(b"\n", 1)[0])
                    seen.append(value)
                    response = {
                        "protocol": "agent-mailbox.host-retraction.v1",
                        "delivery_id": value["delivery_id"],
                        "binding_id": value["binding"]["id"],
                        "project_id": value["binding"]["project_id"],
                        "employee_id": value["employee_id"],
                        "message_ids": value["fresh"],
                        "status": "retracted",
                    }
                    channel.sendall(json.dumps(response).encode() + b"\n")

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            assert retract_dsh_queue(endpoint, payload) == "retracted"
            thread.join(3)
            assert not thread.is_alive()
    assert seen == [payload]


@pytest.mark.skipif(not NODE or sys.platform == "win32", reason="Node/Unix socket required")
def test_python_retraction_reaches_node_receiver_without_prompting_again():
    """The real Python wire client and Node receiver agree on the new protocol."""
    base = "/private/tmp" if sys.platform == "darwin" else "/tmp"
    with tempfile.TemporaryDirectory(prefix="amr-", dir=base) as directory:
        private = Path(directory)
        private.chmod(0o700)
        endpoint = private / "s"
        events = private / "events.jsonl"
        fixture = private / "receiver.mjs"
        source = (
            "import fs from 'node:fs';\n"
            "import {startReceiver,createReceiver} from __ADAPTER__;\n"
            "const binding={id:'binding_test',employee_id:'employee_test',"
            "project_id:'project_test',host_kind:'deepseek',host_session_id:'session_test'};\n"
            "const queue=[];\n"
            "const record=value=>fs.appendFileSync(process.argv[3],JSON.stringify(value)+'\\n');\n"
            "const receive=createReceiver({loadBindings:async()=>[binding],sessionController:{\n"
            "prompt:async request=>{record({kind:'prompt',requestId:request.requestId});"
            "queue.push({id:'queue_one',source:{kind:'user',rpcId:request.requestId}});"
            "return {accepted:true}},\n"
            "projections:async()=>({values:{inbox:{'next-turn':queue,'next-step':[]}}}),\n"
            "updateQueue:async request=>{record({kind:'remove',itemId:request.itemId});"
            "queue.splice(queue.findIndex(item=>item.id===request.itemId),1);"
            "return {accepted:true}},\n"
            "cancel:()=>{throw new Error('must_not_cancel')},\n"
            "}});\n"
            "await startReceiver({socketPath:process.argv[2],receive});console.log('ready');\n"
        )
        fixture.write_text(
            source.replace("__ADAPTER__", json.dumps(ADAPTER.as_uri())), encoding="utf-8"
        )
        binding = {
            "id": "binding_test",
            "employee_id": "employee_test",
            "project_id": "project_test",
            "host_kind": "deepseek",
            "host_session_id": "session_test",
        }
        original = {
            "receipt_protocol": "agent-mailbox.host-receipt.v1",
            "delivery_id": "wake-test",
            "employee_id": binding["employee_id"],
            "binding": binding,
            "fresh": ["message_test"],
        }
        process = subprocess.Popen(
            [NODE, str(fixture), str(endpoint), str(events)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                assert selector.select(timeout=10), "Node receiver did not become ready"
                assert process.stdout.readline().strip() == "ready"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                channel.connect(str(endpoint))
                channel.sendall(json.dumps(original).encode() + b"\n")
                received = bytearray()
                while b"\n" not in received:
                    received.extend(channel.recv(4096))
            assert json.loads(received)["status"] == "received"
            opened = {
                **original,
                "operation": "retract_opened",
                "receipt_protocol": "agent-mailbox.host-retraction.v1",
            }
            assert retract_dsh_queue(endpoint, opened) == "retracted"
            assert retract_dsh_queue(endpoint, opened) == "not_pending"
            assert [
                event["kind"] for event in map(json.loads, events.read_text().splitlines())
            ] == [
                "prompt",
                "remove",
            ]
        finally:
            process.terminate()
            process.communicate(timeout=10)
