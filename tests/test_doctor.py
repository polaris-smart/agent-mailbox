"""t-59（A-2 失败必响 + doctor 六检）。

- doctor 六检各态全部用 tmp root + 假日志 fixture（真机日志绝不进测试）。
- run --once 投递失败必须非零退出（禁 rc=0 伪装成功）。
- 告警收件人无 boss（boss 席仅存档语义，老板只看飞书）。
- LocalCommandAdapter 未登录态特征（Authentication required）判失败。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agent_mailbox.cli import SUBCOMMANDS, cli_main, doctor_report
from agent_mailbox.store import MailStore
from agent_mailbox.wake import (
    LocalCommandAdapter,
    WakeConfig,
    send_wake_alert,
    wake_main,
)

CODEX_SPAWN_LINE = (
    "[agent-mailbox wake] local-command spawn failed: [Errno 2] No such file or directory: 'codex'"
)
WB_AUTH_LINE = "2026-09-29 09:09:10 Authentication required. Please use /login to continue"


def _mkroot(tmp_path: Path, *, agents_cfg: dict | None = None) -> Path:
    root = tmp_path / "mail"
    (root / "inbox" / "ZC").mkdir(parents=True)
    (root / "wake.json").write_text(
        json.dumps(
            {
                "agent_id": "ZC",
                "adapter": "hermes",
                "webhook": {"url": "http://127.0.0.1:9/gw"},
                "agents": agents_cfg
                if agents_cfg is not None
                else {
                    "ZC": {
                        "adapter": "local-command",
                        "command": [sys.executable, "-c", "pass"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return root


def _mkla(tmp_path: Path, *ids: str) -> Path:
    la = tmp_path / "launchagents"
    la.mkdir(parents=True, exist_ok=True)
    for i in ids:
        (la / f"com.polaris-smart.agent-mailbox-wake-{i}.plist").write_text(
            "<plist/>", encoding="utf-8"
        )
    return la


def _check(report: dict, cid: str) -> dict:
    return next(c for c in report["checks"] if c["id"] == cid)


# ------------------------------------------------------------ doctor 六检各态


def test_doctor_healthy_root(tmp_path):
    """全绿样态：② plist 已装、③ 最近 ok、④ 路由健康、⑤ 无积压、⑥ 无日志可扫。"""
    root = _mkroot(tmp_path)
    la = _mkla(tmp_path, "ZC")
    (root / "wake-attempts.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-09-29T08:00:00Z",
                "agent": "ZC",
                "route": "daemon",
                "attempt": 1,
                "outcome": "ok",
                "error_class": "",
                "executor": "local-command",
                "latency_ms": 12,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = doctor_report(root, wb_wake_log=tmp_path / "no-such-wb.log", launch_agents_dir=la)
    assert report["healthy"] is True and report["unhealthy_count"] == 0
    assert [
        _check(report, c)["ok"]
        for c in ("root", "wake_loaded", "last_wake", "routing", "backlog", "host_auth")
    ] == [True] * 6
    assert "真成功" in _check(report, "last_wake")["detail"]


def test_doctor_detects_codex_spawn_failed_with_fix(tmp_path):
    """实机判据①: wake-daemon.log 里 'codex' spawn failed → 判定 + 指名道姓的修法。"""
    root = _mkroot(tmp_path)
    (root / "wake-daemon.log").write_text(CODEX_SPAWN_LINE + "\n", encoding="utf-8")
    report = doctor_report(
        root, wb_wake_log=tmp_path / "no-such-wb.log", launch_agents_dir=_mkla(tmp_path, "ZC")
    )
    auth_check = _check(report, "host_auth")
    assert auth_check["ok"] is False
    assert "codex" in auth_check["detail"]
    assert "PATH" in auth_check["next_step"] and "绝对路径" in auth_check["next_step"]
    assert report["healthy"] is False


def test_doctor_detects_wb_auth_required_with_plain_reason(tmp_path):
    """实机判据②: WB 的 Authentication required → 「宿主 CLI 未登录」人话原因 + 修法。"""
    root = _mkroot(tmp_path)
    wb_log = tmp_path / "wb-wake.log"
    wb_log.write_text(WB_AUTH_LINE + "\n", encoding="utf-8")
    report = doctor_report(root, wb_wake_log=wb_log, launch_agents_dir=_mkla(tmp_path, "ZC"))
    auth_check = _check(report, "host_auth")
    assert auth_check["ok"] is False
    assert "未登录" in auth_check["detail"]
    assert "登录" in auth_check["next_step"] and "doctor" in auth_check["next_step"]


def test_doctor_last_wake_distinguishes_fake_success(tmp_path):
    """③ 被拉起 ≠ 真消费: no_progress fail 行 = 假成功，必须报失败并给下一步。"""
    root = _mkroot(tmp_path)
    la = _mkla(tmp_path, "ZC")
    (root / "wake-attempts.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-09-29T09:09:12Z",
                "agent": "ZC",
                "route": "belt",
                "attempt": 5,
                "outcome": "fail",
                "error_class": "no_progress",
                "executor": "zcode-drain",
                "latency_ms": 1000,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = doctor_report(root, wb_wake_log=tmp_path / "no-such-wb.log", launch_agents_dir=la)
    check = _check(report, "last_wake")
    assert check["ok"] is False
    assert "假成功" in check["detail"] and "没真消费" in check["detail"]
    assert check["next_step"]


def test_doctor_routing_check_uses_effective_route(tmp_path):
    """④ 路由检查依赖 A-1 解析：非绝对路径 command 判失败并给修法；
    绝对路径 command 健康。"""
    root_bad = _mkroot(
        tmp_path, agents_cfg={"ZC": {"adapter": "local-command", "command": ["codex", "--mail"]}}
    )
    report = doctor_report(
        root_bad, wb_wake_log=tmp_path / "no-such.log", launch_agents_dir=_mkla(tmp_path, "ZC")
    )
    routing = _check(report, "routing")
    assert routing["ok"] is False
    assert "绝对路径" in routing["next_step"]
    # 顺手验 ⑤ 积压计数面（pending 信在箱 = 计数进 detail，不误判失败）
    st = MailStore(root_bad)
    st.register("HS")
    st.send("HS", "ZC", "ping", "x")
    report2 = doctor_report(
        root_bad, wb_wake_log=tmp_path / "no-such.log", launch_agents_dir=_mkla(tmp_path, "ZC")
    )
    backlog = _check(report2, "backlog")
    assert backlog["ok"] is True and "pending=1" in backlog["detail"]


def test_doctor_cli_exit_codes_and_json(tmp_path, capsys):
    """CLI 面: doctor 健康退出 0、有断点退出 1；--json 可解析；子命令已注册。"""
    assert "doctor" in SUBCOMMANDS
    root = _mkroot(tmp_path)
    (root / "wake-daemon.log").write_text(CODEX_SPAWN_LINE + "\n", encoding="utf-8")
    rc = cli_main(["doctor", "--home", str(root), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1 and payload["healthy"] is False
    # 无 wake.json 的空根 → ② 直接判未安装
    empty = tmp_path / "empty-root"
    empty.mkdir()
    rc2 = cli_main(["doctor", "--home", str(empty), "--json"])
    payload2 = json.loads(capsys.readouterr().out)
    loaded = next(c for c in payload2["checks"] if c["id"] == "wake_loaded")
    assert rc2 == 1 and loaded["ok"] is False and "install" in loaded["next_step"]


# ---------------------------------------------------------- 失败必响（run --once）


def test_run_once_exit_nonzero_on_spawn_failure(tmp_path, monkeypatch, capsys):
    """spawn failed → `wake run --once` 必须非零退出（SystemExit 1），禁 rc=0。"""
    root = tmp_path / "mail"
    monkeypatch.setenv("AGENT_MAIL_HOME", str(root))
    st = MailStore(root=root)
    st.register("HS")
    st.register("ZC")
    st.send("HS", "ZC", "hello", "wake me")
    (root / "wake.json").write_text(
        json.dumps(
            {
                "agent_id": "ZC",
                "adapter": "local-command",
                "command": ["definitely-not-a-real-binary-xyz-12345"],
                "retry_max": 1,
                "retry_interval": 0,
                "agents": {
                    "ZC": {
                        "adapter": "local-command",
                        "command": ["definitely-not-a-real-binary-xyz-12345"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as ei:
        wake_main(["run", "--agent", "ZC", "--root", str(root), "--once"])
    assert ei.value.code == 1
    assert "失败必响" in capsys.readouterr().err
    # 反证（rc=0 的世界已死）: 修好后同一命令退出 0
    ok_cmd = [sys.executable, "-c", "pass"]
    (root / "wake.json").write_text(
        json.dumps(
            {
                "agent_id": "ZC",
                "adapter": "local-command",
                "command": ok_cmd,
                "agents": {"ZC": {"adapter": "local-command", "command": ok_cmd}},
            }
        ),
        encoding="utf-8",
    )
    wake_main(["run", "--agent", "ZC", "--root", str(root), "--once"])  # 不抛 = rc 0


def test_run_once_exit_nonzero_on_round_error(tmp_path, monkeypatch):
    """整轮异常（fail-open 吞掉）也不许 rc=0：round_error 进 stats → exit 1。"""
    root = tmp_path / "mail"
    monkeypatch.setenv("AGENT_MAIL_HOME", str(root))
    MailStore(root=root)
    (root / "wake.json").write_text(
        json.dumps({"agent_id": "ZC", "adapter": "hermes"}), encoding="utf-8"
    )
    import argparse

    import agent_mailbox.wake as wk

    def boom(*a, **k):
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(wk, "MailStore", boom)  # run_once 内部构造即抛 → outer except
    cfg = WakeConfig.load(root)
    assert cfg is not None
    with pytest.raises(SystemExit) as ei:
        wk._cmd_run(
            argparse.Namespace(
                agent="ZC", root=str(root), once=True, adapter="", command="", webhook_url=""
            )
        )
    assert ei.value.code == 1


# ------------------------------------------------- 告警收件人 + 未登录态检测


def test_wake_alert_recipients_never_boss(tmp_path):
    """告警收件人 = HS（负责方）+ 已注册发件人，绝不含 boss（boss 仅存档）。"""
    root = tmp_path / "mail"
    st = MailStore(root=root)
    for aid in ("boss", "HS", "ZC"):
        st.register(aid)
    st.send("HS", "ZC", "please handle", "body")
    letter = next((root / "inbox" / "ZC").glob("*.json"))
    msg = json.loads(letter.read_text(encoding="utf-8"))
    sent = send_wake_alert(st, root, "ZC", msg)
    assert sent is True
    # register() 会给 boss 建空箱：断言点是「boss 箱里没有告警信」，不是目录不存在
    assert not list((root / "inbox" / "boss").glob("*.json")), "boss 席不收告警"
    # 告警信落在 HS 箱
    hs_letters = [
        json.loads(p.read_text(encoding="utf-8")) for p in (root / "inbox" / "HS").glob("*.json")
    ]
    assert any("[wake-fail]" in m["subject"] for m in hs_letters)
    # 幂等：重读落盘的 letter（handled_log 已带 wake_alert 标记）后不再发
    msg2 = json.loads(letter.read_text(encoding="utf-8"))
    assert send_wake_alert(st, root, "ZC", msg2) is False


def test_local_command_auth_signature_fails_even_with_exit_zero():
    """WB 现场真故障回归: exit 0 但输出 Authentication required → 判失败。"""
    adapter = LocalCommandAdapter(
        [sys.executable, "-c", "print('Authentication required. Please use /login')"]
    )
    assert adapter.deliver({"id": "m"}) is False
    assert adapter.last_error_class == "auth_required"
    # 干净 exit 0 照旧成功
    ok = LocalCommandAdapter([sys.executable, "-c", "print('all good'); import sys; sys.exit(0)"])
    assert ok.deliver({"id": "m"}) is True
    # exit 1 + 认证特征也判 auth_required（根因优先于笼统 nonzero）
    both = LocalCommandAdapter(
        [sys.executable, "-c", "import sys; print('未登录，请先登录'); sys.exit(1)"]
    )
    assert both.deliver({"id": "m"}) is False
    assert both.last_error_class == "auth_required"


def test_belt_script_alert_recipient_is_hs_only():
    """belt 模板（仓内部件）: recipients 不再拼 boss，告警只发 HS。"""
    text = (
        Path(__file__)
        .resolve()
        .parents[1]
        .joinpath("scripts", "wake-zc.sh")
        .read_text(encoding="utf-8")
    )
    assert (
        'recipients = ["HS"]' in text or "recipients = [ALERT_RECIPIENT] + sorted(senders)" in text
    )
    assert 'sorted(senders) + ["boss"]' not in text
