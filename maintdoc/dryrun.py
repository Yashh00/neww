"""Dry-run support: run a stage against a throw-away copy of the database and output folder."""

from __future__ import annotations

import contextlib
import shutil
import tempfile
from pathlib import Path
from typing import Iterator

from maintdoc.config import Config
from maintdoc.db import backup_to, connect


@contextlib.contextmanager
def scratch_workspace(cfg: Config) -> Iterator[Config]:
    """Yield a Config whose database/output/work folders point to a temporary copy."""
    tmp = Path(tempfile.mkdtemp(prefix="maintdoc_dryrun_"))
    try:
        db_copy = tmp / "maintenance.db"
        if cfg.db_path.exists():
            src = connect(cfg.db_path, readonly=True)
            try:
                backup_to(src, db_copy)
            finally:
                src.close()
        scratch = cfg.with_overrides({"paths": {"database": str(db_copy), "output_dir": str(tmp / "output"),
                                                "work_dir": str(tmp / "work")}})
        yield scratch
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
