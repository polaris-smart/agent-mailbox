"""Packaged desktop launcher and the frozen executable's stdio MCP entry point."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def bundled_runtime() -> dict[str, str] | None:
    """Point the execution component at read-only resources inside the app bundle."""
    if not getattr(sys, "frozen", False):
        return None
    bundle = Path(sys._MEIPASS)
    node = bundle / "runtime/bin/node"
    dependencies = bundle / "runtime/deps"
    if not node.is_file() or not (dependencies / "node_modules/acpx/package.json").is_file():
        raise RuntimeError("This application bundle is missing its managed execution runtime")
    os.environ["AGENT_MAIL_NODE_BIN"] = str(node)
    os.environ["AGENT_MAIL_RUNTIME_DIR"] = str(dependencies)
    return {"node_binary": str(node), "runtime_dir": str(dependencies)}


def main(argv: list[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    bundled_runtime()
    if arguments == ["--workspace-mcp"]:
        from agent_mailbox.workspace_mcp import main as workspace_main

        workspace_main()
    else:
        from agent_mailbox.cli import main as workbench_main

        workbench_main(arguments)


if __name__ == "__main__":
    main()
