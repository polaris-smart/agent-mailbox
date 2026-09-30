"""The optional adapter exposes unsupported platforms without launching tools."""

from types import SimpleNamespace

import pytest

from agent_mailbox import workbench_knowledge
from agent_mailbox.workbench_store import WorkbenchError


def test_windows_codegraph_returns_actionable_unsupported_status(tmp_path, monkeypatch):
    monkeypatch.setattr(workbench_knowledge, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(workbench_knowledge, "_binary", lambda: pytest.fail("Do not launch tools"))
    result = workbench_knowledge.knowledge_status(tmp_path)
    assert result["error_code"] == "KNOWLEDGE_PLATFORM_UNSUPPORTED"
    assert not result["available"]
    with pytest.raises(WorkbenchError) as error:
        workbench_knowledge.knowledge_query(tmp_path, "Example")
    assert error.value.code == "KNOWLEDGE_PLATFORM_UNSUPPORTED"
