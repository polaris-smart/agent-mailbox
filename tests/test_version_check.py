"""Tests for v0.7.6 E 单元 — 版本检查 + 提示 + 点击更新（t-53）。

覆盖判据五条：①版本比较与提示文案 ②24h 限流（第二次查直接走缓存）
③fail-open（断网/坏 JSON 不抛异常返回 None）④安装方式探测三分支
⑤upgrade 拼装命令正确且执行前有审计。网络全程 mock（注入 fetch /
monkeypatch 模块函数），绝不真联网。
"""

import http.client
import json
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from agent_mailbox import version_check as vc
from agent_mailbox import web
from agent_mailbox.cli import cli_main
from agent_mailbox.server import CLI_SUBCOMMANDS
from agent_mailbox.store import MailStore

UV_CMD = ["uv", "tool", "upgrade", "agent-mailbox"]
PIPX_CMD = ["pipx", "upgrade", "agent-mailbox"]


def _which_uv(name: str) -> str | None:
    return "/usr/local/bin/uv" if name == "uv" else None


def _which_pipx(name: str) -> str | None:
    return "/opt/homebrew/bin/pipx" if name == "pipx" else None


def _fetch_json(version: str, calls: list):
    def get(url, timeout=3.0):
        calls.append(url)
        return json.dumps({"info": {"version": version}})

    return get


def _latest_spy(version: str, calls: list):
    """Replacement for ``vc.fetch_latest_version`` (keyword-only signature)."""

    def latest(*, fetch=None, timeout=3.0):
        calls.append(vc.PYPI_URL)
        return version

    return latest


# ------------------------------------------------------- ① 比较与提示文案


def test_is_newer_numeric_compare():
    assert vc._is_newer("0.7.7", "0.7.6")
    assert vc._is_newer("1.0", "0.9.9")
    assert not vc._is_newer("0.7.6", "0.7.6")
    assert not vc._is_newer("0.7.5", "0.7.6")
    assert not vc._is_newer("garbage!!", "")  # 解析异常按无新版（fail-open）


def test_fetch_latest_version_reads_pypi_info_version():
    calls: list = []
    get = _fetch_json("1.2.3", calls)
    assert vc.fetch_latest_version(fetch=get) == "1.2.3"
    assert calls == [vc.PYPI_URL]  # 只打 PyPI 版本级端点


def test_update_notice_carries_current_latest_and_command():
    info = {
        "current": "0.7.6",
        "latest": "0.7.7",
        "update_available": True,
        "command": UV_CMD,
    }
    text = vc.format_update_notice(info)
    assert "0.7.6" in text and "0.7.7" in text
    assert "uv tool upgrade agent-mailbox" in text  # README 口径照抄


def test_check_for_update_assembles_notice(tmp_path):
    info = vc.check_for_update(
        tmp_path, fetch=_fetch_json("9.9.9", []), now=1000.0, which=_which_uv
    )
    assert info is not None
    assert info["current"] == vc.current_version()
    assert info["latest"] == "9.9.9"
    assert info["update_available"] is True
    assert info["command"] == UV_CMD
    assert (tmp_path / "version_check.json").exists()  # 缓存落在邮件根


# --------------------------------------------- ② 24h 限流（第二次走缓存）


def test_check_rate_limited_by_24h_cache(tmp_path):
    calls: list = []
    first = vc.check_for_update(tmp_path, fetch=_fetch_json("9.9.9", calls), now=1000.0)
    assert first is not None and not first["cached"]
    # 23h 后第二次查：直接走缓存，不再联网
    second = vc.check_for_update(
        tmp_path, fetch=_fetch_json("9.9.9", calls), now=1000.0 + 23 * 3600
    )
    assert calls == [vc.PYPI_URL]  # 全程只打过一次网络
    assert second is not None and second["cached"] and second["latest"] == "9.9.9"
    # 超过 24h 才重新联网
    third = vc.check_for_update(tmp_path, fetch=_fetch_json("9.9.9", calls), now=1000.0 + 25 * 3600)
    assert len(calls) == 2 and third is not None and not third["cached"]


def test_force_bypasses_cache(tmp_path):
    calls: list = []
    vc.check_for_update(tmp_path, fetch=_fetch_json("1.0.0", calls), now=1000.0)
    forced = vc.check_for_update(
        tmp_path, fetch=_fetch_json("2.0.0", calls), now=1001.0, force=True
    )
    assert len(calls) == 2 and forced is not None and forced["latest"] == "2.0.0"


# ------------------------------------------------- ③ fail-open（静默跳过）


def test_fail_open_offline_returns_none(tmp_path):
    def boom(url, timeout=3.0):
        raise OSError("network down")

    assert vc.fetch_latest_version(fetch=boom) is None  # 不抛
    assert vc.check_for_update(tmp_path, fetch=boom) is None  # 静默跳过


