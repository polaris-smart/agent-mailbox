"""t-62（判据7+S4）: LLM 可选 — digest 纯本地信件流转。

判据7: 机器上没有任何 CLI 登录态，信件仍能流转（读信 → 写摘要 → 标 done
→ 列「建议回复」），不卡看门狗、不烧额度。LLM 是增强不是必需路径。

- 无登录态全链路: wake 投递命中 Authentication required → 自动降级 digest，
  信标 done、摘要落盘、告警照发（降级事实进 handled_log 与告警文本）。
- spawn 失败同样降级（codex 不在 PATH 的现场回归）。
- digest 文件内容结构 + done 标记 + handled_log 摘要锚。
- 不烧额度: digest 路径断言零 subprocess、零网络（socket 一碰就炸）。
- digest_fallback=false 恢复 0.7.5 语义（信留在收件箱重投）。
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from agent_mailbox.cli import SUBCOMMANDS, cli_main
from agent_mailbox.digest import DIGEST_ACTION, digest_path, run_digest
from agent_mailbox.store import MailStore
from agent_mailbox.wake import wake_main


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("AGENT_MAIL_HOME", "")
    root = tmp_path / "mail"
    st = MailStore(root)
    st.register("HS")
    st.register("ZC")
    return root, st


def _wake_json(root: Path, command: list[str], *, digest_fallback: bool | None = None) -> None:
    data: dict = {
        "agent_id": "ZC",
        "adapter": "hermes",
        "webhook": {"url": "http://127.0.0.1:9/gw"},
        "retry_max": 1,
        "retry_interval": 0,
        "agents": {"ZC": {"adapter": "local-command", "command": command}},
    }
    if digest_fallback is not None:
        data["digest_fallback"] = digest_fallback
    (root / "wake.json").write_text(json.dumps(data), encoding="utf-8")


def _woken_letter(root: Path) -> dict:
    letters = list((root / "inbox" / "ZC").glob("*.json"))
    assert len(letters) == 1
    return json.loads(letters[0].read_text(encoding="utf-8"))


# ------------------------------------------------- 无登录态全链路（判据7 主链）


def test_auth_required_degrades_to_digest_end_to_end(env, capsys):
    """WB 现场回归: CLI 未登录（exit 0 + Authentication required）→ 信不卡死、
    不静默：降级 digest → 标 done + 摘要落盘 + 告警（含降级事实）+ 非零退出。"""
    root, st = env
    st.send("HS", "ZC", "上会前要看的数据", "这是正文，需要 ZC 处理后回报。")
    _wake_json(root, [sys.executable, "-c", "print('Authentication required. Please use /login')"])
    with pytest.raises(SystemExit) as ei:  # 失败必响不回退：投递失败 rc 仍非零
        wake_main(["run", "--agent", "ZC", "--root", str(root), "--once"])
    assert ei.value.code == 1
    assert "降级" in capsys.readouterr().err
    letter = _woken_letter(root)
    assert letter["status"] == "done"  # 信被消化，不再卡看门狗
    actions = [e["action"] for e in letter["handled_log"]]
    assert DIGEST_ACTION in actions and "wake" not in actions
    digest_entry = next(e for e in letter["handled_log"] if e["action"] == DIGEST_ACTION)
    assert "auth_required" in digest_entry["note"] and "digest:" in digest_entry["note"]
    # 摘要落盘（信不丢：内容在 digest 里）
    md = digest_path(root).read_text(encoding="utf-8")
    assert "上会前要看的数据" in md and "这是正文" in md and "零网络" in md
    assert "待人工判断" in md  # 建议回复占位清单
    # 告警照发（只发负责方 HS），且降级事实进告警文本
    hs_letters = [
        json.loads(p.read_text(encoding="utf-8")) for p in (root / "inbox" / "HS").glob("*.json")
    ]
    alert = next(m for m in hs_letters if "[wake-fail]" in m["subject"])
    assert "降级" in alert["body"] and "digest" in alert["body"]
    # pending 归零
    assert st.check("ZC", mark=False) == []


def test_spawn_failure_degrades_to_digest(env):
    """codex 现场回归: 命令不存在（spawn_failed）→ 同样降级 digest。"""
    root, st = env
    st.send("HS", "ZC", "deploy", "body")
    _wake_json(root, ["definitely-not-a-real-binary-xyz-12345"])
    with pytest.raises(SystemExit):
        wake_main(["run", "--agent", "ZC", "--root", str(root), "--once"])
    letter = _woken_letter(root)
    assert letter["status"] == "done"
    digest_entry = next(e for e in letter["handled_log"] if e["action"] == DIGEST_ACTION)
    assert "spawn_failed" in digest_entry["note"]  # 直配命令不存在 = spawn_failed
    assert digest_path(root).exists()


# ------------------------------------------------- digest 引擎自身


def test_digest_file_structure_and_truncation(env):
    """digest 文件结构: 主题/摘要截断/时间/优先级/建议回复占位，逐字段在位。"""
    root, st = env
    long_body = "字" * 900
    st.send("HS", "ZC", "长信标题", long_body)
    st.send("boss", "ZC", "短信标题", "很短的正文", priority="high")
    r1 = run_digest(root, "ZC")
    assert r1["digested"] == 2 and r1["digest_file"]
    md = Path(r1["digest_file"]).read_text(encoding="utf-8")
    assert "长信标题" in md and "截断" in md  # 正文 500 字符截断标注
    assert md.count("字") < 900  # 摘要不是全文
    assert "HS" in md and "boss" in md and "high" in md
    assert "待人工判断" in md and "未调用 LLM" in md
    # 第二次跑: pending 为 0，不再产出（幂等，不重复消化）
    r2 = run_digest(root, "ZC")
    assert r2["digested"] == 0
    # done 标记 + handled_log 摘要锚
    letters = [
        json.loads(p.read_text(encoding="utf-8")) for p in (root / "inbox" / "ZC").glob("*.json")
    ]
    for m in letters:
        assert m["status"] == "done"
        assert any(e["action"] == DIGEST_ACTION for e in m["handled_log"])


def test_digest_zero_subprocess_zero_network(env, monkeypatch):
    """不烧额度: digest 路径零 subprocess、零网络——真炸了才算数。"""

    def no_subprocess(*a, **k):  # 任何 spawn = 违规
        raise AssertionError("digest 路径不得起子进程")

    def no_socket(*a, **k):  # 任何网络 = 违规
        raise AssertionError("digest 路径不得有网络")

    monkeypatch.setattr(subprocess, "Popen", no_subprocess)
    monkeypatch.setattr(subprocess, "run", no_subprocess)
    monkeypatch.setattr(socket, "socket", no_socket)
    monkeypatch.setattr(socket, "create_connection", no_socket)
    root, st = env
    st.send("HS", "ZC", "no-llm", "纯本地")
    out = run_digest(root, "ZC")
    assert out["digested"] == 1 and out["digest_file"]


def test_digest_fallback_disabled_keeps_letter_pending(env, capsys):
    """digest_fallback=false → 恢复 0.7.5 语义: 信 release 回 pending 等重投，
    不产出摘要（存量行为开关不回退）。"""
    root, st = env
    st.send("HS", "ZC", "hold", "body")
    _wake_json(root, ["definitely-not-a-real-binary-xyz-12345"], digest_fallback=False)
    with pytest.raises(SystemExit):
        wake_main(["run", "--agent", "ZC", "--root", str(root), "--once"])
    letter = _woken_letter(root)
    assert letter["status"] == "pending"  # 信不丢，等下一个触发重投
    assert not digest_path(root).exists()
    assert all(e["action"] != DIGEST_ACTION for e in letter["handled_log"])


# ------------------------------------------------- CLI 子命令


def test_digest_cli_subcommand_and_json(env, capsys):
    """`agent-mailbox digest` 手动触发全量 digest；--json 可解析；已注册进
    CLI_SUBCOMMANDS（server 路由面）。"""
    root, st = env
    assert "digest" in SUBCOMMANDS
    assert (
        "digest" in __import__("agent_mailbox.server", fromlist=["CLI_SUBCOMMANDS"]).CLI_SUBCOMMANDS
    )
    st.send("HS", "ZC", "cli-digest", "body-1")
    st.send("boss", "ZC", "cli-digest-2", "body-2")
    rc = cli_main(["digest", "--home", str(root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "消化 2 封" in out and "零网络" in out and "建议回复清单" in out
    # 再跑: 无 pending → 空转不炸
    rc2 = cli_main(["digest", "--home", str(root), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc2 == 0 and payload["total"] == 0
    # 单身份限定
    st.send("HS", "ZC", "again", "body-3")
    rc3 = cli_main(["digest", "--agent", "ZC", "--home", str(root)])
    out3 = capsys.readouterr().out
    assert rc3 == 0 and "消化 1 封" in out3
