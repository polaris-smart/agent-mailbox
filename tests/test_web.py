"""Tests for the --web board: auth, read, create, move, error envelope.

Requests go through http.client straight to the fixture's loopback server
(port 0 → ephemeral, 127.0.0.1 only) — no outbound URL fetching happens.
"""

import http.client
import json
import threading

import pytest

from agent_mailbox.store import MailStore
from agent_mailbox.web import PAGE, _BoardHandler

TOKEN = "test-token-123"


@pytest.fixture()
def board(tmp_path):
    store = MailStore(root=tmp_path / "mail")
    store.register("HS")
    handler = type("H", (_BoardHandler,), {"store": store, "token": TOKEN})
    srv = __import__("http.server", fromlist=["ThreadingHTTPServer"]).ThreadingHTTPServer(
        ("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield port, store
    srv.shutdown()
    srv.server_close()


def _req(port: int, path: str, *, token: str | None = None,
         method: str = "GET", body: dict | None = None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    payload = json.dumps(body) if body is not None else None
    if payload:
        headers["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def test_missing_token_is_401(board):
    port, _ = board
    assert _req(port, "/api/tasks")[0] == 401
    assert _req(port, "/?token=wrong")[0] == 401


def test_board_page_served(board):
    port, _ = board
    status, body = _req(port, "/?token=" + TOKEN)
    assert status == 200
    assert b"agent-mailbox" in body
    assert b'data-status="review"' in body  # four lanes present
    assert PAGE.encode() == body


def test_api_tasks_lists(board):
    port, store = board
    store.task_create("existing", "ZC", "HS", notify=False)
    status, body = _req(port, "/api/tasks", token=TOKEN)
    assert status == 200
    tasks = json.loads(body)["tasks"]
    assert [t["title"] for t in tasks] == ["existing"]


def test_web_create_task_notifies_assignee(board):
    port, store = board
    status, body = _req(port, "/api/tasks", token=TOKEN, method="POST",
                        body={"title": "from board", "assignee": "HS", "due": "2026-09-10"})
    assert status == 200
    task = json.loads(body)["task"]
    assert task["id"] == "t-1" and task["created_by"] == "boss"
    # the board acts as boss, so the assignee got the wake-up mail
    msgs = store.check("HS")
    assert len(msgs) == 1
    assert "[task#t-1 → todo] from board" == msgs[0]["subject"]
    assert msgs[0]["from"] == "boss"


def test_web_move_happy_path_and_illegal_rejected(board):
    port, store = board
    store.task_create("t", "HS", "HS", notify=False)
    status, body = _req(port, "/api/tasks/t-1/move", token=TOKEN, method="POST",
                        body={"status": "doing", "note": "started"})
    assert status == 200
    assert json.loads(body)["task"]["status"] == "doing"
    status, body = _req(port, "/api/tasks/t-1/move", token=TOKEN, method="POST",
                        body={"status": "done"})
    assert status == 400
    assert "illegal transition" in json.loads(body)["error"]
    # state unchanged after the rejected call
    assert store.task_list()[0]["status"] == "doing"


def test_web_move_wakes_assignee(board):
    port, store = board
    store.task_create("review me", "HS", "boss", notify=False)
    status, _ = _req(port, "/api/tasks/t-1/move", token=TOKEN, method="POST",
                     body={"status": "doing"})
    assert status == 200
    assert "[task#t-1 → doing]" in store.check("HS")[0]["subject"]


def test_unknown_endpoint_404(board):
    port, _ = board
    assert _req(port, "/api/nope", token=TOKEN, method="POST", body={})[0] == 404
