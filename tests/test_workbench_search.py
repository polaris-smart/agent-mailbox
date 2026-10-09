"""Message search: FTS5 index + triggers + short-query fallback (roadmap T5)."""

from __future__ import annotations

import pytest

from agent_mailbox import workbench_search as ws
from agent_mailbox.workbench_store import WorkbenchStore


@pytest.fixture
def scene(tmp_path):
    store = WorkbenchStore(tmp_path / "data")
    directory = tmp_path / "project"
    directory.mkdir()
    project = store.create_project("Demo", directory)
    alice = store.create_employee("Alice", "codex", project["id"])
    bob = store.create_employee("Bob", "codex", project["id"])
    return store, project, alice, bob


def _send(store, project, title, body, recipient=None):
    return store.send_message(project["id"], title, body, recipient_id=recipient)


@pytest.fixture(autouse=True)
def _built(scene):
    """本文件的用例都在"索引已建"前提下验证 FTS 行为（建索引现在是显式步骤）。"""
    from agent_mailbox import workbench_search as _ws

    _ws.build_index(scene[0])
    return scene


def test_backfill_indexes_messages_that_existed_before_search(scene):
    store, project, alice, _bob = scene
    _send(store, project, "风暴复盘", "今天 watcher 不停的制造风暴，收件箱被刷爆", alice["id"])
    _send(store, project, "无关话题", "这是一封普通的信", alice["id"])

    out = ws.search(store, "收件箱", project_id=project["id"])
    assert out["mode"] == "fts"
    assert out["count"] == 1
    assert out["results"][0]["title"] == "风暴复盘"
    assert "body" not in out["results"][0]  # 默认不返回正文（省 token）


def test_two_char_chinese_query_falls_back_to_like(scene):
    """trigram 需要 ≥3 字符（实测「风暴」命中 0）⇒ 2 字查询必须走回退路径。"""
    store, project, alice, _bob = scene
    _send(store, project, "风暴", "刷屏风暴", alice["id"])
    out = ws.search(store, "风暴", project_id=project["id"])
    assert out["mode"] == "like"
    assert out["count"] == 1

    three = ws.search(store, "刷屏风", project_id=project["id"])
    assert three["mode"] == "fts"
    assert three["count"] == 1


def test_triggers_keep_index_in_sync_on_insert(scene):
    store, project, alice, _bob = scene
    _send(store, project, "第一封", "初始内容", alice["id"])
    assert ws.search(store, "初始内容", project_id=project["id"])["count"] == 1

    _send(store, project, "第二封", "新增内容关于折叠", alice["id"])
    assert ws.search(store, "折叠", project_id=project["id"])["count"] == 1
    assert ws.index_stats(store)["in_sync"] is True


def test_triggers_keep_index_in_sync_on_update_and_delete(scene):
    store, project, alice, _bob = scene
    message = _send(store, project, "旧标题", "旧正文含关键词甲", alice["id"])
    assert ws.search(store, "关键词甲", project_id=project["id"])["count"] == 1

    with store._transaction() as db:
        db.execute("UPDATE messages SET body=? WHERE id=?", ("新正文含关键词乙", message["id"]))
    assert ws.search(store, "关键词甲", project_id=project["id"])["count"] == 0
    assert ws.search(store, "关键词乙", project_id=project["id"])["count"] == 1

    with store._transaction() as db:
        db.execute("DELETE FROM messages WHERE id=?", (message["id"],))
    assert ws.search(store, "关键词乙", project_id=project["id"])["count"] == 0
    assert ws.index_stats(store)["in_sync"] is True


def test_results_are_scoped_to_the_project(scene, tmp_path):
    store, project, alice, _bob = scene
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = store.create_project("Other", other_dir)
    other_emp = store.create_employee("Carol", "codex", other["id"])

    _send(store, project, "本项目的信", "共同关键词在此", alice["id"])
    _send(store, other, "别项目的信", "共同关键词elsewhere", other_emp["id"])

    assert ws.search(store, "共同关键词", project_id=project["id"])["count"] == 1
    assert ws.search(store, "共同关键词", project_id=other["id"])["count"] == 1
    assert ws.search(store, "共同关键词")["count"] == 2  # 不限定项目 = 全部


def test_limit_bodies_and_empty_query(scene):
    store, project, alice, _bob = scene
    for i in range(5):
        _send(store, project, f"批量 {i}", f"正文含标记词 {i}", alice["id"])

    out = ws.search(store, "标记词", project_id=project["id"], limit=2)
    assert out["count"] == 2
    with_body = ws.search(store, "标记词", project_id=project["id"], limit=1, include_bodies=True)
    assert with_body["results"][0]["body"].startswith("正文含标记词")

    with pytest.raises(ValueError):
        ws.search(store, "   ")


def test_query_is_safe_against_fts_syntax(scene):
    """用户输入里的引号/星号不能让 MATCH 抛语法错。"""
    store, project, alice, _bob = scene
    _send(store, project, "语法测试", '含"引号"与 * 星号的内容', alice["id"])
    for raw in ('"引号"', "*", "星号 *", "引号 AND 内容"):
        out = ws.search(store, raw, project_id=project["id"])
        assert out["count"] >= 0  # 不抛异常即通过
    assert ws.search(store, "星号", project_id=project["id"])["count"] == 1


def test_cli_reports_empty_query_without_traceback(tmp_path, capsys):
    """规则 U5：检索失败要自解释（是什么 + 用法），不抛裸 traceback。"""
    from agent_mailbox import cli

    code = cli.main(["search", "", "--home", str(tmp_path / "data")])
    assert code == 2
    err = capsys.readouterr().err
    assert "检索词不能为空" in err
    assert "用法：agent-mailbox search" in err
    assert "Traceback" not in err


def test_search_is_read_only_and_never_builds_the_index(tmp_path):
    """独立审查指出：一次"只读检索"顺手建 6 张表 + 3 个触发器 ⇒ 承诺失真。"""

    from agent_mailbox.workbench_store import WorkbenchStore

    store = WorkbenchStore(tmp_path / "fresh")
    directory = tmp_path / "proj"
    directory.mkdir()
    project = store.create_project("Fresh", directory)
    alice = store.create_employee("Alice", "codex", project["id"])
    store.send_message(project["id"], "一条消息", "正文", recipient_id=alice["id"])

    def schema():
        with store._transaction() as db:
            return sorted(row[0] for row in db.execute("SELECT name FROM sqlite_master").fetchall())

    before = schema()
    out = ws.search(store, "一条消息", project_id=project["id"])
    assert out["count"] == 1 and out["mode"] == "like_no_index"  # 仍能查到
    assert "hint" in out  # 并告诉你怎么变快
    assert schema() == before, "检索路径建了表（应只在 --index 时建）"
    assert ws.index_stats(store)["index_built"] is False
    assert schema() == before, "index_stats 也不许建表"

    ws.build_index(store)  # 显式写入口
    assert ws.index_stats(store)["in_sync"] is True
    assert ws.search(store, "一条消息", project_id=project["id"])["mode"] == "fts"
