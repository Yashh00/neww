"""Structured manual model shared by the DOCX and PDF writers.

Both the draft and the approved manual are produced by :func:`build_manual`
from the same evidence tables; only the inclusion filter differs:

* draft    - every active, non-rejected canonical statement (unreviewed items
             are labelled), plus unclassified statements and open conflicts.
* approved - only statements whose approval is still valid: fingerprint
             unchanged AND every approval rule still passes. Generation is
             refused while blocking (critical) conflicts are open.

Nothing is paraphrased or invented: items carry the original extracted text
or the reviewer-approved wording (which preserves every number and unit).
Every rendered technical item must carry at least one evidence citation;
uncited items are withheld and recorded as generation errors.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from maintdoc.analysis.dedupe import topic_key
from maintdoc.approval import approval_blockers, effective_chapter, effective_text, fingerprint
from maintdoc.config import Config
from maintdoc.constants import (DRAFT_DISCLAIMER, TOOL_VERSION, UNCLASSIFIED_CHAPTER, BlockType, ConflictStatus,
                                ReviewStatus, SourceStatus)
from maintdoc.db import rows
from maintdoc.utils import loads, now_iso, numeric_tokens

log = logging.getLogger(__name__)


class GenerationBlocked(RuntimeError):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons[:10]))
        self.reasons = reasons


@dataclass
class Citation:
    evidence_id: str
    source_id: str
    filename: str
    page_no: int
    section: str | None
    sha256: str
    method: str
    ocr_confidence: float | None
    review_status: str
    revision: str | None
    role: str = "primary"            # primary | duplicate | identical_file | caption

    def short(self) -> str:
        return self.evidence_id


@dataclass
class Item:
    item_id: str
    kind: str                         # text | list_item | admonition | table | figure
    block_type: str
    text: str
    original_text: str
    citations: list[Citation]
    safety_level: str | None = None
    table_rows: list[list[str | None]] | None = None
    caption: str | None = None
    figure_path: str | None = None
    review_status: str = ReviewStatus.UNREVIEWED
    flags: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    source_label: str = ""

    def citation_text(self) -> str:
        ids = []
        for c in self.citations:
            if c.role == "identical_file":
                continue
            if c.evidence_id not in ids:
                ids.append(c.evidence_id)
        return "[" + "; ".join(ids) + "]"

    def numbers(self) -> list[str]:
        base = self.text
        if self.table_rows:
            base = "\n".join(" ".join(c or "" for c in r) for r in self.table_rows)
        return sorted(numeric_tokens(base).elements())


@dataclass
class Topic:
    heading: str
    heading_citations: list[str]
    applicability: str
    items: list[Item] = field(default_factory=list)


@dataclass
class Chapter:
    id: str
    title: str
    topics: list[Topic] = field(default_factory=list)
    generated_tables: list[dict[str, Any]] = field(default_factory=list)

    def item_count(self) -> int:
        return sum(len(t.items) for t in self.topics)


@dataclass
class Manual:
    mode: str
    title: str
    organisation: str
    generated_at: str
    run_id: str
    config_hash: str
    chapters: list[Chapter]
    sources: list[dict]
    citation_index: dict[str, Citation]
    conflicts: list[dict]
    stats: dict[str, Any]
    withheld: list[dict]
    disclaimer: str = DRAFT_DISCLAIMER

    def items(self) -> list[Item]:
        return [i for c in self.chapters for t in c.topics for i in t.items]


def _app_label(e: dict) -> str:
    parts = [p for p in (e.get("equipment"), e.get("model")) if p]
    return " / ".join(parts) if parts else "Applicability not stated"


def _kind(block_type: str) -> str:
    if block_type in BlockType.ADMONITIONS:
        return "admonition"
    if block_type == BlockType.TABLE:
        return "table"
    if block_type == BlockType.FIGURE:
        return "figure"
    if block_type == BlockType.LIST_ITEM:
        return "list_item"
    return "text"


def build_manual(conn: sqlite3.Connection, cfg: Config, mode: str, run_id: str = "") -> Manual:
    if mode not in ("draft", "approved"):
        raise ValueError("mode must be draft or approved")
    sources = {r["source_id"]: r for r in rows(conn, "SELECT * FROM sources ORDER BY source_id")}
    identical_files: dict[str, list[dict]] = defaultdict(list)
    for s in sources.values():
        if s["status"] == SourceStatus.DUPLICATE and s["duplicate_of"]:
            identical_files[s["duplicate_of"]].append(s)
    conflicts = rows(conn, "SELECT * FROM conflicts ORDER BY conflict_id")
    open_blocking = [c for c in conflicts if c["status"] == ConflictStatus.OPEN and c["blocking"]]
    if mode == "approved" and open_blocking and cfg.get("generation.block_approved_on_open_critical_conflicts", True):
        raise GenerationBlocked([f"{c['conflict_id']} ({c['severity']} {c['conflict_type']}) is unresolved"
                                 for c in open_blocking])
    ev_conflicts: dict[str, list[str]] = defaultdict(list)
    for r in conn.execute("SELECT ce.evidence_id, c.conflict_id FROM conflict_evidence ce JOIN conflicts c ON "
                          "c.conflict_id=ce.conflict_id WHERE c.status='open'"):
        ev_conflicts[r[0]].append(r[1])

    all_ev = rows(conn, """
        SELECT * FROM evidence WHERE status='active' AND (in_manual=1 OR dup_role IN ('exact_duplicate',
        'near_duplicate_merged')) ORDER BY source_id, page_no, seq""")
    by_id = {e["evidence_id"]: e for e in all_ev}
    captions = {e["evidence_id"]: e for e in rows(conn, "SELECT * FROM evidence WHERE status='active' AND "
                                                        "block_type='caption'")}
    headings = {e["evidence_id"]: e for e in rows(conn, "SELECT evidence_id, text, source_id, page_no FROM evidence "
                                                        "WHERE block_type='heading'")}
    members: dict[str, list[dict]] = defaultdict(list)
    for e in all_ev:
        if e["canonical_evidence_id"]:
            members[e["canonical_evidence_id"]].append(e)

    def cit(e: dict, role: str) -> Citation:
        s = sources.get(e["source_id"], {})
        return Citation(e["evidence_id"], e["source_id"], s.get("filename", ""), e["page_no"], e.get("section_path"),
                        e["source_sha256"], e["extraction_method"], e.get("ocr_confidence"), e["review_status"],
                        s.get("revision"), role)

    withheld: list[dict] = []

    def include(e: dict) -> bool:
        if not e["in_manual"] or e["dup_role"] in ("exact_duplicate", "near_duplicate_merged"):
            return False
        if mode == "draft":
            if e["review_status"] == ReviewStatus.REJECTED:
                return False
            if effective_chapter(e) in (None, UNCLASSIFIED_CHAPTER) and not cfg.get(
                    "generation.include_unclassified_in_draft", True):
                withheld.append({"evidence_id": e["evidence_id"], "reason": "unclassified (excluded by config)"})
                return False
            return True
        if e["review_status"] != ReviewStatus.APPROVED:
            return False
        if fingerprint(conn, e) != e["approval_fingerprint"]:
            withheld.append({"evidence_id": e["evidence_id"], "reason": "approval fingerprint no longer matches"})
            return False
        blockers = approval_blockers(conn, cfg, e["evidence_id"])
        if blockers:
            withheld.append({"evidence_id": e["evidence_id"], "reason": "; ".join(blockers)})
            return False
        return True

    def make_item(e: dict) -> Item:
        cites = [cit(e, "primary")] + [cit(m, "duplicate") for m in members.get(e["evidence_id"], [])]
        for c in list(cites):
            for dup_src in identical_files.get(c.source_id, []):
                cites.append(Citation(c.evidence_id, dup_src["source_id"], dup_src["filename"], c.page_no, c.section,
                                      dup_src["sha256"], c.method, c.ocr_confidence, c.review_status,
                                      dup_src.get("revision"), "identical_file"))
        caption = None
        rel = e.get("related_evidence_id")
        if e["block_type"] in (BlockType.TABLE, BlockType.FIGURE) and rel and rel in captions:
            caption = captions[rel]["text"]
            cites.append(cit(captions[rel], "caption"))
        text = effective_text(e)
        item = Item(item_id=e["evidence_id"], kind=_kind(e["block_type"]), block_type=e["block_type"], text=text,
                    original_text=e["text"], citations=cites, safety_level=e.get("safety_level"),
                    table_rows=loads(e["table_json"]) if e.get("table_json") else None, caption=caption,
                    figure_path=e.get("figure_path"), review_status=e["review_status"])
        if e["block_type"] == BlockType.FIGURE and caption:
            item.text = caption
        s = sources.get(e["source_id"], {})
        item.source_label = f"{s.get('filename', e['source_id'])}" + (f" Rev {s['revision']}" if s.get("revision") else "")
        ids = [e["evidence_id"]] + [m["evidence_id"] for m in members.get(e["evidence_id"], [])]
        item.conflicts = sorted({cf for i in ids for cf in ev_conflicts.get(i, [])})
        if mode == "draft":
            if e["review_status"] != ReviewStatus.APPROVED:
                item.flags.append(e["review_status"].upper())
            if e["extraction_method"] == "tesseract_ocr":
                item.flags.append(f"OCR {e.get('ocr_confidence') or '?'}%" +
                                  ("" if e.get("ocr_verified") else " unverified"))
            if e.get("display_text"):
                item.flags.append("REVIEWER WORDING")
            if e.get("quality_score") is not None and e["quality_score"] < float(
                    cfg.get("extraction.gibberish_threshold", 0.45)):
                item.flags.append("LOW TEXT QUALITY")
        return item

    # ---- chapters -> topics (aligned across sources) -> items
    chapter_order = [c["id"] for c in cfg.chapters()] + [UNCLASSIFIED_CHAPTER]
    topics: dict[tuple[str, str, str], dict[str, Any]] = {}
    for e in all_ev:
        canon = by_id.get(e["canonical_evidence_id"]) if e["canonical_evidence_id"] else e
        if canon is None:
            continue
        ch = effective_chapter(canon) or UNCLASSIFIED_CHAPTER
        key = (ch, _app_label(canon), topic_key(canon.get("section_heading")))
        t = topics.setdefault(key, {"seqs": defaultdict(list), "heading": canon.get("section_heading"),
                                    "heading_ids": []})
        t["seqs"][e["source_id"]].append(canon["evidence_id"])
        if e.get("section_evidence_id") and e["section_evidence_id"] not in t["heading_ids"]:
            t["heading_ids"].append(e["section_evidence_id"])

    chapters: dict[str, Chapter] = {cid: Chapter(cid, cfg.chapter_title(cid)) for cid in chapter_order}
    uncited = 0
    for (ch, app, tkey), t in sorted(topics.items(), key=lambda kv: (chapter_order.index(kv[0][0])
                                                                      if kv[0][0] in chapter_order else 999,
                                                                      kv[0][1], min(kv[1]["seqs"]))):
        # Union of the sources' sequences: shared (duplicate) items appear once at their common
        # position; items unique to a later source are placed after the earlier sources' items at
        # the same position. Nothing is re-ordered within a source and nothing is dropped.
        merged: list[str] = []
        for sid in sorted(t["seqs"]):
            seq = t["seqs"][sid]
            in_seq = set(seq)
            cursor = -1
            for cid in seq:
                if cid in merged:
                    cursor = merged.index(cid)
                else:
                    pos = cursor + 1
                    while pos < len(merged) and merged[pos] not in in_seq:
                        pos += 1
                    merged.insert(pos, cid)
                    cursor = pos
        topic = Topic(heading=t["heading"] or "General", heading_citations=t["heading_ids"], applicability=app)
        for cid in merged:
            e = by_id[cid]
            if not include(e):
                continue
            item = make_item(e)
            if not item.citations:
                uncited += 1
                withheld.append({"evidence_id": cid, "reason": "no citation available (withheld)"})
                continue
            topic.items.append(item)
        if topic.items:
            chapters.setdefault(ch, Chapter(ch, cfg.chapter_title(ch))).topics.append(topic)

    # ---- generated reference tables (source register, figure index)
    rev = chapters.get("revision_references")
    if rev is not None:
        reg_rows = []
        for s in sources.values():
            origins = loads(s["metadata_origin_json"], {}) or {}
            o = origins.get("revision", {})
            reg_rows.append({
                "source_id": s["source_id"], "file": s["rel_path"], "doc_number": s["doc_number"] or "",
                "title": s["doc_title"] or "", "revision": s["revision"] or "not determined",
                "date": s["doc_date"] or "not determined", "status": f"{s['status']}/{s['extraction_status']}",
                "sha256": (s["sha256"] or "")[:16], "revision_origin": o.get("evidence_id") or o.get("origin") or "-",
            })
        rev.generated_tables.append({"title": "Source document register (from file metadata; origin per field)",
                                     "columns": ["source_id", "file", "doc_number", "title", "revision", "date",
                                                 "status", "sha256", "revision_origin"], "rows": reg_rows})
    dia = chapters.get("diagrams")
    fig_items = [i for c in chapters.values() for t in c.topics for i in t.items if i.kind == "figure"]
    if dia is not None and fig_items:
        dia.generated_tables.append({
            "title": "Index of source figures (each figure is shown in its chapter context)",
            "columns": ["figure", "caption", "source", "page", "citation"],
            "rows": [{"figure": i.item_id, "caption": i.caption or "(no caption)", "source": i.source_label,
                      "page": i.citations[0].page_no, "citation": i.citation_text()} for i in fig_items]})

    ordered = [chapters[c] for c in chapter_order if c in chapters]
    if mode == "approved" or not cfg.get("generation.include_unclassified_in_draft", True):
        ordered = [c for c in ordered if c.id != UNCLASSIFIED_CHAPTER or c.topics]
    citation_index: dict[str, Citation] = {}
    for c in ordered:
        for t in c.topics:
            for i in t.items:
                for ct in i.citations:
                    if ct.role != "identical_file":
                        citation_index.setdefault(ct.evidence_id, ct)
    items = [i for c in ordered for t in c.topics for i in t.items]
    stats = {
        "items": len(items),
        "tables": sum(1 for i in items if i.kind == "table"),
        "figures": sum(1 for i in items if i.kind == "figure"),
        "admonitions": sum(1 for i in items if i.kind == "admonition"),
        "approved_items": sum(1 for i in items if i.review_status == ReviewStatus.APPROVED),
        "withheld": len(withheld),
        "uncited_withheld": uncited,
        "sources": len(sources),
        "open_conflicts": sum(1 for c in conflicts if c["status"] == ConflictStatus.OPEN),
        "open_blocking_conflicts": len(open_blocking),
        "pending_visual_checks": conn.execute("SELECT COUNT(*) FROM visual_checks WHERE status='pending'").fetchone()[0],
    }
    shown_conflicts = [c for c in conflicts if c["status"] == ConflictStatus.OPEN or
                       (mode == "approved" and c["status"] in (ConflictStatus.RESOLVED, ConflictStatus.NOT_A_CONFLICT))]
    title = cfg.get("project.manual_title", "Master Maintenance Manual")
    return Manual(mode=mode, title=title, organisation=cfg.get("project.organisation", ""), generated_at=now_iso(),
                  run_id=run_id, config_hash=cfg.fingerprint()[:16], chapters=ordered, sources=list(sources.values()),
                  citation_index=citation_index, conflicts=shown_conflicts, stats=stats, withheld=withheld)


def manifest(manual: Manual, outputs: dict[str, str]) -> dict[str, Any]:
    """Machine-readable record of exactly what was rendered (used by verification)."""
    return {
        "mode": manual.mode, "title": manual.title, "generated_at": manual.generated_at, "run_id": manual.run_id,
        "tool_version": TOOL_VERSION, "config_hash": manual.config_hash, "outputs": outputs, "stats": manual.stats,
        "chapters": [{"id": c.id, "title": c.title, "items": c.item_count(),
                      "topics": [t.heading for t in c.topics]} for c in manual.chapters],
        "items": [{"item_id": i.item_id, "kind": i.kind, "block_type": i.block_type, "text": i.text,
                   "original_text": i.original_text, "numbers": i.numbers(), "citation_text": i.citation_text(),
                   "citations": [c.evidence_id for c in i.citations if c.role != "identical_file"],
                   "identical_files": [c.source_id for c in i.citations if c.role == "identical_file"],
                   "table_rows": i.table_rows, "review_status": i.review_status, "conflicts": i.conflicts}
                  for i in manual.items()],
        "withheld": manual.withheld,
    }
