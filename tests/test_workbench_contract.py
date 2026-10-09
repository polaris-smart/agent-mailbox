"""Capability contract: one source of truth, verified by flipping it (T29).

The point of these tests: mutate the contract **in place** and require every path
to follow. A path that still hardcodes the old set keeps its old behaviour and
fails here — which is exactly the bug class we are eliminating.
"""

from __future__ import annotations

import pathlib

import pytest

from agent_mailbox import workbench_contract as wc
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore

ORIGINAL = dict(wc.EXECUTION_KINDS)
NEWCOMER = "opencode"  # 已登记的员工类型，但不在默认可执行集合里


@pytest.fixture(autouse=True)
def restore_contract():
    yield
    wc.use_kinds(ORIGINAL)


@pytest.fixture
def scene(tmp_path):
    store = WorkbenchStore(tmp_path / "data")
    directory = tmp_path / "project"
    directory.mkdir()
    project = store.create_project("Demo", directory)
    newcomer = store.create_employee("OpenCode", NEWCOMER, project["id"])
    return store, project, newcomer


def test_default_contract_refuses_the_newcomer(scene):
    store, project, newcomer = scene
    assert wc.execution_supported(NEWCOMER, "cli") is False
    with store._transaction() as db:
        row = db.execute("SELECT * FROM employees WHERE id=?", (newcomer["id"],)).fetchone()
        assert store._employee(db, row)["execution_supported"] is False
    with pytest.raises(WorkbenchError) as excinfo:
        store.create_task(project["id"], "managed", "work", newcomer["id"])
    assert excinfo.value.code == "ADAPTER_UNSUPPORTED"


def test_employee_view_follows_the_contract(scene):
    wc.use_kinds({**ORIGINAL, NEWCOMER: "OpenCode"})
    store, _project, newcomer = scene
    with store._transaction() as db:
        row = db.execute("SELECT * FROM employees WHERE id=?", (newcomer["id"],)).fetchone()
        employee = store._employee(db, row)
    assert employee["execution_supported"] is True
    # execution_verified 仍要求真实证据：契约放行 ≠ 已验证
    assert employee["execution_verified"] is False


def test_create_task_follows_the_contract(scene):
    wc.use_kinds({**ORIGINAL, NEWCOMER: "OpenCode"})
    store, project, newcomer = scene
    task = store.create_task(project["id"], "managed", "work", newcomer["id"])
    assert task["execution_mode"] == "managed"


def test_claim_sql_follows_the_contract(scene):
    """SQL 里那处硬编码已参数化：契约一变，认领行为跟着变。"""
    store, project, newcomer = scene
    wc.use_kinds({**ORIGINAL, NEWCOMER: "OpenCode"})
    first = store.create_task(project["id"], "第一单", "work", newcomer["id"])
    second = store.create_task(project["id"], "第二单", "work", newcomer["id"])
    assert store.claim_task(store.local_node()["id"])["id"] == first["id"]

    # 把新员工移出契约：剩下的排队任务就不该再被认领
    wc.use_kinds(ORIGINAL)
    assert store.claim_task(store.local_node()["id"]) is None
    with store._transaction() as db:
        status = db.execute("SELECT status FROM tasks WHERE id=?", (second["id"],)).fetchone()[
            "status"
        ]
    assert status == "queued"


def test_execution_sql_is_parameterised():
    clause, params = wc.execution_sql("e")
    assert "?" in clause and params and sorted(params) == sorted(wc.EXECUTION_KINDS)
    assert "codex" not in clause and "claude" not in clause  # 没有字面量
    assert f"connection_type='{wc.EXECUTION_CONNECTION}'" in clause


def test_no_hardcoded_capability_sets_remain():
    """契约守卫：源码里不许再出现写死的可执行集合（T29 的防回归）。"""
    root = pathlib.Path(wc.__file__).parent
    offenders = []
    for path in sorted(root.glob("*.py")):
        if path.name == pathlib.Path(wc.__file__).name:
            continue
        text = path.read_text(encoding="utf-8")
        for needle in ('{"codex", "claude"}', '{"codex","claude"}', "IN ('codex','claude')"):
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    assert offenders == [], f"仍有写死的可执行集合：{offenders}"
