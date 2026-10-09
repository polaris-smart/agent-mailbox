"""Regressions for the **second** independent review (2026-10-05, adversarial).

The reviewer's verdict was "1 高 / 5 中 / 7 低；作者『已全部修复』不成立". Each case
below reproduces one finding so it cannot come back.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import sys

import pytest

from agent_mailbox import workbench_policy, workbench_proof
from agent_mailbox.workbench_mail_tasks import create_mail_task
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore
from agent_mailbox.workbench_workspaces import apply_delivery

REPO = pathlib.Path(__file__).resolve().parents[1]
CLI = [sys.executable, "-m", "agent_mailbox"]


@pytest.fixture
def scene(tmp_path):
    store = WorkbenchStore(tmp_path / "data")
    directory = tmp_path / "project"
    directory.mkdir()
    project = store.create_project("Demo", directory)
    alice = store.create_employee("Alice", "codex", project["id"])
    bob = store.create_employee("Bob", "codex", project["id"])
    task = create_mail_task(store, project["id"], "写能力交付", "说明", alice["id"])
    with store._transaction() as db:
        db.execute("UPDATE tasks SET status='review' WHERE id=?", (task["id"],))
    artifact = tmp_path / "out.md"
    artifact.write_text("第一版", encoding="utf-8")
    workbench_proof.build_proof(store, task["id"], artifacts=[str(artifact)])
    workbench_policy.set_policy(
        store, {"peer_review": "required_for_write"}, project_id=project["id"]
    )
    return store, project, alice, bob, task, artifact


def test_rv2_2_cli_record_requires_a_real_verifier(scene, tmp_path):
    """RV2-3：CLI 留痕入口曾写 verifier_id=None ⇒ 同行校验永远不满足（功能性死锁）。"""
    store, _project, _alice, bob, task, _artifact = scene
    home = store.root
    without = subprocess.run(
        [*CLI, "proof-verify", task["id"], "--record", "--home", str(home)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert without.returncode == 2 and "--verifier" in without.stderr
    assert "Traceback" not in without.stderr

    with_peer = subprocess.run(
        [
            *CLI,
            "proof-verify",
            task["id"],
            "--record",
            "--verifier",
            bob["id"],
            "--home",
            str(home),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert with_peer.returncode == 0, with_peer.stderr
    assert workbench_policy.peer_verified(store, task["id"], _alice["id"]) is True


def test_rv2_3_verifier_identity_is_enforced(scene):
    """RV2-3：任意字符串都能当"另一名员工"⇒ 未强制。"""
    store, _project, alice, bob, task, _artifact = scene
    with pytest.raises(ValueError):
        workbench_proof.verify_proof(store, task["id"], record=True, verifier_id="whoever_i_say")
    with pytest.raises(ValueError):
        workbench_proof.verify_proof(
            store, task["id"], record=True, verifier_id=alice["id"] + "_alt"
        )
    with pytest.raises(ValueError):
        workbench_proof.verify_proof(
            store, task["id"], record=True, verifier_id=alice["id"]
        )  # 自己
    other_dir = store.root.parent / "other"
    other_dir.mkdir()
    other = store.create_project("Other", other_dir)
    stranger = store.create_employee("Stranger", "codex", other["id"])
    with pytest.raises(ValueError):
        workbench_proof.verify_proof(
            store, task["id"], record=True, verifier_id=stranger["id"]
        )  # 非本项目
    assert workbench_policy.peer_verified(store, task["id"], alice["id"]) is False
    workbench_proof.verify_proof(store, task["id"], record=True, verifier_id=bob["id"])
    assert workbench_policy.peer_verified(store, task["id"], alice["id"]) is True


def test_rv2_4_gate_rechecks_hashes_after_verification(scene):
    """RV2-3（TOCTOU）：同行验过后换掉产出物字节，门禁曾仍放行。"""
    store, _project, alice, bob, task, artifact = scene
    workbench_proof.verify_proof(store, task["id"], record=True, verifier_id=bob["id"])
    assert workbench_policy.peer_verified(store, task["id"], alice["id"]) is True

    artifact.write_text("被我换掉了", encoding="utf-8")
    with pytest.raises(WorkbenchError) as excinfo:
        apply_delivery(store, task["id"])
    assert excinfo.value.code == "PROOF_NOT_VERIFIED", "验后换字节仍被放行"


def test_rv2_5_python_m_exit_code_parity(tmp_path, scene):
    """RV2-4：`python -m agent_mailbox` 曾吞掉所有非零退出码。"""
    store, _project, _alice, _bob, _task, _artifact = scene
    home = str(store.root)
    bad = ["policy", "--home", home, "--set", "peer_review=不是选项"]
    script = subprocess.run([*CLI, *bad], capture_output=True, text=True, check=False)
    module = subprocess.run(
        [sys.executable, "-m", "agent_mailbox", *bad],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO),
        env={"PYTHONPATH": str(REPO / "src"), "PATH": "/usr/bin:/bin"},
    )
    assert script.returncode == module.returncode == 2, (script.returncode, module.returncode)


def test_rv2_6_project_message_requires_a_recipient(scene):
    """RV2-3：MCP project_message 的 recipient_id 默认空串 ⇒ 默认全项目群发。"""
    import asyncio

    from agent_mailbox import mailbox_mcp

    store, project, alice, _bob, _task, _artifact = scene
    from agent_mailbox.workbench_mail_sessions import create_session

    session = create_session(store, alice["id"], project["id"], "probe")
    session_file = store.root / "workbench/mail-sessions" / f"{session['id']}.json"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text(
        json.dumps(
            {
                "home": str(store.root),
                "session_id": session["id"],
                "employee_id": alice["id"],
                "project_id": project["id"],
                "token": session["token"],
            }
        ),
        encoding="utf-8",
    )
    session_file.chmod(0o600)
    server = mailbox_mcp.build_server(session_file)
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    schema = tools["project_message"].input_schema or tools["project_message"].inputSchema
    assert "recipient_id" in schema.get("required", []), "收件人不是必填 ⇒ 仍可能默认群发"

    # 真调用（schema 只保证「参数存在」）：空串/缺参必须在派发层被拒
    from agent_mailbox.workbench_mail_sessions import invoke

    for args in (
        {"title": "缺参", "body": "b"},
        {"title": "空串", "body": "b", "recipient_id": ""},
    ):
        with pytest.raises(WorkbenchError) as excinfo:
            invoke(store, session["token"], "message", args)
        assert excinfo.value.code == "MAILBOX_ARGUMENT_DENIED"


def test_rv2_7_like_wildcards_are_escaped(scene, tmp_path):
    """RV2-低：短查询 LIKE 未转义 %/_ ⇒ 查 % 命中全部。"""
    from agent_mailbox import workbench_search

    store, project, alice, _bob, _task, _artifact = scene
    store.send_message(project["id"], "甲消息", "正文", recipient_id=alice["id"])
    store.send_message(project["id"], "乙消息", "正文", recipient_id=alice["id"])
    result = workbench_search.search(store, "%", project_id=project["id"])
    assert result["count"] == 0, f"通配符未转义：查 % 命中 {result['count']} 条"
    assert workbench_search.search(store, "_", project_id=project["id"])["count"] == 0


def test_rv2_9_artifact_head_reports_real_bytes(scene, tmp_path):
    """RV2-低：head 投影的 returned_bytes 曾报切片大小而非返回内容大小。"""
    from agent_mailbox import workbench_artifact

    store, _project, _alice, _bob, task, artifact = scene
    artifact.write_text("首行" + "x" * 500 + "\n第二行\n第三行\n", encoding="utf-8")
    proof = workbench_proof.build_proof(store, task["id"], artifacts=[str(artifact)])
    ref = proof["artifacts"][0]["ref"]
    result = workbench_artifact.read(store, ref, budget=4000, projection="head")
    assert result["returned_bytes"] == len(result["content"].encode("utf-8"))
    assert result["returned_bytes"] < 600


def test_rv2_8_read_paths_do_not_take_a_write_lock(scene, monkeypatch):
    """RV2-2：只读检索曾开 BEGIN IMMEDIATE ⇒ 被写者挡住 10s 后误报"磁盘"故障。"""
    import sqlite3
    import time

    from agent_mailbox import workbench_brief, workbench_ledger, workbench_search, workbench_wall

    store, project, alice, _bob, _task, _artifact = scene
    store.send_message(project["id"], "一条消息", "正文", recipient_id=alice["id"])

    blocker = sqlite3.connect(store.db_path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")  # 另一个写者持锁
    try:
        started = time.monotonic()
        for name, call in (
            (
                "search",
                lambda: workbench_search.search(store, "一条消息", project_id=project["id"]),
            ),
            ("wall", lambda: workbench_wall.wall(store, project["id"])),
            ("ledger", lambda: workbench_ledger.ledger(store, project["id"])),
            ("brief", lambda: workbench_brief.brief(store, project_id=project["id"])),
        ):
            result = call()
            assert result is not None, name
        assert time.monotonic() - started < 2.0, "只读路径被写锁挡住了"

        # 写路径：应给出"忙，请稍后重试"，而不是谎报磁盘/保存错误
        monkeypatch.setenv("AGENT_MAIL_BUSY_TIMEOUT_MS", "100")
        with pytest.raises(WorkbenchError) as excinfo:
            store.send_message(project["id"], "写一条", "正文", recipient_id=alice["id"])
        assert excinfo.value.code == "busy", excinfo.value.code
        assert "磁盘" not in str(excinfo.value.message)
    finally:
        blocker.rollback()
        blocker.close()


def test_rv2_10_unknown_project_is_rejected(scene, tmp_path):
    """RV2-低：打错的 --project 曾被静默当成"全局/空"，rc=0。"""
    from agent_mailbox import workbench_brief, workbench_ledger, workbench_wall

    store, _project, _alice, _bob, _task, _artifact = scene
    for call in (
        lambda: workbench_ledger.ledger(store, "project_nope"),
        lambda: workbench_wall.wall(store, "project_nope"),
        lambda: workbench_brief.brief(store, project_id="project_nope"),
    ):
        with pytest.raises(WorkbenchError) as excinfo:
            call()
        assert excinfo.value.code == "not_found"
    cli = subprocess.run(
        [*CLI, "ledger", "--project", "project_nope", "--home", str(store.root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert cli.returncode == 2 and "Traceback" not in cli.stderr


def test_rv2_11_same_content_artifact_prefers_the_newest_copy(scene, tmp_path):
    """RV2-低：同内容多副本时索引保留最旧 ⇒ 最旧文件没了就拒绝（等价副本仍在）。"""
    from agent_mailbox import workbench_artifact

    store, project, alice, _bob, task, _artifact = scene
    older = tmp_path / "older.md"
    older.write_text("同一份内容", encoding="utf-8")
    newer = tmp_path / "newer.md"
    newer.write_text("同一份内容", encoding="utf-8")
    first = workbench_proof.build_proof(store, task["id"], artifacts=[str(older)])
    ref = first["artifacts"][0]["ref"]
    second_task = create_mail_task(store, project["id"], "第二单", "说明", alice["id"])
    workbench_proof.build_proof(store, second_task["id"], artifacts=[str(newer)])

    entry = workbench_artifact.index(store)[ref]
    assert entry["task_id"] == second_task["id"], "同内容多副本应归最新那份"

    older.unlink()  # 最旧副本没了
    result = workbench_artifact.read(store, ref, budget=100)
    assert result["hash_verified"] is True and result["content"], "等价副本仍在却拒绝读取"
    assert result["path"] == str(newer)


def test_rv2_12_lease_path_is_normalised():
    """RV2-低：`src/../etc/passwd` 曾绕过 `etc/**` 认领。"""
    from agent_mailbox import workbench_lease

    assert workbench_lease.matches("etc/**", "src/../etc/passwd") is True
    assert workbench_lease.matches("etc/**", "./etc/passwd") is True


def test_rv2_13_deep_json_and_huge_files_do_not_crash(scene, tmp_path):
    """RV2-7：深嵌套 JSON 曾抛 RecursionError（CLI 裸栈）；大文件曾整份进内存。"""
    from agent_mailbox import workbench_artifact

    store, _project, _alice, _bob, task, _artifact = scene
    deep = tmp_path / "deep.json"
    deep.write_text("[" * 20000 + "]" * 20000, encoding="utf-8")
    ref = "art:" + hashlib.sha256(deep.read_bytes()).hexdigest()[:16]
    workbench_proof.build_proof(store, task["id"], artifacts=[str(deep)])
    result = workbench_artifact.read(store, ref, budget=100, projection="json_keys")
    assert result["refused"] == "too_deep" and result["content"] is None

    huge = tmp_path / "huge.bin"
    with huge.open("wb") as handle:
        handle.truncate(workbench_artifact.MAX_ARTIFACT_BYTES + 1)  # 稀疏文件，不占磁盘
    workbench_proof.build_proof(store, task["id"], artifacts=[str(huge)])
    entry_ref = next(row["ref"] for row in workbench_artifact.list_refs(store, task_id=task["id"]))
    big = workbench_artifact.read(store, entry_ref, budget=100)
    assert big["refused"] == "too_large" and big["content"] is None


def test_rv2_14_invalid_busy_timeout_env_does_not_crash(scene, monkeypatch):
    """自查：环境变量写错不该让命令吐裸 ValueError。"""
    store, _project, _alice, _bob, _task, _artifact = scene
    # 逐输入的确切期望：非法/负数回落默认；0 是合法值（不等待）
    for raw, expected in (("abc", 10000), ("", 10000), ("-5", 10000), ("0", 0), ("250", 250)):
        monkeypatch.setenv("AGENT_MAIL_BUSY_TIMEOUT_MS", raw)
        assert WorkbenchStore._busy_timeout_ms() == expected, raw
    monkeypatch.setenv("AGENT_MAIL_BUSY_TIMEOUT_MS", "250")
    assert WorkbenchStore._busy_timeout_ms() == 250
    monkeypatch.delenv("AGENT_MAIL_BUSY_TIMEOUT_MS")
    assert WorkbenchStore._busy_timeout_ms() == 10000
    # 真开一次事务（会读 PRAGMA），确认不崩
    monkeypatch.setenv("AGENT_MAIL_BUSY_TIMEOUT_MS", "abc")
    with store._transaction() as db:
        assert db.execute("SELECT count(*) AS c FROM projects").fetchone()["c"] >= 1
