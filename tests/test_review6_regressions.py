"""Regressions for the **sixth** adversarial review (2026-10-05).

That reviewer crashed mid-report, but wrote findings to ``/tmp/amr6/report.md``
first — the crash-safe reporting rule paid off. Its findings:

* high: ``bridge project``'s **dry run** still took a write lock (private
  ``_resolve_assignee`` used a write transaction for a SELECT);
* high: ``read()`` was a TOCTOU — hashing and then re-opening the path could serve
  **unverified** bytes and could hang forever if the path became a FIFO;
* medium: budget did not hold for invalid UTF-8 (22 → 62 bytes), continuation could
  not reconstruct text at multibyte boundaries, and the property test had three
  mutant-proven bypasses (all fixed in ``tests/test_readonly_property.py``).
"""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import subprocess
import sys

import pytest

from agent_mailbox import workbench_artifact, workbench_bridge, workbench_proof
from agent_mailbox.workbench_mail_tasks import create_mail_task
from agent_mailbox.workbench_store import WorkbenchStore

REPO = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def scene(tmp_path):
    store = WorkbenchStore(tmp_path / "data")
    directory = tmp_path / "project"
    directory.mkdir()
    project = store.create_project("Demo", directory)
    alice = store.create_employee("Alice", "codex", project["id"])
    return store, project, alice, tmp_path


def _journal(store) -> str:
    raw = sqlite3.connect(store.db_path, isolation_level=None)
    try:
        return raw.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        raw.close()


def _proof_for_text(store, project, alice, tmp_path, text: str, name: str = "out.txt") -> str:
    artifact = tmp_path / name
    artifact.write_text(text, encoding="utf-8")
    task = create_mail_task(store, project["id"], "活", "说明", alice["id"])
    proof = workbench_proof.build_proof(store, task["id"], artifacts=[str(artifact)])
    return proof["artifacts"][0]["ref"], str(artifact)


def test_rv6_1_bridge_dry_run_does_not_write(scene):
    """RV6-高：`bridge project`（干跑）曾因私有助手用写事务而拿写锁、把库翻成 WAL。"""
    store, project, _alice, tmp_path = scene
    mail = tmp_path / "mail" / "inbox" / "Codex"
    mail.mkdir(parents=True)
    (mail / "L-1.json").write_text(
        json.dumps(
            {"id": "L-1", "subject": "【任务书】x", "from": "HS", "to": "Codex", "body": "b"}
        ),
        encoding="utf-8",
    )
    raw = sqlite3.connect(store.db_path, isolation_level=None)
    raw.execute("PRAGMA journal_mode=delete")
    raw.close()

    outline = workbench_bridge.project(store, tmp_path / "mail", project["id"], apply=False)
    assert outline["dry_run"] is True
    assert _journal(store) == "delete", "干跑路径把库改成了 WAL（拿了写锁）"


def test_rv6_2_continuation_reconstructs_the_text(scene):
    """RV6-中：按 next_offset 续读拼回的文本必须等于原文（含多字节字符与小预算）。"""
    store, project, alice, tmp_path = scene
    original = "中文测试：逐字节切片读取中文测试"
    ref, _path = _proof_for_text(store, project, alice, tmp_path, original)

    for budget in (2, 3, 5, 7, 50):
        joined, offset, guard = "", 0, 0
        while True:
            result = workbench_artifact.read(store, ref, budget=budget, offset=offset)
            joined += result["content"] or ""
            if not result["truncated"]:
                break
            assert result["next_offset"] > offset, f"budget={budget} 未前进（会死循环）"
            offset = result["next_offset"]
            guard += 1
            assert guard < 200, "续读未收敛"
        assert joined == original, f"budget={budget}: 拼回 {joined!r} != 原文"


def test_rv6_3_budget_holds_for_invalid_utf8(scene):
    """RV6-中：非法字节曾让 22 字节预算返回 62 字节（替换符膨胀）且无任何信号。"""
    store, project, alice, tmp_path = scene
    artifact = tmp_path / "bad.bin"
    artifact.write_bytes(b"A" + b"\xff" * 20 + b"B")
    task = create_mail_task(store, project["id"], "坏字节", "说明", alice["id"])
    ref = workbench_proof.build_proof(store, task["id"], artifacts=[str(artifact)])["artifacts"][0][
        "ref"
    ]

    result = workbench_artifact.read(store, ref, budget=22)
    assert result["lossy"] is True, "非法字节必须被标记"
    assert result["returned_bytes"] <= 22 + 3, result["returned_bytes"]


