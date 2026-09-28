"""Extraction / validation error register helpers.

Errors are keyed by a fingerprint so that re-running a stage does not create
duplicates and reviewer status (acknowledged / resolved) is preserved.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from maintdoc.constants import ERROR_CODES, ErrorStatus, Severity
from maintdoc.utils import dumps, now_iso, sha256_text

log = logging.getLogger(__name__)


def error_fingerprint(code: str, source_id: str | None, source_sha256: str | None, page_no: int | None,
                      evidence_id: str | None, key: str | None = None) -> str:
    return sha256_text("|".join(str(x) for x in (code, source_id, source_sha256, page_no, evidence_id, key)))


def record_error(conn: sqlite3.Connection, code: str, message: str, *, stage: str,
                 source_id: str | None = None, source_sha256: str | None = None,
                 page_no: int | None = None, evidence_id: str | None = None,
                 severity: str | None = None, details: dict[str, Any] | None = None,
                 run_id: str | None = None, key: str | None = None) -> None:
    """Insert or refresh an error. Caller controls the transaction."""
    if code not in ERROR_CODES:
        log.debug("Unregistered error code %s", code)
    sev = severity or ERROR_CODES.get(code, (Severity.ERROR, ""))[0]
    fp = error_fingerprint(code, source_id, source_sha256, page_no, evidence_id, key)
    ts = now_iso()
    conn.execute(
        """INSERT INTO extraction_errors(fingerprint, source_id, source_sha256, page_no, evidence_id, stage,
               error_code, severity, message, details_json, status, created_at, last_seen_at, run_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(fingerprint) DO UPDATE SET last_seen_at=excluded.last_seen_at,
               message=excluded.message, details_json=excluded.details_json, run_id=excluded.run_id,
               status=CASE WHEN extraction_errors.status='superseded' THEN 'open' ELSE extraction_errors.status END""",
        (fp, source_id, source_sha256, page_no, evidence_id, stage, code, sev, message,
         dumps(details) if details else None, ErrorStatus.OPEN, ts, ts, run_id),
    )


def supersede_source_errors(conn: sqlite3.Connection, source_id: str, keep_sha256: str | None,
                            stages: tuple[str, ...] | None = None) -> int:
    """Mark errors that refer to an older content version of a source as superseded."""
    sql = ("UPDATE extraction_errors SET status='superseded', status_by='system', status_at=? "
           "WHERE source_id=? AND status NOT IN ('superseded') AND (source_sha256 IS NOT ? )")
    params: list[Any] = [now_iso(), source_id, keep_sha256]
    if stages:
        sql += f" AND stage IN ({','.join('?' * len(stages))})"
        params.extend(stages)
    return conn.execute(sql, params).rowcount


def auto_resolve(conn: sqlite3.Connection, code: str, keep_fingerprints: set[str], comment: str) -> int:
    """Resolve open errors of ``code`` that were not re-detected in the current pass."""
    n = 0
    for r in conn.execute("SELECT error_id, fingerprint FROM extraction_errors WHERE error_code=? AND status='open'",
                          (code,)).fetchall():
        if r["fingerprint"] not in keep_fingerprints:
            conn.execute("UPDATE extraction_errors SET status='resolved', status_by='system', status_at=?, "
                         "status_comment=? WHERE error_id=?", (now_iso(), comment, r["error_id"]))
            n += 1
    return n
