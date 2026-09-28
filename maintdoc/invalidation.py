"""Invalidation of approvals and dependent review state when source data changes.

Used by inventory (file changed / missing), extraction (page re-extracted),
validation (fingerprint mismatch, new critical conflict) and review actions
(wording or classification edits). Every invalidation is written to both the
append-only ``review_decisions`` table and the hash-chained audit log.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Iterable

from maintdoc import audit
from maintdoc.constants import ConflictStatus, EvidenceStatus, ReviewStatus, VisualCheckStatus
from maintdoc.db import transaction
from maintdoc.errors import supersede_source_errors
from maintdoc.utils import dumps, now_iso

log = logging.getLogger(__name__)
SYSTEM = "system"


def record_decision(conn: sqlite3.Connection, entity_type: str, entity_id: str, decision: str, reviewer: str,
                    comment: str | None = None, fingerprint: str | None = None, details: dict | None = None,
                    before: dict | None = None, after: dict | None = None, run_id: str | None = None) -> int:
    """Append a review decision together with its audit entry (single transaction)."""
    with transaction(conn):
        audit_id = audit.append(conn, reviewer, f"review.{decision}", entity_type, entity_id,
                                before=before, after=after, reason=comment, run_id=run_id)
        cur = conn.execute(
            "INSERT INTO review_decisions(entity_type, entity_id, decision, reviewer, comment, fingerprint, "
            "details_json, created_at, audit_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (entity_type, entity_id, decision, reviewer, comment, fingerprint,
             dumps(details) if details else None, now_iso(), audit_id))
        return int(cur.lastrowid)


def invalidate_evidence_approvals(conn: sqlite3.Connection, evidence_ids: Iterable[str], reason: str,
                                  run_id: str | None = None) -> int:
    """Set approved evidence to 'invalidated'. Returns number invalidated."""
    ids = list(dict.fromkeys(evidence_ids))
    n = 0
    with transaction(conn):
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ",".join("?" * len(chunk))
            for r in conn.execute(f"SELECT evidence_id, review_status, reviewed_by FROM evidence "
                                  f"WHERE evidence_id IN ({q}) AND review_status=?",
                                  (*chunk, ReviewStatus.APPROVED)).fetchall():
                conn.execute("UPDATE evidence SET review_status=?, approval_fingerprint=NULL, review_comment=? "
                             "WHERE evidence_id=?", (ReviewStatus.INVALIDATED, reason, r["evidence_id"]))
                record_decision(conn, "evidence", r["evidence_id"], "invalidate", SYSTEM, reason,
                                before={"review_status": r["review_status"], "approved_by": r["reviewed_by"]},
                                after={"review_status": ReviewStatus.INVALIDATED}, run_id=run_id)
                n += 1
    if n:
        log.info("Invalidated %d approval(s): %s", n, reason)
    return n


def dependents_of(conn: sqlite3.Connection, evidence_ids: Iterable[str]) -> set[str]:
    """Canonical evidence items that cite any of ``evidence_ids`` as duplicates."""
    ids = list(evidence_ids)
    out: set[str] = set()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = ",".join("?" * len(chunk))
        for r in conn.execute(f"SELECT DISTINCT canonical_evidence_id FROM evidence WHERE evidence_id IN ({q}) "
                              f"AND canonical_evidence_id IS NOT NULL", chunk):
            out.add(r[0])
    return out


def supersede_evidence(conn: sqlite3.Connection, evidence_ids: list[str], reason: str,
                       run_id: str | None = None) -> dict[str, int]:
    """Mark evidence superseded and cascade to approvals, conflicts, duplicates and checks."""
    counts = {"evidence": 0, "approvals": 0, "conflicts": 0, "near_duplicates": 0, "visual_checks": 0}
    if not evidence_ids:
        return counts
    with transaction(conn):
        deps = dependents_of(conn, evidence_ids)
        for i in range(0, len(evidence_ids), 500):
            chunk = evidence_ids[i:i + 500]
            q = ",".join("?" * len(chunk))
            counts["evidence"] += conn.execute(
                f"UPDATE evidence SET status=?, status_reason=?, in_manual=0 WHERE evidence_id IN ({q}) "
                f"AND status != ?", (EvidenceStatus.SUPERSEDED, reason, *chunk, EvidenceStatus.SUPERSEDED)).rowcount
        counts["approvals"] = invalidate_evidence_approvals(conn, list(evidence_ids) + sorted(deps),
                                                            f"source data changed: {reason}", run_id)
        # conflicts referencing superseded evidence become stale
        stale_ids = set()
        for i in range(0, len(evidence_ids), 500):
            chunk = evidence_ids[i:i + 500]
            q = ",".join("?" * len(chunk))
            for r in conn.execute(f"SELECT DISTINCT conflict_id FROM conflict_evidence WHERE evidence_id IN ({q})",
                                  chunk):
                stale_ids.add(r[0])
        for cid in sorted(stale_ids):
            old = conn.execute("SELECT status FROM conflicts WHERE conflict_id=?", (cid,)).fetchone()
            if old and old["status"] != ConflictStatus.STALE:
                conn.execute("UPDATE conflicts SET status=? WHERE conflict_id=?", (ConflictStatus.STALE, cid))
                audit.append(conn, SYSTEM, "conflict.stale", "conflict", cid, before={"status": old["status"]},
                             after={"status": ConflictStatus.STALE}, reason=reason, run_id=run_id)
                counts["conflicts"] += 1
        for i in range(0, len(evidence_ids), 500):
            chunk = evidence_ids[i:i + 500]
            q = ",".join("?" * len(chunk))
            counts["near_duplicates"] += conn.execute(
                f"UPDATE near_duplicates SET status='stale' WHERE status!='stale' AND "
                f"(evidence_a IN ({q}) OR evidence_b IN ({q}))", (*chunk, *chunk)).rowcount
            counts["visual_checks"] += conn.execute(
                f"UPDATE visual_checks SET status=?, comment=COALESCE(comment,'') || ' [invalidated: ' || ? || ']' "
                f"WHERE evidence_id IN ({q}) AND status != ?",
                (VisualCheckStatus.INVALIDATED, reason, *chunk, VisualCheckStatus.INVALIDATED)).rowcount
    return counts


def supersede_source_versions(conn: sqlite3.Connection, source_id: str, keep_sha256: str | None, reason: str,
                              run_id: str | None = None) -> dict[str, int]:
    """Supersede all evidence of ``source_id`` whose content hash differs from ``keep_sha256``.

    ``keep_sha256=None`` supersedes everything (source missing).
    """
    with transaction(conn):
        ids = [r[0] for r in conn.execute(
            "SELECT evidence_id FROM evidence WHERE source_id=? AND status != ? AND source_sha256 IS NOT ?",
            (source_id, EvidenceStatus.SUPERSEDED, keep_sha256))]
        counts = supersede_evidence(conn, ids, reason, run_id)
        counts["visual_checks"] += conn.execute(
            "UPDATE visual_checks SET status=? WHERE source_id=? AND source_sha256 IS NOT ? AND status != ?",
            (VisualCheckStatus.INVALIDATED, source_id, keep_sha256, VisualCheckStatus.INVALIDATED)).rowcount
        counts["errors"] = supersede_source_errors(conn, source_id, keep_sha256,
                                                   stages=("text", "table", "ocr", "quality", "classification",
                                                           "units", "integrity", "metadata"))
        conn.execute("DELETE FROM extraction_checkpoints WHERE source_id=? AND source_sha256 IS NOT ?",
                     (source_id, keep_sha256))
        if any(counts.values()):
            audit.append(conn, SYSTEM, "source.superseded_dependents", "source", source_id,
                         after=counts, reason=reason, run_id=run_id)
    return counts
