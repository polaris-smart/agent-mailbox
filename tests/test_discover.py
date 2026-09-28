"""v0.7.5 PR A — four-layer discovery engine (discover.py), all fixtures.

Nothing here reads the real machine: HOME, PATH, app dirs, the launchd dir
and every system probe (lsof / ps / --version) are injected. The only real
dependency is the tmp mail root.
"""

from __future__ import annotations

import json
import os
import plistlib
import socket
import threading
import time
from pathlib import Path

import pytest

from agent_mailbox.discover import (
    CATALOG,
    DiscoverContext,
    _channel_health,
    build_report,
    default_context,
    fingerprint_changes,
    fmt_local,
    load_fingerprint,
    mask_url,
    now_local,
    save_fingerprint,
)
from agent_mailbox.discover import test_member as send_test_letter
from agent_mailbox.store import MailStore

# ------------------------------------------------------------- fixtures


def make_ctx(
    tmp_path: Path, *, home: Path | None = None, runner=None, probe_urls: bool = True
) -> DiscoverContext:
    home = home or (tmp_path / "home")
    home.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "mail"
    return DiscoverContext(
        root=root,
        home=home,
        path_env=str(home / "bin"),  # only fixture executables are visible
        apps_dirs=(home / "Applications",),
        launch_agents_dir=home / "Library" / "LaunchAgents",
        runner=runner,
        probe_urls=probe_urls,
    )


def add_cli(
    home: Path,
    name: str,
    body: str = "#!/bin/sh\nexit 0\n",
    win_body: str | None = None,
) -> Path:
    """Fixture CLI, both path flavors. Windows: with the default mode
    (``X_OK`` included) shutil.which does not match extension-less names
    (PATHEXT semantics), only a .bat body spawns shell-free; POSIX: bare name."""
    bin_dir = home / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        p = bin_dir / f"{name}.bat"
        p.write_text(win_body or "@echo off\r\nexit 0\r\n")
    else:
        p = bin_dir / name
        p.write_text(body)
    p.chmod(0o755)
    return p


def add_app(home: Path, name: str, schemes: list[str] | None = None) -> Path:
    bundle = home / "Applications" / f"{name}.app" / "Contents"
    bundle.mkdir(parents=True, exist_ok=True)
    info: dict = {"CFBundleIdentifier": f"com.example.{name}"}
    if schemes is not None:
        info["CFBundleURLTypes"] = [{"CFBundleURLSchemes": schemes}]
    (bundle / "Info.plist").write_bytes(plistlib.dumps(info))
    return bundle.parent


def add_registry(root: Path, *agents: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "registry.json").write_text(
        json.dumps({"agents": {a: {"created_at": "2026-09-27T00:00:00Z"} for a in agents}}),
        encoding="utf-8",
    )


def fake_runner(mapping: dict[tuple, str]):
    def run(cmd: list[str]) -> tuple[int, str]:
        key = tuple(cmd)
        if key in mapping:
            return 0, mapping[key]
        return 1, ""

    return run


# ----------------------------------------------------------------- L1


def test_l1_cli_on_path(tmp_path):
    home = tmp_path / "home"
    add_cli(home, "codex")
    ctx = make_ctx(tmp_path, home=home)
    report = build_report(ctx, save=False)
    codex = next(m for m in report["members"] if m["member"] == "codex")
    assert codex["kind"] == "cli"
    expected_tail = os.path.join("bin", "codex.bat") if os.name == "nt" else "/bin/codex"
    # shutil.which on win32 appends PATHEXT entries verbatim (typically
    # uppercase ".BAT"), so the resolved path's extension casing follows the
    # PATHEXT env, not the on-disk name; compare like the OS does (normcase is
    # a no-op on POSIX, so the POSIX branch stays byte-exact).
    cli_details = [ev["detail"] for ev in codex["evidence"] if ev["type"] == "cli"]
    assert any(
        os.path.normcase(d).endswith(os.path.normcase(expected_tail)) for d in cli_details
    ), cli_details


