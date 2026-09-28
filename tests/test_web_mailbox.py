"""v0.7.5 PR C — 三栏真邮箱 /setup 向导 / 可见性页 (任务书 §3.6/§3.4/§4/§5⑬).

Hermetic fixtures only (tmp mail root + injected discover engine): no test
reads the real machine. The web UI state file, the owner mailbox payload and
the wizard endpoints all go through the loopback server with a bearer token,
exactly like test_web.py.
"""

import json
import threading

import pytest

from agent_mailbox import server, web
from agent_mailbox.store import MailStore, load_visibility
from agent_mailbox.wake import WakeConfig, run_once
from agent_mailbox.web import VISIBILITY_PAGE, _BoardHandler, _mailbox_payload
from agent_mailbox.webpages import MAILBOX_PAGE, SETUP_PAGE

TOKEN = "prc-token-1"


@pytest.fixture()
def root(tmp_path):
    return tmp_path / "mail"


@pytest.fixture()
def store(root):
    st = MailStore(root=root)
    st.register("ZC")  # one local agent so the roster is non-trivial
    st.register("HS")  # A7: unregistered recipients are hard-rejected — the
    # board/web flows address HS, so the roster must know it.
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


def test_setup_and_visibility_pages_structure():
    assert "发现你的智能体" in SETUP_PAGE and "测一下" in SETUP_PAGE and "完事" in SETUP_PAGE
    assert "不自动处理来信" in SETUP_PAGE  # 模型是可选功能，不是必答题
    assert "全部测一遍" in SETUP_PAGE  # 结果+下一步成对出现的承载表
    for key in (
        "owner_sees_all",
        "agent_cross_read",
        "sealed_in_human_view",
        "external_auto_execute",
    ):
        assert key in VISIBILITY_PAGE
    assert "发信权限" in VISIBILITY_PAGE and "改动会记录在案" in VISIBILITY_PAGE


def test_human_pages_require_token(board):
    for path in ("/mail", "/setup", "/visibility", "/api/mail", "/api/visibility"):
        assert _req(board, path, token=None)[0] == 401


# ------------------------------------------------------- mailbox API (§4.2)


def test_empty_mailbox_payload_shapes_empty_state(store):
    out = _mailbox_payload(store)
    assert out["owner"] == "boss"
    assert out["letters"] == [] and out["drafts"] == []
    assert out["counts"]["inbox"] == 0
    assert {m["id"] for m in out["members"]} == {"boss", "ZC", "HS"}  # A7: HS 注册进名册
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
    assert (
        _req(
            board,
            "/api/mail/send",
            method="POST",
            body={"to": "ZC", "body": "x", "attention": "loud"},
        )[0]
        == 400
    )


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
    status, out = _req(board, f"/api/mail/{mid}/task", method="POST", body={"assignee": "ZC"})
    assert status == 200 and out["task"]["assignee"] == "ZC"
    assert out["task"]["title"] == "please handle this"
    # 信→任务卡后承办人被自动提醒（复用看板既有机制，不回退）
    assert "[task#t-1 → todo]" in store.check("ZC")[0]["subject"]


def test_task_requires_assignee(board):
    assert _req(board, "/api/mail/x/task", method="POST", body={})[0] in (400, 404)


def test_drafts_save_list_delete(store, board):
    assert (
        _req(
            board,
            "/api/mail/drafts",
            method="POST",
            body={"to": "ZC", "subject": "草稿", "body": "wip"},
        )[0]
        == 200
    )
    payload = _mailbox_payload(store)
    assert payload["counts"]["drafts"] == 1 and payload["drafts"][0]["subject"] == "草稿"
    did = payload["drafts"][0]["id"]
    assert _req(board, "/api/mail/drafts/delete", method="POST", body={"id": did})[0] == 200
    assert _mailbox_payload(store)["counts"]["drafts"] == 0


