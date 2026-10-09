"""零引擎默认（老板 2026-10-07 亲口确认 ✓）：**包里不得有任何 agent cli** ✗。"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "build-workbench.py"


def _load(monkeypatch, engines: bool):
    if engines:
        monkeypatch.setenv("AGENT_MAILBOX_BUILD_ENGINES", "1")
    else:
        monkeypatch.delenv("AGENT_MAILBOX_BUILD_ENGINES", raising=False)
    spec = importlib.util.spec_from_file_location(f"build_mod_{engines}", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fake_engine_tree(tmp_path):
    runtime = tmp_path / "runtime_bridge"
    (runtime / "node_modules" / "acpx" / "bin").mkdir(parents=True)
    binary = runtime / "node_modules" / "acpx" / "bin" / "codex"
    binary.write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 32)  # 假 Mach-O ✓
    return runtime


def test_engines_are_off_by_default(monkeypatch):
    module = _load(monkeypatch, engines=False)
    assert module.INCLUDE_ENGINES is False, "默认必须**不带引擎** ✗（老板拍板 ✓）"


def test_no_engine_binaries_are_collected_by_default(tmp_path, monkeypatch):
    """关掉开关 ⇒ `runtime_binaries()` **必须返回空** ✓ ⇒ 包里不可能有 agent 可执行文件 ✗。"""
    module = _load(monkeypatch, engines=False)
    assert module.runtime_binaries(_fake_engine_tree(tmp_path)) == []


def test_switch_still_collects_when_explicitly_enabled(tmp_path, monkeypatch):
    """内部测试仍可显式开启 ✓（否则我们自己没法验"带引擎"那条路 ✓）。"""
    module = _load(monkeypatch, engines=True)
    entries = module.runtime_binaries(_fake_engine_tree(tmp_path))
    assert entries and entries[0][1].endswith("runtime/deps/node_modules/acpx/bin"), entries


def test_validators_are_skipped_when_engines_are_off(tmp_path, monkeypatch):
    """关引擎时两个校验器**不得报错** ✓（否则默认构建根本跑不完 ✗）。"""
    module = _load(monkeypatch, engines=False)
    bundle = tmp_path / "Agent Mailbox.app" / "Contents" / "Resources"
    bundle.mkdir(parents=True)
    # 空清单一律通过 ✓（零引擎时这就是常态 ✓）
    module.verify_runtime_binaries(bundle, [])
    assert module.repair_expected_binaries(bundle, tmp_path) == []
    module.verify_expected_binaries(bundle)


def test_empty_entries_refusal_only_applies_with_engines(tmp_path, monkeypatch):
    """结构断言：「空清单 ⇒ 拒绝出包」只在**带引擎**时生效 ✓（零引擎时 entries 本就为空 ✓）。"""
    source = SCRIPT.read_text(encoding="utf-8")
    assert "if not entries and INCLUDE_ENGINES:" in source, "缺条件 ⇒ 零引擎构建会被误拒 ✗"


def test_engine_dependency_tree_is_not_shipped_by_default():
    """**关键**：引擎依赖树是经 `datas=` 整棵进包的 ✗ ⇒ 必须随开关 ✓（实测：不然仍 777M ✗）。"""
    source = SCRIPT.read_text(encoding="utf-8")
    idx = source.index("runtime/deps/node_modules")
    block = source[max(0, idx - 1500) : idx + 400]
    assert "if INCLUDE_ENGINES" in block, "引擎依赖树必须在开关之内 ⇒ 否则零引擎包仍会 777M ✗"
    assert "else []" in block, "开关的另一支必须是空清单 ✓（不是「照旧打包」✗）"


def test_app_boots_without_the_engine_tree():
    """零引擎发行态 ⇒ App **必须仍能启动**（实测：硬失败 ⇒ 装好的 App 起不来 ✗）。

    包里不带引擎依赖树是**设计**（老板拍板 ✓）⇒ 启动器不得因此抛错 ✓
    ⇒ 以邮箱模式启动 ✓；受管执行走既有的「按需安装运行时」✓。
    判据用**函数体切片**（不靠注释文字 ✗）：`bundled_runtime` 里不得有 `raise RuntimeError` ✓ 且必须有 `return None` ✓。
    """
    import pathlib as _pathlib  # 自算路径 ✓ 不依赖本文件里可能不存在的名字 ✗

    from agent_mailbox import workbench_app

    app_py = (
        _pathlib.Path(__file__).resolve().parents[1] / "src" / "agent_mailbox" / "workbench_app.py"
    )
    source = app_py.read_text(encoding="utf-8")
    body_start = source.index("def bundled_runtime")
    body_end = source.index("def main")
    body = source[body_start:body_end]
    assert "raise RuntimeError" not in body, "启动器不得因缺依赖树而抛错 ✗"
    assert "return None" in body, "缺依赖树时应以邮箱模式启动（返回 None ✓）"
    assert workbench_app.bundled_runtime() is None, "非 frozen 环境应直接返回 None ✓"
