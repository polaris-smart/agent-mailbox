"""v0.7.6 F 单元 — 状态可信：unread=真待办(pending)口径 + 待处理数字可下钻.

背景（HS 实测）：信箱 18 封全 acked，mailbox_check 仍报 unread=18 —— 旧实现
``unread: len(msgs)`` 把本次调用自己刚翻成 acked 的信计成了未读。修法（判据）：

1. ``unread`` 口径 = 响应时点仓内 status==pending 的信（acked 永不计入）；
   新增 ``total`` = 全部非 done（pending+acked 积压）。默认 mark=False ——
   点数不消费（禁自动 ack / 自动已读）；mark=True 是显式旧消费路径，数字仍在
   消费之后的仓内真值上取。
2. web 看板 ``counts.pending`` + 侧栏「待处理 N」下钻（?folder=pending 深链）。

Hermetic fixtures only，模式照抄 test_thread.py（store 单例重置）与
test_web_mailbox.py（loopback + bearer token）。
"""

import http.client
import json
import threading

import pytest

import agent_mailbox.server as server_mod
from agent_mailbox import web
from agent_mailbox.server import mailbox_check
from agent_mailbox.store import MailStore
from agent_mailbox.web import LoopbackServer, _BoardHandler, _mailbox_payload

TOKEN = "f-unit-token-1"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    root = tmp_path / "mail"
    monkeypatch.setenv("AGENT_MAIL_HOME", str(root))
    server_mod._store = None  # the server caches one store — reset per test
    st = MailStore(root=root)
    st.register("HS")
    st.register("ZC")
    return st


@pytest.fixture()
def board(store, monkeypatch):
    monkeypatch.setattr(web, "_REPORTS", {})  # isolate the report cache
    handler = type("H", (_BoardHandler,), {"store": store, "token": TOKEN})
    srv = LoopbackServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield port
    srv.shutdown()
    srv.server_close()


def _req(port: int, path: str) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", path, headers={"Authorization": f"Bearer {TOKEN}"})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())
    finally:
        conn.close()


# ------------------------------------------------- ① 18 封全 acked → unread==0


def test_all_acked_mailbox_reports_unread_zero(store, monkeypatch):
    """HS 实测场景的回归测试：造 18 封 → 全部 acked → check 必须报 unread==0
    （旧实现把消费掉的信报成 unread=18）。total 露出积压真值。"""
    monkeypatch.setenv("AGENT_MAIL_ID", "HS")
    for i in range(18):
        store.send("ZC", "HS", f"s{i}", f"b{i}", dedupe=False)
    store.claim("HS")  # 消费路径：18 封 pending → acked
    assert [m["status"] for m in store.list_messages("HS")] == ["acked"] * 18

    out = mailbox_check(agent_id="HS")
    assert out["unread"] == 0  # acked 永不计入 unread
    assert out["total"] == 18  # 非 done 积压照实露出
    assert out["messages"] == []
    assert set(out) >= {"agent_id", "unread", "total", "messages"}  # 结构向后兼容


def test_consuming_check_reports_post_state(store, monkeypatch):
    """mark=True（显式旧消费路径）：信被翻成 acked，但 unread/total 取消费后
    的仓内真值——不再把刚 acked 的信报成 unread。"""
    monkeypatch.setenv("AGENT_MAIL_ID", "HS")
    for i in range(3):
        store.send("ZC", "HS", f"m{i}", f"x{i}", dedupe=False)

    out = mailbox_check(agent_id="HS", mark=True)
    assert [m["status"] for m in out["messages"]] == ["acked"] * 3
    assert out["unread"] == 0
    assert out["total"] == 3
    assert store.list_messages("HS", status="pending") == []


# ------------------------------------ ② 混合 pending/acked → unread==pending 数


def test_mixed_mailbox_unread_counts_pending_only(store, monkeypatch):
    """5 封先消费成 acked，再进 3 封新的：unread==3（只数 pending），
    total==8（pending+acked 全部非 done）。"""
    monkeypatch.setenv("AGENT_MAIL_ID", "HS")
    for i in range(5):
        store.send("ZC", "HS", f"old{i}", f"o{i}", dedupe=False)
    store.claim("HS")
    for i in range(3):
        store.send("ZC", "HS", f"new{i}", f"n{i}", dedupe=False)

    out = mailbox_check(agent_id="HS")  # 默认只读探测，不消费
    assert out["unread"] == 3
    assert out["total"] == 8
    assert len(out["messages"]) == 3
    assert all(m["status"] == "pending" for m in out["messages"])

    again = mailbox_check(agent_id="HS", mark=False)  # 显式 mark=False 同口径
    assert again["unread"] == 3 and again["total"] == 8


# -------------------------------------------- ③ total：pending+acked 非 done 全集