def test_l1_app_bundle_and_config_dir(tmp_path):
    home = tmp_path / "home"
    add_app(home, "WorkBuddy")
    (home / ".workbuddy").mkdir()
    ctx = make_ctx(tmp_path, home=home)
    report = build_report(ctx, save=False)
    wb = next(m for m in report["members"] if m["member"] == "workbuddy")
    assert wb["kind"] == "app"
    assert any(ev["type"] == "config_dir" for ev in wb["evidence"])


def test_no_signal_no_member_and_empty_fallback(tmp_path):
    ctx = make_ctx(tmp_path)
    report = build_report(ctx, save=False)
    assert report["empty"] is True
    assert set(report["supported"]) == set(CATALOG)


def test_registry_only_member_marked_unknown_not_guessed(tmp_path):
    home = tmp_path / "home"
    ctx = make_ctx(tmp_path, home=home)
    add_registry(ctx.root, "mystery-agent")
    report = build_report(ctx, save=False)
    mystery = next(m for m in report["members"] if m["member"] == "mystery-agent")
    assert mystery["kind"] == "unknown"
    assert mystery["registered"] is True
    assert mystery["evidence"][0]["layer"] == "L2"


# ----------------------------------------------------------------- L2


def test_l2_json_mcp_config_detected(tmp_path):
    home = tmp_path / "home"
    wb_dir = home / ".workbuddy"
    wb_dir.mkdir(parents=True)
    (wb_dir / "mcp.json").write_text(
        json.dumps({"mcpServers": {"agent-mailbox": {"command": "agent-mailbox", "args": []}}})
    )
    ctx = make_ctx(tmp_path, home=home)
    report = build_report(ctx, save=False)
    wb = next(m for m in report["members"] if m["member"] == "workbuddy")
    assert wb["connected"] is True
    hit = [ev for ev in wb["evidence"] if ev["layer"] == "L2" and "已挂" in ev["detail"]]
    assert hit and "mcp.json" in hit[0]["source"]


def test_l2_toml_config_detected(tmp_path):
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text(
        '[mcp_servers.agent-mailbox]\ncommand = "agent-mailbox"\n'
    )
    ctx = make_ctx(tmp_path, home=home)
    add_cli(home, "codex")  # need one L1 signal for the member to appear
    report = build_report(ctx, save=False)
    codex = next(m for m in report["members"] if m["member"] == "codex")
    assert codex["connected"] is True


def test_l2_config_without_mailbox_reports_absent(tmp_path):
    home = tmp_path / "home"
    wb_dir = home / ".workbuddy"
    wb_dir.mkdir(parents=True)
    (wb_dir / "mcp.json").write_text(json.dumps({"mcpServers": {"srv": {"command": "x"}}}))
    ctx = make_ctx(tmp_path, home=home)
    add_app(home, "WorkBuddy")
    report = build_report(ctx, save=False)
    wb = next(m for m in report["members"] if m["member"] == "workbuddy")
    assert wb["connected"] is False
    assert any("未见 mailbox" in ev["detail"] for ev in wb["evidence"])


# ----------------------------------------------------------------- L3


def test_l3_url_scheme_from_info_plist(tmp_path):
    home = tmp_path / "home"
    add_app(home, "ZCode", schemes=["zcode"])
    ctx = make_ctx(tmp_path, home=home)
    report = build_report(ctx, save=False)
    zc = next(m for m in report["members"] if m["member"] == "zcode")
    scheme_channels = [c for c in zc["channels"] if c["type"] == "url_scheme"]
    assert scheme_channels and scheme_channels[0]["value"] == "zcode://"
    assert scheme_channels[0]["source"].endswith("Info.plist")


