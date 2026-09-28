"""Approval rules shared by the review workflow, validation and verification.

An approval is bound to a *fingerprint* of everything it depends on: the
original text, reviewer wording, source SHA-256, block type, chapter,
applicability, table content and every cited evidence item (canonical plus
duplicates) with its own source hash and status. If any of it changes, the
fingerprint changes and the approval is invalidated.

:func:`approval_blockers` lists every reason an evidence item cannot (or can
no longer) be approved. The same function is used before approving and when
verifying approved content, so the rules cannot drift apart.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from maintdoc.config import Config
from maintdoc.constants import (UNCLASSIFIED_CHAPTER, BlockType, EvidenceStatus, ExtractionMethod, SourceStatus,
                                VisualCheckStatus)
from maintdoc.db import row, rows
from maintdoc.utils import numeric_tokens, sha256_json

PAGE_CHECK_KINDS = ("multi_column", "ocr_page", "table_failure", "low_quality_text", "failed_page", "complex_table",
                    "graphic_only")


def effective_text(e: dict) -> str:
    return e.get("display_text") or e["text"]


def effective_chapter(e: dict) -> str | None:
    return e.get("chapter_override") or e.get("chapter")


def cited_evidence(conn: sqlite3.Connection, evidence_id: str) -> list[dict]:
    """The canonical item plus all items merged into it (exact or reviewer-confirmed near duplicates)."""
    return rows(conn, "SELECT evidence_id, source_id, source_sha256, page_no, status, text FROM evidence "
                      "WHERE evidence_id=? OR canonical_evidence_id=? ORDER BY source_id, page_no, seq",
                (evidence_id, evidence_id))


def fingerprint(conn: sqlite3.Connection, e: dict) -> str:
    cites = cited_evidence(conn, e["evidence_id"])
    payload = {
        "text": e["text"],
        "display_text": e.get("display_text"),
        "source_sha256": e["source_sha256"],
        "block_type": e["block_type"],
        "chapter": effective_chapter(e),
        "applicability": [e.get("equipment"), e.get("model"), e.get("component"), e.get("revision")],
        "table_json": e.get("table_json"),
        "status": e["status"],
        "citations": [(c["evidence_id"], c["source_sha256"], c["status"]) for c in cites],
    }
    return sha256_json(payload)


def numeric_identity(original: str, edited: str | None, cfg: Config | None = None) -> list[str]:
    """Problems when reviewer wording does not preserve every number and unit of the original."""
    if not edited or edited == original:
        return []
    problems = []
    a, b = numeric_tokens(original), numeric_tokens(edited)
    if a != b:
        missing = a - b
        added = b - a
        if missing:
            problems.append(f"numbers missing from wording: {sorted(missing.elements())}")
        if added:
            problems.append(f"numbers not present in source: {sorted(added.elements())}")
    if cfg is not None:
        from maintdoc.analysis.units import UnitExtractor, quantity_signature
        ux = UnitExtractor(cfg)
        sa = quantity_signature(ux.extract(original))
        sb = quantity_signature(ux.extract(edited))
        if sa != sb:
            problems.append(f"values/units differ from source: source {sa} vs wording {sb}")
    return problems


def approval_blockers(conn: sqlite3.Connection, cfg: Config, evidence_id: str,
                      pending_display_text: str | None = None) -> list[str]:
    e = row(conn, "SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,))
    if e is None:
        return ["evidence does not exist"]
    if pending_display_text is not None:
        e["display_text"] = pending_display_text
    blockers: list[str] = []
    if e["status"] != EvidenceStatus.ACTIVE:
        blockers.append(f"evidence is {e['status']} ({e.get('status_reason') or ''})")
    if e["dup_role"] in ("exact_duplicate", "near_duplicate_merged"):
        blockers.append(f"evidence is a duplicate; approve canonical {e['canonical_evidence_id']} instead")
    elif not e["in_manual"] and e["block_type"] not in (BlockType.HEADING,):
        blockers.append(f"not a manual statement ({e.get('manual_exclusion') or e['block_type']})")
    src = row(conn, "SELECT * FROM sources WHERE source_id=?", (e["source_id"],))
    if src is None or not src["present"]:
        blockers.append("source file is missing")
    elif src["status"] != SourceStatus.OK:
        blockers.append(f"source status is {src['status']}")
    elif src["sha256"] != e["source_sha256"]:
        blockers.append("source file content changed since extraction")
    chapter = effective_chapter(e)
    if not chapter or chapter == UNCLASSIFIED_CHAPTER:
        blockers.append("statement is unclassified; assign a chapter first")
    blockers.extend(numeric_identity(e["text"], e.get("display_text"), cfg))
    cites = cited_evidence(conn, evidence_id)
    for c in cites:
        if c["status"] != EvidenceStatus.ACTIVE:
            blockers.append(f"cited evidence {c['evidence_id']} is {c['status']}")
    ids = [c["evidence_id"] for c in cites]
    q = ",".join("?" * len(ids))
    for r in conn.execute(f"SELECT DISTINCT c.conflict_id, c.severity FROM conflict_evidence ce JOIN conflicts c "
                          f"ON c.conflict_id=ce.conflict_id WHERE ce.evidence_id IN ({q}) AND c.status='open' "
                          f"AND c.blocking=1", ids):
        blockers.append(f"unresolved {r['severity']} conflict {r['conflict_id']}")
    if (e["extraction_method"] == ExtractionMethod.TESSERACT_OCR and cfg.get("review.require_ocr_confirmation", True)
            and not e["ocr_verified"]):
        blockers.append("OCR text must be confirmed against the page image (tick 'OCR verified')")
    thr = float(cfg.get("extraction.gibberish_threshold", 0.45))
    if e["quality_score"] is not None and e["quality_score"] < thr and not e.get("display_text"):
        blockers.append(f"text quality score {e['quality_score']:.2f} is low; correct the wording or reject")
    # mandatory human visual checks
    kinds = ",".join("?" * len(PAGE_CHECK_KINDS))
    for r in conn.execute(
            f"SELECT check_id, kind, status FROM visual_checks WHERE source_id=? AND source_sha256=? AND "
            f"((evidence_id=?) OR (page_no=? AND evidence_id IS NULL AND kind IN ({kinds}))) AND status != ?",
            (e["source_id"], e["source_sha256"], evidence_id, e["page_no"], *PAGE_CHECK_KINDS,
             VisualCheckStatus.PASSED)):
        blockers.append(f"visual check {r['check_id']} ({r['kind']}) is {r['status']}")
    return blockers


def approved_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    return {r["review_status"]: r["n"] for r in conn.execute(
        "SELECT review_status, COUNT(*) n FROM evidence WHERE status='active' AND in_manual=1 GROUP BY review_status")}
