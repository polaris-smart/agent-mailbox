"""Property tests: who may open a **write** connection, and who may not.

Fifth/sixth reviews' repeated criticism: my fixes were *enumerations* (the paths I
happened to know) rather than *properties*. The sixth review then proved three ways
to slip past the first version of this file with mutants:

1. private functions were skipped entirely (``_resolve_assignee`` hid there while
   taking a write lock in ``bridge project``'s **dry-run** path);
2. the token check missed ``_connection(readonly=False)`` (it looked for the exact
   string ``_connection()``);
3. names matching a write-ish marker (e.g. ``probe_devices``) were exempted, and the
   marker list contained ambiguous words like ``resolve``.

This version detects calls via the AST (any arguments), covers private functions,
uses only **unambiguous** write markers, and self-tests the detector so it cannot
silently stop detecting.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "agent_mailbox"
CONNECTION_CALLS = ("_connection", "_transaction")
# raw_connect() 也是"开一条连接"（无 readonly 形参 ⇒ 视为写）：检测器必须认得它，
# 否则读语义函数只要改用 raw_connect 就能绕过整条只读性质（第八轮指出）。
DETECTED_CALLS = (*CONNECTION_CALLS, "raw_connect")
PRIMITIVES = frozenset((*CONNECTION_CALLS, "raw_connect"))  # 连接工厂本身，不参与判定

# 读语义名字前缀（会先去掉前导下划线再匹配，避免"私有函数免检"）
READ_PREFIXES = (
    "get_",
    "list_",
    "read_",
    "search_",
    "query_",
    "validate_",
    "peek_",
    "context",
    "status",
    "versions",
    "activity",
    "backlog_",
    "last_viewed",
    "enrolled",
    "plan",
    "wall",
    "ledger",
    "brief",
    "due_tasks",
    "schedules",
    "inspect",
    "active_claims",
    "check_conflicts",
    "resolve",
    "index",
    "list_refs",
    "window",
    "evaluate",
    "snapshot",
    "governance_events",
    "latest_proof",
    "verify_proof",
    "peer_verified",
    "effective_policy",
    "marks_present",
    "readonly_index_status",
    "index_stats",
    "contacts",
    "may_message",
    "discover",
    "project_context",
    "local_node",
    "onboarding_status",
    "assembl",
    "find_",
    "fetch_",
    "count_",
    "classify",
    "order_",
)

# 只保留"明确表示写"的词（去掉了 resolve/probe/start/stop/run/pending/open/close 等歧义词）
WRITE_MARKERS = (
    "create",
    "update",
    "set_",
    "add_",
    "send",
    "record",
    "finish",
    "claim",
    "cancel",
    "freeze",
    "unfreeze",
    "ensure",
    "install",
    "approve",
    "reject",
    "delete",
    "remove",
    "apply",
    "release",
    "write",
    "mark",
    "attach",
    "detach",
    "backup",
    "restore",
    "import",
    "export",
    "pause",
    "resume",
    "prepare",
    "register",
    "unregister",
    "revoke",
    "grant",
    "capture",
    "prune",
    "reap",
    "fold",
    "compact",
    "sync",
    "bootstrap",
    "init",
    "migrate",
    "save",
    "emit",
    "retry",
    "flush",
    "stage",
    "commit",
    "upsert",
    "governance",
    "scrub",
    "seed",
)

# 名字像读、但**按设计确实会写** —— 每条都必须写清理由（新增条目要能说服审查者）。
WRITE_BY_DESIGN: dict[str, str] = {
    "verify_proof": "record=True 时写 delivery_verified 事件（默认只读）",
    "resolve_permission": "落审批决定（经 _resolve_permission 写库）——读语义的名字、写语义的行为",
}

# 开写连接、但名字里看不出「写」的函数 —— 必须显式声明，尽量短、每条都能讲清为什么必须写。
DECLARED_WRITERS: frozenset[str] = frozenset(
    {
        "record_notification",  # 2026-10-07：外发通知落库（写 ✓）
        "mark_notification",  # 发送结果回写（写 ✓）
        "link_identity",  # 外部身份↔员工映射（写 ✓）
        "repoint_employee_references",  # 2026-10-07：员工引用重指向（写 ✓ 显式声明 ✓）
        # 2026-10-07：员工行合并的 memberships 去重（写操作 ✓ 名字不含 create/update ⇒ 显式声明 ✓）
        "merge_employee_memberships",
        "project",
        "request_contact",
        "enroll",
        "invoke",
        "accept_mail_task",
        "submit_mail_task",
        "start",
        "build_proof",
        "schedule",
        "run_now",
        "build_index",
        "employee_messages",
        "review_task",
        "follow_up_task",
        "recover_runs",
        "request_permission",
        "permission_decision",
        "expire_permission",
        "update_channel",
        "resolve_permission",
        "verify_proof",
    }
)


def _sqlite_aliases(tree: ast.AST) -> set[str]:
    """模块里 ``sqlite3`` 的可用名（``import sqlite3`` / ``import sqlite3 as sq``）。"""
    names = {"sqlite3"}
    for sub in ast.walk(tree):
        if isinstance(sub, ast.Import):
            for alias in sub.names:
                if alias.name == "sqlite3":
                    names.add(alias.asname or alias.name)
    return names


def _is_sqlite_connect(call: ast.Call, aliases: set[str]) -> bool:
    """Is this call a **SQLite** connect (rather than e.g. HTTP ``connection.connect()``)?"""
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr != "connect":
        return False
    receiver = func.value
    if not isinstance(receiver, ast.Name) or receiver.id not in aliases:
        return False
    # ``file:...?mode=ro`` + uri=True ⇒ 只读连接，不算写。
    # URI 常是拼接/f-string（如 ``path.as_uri() + "?mode=ro…"``），故按**源码文本**判定。
    if call.args:
        try:
            rendered = ast.unparse(call.args[0])
        except (ValueError, AttributeError):  # pragma: no cover
            rendered = ""
        if "mode=ro" in rendered:
            return False
    return True


def opens_write_connection(node: ast.FunctionDef, *, aliases: set[str] | None = None) -> bool:
    """True when the function opens a connection without ``readonly=True``.

    覆盖三种真实写法（第六轮变异证明前两种曾能绕过）：
    * ``store._connection(...)`` / ``store._transaction(...)``（带参不带的都算）；
    * **直接** ``sqlite3.connect(...)``（仓库里真有这条路径，例如 updates 的备份连接）；
    * ``getattr(store, "_transaction")()`` 之类间接调用。
    """
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        name = None
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Call):  # getattr(x, "_transaction")()
            inner = func.func
            if isinstance(inner, ast.Name) and inner.id == "getattr" and func.args:
                second = func.args[1] if len(func.args) > 1 else None
                if isinstance(second, ast.Constant) and isinstance(second.value, str):
                    name = second.value
        if isinstance(func, ast.Attribute) and func.attr == "connect":
            if not _is_sqlite_connect(sub, aliases or {"sqlite3"}):
                continue
            name = "connect"
        if name in DETECTED_CALLS or name == "connect":
            readonly = any(
                kw.arg == "readonly"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in sub.keywords
            )
            if not readonly:
                return True
    return False


def _functions() -> list[tuple[str, ast.FunctionDef, set[str]]]:
    out: list[tuple[str, ast.FunctionDef, set[str]]] = []
    for path in sorted(SRC.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = _sqlite_aliases(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                out.append((path.name, node, aliases))
    return out


def _base_name(name: str) -> str:
    return name.lstrip("_")


def _is_write_named(name: str) -> bool:
    return any(marker in _base_name(name).lower() for marker in WRITE_MARKERS)


def _is_read_named(name: str) -> bool:
    base = _base_name(name)
    return any(base.startswith(prefix) for prefix in READ_PREFIXES)


def test_read_semantics_functions_never_open_a_write_connection():
    offenders = [
        f"{fname}:{node.lineno} {node.name}"
        for fname, node, aliases in _functions()
        if node.name not in PRIMITIVES
        and node.name not in WRITE_BY_DESIGN
        and _is_read_named(node.name)
        and not _is_write_named(node.name)
        and opens_write_connection(node, aliases=aliases)
    ]
    assert offenders == [], (
        "以下「只读语义」函数仍开写连接（会把库 delete→WAL、并与写者抢锁）：\n  "
        + "\n  ".join(offenders)
        + "\n  确属按设计的写路径请加入 WRITE_BY_DESIGN 并写明理由。"
    )


def test_every_write_connection_is_a_declared_writer():
    offenders = [
        f"{fname}:{node.lineno} {node.name}"
        for fname, node, aliases in _functions()
        if node.name not in PRIMITIVES
        and opens_write_connection(node, aliases=aliases)
        and node.name not in DECLARED_WRITERS
        and not _is_write_named(node.name)
    ]
    assert offenders == [], (
        "以下函数开了写连接、名字却不像「写」，且未在 DECLARED_WRITERS 声明：\n  "
        + "\n  ".join(offenders)
        + "\n  若确属只读语义，请改用 _transaction(readonly=True)。"
    )


def test_the_detector_actually_detects_the_three_known_bypasses():
    """自检：第六轮用变异证明过三种绕过 —— 检测器必须能抓住它们。

    没有这条，性质测试可能某天"静默失效"（只剩绿），这正是第六轮 mutant 实验暴露的风险。
    """
    sample = ast.parse(
        "def probe_devices():\n"
        "    with store._connection() as db:\n"
        "        return db\n"
        "\n"
        "def device_details():\n"
        "    with store._connection(readonly=False) as db:\n"
        "        return db\n"
        "\n"
        "def _resolve_assignee():\n"
        "    with store._transaction() as db:\n"
        "        return db\n"
        "\n"
        "def read_thing():\n"
        "    with store._transaction(readonly=True) as db:\n"
        "        return db\n"
    )
    checked = {
        node.name: opens_write_connection(node)
        for node in ast.walk(sample)
        if isinstance(node, ast.FunctionDef)
    }
    assert checked["probe_devices"] is True  # 写标记词不能当免检
    assert checked["device_details"] is True  # 带参数的 readonly=False 必须被识别
    assert checked["_resolve_assignee"] is True  # 私有函数不能免检
    assert checked["read_thing"] is False  # readonly=True 不算写


def test_write_markers_have_no_ambiguous_words():
    """歧义词（resolve/probe/start…）会让"名字像写"变成免检通道 —— 禁止回归。"""
    ambiguous = {"resolve", "probe", "start", "stop", "run", "pending", "open", "close", "load"}
    assert not (set(WRITE_MARKERS) & ambiguous)


def test_declared_lists_have_no_dead_entries():
    """反向校验两张免检名单：每个名字必须**存在**、且**真的**开写连接。

    第六轮靠人记得手工删掉死条目 `plan_update`；第七轮用变异证明"新增读语义函数 + 两行名单"
    就能全绿。这条把名单从"无限免责通道"变成"可校验声明"。
    """
    by_name: dict[str, list[tuple[ast.FunctionDef, set[str]]]] = {}
    for _fname, node, aliases in _functions():
        by_name.setdefault(node.name, []).append((node, aliases))

    problems: list[str] = []
    for name in sorted(DECLARED_WRITERS):
        nodes = by_name.get(name)
        if not nodes:
            problems.append(f"DECLARED_WRITERS 里的 {name} 在源码中不存在（死条目）")
            continue
        if not any(opens_write_connection(node, aliases=al) for node, al in nodes):
            problems.append(f"DECLARED_WRITERS 里的 {name} 其实不开写连接（应移出清单）")
    for name in sorted(WRITE_BY_DESIGN):
        nodes = by_name.get(name)
        if not nodes:
            problems.append(f"WRITE_BY_DESIGN 里的 {name} 在源码中不存在（死条目）")
            continue
        if not any(opens_write_connection(node, aliases=al) for node, al in nodes):
            problems.append(f"WRITE_BY_DESIGN 里的 {name} 其实不开写连接")
        if not _is_read_named(name):
            problems.append(f"WRITE_BY_DESIGN 里的 {name} 名字并不像读语义（应移出清单）")
    assert problems == [], "\n  ".join(problems)


def test_detector_covers_raw_connect_and_indirection():
    """第五/六轮盲区：直接 ``sqlite3.connect`` / ``getattr`` 间接 / ``async def`` 都要被抓住。"""
    sample = ast.parse(
        "import sqlite3\n"
        "def ledger_lookup():\n"
        "    return sqlite3.connect('x.db')\n"
        "\n"
        "def sneaky():\n"
        "    return getattr(store, '_transaction')()\n"
        "\n"
        "async def async_reader():\n"
        "    with store._transaction() as db:\n"
        "        return db\n"
        "\n"
        "def read_only_ok():\n"
        "    with store._transaction(readonly=True) as db:\n"
        "        return db\n"
    )
    checked = {
        node.name: opens_write_connection(node)
        for node in ast.walk(sample)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert checked["ledger_lookup"] is True
    assert checked["sneaky"] is True
    assert checked["async_reader"] is True
    assert checked["read_only_ok"] is False
