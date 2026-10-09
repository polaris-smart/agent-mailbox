"""唤醒通道对抗测试（eng-verify · task-5 · 2026-10-08）。

**约定**：以下用例断言"修好之后应有的安全行为"，现在用 ``xfail(strict=True)`` 标记 ——
现在整套测试仍是绿的（不破坏四门），**修好后会 XPASS 而让 pytest 变红**，提醒修复者
删掉 ``xfail`` 标记并把断言保留下来。理由：tests/ 与 task-4/task-6 同写域，不适合留一堆红测试。

每个用例的 ``reason`` 里带攻击复现要点；完整证据见
``docs/reviews/2026-10-08-wake-adversarial-eng-verify.md``。
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

import pytest

from agent_mailbox import workbench_cli_query as cq
from agent_mailbox import workbench_wake as wk
from agent_mailbox.workbench_store import WorkbenchStore


def _git_repo(path: pathlib.Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t.t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t.t",
    }
    subprocess.run(["git", "init", "-q", "."], cwd=path, check=True, env=env)
    (path / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "c"], cwd=path, check=True, env=env)
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


# ── ② 路径绕过 ────────────────────────────────────────────────────────────


def test_git_dir_file_pointing_outside_must_be_rejected(tmp_path):
    """`.git` 是文件且 `gitdir:` 指向**别的仓**时，不得当成本项目的工作树。

    实测：`resolve_project_root(fake)` 通过，`git_facts(fake)` 返回别仓 HEAD。
    """
    real = tmp_path / "realrepo"
    real_rev = _git_repo(real)
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / ".git").write_text(f"gitdir: {real / '.git'}\n", encoding="utf-8")

    if cq.git_facts(fake)[0] == real_rev:  # 前提：攻击确实生效
        with pytest.raises(cq.ProjectPathError):
            cq.resolve_project_root(fake)
    else:  # pragma: no cover - 环境无 git 时视为通过
        pytest.skip("环境无法复现 gitdir 重定向")


def test_git_symlink_pointing_outside_must_be_rejected(tmp_path):
    real = tmp_path / "realrepo"
    real_rev = _git_repo(real)
    fake = tmp_path / "fake"
    fake.mkdir()
    os.symlink(real / ".git", fake / ".git")

    if cq.git_facts(fake)[0] == real_rev:
        with pytest.raises(cq.ProjectPathError):
            cq.resolve_project_root(fake)
    else:  # pragma: no cover
        pytest.skip("环境无法复现符号链接重定向")


def test_empty_git_directory_is_not_a_worktree(tmp_path):
    fake = tmp_path / "fake"
    (fake / ".git").mkdir(parents=True)
    with pytest.raises(cq.ProjectPathError):
        cq.resolve_project_root(fake)


def test_git_facts_must_ignore_inherited_git_dir(tmp_path, monkeypatch):
    real = tmp_path / "realrepo"
    real_rev = _git_repo(real)
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / ".git").mkdir()  # 空 .git：不是有效仓

    monkeypatch.setenv("GIT_DIR", str(real / ".git"))
    assert cq.git_facts(plain) == (None, None), (
        f"GIT_DIR 注入生效：拿到了 {real_rev}；git 调用必须传清洗过的 env"
    )


# ── ④ wake-state.json ────────────────────────────────────────────────────


def _store(tmp_path) -> WorkbenchStore:
    store = WorkbenchStore(tmp_path / "data")
    directory = tmp_path / "project"
    directory.mkdir()
    project = store.create_project("Demo", directory)
    store.create_employee("Alice", "codex", project["id"])
    return store


def test_corrupt_state_must_not_silently_cold_start(tmp_path):
    store = _store(tmp_path)
    home = tmp_path / "data"
    (home / wk.WAKE_DIRNAME).mkdir(parents=True, exist_ok=True)
    state_file = home / wk.WAKE_DIRNAME / wk.STATE_FILENAME
    state_file.write_text('{"version":1,"employees"', encoding="utf-8")  # 截断

    result = wk.run_once(
        store,
        home,
        unread_provider=lambda s, e: ["m1", "m2"],
        deliver=lambda e, d: True,
        employee_ids=store.snapshot() and ["e1"],
        cold_started=False,
    )
    assert result["cold"] is False, "state 损坏不得被当成冷启动（会静默吞掉全部未读）"


def test_forged_state_disable_must_be_detected(tmp_path):
    home = tmp_path / "data"
    (home / wk.WAKE_DIRNAME).mkdir(parents=True, exist_ok=True)
    forged = {
        "version": 1,
        "enabled": False,  # 攻击者/损坏写下的静音开关
        "cold_done": True,
        "employees": {"e1": {"seen": [], "wakes": []}},
    }
    (home / wk.WAKE_DIRNAME / wk.STATE_FILENAME).write_text(json.dumps(forged), encoding="utf-8")
    state = wk.load_state(home)
    assert wk.decide(state, "e1", ["m1"], now=1000.0) is not None, (
        "无签名/无单调序号的 state 篡改必须被检出，而不是静默静音"
    )


# ── ③ 权限 / 越界 ────────────────────────────────────────────────────────


def test_hook_must_be_pinned_to_store_home(tmp_path):
    store_home = tmp_path / "store"
    evil = tmp_path / "evil"
    (evil / wk.WAKE_DIRNAME).mkdir(parents=True)
    (evil / wk.WAKE_DIRNAME / wk.HOOK_FILENAME).write_text("#!/bin/bash\ntrue\n", encoding="utf-8")
    (evil / wk.WAKE_DIRNAME / wk.HOOK_FILENAME).chmod(0o755)

    calls: list[list[str]] = []

    class _Done:
        returncode = 0

    def _runner(cmd, **kwargs):
        calls.append(list(cmd))
        return _Done()

    deliver = wk.hook_deliver(wk.state_dir(evil), runner=_runner)
    deliver("e1", {"reason": "new_mail", "unread": 1, "fresh": ["m1"]})

    assert calls == [], "hook 不得从 --state-dir 指向的目录加载（应钉死 store home）"
    assert not (store_home / wk.WAKE_DIRNAME / wk.HOOK_FILENAME).exists()


def test_outbox_marker_is_unique_and_private(tmp_path):
    base = tmp_path / "home"
    (base / wk.WAKE_DIRNAME).mkdir(parents=True)
    deliver = wk.hook_deliver(base)  # C1 之后：hook_deliver 收 store home ✓（原来是 wake dir ✗）
    decision = {"reason": "new_mail", "unread": 2, "fresh": ["m1", "m2"]}

    # **契约已变（2026-10-08）** ✗：投递前对每封信做 `O_CREAT|O_EXCL` **原子认领** ✓
    # ⇒ 同一封信重复投递**不再重复叫** ✓（幂等 ✓）⇒ 验"标记唯一且 0600"要换**不同的信** ✓
    assert deliver("e1", decision) is False
    assert deliver("e1", {"reason": "new_mail", "unread": 2, "fresh": ["m3", "m4"]}) is False
    outbox = base / wk.WAKE_DIRNAME / wk.OUTBOX_DIRNAME
    markers = sorted(outbox.iterdir())
    assert len(markers) == 2, f"同秒两次投递只留了 {len(markers)} 个标记（会吞审计）"
    for marker in markers:
        assert (marker.stat().st_mode & 0o777) == 0o600, "标记含 employee_id/fresh，必须 0600"


# ── ① 风暴 ──────────────────────────────────────────────────────────────


def test_unread_provider_must_not_silently_drop_backlog(tmp_path):
    """积压超过窗口时，未读信 id 不得被**静默丢弃** —— 否则最老那批永不进水线，日后批量补叫。

    实测：253 封 ⇒ provider 只给 200，水位线 seen=203，53 封永久在水线外。
    """
    store = _store(tmp_path)
    project = store.snapshot()["projects"][0]
    employee = store.snapshot()["employees"][0]
    task = store.create_task(project["id"], "活", "说明", employee["id"])
    store.claim_task(store.local_node()["id"])
    store.set_status(task["id"], "running")
    total = 230
    for i in range(total):
        store.send_message(
            project["id"],
            f"信 {i}",
            "正文",
            recipient_id=employee["id"],
            sender_id=employee["id"],
            source_task_id=task["id"],
        )

    ids = wk.unread_ids_for_employee(store, employee["id"])
    assert len(ids) == total, (
        f"积压 {total} 封但 provider 只给了 {len(ids)} 封（LIMIT 静默截断 ⇒ 水位线漏掉最早的一批）"
    )


# ── 第五轮补测（task-8 对抗面）：A1 并发 · 锁健壮性 · 卸载 · plist 绝对路径 · worktree ──
#
# 命名（供 lead 引用）：
#   1 test_multiprocess_concurrent_wake_once_wakes_exactly_once   [新增·保护 A1 跨进程]
#   2 test_in_process_threads_serialise_wake_once                [新增·保护 A1 进程内]
#   3 test_lock_file_deletion_is_recoverable                     [新增·保护锁重建]
#   4 test_uninstall_is_idempotent_and_leaves_no_residue         [新增·保护卸载零残留]
#   5 test_uninstall_boots_out_before_deleting_the_file          [新增·保护"先 bootout 再删"]
#   6 test_installed_plist_executable_must_be_absolute_and_exist [xfail·③ 裸名回归]
#   7 test_lock_path_replaced_by_directory_must_not_brick_wake   [xfail·IsADirectoryError 堵塞]
#   8 test_legit_git_worktree_must_be_accepted                   [xfail·合法 worktree 被误拒]


def _ready_store(home, count: int = 40):
    """临时库：员工 + 已接单任务 + count 封信（唤醒通道的可用输入）。"""
    store = WorkbenchStore(home)
    directory = home.parent / "project"
    directory.mkdir(exist_ok=True)
    project = store.create_project("Demo", directory)
    employee = store.create_employee("Alice", "codex", project["id"])
    task = store.create_task(project["id"], "活", "说明", employee["id"])
    store.claim_task(store.local_node()["id"])
    store.set_status(task["id"], "running")
    for index in range(count):
        store.send_message(
            project["id"],
            f"信 {index}",
            "正文",
            recipient_id=employee["id"],
            sender_id=employee["id"],
            source_task_id=task["id"],
        )
    return store, employee["id"]


def _cli(home, *args):  # 真 CLI（与用户输入等价）
    return [
        sys.executable,
        "-c",
        "from agent_mailbox.cli import main; raise SystemExit(main())",
        *args,
        "--home",
        str(home),
    ]


def _send_more(store, employee_id: str, count: int) -> None:
    """冷启动之后追加新信（否则会被 cold_start 一次性记为已见 ⇒ 永不触发）。"""
    snapshot = store.snapshot()
    project = snapshot["projects"][0]
    task = snapshot["tasks"][0]
    for index in range(count):
        store.send_message(
            project["id"],
            f"新信 {index}",
            "正文",
            recipient_id=employee_id,
            sender_id=employee_id,
            source_task_id=task["id"],
        )


def test_multiprocess_concurrent_wake_once_wakes_exactly_once(tmp_path):
    """A1 跨进程：5 个并发 `wake --once` ⇒ 宿主 hook 只能被调用 1 次（flock 整轮串行）。"""
    home = tmp_path / "store"
    store, employee = _ready_store(home, 5)
    subprocess.run(_cli(home, "wake", "--once"), check=True, capture_output=True)  # 冷启动
    _send_more(store, employee, 35)  # 冷启动后的突发（才是"新信"）

    (home / wk.WAKE_DIRNAME).mkdir(parents=True, exist_ok=True)
    log = tmp_path / "hook.log"
    hook = home / wk.WAKE_DIRNAME / wk.HOOK_FILENAME
    hook.write_text(
        f'#!/bin/bash\necho "$(date +%s.%N) $$" >> {log}\nsleep 0.4\n', encoding="utf-8"
    )
    hook.chmod(0o755)

    procs = [
        subprocess.Popen(
            _cli(home, "wake", "--once"), stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        for _ in range(5)
    ]
    outs = [p.communicate()[0].decode() for p in procs]
    assert all(p.returncode == 0 for p in procs), outs

    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    assert len(calls) == 1, f"并发下 hook 被调用 {len(calls)} 次（闸门被击穿）：{calls}"
    assert sum("叫醒 1 位" in text for text in outs) == 1, outs
    state = wk.load_state(home)
    assert len(state["employees"][next(iter(state["employees"]))]["wakes"]) == 1


def test_in_process_threads_serialise_wake_once(tmp_path):
    """A1 进程内：5 个线程同时 run_once ⇒ deliver 只应成功一次（threading.Lock 兜住）。"""
    import threading

    home = tmp_path / "store"
    _ready_store(home, 40)
    state = wk.load_state(home)
    state["cold_done"] = True
    wk.save_state(home, state)

    store = WorkbenchStore(home)
    delivered: list[str] = []
    barrier = threading.Barrier(5)

    def deliver(employee_id, decision):
        delivered.append(employee_id)
        time.sleep(0.2)
        return True

    results = []

    def one():
        barrier.wait()
        results.append(
            wk.run_once(
                store,
                home,
                unread_provider=lambda s, e: [f"m{i}" for i in range(40)],
                deliver=deliver,
                employee_ids=["e1"],
                cold_started=True,
            )
        )

    threads = [threading.Thread(target=one) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(delivered) == 1, f"进程内并发击穿：deliver 被调用 {len(delivered)} 次"
    assert sum(bool(r["woke"]) for r in results) == 1


def test_lock_file_deletion_is_recoverable(tmp_path):
    """锁文件被删 ⇒ 下一轮自动重建，不 brick。"""
    home = tmp_path / "store"
    _ready_store(home, 5)
    lock = home / wk.WAKE_DIRNAME / "wake.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("", encoding="utf-8")
    lock.unlink()
    done = subprocess.run(_cli(home, "wake", "--once"), capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    # **契约已变（2026-10-08）** ✗：锁锚从 `wake.lock` 文件改为 `wake/` **目录 fd** ✓
    # （flow-product 三轮证明：拿文件当锚时，持锁期 `rm wake.lock` ⇒ 新 inode 上能再拿一把锁 ✓）
    # ⇒ 锁文件不再被创建 ✓ 断言改为「轮次真的跑过」✓ 意图不变 ✓：删锁**不得 brick**
    assert "冷启动" in done.stdout or "叫醒" in done.stdout, done.stdout


def test_uninstall_is_idempotent_and_leaves_no_residue(tmp_path):
    """从未加载过时 `--uninstall` 幂等且无残留（文件层面；launchd 侧由 bootout 兜）。"""
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir()
    unit = plist_dir / f"{wk.PLIST_LABEL}.plist"
    unit.write_text("<plist/>", encoding="utf-8")
    first = subprocess.run(
        _cli(tmp_path / "home", "wake", "--uninstall", "--plist-dir", str(plist_dir)),
        capture_output=True,
        text=True,
        check=False,
    )
    second = subprocess.run(
        _cli(tmp_path / "home", "wake", "--uninstall", "--plist-dir", str(plist_dir)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert first.returncode == 0 and second.returncode == 0
    assert not unit.exists(), "卸载后单元文件必须不存在"
    assert not list(plist_dir.iterdir()), "plist 目录不得留残渣"


def test_uninstall_boots_out_before_deleting_the_file(tmp_path, monkeypatch):
    """`--uninstall` 必须先 `launchctl bootout gui/<uid>/<label>` 再删文件（顺序用调用序验证）。"""
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir()
    unit = plist_dir / f"{wk.PLIST_LABEL}.plist"
    unit.write_text("<plist/>", encoding="utf-8")

    order: list[str] = []
    real_run = subprocess.run

    def spy(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[:1] == ["launchctl"]:
            order.append("bootout")
            assert cmd[1] == "bootout" and cmd[2] == f"gui/{os.getuid()}/{wk.PLIST_LABEL}", cmd
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(wk.subprocess, "run", spy)
    original_unlink = pathlib.Path.unlink

    def spy_unlink(self, *args, **kwargs):
        if self == unit:
            order.append("delete")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", spy_unlink)
    rc = wk.cli_main(["wake", "--uninstall", "--plist-dir", str(plist_dir)], home=tmp_path / "home")
    assert rc == 0
    assert order == ["bootout", "delete"], f"顺序不对：{order}"


def test_installed_plist_executable_must_be_absolute_and_exist(tmp_path):
    """`install_plist`（CLI 的调用形态）写下的可执行项必须是**绝对路径且真实存在**。

    实测：`agent-mailbox wake --install` 生成的 `ProgramArguments[0] == 'agent-mailbox'`
    ⇒ launchd 后台 PATH 查不到 ⇒ 127 退出。根因：`install_plist` 的默认参数
    `executable="agent-mailbox"` 覆盖了 `plist_text` 里的 `default_executable()`。
    """
    import plistlib

    plist_dir = tmp_path / "LaunchAgents"
    target = wk.install_plist(plist_dir, tmp_path / "home")  # ← CLI 正是这样调（不传 executable）
    data = plistlib.loads(target.read_bytes())
    executable = data["ProgramArguments"][0]
    assert executable.startswith("/"), f"不是绝对路径：{executable!r}"
    assert pathlib.Path(executable).exists(), f"路径不存在：{executable!r}"


def test_lock_path_replaced_by_directory_must_not_brick_wake(tmp_path):
    """`wake.lock` 被换成目录时不得抛未捕获异常（否则核心链路被一个空目录堵死）。"""
    home = tmp_path / "store"
    _ready_store(home, 5)
    lock = home / wk.WAKE_DIRNAME / "wake.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.mkdir()
    done = subprocess.run(_cli(home, "wake", "--once"), capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert "IsADirectoryError" not in done.stderr
    assert "Traceback" not in done.stderr


def test_legit_git_worktree_must_be_accepted(tmp_path):
    """`git worktree add` 出来的**合法**工作树必须能用（B1 修复的过度拦截）。"""
    main = tmp_path / "main"
    _git_repo(main)
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-q", str(linked), "-b", "wt"],
        check=True,
    )
    assert cq.resolve_project_root(linked) == linked.resolve()
