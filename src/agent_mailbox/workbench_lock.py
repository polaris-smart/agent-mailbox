"""Keep one execution owner per data directory across application launches."""

import os
from pathlib import Path

from .workbench_store import WorkbenchError


class WorkbenchLock:
    def __init__(self, root: Path):
        path = root / "workbench/instance.lock"
        self.file = path.open("a+b")
        path.chmod(0o600)
        try:
            if os.name == "nt":
                import msvcrt

                self.file.seek(0)
                self.file.write(b"0")
                self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise WorkbenchError(
                "ALREADY_RUNNING", "This data directory already has a running workbench."
            ) from exc

    def close(self):
        if not self.file.closed:
            self.file.close()
