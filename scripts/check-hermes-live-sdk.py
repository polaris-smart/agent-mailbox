"""Run metadata contracts in an isolated installed SDK, with no Gateway boot.

Requires explicit local Hermes interpreter/source. Dependency preparation is
stubbed in this test process only; no downloads or installed host changes.
Ordinary pytest skips these SDK contracts. This gate fails on *any* skip.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

CHILD = r'''
import json, runpy, sys, unittest
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
with (patch("hermes_cli.venv_sync.prepare_launch", return_value=None),
      patch("hermes_cli._early_recovery.recover_if_needed"),
      patch("pm.environments.activate_dependencies")):
    import hermes_bootstrap
import gateway.run
namespace = runpy.run_path(sys.argv[2], run_name="hermes_live_sdk_contract")
suite = unittest.defaultTestLoader.loadTestsFromTestCase(namespace["LiveTest"])
result = unittest.TextTestRunner(verbosity=2).run(suite)
summary = {"tests": result.testsRun, "skipped": len(result.skipped),
           "errors": len(result.errors), "failures": len(result.failures),
           "real_gateway_started": False}
print(json.dumps(summary))
sys.exit(0 if result.wasSuccessful() and not result.skipped and result.testsRun else 1)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    args = parser.parse_args()
    if not args.python.is_absolute() or not args.source.is_absolute():
        parser.error("Use explicit absolute local paths.")
    test = Path(__file__).resolve().parents[1] / "tests/test_hermes_live.py"
    with tempfile.TemporaryDirectory(prefix="mailbox-hermes-sdk-") as temporary:
        temporary = str(Path(temporary).resolve())
        try:
            result = subprocess.run(
                [str(args.python), "-I", "-B", "-c", CHILD, str(args.source), str(test)],
                cwd=temporary, env={"PATH": "/usr/bin:/bin", "HOME": temporary,
                                   "HERMES_HOME": temporary, "LANG": "en_US.UTF-8"},
                timeout=45, check=False,
            )
        except subprocess.TimeoutExpired:
            print(json.dumps({"error": "isolated_sdk_timeout", "validated": False}))
            return 1
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