def test_l3_port_reverse_lookup_by_exe_path_not_process_name(tmp_path):
    """WorkBuddy 场景：进程名只是 Electron，必须靠可执行路径反查。"""
    home = tmp_path / "home"
    app = add_app(home, "WorkBuddy")
    exe = app / "Contents" / "MacOS" / "Electron"  # the real process name
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    # 反查按路径「字符串」匹配 bundle 段：统一正斜杠形态，Windows 宿主的
    # 反斜杠路径串（str(exe)）才不会在归属匹配处断链（CI 0.7.5 win 红）。
    exe_label = exe.as_posix()
    runner = fake_runner(
        {
            ("lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "+c0"): (
                "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
                "Electron 4242 me 21u IPv4 0x0 0t0 TCP 127.0.0.1:18488 (LISTEN)\n"
            ),
            ("ps", "-p", "4242", "-o", "command="): f"{exe_label} --worker\n",
        }
    )
    ctx = make_ctx(tmp_path, home=home, runner=runner, probe_urls=False)
    report = build_report(ctx, save=False)
    wb = next(m for m in report["members"] if m["member"] == "workbuddy")
    port_channels = [c for c in wb["channels"] if c["type"] == "port"]
    assert port_channels and port_channels[0]["value"] == "18488"
    assert "WorkBuddy.app" in port_channels[0]["source"]  # 来源可核对


def test_l3_unidentified_listener_listed_not_claimed(tmp_path):
    home = tmp_path / "home"
    exe = home / "somewhere" / "mysteryd"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    runner = fake_runner(
        {
            ("lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "+c0"): (
                "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
                "mysteryd 7 7 7u IPv4 0x0 0t0 TCP 127.0.0.1:9999 (LISTEN)\n"
            ),
            ("ps", "-p", "7", "-o", "command="): f"{exe}\n",
        }
    )
    ctx = make_ctx(tmp_path, home=home, runner=runner, probe_urls=False)
    report = build_report(ctx, save=False)
    assert report["other_listeners"] == [{"exe": str(exe), "port": "9999"}]
    assert all(m["member"] != "mysteryd" for m in report["members"])  # 不猜


# --------------------------------------------------- health + 断链场景


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_broken_wake_webhook_shows_x_with_human_reason(tmp_path):
    """验收③ 断链场景：唤醒目标改错 → ❌ + 人话原因，不许静默。"""
    home = tmp_path / "home"
    ctx = make_ctx(tmp_path, home=home)
    ctx.root.mkdir(parents=True)
    dead_port = free_port()
    (ctx.root / "wake.json").write_text(
        json.dumps(
            {
                "agent_id": "codex",
                "adapter": "hermes",
                "webhook": {"url": f"http://127.0.0.1:{dead_port}/hook", "secret": "s"},
            }
        )
    )
    add_cli(home, "codex")
    report = build_report(ctx, save=False)
    codex = next(m for m in report["members"] if m["member"] == "codex")
    hook = [c for c in codex["channels"] if c["type"] == "webhook"]
    assert hook and hook[0]["status"] == "broken"
    assert "连不上" in hook[0]["reason"]
    assert hook[0]["next_step"]


def test_webhook_url_masked_no_secret_echo(tmp_path):
    assert mask_url("https://user:token123@gw.example.com/hook?secret=abcdef") == (
        "https://gw.example.com/hook"
    )


def test_missing_command_channel_broken(tmp_path):
    """命令通道指向已删除的可执行文件 → ❌（防御分支直测）。"""
    ch = {"type": "command", "value": str(tmp_path / "gone" / "codex"), "source": "PATH"}
    out = _channel_health(make_ctx(tmp_path), ch)
    assert out["status"] == "broken"
    assert "已不存在" in out["reason"] and out["next_step"]


# --------------------------------------------------------- fingerprint


def test_fingerprint_saved_and_staleness_flagged(tmp_path):
    home = tmp_path / "home"
    add_cli(home, "codex")
    ctx = make_ctx(tmp_path, home=home)
    build_report(ctx, save=True)
    assert load_fingerprint(ctx.root).get("codex")

    # 同环境重扫：不告警
    second = build_report(ctx, save=True)
    codex = next(m for m in second["members"] if m["member"] == "codex")
    assert codex["stale"] is False

    # 指纹变化（CLI 换路径）→ stale + 人话提示
    old_fp = load_fingerprint(ctx.root)
    old_fp["codex"]["cli"] = ["/old/place/codex"]
    save_fingerprint(ctx.root, old_fp)
    third = build_report(ctx, save=True)
    codex = next(m for m in third["members"] if m["member"] == "codex")
    assert codex["stale"] is True and "重跑" in codex["stale_note"]


