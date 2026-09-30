"""Independent product entry; no legacy mailbox startup or global installation."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "prepare":
        parser = argparse.ArgumentParser(
            description="Prepare this home's locked execution runtime."
        )
        parser.add_argument("--home", type=Path, default=Path.home() / ".agent-mailbox")
        options = parser.parse_args(args[1:])
        from .runtime_bridge.install_runtime import install_runtime
        from .workbench_lock import WorkbenchLock
        from .workbench_runtime import runtime_status

        home = options.home.expanduser().resolve()
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        (home / "workbench").mkdir(exist_ok=True, mode=0o700)
        from .workbench_store import WorkbenchError

        try:
            with WorkbenchLock(home):
                install_runtime(home / "workbench/runtime/deps")
                print(json.dumps(runtime_status(home), ensure_ascii=False))
        except (
            WorkbenchError,
            OSError,
            ValueError,
            RuntimeError,
            subprocess.SubprocessError,
        ) as exc:
            code = exc.code if isinstance(exc, WorkbenchError) else "RUNTIME_INSTALL_FAILED"
            print(json.dumps({"error": {"code": code, "message": str(exc)}}), file=sys.stderr)
            raise SystemExit(1) from exc
        return
    if args and args[0] == "node":
        from .workbench_node import main as node_main

        raise SystemExit(node_main(args[1:]))
    if args and args[0] == "workbench":
        args.pop(0)
    if args == ["--version"]:
        from . import __version__

        print(__version__)
        return
    from .workbench import main as workbench_main

    workbench_main(args)


if __name__ == "__main__":
    main()
