"""An extracted App must retain the legal notices promised by its build manifest."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check-package.py"


def _module():
    spec = importlib.util.spec_from_file_location("check_package_notices", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_extracted_package_requires_matching_notice_inventory_and_files(tmp_path):
    module = _module()
    app = tmp_path / "Agent Mailbox.app"
    notices = app / "Contents/Resources/licenses/python-dependencies"
    license_file = notices / "synthetic/0/LICENSE"
    license_file.parent.mkdir(parents=True)
    license_file.write_text("Synthetic license text\n", encoding="utf-8")
    inventory = [{"name": "synthetic", "version": "1.0", "notices": ["synthetic/0/LICENSE"]}]
    (notices / "inventory.json").write_text(json.dumps(inventory), encoding="utf-8")
    manifest = {"platform": "darwin", "python_dependency_notice_inventory": inventory}

    assert module._verify_package_notices(app, manifest) == 1
    license_file.unlink()
    with pytest.raises(RuntimeError, match="notice file missing"):
        module._verify_package_notices(app, manifest)
    license_file.write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * 16)
    with pytest.raises(RuntimeError, match="binary notice"):
        module._verify_package_notices(app, manifest)
    license_file.write_text("Synthetic license text\n", encoding="utf-8")
    (notices / "inventory.json").write_text("[]", encoding="utf-8")
    with pytest.raises(RuntimeError, match="inventory mismatch"):
        module._verify_package_notices(app, manifest)