def test_fingerprint_changes_diff():
    assert fingerprint_changes(None, {"cli": ["x"]}) == ["首次记录"]
    assert fingerprint_changes({"cli": ["a"]}, {"cli": ["a"]}) == []
    assert fingerprint_changes({"cli": ["a"]}, {"cli": ["b"], "scheme": ["x://"]}) == [
        "cli",
        "scheme",
    ]


def test_deep_mode_captures_cli_version(tmp_path):
    home = tmp_path / "home"
    add_cli(
        home,
        "codex",
        body="#!/bin/sh\necho 'codex 1.2.3'\n",
        win_body="@echo off\r\necho codex 1.2.3\r\n",
    )
    ctx = make_ctx(tmp_path, home=home)
    ctx.probe_versions = True
    report = build_report(ctx, save=False)
    codex = next(m for m in report["members"] if m["member"] == "codex")
    assert codex["fingerprint"]["version"] == "codex 1.2.3"


# ------------------------------------------------------------- time ⑪


def test_time_helpers_local_timezone():
    stamp = now_local()
    assert stamp[-5:].startswith("+")  # carries an explicit UTC offset
    local = fmt_local("2026-09-27T00:30:00Z")
    assert local.endswith(time.strftime("%z"))  # rendered in local tz
    assert fmt_local("not-a-time") == "not-a-time"


# ----------------------------------------------------- L4: test letter


def test_member_receipt_ok(tmp_path):
    ctx = make_ctx(tmp_path)
    st = MailStore(ctx.root)
    st.register("codex")
    add_cli(tmp_path / "home", "codex")

    def fake_receipt():
        time.sleep(1.0)  # 系统忙时 0.3s 会飘出轮询窗口（09-28 flaky 实录）
        st.check("codex")

    threading.Thread(target=fake_receipt, daemon=True).start()
    result = send_test_letter("codex", ctx.root, timeout=5.0)
    assert result["ok"] is True and result["stage"] in ("acked", "done")
    assert result["letter_id"]


def test_member_timeout_gives_human_reason_and_next_step(tmp_path):
    ctx = make_ctx(tmp_path)
    MailStore(ctx.root).register("codex")  # nobody ever checks
    result = send_test_letter("codex", ctx.root, timeout=0.6, poll=0.1)
    assert result["ok"] is False and result["stage"] == "timeout"
    assert result["reason"] and result["next_step"]


def test_member_unknown_gives_support_hint(tmp_path):
    ctx = make_ctx(tmp_path)
    result = send_test_letter("nobody-here", ctx.root, timeout=0.6, poll=0.1)
    assert result["ok"] is False
    assert "不认识" in result["reason"]
    assert "discover" in result["next_step"]


def test_member_invalid_id_human_error(tmp_path):
    ctx = make_ctx(tmp_path)
    result = send_test_letter("../evil", ctx.root, timeout=0.6, poll=0.1)
    assert result["ok"] is False and result["stage"] == "send_failed"
    assert "不合法" in result["reason"]


# ------------------------------------------------- real-context builder


def test_default_context_respects_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MAIL_HOME", str(tmp_path / "envroot"))
    ctx = default_context()
    assert ctx.root == tmp_path / "envroot"
    assert ctx.home == Path.home()
    assert ctx.launch_agents_dir is not None
    assert os.environ["AGENT_MAIL_HOME"] == str(tmp_path / "envroot")


@pytest.mark.parametrize("member", sorted(CATALOG))
def test_catalog_specs_are_wellformed(member):
    spec = CATALOG[member]
    assert spec["clis"] or spec["apps"], member  # every member has a probe
    for key in ("clis", "config_dirs", "apps", "mcp_configs", "schemes"):
        assert isinstance(spec[key], tuple)
