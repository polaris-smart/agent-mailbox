"""零引擎首启口径：**没有引擎是常态** ✓ 不是"缺东西" ✗（老板拍板形态 ✓）。"""

from __future__ import annotations

from pathlib import Path

JS = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "agent_mailbox"
    / "workbench_assets"
    / "workbench.js"
)


def test_first_run_copy_treats_mailbox_mode_as_ready():
    source = JS.read_text(encoding="utf-8")
    assert "邮箱模式现在已经可用" in source, "缺引擎 ⇒ 必须说「现在就能用」✗ 不许说成等准备 ✓"
    assert "不含任何内置智能体" in source, "必须如实写明**不含内置智能体** ✓（老板拍板形态 ✓）"
    assert "才是需要准备执行环境" in source or "才需要准备执行环境" in source, (
        "必须写明执行环境是**可选** ✓（只在我们代跑时才需要 ✓）"
    )
    assert "让员工开始工作，还需要准备执行环境" not in source, "旧的「缺东西」口径必须消失 ✗"