def test_rv6_4_zero_byte_artifact_is_distinguishable(scene):
    """RV6-低：0 字节产出物曾报 total_bytes=None，与"没读到"不可区分。"""
    store, project, alice, tmp_path = scene
    empty = tmp_path / "empty.md"
    empty.write_text("", encoding="utf-8")
    task = create_mail_task(store, project["id"], "空交付", "说明", alice["id"])
    ref = workbench_proof.build_proof(store, task["id"], artifacts=[str(empty)])["artifacts"][0][
        "ref"
    ]

    result = workbench_artifact.read(store, ref, budget=100)
    assert result["refused"] is None
    assert result["total_bytes"] == 0, "0 字节应如实报 0（None 表示没读到）"
    assert result["content"] == ""


def test_rv6_5_json_keys_reports_that_offset_is_ignored(scene):
    """RV6-低：结构投影回显 offset 却不使用它，调用方会误读为"从该字节开始的结构"。"""
    store, project, alice, tmp_path = scene
    artifact = tmp_path / "data.json"
    artifact.write_text('{"a": {"b": {"c": 1}}}', encoding="utf-8")
    task = create_mail_task(store, project["id"], "结构", "说明", alice["id"])
    ref = workbench_proof.build_proof(store, task["id"], artifacts=[str(artifact)])["artifacts"][0][
        "ref"
    ]

    result = workbench_artifact.read(store, ref, budget=500, offset=5, projection="json_keys")
    assert result["offset_ignored"] is True
    # 反例：offset=0 时必须为 False —— 否则"恒真"也会全绿（第七轮变异 M20）
    zero = workbench_artifact.read(store, ref, budget=500, offset=0, projection="json_keys")
    assert zero["offset_ignored"] is False


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX filesystem FIFO is unavailable")
def test_rv6_6_fifo_candidate_does_not_hang(scene, tmp_path):
    """RV6-高：路径在取窗口前变成 FIFO 会永久挂起（O_NONBLOCK + fstat 复核后不会）。"""
    store, project, alice, _root = scene
    artifact = tmp_path / "swap.txt"
    artifact.write_text("原始内容", encoding="utf-8")
    task = create_mail_task(store, project["id"], "FIFO", "说明", alice["id"])
    ref = workbench_proof.build_proof(store, task["id"], artifacts=[str(artifact)])["artifacts"][0][
        "ref"
    ]

    artifact.unlink()
    os.mkfifo(artifact)
    script = (
        f"import sys; sys.path.insert(0, {str(REPO / 'src')!r});"
        "from agent_mailbox.workbench_store import WorkbenchStore;"
        "from agent_mailbox import workbench_artifact as wa;"
        f"s = WorkbenchStore({str(store.root)!r});"
        f"r = wa.read(s, {ref!r}, budget=50);"
        "print(r['refused'])"
    )
    finished = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=15, check=False
    )
    assert finished.returncode == 0, finished.stderr
    assert "not_a_regular_file" in finished.stdout, finished.stdout


def test_rv6_7_in_place_rewrite_is_refused(scene, tmp_path, monkeypatch):
    """RV6-高（TOCTOU 的另一半）：同 inode 就地改写后不得返回未校验字节。"""
    store, project, alice, tmp_path = scene
    ref, path = _proof_for_text(store, project, alice, tmp_path, "已批准的原始交付内容")

    original_read_window = workbench_artifact._read_window
    swapped = {"done": False}

    def swap_then_read(fd, offset, length):
        if not swapped["done"]:
            swapped["done"] = True
            pathlib.Path(path).write_text("未校验的替换内容" * 3, encoding="utf-8")
        return original_read_window(fd, offset, length)

    monkeypatch.setattr(workbench_artifact, "_read_window", swap_then_read)
    result = workbench_artifact.read(store, ref, budget=200)
    # 内容被换长 ⇒ 报 size_changed_while_reading；等长改写 ⇒ changed_while_reading。
    # 两者都必须拒绝返回内容（第七轮要求把"追加"与"就地改写"区分开）。
    assert result["refused"] in {"changed_while_reading", "size_changed_while_reading"}, result
    assert result["content"] is None
