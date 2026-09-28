"""Export all registers, reports and a consistent database snapshot to the output folder."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from maintdoc import audit
from maintdoc.config import Config
from maintdoc.db import backup_to, transaction
from maintdoc.reporting.summary_pdf import write_summary_pdf
from maintdoc.reporting.xlsx import (export_conflict_register, export_error_register, export_evidence_register,
                                     export_source_register, export_validation_report)
from maintdoc.utils import now_iso, sha256_file

log = logging.getLogger(__name__)

FILES = {
    "source_register": "Source_Register.xlsx",
    "evidence_register": "Evidence_Register.xlsx",
    "conflict_register": "Conflict_Register.xlsx",
    "error_register": "Extraction_Error_Register.xlsx",
    "validation_report": "Validation_Report.xlsx",
    "summary_pdf": "Processing_Summary.pdf",
    "database": "maintenance.db",
}


def run_export(conn: sqlite3.Connection, cfg: Config, run_id: str, report=None,
               include: tuple[str, ...] | None = None) -> dict[str, Any]:
    out = cfg.output_dir
    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    jobs = [
        ("source_register", lambda p: export_source_register(conn, cfg, p)),
        ("evidence_register", lambda p: export_evidence_register(conn, cfg, p)),
        ("conflict_register", lambda p: export_conflict_register(conn, cfg, p)),
        ("error_register", lambda p: export_error_register(conn, cfg, p)),
    ]
    if report is not None:
        jobs.append(("validation_report", lambda p: export_validation_report(report, cfg, p)))
    jobs.append(("summary_pdf", lambda p: write_summary_pdf(conn, cfg, p, report)))
    for key, fn in jobs:
        if include and key not in include:
            continue
        path = out / FILES[key]
        log.info("export: %s", path.name)
        fn(path)
        written[key] = str(path)
    db_target = out / FILES["database"]
    copy_db = (cfg.get("export.copy_database_to_output", True) and (not include or "database" in include)
               and db_target.resolve() != cfg.db_path.resolve())
    with transaction(conn):
        for key, p in written.items():
            conn.execute("INSERT INTO generated_outputs(kind, mode, path, sha256, status, created_at, run_id) "
                         "VALUES(?,?,?,?,?,?,?)", (key, None, p, sha256_file(Path(p)), "generated", now_iso(), run_id))
        if copy_db:
            conn.execute("INSERT INTO generated_outputs(kind, mode, path, sha256, status, created_at, run_id) "
                         "VALUES(?,?,?,?,?,?,?)", ("database", None, str(db_target), None, "generated", now_iso(),
                                                   run_id))
        audit.append(conn, "system", "export.completed", "export", run_id,
                     after={**written, **({"database": str(db_target)} if copy_db else {})}, run_id=run_id)
    if copy_db:  # snapshot last, so it contains the export records and audit entry
        backup_to(conn, db_target)
        written["database"] = str(db_target)
    return written
