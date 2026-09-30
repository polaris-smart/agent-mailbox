"""Explicit installation of pinned runtime dependencies into a chosen directory."""

import argparse
import json
import shutil
import subprocess
from pathlib import Path


def install_runtime(destination: Path, node_binary: str | None = None) -> dict:
    """Install dependencies without changing global packages, agent logins or MCP config."""
    node = node_binary or shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        raise RuntimeError("Node.js >=22.13 and npm are required to install the managed runtime")
    version = subprocess.run(
        [node, "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if tuple(int(part) for part in version.removeprefix("v").split(".")) < (22, 13, 0):
        raise RuntimeError("Node.js >=22.13 is required to install the managed runtime")
    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    assets = Path(__file__).parent
    for name in ("package.json", "package-lock.json"):
        target = destination / name
        if target != (assets / name).resolve():
            shutil.copyfile(assets / name, target)
    subprocess.run(
        [npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=destination,
        check=True,
    )
    return {
        "runtime_dir": str(destination),
        "node_binary": str(Path(node).resolve()),
        "acpx_version": "0.19.3",
        "node_version": version,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--node-binary")
    args = parser.parse_args()
    print(json.dumps(install_runtime(args.destination, args.node_binary)))


if __name__ == "__main__":
    main()
