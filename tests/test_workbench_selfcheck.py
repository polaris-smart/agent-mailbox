"""The self-check itself must pass: it is the one-command proof of our own features."""

from __future__ import annotations

from agent_mailbox import workbench_selfcheck as sc


def test_selfcheck_passes_every_step():
    report = sc.run()
    failed = [row for row in report["steps"] if not row["ok"]]
    assert report["ok"] is True, f"自检失败：{failed}"
    assert len(report["steps"]) >= 15
    assert "全部通过" in sc.render(report)


def test_selfcheck_uses_a_throwaway_home(tmp_path, monkeypatch):
    """绝不碰真实 home：跑完后临时目录应已删除。"""
    import pathlib

    report = sc.run()
    home = report["home"]
    assert home and "agent-mailbox-selfcheck-" in home
    assert not pathlib.Path(home).exists()  # 临时目录已清理
