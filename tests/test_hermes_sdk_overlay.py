"""A private Hermes overlay must stay process-local and refuse mixed host bytes."""

import hashlib
import importlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Hermes overlay is Unix-only")

BOOT = (
    Path(__file__).resolve().parents[1] / "integrations/hermes-private-overlay/host_overlay_boot.py"
)
PRODUCT = BOOT.parents[2]
spec = importlib.util.spec_from_file_location("test_hermes_overlay_boot", BOOT)
overlay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(overlay)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def test_bundled_overlay_matches_product_private_sdk_pin():
    pin_path = PRODUCT / "integrations/hermes-mailbox-host/pinned.py"
    pin_spec = importlib.util.spec_from_file_location("overlay_product_pin", pin_path)
    pins = importlib.util.module_from_spec(pin_spec)
    pin_spec.loader.exec_module(pins)
    bundle = BOOT.parent
    rows = json.loads((bundle / "manifest.json").read_text())["modules"]
    assert {row["module"]: row["patched_sha256"] for row in rows} == pins.PRIVATE_SUPPORTED
    assert digest((bundle / "manifest.json").read_bytes()) == overlay.MANIFEST_SHA256


def test_private_turn_cannot_request_human_platform_reply():
    """A mailbox turn cannot relabel a nested Feishu injection as a Human reply."""
    script = """
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

root = Path(sys.argv[1])
gateway = types.ModuleType("gateway")
gateway.__path__ = []
platforms = types.ModuleType("gateway.platforms")
platforms.__path__ = []
events = types.ModuleType("gateway.platforms.event")
events.MessageEvent = type("MessageEvent", (), {})
events.MessageType = type("MessageType", (), {})
session = types.ModuleType("gateway.session")
session.SessionSource = type("SessionSource", (), {})
sys.modules.update({"gateway": gateway, "gateway.platforms": platforms,
                    "gateway.platforms.event": events, "gateway.session": session})

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

policy = load("gateway.reply_policy", root / "gateway/reply_policy.py")
injection = load("gateway.run_plugin_injection", root / "gateway/run_plugin_injection.py")
feishu = SimpleNamespace(platform=SimpleNamespace(value="feishu"))
private = SimpleNamespace(platform=SimpleNamespace(value="agent_mailbox_private"))
assert injection._reply_policy_supported(feishu, "platform", plugin_id="other")
assert injection._reply_policy_supported(private, "mailbox_only", plugin_id="agent-mailbox-host")
assert not injection._reply_policy_supported(feishu, "platform", plugin_id="agent-mailbox-host")
assert not injection._reply_policy_supported(private, "mailbox_only", plugin_id="other")
with policy.bind_event_reply_policy(SimpleNamespace(_gateway_reply_policy="mailbox_only")):
    assert not injection._reply_policy_supported(feishu, "platform", plugin_id="agent-mailbox-host")
    assert not injection._reply_policy_supported(private, "mailbox_only", plugin_id="agent-mailbox-host")
assert injection._reply_policy_supported(feishu, "platform", plugin_id="other")
"""
    patch_dir = BOOT.parent / "patched"
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script, str(patch_dir)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.fixture
def synthetic(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    host = root / "host"
    bundle = root / "bundle"
    host.mkdir()
    bundle.mkdir()
    pkg = "mailboxoverlaytestpkg"
    (host / pkg).mkdir()
    (host / pkg / "__init__.py").write_text("")
    rows = []
    for number in range(12):
        name = f"{pkg}.part{number:02d}"
        relative = f"{pkg}/part{number:02d}.py"
        old = f"VALUE = 'old-{number}'\n".encode()
        new = f"VALUE = 'patched-{number}'\n".encode()
        if number < 11:
            (host / relative).write_bytes(old)
        target = bundle / "patched" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(new)
        rows.append(
            {
                "module": name,
                "relative_path": relative,
                "old_sha256": digest(old) if number < 11 else None,
                "patched_sha256": digest(new),
                "bytes": len(new),
            }
        )
    license_bytes = b"synthetic MIT test license\n"
    (bundle / "LICENSE").write_bytes(license_bytes)
    manifest = {
        "format": "agent-mailbox-hermes-private-host-candidate-v1",
        "upstream_license": "MIT",
        "modules": rows,
    }
    raw = (json.dumps(manifest, indent=2) + "\n").encode()
    (bundle / "manifest.json").write_bytes(raw)
    monkeypatch.setattr(overlay, "MANIFEST_SHA256", digest(raw))
    monkeypatch.setattr(overlay, "LICENSE_SHA256", digest(license_bytes))
    return host, bundle, pkg


def test_overlay_imports_patched_module_without_changing_original(synthetic):
    host, bundle, pkg = synthetic
    before = (host / pkg / "part00.py").read_bytes()
    original_path, original_meta = list(sys.path), list(sys.meta_path)
    original_bytecode = sys.dont_write_bytecode
    try:
        assert len(overlay.install(host, bundle)) == 12
        module = importlib.import_module(f"{pkg}.part00")
        assert module.VALUE == "patched-0"
        assert (host / pkg / "part00.py").read_bytes() == before
        assert not (host / pkg / "part11.py").exists()
    finally:
        sys.path[:] = original_path
        sys.meta_path[:] = original_meta
        sys.dont_write_bytecode = original_bytecode
        for name in list(sys.modules):
            if name == pkg or name.startswith(pkg + "."):
                sys.modules.pop(name, None)


def test_old_or_patch_byte_change_fails_before_import(synthetic):
    host, bundle, pkg = synthetic
    old = host / pkg / "part00.py"
    original = old.read_bytes()
    old.write_bytes(original + b"# unexpected edit\n")
    with pytest.raises(ValueError, match="unsupported_hermes_dispatch_version"):
        overlay.verify(host, bundle)
    old.write_bytes(original)
    patch = bundle / "patched" / pkg / "part00.py"
    patch.write_bytes(patch.read_bytes() + b"# unexpected edit\n")
    with pytest.raises(ValueError, match="host_patch_file_changed"):
        overlay.verify(host, bundle)


def test_symlinked_patch_parent_is_rejected(synthetic):
    host, bundle, pkg = synthetic
    directory = bundle / "patched" / pkg
    moved = bundle / "actual"
    directory.rename(moved)
    try:
        directory.symlink_to(moved, target_is_directory=True)
    except OSError:
        shutil.move(str(moved), str(directory))
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ValueError, match="unsafe_host_patch_file"):
        overlay.verify(host, bundle)
