"""v0.7.5 PR A — unified CLI subcommands + legacy-entry compatibility.

Everything runs against tmp roots via --home / AGENT_MAIL_HOME; no real
machine state is consulted.
"""

from __future__ import annotations

import json
import plistlib
from pathlib import Path

import pytest

from agent_mailbox import cli, server
from agent_mailbox.cli import cli_main
from agent_mailbox.store import MailStore


def make_env(tmp_path: Path, *, agents: tuple[str, ...] = ()) -> tuple[Path, Path]:
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    root = tmp_path / "mail"
    if agents:
        MailStore(root).register(agents[0])
    return home, root


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    """Pin Path.home() and PATH to fixture dirs — no test touches the real HOME."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("PATH", str(home / "bin"))
    return home


# ------------------------------------------------------------- routing


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["discover"], "discover"),
        (["--home", "/x", "status", "--json"], "status"),
        (["--web", "8642"], None),
        (["--http", "9000"], None),
        ([], None),
        (["wake", "install"], None),  # wake keeps its own path
        (["--home", "/w", "wake", "install"], None),
    ],
)
def test_subcommand_routing(argv, expected):
    routed = next((a for a in argv if a in server.CLI_SUBCOMMANDS), None)
    assert routed == expected


def test_server_main_dispatches_to_cli(monkeypatch, tmp_path):
    _, root = make_env(tmp_path)
    calls = []

    def fake_cli_main(argv):
        calls.append(list(argv))
        return 0

    import agent_mailbox.cli as cli_mod

    monkeypatch.setattr(cli_mod, "cli_main", fake_cli_main)
    monkeypatch.setattr("sys.argv", ["agent-mailbox", "--home", str(root), "status", "--json"])
    with pytest.raises(SystemExit) as exc:
        server.main()  # must not start any server
    assert exc.value.code == 0
    assert calls == [["status", "--json"]]


def test_server_main_legacy_web_path_untouched(monkeypatch, tmp_path):
    """`agent-mailbox --web 8642` must still reach run_web, not the CLI."""
    ran = {}
    monkeypatch.setattr(
        "agent_mailbox.web.run_web",
        lambda port: ran.setdefault("port", port),
    )
    monkeypatch.setattr("sys.argv", ["agent-mailbox", "--web", "8642"])
    server.main()
    assert ran == {"port": 8642}


# ---------------------------------------------------------- discover CLI


def test_cli_discover_json_with_fixture_home(tmp_path, capsys, fake_home):
    _, root = make_env(tmp_path)
    MailStore(root).register("ZC")
    rc = cli_main(["discover", "--json", "--home", str(root), "--no-save"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["root"] == str(root)
    members = {m["member"]: m for m in out["members"]}
    assert members["ZC"]["registered"] is True
    assert out["supported"]


def test_cli_discover_human_prints_roster(tmp_path, capsys, fake_home):
    _, root = make_env(tmp_path, agents=("ZC",))
    rc = cli_main(["discover", "--home", str(root), "--no-save"])
    text = capsys.readouterr().out
    # 注意：default_context 会扫真实 /Applications（故不断言成员总数），
    # 这里断言 CLI 接线正确 + fixture 名册成员被列出。
    assert rc == 0 and "发现" in text and "ZC" in text


def test_cli_status_reports_wake_and_channels(tmp_path, capsys, monkeypatch, fake_home):
    _, root = make_env(tmp_path, agents=("ZC",))
    rc = cli_main(["status", "--json", "--home", str(root)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["wake"]["configured"] is False
    assert any(m["member"] == "ZC" for m in out["members"])


# -------------------------------------------------------------- test L4


def test_cli_test_json_exit_codes(tmp_path, capsys, fake_home):
    _, root = make_env(tmp_path)
    MailStore(root).register("ZC")  # no receiver → timeout path
    rc = cli_main(["test", "ZC", "--timeout", "0.5", "--json", "--home", str(root)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["ok"] is False and out["next_step"]


# ------------------------------------------------------------ connect CLI


def test_cli_connect_preview_then_yes(tmp_path, capsys, fake_home):
    home, root = make_env(tmp_path)
    cfg_dir = home / ".workbuddy"
    cfg_dir.mkdir()
    cfg = cfg_dir / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {}}))
    # preview: nothing written
    rc = cli_main(["connect", "workbuddy", "--home", str(root)])
    text = capsys.readouterr().out
    assert rc == 0 and "预览" in text and json.loads(cfg.read_text()) == {"mcpServers": {}}
    # --yes: written, backed up, ledgered
    cli_main(["connect", "workbuddy", "--home", str(root), "--yes"])
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["agent-mailbox"]["command"]
    out = json.loads((root / "connect-points.json").read_text())
    assert out[0]["member"] == "workbuddy"


# ---------------------------------------------------------- uninstall CLI


def test_cli_uninstall_restores_and_reports(tmp_path, capsys, monkeypatch, fake_home):
    home, root = make_env(tmp_path)
    cfg_dir = home / ".workbuddy"
    cfg_dir.mkdir()
    cfg = cfg_dir / "mcp.json"
    original = json.dumps({"mcpServers": {"x": {"command": "x"}}})
    cfg.write_text(original)
    cli_main(["connect", "workbuddy", "--home", str(root), "--yes"])
    assert json.loads(cfg.read_text()) != json.loads(original)
    rc = cli_main(
        [
            "uninstall",
            "--home",
            str(root),
            "--no-activate",
            "--launch-agents-dir",
            str(home / "Library" / "LaunchAgents"),
        ]
    )
    text = capsys.readouterr().out
    assert rc == 0
    assert cfg.read_text() == original  # diff == 0
    assert "还原完成" in text


def test_cli_uninstall_residual_exits_nonzero(tmp_path, capsys, monkeypatch, fake_home):
    home, root = make_env(tmp_path)
    cfg_dir = home / ".workbuddy"
    cfg_dir.mkdir()
    cfg = cfg_dir / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {}}))
    cli_main(["connect", "workbuddy", "--home", str(root), "--yes"])
    cfg.write_text(json.dumps({"mcpServers": {}, "human": True}))  # 第三方又改了
    rc = cli_main(
        [
            "uninstall",
            "--home",
            str(root),
            "--no-activate",
            "--launch-agents-dir",
            str(home / "Library" / "LaunchAgents"),
        ]
    )
    text = capsys.readouterr().out
    assert rc == 1 and "残留" in text


def test_cli_uninstall_stops_own_wake_plists(tmp_path, capsys, monkeypatch, fake_home):
    home, root = make_env(tmp_path)
    la_dir = home / "Library" / "LaunchAgents"
    la_dir.mkdir(parents=True)
    plist = la_dir / "com.polaris-smart.agent-mailbox-wake-ZC.plist"
    plist.write_bytes(plistlib.dumps({"Label": "com.polaris-smart.agent-mailbox-wake-ZC"}))
    rc = cli_main(
        ["uninstall", "--home", str(root), "--no-activate", "--launch-agents-dir", str(la_dir)]
    )
    assert rc == 0 and not plist.exists()
    assert "已移除唤醒集成" in capsys.readouterr().out


def test_cli_setup_yes_headless(tmp_path, capsys, fake_home):
    _, root = make_env(tmp_path, agents=("ZC",))
    rc = cli_main(["setup", "--yes", "--home", str(root)])
    text = capsys.readouterr().out
    assert rc == 0
    assert "无头" not in text  # --yes 不打交互向导广告
    assert "初始化完成" in text
    assert (root / "discover-fingerprint.json").exists()  # 指纹落盘


def test_cli_setup_interactive_stays_headless(tmp_path, capsys, fake_home):
    _, root = make_env(tmp_path)
    rc = cli_main(["setup", "--home", str(root)])
    text = capsys.readouterr().out
    assert rc == 0 and "不启浏览器" in text  # 交互模式本单 = 无头 + 挂点说明


# --------------------------------------------------------------- misc


def test_cli_help_lists_subcommands(capsys):
    with pytest.raises(SystemExit):
        cli_main(["--help"])
    text = capsys.readouterr().out
    for sub in cli.SUBCOMMANDS:
        assert sub in text


def test_legacy_wake_subcommand_still_routes(monkeypatch, tmp_path):
    """`agent-mailbox --home X wake status` 仍走 wake_main（旧行为）。"""
    ran = {}
    import agent_mailbox.wake as wake_mod

    monkeypatch.setattr(wake_mod, "wake_main", lambda args: ran.setdefault("args", args))
    monkeypatch.setattr("sys.argv", ["agent-mailbox", "wake", "status"])
    server.main()
    assert ran["args"] == ["status"]