def test_add_member_endpoint_audits_kind(store, board):
    status, out = _req(board, "/api/members", method="POST", body={"id": "NEWBOT", "kind": "guest"})
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
    store.register("WB")  # A7: WB 需在册才能收信（断链标签测的是扫描结论，非注册态）
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
                        {
                            "member": "ZC",
                            "kind": "cli",
                            "connected": True,
                            "channels": [{"type": "command", "status": "ok"}],
                        },
                    ]
                },
            }
        },
    )
    payload = _mailbox_payload(store)
    letter = payload["letters"][0]
    assert letter["undeliverable"] is True  # WB 两条通道全断
    assert payload["counts"]["undelivered"] == 1
    wb_member = next(m for m in payload["members"] if m["id"] == "WB")
    assert wb_member["dot"] == "bad"


# ---------------------------------------------------------- visibility (§4.3)


def test_visibility_defaults_match_spec(store, board):
    status, out = _req(board, "/api/visibility")
    assert status == 200
    assert out["visibility"] == {
        "owner_sees_all": True,  # 主人全可见
        "agent_cross_read": False,  # agent 之间互看＝关
        "sealed_in_human_view": False,  # 密封信只元数据（硬锁）
        "external_auto_execute": False,  # 外部信不自动执行
    }
    assert out["hard_locked"] == ["sealed_in_human_view"]
    assert out["audit"] == []


def test_visibility_post_persists_and_audits(store, board):
    status, out = _req(board, "/api/visibility", method="POST", body={"agent_cross_read": True})
    assert status == 200 and out["visibility"]["agent_cross_read"] is True
    # 开关状态有落点：config.json 可 grep
    cfg = json.loads((store.root / "config.json").read_text(encoding="utf-8"))
    assert cfg["visibility"]["agent_cross_read"] is True
    # 改动落审计（谁/何时/改了什么）
    entries = store.audit_entries("visibility_change")
    assert len(entries) == 1 and entries[0]["by"] == "boss"
    assert entries[0]["changes"] == {"agent_cross_read": True}
    _, out = _req(board, "/api/visibility")
    assert len(out["audit"]) == 1


def test_visibility_noop_change_writes_nothing(store, board):
    assert (
        _req(board, "/api/visibility", method="POST", body={"owner_sees_all": True})[0] == 200
    )  # already the default
    assert not (store.root / "config.json").exists()
    assert store.audit_entries("visibility_change") == []


def test_visibility_sealed_switch_is_hard_locked(store, board):
    status, out = _req(board, "/api/visibility", method="POST", body={"sealed_in_human_view": True})
    assert status == 400 and "硬规则" in out["error"]
    assert load_visibility(store.root)["sealed_in_human_view"] is False


def test_visibility_rejects_unknown_and_non_bool(store, board):
    assert _req(board, "/api/visibility", method="POST", body={"nope": True})[0] == 400
    assert _req(board, "/api/visibility", method="POST", body={"agent_cross_read": "yes"})[0] == 400
    assert _req(board, "/api/visibility", method="POST", body={})[0] == 400


def test_agent_cross_read_switch_wires_tool_layer(root, store, monkeypatch):
    """§4.3 开关是真的：开了 agent_cross_read，agent 可读他人信箱；默认拒绝。"""
    store.register("A")  # A7: recipients must be registered members
    store.register("B")
    store.send("A", "B", "hello b", "x")
    monkeypatch.setenv("AGENT_MAIL_HOME", str(root))
    monkeypatch.setenv("AGENT_MAIL_ID", "A")
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_binding", {"enabled": False})
    with pytest.raises(server.MailboxError, match="permission denied"):
        server.mailbox_list(agent_id="B")  # 默认关：结构化拒绝
    store.set_visibility({"agent_cross_read": True}, by="boss")
    out = server.mailbox_list(agent_id="B")  # 开了：公开抄送
    assert out["count"] == 1


