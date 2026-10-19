"""Wake CLI surface: status / toggle / install / uninstall (all opt-in, no daemon)."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from agent_mailbox import workbench_wake as wk
from agent_mailbox.workbench_wake import PLIST_LABEL, cli_main, load_state, save_state, state_lock


def test_status_and_toggle_only_touch_our_own_state(tmp_path):
    assert cli_main(["--status"], home=tmp_path) == 0
    assert load_state(tmp_path)["enabled"] is True, "wake is on by default once installed"
    assert cli_main(["--disable"], home=tmp_path) == 0
    assert load_state(tmp_path)["enabled"] is False, "disable must be respected"
    assert cli_main(["--enable"], home=tmp_path) == 0
    assert load_state(tmp_path)["enabled"] is True


def test_toggle_waits_for_wake_lock_and_preserves_new_delivery_state(tmp_path):
    """A toggle must not replace a worker's newer wake watermark or backup view."""
    started = threading.Event()
    done = threading.Event()
    result = []

    def disable():
        started.set()
        try:
            result.append(cli_main(["--disable"], home=tmp_path))
        finally:
            done.set()

    worker = threading.Thread(target=disable, daemon=True)
    with state_lock(tmp_path):
        worker.start()
        assert started.wait(1)
        assert not done.wait(0.1), "toggle must wait for the same lock as delivery and backup"
        state = load_state(tmp_path)
        state["cold_done"] = True
        state["employees"] = {"e1": {"seen": ["new-message"], "wakes": [123.0]}}
        save_state(tmp_path, state)
    worker.join(2)
    assert not worker.is_alive()
    assert result == [0]
    state = load_state(tmp_path)
    assert state["enabled"] is False
    assert state["employees"]["e1"] == {"seen": ["new-message"], "wakes": [123.0]}


def test_install_and_uninstall_are_opt_in_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(wk.sys, "platform", "darwin")
    monkeypatch.setattr(wk.os, "getuid", lambda: 501, raising=False)
    calls = []

    def bootout(cmd, **kwargs):
        calls.append(cmd)
        assert cmd == ["launchctl", "bootout", f"gui/501/{PLIST_LABEL}"]
        return wk.subprocess.CompletedProcess(cmd, 3, "", "not loaded")

    monkeypatch.setattr(wk.subprocess, "run", bootout)
    agents = tmp_path / "agents"
    assert cli_main(["--install", "--plist-dir", str(agents)], home=tmp_path) == 0
    assert calls == [], "install must not load a service"
    unit = agents / f"{PLIST_LABEL}.plist"
    assert unit.is_file(), "install must write the launchd unit"
    assert cli_main(["--uninstall", "--plist-dir", str(agents)], home=tmp_path) == 0
    assert not unit.exists(), "uninstall must remove it"
    assert cli_main(["--uninstall", "--plist-dir", str(agents)], home=tmp_path) == 0, "idempotent"
    assert len(calls) == 2, "macOS must attempt bootout even when the unit is already absent"


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_install_rejects_non_macos_without_creating_a_plist(
    tmp_path, monkeypatch, capsys, platform
):
    monkeypatch.setattr(wk.sys, "platform", platform)
    agents = tmp_path / "agents"
    assert cli_main(["--install", "--plist-dir", str(agents)], home=tmp_path) == 2
    assert not agents.exists()
    assert not (tmp_path / "wake").exists()
    assert "macOS" in capsys.readouterr().out


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_uninstall_removes_unit_without_launchd_on_other_platforms(
    tmp_path, monkeypatch, capsys, platform
):
    monkeypatch.setattr(wk.sys, "platform", platform)

    def forbidden(*args, **kwargs):
        pytest.fail("non-macOS uninstall must not invoke launchctl or getuid")

    monkeypatch.setattr(wk.subprocess, "run", forbidden)
    monkeypatch.setattr(wk.os, "getuid", forbidden, raising=False)
    agents = tmp_path / "agents"
    agents.mkdir()
    unit = agents / f"{PLIST_LABEL}.plist"
    unit.write_text("<plist/>", encoding="utf-8")
    assert cli_main(["--uninstall", "--plist-dir", str(agents)], home=tmp_path) == 0
    assert not unit.exists()
    assert cli_main(["--uninstall", "--plist-dir", str(agents)], home=tmp_path) == 0
    assert "launchctl" not in capsys.readouterr().out


