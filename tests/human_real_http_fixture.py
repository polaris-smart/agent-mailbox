"""Disposable real HTTP fixture for the Human browser boundary test."""

import json
import secrets
import sys
import tempfile
import threading
from pathlib import Path

from agent_mailbox.workbench import WorkbenchHTTP
from agent_mailbox.workbench_human_spaces import (
    add_space_member,
    create_human,
    create_human_invitation,
    create_managed_private_project,
    create_shared_space,
    grant_project,
    move_project_to_shared_space,
)
from agent_mailbox.workbench_store import WorkbenchStore


def main():
    with tempfile.TemporaryDirectory(prefix="mailbox-human-browser-", dir="/private/tmp") as work:
        root = Path(work)
        (root / "owner-private").mkdir()
        (root / "shared").mkdir()
        store = WorkbenchStore(root / "home")
        owner_private = store.create_project("OWNER_PRIVATE_MARKER", root / "owner-private")["id"]
        shared = store.create_project("SHARED_PROJECT_MARKER", root / "shared")["id"]
        employee = store.create_employee("Synthetic employee", "codex", shared)["id"]
        store.create_task(shared, "SHARED_TASK_MARKER", "Synthetic task", employee)
        with store._transaction(readonly=True) as db:
            owner = db.execute(
                "SELECT owner_human_id FROM human_bootstrap WHERE singleton=1"
            ).fetchone()[0]
        guest = create_human(store, "Guest")
        guest_id = guest["human_id"]
        guest_private = create_managed_private_project(store, guest_id, "GUEST_PRIVATE_MARKER")[
            "project_id"
        ]
        space = create_shared_space(store, owner, "Family shared")["space_id"]
        add_space_member(store, owner, space, guest_id)
        move_project_to_shared_space(store, owner, shared, space)
        grant_project(store, owner, shared, guest_id, "viewer")
        invitation = create_human_invitation(store, guest_id)["invitation"]
        server = WorkbenchHTTP(store, token="synthetic-owner-" + secrets.token_urlsafe(24))
        # The fixture must join its long-lived Human change-stream handlers
        # before TemporaryDirectory removes the synthetic database.
        server.daemon_threads = False
        server.start_human_forward(port=0, persist=False)
        server.human_forward.daemon_threads = False
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            print(
                json.dumps(
                    {
                        "endpoint": server.endpoint,
                        "human_endpoint": server.human_forward.endpoint,
                        "owner_token": server.token,
                        "guest_invitation": invitation,
                        "guest_id": guest_id,
                        "owner_private": owner_private,
                        "shared": shared,
                        "guest_private": guest_private,
                    }
                ),
                flush=True,
            )
            for command in sys.stdin:
                if command.strip() == "add-shared-task":
                    store.create_task(shared, "NEW_SHARED_TASK_MARKER", "Synthetic task", employee)
                    server.ui_notify()
                    print(json.dumps({"shared_task_added": True}), flush=True)
                elif command.strip() == "add-shared-message":
                    store.send_message(
                        shared, "NEW_SHARED_MESSAGE_MARKER", "Synthetic group update"
                    )
                    server.ui_notify()
                    print(json.dumps({"shared_message_added": True}), flush=True)
                elif command.strip() == "add-shared-resource":
                    resource_file = root / "shared" / "approved-brief.md"
                    resource_file.write_text("NEW_SHARED_RESOURCE_MARKER")
                    resource = store.add_resource(shared, "Approved brief", "prd", resource_file)
                    store.capture_resource_version(shared, resource["id"])
                    server.ui_notify()
                    print(json.dumps({"shared_resource_id": resource["id"]}), flush=True)
                elif command.strip() == "stop-forward":
                    server.stop_human_forward(persist=False)
                    print(json.dumps({"forward_stopped": True}), flush=True)
                elif command.strip() == "quit":
                    break
        finally:
            # End both listeners' long-lived /human/changes handlers before
            # the forward listener joins its non-daemon request threads.
            server.closing.set()
            server.ui_notify()
            server.shutdown()
            thread.join(timeout=5)
            server.close()


if __name__ == "__main__":
    main()
