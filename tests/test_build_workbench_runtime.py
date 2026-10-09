"""打包管线：原生运行时二进制必须真的进包。

实测事故：只靠 PyInstaller 的 ``datas`` 收集 ``node_modules`` 时，
``@openai/codex-darwin-arm64/vendor/<target>/bin/{codex,codex-code-mode-host}``
（228MB + 62MB）**静默没进产物**；装好的 app 第一次派活才以 ``RUNTIME_MISSING`` 暴露。
修法：显式按 ``binaries=`` 收集 + 构建后自检（缺了就构建失败）。
"""

from __future__ import annotations

import importlib.util
import os
import pathlib

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "build-workbench.py"


def _module():
    spec = importlib.util.spec_from_file_location("build_workbench", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    os.environ["AGENT_MAILBOX_BUILD_ENGINES"] = "1"  # 本文件测"带引擎"老路 ✓（默认已关 ✓）
    spec.loader.exec_module(module)
    os.environ.pop("AGENT_MAILBOX_BUILD_ENGINES", None)
    return module


MACHO = b"\xcf\xfa\xed\xfe" + b"\x00" * 32


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / "deps"
    (root / "node_modules/pkg/bin").mkdir(parents=True)
    (root / "node_modules/pkg/lib").mkdir(parents=True)
    (root / "node_modules/pkg/bin/tool").write_bytes(MACHO)  # 真二进制
    (root / "node_modules/pkg/bin/script.js").write_text("#!/usr/bin/env node\n")  # 非二进制
    (root / "node_modules/pkg/lib/tool").write_bytes(MACHO)  # 不在 bin/ 下
    (root / "package.json").write_text("{}")
    return root


def test_binaries_are_collected_from_bin_directories(runtime):
    entries = _module().runtime_binaries(runtime)
    sources = [pathlib.Path(source).name for source, _dest in entries]
    assert sources == ["tool"], entries
    assert entries[0][1] == "runtime/deps/node_modules/pkg/bin"


def test_non_binaries_and_nested_locations_are_ignored(runtime):
    entries = _module().runtime_binaries(runtime)
    assert all("script.js" not in source for source, _ in entries)
    assert all("/lib/" not in source for source, _ in entries)


def test_missing_binary_fails_the_build(runtime, tmp_path):
    module = _module()
    entries = module.runtime_binaries(runtime)
    bundle = tmp_path / "Agent Mailbox.app/Contents/Resources"
    (bundle / entries[0][1]).mkdir(parents=True)
    (bundle / entries[0][1] / "tool").write_bytes(MACHO)  # 先放好：应通过
    module.verify_runtime_binaries(bundle, entries)

    (bundle / entries[0][1] / "tool").unlink()  # 再拿掉：应报错
    with pytest.raises(RuntimeError) as caught:
        module.verify_runtime_binaries(bundle, entries)
    assert "missing native runtime executables" in str(caught.value)
    assert "tool" in str(caught.value)


def test_empty_runtime_is_not_an_error(tmp_path):
    module = _module()
    assert module.runtime_binaries(tmp_path / "nothing") == []
    module.verify_runtime_binaries(tmp_path, [])


def test_desktop_shell_guard_requires_pyobjc(monkeypatch):
    """构建环境缺 pyobjc ⇒ 冻结包界面会跑进浏览器 —— 必须在构建时拦住。"""
    module = _module()
    monkeypatch.setattr(module.sys, "platform", "darwin")

    real_import = module.importlib.import_module

    def fake_import(name, *args, **kwargs):
        if name in {"objc", "AppKit", "WebKit"}:
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(module.importlib, "import_module", fake_import)
    with pytest.raises(RuntimeError) as caught:
        module.require_desktop_shell()
    assert "pyobjc" in str(caught.value)
    assert "objc" in str(caught.value)


def test_desktop_shell_guard_passes_when_available(monkeypatch):
    """导入可用时不得拦截（仓库 venv 本身没装 pyobjc，故这里模拟可用）。"""
    module = _module()
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module.importlib, "import_module", lambda name, *a, **k: object())
    module.require_desktop_shell()


def test_desktop_shell_guard_is_skipped_off_macos(monkeypatch):
    module = _module()
    monkeypatch.setattr(module.sys, "platform", "linux")

    def always_fail(name, *a, **k):
        raise ImportError(name)

    monkeypatch.setattr(module.importlib, "import_module", always_fail)
    module.require_desktop_shell()  # 非 darwin 不要求 pyobjc
