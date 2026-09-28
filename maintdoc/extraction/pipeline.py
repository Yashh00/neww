"""Per-document extraction with page-wise streaming, checkpoints and resumability.

Every page of every processed document receives a row in ``pages`` with an
extraction status - pages are never silently discarded. Each page is written
in its own transaction together with its checkpoint, so an interrupted run
resumes at the next unfinished page.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymupdf

from maintdoc import audit
from maintdoc.config import Config
from maintdoc.constants import (BlockType, EvidenceStatus, ExtractionMethod, ExtractionStatus, PageStatus,
                                Severity, SourceStatus, VisualCheckStatus)
from maintdoc.db import open_db, row, transaction
from maintdoc.errors import error_fingerprint, record_error
from maintdoc.evidence import make_evidence_id, upsert_evidence
from maintdoc.extraction.figures import find_figures, image_coverage, image_regions, render_region
from maintdoc.extraction.layout import LayoutAnalyzer, Unit, hf_template, page_lines
from maintdoc.extraction.ocr import configure_tesseract, ocr_page
from maintdoc.extraction.quality import is_gibberish, text_quality
from maintdoc.extraction.tables import extract_tables, should_detect, table_text
from maintdoc.invalidation import supersede_evidence
from maintdoc.onedrive import detect_placeholder
from maintdoc.utils import (bbox_center_inside, bbox_intersection, bbox_area, dumps, loads, norm_hash, normalize_text,
                            now_iso, sha256_file, sha256_text)

log = logging.getLogger(__name__)

pymupdf.TOOLS.mupdf_display_errors(False)
pymupdf.TOOLS.mupdf_display_warnings(False)
if hasattr(pymupdf, "no_recommend_layout"):
    pymupdf.no_recommend_layout()

EXTRACTION_STAGES = ("text", "table", "ocr", "quality")


@dataclass
class PageResult:
    page_no: int
    status: str
    units: list[Unit] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)       # page-level errors
    unit_errors: list[tuple[int, dict[str, Any]]] = field(default_factory=list)  # (unit index, error)
    visual: list[dict[str, Any]] = field(default_factory=list)       # visual check requests


@dataclass
class DocContext:
    body_size: float
    hf_templates: set[str]


# --------------------------------------------------------------------------- document context
def analyse_document(doc: pymupdf.Document, cfg: Config) -> DocContext:
    n = doc.page_count
    ratio = float(cfg.get("extraction.header_footer_margin_ratio", 0.07))
    templates: Counter = Counter()
    sizes: Counter = Counter()
    sample = set(range(n)) if n <= 40 else {int(i * n / 40) for i in range(40)}
    for i in range(n):
        try:
            page = doc.load_page(i)
        except Exception:  # noqa: BLE001 - reported during page extraction
            continue
        h = page.rect.height
        m = h * ratio
        seen = set()
        for b in page.get_text("blocks"):
            if b[6] != 0:
                continue
            if b[3] <= m + 2 or b[1] >= h - m - 2:
                for line in str(b[4]).splitlines():
                    t = hf_template(line)
                    if t:
                        seen.add(t)
        templates.update(seen)
        if i in sample:
            for ln in page_lines(page):
                sizes[round(ln.size * 2) / 2] += len(ln.text)
    min_pages = int(cfg.get("extraction.header_footer_min_pages", 3))
    min_repeat = max(min_pages, math.ceil(float(cfg.get("extraction.header_footer_min_repeat_ratio", 0.3)) * n))
    hf = {t for t, c in templates.items() if c >= min_repeat} if n >= min_pages else set()
    body = float(sizes.most_common(1)[0][0]) if sizes else 10.0
    return DocContext(body_size=body, hf_templates=hf)


# --------------------------------------------------------------------------- page processing
class PageProcessor:
    def __init__(self, cfg: Config, ctx: DocContext, source: dict, figures_dir: Path, ocr_available: bool,
                 ocr_reason: str):
        self.cfg = cfg
        self.ctx = ctx
        self.source = source
        self.figures_dir = figures_dir
        self.ocr_available = ocr_available and bool(cfg.get("ocr.enabled", True))
        self.ocr_reason = ocr_reason if cfg.get("ocr.enabled", True) else "OCR disabled in configuration"
        self.la = LayoutAnalyzer(cfg, ctx.body_size, ctx.hf_templates)
        self.gib_thr = float(cfg.get("extraction.gibberish_threshold", 0.45))
        self.gib_min = int(cfg.get("extraction.gibberish_min_chars", 20))

    def process(self, doc: pymupdf.Document, plumber_getter, index: int) -> PageResult:
        page_no = index + 1
        t0 = time.monotonic()
        try:
            page = doc.load_page(index)
        except Exception as exc:  # noqa: BLE001
            return PageResult(page_no, PageStatus.FAILED, errors=[
                {"code": "PAGE_LOAD_FAILED", "stage": "text", "message": f"Page could not be loaded: {exc}"}],
                visual=[{"kind": "failed_page", "reason": "page could not be loaded - compare with original"}])
        try:
            res = self._process_page(page, plumber_getter, page_no)
        except Exception as exc:  # noqa: BLE001 - never lose a page silently
            log.exception("Extraction failed on %s page %d", self.source["rel_path"], page_no)
            res = PageResult(page_no, PageStatus.FAILED, errors=[
                {"code": "PAGE_EXTRACTION_FAILED", "stage": "text",
                 "message": f"Unexpected extraction failure: {type(exc).__name__}: {exc}"}],
                visual=[{"kind": "failed_page", "reason": "extraction failed - compare with original"}])
            res.info.update(width=page.rect.width, height=page.rect.height, rotation=page.rotation)
        res.info["duration_ms"] = int((time.monotonic() - t0) * 1000)
        return res

    def _process_page(self, page: pymupdf.Page, plumber_getter, page_no: int) -> PageResult:
        cfg = self.cfg
        rect = page.rect
        res = PageResult(page_no, PageStatus.OK)
        info = res.info
        info.update(width=rect.width, height=rect.height, rotation=page.rotation)

        # ---- native text
        units = self.la.units_from_lines(page_lines(page), rect)
        self.la.mark_headers_footers(units, rect.height)
        content = [u for u in units if u.block_type != BlockType.HEADER_FOOTER]
        content_text = "\n".join(u.text for u in content)
        content_chars = len(content_text.strip())
        quality, qflags = text_quality(content_text) if content_chars else (0.0, ["empty"])
        info.update(native_chars=content_chars, native_quality=quality)

        # ---- graphics
        images = image_regions(page)
        coverage = image_coverage(page, images)
        try:
            drawings = page.get_drawings()
        except Exception as exc:  # noqa: BLE001
            drawings = []
            res.errors.append({"code": "PAGE_EXTRACTION_FAILED", "stage": "text", "severity": Severity.WARNING,
                               "message": f"Vector drawings could not be read: {exc}"})
        info.update(image_count=len(images), image_coverage=round(coverage, 3), drawing_count=len(drawings))

        # ---- OCR decision
        need_ocr, ocr_why, mixed = self._ocr_decision(content, content_chars, quality, images, coverage, drawings)
        method = "native" if content_chars else "none"
        ocr_units: list[Unit] = []
        if need_ocr:
            info["ocr_reason"] = ocr_why
            if not self.ocr_available:
                res.status = PageStatus.OCR_UNAVAILABLE
                res.errors.append({"code": "OCR_UNAVAILABLE", "stage": "ocr",
                                   "message": f"Page needs OCR ({ocr_why}) but OCR is unavailable: {self.ocr_reason}"})
                res.visual.append({"kind": "ocr_page", "reason": f"OCR unavailable ({ocr_why}); content not extracted"})
            else:
                ocr = ocr_page(page, cfg)
                info.update(ocr_confidence=ocr.confidence, ocr_word_count=ocr.word_count,
                            ocr_low_conf_words=ocr.low_conf_words, ocr_preprocess=ocr.preprocess)
                if ocr.error:
                    res.status = PageStatus.OCR_FAILED
                    res.errors.append({"code": "OCR_FAILED", "stage": "ocr", "message": f"OCR failed: {ocr.error}"})
                    res.visual.append({"kind": "ocr_page", "reason": "OCR failed"})
                elif not ocr.units:
                    res.errors.append({"code": "OCR_NO_TEXT", "stage": "ocr",
                                       "message": f"OCR found no text ({ocr_why})"})
                    res.visual.append({"kind": "graphic_only", "reason": "OCR found no text on image page"})
                    res.status = PageStatus.GRAPHIC_ONLY if not content_chars else PageStatus.OK
                else:
                    ocr_units = ocr.units
                    method = "native+ocr" if content_chars else "ocr"
                    if mixed:
                        for u in ocr_units:
                            inside_img = any(bbox_center_inside(u.bbox, r) for r in images)
                            overlaps = any(bbox_intersection(u.bbox, n.bbox) > 0.4 * max(1.0, bbox_area(u.bbox))
                                           for n in units)
                            if not inside_img or overlaps:
                                u.status = EvidenceStatus.EXCLUDED
                                u.status_reason = "OCR text duplicates native text layer"
                    elif content_chars:
                        for u in content:
                            u.status = EvidenceStatus.EXCLUDED
                            u.status_reason = f"native text layer replaced by OCR ({ocr_why})"
                    page_low = float(cfg.get("ocr.page_low_confidence", 70.0))
                    if ocr.confidence is not None and ocr.confidence < page_low:
                        res.status = PageStatus.OCR_LOW_CONFIDENCE
                        res.errors.append({"code": "OCR_LOW_CONFIDENCE", "stage": "ocr",
                                           "message": f"OCR confidence {ocr.confidence:.1f} < {page_low:.0f}",
                                           "details": {"confidence": ocr.confidence,
                                                       "low_conf_words": ocr.low_conf_words}})
                    else:
                        res.status = PageStatus.OK_MIXED if mixed else PageStatus.OK_OCR
                    res.visual.append({"kind": "ocr_page",
                                       "reason": f"OCR page ({ocr_why}); confidence {ocr.confidence}"})
        info["extraction_method"] = method

        all_units = units + ocr_units
        # ---- tables (native pages)
        table_bboxes: list[tuple] = []
        do_tables, why = should_detect(cfg, drawings)
        if why == "too_complex":
            res.errors.append({"code": "TABLE_DETECTION_SKIPPED", "stage": "table",
                               "message": f"{len(drawings)} vector objects exceed tables.max_drawings_for_detection"})
            res.visual.append({"kind": "complex_table", "reason": "very complex page; tables not extracted"})
        info["table_status"] = why
        if do_tables and content_chars:
            tables, terrs = extract_tables(page, plumber_getter(page_no - 1), cfg)
            for code, msg, det in terrs:
                res.errors.append({"code": code, "stage": "table", "message": msg, "details": det})
            if terrs:
                res.visual.append({"kind": "table_failure", "reason": "table extraction failed; text kept as blocks"})
                info["table_status"] = "failed"
            for t in tables:
                tu = Unit(text=table_text(t.rows), bbox=t.bbox, block_type=BlockType.TABLE, method=t.method,
                          table_rows=t.rows, bbox_approx=t.bbox_approx)
                tu.extra["irregular"] = t.irregular
                all_units.append(tu)
                t_idx = len(all_units) - 1
                table_bboxes.append(t.bbox)
                for i, u in enumerate(all_units[:-1]):
                    if u.block_type in (BlockType.HEADER_FOOTER, BlockType.TABLE) or u.status != EvidenceStatus.ACTIVE:
                        continue
                    if bbox_center_inside(u.bbox, t.bbox, pad=2.0):
                        u.block_type = BlockType.TABLE_TEXT
                        u.heading_level = None
                        u.safety_level = None
                        u.status = EvidenceStatus.EXCLUDED
                        u.covered_by = t_idx
                        u.status_reason = "text is part of an extracted table"
                complex_cols = int(cfg.get("tables.complex_table_min_cols", 7))
                if t.irregular or t.n_cols >= complex_cols:
                    res.unit_errors.append((t_idx, {"code": "TABLE_IRREGULAR", "stage": "table",
                                                    "message": f"Table {t.n_rows}x{t.n_cols} with irregular/merged "
                                                               f"cells or many columns; visual check required"}))
                    res.visual.append({"kind": "complex_table", "unit": t_idx,
                                       "reason": f"irregular or wide table ({t.n_rows}x{t.n_cols})"})
            info["table_count"] = len(tables)

        # ---- figures
        figs = find_figures(page, drawings, images, table_bboxes, cfg,
                            skip_full_page_images=bool(ocr_units) or need_ocr)
        fig_count = 0
        for k, f in enumerate(figs, start=1):
            fu = Unit(text="", bbox=f.bbox, block_type=BlockType.FIGURE, method=f.method)
            all_units.append(fu)
            f_idx = len(all_units) - 1
            labels = []
            for i, u in enumerate(all_units[:-1]):
                if u.status != EvidenceStatus.ACTIVE or u.block_type not in (BlockType.PARAGRAPH, BlockType.LIST_ITEM):
                    continue
                if bbox_center_inside(u.bbox, f.bbox) and len(u.text) < 80:
                    u.block_type = BlockType.FIGURE_TEXT
                    u.status = EvidenceStatus.EXCLUDED
                    u.status_reason = "label inside figure region"
                    u.covered_by = f_idx
                    labels.append(u.text)
            cap_idx = self._nearest_caption(all_units, f.bbox, want_table=False)
            if cap_idx is not None:
                all_units[cap_idx].related = f_idx
                fu.related = cap_idx
                fu.text = f"[FIGURE] {all_units[cap_idx].text}"
            else:
                fu.text = f"[FIGURE] (no caption detected on page {page_no})"
            if labels:
                fu.extra["labels"] = labels
            if cfg.get("figures.render", True):
                out = self.figures_dir / f"{self.source['source_id']}_{self.source['sha256'][:8]}_p{page_no:04d}_f{k}.png"
                try:
                    render_region(page, f.bbox, out, int(cfg.get("figures.render_dpi", 150)))
                    fu.figure_path = str(out)
                except Exception as exc:  # noqa: BLE001
                    res.unit_errors.append((f_idx, {"code": "FIGURE_RENDER_FAILED", "stage": "text",
                                                    "message": f"Figure could not be rendered: {exc}"}))
            res.visual.append({"kind": "figure", "unit": f_idx,
                               "reason": f"figure/diagram ({f.method}); verify against source"})
            fig_count += 1
        # table captions
        for i, u in enumerate(all_units):
            if u.block_type == BlockType.TABLE:
                cap = self._nearest_caption(all_units, u.bbox, want_table=True)
                if cap is not None and all_units[cap].related is None:
                    all_units[cap].related = i
                    u.related = cap
        info["figure_count"] = fig_count

        # ---- reading order
        ordered, multi = self.la.order(all_units, rect)
        ordered = self.la.merge_signal_only(ordered)
        # remap related/covered indices from positions in all_units to positions in ordered
        pos = {id(u): i for i, u in enumerate(ordered)}
        for u in ordered:
            if u.related is not None:
                target = all_units[u.related]
                u.related = pos.get(id(target))
            if u.covered_by is not None:
                target = all_units[u.covered_by]
                u.covered_by = pos.get(id(target))
        for v in res.visual:
            if "unit" in v:
                v["unit"] = pos.get(id(all_units[v["unit"]]))
        res.unit_errors = [(pos.get(id(all_units[i])), e) for i, e in res.unit_errors]
        info["multi_column"] = int(multi)
        if multi:
            res.visual.append({"kind": "multi_column", "reason": "multi-column layout; verify reading order"})
        res.units = ordered

        # ---- per-unit quality
        for i, u in enumerate(ordered):
            if u.status != EvidenceStatus.ACTIVE or u.block_type in (BlockType.FIGURE, BlockType.TABLE):
                continue
            gib, score, flags = is_gibberish(u.text, self.gib_thr, self.gib_min)
            u.quality_score = score
            u.quality_flags = sorted(set(u.quality_flags + flags))
            if gib:
                res.unit_errors.append((i, {"code": "GIBBERISH_TEXT", "stage": "quality",
                                            "message": f"Text quality score {score:.2f}: {u.text[:60]!r}",
                                            "details": {"flags": flags}}))
            if u.method == ExtractionMethod.TESSERACT_OCR and u.ocr_confidence is not None and \
                    u.ocr_confidence < float(cfg.get("ocr.block_low_confidence", 60.0)):
                res.unit_errors.append((i, {"code": "OCR_LOW_CONFIDENCE_BLOCK", "stage": "ocr",
                                            "message": f"OCR block confidence {u.ocr_confidence:.1f}",
                                            "details": {"confidence": u.ocr_confidence}}))

        # ---- final page status for non-OCR pages
        active_content = [u for u in ordered if u.status == EvidenceStatus.ACTIVE
                          and u.block_type != BlockType.HEADER_FOOTER]
        if not need_ocr:
            if not active_content:
                if images or drawings:
                    res.status = PageStatus.GRAPHIC_ONLY
                    res.errors.append({"code": "GRAPHIC_ONLY_PAGE", "stage": "text",
                                       "message": f"No text; {len(images)} image(s), {len(drawings)} drawing(s)"})
                    res.visual.append({"kind": "graphic_only", "reason": "graphics without text"})
                else:
                    res.status = PageStatus.EMPTY
                    res.errors.append({"code": "EMPTY_PAGE", "stage": "text",
                                       "message": "Page has no text, images or drawings"})
            elif any(self.la.is_blank_notice(u.text) for u in active_content) and len(active_content) <= 2:
                res.status = PageStatus.BLANK_INTENTIONAL
            elif quality < float(cfg.get("ocr.native_quality_threshold", 0.45)):
                res.status = PageStatus.LOW_QUALITY_TEXT
                res.errors.append({"code": "LOW_QUALITY_PAGE_TEXT", "stage": "quality",
                                   "message": f"Native text quality {quality:.2f} ({', '.join(qflags)}) and no OCR "
                                              f"replacement ({self.ocr_reason if not self.ocr_available else 'OCR mode'})"})
                res.visual.append({"kind": "low_quality_text", "reason": "text layer looks corrupted"})
        info["heading_count"] = sum(1 for u in ordered if u.block_type == BlockType.HEADING)
        info["warning_count"] = sum(1 for u in ordered if u.block_type in BlockType.SAFETY)
        return res

    def _ocr_decision(self, content, content_chars, quality, images, coverage, drawings) -> tuple[bool, str, bool]:
        cfg = self.cfg
        mode = cfg.get("ocr.mode", "auto")
        if not cfg.get("ocr.enabled", True) and mode != "always":
            mode = "auto"  # still report pages that would need OCR
        if mode == "never":
            return False, "", False
        if mode == "always":
            return True, "ocr.mode=always", bool(content_chars)
        min_chars = int(cfg.get("ocr.min_native_chars", 25))
        if content_chars < min_chars:
            if coverage >= float(cfg.get("ocr.min_image_coverage", 0.25)):
                return True, "no usable text layer on image page", False
            if len(drawings) >= int(cfg.get("ocr.min_vector_paths_for_ocr", 400)):
                return True, "no text layer on vector page (outlined text?)", False
            return False, "", False
        if quality < float(cfg.get("ocr.native_quality_threshold", 0.45)):
            return True, f"low-quality native text (score {quality:.2f})", False
        if cfg.get("ocr.mixed_page_ocr", True) and coverage >= float(cfg.get("ocr.mixed_min_image_coverage", 0.35)):
            chars_in_images = sum(len(u.text) for u in content if any(bbox_center_inside(u.bbox, r) for r in images))
            if chars_in_images < 50:  # images are not already covered by a (searchable) text layer
                return True, "large images without text layer on text page", True
        return False, "", False

    @staticmethod
    def _nearest_caption(units: list[Unit], bbox, want_table: bool, max_dist: float = 60.0) -> int | None:
        best = None
        for i, u in enumerate(units):
            if u.block_type != BlockType.CAPTION or u.related is not None:
                continue
            is_table_cap = u.text.strip().lower().startswith(("table", "tab."))
            if is_table_cap != want_table:
                continue
            h_overlap = min(u.bbox[2], bbox[2]) - max(u.bbox[0], bbox[0])
            if h_overlap <= 0:
                continue
            dist = u.bbox[1] - bbox[3] if u.bbox[1] >= bbox[3] - 2 else bbox[1] - u.bbox[3]
            if -2 <= dist <= max_dist and (best is None or dist < best[0]):
                best = (dist, i)
        return best[1] if best else None


# --------------------------------------------------------------------------- persistence
def _page_evidence(res: PageResult, source: dict, run_id: str, stack: list[list]) -> tuple[list[dict], list[str]]:
    """Assign evidence IDs, section context and build rows. Mutates the section stack."""
    ids: list[str] = []
    rows: list[dict] = []
    ts = now_iso()
    for seq, u in enumerate(res.units, start=1):
        ids.append(make_evidence_id(source["source_id"], source["sha256"], res.page_no, seq, u.method, u.text))
    for seq, u in enumerate(res.units, start=1):
        eid = ids[seq - 1]
        if u.block_type == BlockType.HEADING and u.status == EvidenceStatus.ACTIVE:
            level = u.heading_level or 3
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append([level, u.text.strip()[:200], eid])
        section_path = " > ".join(s[1] for s in stack) if stack else None
        section_heading = stack[-1][1] if stack else None
        section_eid = stack[-1][2] if stack else None
        text = u.text
        rows.append({
            "evidence_id": eid, "source_id": source["source_id"], "source_sha256": source["sha256"],
            "page_no": res.page_no, "seq": seq, "block_type": u.block_type, "text": text,
            "norm_text": normalize_text(text), "norm_hash": norm_hash(text), "heading_level": u.heading_level,
            "section_path": section_path, "section_heading": section_heading, "section_evidence_id": section_eid,
            "bbox_x0": round(u.bbox[0], 2), "bbox_y0": round(u.bbox[1], 2), "bbox_x1": round(u.bbox[2], 2),
            "bbox_y1": round(u.bbox[3], 2), "bbox_approx": int(u.bbox_approx), "extraction_method": u.method,
            "ocr_confidence": u.ocr_confidence, "font_size": u.font_size or None, "is_bold": int(u.is_bold),
            "column_index": u.column, "table_json": dumps(u.table_rows) if u.table_rows is not None else None,
            "figure_path": u.figure_path,
            "related_evidence_id": ids[u.related] if u.related is not None else (
                ids[u.covered_by] if u.covered_by is not None else None),
            "safety_level": u.safety_level, "quality_score": u.quality_score,
            "quality_flags": dumps(u.quality_flags) if u.quality_flags else None,
            "status": u.status, "status_reason": u.status_reason, "created_at": ts, "run_id": run_id,
        })
    return rows, ids


def _write_page(conn: sqlite3.Connection, res: PageResult, source: dict, run_id: str, stack: list[list],
                config_hash: str, page_count: int, cfg: Config) -> dict[str, int]:
    rows, ids = _page_evidence(res, source, run_id, stack)
    sid, sha, pno = source["source_id"], source["sha256"], res.page_no
    counts = {"evidence": len(rows), "errors": 0}
    with transaction(conn):
        old = {r[0] for r in conn.execute(
            "SELECT evidence_id FROM evidence WHERE source_id=? AND page_no=? AND status != ?",
            (sid, pno, EvidenceStatus.SUPERSEDED))}
        stale = sorted(old - set(ids))
        if stale:
            supersede_evidence(conn, stale, f"page {pno} re-extracted with different result", run_id)
        upsert_evidence(conn, rows)
        # errors (page + unit level); resolve previously open ones not re-detected
        fps = set()
        for e in res.errors:
            record_error(conn, e["code"], e["message"], stage=e["stage"], source_id=sid, source_sha256=sha,
                         page_no=pno, severity=e.get("severity"), details=e.get("details"), run_id=run_id)
            fps.add(error_fingerprint(e["code"], sid, sha, pno, None, None))
        for idx, e in res.unit_errors:
            eid = ids[idx] if idx is not None and idx < len(ids) else None
            record_error(conn, e["code"], e["message"], stage=e["stage"], source_id=sid, source_sha256=sha,
                         page_no=pno, evidence_id=eid, details=e.get("details"), run_id=run_id)
            fps.add(error_fingerprint(e["code"], sid, sha, pno, eid, None))
        counts["errors"] = len(fps)
        q = ",".join("?" * len(EXTRACTION_STAGES))
        for r in conn.execute(f"SELECT error_id, fingerprint FROM extraction_errors WHERE source_id=? AND page_no=? "
                              f"AND status='open' AND stage IN ({q})", (sid, pno, *EXTRACTION_STAGES)).fetchall():
            if r["fingerprint"] not in fps:
                conn.execute("UPDATE extraction_errors SET status='resolved', status_by='system', status_at=?, "
                             "status_comment='not re-detected on re-extraction' WHERE error_id=?",
                             (now_iso(), r["error_id"]))
        # visual checks
        for v in res.visual:
            eid = ids[v["unit"]] if v.get("unit") is not None and v["unit"] < len(ids) else None
            check_id = "VC-" + sha256_text(f"{sid}|{sha}|{pno}|{v['kind']}|{eid}")[:16]
            conn.execute(
                "INSERT INTO visual_checks(check_id, target_type, source_id, source_sha256, page_no, evidence_id, "
                "kind, reason, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(check_id) DO UPDATE SET reason=excluded.reason, "
                "status=CASE WHEN visual_checks.status='invalidated' THEN 'pending' ELSE visual_checks.status END",
                (check_id, "source_page", sid, sha, pno, eid, v["kind"], v["reason"], VisualCheckStatus.PENDING,
                 now_iso()))
        page_text = "\n".join(u.text for u in res.units if u.status == EvidenceStatus.ACTIVE)
        info = res.info
        conn.execute(
            """INSERT OR REPLACE INTO pages(source_id, page_no, source_sha256, width, height, rotation, native_chars,
                native_quality, image_count, image_coverage, drawing_count, extraction_method, extraction_status,
                status_detail, ocr_confidence, ocr_word_count, ocr_low_conf_words, ocr_preprocess, table_count,
                table_status, figure_count, heading_count, warning_count, evidence_count, multi_column, page_text,
                text_sha256, needs_visual_check, visual_check_reason, error_count, duration_ms, processed_at, run_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sid, pno, sha, info.get("width"), info.get("height"), info.get("rotation"), info.get("native_chars"),
             info.get("native_quality"), info.get("image_count"), info.get("image_coverage"),
             info.get("drawing_count"), info.get("extraction_method", "none"), res.status, info.get("ocr_reason"),
             info.get("ocr_confidence"), info.get("ocr_word_count"), info.get("ocr_low_conf_words"),
             info.get("ocr_preprocess"), info.get("table_count", 0), info.get("table_status"),
             info.get("figure_count", 0), info.get("heading_count", 0), info.get("warning_count", 0), len(rows),
             info.get("multi_column", 0), page_text if cfg.get("extraction.store_page_text", True) else None,
             sha256_text(page_text), int(bool(res.visual)), "; ".join(sorted({v["kind"] for v in res.visual})) or None,
             counts["errors"], info.get("duration_ms"), now_iso(), run_id))
        conn.execute(
            "INSERT INTO extraction_checkpoints(source_id, source_sha256, config_hash, page_count, last_page_done, "
            "state_json, status, updated_at, run_id) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source_id) DO UPDATE SET source_sha256=excluded.source_sha256, config_hash=excluded.config_hash, "
            "page_count=excluded.page_count, last_page_done=excluded.last_page_done, state_json=excluded.state_json, "
            "status=excluded.status, updated_at=excluded.updated_at, run_id=excluded.run_id",
            (sid, sha, config_hash, page_count, pno, dumps({"stack": stack}), "in_progress", now_iso(), run_id))
    return counts


