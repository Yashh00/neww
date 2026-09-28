"""Append-only, hash-chained audit history.

Each entry stores the SHA-256 of the previous entry, so any modification of
history is detectable by :func:`verify_chain`. SQLite triggers additionally
reject UPDATE and DELETE statements on ``audit_log``.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from maintdoc.db import transaction
from maintdoc.utils import dumps, now_iso

GENESIS = "0" * 64


def _entry_hash(prev_hash: str, fields: dict[str, Any]) -> str:
    payload = prev_hash + "|" + dumps(fields)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def append(conn: sqlite3.Connection, actor: str, action: str, entity_type: str | None = None,
           entity_id: str | None = None, before: Any = None, after: Any = None,
           reason: str | None = None, run_id: str | None = None) -> int:
    """Append an audit entry. Joins the caller's transaction if one is open."""
    with transaction(conn):
        prev = conn.execute("SELECT entry_hash FROM audit_log ORDER BY audit_id DESC LIMIT 1").fetchone()
        prev_hash = prev[0] if prev else GENESIS
        fields = {
            "ts": now_iso(),
            "actor": actor or "system",
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "before_json": dumps(before) if before is not None else None,
            "after_json": dumps(after) if after is not None else None,
            "reason": reason,
            "run_id": run_id,
        }
        h = _entry_hash(prev_hash, fields)
        cur = conn.execute(
            "INSERT INTO audit_log(ts, actor, action, entity_type, entity_id, before_json, after_json, "
            "reason, run_id, prev_hash, entry_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (fields["ts"], fields["actor"], action, entity_type, entity_id, fields["before_json"],
             fields["after_json"], reason, run_id, prev_hash, h),
        )
        return int(cur.lastrowid)


def verify_chain(conn: sqlite3.Connection) -> tuple[bool, int | None, int]:
    """Return (ok, first_bad_audit_id, entries_checked)."""
    prev_hash = GENESIS
    count = 0
    for r in conn.execute("SELECT * FROM audit_log ORDER BY audit_id"):
        count += 1
        fields = {k: r[k] for k in ("ts", "actor", "action", "entity_type", "entity_id",
                                    "before_json", "after_json", "reason", "run_id")}
        if r["prev_hash"] != prev_hash or _entry_hash(prev_hash, fields) != r["entry_hash"]:
            return False, int(r["audit_id"]), count
        prev_hash = r["entry_hash"]
    return True, None, count
