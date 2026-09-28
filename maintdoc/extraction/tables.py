"""Table detection and extraction: pdfplumber first, PyMuPDF ``find_tables`` as fallback."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pymupdf

from maintdoc.constants import ExtractionMethod

log = logging.getLogger(__name__)


@dataclass
class TableResult:
    bbox: tuple[float, float, float, float]
    rows: list[list[str | None]]
    method: str
    n_rows: int
    n_cols: int
    filled_ratio: float
    irregular: bool = False
    bbox_approx: bool = False
    notes: list[str] = field(default_factory=list)


def count_line_segments(drawings: list[dict]) -> int:
    n = 0
    for d in drawings:
        for item in d.get("items", []):
            op = item[0]
            if op == "l":
                p1, p2 = item[1], item[2]
                if abs(p1.x - p2.x) < 1 or abs(p1.y - p2.y) < 1:  # horizontal / vertical rulings
                    n += 1
            elif op == "re":
                n += 4
    return n


def should_detect(cfg, drawings: list[dict]) -> tuple[bool, str]:
    mode = cfg.get("tables.mode", "auto")
    if mode == "never":
        return False, "tables.mode=never"
    if len(drawings) > int(cfg.get("tables.max_drawings_for_detection", 4000)):
        return False, "too_complex"
    if mode == "always":
        return True, "tables.mode=always"
    n = count_line_segments(drawings)
    if n >= int(cfg.get("tables.auto_min_line_segments", 4)):
        return True, f"{n} ruling segments"
    return False, "no ruling lines"


def _clean(rows) -> list[list[str | None]]:
    out = []
    for r in rows or []:
        out.append([None if c is None else " ".join(str(c).split()) for c in r])
    return out


def _evaluate(rows, cfg) -> tuple[bool, int, int, float, bool]:
    n_rows = len(rows)
    n_cols = max((len(r) for r in rows), default=0)
    cells = n_rows * n_cols or 1
    filled = sum(1 for r in rows for c in r if c not in (None, ""))
    ratio = filled / cells
    ok = (n_rows >= int(cfg.get("tables.min_rows", 2)) and n_cols >= int(cfg.get("tables.min_cols", 2))
          and ratio >= float(cfg.get("tables.min_filled_ratio", 0.3)))
    irregular = any(len(r) != n_cols for r in rows) or any(c is None for r in rows for c in r)
    return ok, n_rows, n_cols, ratio, irregular


def _to_fitz_rect(bbox, page: pymupdf.Page) -> tuple[tuple[float, float, float, float], bool]:
    """pdfplumber coordinates (unrotated, top-left of mediabox) -> PyMuPDF page coordinates."""
    r = pymupdf.Rect(bbox)
    approx = False
    cb = page.cropbox_position
    if cb.x or cb.y:
        r = r - (cb.x, cb.y, cb.x, cb.y)
        approx = True
    if page.rotation:
        r = r * page.rotation_matrix
        r.normalize()
        approx = True
    return (r.x0, r.y0, r.x1, r.y1), approx


def extract_tables(page: pymupdf.Page, plumber_page, cfg) -> tuple[list[TableResult], list[tuple[str, str, dict]]]:
    """Return (tables, errors). Errors are (code, message, details) tuples."""
    errors: list[tuple[str, str, dict]] = []
    tables: list[TableResult] = []
    primary_failed = False
    if plumber_page is not None:
        try:
            for t in plumber_page.find_tables():
                rows = _clean(t.extract())
                ok, nr, nc, ratio, irregular = _evaluate(rows, cfg)
                if not ok:
                    continue
                bbox, approx = _to_fitz_rect(t.bbox, page)
                tables.append(TableResult(bbox, rows, ExtractionMethod.PDFPLUMBER_TABLE, nr, nc, ratio,
                                          irregular, approx))
        except Exception as exc:  # noqa: BLE001 - library failures are recorded, not fatal
            primary_failed = True
            errors.append(("TABLE_EXTRACTION_FAILED", f"pdfplumber table extraction failed: {exc}",
                           {"library": "pdfplumber"}))
    if (plumber_page is None or primary_failed) and cfg.get("tables.fallback_pymupdf", True):
        try:
            for t in page.find_tables().tables:
                rows = _clean(t.extract())
                ok, nr, nc, ratio, irregular = _evaluate(rows, cfg)
                if not ok:
                    continue
                tables.append(TableResult(tuple(t.bbox), rows, ExtractionMethod.PYMUPDF_TABLE, nr, nc, ratio,
                                          irregular, False))
        except Exception as exc:  # noqa: BLE001
            errors.append(("TABLE_EXTRACTION_FAILED", f"PyMuPDF table extraction failed: {exc}",
                           {"library": "pymupdf"}))
    return tables, errors


def table_text(rows: list[list[str | None]]) -> str:
    return "\n".join(" | ".join("" if c is None else c for c in r) for r in rows)
