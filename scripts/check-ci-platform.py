"""Reject mixed-architecture CI runtimes and mislabeled native build manifests."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from pathlib import Path


def architecture(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    return {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64"}.get(value.lower(), value.lower())


def mismatches(evidence: dict, expected_platform: str, expected_arch: str) -> list[str]:
    errors = []
    for source in ("python", "node", "manifest"):
        if source not in evidence:
            continue
        actual = evidence[source]
        if actual.get("platform") != expected_platform:
            errors.append(f"{source}: platform {actual.get('platform')!r} != {expected_platform}")
        if architecture(actual.get("architecture", "")) != expected_arch:
            errors.append(
                f"{source}: architecture {actual.get('architecture')!r} != {expected_arch}"
            )
    runner_arch = evidence.get("runner_arch")
    if runner_arch and architecture(runner_arch) != expected_arch:
        errors.append(f"runner: architecture {runner_arch!r} != {expected_arch}")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, choices=("darwin", "win32", "linux"))
    parser.add_argument("--arch", required=True, choices=("arm64", "x64"))
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    node = json.loads(
        subprocess.check_output(
            [
                "node",
                "-p",
                "JSON.stringify({platform:process.platform,architecture:process.arch,version:process.version})",
            ],
            text=True,
            timeout=15,
        )
    )
    evidence = {
        "expected": {"platform": args.platform, "architecture": args.arch},
        "runner_arch": os.environ.get("RUNNER_ARCH"),
        "python": {
            "platform": sys.platform,
            "architecture": platform.machine(),
            "version": platform.python_version(),
        },
        "node": node,
    }
    if args.manifest:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        evidence["manifest"] = {
            key: manifest.get(key) for key in ("platform", "architecture", "version")
        }
    errors = mismatches(evidence, args.platform, args.arch)
    evidence["errors"] = errors
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
