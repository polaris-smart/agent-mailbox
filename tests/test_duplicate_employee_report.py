"""重复员工审计（只读）：分组 + 引用计数 + 建议保留行（合并前的数字对账基准 ✓）。"""

from __future__ import annotations

from agent_mailbox.workbench_store import WorkbenchStore


def test_single_identity_is_ok(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    store.create_project("P", tmp_path)
    store.create_employee("DSH", "deepseek", None)
    report = store.duplicate_employee_report()
    assert report["ok"] is True and report["duplicate_groups"] == []


def test_duplicates_grouped_with_keeper_and_reference_counts(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    store.create_project("P", tmp_path)
    a = store.create_employee("DSH", "deepseek", None)
    b = store.create_employee("DSH App", "deepseek", None)
    c = store.create_employee("Hermes", "hermes", None)
    report = store.duplicate_employee_report()
    assert report["ok"] is False
    groups = {g["person"]: g for g in report["duplicate_groups"]}
    assert "dsh" in groups and groups["dsh"]["forms"] == 2
    assert groups["dsh"]["keeper_suggestion"] in {a["id"], b["id"]}
    assert c["id"] not in {r["id"] for r in groups["dsh"]["rows"]}
    assert {r["id"] for r in groups["dsh"]["rows"]} == {a["id"], b["id"]}
    assert all("references" in r for r in groups["dsh"]["rows"])
    assert "reference_breakdown" in report
