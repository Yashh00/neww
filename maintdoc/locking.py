"""Single-writer run lock so two pipeline runs never process the same workspace."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


class LockError(RuntimeError):
    pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RunLock:
    def __init__(self, path: Path, command: str):
        self.path = path
        self.command = command
        self.acquired = False

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    info = json.loads(self.path.read_text(encoding="utf-8") or "{}")
                except (OSError, ValueError):
                    info = {}
                if _pid_alive(int(info.get("pid", -1))):
                    raise LockError(
                        f"Another maintdoc run ({info.get('command')}, pid {info.get('pid')}) holds {self.path}. "
                        "Wait for it to finish or delete the lock file if that process no longer exists.")
                try:
                    self.path.unlink()  # stale lock
                except OSError:
                    pass
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"pid": os.getpid(), "command": self.command, "since": time.time()}, fh)
            self.acquired = True
            return self
        raise LockError(f"Could not acquire lock {self.path}")

    def __exit__(self, *exc) -> None:
        if self.acquired:
            try:
                self.path.unlink()
            except OSError:
                pass