# --------------------------------------------------------------------------- document driver
def extract_source(cfg: Config, source_id: str, run_id: str, force: bool = False,
                   db_path: str | None = None) -> dict[str, Any]:
    """Extract one source document. Safe to call in a worker process."""
    conn = open_db(db_path or cfg.db_path)
    try:
        return _extract(conn, cfg, source_id, run_id, force)
    finally:
        conn.close()


def _fail_source(conn, source: dict, run_id: str, code: str, message: str, page_count: int | None) -> dict:
    sid, sha = source["source_id"], source["sha256"]
    with transaction(conn):
        record_error(conn, code, message, stage="text", source_id=sid, source_sha256=sha, run_id=run_id)
        # every page still gets an extraction status
        for pno in range(1, (page_count or 0) + 1):
            conn.execute("INSERT OR REPLACE INTO pages(source_id, page_no, source_sha256, extraction_method, "
                         "extraction_status, status_detail, processed_at, run_id) VALUES(?,?,?,?,?,?,?,?)",
                         (sid, pno, sha, "none", PageStatus.FAILED, message[:500], now_iso(), run_id))
        conn.execute("UPDATE sources SET extraction_status=?, last_run_id=?, problem_pages=? WHERE source_id=?",
                     (ExtractionStatus.FAILED, run_id, page_count or 0, sid))
        audit.append(conn, "system", "source.extraction_failed", "source", sid, after={"error": message}, run_id=run_id)
    return {"source_id": sid, "status": ExtractionStatus.FAILED, "error": message}


