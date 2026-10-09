"""Self-check: prove our own features work, on a scratch home, in one command (T31 的邻居).

The unit tests prove each piece; the e2e test proves they fit together; this makes
that fit **runnable by a human**:

    agent-mailbox selfcheck

It creates a throwaway home in a temp directory, walks the whole product path
(enrol → assign → schedule → brief → lease → accept → submit → proof → peer verify
→ artifact → wall/ledger/audit → search → contacts → bridge → observation), and
prints one line per step. Nothing touches a real home; nothing is published.

Exit code 0 = every step passed.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

STEPS: list[tuple[str, Callable[[dict], str]]] = []


def step(name: str):
    def wrap(fn: Callable[[dict], str]):
        STEPS.append((name, fn))
        return fn

    return wrap


@step("建立项目与员工")
def _project(ctx: dict) -> str:
    from .workbench_store import WorkbenchStore

    root = Path(ctx["tmp"])
    store = WorkbenchStore(root / "home")
    directory = root / "project"
    directory.mkdir()
    project = store.create_project("自检项目", directory)
    employee = store.create_employee("Codex", "codex", project["id"])
    reviewer = store.create_employee("Reviewer", "codex", project["id"])
    ctx.update(store=store, project=project, employee=employee, reviewer=reviewer, root=root)
    return f"project={project['id'][:14]}… 员工 2 名"


@step("开观察窗（先开窗，再发生一切）")
def _start_window(ctx: dict) -> str:
    from . import workbench_observe

    info = workbench_observe.start(ctx["store"])
    again = workbench_observe.start(ctx["store"])  # 幂等：不得覆盖起点
    assert info["started_at"] == again["started_at"]
    return f"起点 {info['started_at'][:19]}Z · 重复执行不覆盖"


@step("会话与身份绑定（入伙）")
def _enroll(ctx: dict) -> str:
    from .workbench_mail_sessions import create_session

    store, project, employee = ctx["store"], ctx["project"], ctx["employee"]
    session = create_session(store, employee["id"], project["id"], "自检会话")
    session_file = store.root / "workbench/mail-sessions" / f"{session['id']}.json"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text(
        json.dumps(
            {
                "home": str(store.root),
                "session_id": session["id"],
                "employee_id": employee["id"],
                "project_id": project["id"],
                "token": session["token"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    session_file.chmod(0o600)
    ctx["token"] = session["token"]
    mode = session_file.stat().st_mode & 0o777
    assert mode == 0o600, f"会话文件权限应为 600，实际 {oct(mode)}"
    return f"会话 {session['id'][:18]}… · 文件权限 600"


@step("派单（mailbox 任务）")
def _assign(ctx: dict) -> str:
    from .workbench_mail_tasks import create_mail_task

    task = create_mail_task(
        ctx["store"], ctx["project"]["id"], "写一份自检报告", "把结论写清楚", ctx["employee"]["id"]
    )
    assert task["execution_mode"] == "mailbox"
    ctx["task"] = task
    return f"任务 {task['id'][:18]}… · 模式 mailbox"


@step("排期与到期派生（拉取式，无常驻）")
def _schedule(ctx: dict) -> str:
    from . import workbench_schedule

    workbench_schedule.schedule(ctx["store"], ctx["task"]["id"], "2020-01-01T00:00:00+00:00")
    due = workbench_schedule.due_tasks(ctx["store"], ctx["project"]["id"])
    assert [row["id"] for row in due] == [ctx["task"]["id"]]
    return "到期 1 件（未起任何常驻进程）"


@step("会话起始简报（钩子轻通道 · 有预算）")
def _brief(ctx: dict) -> str:
    from . import workbench_brief

    data = workbench_brief.brief(
        ctx["store"], employee_id=ctx["employee"]["id"], project_id=ctx["project"]["id"], budget=400
    )
    text = workbench_brief.render(data, budget=400)
    assert len(text.encode("utf-8")) <= 400
    return f"简报 {len(text.encode('utf-8'))}B ≤ 预算 400B"


@step("文件认领与冲突拦截")
def _lease(ctx: dict) -> str:
    from . import workbench_lease

    store, project = ctx["store"], ctx["project"]
    workbench_lease.claim_paths(
        store, project["id"], ctx["reviewer"]["id"], ["src/**"], reason="先占"
    )
    blocked = workbench_lease.claim_paths(
        store, project["id"], ctx["employee"]["id"], ["src/app.py"]
    )
    assert blocked["granted"] == [] and blocked["conflicts"], "他人认领应拦下"
    workbench_lease.release_paths(store, project["id"], ctx["reviewer"]["id"])
    assert workbench_lease.claim_paths(store, project["id"], ctx["employee"]["id"], ["src/app.py"])[
        "granted"
    ]
    return "冲突被拦 → 释放 → 可认领"


@step("员工接单并提交（走自己的会话）")
def _deliver(ctx: dict) -> str:
    from .workbench_mail_sessions import invoke
    from .workbench_mail_tasks import submit_mail_task

    store, token, task_id = ctx["store"], ctx["token"], ctx["task"]["id"]
    invoke(store, token, "task_accept", {"task_id": task_id})
    submitted = submit_mail_task(store, token, task_id, "自检报告：11/11 PASS")
    assert submitted["status"] == "review", submitted["status"]
    return "queued → running → review（等人验收）"


@step("交付证明 + 同行校验（自己验不算）")
def _proof(ctx: dict) -> str:
    from . import workbench_policy, workbench_proof

    store, task_id = ctx["store"], ctx["task"]["id"]
    artifact = ctx["root"] / "report.md"
    artifact.write_text("自检报告\n11/11 PASS\n", encoding="utf-8")
    proof = workbench_proof.build_proof(
        store,
        task_id,
        artifacts=[str(artifact)],
        criteria=[{"criterion": "结论齐全", "self_check": "pass"}],
    )
    ctx["proof"] = proof
    workbench_policy.set_policy(
        store, {"peer_review": "required_for_write"}, project_id=ctx["project"]["id"]
    )
    try:
        workbench_proof.verify_proof(store, task_id, record=True, verifier_id=ctx["employee"]["id"])
    except ValueError:
        pass  # 自己验自己必须被拒
    else:
        raise AssertionError("自己验自己竟然被接受")
    assert workbench_policy.peer_verified(store, task_id, ctx["employee"]["id"]) is False
    workbench_proof.verify_proof(store, task_id, record=True, verifier_id=ctx["reviewer"]["id"])
    assert workbench_policy.peer_verified(store, task_id, ctx["employee"]["id"]) is True
    return f"证明 {proof['proof_sha256'][:12]}… · 换人校验后通过"


@step("产出物引用按预算读回")
def _artifact(ctx: dict) -> str:
    from . import workbench_artifact

    result = workbench_artifact.read(ctx["store"], ctx["proof"]["artifacts"][0]["ref"], budget=8)
    # 契约：returned_bytes ≤ budget + 3（为补齐一个完整字符最多多读 3 字节）
    assert result["returned_bytes"] <= 8 + 3 and result["hash_verified"] is True
    return f"引用回读 {result['returned_bytes']}B（预算 8B）· 哈希校验通过"


@step("团队墙 / 账本 / 审计链")
def _observe_layer(ctx: dict) -> str:
    from . import workbench_ledger, workbench_wall

    store, project, task = ctx["store"], ctx["project"], ctx["task"]
    view = workbench_wall.wall(store, project["id"])
    chain = workbench_wall.audit_chain(store, task["request_message_id"])
    steps = [s["step"] for s in chain["steps"]]
    assert steps[0] == "sent" and any(s.startswith("terminal:") for s in steps)
    ledger = workbench_ledger.ledger(store, project["id"])
    row = next(r for r in ledger["tasks"] if r["task_id"] == task["id"])
    assert row["has_proof"] and row["proof_verdict"] == "verified"
    return f"墙：等人 {len(view['waiting_on_human'])} · 审计 {len(steps)} 步 · 账本证明={row['proof_verdict']}"


@step("检索（中文 2 字与 3 字两条路径）")
def _search(ctx: dict) -> str:
    from . import workbench_search

    store, project = ctx["store"], ctx["project"]
    workbench_search.build_index(store)  # 显式建索引（检索本身只读）
    long_hit = workbench_search.search(store, "自检报告", project_id=project["id"])
    short_hit = workbench_search.search(store, "报告", project_id=project["id"])
    assert long_hit["count"] >= 1 and short_hit["count"] >= 1
    assert workbench_search.index_stats(store)["in_sync"] is True
    return f"fts 命中 {long_hit['count']} · like 命中 {short_hit['count']} · 索引同步"


@step("通讯录白名单（默认开放，设了才拦）")
def _contacts(ctx: dict) -> str:
    from . import workbench_contacts

    store, project = ctx["store"], ctx["project"]
    assert workbench_contacts.may_message(
        store, project["id"], ctx["reviewer"]["id"], ctx["employee"]["id"]
    )["allowed"]
    workbench_contacts.add_contact(
        store, project["id"], owner_id=ctx["employee"]["id"], target_id=ctx["reviewer"]["id"]
    )
    denied = workbench_contacts.may_message(
        store, project["id"], "employee_outsider", ctx["employee"]["id"]
    )
    assert denied["allowed"] is False
    return "设列表前开放 → 设后拦下陌生发件人"


@step("单向桥接（旧信 → 任务卡，不写回旧信箱）")
def _bridge(ctx: dict) -> str:
    from . import workbench_bridge

    mail = ctx["root"] / "mail" / "inbox" / "Codex"
    mail.mkdir(parents=True)
    letter = mail / "L-9.json"
    letter.write_text(
        json.dumps(
            {
                "id": "L-9",
                "subject": "【任务书】另一件事",
                "from": "HS",
                "to": "Codex",
                "body": "正文",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    before = letter.read_bytes()
    out = workbench_bridge.project(
        ctx["store"],
        ctx["root"] / "mail",
        ctx["project"]["id"],
        apply=True,
        assignee_map={"Codex": ctx["employee"]["id"]},
    )
    assert out["counts"].get("projected") == 1
    assert letter.read_bytes() == before, "旧信箱被写回了（双写）"
    return "投影 1 张卡 · 旧文件字节未变"


@step("观察窗判据（事件计数）")
def _window(ctx: dict) -> str:
    from . import workbench_observe

    store = ctx["store"]
    result = workbench_observe.evaluate(store, sample=5)
    assert result["window"]["started_at"] and result["criteria"]["audit_full"] is True
    c = result["criteria"]
    return f"风暴 {c['storms']} · 真验收 {c['acceptances']} · 审计 {c['audit_reconstructable']}/{c['audit_sampled']}"


def run() -> dict[str, Any]:
    """Run every step on a throwaway home. Returns ``{"ok", "steps", "home"}``."""
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="agent-mailbox-selfcheck-") as tmp:
        context: dict[str, Any] = {"tmp": tmp}
        for name, fn in STEPS:
            try:
                detail = fn(context)
                results.append({"step": name, "ok": True, "detail": detail})
            except Exception as exc:  # noqa: BLE001 - a self-check reports, never crashes
                results.append(
                    {"step": name, "ok": False, "detail": f"{type(exc).__name__}: {exc}"}
                )
        home = str(context.get("store").root) if context.get("store") else None
    return {"ok": all(row["ok"] for row in results), "steps": results, "home": home}


def render(report: dict[str, Any]) -> str:
    lines = [f"自检（临时 home · 不碰真实数据）：{'全部通过' if report['ok'] else '有失败'}"]
    for index, row in enumerate(report["steps"], 1):
        mark = "✓" if row["ok"] else "✗"
        lines.append(f"  {mark} {index:>2}. {row['step']} —— {row['detail']}")
    return "\n".join(lines)