def test_external_auto_execute_switch_wires_wake(root, store, monkeypatch):
    from agent_mailbox import store as store_mod

    store.register("B")
    notified = []
    monkeypatch.setattr(store_mod, "notify_new_messages", lambda msgs, **kw: notified.extend(msgs))
    ext = store.send("OUTSIDE", "B", "external", "x", origin="external")
    cfg = WakeConfig(
        {
            "agent_id": "B",
            "adapter": "generic-webhook",
            "webhook": {"url": "http://127.0.0.1:9/hook"},
            "retry_interval": 0,
        },
        root,
    )

    class Rec:
        def __init__(self):
            self.calls = []

        def deliver(self, msg):
            self.calls.append(msg["id"])
            return True

    rec = Rec()
    assert run_once(root, cfg, adapter=rec)["skipped_external"] == 1  # 默认关＝门开
    assert rec.calls == []
    store.set_visibility({"external_auto_execute": True}, by="boss")
    stats = run_once(root, cfg, adapter=rec)
    assert stats["skipped_external"] == 0 and rec.calls == [ext[0]["id"]]


# --------------------------------------------------------------- wizard (§4.1)

FAKE_REPORT = {
    "generated_at_local": "2026-09-27 10:00:00 +0800",
    "members": [
        {
            "member": "ZC",
            "kind": "cli",
            "connected": True,
            "channels": [
                {"type": "command", "status": "ok", "value": "/usr/bin/zcode", "source": "PATH"}
            ],
        },
        {
            "member": "WB",
            "kind": "app",
            "connected": True,
            "channels": [
                {"type": "webhook", "status": "broken", "reason": "断", "next_step": "起服务"}
            ],
        },
    ],
    "supported": ["claude", "codex"],
}


def test_discover_endpoint_uses_real_engine_glue(store, board, monkeypatch):
    calls = []

    def fake_discover(root, *, save):
        calls.append((str(root), save))
        return FAKE_REPORT

    monkeypatch.setattr(web, "_run_discover", fake_discover)
    status, out = _req(board, "/api/status")
    assert status == 200 and out["members"][0]["member"] == "ZC"
    assert calls and calls[0][1] is False  # status 不重写指纹
    status, out = _req(board, "/api/discover", method="POST", body={})
    assert status == 200 and out["supported"] == ["claude", "codex"]
    assert calls[-1][1] is True  # 向导扫描默认落指纹（与 CLI discover 一致）


def test_test_endpoint_returns_result_plus_next_step(store, board, monkeypatch):
    calls = []

    def fake_test(root, member, timeout):
        calls.append((str(root), member, timeout))
        return {
            "member": member,
            "ok": False,
            "stage": "timeout",
            "elapsed_s": 15.0,
            "reason": "没等到回执",
            "next_step": "修一下唤醒通道",
        }

    monkeypatch.setattr(web, "_run_test_letter", fake_test)
    status, out = _req(board, "/api/test", method="POST", body={"member": "WB"})
    assert status == 200
    assert out["ok"] is False and out["next_step"] == "修一下唤醒通道"  # 结果+下一步成对
    assert calls[0][1] == "WB" and calls[0][2] == 15.0
    assert _req(board, "/api/test", method="POST", body={})[0] == 400


def test_setup_summary_endpoint(store, board, monkeypatch):
    monkeypatch.setattr(web, "_REPORTS", {str(store.root): {"at": 10**12, "report": FAKE_REPORT}})
    status, out = _req(board, "/api/setup-summary")
    assert status == 200
    assert out["registered"] == 2 and out["discovered"] == 2  # A7: HS 注册进名册
    assert out["model"] == {
        "auto": False,
        "label": "不自动处理来信",
        "note": "可选功能——收发信件本身不需要任何模型",
    }
    assert [b["member"] for b in out["broken"]] == ["WB"]
    assert out["wake"]["configured"] is False  # 未装 wake.json → 如实报未安装
    (store.root / "wake.json").write_text(
        json.dumps(
            {"agent_id": "ZC", "adapter": "hermes", "webhook": {"url": "http://127.0.0.1:1/hook"}}
        ),
        encoding="utf-8",
    )
    _, out = _req(board, "/api/setup-summary")
    assert out["wake"]["configured"] is True and out["wake"]["agent_id"] == "ZC"