def test_fail_open_bad_json_no_cache_write(tmp_path):
    assert vc.fetch_latest_version(fetch=lambda u, t: "{not json") is None
    assert vc.fetch_latest_version(fetch=lambda u, t: json.dumps({"info": {}})) is None
    assert vc.check_for_update(tmp_path, fetch=lambda u, t: "{oops") is None
    assert not (tmp_path / "version_check.json").exists()  # 失败不写缓存，下次可重试
    # 同一根随后查到好数据仍能正常工作
    ok = vc.check_for_update(tmp_path, fetch=_fetch_json("1.0.0", []))
    assert ok is not None and ok["latest"] == "1.0.0"


# -------------------------------------------------- ④ 安装方式探测三分支


def test_detect_uv_tool_wins():
    assert vc.detect_install_method(which=_which_uv) == ("uv", UV_CMD)


def test_detect_pipx_second():
    assert vc.detect_install_method(which=_which_pipx) == ("pipx", PIPX_CMD)


def test_detect_pip_fallback_pins_current_interpreter():
    method, cmd = vc.detect_install_method(which=lambda name: None)
    assert method == "pip"
    assert cmd == [sys.executable, "-m", "pip", "install", "--upgrade", "agent-mailbox"]


# --------------------------------------- ⑤ upgrade 拼装 + 执行前/后审计


def test_upgrade_dry_run_shows_command_without_running(tmp_path):
    st = MailStore(root=tmp_path / "mail")

    def bomb(cmd):
        raise AssertionError("dry-run 绝不起进程")

    out = vc.run_upgrade(
        tmp_path / "mail", confirm=False, which=_which_uv, runner=bomb, store=st, by="test"
    )
    assert out["executed"] is False and out["command"] == UV_CMD
    trail = st.audit_entries("upgrade_dry_run")
    assert trail and trail[-1]["command"] == UV_CMD and trail[-1]["by"] == "test"


def test_upgrade_executes_external_command_with_start_and_result_audit(tmp_path):
    ran: list = []
    st = MailStore(root=tmp_path / "mail")

    def runner(cmd):
        ran.append(list(cmd))
        return SimpleNamespace(returncode=0)

    out = vc.run_upgrade(
        tmp_path / "mail", confirm=True, which=_which_pipx, runner=runner, store=st, by="test"
    )
    assert ran == [PIPX_CMD]  # 唯一形态 = 起外部命令（禁 self-update）
    assert out["ok"] is True and out["exit_code"] == 0
    starts = st.audit_entries("upgrade_start")
    results = st.audit_entries("upgrade_result")
    assert starts and starts[-1]["command"] == PIPX_CMD  # 执行前留痕
    assert results and results[-1]["exit_code"] == 0 and results[-1]["ok"] is True


def test_upgrade_failure_audits_nonzero_exit(tmp_path):
    st = MailStore(root=tmp_path / "mail")
    out = vc.run_upgrade(
        tmp_path / "mail",
        confirm=True,
        which=lambda n: None,
        runner=lambda cmd: SimpleNamespace(returncode=3),
        store=st,
        by="test",
    )
    assert out["ok"] is False and out["exit_code"] == 3
    assert st.audit_entries("upgrade_result")[-1]["exit_code"] == 3


def test_upgrade_runner_crash_is_caught_not_raised(tmp_path):
    st = MailStore(root=tmp_path / "mail")

    def boom(cmd):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=600)

    out = vc.run_upgrade(
        tmp_path / "mail", confirm=True, which=lambda n: None, runner=boom, store=st, by="test"
    )
    assert out["ok"] is False and out["exit_code"] is None
    entry = st.audit_entries("upgrade_result")[-1]
    assert entry["exit_code"] is None and "error" in entry  # 异常只留审计不抛出


# ------------------------------------------------------------- web 一键入口


@pytest.fixture()
def board(tmp_path):
    store = MailStore(root=tmp_path / "mail")
    handler = type("H", (web._BoardHandler,), {"store": store, "token": "test-token-123"})
    srv = web.LoopbackServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield port, store
    srv.shutdown()
    srv.server_close()