def test_usage_is_printed_when_no_action_is_given(tmp_path, capsys):
    assert cli_main([], home=tmp_path) == 0
    assert "用法" in capsys.readouterr().out, "bare invocation must explain the surface"


def test_cold_then_warm_transition_via_cli(tmp_path, monkeypatch):
    """回归 ✓：`--once` 第一次=冷启动（不叫 ✓）第二次=常温（会叫 ✓）。

    这条本该抓到"cold_done 没落进 run_once"✗ —— 我当时只断言了字符串存在 ✓ 没断言位置 ✗。
    """
    from agent_mailbox.workbench_wake import PLIST_LABEL, cli_main, load_state, state_dir

    fake_store = SimpleNamespace(
        _transaction=lambda **_kw: _Ctx(_Mail([])),
        snapshot=lambda: {"employees": [{"id": "e1", "lifecycle": "active"}]},
        update_maintenance=lambda: {"paused": False},
    )
    assert cli_main(["--once"], home=tmp_path, store=fake_store) == 0
    assert load_state(tmp_path).get("cold_done") is True, "cold pass must persist the flag"
    assert not (state_dir(tmp_path) / "wake-outbox").exists(), "cold pass must not wake anybody"
    assert PLIST_LABEL  # 保持导入被使用 ✓


class _Mail:
    """假信箱 ✓：id 列表可变 ⇒ 能模拟"来了新信"✓（第一版两次给同一个 id ✗ 所以测不出唤醒 ✓）。"""

    def __init__(self, ids):
        self.ids = list(ids)


class _Rows(list):
    def fetchall(self):
        return list(self)


class _Ctx:
    def __init__(self, mail):
        self.mail = mail

    def __enter__(self):
        mail = self.mail

        class _Db:
            def execute(self, sql, *_args, **_kwargs):
                if "FROM notifications" in sql:
                    return _Rows([])  # Legacy mail fixture has no native intents.
                return _Rows(
                    [{"id": i, "status": None, "cancel_requested": None} for i in mail.ids]
                )

        return _Db()

    def __exit__(self, *_exc):
        return False


def _store(mail):
    from types import SimpleNamespace

    return SimpleNamespace(
        _transaction=lambda **_kw: _Ctx(mail),
        snapshot=lambda: {"employees": [{"id": "e1", "lifecycle": "active"}]},
        update_maintenance=lambda: {"paused": False},
    )


def test_warm_pass_without_binding_preserves_pending_not_host_success(tmp_path):
    from agent_mailbox.workbench_wake import cli_main, state_dir

    mail = _Mail(["m1"])
    store = _store(mail)
    cli_main(["--once"], home=tmp_path, store=store)
    store.root = tmp_path
    mail.ids.insert(0, "m2")
    cli_main(["--once"], home=tmp_path, store=store)
    assert (state_dir(tmp_path) / "claims/m2.pending").read_text() == "e1"
    assert not list((state_dir(tmp_path) / "wake-outbox").glob("*.json"))
    assert not load_state(tmp_path)["employees"]["e1"].get("wakes")
    cli_main(["--once"], home=tmp_path, store=store)
    assert not list((state_dir(tmp_path) / "wake-outbox").glob("*.json"))


def test_status_shows_pending_and_unknown(tmp_path, capsys):
    """AM-01/AM-02 visibility: `--status` must name the two non-delivered states.

    HS (AM-01): writing .pending without ever reading it is silent loss.
    Codex (AM-02): an unknown outcome must be surfaced for a human, never auto-retried.
    """
    from agent_mailbox.workbench_wake import cli_main, hook_deliver

    home = tmp_path / "home"
    (home / "wake").mkdir(parents=True)
    assert (
        hook_deliver(home)(
            "employee_demo", {"reason": "new_mail", "unread": 1, "fresh": ["m-alpha"]}
        )
        is False
    )
    (home / "wake" / "claims" / "m-beta.unknown").write_text("employee_demo", encoding="utf-8")

    # A prior not_received marker can remain when the next attempt becomes unknown.
    (home / "wake" / "claims" / "m-beta.pending").write_text("employee_demo")
    assert cli_main(["--status"], home=home) == 0
    out = capsys.readouterr().out
    assert "未接通待办 1 封" in out, "未接通待办必须可见 ✓（AM-01）"
    assert "未知交付 1 封" in out, "未知交付必须可见 ✓（AM-02）"
    assert "不自动重投" in out, "未知交付必须写明不自动重投 ✗"
