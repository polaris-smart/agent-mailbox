"""卫生门：凭据进仓必须 FAIL ✓ 本机路径只 WARN ✓（发布"干净系统"的机器判据）。"""

from __future__ import annotations

import importlib.util
import pathlib

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "check-hygiene.py"


def _mod():
    spec = importlib.util.spec_from_file_location("check_hygiene", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_clean_repo_passes(tmp_path):
    (tmp_path / "a.py").write_text("print('hello')\n")
    fails, warns = _mod().scan(tmp_path, [tmp_path / "a.py"])
    assert fails == [] and warns == []


def test_session_and_secret_are_fatal(tmp_path):
    (tmp_path / "session.json").write_text('{"token": "abcdefghijklmnop1234"}')
    (tmp_path / "s.env").write_text("FEISHU_APP_SECRET=xyz\n")
    files = [tmp_path / "session.json", tmp_path / "s.env"]
    fails, _ = _mod().scan(tmp_path, files)
    assert len(fails) >= 2 and all(line.startswith("[FAIL]") for line in fails)


def test_home_path_is_warn_not_fail(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("see /Users/someone/project/file.py\n")
    fails, warns = _mod().scan(tmp_path, [doc])
    assert fails == [] and any("本机绝对路径" in w for w in warns)


def test_main_exit_code_reflects_fails(tmp_path):
    m = _mod()
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "ok.py").write_text("x = 1\n")
    assert m.main(["--all", "--root", str(clean)]) == 0
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    (dirty / "leak.json").write_text('{"token": "abcdefghijklmnop1234"}')
    assert m.main(["--all", "--root", str(dirty)]) == 1


def test_fixture_allowlist_is_narrow_and_documented(tmp_path):
    """白名单只放测试夹具 ✓ 且必须写明理由 ✓（防止为"让扫描变绿"而放宽规则 ✗）。"""
    m = _mod()
    assert set(m.FIXTURE_RULE_ALLOWLIST) == {
        "tests/test_workbench_resources.py",
        "tests/test_workbench_runtime.py",
        "tests/test_hygiene_scan.py",
        "docs/reviews/2026-10-07-round6-process.md",
    }
    # 更强的**不变量**：白名单只可能是"测试夹具"或"归档证据报告" ✓ 别的路径一律不许进 ✗
    for key in m.FIXTURE_RULE_ALLOWLIST:
        assert key.startswith(("tests/", "docs/reviews/")), f"白名单出现了不该有的路径: {key}"
    source = SCRIPT.read_text(encoding="utf-8")
    assert "不是泄露" in source and "整文件跳过" in source, "白名单必须写明为何不整文件跳过 ✓"


def test_internal_doc_warning_only_applies_to_docs(tmp_path):
    m = _mod()
    (tmp_path / "docs").mkdir()
    doc = tmp_path / "docs" / "plan.md"
    doc.write_text("任务书 评审报告\n")
    _fails, warns = m.scan(tmp_path, [doc])
    assert any("内部" in w for w in warns)
    test_file = tmp_path / "test_review_x.py"
    test_file.write_text("# 评审 用例\n")
    _fails2, warns2 = m.scan(tmp_path, [test_file])
    assert not any("内部" in w for w in warns2), "测试文件名不该触发内部资料告警 ✗"


def test_per_rule_allowlist_still_blocks_other_rules(tmp_path):
    """核心加固：白名单**按规则** ⇒ 白名单文件里的**其它**凭据仍然 FAIL ✓（评审：整文件跳过 ✗）。"""
    m = _mod()
    fixture = tmp_path / "tests"
    fixture.mkdir()
    f = fixture / "test_workbench_resources.py"  # 只允许"私钥"这一条 ✓
    f.write_text("-----BEGIN PRIVATE KEY-----\nfake\n")
    fails, _ = m.scan(tmp_path, [f])
    assert fails == [], "被允许的规则不该报 ✗"
    f.write_text("-----BEGIN PRIVATE KEY-----\nfake\nFEISHU_APP_SECRET=abcdefghijklmnopqrst\n")
    fails2, _ = m.scan(tmp_path, [f])
    assert fails2, "同一文件里的**其它**凭据必须仍然 FAIL ✓（旧实现整文件跳过 ⇒ 会漏 ✗）"


def test_yaml_and_bare_assignments_are_caught(tmp_path):
    m = _mod()
    cases = (
        'feishu_app_secret: "abcdefghijklmnop1234"\n',
        "VOLC_AP_WU_API_KEY=abcdefghijklmnop1234\n",
    )
    for i, body in enumerate(cases):
        path = tmp_path / f"c{i}.env"
        path.write_text(body)
        fails, _ = m.scan(tmp_path, [path])
        assert any("环境/YAML 赋值" in f for f in fails), body


def test_expression_assignments_are_not_flagged(tmp_path):
    """误伤防线：`token = await get_token()` 这类**变量赋值**不算凭据 ✓。"""
    m = _mod()
    path = tmp_path / "code.py"
    path.write_text("token = await get_token()\napi_key = credentials.get('api_key')\n")
    fails, _ = m.scan(tmp_path, [path])
    assert fails == [], fails
