"""Review actions. The Streamlit UI and tests call only these functions.

Every action:
  * requires a named reviewer (config ``review.require_reviewer_name``),
  * validates its preconditions (approval blockers, numeric identity, ...),
  * changes state in one transaction together with an append-only review
    decision and a hash-chained audit entry (before/after values).

Nothing here rewrites technical content automatically. Reviewer wording edits
are stored separately from the immutable original text and must preserve
every number and unit of the source.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Iterable

from maintdoc.analysis.dedupe import guard_flags
from maintdoc.approval import approval_blockers, fingerprint, numeric_identity
from maintdoc.config import Config
from maintdoc.constants import (UNCLASSIFIED_CHAPTER, BlockType, ConflictStatus, ErrorStatus, EvidenceStatus,
                                ReviewStatus, VisualCheckStatus)
from maintdoc.db import row, rows, transaction
from maintdoc.invalidation import invalidate_evidence_approvals, record_decision
from maintdoc.utils import dumps, loads, now_iso


class ReviewError(Exception):
    pass


class ApprovalBlocked(ReviewError):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def _reviewer(cfg: Config, reviewer: str | None) -> str:
    name = (reviewer or "").strip()
    if not name and cfg.get("review.require_reviewer_name", True):
        raise ReviewError("A reviewer name is required for every review decision")
    if name.lower() == "system":
        raise ReviewError("'system' is reserved for automatic actions")
    return name or "anonymous"


def _need_comment(comment: str | None, what: str) -> str:
    c = (comment or "").strip()
    if not c:
        raise ReviewError(f"A comment/justification is required to {what}")
    return c


def _evidence(conn: sqlite3.Connection, evidence_id: str) -> dict:
    e = row(conn, "SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,))
    if e is None:
        raise ReviewError(f"Unknown evidence {evidence_id}")
    return e


def _review_snapshot(e: dict) -> dict:
    return {k: e.get(k) for k in ("review_status", "display_text", "chapter_override", "reviewed_by",
                                  "ocr_verified", "approval_fingerprint")}


# --------------------------------------------------------------------------- statements
def approve_evidence(conn: sqlite3.Connection, cfg: Config, evidence_id: str, reviewer: str,
                     comment: str | None = None) -> str:
    who = _reviewer(cfg, reviewer)
    with transaction(conn):
        blockers = approval_blockers(conn, cfg, evidence_id)
        if blockers:
            raise ApprovalBlocked(blockers)
        e = _evidence(conn, evidence_id)
        fp = fingerprint(conn, e)
        ts = now_iso()
        conn.execute("UPDATE evidence SET review_status=?, reviewed_by=?, reviewed_at=?, review_comment=?, "
                     "approval_fingerprint=? WHERE evidence_id=?",
                     (ReviewStatus.APPROVED, who, ts, comment, fp, evidence_id))
        record_decision(conn, "evidence", evidence_id, "approve", who, comment, fingerprint=fp,
                        details={"cited": [c for c in _cited_ids(conn, evidence_id)]},
                        before=_review_snapshot(e), after={"review_status": ReviewStatus.APPROVED,
                                                           "approval_fingerprint": fp})
    return fp


def _cited_ids(conn: sqlite3.Connection, evidence_id: str) -> list[str]:
    return [r[0] for r in conn.execute("SELECT evidence_id FROM evidence WHERE evidence_id=? OR canonical_evidence_id=? "
                                       "ORDER BY source_id, page_no, seq", (evidence_id, evidence_id))]


def bulk_approve(conn: sqlite3.Connection, cfg: Config, evidence_ids: Iterable[str], reviewer: str,
                 comment: str) -> dict[str, list]:
    """Approve several items; each one is checked individually. Returns approved/blocked lists."""
    _need_comment(comment, "bulk-approve")
    out: dict[str, list] = {"approved": [], "blocked": []}
    for eid in evidence_ids:
        try:
            approve_evidence(conn, cfg, eid, reviewer, comment)
            out["approved"].append(eid)
        except ApprovalBlocked as exc:
            out["blocked"].append((eid, exc.reasons))
    return out


def _set_status(conn: sqlite3.Connection, cfg: Config, evidence_id: str, status: str, reviewer: str,
                comment: str | None, decision: str) -> None:
    who = _reviewer(cfg, reviewer)
    with transaction(conn):
        e = _evidence(conn, evidence_id)
        conn.execute("UPDATE evidence SET review_status=?, reviewed_by=?, reviewed_at=?, review_comment=?, "
                     "approval_fingerprint=NULL WHERE evidence_id=?", (status, who, now_iso(), comment, evidence_id))
        record_decision(conn, "evidence", evidence_id, decision, who, comment, before=_review_snapshot(e),
                        after={"review_status": status})


def reject_evidence(conn: sqlite3.Connection, cfg: Config, evidence_id: str, reviewer: str, comment: str) -> None:
    if cfg.get("review.require_comment_for_rejection", True):
        _need_comment(comment, "reject a statement")
    _set_status(conn, cfg, evidence_id, ReviewStatus.REJECTED, reviewer, comment, "reject")


def request_revision(conn: sqlite3.Connection, cfg: Config, evidence_id: str, reviewer: str, comment: str) -> None:
    _need_comment(comment, "request a revision")
    _set_status(conn, cfg, evidence_id, ReviewStatus.NEEDS_REVISION, reviewer, comment, "needs_revision")


def reopen_evidence(conn: sqlite3.Connection, cfg: Config, evidence_id: str, reviewer: str, comment: str) -> None:
    _need_comment(comment, "reopen a statement")
    _set_status(conn, cfg, evidence_id, ReviewStatus.UNREVIEWED, reviewer, comment, "reopen")


_SIGNAL = re.compile(r"\b(DANGER|WARNING|CAUTION|NOTICE|NOTE|IMPORTANT|GEFAHR|WARNUNG|VORSICHT|ACHTUNG|HINWEIS)\b")


def edit_wording(conn: sqlite3.Connection, cfg: Config, evidence_id: str, new_text: str | None, reviewer: str,
                 reason: str) -> None:
    """Store reviewer wording. The original extracted text is never modified.

    Rejected when numbers/units differ from the source or a safety signal word is lost.
    Any existing approval is invalidated (the approved wording changed).
    """
    who = _reviewer(cfg, reviewer)
    reason = _need_comment(reason, "edit wording")
    with transaction(conn):
        e = _evidence(conn, evidence_id)
        text = (new_text or "").strip()
        if not text or text == e["text"].strip():
            text_to_store = None
        else:
            problems = numeric_identity(e["text"], text, cfg)
            if problems:
                raise ReviewError("Wording change rejected - numeric identity violated: " + "; ".join(problems))
            if e["block_type"] in BlockType.ADMONITIONS or _SIGNAL.search(e["text"]):
                lost = set(_SIGNAL.findall(e["text"])) - set(_SIGNAL.findall(text))
                if lost:
                    raise ReviewError(f"Wording change rejected - safety signal word(s) removed: {sorted(lost)}")
            text_to_store = text
        was_approved = e["review_status"] == ReviewStatus.APPROVED
        new_status = ReviewStatus.UNREVIEWED if was_approved else e["review_status"]
        conn.execute("UPDATE evidence SET display_text=?, edit_reason=?, review_status=?, approval_fingerprint=CASE "
                     "WHEN ?=1 THEN NULL ELSE approval_fingerprint END WHERE evidence_id=?",
                     (text_to_store, reason, new_status, int(was_approved), evidence_id))
        record_decision(conn, "evidence", evidence_id, "edit_wording", who, reason,
                        before={"display_text": e["display_text"], "review_status": e["review_status"]},
                        after={"display_text": text_to_store, "review_status": new_status,
                               "approval_invalidated": was_approved})


def set_ocr_verified(conn: sqlite3.Connection, cfg: Config, evidence_id: str, verified: bool, reviewer: str,
                     comment: str | None = None) -> None:
    who = _reviewer(cfg, reviewer)
    with transaction(conn):
        e = _evidence(conn, evidence_id)
        conn.execute("UPDATE evidence SET ocr_verified=? WHERE evidence_id=?", (int(verified), evidence_id))
        if not verified and e["review_status"] == ReviewStatus.APPROVED:
            invalidate_evidence_approvals(conn, [evidence_id], "OCR verification withdrawn")
        record_decision(conn, "evidence", evidence_id, "ocr_verified" if verified else "ocr_unverified", who, comment,
                        before={"ocr_verified": e["ocr_verified"]}, after={"ocr_verified": int(verified)})


def reclassify(conn: sqlite3.Connection, cfg: Config, evidence_id: str, chapter: str | None, reviewer: str,
               reason: str) -> None:
    who = _reviewer(cfg, reviewer)
    reason = _need_comment(reason, "reclassify")
    valid = {c["id"] for c in cfg.chapters()} | {UNCLASSIFIED_CHAPTER}
    if chapter is not None and chapter not in valid:
        raise ReviewError(f"Unknown chapter '{chapter}'")
    with transaction(conn):
        e = _evidence(conn, evidence_id)
        was_approved = e["review_status"] == ReviewStatus.APPROVED
        conn.execute("UPDATE evidence SET chapter_override=?, classification_status=? WHERE evidence_id=?",
                     (chapter, "manual" if chapter else "pending", evidence_id))
        if was_approved:
            invalidate_evidence_approvals(conn, [evidence_id], "chapter changed by reviewer")
        if chapter and chapter != UNCLASSIFIED_CHAPTER:
            conn.execute("UPDATE extraction_errors SET status='resolved', status_by=?, status_at=?, status_comment=? "
                         "WHERE evidence_id=? AND error_code IN ('UNCLASSIFIED_STATEMENT','AMBIGUOUS_CLASSIFICATION') "
                         "AND status='open'", (who, now_iso(), f"reclassified to {chapter}", evidence_id))
        record_decision(conn, "evidence", evidence_id, "reclassify", who, reason,
                        before={"chapter": e["chapter"], "chapter_override": e["chapter_override"]},
                        after={"chapter_override": chapter})


# --------------------------------------------------------------------------- near duplicates
def decide_near_duplicate(conn: sqlite3.Connection, cfg: Config, pair_id: str, decision: str, reviewer: str,
                          comment: str | None = None, keep: str | None = None) -> None:
    who = _reviewer(cfg, reviewer)
    if decision not in ("confirmed_duplicate", "not_duplicate", "candidate"):
        raise ReviewError("decision must be confirmed_duplicate, not_duplicate or candidate")
    with transaction(conn):
        p = row(conn, "SELECT * FROM near_duplicates WHERE pair_id=?", (pair_id,))
        if p is None:
            raise ReviewError(f"Unknown near-duplicate pair {pair_id}")
        a, b = _evidence(conn, p["evidence_a"]), _evidence(conn, p["evidence_b"])
        details: dict[str, Any] = {}
        if decision == "confirmed_duplicate":
            _need_comment(comment, "confirm a merge")
            flags = guard_flags(a, b, cfg)
            if flags:
                raise ReviewError("Merge not allowed - statements differ in: " + ", ".join(flags) +
                                  ". Keep them separate (or resolve via the conflict register).")
            if a["status"] != EvidenceStatus.ACTIVE or b["status"] != EvidenceStatus.ACTIVE:
                raise ReviewError("Both statements must be active")
            keep_id = keep or a["evidence_id"]
            if keep_id not in (a["evidence_id"], b["evidence_id"]):
                raise ReviewError("keep must be one of the pair")
            drop = b if keep_id == a["evidence_id"] else a
            keep_row = a if keep_id == a["evidence_id"] else b
            if keep_row["canonical_evidence_id"]:
                keep_id = keep_row["canonical_evidence_id"]
            conn.execute("UPDATE evidence SET dup_role='near_duplicate_merged', canonical_evidence_id=?, in_manual=0, "
                         "manual_exclusion=? WHERE evidence_id=? OR canonical_evidence_id=?",
                         (keep_id, f"near duplicate merged into {keep_id} by {who}", drop["evidence_id"],
                          drop["evidence_id"]))
            invalidate_evidence_approvals(conn, [keep_id, drop["evidence_id"]], f"near-duplicate merge {pair_id}")
            details = {"kept": keep_id, "merged": drop["evidence_id"]}
        elif p["status"] == "confirmed_duplicate":
            # undo a previous merge
            conn.execute("UPDATE evidence SET dup_role=NULL, canonical_evidence_id=NULL WHERE dup_role="
                         "'near_duplicate_merged' AND evidence_id IN (?,?)", (a["evidence_id"], b["evidence_id"]))
            invalidate_evidence_approvals(conn, [a["evidence_id"], b["evidence_id"]], f"near-duplicate merge undone {pair_id}")
            details = {"merge_undone": True}
        conn.execute("UPDATE near_duplicates SET status=?, decided_by=?, decided_at=?, comment=? WHERE pair_id=?",
                     (decision, who, now_iso(), comment, pair_id))
        record_decision(conn, "near_duplicate", pair_id, decision, who, comment, details=details,
                        before={"status": p["status"]}, after={"status": decision, **details})


# --------------------------------------------------------------------------- conflicts
RESOLUTIONS = ("select_authoritative", "distinct_applicability", "accepted_variance", "not_a_conflict")


def resolve_conflict(conn: sqlite3.Connection, cfg: Config, conflict_id: str, resolution_type: str, reviewer: str,
                     comment: str, authoritative_ids: list[str] | None = None) -> None:
    """Record a human resolution. The tool never picks a value itself."""
    who = _reviewer(cfg, reviewer)
    comment = _need_comment(comment, "resolve a conflict")
    if resolution_type not in RESOLUTIONS:
        raise ReviewError(f"resolution_type must be one of {RESOLUTIONS}")
    with transaction(conn):
        c = row(conn, "SELECT * FROM conflicts WHERE conflict_id=?", (conflict_id,))
        if c is None:
            raise ReviewError(f"Unknown conflict {conflict_id}")
        if c["status"] == ConflictStatus.STALE:
            raise ReviewError("Conflict is stale (evidence changed); re-run validate")
        ids = loads(c["evidence_ids"], [])
        rejected: list[str] = []
        if resolution_type == "select_authoritative":
            auth = [i for i in (authoritative_ids or []) if i in ids]
            if not auth:
                raise ReviewError("Select at least one authoritative evidence item from the conflict")
            for eid in ids:
                if eid in auth:
                    continue
                e = _evidence(conn, eid)
                if e["review_status"] != ReviewStatus.REJECTED:
                    conn.execute("UPDATE evidence SET review_status=?, reviewed_by=?, reviewed_at=?, review_comment=?, "
                                 "approval_fingerprint=NULL WHERE evidence_id=?",
                                 (ReviewStatus.REJECTED, who, now_iso(), f"not authoritative per {conflict_id}: {comment}",
                                  eid))
                    record_decision(conn, "evidence", eid, "reject", who, f"conflict {conflict_id}: {comment}",
                                    before=_review_snapshot(e), after={"review_status": ReviewStatus.REJECTED})
                    rejected.append(eid)
        new_status = ConflictStatus.NOT_A_CONFLICT if resolution_type == "not_a_conflict" else ConflictStatus.RESOLVED
        conn.execute("UPDATE conflicts SET status=?, resolution_type=?, resolution=?, authoritative_ids=?, resolved_by=?, "
                     "resolved_at=? WHERE conflict_id=?",
                     (new_status, resolution_type, comment, dumps(authoritative_ids or []), who, now_iso(), conflict_id))
        record_decision(conn, "conflict", conflict_id, f"resolve:{resolution_type}", who, comment,
                        details={"authoritative": authoritative_ids or [], "rejected": rejected},
                        before={"status": c["status"]}, after={"status": new_status})


def reopen_conflict(conn: sqlite3.Connection, cfg: Config, conflict_id: str, reviewer: str, comment: str) -> None:
    who = _reviewer(cfg, reviewer)
    comment = _need_comment(comment, "reopen a conflict")
    with transaction(conn):
        c = row(conn, "SELECT * FROM conflicts WHERE conflict_id=?", (conflict_id,))
        if c is None:
            raise ReviewError(f"Unknown conflict {conflict_id}")
        conn.execute("UPDATE conflicts SET status=?, resolution_type=NULL, resolution=NULL, resolved_by=NULL, "
                     "resolved_at=NULL WHERE conflict_id=?", (ConflictStatus.OPEN, conflict_id))
        if c["blocking"]:
            invalidate_evidence_approvals(conn, loads(c["evidence_ids"], []), f"conflict {conflict_id} reopened")
        record_decision(conn, "conflict", conflict_id, "reopen", who, comment, before={"status": c["status"]},
                        after={"status": ConflictStatus.OPEN})


# --------------------------------------------------------------------------- errors & visual checks
def set_error_status(conn: sqlite3.Connection, cfg: Config, error_id: int, status: str, reviewer: str,
                     comment: str | None = None) -> None:
    who = _reviewer(cfg, reviewer)
    if status not in (ErrorStatus.OPEN, ErrorStatus.ACKNOWLEDGED, ErrorStatus.RESOLVED, ErrorStatus.WONT_FIX):
        raise ReviewError("invalid error status")
    if status in (ErrorStatus.RESOLVED, ErrorStatus.WONT_FIX):
        comment = _need_comment(comment, f"set an error to {status}")
    with transaction(conn):
        e = row(conn, "SELECT * FROM extraction_errors WHERE error_id=?", (error_id,))
        if e is None:
            raise ReviewError(f"Unknown error {error_id}")
        conn.execute("UPDATE extraction_errors SET status=?, status_by=?, status_at=?, status_comment=? WHERE error_id=?",
                     (status, who, now_iso(), comment, error_id))
        record_decision(conn, "error", str(error_id), f"error:{status}", who, comment,
                        before={"status": e["status"]}, after={"status": status})


def decide_visual_check(conn: sqlite3.Connection, cfg: Config, check_id: str, result: str, reviewer: str,
                        comment: str | None = None) -> None:
    """Mandatory human visual check of complex layouts, figures, OCR pages and generated output."""
    who = _reviewer(cfg, reviewer)
    if result not in (VisualCheckStatus.PASSED, VisualCheckStatus.FAILED, VisualCheckStatus.PENDING):
        raise ReviewError("result must be passed, failed or pending")
    if result == VisualCheckStatus.FAILED:
        comment = _need_comment(comment, "fail a visual check")
    with transaction(conn):
        v = row(conn, "SELECT * FROM visual_checks WHERE check_id=?", (check_id,))
        if v is None:
            raise ReviewError(f"Unknown visual check {check_id}")
        if v["status"] == VisualCheckStatus.INVALIDATED:
            raise ReviewError("Visual check was invalidated because the source changed")
        if v["target_type"] == "source_page":
            src = row(conn, "SELECT sha256 FROM sources WHERE source_id=?", (v["source_id"],))
            if src is None or src["sha256"] != v["source_sha256"]:
                raise ReviewError("Source changed since this check was created; re-run inventory/extract")
        conn.execute("UPDATE visual_checks SET status=?, reviewer=?, reviewed_at=?, comment=? WHERE check_id=?",
                     (result, who, now_iso(), comment, check_id))
        affected: list[str] = []
        if result == VisualCheckStatus.FAILED and v["target_type"] == "source_page":
            if v["evidence_id"]:
                affected = [v["evidence_id"]]
            else:
                affected = [r[0] for r in conn.execute(
                    "SELECT evidence_id FROM evidence WHERE source_id=? AND source_sha256=? AND page_no=? "
                    "AND status='active' AND in_manual=1", (v["source_id"], v["source_sha256"], v["page_no"]))]
            for eid in affected:
                e = _evidence(conn, eid)
                if e["review_status"] in (ReviewStatus.APPROVED, ReviewStatus.UNREVIEWED, ReviewStatus.INVALIDATED):
                    conn.execute("UPDATE evidence SET review_status=?, approval_fingerprint=NULL, review_comment=? "
                                 "WHERE evidence_id=?",
                                 (ReviewStatus.NEEDS_REVISION, f"visual check {check_id} failed: {comment}", eid))
                    record_decision(conn, "evidence", eid, "needs_revision", who,
                                    f"visual check {check_id} failed: {comment}", before=_review_snapshot(e),
                                    after={"review_status": ReviewStatus.NEEDS_REVISION})
        record_decision(conn, "visual_check", check_id, f"visual:{result}", who, comment,
                        details={"affected_evidence": affected}, before={"status": v["status"]},
                        after={"status": result})


# --------------------------------------------------------------------------- queries for the UI
def statement_context(conn: sqlite3.Connection, cfg: Config, evidence_id: str) -> dict[str, Any]:
    e = _evidence(conn, evidence_id)
    return {
        "evidence": e,
        "source": row(conn, "SELECT * FROM sources WHERE source_id=?", (e["source_id"],)),
        "cited": rows(conn, "SELECT e.evidence_id, e.source_id, s.filename, s.revision, e.page_no, e.dup_role, e.status "
                            "FROM evidence e JOIN sources s ON s.source_id=e.source_id WHERE e.evidence_id=? OR "
                            "e.canonical_evidence_id=? ORDER BY e.source_id", (evidence_id, evidence_id)),
        "quantities": rows(conn, "SELECT raw_text, parameter, qualifier, subject, canonical_value, canonical_unit, "
                                 "conversion_status, flags FROM quantities WHERE evidence_id=?", (evidence_id,)),
        "conflicts": rows(conn, "SELECT c.conflict_id, c.conflict_type, c.severity, c.status, c.description FROM "
                                "conflicts c JOIN conflict_evidence ce ON ce.conflict_id=c.conflict_id WHERE "
                                "ce.evidence_id IN (SELECT evidence_id FROM evidence WHERE evidence_id=? OR "
                                "canonical_evidence_id=?) GROUP BY c.conflict_id", (evidence_id, evidence_id)),
        "errors": rows(conn, "SELECT error_id, error_code, severity, status, message FROM extraction_errors WHERE "
                             "evidence_id=?", (evidence_id,)),
        "decisions": rows(conn, "SELECT created_at, reviewer, decision, comment FROM review_decisions WHERE "
                                "entity_type='evidence' AND entity_id=? ORDER BY decision_id DESC", (evidence_id,)),
        "blockers": approval_blockers(conn, cfg, evidence_id),
    }


def dashboard(conn: sqlite3.Connection) -> dict[str, Any]:
    def grp(sql: str) -> dict:
        return {r[0]: r[1] for r in conn.execute(sql)}
    return {
        "sources": grp("SELECT status, COUNT(*) FROM sources GROUP BY status"),
        "extraction": grp("SELECT extraction_status, COUNT(*) FROM sources GROUP BY extraction_status"),
        "pages": grp("SELECT extraction_status, COUNT(*) FROM pages GROUP BY extraction_status"),
        "statements": grp("SELECT review_status, COUNT(*) FROM evidence WHERE status='active' AND in_manual=1 "
                          "GROUP BY review_status"),
        "conflicts_open": grp("SELECT severity, COUNT(*) FROM conflicts WHERE status='open' GROUP BY severity"),
        "errors_open": grp("SELECT severity, COUNT(*) FROM extraction_errors WHERE status='open' GROUP BY severity"),
        "near_duplicates": grp("SELECT status, COUNT(*) FROM near_duplicates GROUP BY status"),
        "visual_checks": grp("SELECT status, COUNT(*) FROM visual_checks GROUP BY status"),
    }
