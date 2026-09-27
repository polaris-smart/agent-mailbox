"""v0.7.5 PR A — connect (备份+点位+写入) and uninstall restore skeleton.

All fixtures live under tmp_path; no real user config is ever touched.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from agent_mailbox.connect import (
    ENTRY_KEY,
    connect,
    load_points,
    plan_connect,
    revert_points,
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def home_with_json_config(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    cfg_dir = home / ".workbuddy"
    cfg_dir.mkdir(parents=True)
    cfg = cfg_dir / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"other-srv": {"command": "x"}}}, indent=2))
    return home, cfg


# ------------------------------------------------------------- planning


def test_plan_lists_target_and_entry_without_writing(tmp_path):
    home, cfg = home_with_json_config(tmp_path)
    plan = plan_connect("workbuddy", tmp_path / "root", home=home)
    assert plan["known"] is True
    target = next(t for t in plan["targets"] if t["path"] == str(cfg))
    assert target["status"] == "writable-json"
    assert plan["entry"]["command"]
    assert cfg.read_text() == json.dumps(  # untouched by the plan
        {"mcpServers": {"other-srv": {"command": "x"}}}, indent=2
    )


def test_plan_unknown_member_rejected(tmp_path):
    plan = plan_connect("nope", tmp_path / "root", home=tmp_path)
    assert plan["known"] is False and plan["targets"] == []


def test_plan_flags_unsupported_format(tmp_path):
    home = tmp_path / "home"
    codex = home / ".codex"
    codex.mkdir(parents=True)
    (codex / "config.toml").write_text("[a]\nb=1\n")
    plan = plan_connect("codex", tmp_path / "root", home=home)
    target = next(t for t in plan["targets"] if t["format"] == "toml")
    assert target["status"] == "unsupported-format"


# -------------------------------------------------- connect with --yes


def test_connect_yes_backs_up_writes_and_ledgers(tmp_path):
    home, cfg = home_with_json_config(tmp_path)
    root = tmp_path / "root"
    result = connect("workbuddy", root, yes=True, home=home)
    assert len(result["written"]) == 1
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["other-srv"] == {"command": "x"}  # existing intact
    assert data["mcpServers"][ENTRY_KEY]["command"]
    points = load_points(root)
    assert len(points) == 1
    point = points[0]
    backup = Path(point["backup"])
    assert point["original_sha256"] == hashlib.sha256(backup.read_bytes()).hexdigest()
    assert point["new_sha256"] == sha(cfg)  # 台账覆盖写入后状态
    assert backup.exists()
    assert json.loads(backup.read_text()) == {"mcpServers": {"other-srv": {"command": "x"}}}


def test_connect_without_yes_writes_nothing(tmp_path):
    home, cfg = home_with_json_config(tmp_path)
    before = cfg.read_text()
    result = connect("workbuddy", tmp_path / "root", yes=False, home=home)
    assert result["written"] == []
    assert cfg.read_text() == before
    assert load_points(tmp_path / "root") == []


def test_connect_skips_already_connected(tmp_path):
    home, _cfg = home_with_json_config(tmp_path)
    root = tmp_path / "root"
    connect("workbuddy", root, yes=True, home=home)
    result = connect("workbuddy", root, yes=True, home=home)
    assert result["written"] == []
    assert any("已挂" in s["why"] for s in result["skipped"])


def test_connect_never_writes_unsupported_format(tmp_path):
    home = tmp_path / "home"
    codex = home / ".codex"
    codex.mkdir(parents=True)
    toml = codex / "config.toml"
    toml.write_text("[mcp_servers.x]\ncommand='x'\n")
    result = connect("codex", tmp_path / "root", yes=True, home=home)
    assert result["written"] == []
    assert toml.read_text() == "[mcp_servers.x]\ncommand='x'\n"
    assert any(s.get("manual") for s in result["skipped"])  # 给手动添加口径


def test_connect_missing_target_reports_not_writes(tmp_path):
    home = tmp_path / "home"  # .claude dir absent entirely
    result = connect("claude", tmp_path / "root", yes=True, home=home)
    assert result["written"] == []
    assert result["errors"]  # 指出为什么不能接，不静默


# -------------------------------------------------- uninstall restore


def test_revert_restores_byte_identical_diff_zero(tmp_path):
    home, cfg = home_with_json_config(tmp_path)
    root = tmp_path / "root"
    original_sha = sha(cfg)
    connect("workbuddy", root, yes=True, home=home)
    assert sha(cfg) != original_sha
    out = revert_points(root)
    assert out["residual"] == []
    assert len(out["reverted"]) == 1
    assert sha(cfg) == original_sha  # diff == 0（验收⑤口径）
    assert load_points(root)[0]["reverted"] is True


def test_revert_reports_residual_when_third_party_edited(tmp_path):
    home, cfg = home_with_json_config(tmp_path)
    root = tmp_path / "root"
    connect("workbuddy", root, yes=True, home=home)
    data = json.loads(cfg.read_text())
    data["human_edit"] = True  # 用户后来又改了
    cfg.write_text(json.dumps(data))
    out = revert_points(root)
    assert out["reverted"] == []
    assert len(out["residual"]) == 1
    assert "改过" in out["residual"][0]["reason"]  # 指出残留，不静默
    assert json.loads(cfg.read_text())["human_edit"] is True  # 不覆盖别人改动


def test_revert_created_file_is_deleted(tmp_path):
    home = tmp_path / "home"
    cfg_dir = home / ".config" / "opencode"  # dir exists, config missing
    cfg_dir.mkdir(parents=True)
    cfg = cfg_dir / "opencode.json"
    root = tmp_path / "root"
    result = connect("opencode", root, yes=True, home=home)
    assert len(result["written"]) == 1 and cfg.exists()
    out = revert_points(root)
    assert out["residual"] == []
    assert not cfg.exists()  # created 点位 → 还原 = 删除
    assert not any(p.name == "opencode.json" for p in cfg_dir.iterdir())
