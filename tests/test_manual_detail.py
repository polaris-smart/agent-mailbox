"""手工登记说明文案：**如实**标注"未被发现 ⇒ 未验证" ✗（HS 第 10b 条 · 用户自救路径 ✓）。"""

from __future__ import annotations

from agent_mailbox.workbench import t_manual_detail


def test_manual_detail_is_honest_about_not_verified():
    """文案必须含"手工登记"与"未验证"，且带上入口 ✓ —— 不假装已验证 ✗。"""
    text = t_manual_detail("/usr/local/bin/my-agent")
    assert "手工登记" in text, text
    assert "未验证" in text, text
    assert "/usr/local/bin/my-agent" in text, "入口要写进说明（用户才知道登记了什么 ✓）"
