"""Unread ids for waking must not depend on brief's 5-row window (e2e-found bug)."""

from __future__ import annotations

import inspect

from agent_mailbox import workbench_wake


def test_wake_uses_recipient_query_not_the_brief_window():
    source = inspect.getsource(workbench_wake.cli_main)
    assert "unread_ids_for_employee" in source, "waking must use the recipient query"
    assert "unread_ids_from_brief" not in source, "the 5-row brief window must not drive waking"


def test_recipient_query_exists_and_is_documented():
    source = inspect.getsource(workbench_wake.unread_ids_for_employee)
    assert "recipient_id" in source, "query by recipient"
    assert "水位线" in source, "the watermark decides freshness, not the unread count"
