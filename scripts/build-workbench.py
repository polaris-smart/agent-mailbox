#!/usr/bin/env python3
"""Build an isolated macOS .app from already installed, pinned local runtimes.

Run this script with an isolated Python environment containing the project and
PyInstaller. It does not install dependencies, fetch updates, sign for distribution,
or modify agent credentials/configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

PINNED = {
    "acpx": "0.19.3",
    "@openai/codex": "0.158.0",
    "@agentclientprotocol/codex-acp": "2.0.0",
    "@agentclientprotocol/claude-agent-acp": "0.84.0",
}


def validate_runtime(runtime_dir: Path, node: Path) -> str:
    version = subprocess.run([str(node), "--version"], capture_output=True, text=True, check=True)
    node_version = version.stdout.strip()
    if tuple(int(part) for part in node_version.removeprefix("v").split(".")) < (22, 13, 0):
        raise ValueError("Node.js >=22.13 is required")
    subprocess.run([str(node), "--check", str(SOURCE / "runtime_bridge/bridge.mjs")], check=True)
    for name, expected in PINNED.items():
        filename = runtime_dir / "node_modules" / name / "package.json"
        if not filename.is_file() or json.loads(filename.read_text())["version"] != expected:
            raise ValueError(f"Install pinned {name}@{expected} before building")
    for filename in ("package.json", "package-lock.json"):
        if not (runtime_dir / filename).is_file():
            raise ValueError(f"Runtime directory must contain {filename}")
        if (runtime_dir / filename).read_bytes() != (
            SOURCE / "runtime_bridge" / filename
        ).read_bytes():
            raise ValueError(
                f"Runtime {filename} differs from the checked-in lock; reinstall explicitly"
            )
    subprocess.run(
        [
            str(node),
            "--input-type=module",
            "-e",
            "const {resolveCodexPair}=await import(process.argv[1]);await resolveCodexPair(process.argv[2]);",
            (SOURCE / "runtime_bridge/codex_pair.mjs").as_uri(),
            str(runtime_dir),
        ],
        check=True,
    )
    return node_version


REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE = REPOSITORY / "src/agent_mailbox"


def build(runtime_dir: Path, node: Path, output: Path, name: str) -> dict:
    if sys.platform != "darwin":
        raise RuntimeError(
            "This build creates a macOS app; build Windows/Linux on their native hosts"
        )
    if importlib.util.find_spec("PyInstaller") is None:
        raise RuntimeError("Install PyInstaller in the selected isolated Python environment first")
    runtime_dir, node, output = runtime_dir.resolve(), node.resolve(), output.resolve()
    if output.is_relative_to(REPOSITORY) or output in REPOSITORY.parents:
        raise ValueError("Choose an isolated build output directory")
    node_version = validate_runtime(runtime_dir, node)
    output.mkdir(parents=True, exist_ok=True)
    from PyInstaller.building.makespec import main as make_spec

    version = importlib.metadata.version("agent-mailbox")
    match = re.fullmatch(r"(\d+\.\d+\.\d+)([ab]\d+)?", version)
    if not match:
        raise ValueError("The macOS bundle requires a release or alpha/beta version")
    short_version = match.group(1)
    info_plist = {
        "CFBundleShortVersionString": short_version,
        "CFBundleVersion": short_version,
        "CFBundleGetInfoString": f"Agent Mailbox {version}",
        "AgentMailboxVersion": version,
    }
    spec = Path(
        make_spec(
            [str(SOURCE / "workbench_app.py")],
            name=name,
            console=False,
            onefile=False,
            shorthand_manifest=None,
            bundle_identifier="com.polaris-smart.agent-mailbox",
            specpath=str(output),
            pathex=[str(REPOSITORY / "src")],
            copy_metadata=["agent-mailbox"],
            collect_submodules=["mcp.server"],
            datas=[
                (str(SOURCE / "runtime_bridge"), "agent_mailbox/runtime_bridge"),
                (str(SOURCE / "workbench_assets"), "agent_mailbox/workbench_assets"),
                (str(runtime_dir / "node_modules"), "runtime/deps/node_modules"),
                (str(runtime_dir / "package.json"), "runtime/deps"),
                (str(runtime_dir / "package-lock.json"), "runtime/deps"),
            ],
            binaries=[(str(node), "runtime/bin")],
        )
    )
    content = spec.read_text()
    if content.count("app = BUNDLE(\n") != 1:
        raise RuntimeError("PyInstaller generated an unexpected macOS bundle specification")
    spec.write_text(
        content.replace(
            "app = BUNDLE(\n",
            f"app = BUNDLE(\n    version={short_version!r},\n    info_plist={info_plist!r},\n",
        )
    )
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(output / "dist"),
        "--workpath",
        str(output / "build"),
        str(spec),
    ]
    environment = {**os.environ, "PYINSTALLER_CONFIG_DIR": str(output / "cache")}
    subprocess.run(command, cwd=REPOSITORY, env=environment, check=True)
    app = output / "dist" / f"{name}.app"
    manifest = {
        "app": str(app),
        "executable": str(app / "Contents/MacOS" / name),
        "architecture": platform.machine(),
        "version": version,
        "bundle_short_version": short_version,
        "bundle_version": short_version,
        "pyinstaller_version": importlib.metadata.version("pyinstaller"),
        "node_version": node_version,
        "runtime_dependencies": PINNED,
        "runtime_lock_sha256": hashlib.sha256(
            (runtime_dir / "package-lock.json").read_bytes()
        ).hexdigest(),
        "distribution_signed": False,
        "notarized": False,
        "automatic_updates": False,
    }
    (output / "build-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--node-binary", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--name", default="Agent Mailbox")
    args = parser.parse_args()
    if (
        not args.name
        or Path(args.name).name != args.name
        or any(c in args.name for c in ("/", "\\"))
    ):
        parser.error("--name must be a single application name")
    print(
        json.dumps(build(args.runtime_dir, args.node_binary, args.output_dir, args.name), indent=2)
    )


if __name__ == "__main__":
    main()
