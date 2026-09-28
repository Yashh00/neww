"""The ``validate`` stage: rule-based analysis of extracted evidence.

Order matters:
 1. refresh document metadata from extracted text (e.g. revision on scanned pages)
 2. classify every active evidence item (chapter + applicability)
 3. extract numeric values/units (incremental)
 4. exact de-duplication, manual eligibility
 5. near-duplicate review candidates
 6. conflict detection
 7. re-validate existing approvals (fingerprints, blocking conflicts)
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any

from maintdoc import audit
from maintdoc.analysis.classify import ChapterClassifier, DocumentClassifier, applicability
from maintdoc.analysis.conflicts import open_blocking_evidence, run_conflict_detection
from maintdoc.analysis.dedupe import run_exact_dedupe, run_near_dedupe
from maintdoc.analysis.units import UnitExtractor
from maintdoc.approval import fingerprint
from maintdoc.config import Config
from maintdoc.constants import BlockType, EvidenceStatus, ReviewStatus, SourceStatus
from maintdoc.db import rows, transaction
from maintdoc.errors import auto_resolve, error_fingerprint, record_error
from maintdoc.invalidation import invalidate_evidence_approvals
from maintdoc.progress import Progress
from maintdoc.utils import dumps, loads, now_iso

log = logging.getLogger(__name__)

QUANTITY_TYPES = (BlockType.PARAGRAPH, BlockType.LIST_ITEM, BlockType.TABLE, BlockType.DANGER, BlockType.WARNING,
                  BlockType.CAUTION, BlockType.NOTICE, BlockType.NOTE)


# --------------------------------------------------------------------------- 1. metadata
def refresh_source_metadata(conn: sqlite3.Connection, cfg: Config, run_id: str) -> dict[str, int]:
    """Fill metadata fields that were missing at inventory time (e.g. scanned title pages)."""
    doc_cls = DocumentClassifier(cfg)
    required = cfg.get("metadata.required_fields", [])
    scan_pages = int(cfg.get("metadata.scan_pages", 2))
    filled = 0
    for s in rows(conn, "SELECT * FROM sources WHERE status=? AND present=1 AND extraction_status IN "
                        "('complete','complete_with_errors')", (SourceStatus.OK,)):
        missing = [f for f in DocumentClassifier.FIELDS if not s.get(f)]
        if not missing:
            continue
        ev = rows(conn, "SELECT evidence_id, text, extraction_method FROM evidence WHERE source_id=? AND "
                        "source_sha256=? AND status='active' AND page_no<=? AND block_type!='header_footer' "
                        "ORDER BY page_no, seq", (s["source_id"], s["sha256"], scan_pages))
        if not ev:
            continue
        text = "\n".join(e["text"] for e in ev)
        meta = doc_cls.classify(s["rel_path"], {}, text)
        origins = loads(s["metadata_origin_json"], {}) or {}
        updates: dict[str, Any] = {}
        for f in missing:
            val = meta.values.get(f)
            if not val or meta.origins.get(f, {}).get("origin") != "page_text":
                continue
            hit = next((e for e in ev if str(val).split(";")[0].strip().lower() in e["text"].lower()), None)
            origin = "page_text(ocr)" if hit and hit["extraction_method"] == "tesseract_ocr" else "page_text"
            updates[f] = val
            origins[f] = {"value": val, "origin": origin, "evidence_id": hit["evidence_id"] if hit else None}
        if not updates:
            continue
        with transaction(conn):
            sets = ", ".join(f"{k}=?" for k in updates)
            conn.execute(f"UPDATE sources SET {sets}, metadata_origin_json=? WHERE source_id=?",
                         (*updates.values(), dumps(origins), s["source_id"]))
            for f, v in updates.items():
                if f in required:
                    fp = error_fingerprint("MISSING_METADATA", s["source_id"], s["sha256"], None, None, f)
                    conn.execute("UPDATE extraction_errors SET status='resolved', status_by='system', status_at=?, "
                                 "status_comment=? WHERE fingerprint=? AND status='open'",
                                 (now_iso(), f"determined from extracted text: {v} ({origins[f].get('evidence_id')})",
                                  fp))
            audit.append(conn, "system", "source.metadata_from_text", "source", s["source_id"],
                         after={k: origins[k] for k in updates}, run_id=run_id)
        filled += len(updates)
    return {"fields_filled": filled}


# --------------------------------------------------------------------------- 2. classification
def classify_evidence(conn: sqlite3.Connection, cfg: Config, run_id: str, show_progress: bool = False) -> dict[str, int]:
    cc = ChapterClassifier(cfg)
    dc = DocumentClassifier(cfg)
    sources = {r["source_id"]: r for r in rows(conn, "SELECT * FROM sources")}
    items = rows(conn, "SELECT evidence_id, source_id, block_type, text, section_heading, section_path, "
                       "chapter_override FROM evidence WHERE status=?", (EvidenceStatus.ACTIVE,))
    stats = {"classified": 0, "ambiguous": 0, "unclassified": 0, "manual": 0}
    prog = Progress(len(items), "classify", enabled=show_progress and len(items) > 5000)
    updates = []
    for e in items:
        res = cc.classify(e["text"], e["block_type"], e["section_heading"], e["section_path"])
        app = applicability(dc, e["text"], e["section_path"], sources.get(e["source_id"], {}))
        status = "manual" if e["chapter_override"] else res.status
        stats[status] = stats.get(status, 0) + 1
        updates.append((res.chapter, round(res.score, 2), res.rule[:500], status, app.get("equipment"),
                        app.get("model"), app.get("component"), app.get("revision"), app.get("applicability_origin"),
                        e["evidence_id"]))
        prog.update()
    prog.close()
    with transaction(conn):
        conn.executemany("UPDATE evidence SET chapter=?, chapter_score=?, chapter_rule=?, classification_status=?, "
                         "equipment=?, model=?, component=?, revision=?, applicability_origin=? WHERE evidence_id=?",
                         updates)
    return stats


def record_classification_errors(conn: sqlite3.Connection, run_id: str) -> dict[str, int]:
    keep_unc: set[str] = set()
    keep_amb: set[str] = set()
    with transaction(conn):
        for e in rows(conn, "SELECT evidence_id, source_id, source_sha256, page_no, text, classification_status, "
                            "chapter_rule FROM evidence WHERE status='active' AND in_manual=1 AND "
                            "classification_status IN ('unclassified','ambiguous') AND chapter_override IS NULL"):
            if e["classification_status"] == "unclassified":
                record_error(conn, "UNCLASSIFIED_STATEMENT", f"No chapter rule matched: {e['text'][:100]!r}",
                             stage="classification", source_id=e["source_id"], source_sha256=e["source_sha256"],
                             page_no=e["page_no"], evidence_id=e["evidence_id"], run_id=run_id)
                keep_unc.add(error_fingerprint("UNCLASSIFIED_STATEMENT", e["source_id"], e["source_sha256"],
                                               e["page_no"], e["evidence_id"], None))
            else:
                record_error(conn, "AMBIGUOUS_CLASSIFICATION", f"Close chapter scores: {e['chapter_rule'][:200]}",
                             stage="classification", source_id=e["source_id"], source_sha256=e["source_sha256"],
                             page_no=e["page_no"], evidence_id=e["evidence_id"], run_id=run_id)
                keep_amb.add(error_fingerprint("AMBIGUOUS_CLASSIFICATION", e["source_id"], e["source_sha256"],
                                               e["page_no"], e["evidence_id"], None))
        r1 = auto_resolve(conn, "UNCLASSIFIED_STATEMENT", keep_unc, "classified by rule or reviewer")
        r2 = auto_resolve(conn, "AMBIGUOUS_CLASSIFICATION", keep_amb, "no longer ambiguous")
    return {"unclassified_open": len(keep_unc), "ambiguous_open": len(keep_amb), "auto_resolved": r1 + r2}


# --------------------------------------------------------------------------- 3. quantities
def extract_quantities(conn: sqlite3.Connection, cfg: Config, run_id: str) -> dict[str, int]:
    ux = UnitExtractor(cfg)
    units_hash = cfg.fingerprint("units", "conflicts.stopwords", "conflicts.synonyms")
    row_ = conn.execute("SELECT value FROM meta WHERE key='units_config_hash'").fetchone()
    with transaction(conn):
        if row_ is None or row_[0] != units_hash:
            conn.execute("DELETE FROM quantities")
            conn.execute("UPDATE evidence SET quantities_done=0")
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('units_config_hash', ?)", (units_hash,))
    q = ",".join("?" * len(QUANTITY_TYPES))
    items = rows(conn, f"SELECT evidence_id, source_id, source_sha256, page_no, text, table_json, section_heading "
                       f"FROM evidence WHERE status='active' AND quantities_done=0 AND block_type IN ({q})",
                 QUANTITY_TYPES)
    stats = {"evidence": len(items), "quantities": 0, "missing_unit": 0, "dimension_mismatch": 0, "unsafe": 0,
             "dual_inconsistent": 0}
    batch_q: list[tuple] = []
    with transaction(conn):
        for e in items:
            qs = ux.extract(e["text"], e["section_heading"], loads(e["table_json"]) if e["table_json"] else None)
            ids = [f"{e['evidence_id']}#Q{i + 1}" for i in range(len(qs))]
            for i, qq in enumerate(qs):
                batch_q.append((ids[i], e["evidence_id"], qq.raw_text, qq.value_text, qq.value, qq.value_min,
                                qq.value_max, qq.tolerance, qq.unit_raw, qq.unit_norm, qq.dimensionality, qq.parameter,
                                qq.interval_basis, qq.qualifier, qq.subject, qq.canonical_unit, qq.canonical_value,
                                qq.canonical_min, qq.canonical_max, qq.conversion_status, dumps(qq.flags),
                                ids[qq.alternate_of] if qq.alternate_of is not None else None, qq.char_start,
                                qq.char_end))
                stats["quantities"] += 1
                common = dict(stage="units", source_id=e["source_id"], source_sha256=e["source_sha256"],
                              page_no=e["page_no"], evidence_id=e["evidence_id"], run_id=run_id, key=ids[i])
                if "missing_unit" in qq.flags:
                    stats["missing_unit"] += 1
                    record_error(conn, "UNIT_MISSING", f"'{qq.raw_text}' ({qq.parameter} context) has no unit",
                                 details={"text": e["text"][:200]}, **common)
                mism = [f for f in qq.flags if f.startswith("dimension_mismatch")]
                if mism:
                    stats["dimension_mismatch"] += 1
                    record_error(conn, "UNIT_DIMENSION_MISMATCH", f"'{qq.raw_text}': {mism[0]}",
                                 details={"text": e["text"][:200]}, **common)
                elif qq.conversion_status == "unsafe":
                    stats["unsafe"] += 1
                    record_error(conn, "UNIT_AMBIGUOUS", f"'{qq.raw_text}': conversion unsafe "
                                 f"({', '.join(f for f in qq.flags if f not in ('from_table',))})",
                                 details={"flags": qq.flags}, **common)
                if any(f.startswith("dual_unit_inconsistent") for f in qq.flags) and qq.alternate_of is not None:
                    stats["dual_inconsistent"] += 1
                    record_error(conn, "DUAL_UNIT_INCONSISTENT", f"'{qq.raw_text}' does not match the primary value",
                                 details={"flags": qq.flags}, **common)
            if len(batch_q) > 2000:
                _flush_q(conn, batch_q)
        _flush_q(conn, batch_q)
        conn.executemany("UPDATE evidence SET quantities_done=1 WHERE evidence_id=?",
                         [(e["evidence_id"],) for e in items])
    return stats


def _flush_q(conn: sqlite3.Connection, batch: list[tuple]) -> None:
    if batch:
        conn.executemany("INSERT OR REPLACE INTO quantities(quantity_id, evidence_id, raw_text, value_text, value, "
                         "value_min, value_max, tolerance, unit_raw, unit_norm, dimensionality, parameter, "
                         "interval_basis, qualifier, subject, canonical_unit, canonical_value, canonical_min, "
                         "canonical_max, conversion_status, flags, alternate_of, char_start, char_end) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
        batch.clear()


# --------------------------------------------------------------------------- 4. manual eligibility
_BLANK = re.compile(r"(?i)^\s*this\s+page\s+(?:is\s+)?intentionally\s+(?:left\s+)?blank\.?\s*$")


def compute_in_manual(conn: sqlite3.Connection, cfg: Config) -> dict[str, int]:
    include_figs = bool(cfg.get("generation.include_figures", True))
    figure_ids = {r[0] for r in conn.execute("SELECT evidence_id FROM evidence WHERE block_type='figure' AND status='active'")}
    table_ids = {r[0] for r in conn.execute("SELECT evidence_id FROM evidence WHERE block_type='table' AND status='active'")}
    updates = []
    stats: dict[str, int] = {"in_manual": 0}
    for e in rows(conn, "SELECT evidence_id, block_type, text, dup_role, canonical_evidence_id, related_evidence_id "
                        "FROM evidence WHERE status='active'"):
        bt = e["block_type"]
        reason = None
        if bt == BlockType.HEADING:
            reason = "structure: rendered as section heading"
        elif bt == BlockType.HEADER_FOOTER:
            reason = "page header/footer"
        elif bt in (BlockType.TABLE_TEXT, BlockType.FIGURE_TEXT):
            reason = "covered by table/figure"
        elif e["dup_role"] in ("exact_duplicate", "near_duplicate_merged"):
            reason = f"duplicate: cited via {e['canonical_evidence_id']}"
        elif bt == BlockType.CAPTION and e["related_evidence_id"] in figure_ids and include_figs:
            reason = f"caption rendered with figure {e['related_evidence_id']}"
        elif bt == BlockType.CAPTION and e["related_evidence_id"] in table_ids:
            reason = f"caption rendered with table {e['related_evidence_id']}"
        elif bt == BlockType.FIGURE and not include_figs:
            reason = "figures disabled (generation.include_figures=false)"
        elif not e["text"].strip():
            reason = "empty text"
        elif _BLANK.match(e["text"]):
            reason = "blank-page notice"
        elif bt not in BlockType.STATEMENT_TYPES:
            reason = f"block type {bt} not rendered"
        updates.append((0 if reason else 1, reason, e["evidence_id"]))
        if reason:
            key = reason.split(":")[0]
            stats[key] = stats.get(key, 0) + 1
        else:
            stats["in_manual"] += 1
    with transaction(conn):
        conn.executemany("UPDATE evidence SET in_manual=?, manual_exclusion=? WHERE evidence_id=?", updates)
        conn.execute("UPDATE evidence SET in_manual=0, manual_exclusion=COALESCE(status_reason, status) "
                     "WHERE status!='active'")
    return stats


# --------------------------------------------------------------------------- 7. approvals
def revalidate_approvals(conn: sqlite3.Connection, cfg: Config, run_id: str) -> dict[str, int]:
    blocking = open_blocking_evidence(conn)
    changed: list[str] = []
    conflicted: list[tuple[str, list[str]]] = []
    for e in rows(conn, "SELECT * FROM evidence WHERE review_status=?", (ReviewStatus.APPROVED,)):
        if fingerprint(conn, e) != e["approval_fingerprint"]:
            changed.append(e["evidence_id"])
            continue
        ids = [e["evidence_id"]] + [r[0] for r in conn.execute(
            "SELECT evidence_id FROM evidence WHERE canonical_evidence_id=?", (e["evidence_id"],))]
        cf = sorted({c for i in ids for c in blocking.get(i, [])})
        if cf:
            conflicted.append((e["evidence_id"], cf))
    n1 = invalidate_evidence_approvals(conn, changed, "approved content, classification or cited evidence changed",
                                       run_id)
    n2 = 0
    for eid, cf in conflicted:
        n2 += invalidate_evidence_approvals(conn, [eid], f"unresolved blocking conflict(s): {', '.join(cf)}", run_id)
    return {"invalidated_changed": n1, "invalidated_conflict": n2}


# --------------------------------------------------------------------------- orchestration
def run_validate(conn: sqlite3.Connection, cfg: Config, run_id: str, show_progress: bool = True) -> dict[str, Any]:
    stats: dict[str, Any] = {}
    steps = [
        ("metadata", lambda: refresh_source_metadata(conn, cfg, run_id)),
        ("classification", lambda: classify_evidence(conn, cfg, run_id, show_progress)),
        ("quantities", lambda: extract_quantities(conn, cfg, run_id)),
        ("dedupe_exact", lambda: run_exact_dedupe(conn, cfg)),
        ("manual_eligibility", lambda: compute_in_manual(conn, cfg)),
        ("classification_errors", lambda: record_classification_errors(conn, run_id)),
        ("dedupe_near", lambda: run_near_dedupe(conn, cfg, run_id)),
        ("conflicts", lambda: run_conflict_detection(conn, cfg, run_id)),
        ("approvals", lambda: revalidate_approvals(conn, cfg, run_id)),
    ]
    prog = Progress(len(steps), "validate", enabled=show_progress)
    for name, fn in steps:
        log.info("validate: %s", name)
        stats[name] = fn()
        prog.update(note=name)
    prog.close()
    return stats
