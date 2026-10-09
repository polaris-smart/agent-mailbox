"""Capability contract: one source of truth for what can execute a task (T29).

Why this exists (found 2026-10-05): the "who can be assigned managed work" rule
was written out in **seven** places, one of them inside a SQL string::

    store._employee()            row["kind"] in {"codex","claude"} and connection == "cli"
    store.create_task()          employee["kind"] not in {"codex","claude"} or …
    store claim SQL              e.kind IN ('codex','claude') AND connection_type='cli'
    onboarding._probe()          employee["kind"] not in {"codex","claude"} or …
    runtime.discovery()          kind in SUPPORTED                (×3 uses)

A capability change therefore had to be made in all seven, and any missed spot
fails silently: the store can accept a task the runner never picks up, or refuse
one it could run. This module owns the rule; everything else asks it.

Tests use :func:`use_kinds` to mutate the mapping **in place** — because the
mapping is never rebound, every importer sees the change, so a path that still
hardcodes the old set fails the test.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

# The single source. Mutate in place via use_kinds(); never rebind.
EXECUTION_KINDS: dict[str, str] = {"codex": "Codex", "claude": "Claude Code"}
EXECUTION_CONNECTION = "cli"


def execution_supported(kind: str | None, connection_type: str | None) -> bool:
    """Can this employee kind run managed work at all?

    Note: mailbox tasks (human assigns work through the project mailbox) are
    deliberately **not** gated by this — such an employee works in its own app or
    session. This rule only answers "can the harness spawn and supervise it".
    """
    return bool(kind) and kind in EXECUTION_KINDS and connection_type == EXECUTION_CONNECTION


def execution_sql(employee_alias: str = "e") -> tuple[str, list[str]]:
    """Parameterised SQL fragment + params for the same rule (no string literals).

    Returns ``("AND e.kind IN (?,?) AND e.connection_type='cli'", ["claude","codex"])``
    so callers can append it to a query and extend their parameter list.
    """
    kinds = sorted(EXECUTION_KINDS)
    placeholders = ",".join("?" for _ in kinds)
    clause = (
        f"AND {employee_alias}.kind IN ({placeholders}) "
        f"AND {employee_alias}.connection_type='{EXECUTION_CONNECTION}'"
    )
    return clause, list(kinds)


def use_kinds(kinds: Mapping[str, str] | Iterable[str]) -> None:
    """Replace the contract **in place** (tests, and future runtime capability updates)."""
    mapping = dict(kinds) if isinstance(kinds, Mapping) else {k: k for k in kinds}
    EXECUTION_KINDS.clear()
    EXECUTION_KINDS.update(mapping)