def _req(port, path, *, token=None, method="GET", body=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    payload = json.dumps(body) if body is not None else None
    if payload:
        headers["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"{}")
    finally:
        conn.close()


def test_local_only_guard_rejects_non_loopback_peer():
    h = object.__new__(web._BoardHandler)
    h.client_address = ("192.168.1.7", 51000)
    assert web._BoardHandler._local_only(h) is False
    h.client_address = ("127.0.0.1", 51000)
    assert web._BoardHandler._local_only(h) is True


def test_api_version_requires_token(board):
    port, _ = board
    assert _req(port, "/api/version")[0] == 401  # 无凭据不得触发


def test_api_version_rate_limited_and_fail_open(board, monkeypatch):
    port, store = board
    calls: list = []
    monkeypatch.setattr(vc, "fetch_latest_version", lambda *, fetch=None: None)
    status, body = _req(port, "/api/version", token="test-token-123")
    assert status == 200 and body["latest"] is None and body["update_available"] is False

    monkeypatch.setattr(vc, "fetch_latest_version", _latest_spy("9.9.9", calls))
    status, first = _req(port, "/api/version", token="test-token-123")
    assert status == 200 and first["update_available"] and first["latest"] == "9.9.9"
    _status, second = _req(port, "/api/version", token="test-token-123")
    assert calls == [vc.PYPI_URL]  # 看板刷新不打爆 PyPI：24h 缓存内不重查
    assert second["cached"] is True
    assert (store.root / "version_check.json").exists()


def test_api_upgrade_dry_run_returns_command_without_executing(board, monkeypatch):
    port, _store = board
    monkeypatch.setattr(vc, "detect_install_method", lambda *, which=None: ("uv", UV_CMD))
    monkeypatch.setattr(
        vc, "_default_runner", lambda cmd: (_ for _ in ()).throw(AssertionError("no spawn"))
    )
    status, body = _req(port, "/api/upgrade", token="test-token-123", method="POST", body={})
    assert status == 200 and body["executed"] is False and body["command"] == UV_CMD


def test_api_upgrade_confirm_runs_external_command_and_audits(board, monkeypatch):
    port, store = board
    monkeypatch.setattr(vc, "detect_install_method", lambda *, which=None: ("pipx", PIPX_CMD))
    monkeypatch.setattr(vc, "_default_runner", lambda cmd: SimpleNamespace(returncode=0))
    status, body = _req(
        port, "/api/upgrade", token="test-token-123", method="POST", body={"confirm": True}
    )
    assert status == 200 and body["ok"] is True and body["method"] == "pipx"
    assert store.audit_entries("upgrade_start")[-1]["command"] == PIPX_CMD
    assert store.audit_entries("upgrade_result")[-1]["exit_code"] == 0


def test_board_page_has_upgrade_button_and_command_display(board):
    port, _ = board
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", "/board", headers={"Authorization": "Bearer test-token-123"})
        resp = conn.getresponse()
        assert resp.status == 200
        body = resp.read()
    finally:
        conn.close()
    assert b'id="upgrade"' in body  # 看板一键入口
    assert b"/api/upgrade" in body and b"will run: " in body  # 执行前显示完整命令


# ------------------------------------------------------------- CLI 子命令


def test_upgrade_registered_in_both_routing_tables():
    assert "upgrade" in CLI_SUBCOMMANDS
    import agent_mailbox.cli as cli_mod

    assert "upgrade" in cli_mod.SUBCOMMANDS


def test_cli_upgrade_check_prints_notice(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(vc, "fetch_latest_version", lambda *, fetch=None: "9.9.9")
    monkeypatch.setattr(vc, "detect_install_method", lambda *, which=None: ("uv", UV_CMD))
    rc = cli_main(["upgrade", "--check", "--home", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "9.9.9" in out and "uv tool upgrade agent-mailbox" in out  # 判据② 三要素


def test_cli_upgrade_check_offline_fail_open(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(vc, "fetch_latest_version", lambda *, fetch=None: None)
    rc = cli_main(["upgrade", "--check", "--home", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0 and "不受影响" in out  # 判据⑤：查不到就地收场


def test_cli_upgrade_noninteractive_is_dry_run_only(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(vc, "fetch_latest_version", lambda *, fetch=None: "9.9.9")
    monkeypatch.setattr(vc, "detect_install_method", lambda *, which=None: ("uv", UV_CMD))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))

    def bomb(cmd):
        raise AssertionError("非交互确认前绝不起进程")

    monkeypatch.setattr(vc, "_default_runner", bomb)
    rc = cli_main(["upgrade", "--home", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "uv tool upgrade agent-mailbox" in out  # 完整命令先显示
    assert "仅预览未执行" in out
    st = MailStore(root=tmp_path)
    assert st.audit_entries("upgrade_dry_run")  # dry-run 也留痕


def test_cli_upgrade_yes_executes_and_audits(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(vc, "fetch_latest_version", lambda *, fetch=None: "9.9.9")
    monkeypatch.setattr(vc, "detect_install_method", lambda *, which=None: ("pipx", PIPX_CMD))
    monkeypatch.setattr(vc, "_default_runner", lambda cmd: SimpleNamespace(returncode=0))
    rc = cli_main(["upgrade", "--yes", "--home", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0 and "升级完成" in out
    st = MailStore(root=tmp_path)
    assert st.audit_entries("upgrade_start")[-1]["command"] == PIPX_CMD
    assert st.audit_entries("upgrade_result")[-1]["exit_code"] == 0


def test_cli_upgrade_already_latest_is_noop(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(vc, "fetch_latest_version", lambda *, fetch=None: vc.current_version())
    monkeypatch.setattr(vc, "detect_install_method", lambda *, which=None: ("uv", UV_CMD))
    monkeypatch.setattr(
        vc, "_default_runner", lambda cmd: (_ for _ in ()).throw(AssertionError("no spawn"))
    )
    rc = cli_main(["upgrade", "--yes", "--home", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0 and "已是最新版" in out