def _extract(conn: sqlite3.Connection, cfg: Config, source_id: str, run_id: str, force: bool) -> dict[str, Any]:
    source = row(conn, "SELECT * FROM sources WHERE source_id=?", (source_id,))
    if source is None:
        raise KeyError(source_id)
    if source["status"] != SourceStatus.OK:
        return {"source_id": source_id, "status": "skipped", "reason": source["status"]}
    path = Path(source["abs_path"])
    t0 = time.monotonic()
    # the file must still be local and unchanged since inventory
    try:
        st = path.stat()
    except OSError as exc:
        return _fail_source(conn, source, run_id, "FILE_UNREADABLE", f"Cannot access file: {exc}", source["page_count"])
    if cfg.get("onedrive.detect_placeholders", True) and detect_placeholder(
            st, bool(cfg.get("onedrive.treat_zero_allocated_blocks_as_placeholder", True))).is_placeholder:
        with transaction(conn):
            record_error(conn, "OFFLINE_PLACEHOLDER", "File became an online-only placeholder after inventory",
                         stage="text", source_id=source_id, source_sha256=source["sha256"], run_id=run_id)
        return {"source_id": source_id, "status": "skipped", "reason": "offline_placeholder"}
    if sha256_file(path) != source["sha256"]:
        with transaction(conn):
            record_error(conn, "SOURCE_CHANGED", "File changed after inventory; run 'inventory' again before extraction",
                         stage="text", source_id=source_id, source_sha256=source["sha256"], run_id=run_id,
                         key="changed_after_inventory")
            conn.execute("UPDATE sources SET extraction_status=? WHERE source_id=?",
                         (ExtractionStatus.PENDING, source_id))
        return {"source_id": source_id, "status": "skipped", "reason": "changed_since_inventory"}

    config_hash = cfg.extraction_fingerprint()
    try:
        doc = pymupdf.open(str(path))
    except Exception as exc:  # noqa: BLE001
        return _fail_source(conn, source, run_id, "CORRUPT_PDF", f"PDF could not be opened for extraction: {exc}",
                            source["page_count"])
    plumber = {"pdf": None, "failed": False, "page": None}

    def plumber_getter(index: int):
        if plumber["failed"]:
            return None
        if plumber["pdf"] is None:
            try:
                import pdfplumber
                plumber["pdf"] = pdfplumber.open(str(path))
            except Exception as exc:  # noqa: BLE001
                log.warning("pdfplumber cannot open %s: %s", path, exc)
                plumber["failed"] = True
                return None
        try:
            if plumber["page"] is not None:
                plumber["page"].close()
            plumber["page"] = plumber["pdf"].pages[index]
            return plumber["page"]
        except Exception as exc:  # noqa: BLE001
            log.warning("pdfplumber cannot load page %d of %s: %s", index + 1, path, exc)
            return None

    try:
        n = doc.page_count
        ckpt = row(conn, "SELECT * FROM extraction_checkpoints WHERE source_id=?", (source_id,))
        start = 1
        stack: list[list] = []
        if (not force and ckpt and ckpt["source_sha256"] == source["sha256"] and ckpt["config_hash"] == config_hash
                and ckpt["status"] == "in_progress" and ckpt["last_page_done"] < n):
            start = int(ckpt["last_page_done"]) + 1
            stack = (loads(ckpt["state_json"], {}) or {}).get("stack", [])
            log.info("Resuming %s at page %d/%d", source["rel_path"], start, n)
        with transaction(conn):
            conn.execute("UPDATE sources SET extraction_status=?, last_run_id=? WHERE source_id=?",
                         (ExtractionStatus.IN_PROGRESS, run_id, source_id))
        ctx = analyse_document(doc, cfg)
        ocr_ok, ocr_msg = configure_tesseract(cfg) if cfg.get("ocr.enabled", True) else (False, "OCR disabled")
        proc = PageProcessor(cfg, ctx, source, cfg.figures_dir, ocr_ok, ocr_msg)
        for index in range(start - 1, n):
            res = proc.process(doc, plumber_getter, index)
            _write_page(conn, res, source, run_id, stack, config_hash, n, cfg)
            if index % 20 == 19:
                pymupdf.TOOLS.store_shrink(100)
                log.info("%s: %d/%d pages", source["rel_path"], index + 1, n)
        # finalize
        with transaction(conn):
            beyond = [r[0] for r in conn.execute(
                "SELECT evidence_id FROM evidence WHERE source_id=? AND page_no>? AND status != ?",
                (source_id, n, EvidenceStatus.SUPERSEDED))]
            if beyond:
                supersede_evidence(conn, beyond, "page no longer exists", run_id)
            conn.execute("DELETE FROM pages WHERE source_id=? AND page_no>?", (source_id, n))
            stats = {r["extraction_status"]: r["c"] for r in conn.execute(
                "SELECT extraction_status, COUNT(*) c FROM pages WHERE source_id=? GROUP BY extraction_status",
                (source_id,))}
            ocr_pages = conn.execute("SELECT COUNT(*) FROM pages WHERE source_id=? AND extraction_method LIKE '%ocr%'",
                                     (source_id,)).fetchone()[0]
            problem = sum(c for s, c in stats.items() if s in PageStatus.PROBLEM)
            serious = conn.execute(
                "SELECT COUNT(*) FROM extraction_errors WHERE source_id=? AND source_sha256=? AND status='open' "
                "AND severity IN ('critical','error')", (source_id, source["sha256"])).fetchone()[0]
            failed_pages = stats.get(PageStatus.FAILED, 0)
            if n and failed_pages == n:
                status = ExtractionStatus.FAILED
            elif problem or serious:
                status = ExtractionStatus.COMPLETE_WITH_ERRORS
            else:
                status = ExtractionStatus.COMPLETE
            conn.execute("UPDATE sources SET extraction_status=?, extracted_sha256=?, extraction_config_hash=?, "
                         "pages_extracted=?, ocr_pages=?, problem_pages=?, last_run_id=? WHERE source_id=?",
                         (status, source["sha256"], config_hash, n, ocr_pages, problem, run_id, source_id))
            conn.execute("UPDATE extraction_checkpoints SET status='complete', updated_at=? WHERE source_id=?",
                         (now_iso(), source_id))
            audit.append(conn, "system", "source.extracted", "source", source_id,
                         after={"status": status, "pages": n, "page_status": stats, "sha256": source["sha256"]},
                         run_id=run_id)
        return {"source_id": source_id, "status": status, "pages": n, "resumed_from": start,
                "page_status": stats, "ocr_pages": ocr_pages, "seconds": round(time.monotonic() - t0, 2)}
    finally:
        if plumber["pdf"] is not None:
            try:
                plumber["pdf"].close()
            except Exception:  # noqa: BLE001
                pass
        doc.close()
