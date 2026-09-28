"""Dependency-free console progress indicator with ETA."""

from __future__ import annotations

import sys
import time


class Progress:
    def __init__(self, total: int, desc: str = "", stream=None, enabled: bool = True, min_interval: float = 0.5):
        self.total = max(0, int(total))
        self.desc = desc
        self.done = 0
        self.stream = stream or sys.stderr
        self.enabled = enabled
        self.start = time.monotonic()
        self._last = 0.0
        self.min_interval = min_interval
        self.is_tty = hasattr(self.stream, "isatty") and self.stream.isatty()

    def update(self, n: int = 1, note: str = "") -> None:
        self.done += n
        now = time.monotonic()
        if not self.enabled:
            return
        if self.is_tty and now - self._last < self.min_interval and self.done < self.total:
            return
        self._last = now
        self._render(note)

    def _render(self, note: str) -> None:
        elapsed = time.monotonic() - self.start
        pct = (100.0 * self.done / self.total) if self.total else 100.0
        eta = ""
        if self.done and self.total and self.done < self.total:
            remaining = elapsed / self.done * (self.total - self.done)
            eta = f" ETA {_fmt(remaining)}"
        width = 24
        filled = int(width * pct / 100)
        bar = "#" * filled + "-" * (width - filled)
        line = f"{self.desc} [{bar}] {self.done}/{self.total} {pct:5.1f}% {_fmt(elapsed)}{eta} {note}"
        if self.is_tty:
            self.stream.write("\r" + line[:160].ljust(160))
        else:
            self.stream.write(line + "\n")
        self.stream.flush()

    def close(self) -> None:
        if self.enabled and self.is_tty:
            self.stream.write("\n")
            self.stream.flush()


def _fmt(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
