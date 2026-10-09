"""A1 并发回归（flow-product · 独立复验 task-8 补的自动化）。

背景：lead 在 38d7624 里新增了 ``state_lock()``（``flock`` 跨进程 + ``threading.Lock`` 进程内），
但**进程内**那条回归用例当时被撤掉了（作者自述前提写错），跨进程也只有对手的口头结论。
本文件把两条都变成可回归的断言：

1. ``test_multiprocess_concurrent_wake_once_wakes_exactly_once`` —— 5 个**真进程**同时跑
   ``python -m agent_mailbox wake --once``，宿主 hook 只能被调用 1 次、水位线里只能有 1 次叫醒。
2. ``test_in_process_threads_serialise_wake_once`` —— 同一进程 5 个线程并发 ``run_once``，
   ``deliver`` 只能被调用 1 次。

**变异实测（我跑的，结论与代码注释里的说法不同，如实记）**：

===========  =========================  ============================
变异           跨进程用例                  进程内用例
===========  =========================  ============================
去掉 flock    **红**（击穿）             绿
去掉 thread   绿                         绿
两层都去      **红**                     **红**
===========  =========================  ============================

⇒ **承重的是 ``flock``**：``state_lock`` 每次调用都 ``open()`` 一个新的 fd，
``flock`` 对**不同的 open file description** 一样互斥（同进程内的多个线程也各自 open ⇒ 一样串行）。
所以作者注释里"``flock`` 只保证跨进程、实测 5 线程仍能并发击穿"**在我的实验里不成立** ——
``threading.Lock`` 目前是冗余的第二层（留着无害，但别把它当独立防线报）。
两条用例一起保留：它们共同保证"两层都没了必红"。

测试数据全部在 ``tmp_path``，不碰真库。
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import threading
import time

from agent_mailbox import workbench_wake as wk
from agent_mailbox.workbench_store import WorkbenchStore

CONCURRENCY = 5


def _ready_store(home: pathlib.Path) -> tuple[WorkbenchStore, dict]:
    """建一个真库：1 个项目 + 1 位在岗员工（与 wake 轮询真实形态一致）。"""
    store = WorkbenchStore(home)
    directory = home / "project"
    directory.mkdir(parents=True, exist_ok=True)
    project = store.create_project("Concurrency", directory)
    employee = store.create_employee("Alice", "codex", project["id"])
    return store, {"project": project, "employee": employee}


def _cli(home: pathlib.Path) -> list[str]:
    return [sys.executable, "-m", "agent_mailbox", "wake", "--once", "--home", str(home)]


def _total_wakes(home: pathlib.Path) -> int:
    state = wk.load_state(home)
    return sum(len(entry.get("wakes") or []) for entry in state["employees"].values())


def test_multiprocess_concurrent_wake_once_wakes_exactly_once(tmp_path):
    """5 个真进程同时 wake --once ⇒ hook 调 1 次、叫醒 1 次（flock 整轮串行）。"""
    home = tmp_path / "store"
    store, refs = _ready_store(home)
    assert subprocess.run(_cli(home), capture_output=True, check=True).returncode == 0  # 冷启动

    store.send_message(
        refs["project"]["id"], "并发新信", "正文", recipient_id=refs["employee"]["id"]
    )

    (home / wk.WAKE_DIRNAME).mkdir(parents=True, exist_ok=True)
    log = tmp_path / "hook.log"
    hook = home / wk.WAKE_DIRNAME / wk.HOOK_FILENAME
    # sleep 拉长临界区 ⇒ 没有整轮锁时 5 个进程必然都进来（"无锁即红"）。
    hook.write_text(f'#!/bin/sh\necho "$1" >> {log}\nsleep 0.5\n', encoding="utf-8")
    hook.chmod(0o755)

    procs = [
        subprocess.Popen(_cli(home), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(CONCURRENCY)
    ]
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=60)
        assert proc.returncode == 0, f"并发轮失败：{stderr or stdout}"

    calls = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
    assert len(calls) == 1, f"宿主 hook 被并发调用 {len(calls)} 次（整轮锁没生效）"
    assert _total_wakes(home) == 1, f"水位线里记录了 {_total_wakes(home)} 次叫醒（应恰好 1 次）"


def test_in_process_threads_serialise_wake_once(tmp_path):
    """同进程 5 线程并发 run_once ⇒ deliver 只调 1 次（threading.Lock 进程内串行）。"""
    home = tmp_path / "home"
    mailbox = {"ids": ["m1"]}
    delivered: list[str] = []

    def provider(_store, _employee_id):
        return list(mailbox["ids"])

    def deliver(employee_id, _decision):
        delivered.append(employee_id)
        time.sleep(0.05)  # 拉长临界区，放大无锁时的交错
        return True

    wk.run_once(
        None,
        home,
        unread_provider=provider,
        deliver=deliver,
        employee_ids=["e1"],
        cold_started=False,
    )  # 冷启动：把 m1 记为已见
    mailbox["ids"].insert(0, "m2")  # 新信到达

    results: list[dict] = []
    threads = [
        threading.Thread(
            target=lambda: results.append(
                wk.run_once(
                    None,
                    home,
                    unread_provider=provider,
                    deliver=deliver,
                    employee_ids=["e1"],
                    cold_started=True,
                )
            )
        )
        for _ in range(CONCURRENCY)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads), "线程未在超时内结束（可能死锁）"
    assert len(delivered) == 1, f"进程内并发击穿：deliver 被调用 {len(delivered)} 次"
    assert sum(bool(result["woke"]) for result in results) == 1
