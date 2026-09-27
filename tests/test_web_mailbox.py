"""v0.7.5 PR C — 三栏真邮箱 /setup 向导 / 可见性页 (任务书 §3.6/§3.4/§4/§5⑬).

Hermetic fixtures only (tmp mail root + injected discover engine): no test
reads the real machine. The web UI state file, the owner mailbox payload and
the wizard endpoints all go through the loopback server with a bearer token,
exactly like test_web.py.
"""

import json
import threading

import pytest

from agent_mailbox import web
from agent_mailbox.store import MailStore
from agent_mailbox.web import _BoardHandler, _mailbox_payload
from agent_mailbox.webpages import MAILBOX_PAGE

TOKEN = "prc-token-1"


@pytest.fixture()
def root(tmp_path):
    return tmp_path / "mail"


@pytest.fixture()
def store(root):
    st = MailStore(root=root)
    st.register("ZC")  # one local agent so the roster is non-trivial
    return st


@pytest.fixture()
def board(root, store, monkeypatch):
    monkeypatch.setattr(web, "_REPORTS", {})  # isolate the report cache
    handler = type("H", (_BoardHandler,), {"store": store, "token": TOKEN})
    srv = web.LoopbackServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield port
    srv.shutdown()
    srv.server_close()


def _req(
    port: int,
    path: str,
    *,
    token: str | None = TOKEN,
    method: str = "GET",
    body: dict | None = None,
):
    import http.client

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    payload = json.dumps(body) if body is not None else None
    if payload:
        headers["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())
    except json.JSONDecodeError:
        return resp.status, {}
    finally:
        conn.close()


# ---------------------------------------------------------------- pages (§4)

def test_mailbox_page_structure(board):
    status, _ = _req(board, "/mail")
    assert status == 200
    html = MAILBOX_PAGE
    # 三栏容器 + 左中右关键内容（验收判据 1 可 curl 验证）
    assert 'class="app"' in html and html.count('class="col"') == 3
    assert 'data-folder="inbox"' in html and 'data-folder="starred"' in html
    assert 'data-folder="drafts"' in html and 'data-folder="archived"' in html
    assert 'data-folder="monitor"' in html and "agent 之间的全部往来" in html
    assert "未送达 / 失败" in html and "密封信（仅元数据）" in html
    assert "通道体检" in html and "任务卡看板" in html
    # 快捷键脚本（j/k/e/r/t//）
    assert "j/k" in html and "派成任务卡" in html and '"j"' in html and '"/"' in html
    # 空状态行动点文案（§3.6）+ 信 vs 任务卡说明（§5⑬）
    assert "给你的 agent 写第一封信" in html
    assert "任务卡＝要人动手的活" in html


# ------------------------------------------------------- mailbox API (§4.2)

def test_empty_mailbox_payload_shapes_empty_state(store):
    out = _mailbox_payload(store)
    assert out["owner"] == "boss"
    assert out["letters"] == [] and out["drafts"] == []
    assert out["counts"]["inbox"] == 0
    assert {m["id"] for m in out["members"]} == {"boss", "ZC"}
    assert out["members"][0]["dot"] == "idle"  # no scan yet → 未实测, never guessed


def test_first_letter_flow_lands_in_inbox(store, board):
    # §3.6: 空信箱 → 「写第一封信」POST 成功落箱
    status, out = _req(
        board,
        "/api/mail/send",
        method="POST",
        body={"to": "ZC", "subject": "你好，这是你的信箱", "body": "第一封"},
    )
    assert status == 200 and out["delivered"][0]["to"] == "ZC"
    landed = store.list_messages("ZC")
    assert len(landed) == 1 and landed[0]["attention"] == "decision"
    payload = _mailbox_payload(store)
    assert payload["counts"]["sent"] == 1  # 已发送 folder sees boss's letter
    assert payload["letters"][0]["read"] is True  # boss's own sent mail: not pending


def test_send_rejects_empty_body_and_bad_attention(board):
    assert _req(board, "/api/mail/send", method="POST", body={"to": "ZC", "body": "  "})[0] == 400
    assert _req(
        board,
        "/api/mail/send",
        method="POST",
        body={"to": "ZC", "body": "x", "attention": "loud"},
    )[0] == 400


def test_sealed_letter_is_metadata_only_in_human_view(store, board):
    _req(
        board,
        "/api/mail/send",
        method="POST",
        body={"to": "ZC", "subject": "凭据轮换", "body": "secret-body-xyz", "sealed": True},
    )
    payload = _mailbox_payload(store)
    letter = payload["letters"][0]
    assert letter["redacted"] == "sealed" and "body" not in letter
    assert "secret-body-xyz" not in json.dumps(payload)
    assert payload["counts"]["sealed"] == 1


def test_monitor_letters_readonly_archive_and_state_markers(store, board):
    mid = store.send("ZC", "HS", "agent chatter", "between agents")[0]["id"]
    payload = _mailbox_payload(store)
    letter = next(x for x in payload["letters"] if x["id"] == mid)
    assert letter["monitor"] is True and letter["read"] is False
    # 人只读监看：归档被拒（403），但星标/已读标记落在 web_state，不改信件本体
    assert _req(board, f"/api/mail/{mid}/archive", method="POST", body={})[0] == 403
    assert _req(board, f"/api/mail/{mid}/star", method="POST", body={})[0] == 200
    assert _req(board, f"/api/mail/{mid}/read", method="POST", body={})[0] == 200
    state = json.loads((store.root / "web_state.json").read_text(encoding="utf-8"))
    assert mid in state["starred"] and mid in state["read"]
    raw = store.find_letter(mid)
    assert raw["status"] == "pending" and "handled_log" not in raw  # 信件本体未动
    payload = _mailbox_payload(store)
    letter = next(x for x in payload["letters"] if x["id"] == mid)
    assert letter["starred"] is True and letter["read"] is True


def test_attention_tiers_only_first_tier_reminds(store, board):
    # §3.4 打扰三档：默认只提醒「需你拍板」；报备静默入箱；存档不提醒
    store.send("ZC", "boss", "d1", "x", attention="decision")
    store.send("ZC", "boss", "d2", "x", attention="decision")
    store.send("ZC", "boss", "r1", "x", attention="report")
    store.send("ZC", "boss", "a1", "x", attention="archive")
    payload = _mailbox_payload(store)
    assert payload["attention_unread"] == 2
    assert payload["counts"]["inbox"] == 4  # 全部都在箱里，只是不都提醒


def test_read_unread_and_archive_own_letter(store, board):
    mid = store.send("ZC", "boss", "for boss", "hello")[0]["id"]
    payload = _mailbox_payload(store)
    letter = next(x for x in payload["letters"] if x["id"] == mid)
    assert letter["read"] is False and payload["counts"]["inbox"] == 1
    assert _req(board, f"/api/mail/{mid}/read", method="POST", body={})[0] == 200
    assert store.find_letter(mid)["status"] == "acked"
    assert _req(board, f"/api/mail/{mid}/unread", method="POST", body={})[0] == 200
    assert store.find_letter(mid)["status"] == "pending"
    assert _req(board, f"/api/mail/{mid}/archive", method="POST", body={})[0] == 200
    assert not store.list_messages("boss") and len(store.list_archived("boss")) == 1
    payload = _mailbox_payload(store)
    assert payload["counts"]["inbox"] == 0 and payload["counts"]["archived"] == 1


def test_task_from_letter_wakes_assignee(store, board):
    mid = store.send("HS", "boss", "please handle this", "body")[0]["id"]
    status, out = _req(
        board, f"/api/mail/{mid}/task", method="POST", body={"assignee": "ZC"}
    )
    assert status == 200 and out["task"]["assignee"] == "ZC"
    assert out["task"]["title"] == "please handle this"
    # 信→任务卡后承办人被自动提醒（复用看板既有机制，不回退）
    assert "[task#t-1 → todo]" in store.check("ZC")[0]["subject"]


def test_task_requires_assignee(board):
    assert _req(board, "/api/mail/x/task", method="POST", body={})[0] in (400, 404)


def test_drafts_save_list_delete(store, board):
    assert _req(
        board,
        "/api/mail/drafts",
        method="POST",
        body={"to": "ZC", "subject": "草稿", "body": "wip"},
    )[0] == 200
    payload = _mailbox_payload(store)
    assert payload["counts"]["drafts"] == 1 and payload["drafts"][0]["subject"] == "草稿"
    did = payload["drafts"][0]["id"]
    assert _req(board, "/api/mail/drafts/delete", method="POST", body={"id": did})[0] == 200
    assert _mailbox_payload(store)["counts"]["drafts"] == 0


def test_add_member_endpoint_audits_kind(store, board):
    status, out = _req(
        board, "/api/members", method="POST", body={"id": "NEWBOT", "kind": "guest"}
    )
    assert status == 200 and out["member"]["kind"] == "guest"
    assert "NEWBOT" in store.registry()["agents"]
    assert any(e["action"] == "member_kind" for e in store.audit_entries())


def test_confirm_external_via_mailbox(store, board):
    mid = store.send("OUTSIDE", "boss", "external", "x", origin="external")[0]["id"]
    assert _req(board, f"/api/mail/{mid}/confirm-external", method="POST", body={})[0] == 200
    assert store.find_letter(mid)["origin"] == "local"
    assert any(e["action"] == "confirm_external" for e in store.audit_entries())


def test_unknown_letter_action_is_404(board):
    status, out = _req(board, "/api/mail/nope123/read", method="POST", body={})
    assert status == 404 and "not found" in out["error"]


def test_undelivered_tag_comes_from_cached_report(store, board, monkeypatch):
    """断链成员的信被标「未送达」；没扫过时绝不乱标（不猜）。"""
    store.send("boss", "WB", "will it arrive?", "x")
    payload = _mailbox_payload(store)
    assert payload["letters"][0]["undeliverable"] is False  # 未扫描 → 不猜
    monkeypatch.setattr(
        web,
        "_REPORTS",
        {
            str(store.root): {
                "at": __import__("time").monotonic(),
                "report": {
                    "members": [
                        {
                            "member": "WB",
                            "kind": "app",
                            "connected": True,
                            "channels": [
                                {"type": "webhook", "status": "broken", "reason": "断"},
                                {"type": "port", "status": "broken", "reason": "断"},
                            ],
                        },
                        {"member": "ZC", "kind": "cli", "connected": True,
                         "channels": [{"type": "command", "status": "ok"}]},
                    ]
                },
            }
        },
    )
    payload = _mailbox_payload(store)
    letter = payload["letters"][0]
    assert letter["undeliverable"] is True  # WB 两条通道全断
    assert payload["counts"]["undelivered"] == 1
    assert payload["members"][0]["id"] == "WB" and payload["members"][0]["dot"] == "bad"
