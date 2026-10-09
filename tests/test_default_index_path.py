"""陈旧门要有源的路径：默认索引路径解析（评审：此前 index_path 从未传 ⇒ stale_days 恒 None ✗）。"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time

from agent_mailbox import workbench_aoci as a
from agent_mailbox import workbench_cli_query as cq


def _repo(base):
    d = base / "repo"
    d.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=d, check=False)
    subprocess.run(
        [
            "git",
            "-C",
            str(d),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "x",
        ],
        check=False,
    )
    return d


def test_returns_none_when_index_absent(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    assert cq.default_index_path("aoci", root) is None
    assert cq.default_index_path("codegraph", root) is None
    assert cq.default_index_path("graft", root) is None


def test_finds_standard_locations(tmp_path):
    root = tmp_path / "repo"
    (root / ".aoci").mkdir(parents=True)
    (root / ".aoci" / "baseline.json").write_text("{}")
    (root / ".codegraph").mkdir()
    (root / ".codegraph" / "codegraph.db").write_text("")
    assert cq.default_index_path("aoci", root).name == "baseline.json"
    assert cq.default_index_path("codegraph", root).name == "codegraph.db"


def test_aoci_wrapper_now_reports_staleness(tmp_path, monkeypatch):
    """集成：接上默认索引路径后，provenance 的陈旧门**不再恒 None** ✓。"""
    repo = _repo(tmp_path)
    index = repo / ".aoci" / "baseline.json"
    index.parent.mkdir()
    index.write_text("{}")
    old = time.time() - 3 * 86400
    os.utime(index, (old, old))  # 索引比 HEAD 旧 3 天 ✓
    fake = tmp_path / "aoci"
    fake.write_text(f"#!{sys.executable}\nprint('AOCI Doctor')\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("AGENT_MAIL_AOCI_BIN", str(fake))

    out = a.aoci_doctor(repo)
    staleness = out["provenance"]["staleness"]
    assert staleness["index_path"] is not None, "陈旧门必须有源路径 ✗"
    assert staleness["index_path"].endswith(".aoci/baseline.json")
    assert isinstance(staleness["stale_days"], (int, float)) and staleness["stale_days"] >= 2.5
