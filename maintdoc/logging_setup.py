"""Logging configuration: rotating log file + console, multiprocess-safe via a queue."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s [%(processName)s] %(name)s: %(message)s"


def setup_logging(log_dir: Path, level: str = "INFO", file_name: str = "maintdoc.log",
                  max_bytes: int = 10_000_000, backup_count: int = 5, console: bool = True) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / file_name
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(logging.DEBUG)

    fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count,
                                              encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(fh)

    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(getattr(logging, str(level).upper(), logging.INFO))
        ch.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
        root.addHandler(ch)

    for noisy in ("pdfminer", "PIL", "pint", "matplotlib", "streamlit", "fontTools"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log_file


def worker_logging(queue) -> None:
    """Initializer for worker processes: forward all records to the main process."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(logging.DEBUG)
    if queue is not None:
        root.addHandler(logging.handlers.QueueHandler(queue))
    for noisy in ("pdfminer", "PIL", "pint", "fontTools"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def start_queue_listener(queue) -> logging.handlers.QueueListener:
    listener = logging.handlers.QueueListener(queue, *logging.getLogger().handlers, respect_handler_level=True)
    listener.start()
    return listener
