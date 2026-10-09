"""Agent-Native 接入文档（HS 第 9 条）：四步结构 + 硬约束 + 固定成功文案，缺一不可 ✓。"""

from __future__ import annotations

from pathlib import Path

DOC = Path(__file__).resolve().parents[1] / "docs" / "AGENT-ACCESS.md"


def test_doc_has_the_four_steps_and_hard_constraints():
    text = DOC.read_text(encoding="utf-8")
    for step in ("第 1 步", "第 2 步", "第 3 步", "第 4 步"):
        assert step in text, f"缺 {step} ✗（对标腾讯 cli-setup 的四步结构 ✓）"
    assert "硬约束" in text, "缺硬约束段 ✗"
    assert "不要猜凭据" in text and "不要重试" in text, "AI 约束条款缺失 ✗"
    assert "opaque string" in text, "必须写明「URL/命令不可改写」✓"


def test_doc_is_truthful_about_who_issues_the_key():
    """**如实**：签发钥匙是 owner-only ✓ agent **不能**自助签发 ✗（不许写成"全自动"✗）。"""
    text = DOC.read_text(encoding="utf-8")
    assert "owner-only" in text, "必须写明签发是人侧操作 ✗"
    assert "agent 不得自行签发" in text, "必须明确禁止 agent 自行签发 ✗"


def test_doc_has_a_fixed_success_line_and_honest_expectations():
    text = DOC.read_text(encoding="utf-8")
    assert "agent-mailbox 接入完成：" in text, "缺固定成功文案 ✗"
    assert "不会自动唤醒" in text, "必须如实写明不会自动唤醒 ✗"
    assert "验收永远由人做" in text, "必须写明终态归人 ✓"
    assert "不需要引擎" in text, "必须写明邮箱模式不需要引擎（与零引擎定版一致 ✓）"
