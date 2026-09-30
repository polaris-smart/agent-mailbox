#!/usr/bin/env python3
"""Two isolated processes over pinned HTTPS; deterministic fixture, never a model.

Run with the installed package on Ubuntu, or --docker-container NAME on a Mac
whose named container mounts this repository at /repo and --output at /evidence.
Only temporary test credentials are created, and none are printed.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from agent_mailbox.workbench_fleet import FleetCoordinator, _write_private
from agent_mailbox.workbench_node import _client, run_node
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore

FIXTURE = """
import json,sys,pathlib
request=json.loads(sys.stdin.readline())
marker=pathlib.Path(request['cwd'])/'executions.txt'
with marker.open('a') as file: file.write(request['run_id']+'\\n')
def send(kind,**fields):
 print(json.dumps(dict(protocol=1,run_id=request['run_id'],session_id=request['session_id'],type=kind,**fields)),flush=True)
send('started')
send('result',status='completed',output_text='Deterministic Ubuntu fixture completed.')
sys.stdin.readline()
"""


def wait_for(fn, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.1)
    raise RuntimeError("Timed out waiting for isolated node verification")


def scope_probe(root, request_path):
    request = json.loads(request_path.read_text())
    client = _client(root / "node")
    results = {}
    for tool, args in [
        ("context", {}),
        ("resource_read", {"resource_id": request["resource_id"]}),
        ("code_search", {"query": "WorkbenchError"}),
        ("messages", {}),
    ]:
        try:
            results[tool] = client.project_tool(
                request["project_id"],
                request["employee_id"],
                tool,
                args,
                task_id=request["task_id"],
                run_id=request["run_id"],
            )
        except WorkbenchError as exc:
            results[tool] = {"denied": exc.code}
    if request.get("finalize_cancel"):
        client.receipt(request["task_id"], request["run_id"], "cancelled")
    _write_private(root / "probe-result.json", json.dumps(results).encode())
    return 0


def worker(root, invite_path, claim_only=False):
    home = root / "node"
    client = _client(home, json.loads(invite_path.read_text()))
    project_id = client.projects()[0]["id"]
    checkout = root / "checkout"
    checkout.mkdir(exist_ok=True)
    client.map_project(project_id, checkout)
    ready = root / "node-ready.json"
    if not ready.exists():
        employee = client.register_employee(project_id, "Ubuntu deterministic fixture", "codex")
        _write_private(
            ready,
            json.dumps(
                {"employee": employee["id"], "device": client.credentials["device_id"]}
            ).encode(),
        )
    if claim_only:
        assert client.claim(project_id, wait=0) is not None
        return 0
    bridge = root / "fixture.py"
    bridge.write_text(FIXTURE)
    stopping = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopping.set())
    try:
        run_node(client, stopping=stopping, bridge_command=[sys.executable, str(bridge)])
    except WorkbenchError as exc:
        # A revoked node must not claim or silently regain access.
        if exc.code != "permission_denied":
            raise
        return 7
    return 0


def check(output, container):
    output.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="node-check-", dir=output)).resolve()
    root.chmod(0o700)
    owner = WorkbenchStore(root / "owner")
    project = owner.create_project("Isolated Beta transport fixture", root)
    source = root / "shared-prd.md"
    source.write_text("Approved resource before task claim.\n")
    resource = owner.add_resource(project["id"], "PRD fixture", "prd", source)
    original = owner.capture_resource_version(project["id"], resource["id"])
    coordinator = FleetCoordinator(owner)
    coordinator.start(advertised_host="host.docker.internal" if container else None)
    invite_path = root / "invite.json"
    _write_private(invite_path, json.dumps(coordinator.issue_invite([project["id"]])).encode())
    if container:
        remote_root = "/evidence/" + root.name
        command = [
            "docker",
            "exec",
            "-e",
            "PYTHONPATH=/repo/src",
            container,
            "/venv/bin/python",
            "/repo/scripts/check-node-beta.py",
            "--worker",
            remote_root,
            "--invite",
            remote_root + "/invite.json",
        ]
    else:
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            str(root),
            "--invite",
            str(invite_path),
        ]
    processes = []
    logs = []

    def start():
        log = (root / f"node-{len(logs)}.log").open("w")
        os.chmod(log.name, 0o600)
        logs.append(log)
        process = subprocess.Popen(command, stdout=log, stderr=log)
        processes.append(process)
        return process

    def stop(process):
        if process.poll() is None:
            if container:
                subprocess.run(
                    [
                        "docker",
                        "exec",
                        container,
                        "pkill",
                        "-TERM",
                        "-f",
                        "check-node-beta.py --worker",
                    ],
                    check=False,
                    capture_output=True,
                )
            else:
                process.terminate()
            try:
                process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def probe(task, denied=False, finalize_cancel=False):
        request = {
            "project_id": project["id"],
            "employee_id": ready["employee"],
            "resource_id": resource["id"],
            "task_id": task["id"],
            "run_id": task["run_id"],
            "finalize_cancel": finalize_cancel,
        }
        request_path = root / "probe.json"
        _write_private(request_path, json.dumps(request).encode())
        selected_path = "/evidence/" + root.name + "/probe.json" if container else str(request_path)
        completed = subprocess.run(
            command + ["--probe", selected_path], capture_output=True, timeout=15, check=False
        )
        assert completed.returncode == 0, "Private scoped probe failed"
        result = json.loads((root / "probe-result.json").read_text())
        if denied:
            assert all(
                result[tool].get("denied") == "permission_denied"
                for tool in ("context", "resource_read", "code_search", "messages")
            )
        return result

    checks = []
    try:
        process = start()
        ready = wait_for(
            lambda: (
                json.loads((root / "node-ready.json").read_text())
                if (root / "node-ready.json").exists()
                else None
            )
        )
        checks.append("private invitation + pinned TLS + explicit mapping + registration")
        task = owner.create_task(project["id"], "Fixture task", "deterministic", ready["employee"])
        coordinator.notify()
        result = wait_for(
            lambda: (
                owner.get_task(task["id"])
                if owner.get_task(task["id"])["status"] == "review"
                else None
            )
        )
        assert result["result"] == "Deterministic Ubuntu fixture completed."
        owner.review_task(task["id"], "accept")
        checks.append("fixture child execution + review + human acceptance")
        stop(process)
        probe(task, denied=True)
        snapshot_task = owner.create_task(
            project["id"], "Frozen resource", "deterministic", ready["employee"]
        )
        assert (
            subprocess.run(
                command + ["--claim-only"], capture_output=True, timeout=15, check=False
            ).returncode
            == 0
        )
        pinned = owner.execution_resource_manifest(snapshot_task["id"], snapshot_task["run_id"])
        source.write_text("New approved resource after task claim.\n")
        newer = owner.capture_resource_version(project["id"], resource["id"])
        assert newer["id"] != original["id"]
        frozen = probe(snapshot_task)
        remote_read = frozen["resource_read"]
        local_read = owner.read_execution_resource(
            project["id"], resource["id"], snapshot_task["id"], snapshot_task["run_id"]
        )
        assert (
            remote_read["content"]
            == local_read["content"]
            == "Approved resource before task claim.\n"
        )
        assert (
            remote_read["provenance"]["content_sha256"]
            == original["content_sha256"]
            == local_read["provenance"]["content_sha256"]
        )
        assert (
            frozen["context"]["resource_manifest"]["manifest_sha256"] == pinned["manifest_sha256"]
        )
        owner.cancel_task(snapshot_task["id"])
        probe(snapshot_task, denied=True, finalize_cancel=True)
        checks.append(
            "approved resource frozen at claim; remote/local hashes agree after new approval"
        )
        checks.append("ended and canceled runs deny context/resource_read/code_search/messages")
        # Claim without executing. A fresh process must interrupt, never replay.
        orphan = owner.create_task(
            project["id"], "Uncertain claim", "deterministic", ready["employee"]
        )
        assert (
            subprocess.run(
                command + ["--claim-only"], capture_output=True, timeout=15, check=False
            ).returncode
            == 0
        )
        before = (root / "checkout/executions.txt").read_text()
        process = start()
        wait_for(lambda: owner.get_task(orphan["id"])["status"] == "interrupted")
        assert (root / "checkout/executions.txt").read_text() == before
        checks.append("restart recovers uncertain claim without fixture replay")
        # Real listener outage; saved pairing and worker recover without reinvitation.
        port = coordinator.server.server_port
        coordinator.stop()
        time.sleep(1)
        coordinator.start(port=port, advertised_host="host.docker.internal" if container else None)
        resumed = owner.create_task(
            project["id"], "After reconnect", "deterministic", ready["employee"]
        )
        coordinator.notify()
        wait_for(lambda: owner.get_task(resumed["id"])["status"] == "review")
        checks.append("listener disconnect + reconnect with original device identity")
        stop(process)
        coordinator.revoke_device(ready["device"])
        process = start()
        assert process.wait(timeout=15) != 0
        assert len((root / "checkout/executions.txt").read_text().splitlines()) == 2
        probe(resumed, denied=True)
        checks.append("revocation denies fresh node startup and all execution-scoped reads")
        report = {
            "passed": True,
            "worker": "deterministic protocol fixture, not an LLM",
            "coordinator_os": sys.platform,
            "node_os": "Ubuntu 24.04 (Docker)" if container else sys.platform,
            "checks": checks,
            "external_servers_tested": False,
            "source_directory": str(Path(__file__).resolve().parents[1]),
        }
        (output / "node-beta-verification.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
    finally:
        for process in processes:
            stop(process)
        coordinator.stop()
        for log in logs:
            log.close()
        # Keep evidence and journals but no reusable pairing/invitation secrets.
        for relative in (
            "invite.json",
            "node/workbench/fleet/client.json",
            "node/workbench/fleet/pair-attempt.json",
            "owner/workbench/fleet/tls-key.pem",
        ):
            (root / relative).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("node-beta-evidence"))
    parser.add_argument("--docker-container")
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--invite", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--claim-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--probe", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        try:
            if args.probe:
                return scope_probe(args.worker, args.probe)
            return worker(args.worker, args.invite, args.claim_only)
        except WorkbenchError as exc:
            print(json.dumps({"error_code": exc.code}), file=sys.stderr)
            return 7
    check(args.output.resolve(), args.docker_container)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
