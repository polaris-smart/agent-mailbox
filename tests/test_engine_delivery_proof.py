"""交付证明自动化（T12）：引擎执行成功后必须留下可核验的证明。

真机测试暴露：任务跑完账本显示「证明=无」——引擎只 `capture_delivery`，不记
`delivery_proof` 事件 ⇒ 验收只能靠肉眼，PRD §5「不看聊天记录，只看交接单+证据」不成立。

识别口径（不猜工具名，用可核验信号）：
* 运行期流式事件里 tool_call 的 rawInput 出现过的**绝对路径**；
* 该文件在**本次运行期间**被写过（mtime ≥ run 起始 - 1s）；
* 排除 agent 自己的内部目录；
* 没有产出物就不记（空证明按既有规则 = incomplete，反而更差）。
"""

from __future__ import annotations

import os
import pathlib
import time

import pytest

from agent_mailbox.workbench_engine import WorkbenchEngine
from agent_mailbox.workbench_mail_tasks import create_mail_task
from agent_mailbox.workbench_proof import latest_proof
from agent_mailbox.workbench_store import WorkbenchStore


@pytest.fixture
def scene(tmp_path):
    store = WorkbenchStore(tmp_path / "data")
    directory = tmp_path / "project"
    directory.mkdir()
    project = store.create_project("Demo", directory)
    alice = store.create_employee("Alice", "codex", project["id"])
    task = create_mail_task(store, project["id"], "活", "说明", alice["id"])
    return store, project, alice, task


def _engine(store) -> WorkbenchEngine:
    return WorkbenchEngine(store, bridge_command=None, task_timeout=5)


def test_proof_is_recorded_for_files_written_during_the_run(scene, tmp_path):
    store, _project, _alice, task = scene
    deliverable = tmp_path / "deliverable.md"
    run_started = time.time()
    deliverable.write_text("交付内容", encoding="utf-8")

    _engine(store)._record_delivery_proof(task["id"], {str(deliverable)}, run_started)

    proof = latest_proof(store, task["id"])
    assert proof is not None, "执行成功后应留下交付证明"
    assert any(str(deliverable) in str(item.get("path") or item) for item in proof["artifacts"])


def test_old_files_are_not_mistaken_for_deliverables(scene, tmp_path):
    """运行**之前**就存在的文件不算产出物 ⇒ 无产出物就不记证明（不记空证明）。"""
    store, _project, _alice, task = scene
    stale = tmp_path / "old.txt"
    stale.write_text("早就有了", encoding="utf-8")
    os.utime(stale, (time.time() - 3600, time.time() - 3600))
    run_started = time.time()

    _engine(store)._record_delivery_proof(task["id"], {str(stale)}, run_started)
    assert latest_proof(store, task["id"]) is None


def test_agent_internal_files_are_excluded(scene, tmp_path, monkeypatch):
    """agent 自己的 plan/config 目录不是交付物（**hermetic**：造真文件，别走 OSError 分支）。

    第 10 轮 [中] ①-1：原版用 `~/.claude/plans/demo-plan.md`，本机不存在 ⇒ 走 OSError
    分支通过，删掉排除逻辑也照样绿（假测试）。
    """
    store, _project, _alice, task = scene
    fake_home = tmp_path / "home"
    (fake_home / ".claude" / "plans").mkdir(parents=True)
    sentinel = fake_home / ".claude" / "plans" / "demo-plan.md"
    sentinel.write_text("内部计划", encoding="utf-8")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: fake_home))
    run_started = time.time()

    _engine(store)._record_delivery_proof(task["id"], {str(sentinel)}, run_started)
    assert latest_proof(store, task["id"]) is None


def test_dot_dot_traversal_cannot_smuggle_internal_files(scene, tmp_path, monkeypatch):
    """`xx/../.claude/...` 归一化后仍是内部文件（第 10 轮 [中] ①-2）。"""
    store, _project, _alice, task = scene
    fake_home = tmp_path / "home"
    (fake_home / ".claude" / "plans").mkdir(parents=True)
    (fake_home / ".claude" / "plans" / "x.md").write_text("内部", encoding="utf-8")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: fake_home))
    traversal = str(fake_home / "other" / ".." / ".claude" / "plans" / "x.md")

    _engine(store)._record_delivery_proof(task["id"], {traversal}, time.time())
    assert latest_proof(store, task["id"]) is None


def test_missing_files_are_skipped(scene, tmp_path):
    store, _project, _alice, task = scene
    _engine(store)._record_delivery_proof(
        task["id"], {str(tmp_path / "never-existed.md")}, time.time()
    )
    assert latest_proof(store, task["id"]) is None


def test_existing_proof_is_not_replaced(scene, tmp_path):
    store, _project, _alice, task = scene
    first = tmp_path / "one.md"
    run_started = time.time()
    first.write_text("第一份", encoding="utf-8")
    engine = _engine(store)
    engine._record_delivery_proof(task["id"], {str(first)}, run_started)
    recorded = latest_proof(store, task["id"])
    assert recorded is not None

    second = tmp_path / "two.md"
    second.write_text("第二份", encoding="utf-8")
    engine._record_delivery_proof(task["id"], {str(second)}, run_started)

    again = latest_proof(store, task["id"])
    assert again is not None and ago(again) == ago(recorded), "已有证明不应被覆盖"


def ago(proof: dict) -> str:
    return str(proof.get("proof_sha256"))