def test_total_spans_pending_and_acked_not_done(store, monkeypatch):
    """total = pending + acked；done 的信不计入。"""
    monkeypatch.setenv("AGENT_MAIL_ID", "HS")
    a = store.send("ZC", "HS", "will ack", "1", dedupe=False)[0]["id"]
    b = store.send("ZC", "HS", "will done", "2", dedupe=False)[0]["id"]
    c = store.send("ZC", "HS", "stays pending", "3", dedupe=False)[0]["id"]
    store.set_status("HS", a, "acked")
    store.set_status("HS", b, "done")

    out = mailbox_check(agent_id="HS")
    assert out["unread"] == 1  # 只剩 c
    assert [m["id"] for m in out["messages"]] == [c]
    assert out["total"] == 2  # a(acked) + c(pending)；b(done) 不计


# ------------------------------------- ⑤ 禁自动 ack：check 前后 status 不变


def test_check_never_consumes_mail(store, monkeypatch):
    """反面证明：默认 check（以及 mark=False）是纯读——信件 status 原样、
    handled_log 不新增（点数绝不消费信件）；acked 信不被顺手动成 done。"""
    monkeypatch.setenv("AGENT_MAIL_ID", "HS")
    a = store.send("ZC", "HS", "ack me", "1", dedupe=False)[0]["id"]
    b = store.send("ZC", "HS", "leave pending", "2", dedupe=False)[0]["id"]
    store.set_status("HS", a, "acked")
    before = {m["id"]: (m["status"], m.get("acked_at")) for m in store.list_messages("HS")}

    for kwargs in ({}, {"mark": False}):
        out = mailbox_check(agent_id="HS", **kwargs)
        assert out["messages"][0]["id"] == b  # 只回 pending 的那封
        after = {m["id"]: (m["status"], m.get("acked_at")) for m in store.list_messages("HS")}
        assert after == before  # check 前后 status 原样
        assert not any(s == "done" for s, _ in after.values())  # 无自动 done
        letter = json.loads((store.root / "inbox" / "HS" / f"{b}.json").read_text(encoding="utf-8"))
        assert letter.get("handled_log") in (None, [])  # 无新增留痕

    out = mailbox_check(agent_id="HS", mark=True)  # 显式消费路径才翻状态
    assert out["messages"][0]["id"] == b
    assert store.list_messages("HS", status="pending") == []
    assert {m["status"] for m in store.list_messages("HS")} == {"acked"}


# ------------------------------------------- ④ web：待处理数字可下钻（接口+数据）


def test_web_counts_pending_and_drilldown(store, board):
    """counts.pending = 主人收件箱 pending 数；/api/mail 返回的 pending 集合
    就是那几封（下钻视图数据面）。"""
    p1 = store.send("ZC", "boss", "need decision", "1", dedupe=False)[0]["id"]
    p2 = store.send("HS", "boss", "plain pending", "2", dedupe=False)[0]["id"]
    acked = store.send("ZC", "boss", "already read", "3", dedupe=False)[0]["id"]
    store.set_status("boss", acked, "acked")
    done = store.send("ZC", "boss", "already done", "4", dedupe=False)[0]["id"]
    store.set_status("boss", done, "done")
    other = store.send("ZC", "HS", "not mine", "5", dedupe=False)[0]["id"]  # 他人信不算

    payload = _mailbox_payload(store)
    assert payload["counts"]["pending"] == 2
    assert payload["counts"]["inbox"] == 4  # 收件箱总数口径不变（含 acked/done）
    drill = {
        m["id"]
        for m in payload["letters"]
        if m["to"] == "boss" and not m["_archived"] and m.get("status") == "pending"
    }
    assert drill == {p1, p2}  # 下钻看到的就是那 2 封

    status, body = _req(board, "/api/mail")
    assert status == 200
    assert body["counts"]["pending"] == 2
    api_pending = {
        m["id"] for m in body["letters"] if m["to"] == "boss" and m["status"] == "pending"
    }
    assert api_pending == {p1, p2}
    assert other not in api_pending


def test_web_unread_action_pushes_letter_back_into_pending(store, board):
    """闭环：人把信「标为未读」→ counts.pending +1 —— 下钻数字跟手。"""
    mid = store.send("ZC", "boss", "bump", "x", dedupe=False)[0]["id"]
    store.set_status("boss", mid, "acked")
    assert _mailbox_payload(store)["counts"]["pending"] == 0

    # 走 board 的 POST 动作面（token 门内），把信推回 pending
    conn = http.client.HTTPConnection("127.0.0.1", board, timeout=10)
    try:
        conn.request(
            "POST",
            f"/api/mail/{mid}/unread",
            body="{}",
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        assert resp.status == 200
        resp.read()
    finally:
        conn.close()

    assert _mailbox_payload(store)["counts"]["pending"] == 1
