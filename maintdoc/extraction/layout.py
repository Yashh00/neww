"""Native-text layout analysis on top of PyMuPDF.

Turns PyMuPDF text dictionaries into ordered *units* (headings, paragraphs,
list items, warnings, captions, headers/footers) with source coordinates,
font information and reading order (column aware).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import pymupdf

from maintdoc.constants import BlockType, EvidenceStatus, ExtractionMethod

TEXT_FLAGS = (pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES) | pymupdf.TEXT_MEDIABOX_CLIP

LIST_RE = re.compile(r"^\s*(?:\(?\d{1,3}[.)]|\(?[a-z][.)]|step\s+\d{1,3}\s*[:.)]?|[•●▪■◦–*·\-])\s+\S",
                     re.IGNORECASE)
NUMBERED_HEADING_RE = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2}){0,4})\.?\s+[^\s\d]")
PAGE_NUMBER_RE = re.compile(r"^\s*(?:page\s*)?[-–]?\s*\d{1,4}\s*(?:(?:of|/)\s*\d{1,4})?\s*[-–]?\s*$",
                            re.IGNORECASE)


@dataclass
class Unit:
    text: str
    bbox: tuple[float, float, float, float]
    block_type: str = BlockType.PARAGRAPH
    font_size: float = 0.0
    is_bold: bool = False
    method: str = ExtractionMethod.PYMUPDF_TEXT
    ocr_confidence: float | None = None
    heading_level: int | None = None
    safety_level: str | None = None
    column: int = 0
    table_rows: list[list[str | None]] | None = None
    figure_path: str | None = None
    bbox_approx: bool = False
    status: str = EvidenceStatus.ACTIVE
    status_reason: str | None = None
    related: int | None = None          # index of related unit (caption <-> figure/table)
    covered_by: int | None = None       # index of table/figure unit that covers this text
    quality_score: float | None = None
    quality_flags: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Line:
    text: str
    bbox: tuple[float, float, float, float]
    size: float
    bold: bool
    block_no: int


def _span_bold(span: dict) -> bool:
    font = (span.get("font") or "").lower()
    return bool(span.get("flags", 0) & 16) or any(k in font for k in ("bold", "black", "heavy", "semibold"))


def page_lines(page: pymupdf.Page) -> list[Line]:
    d = page.get_text("dict", flags=TEXT_FLAGS)
    out: list[Line] = []
    for b in d.get("blocks", []):
        if b.get("type", 0) != 0:
            continue
        for ln in b.get("lines", []):
            spans = [s for s in ln.get("spans", []) if s.get("text")]
            if not spans:
                continue
            text = "".join(s["text"] for s in spans)
            if not text.strip():
                continue
            nchar = sum(len(s["text"].strip()) or 1 for s in spans)
            size = sum(s.get("size", 0) * (len(s["text"].strip()) or 1) for s in spans) / max(1, nchar)
            bold_chars = sum(len(s["text"].strip()) for s in spans if _span_bold(s))
            out.append(Line(text=text.rstrip(), bbox=tuple(ln["bbox"]), size=round(size, 2),
                            bold=bold_chars >= 0.6 * max(1, sum(len(s["text"].strip()) for s in spans)),
                            block_no=b.get("number", 0)))
    return out


def body_font_size(sizes: Counter) -> float:
    if not sizes:
        return 10.0
    return float(sizes.most_common(1)[0][0])


def hf_template(text: str) -> str:
    t = re.sub(r"\d+", "#", text.lower())
    return re.sub(r"\s+", " ", t).strip()


class LayoutAnalyzer:
    """Stateless per-page analysis; document-level context is passed in."""

    def __init__(self, cfg, body_size: float, hf_templates: set[str]):
        ex = cfg.get("extraction")
        self.cfg = cfg
        self.body_size = body_size or 10.0
        self.hf_templates = hf_templates
        self.heading_ratio = float(ex.get("heading_size_ratio", 1.15))
        self.heading_max = int(ex.get("heading_max_chars", 120))
        self.margin_ratio = float(ex.get("header_footer_margin_ratio", 0.07))
        self.multi_col = bool(ex.get("multi_column_detection", True))
        self.caption_re = re.compile(ex.get("caption_pattern"), re.IGNORECASE)
        self.blank_re = re.compile(ex.get("blank_page_pattern"))
        words: list[tuple[str, str]] = []
        for level, ws in (ex.get("signal_words") or {}).items():
            for w in ws:
                words.append((w, level))
        self.signal_map = {w.upper(): lvl for w, lvl in words}
        # no configured signal words -> a pattern that can never match
        alt = "|".join(re.escape(w) for w, _ in sorted(words, key=lambda x: -len(x[0]))) or r"(?!x)x"
        # uppercase signal word, or any-case signal word followed by a colon
        self.signal_re = re.compile(rf"^\s*(?:(?P<up>{alt})\b|(?i:(?P<any>{alt}))\s*:)\s*[:!\-–—]?\s*")

    # ------------------------------------------------------------------ helpers
    def signal_of(self, text: str) -> tuple[str | None, bool]:
        """Return (safety level, signal-word-only)."""
        m = self.signal_re.match(text)
        if not m:
            return None, False
        word = (m.group("up") or m.group("any")).upper()
        level = self.signal_map.get(word)
        rest = text[m.end():].strip()
        return level, not rest

    def _is_heading_line(self, ln: Line) -> tuple[bool, int | None]:
        text = ln.text.strip()
        if len(text) > self.heading_max or len(text) < 2:
            return False, None
        if LIST_RE.match(text) and not ln.bold and ln.size < self.body_size * self.heading_ratio:
            return False, None
        if self.signal_re.match(text) or self.caption_re.match(text):
            return False, None
        larger = ln.size >= self.body_size * self.heading_ratio
        m = NUMBERED_HEADING_RE.match(text)
        ends_sentence = text.endswith((".", ";", ",")) and not m
        if ends_sentence and not larger:
            return False, None
        if not (larger or ln.bold):
            return False, None
        if not larger and len(text.split()) > 12:
            return False, None
        if m and (ln.bold or larger):
            return True, min(6, m.group(1).count(".") + 1)
        ratio = ln.size / self.body_size if self.body_size else 1
        level = 1 if ratio >= 1.5 else 2 if ratio >= 1.25 else 3
        return True, level

    def in_margin(self, bbox, page_h: float) -> bool:
        m = page_h * self.margin_ratio
        return bbox[3] <= m + 2 or bbox[1] >= page_h - m - 2

    # ------------------------------------------------------------------ segmentation
    def units_from_lines(self, lines: list[Line], page_rect: pymupdf.Rect) -> list[Unit]:
        units: list[Unit] = []
        cur: dict[str, Any] | None = None

        def close():
            nonlocal cur
            if cur is None:
                return
            text = _join_lines(cur["lines"])
            if text.strip():
                bb = (min(l.bbox[0] for l in cur["lines"]), min(l.bbox[1] for l in cur["lines"]),
                      max(l.bbox[2] for l in cur["lines"]), max(l.bbox[3] for l in cur["lines"]))
                nch = sum(len(l.text) for l in cur["lines"]) or 1
                size = sum(l.size * len(l.text) for l in cur["lines"]) / nch
                u = Unit(text=text, bbox=bb, block_type=cur["kind"], font_size=round(size, 2),
                         is_bold=all(l.bold for l in cur["lines"]), heading_level=cur.get("level"))
                if cur["kind"] in BlockType.ADMONITIONS:
                    u.safety_level = cur["kind"]
                    if cur.get("signal_only"):
                        u.extra["signal_only"] = True
                units.append(u)
            cur = None

        prev: Line | None = None
        for ln in lines:
            text = ln.text.strip()
            is_head, level = self._is_heading_line(ln)
            sig, sig_only = self.signal_of(text)
            if sig:
                kind = sig
            elif is_head:
                kind = BlockType.HEADING
            elif self.caption_re.match(text) and len(text) < 200:
                kind = BlockType.CAPTION
            elif LIST_RE.match(text):
                kind = "list_start"
            else:
                kind = "body"
            new_block = prev is None or ln.block_no != prev.block_no
            gap = (ln.bbox[1] - prev.bbox[3]) if prev else 0
            big_gap = prev is not None and gap > max(ln.size, prev.size) * 0.9
            if cur is None:
                start = True
            elif kind in (BlockType.HEADING,) and cur["kind"] == BlockType.HEADING and not new_block and not big_gap \
                    and abs(ln.size - prev.size) < 0.6:
                start = False  # wrapped multi-line heading
            elif kind in ("list_start", BlockType.CAPTION, BlockType.HEADING) or sig:
                start = True
            elif cur["kind"] == BlockType.HEADING:
                start = True
            elif new_block or big_gap:
                start = True
            elif cur.get("signal_only"):
                start = False
            else:
                start = False
            if start:
                close()
                if kind == "list_start":
                    ukind = BlockType.LIST_ITEM
                elif kind == "body":
                    ukind = BlockType.PARAGRAPH
                else:
                    ukind = kind
                cur = {"kind": ukind, "lines": [ln], "level": level if ukind == BlockType.HEADING else None,
                       "signal_only": bool(sig and sig_only)}
            else:
                cur["lines"].append(ln)
                if cur.get("signal_only"):
                    cur["signal_only"] = False
            prev = ln
        close()
        return units

    # ------------------------------------------------------------------ post-processing
    def mark_headers_footers(self, units: list[Unit], page_h: float) -> None:
        for u in units:
            if not self.in_margin(u.bbox, page_h):
                continue
            t = u.text.strip()
            if PAGE_NUMBER_RE.match(t) or hf_template(t) in self.hf_templates:
                u.block_type = BlockType.HEADER_FOOTER
                u.heading_level = None
                u.safety_level = None

    def order(self, units: list[Unit], page_rect: pymupdf.Rect) -> tuple[list[Unit], bool]:
        """Column-aware reading order. Returns (ordered units, multi_column_detected)."""
        if not units:
            return units, False
        units = sorted(units, key=lambda u: (round(u.bbox[1], 1), u.bbox[0]))
        if not self.multi_col:
            return units, False
        width = page_rect.width
        mid = page_rect.x0 + width / 2
        full = [u for u in units if (u.bbox[2] - u.bbox[0]) > 0.55 * width or
                (u.bbox[0] < mid - 5 and u.bbox[2] > mid + 5)]
        full_ids = {id(u) for u in full}
        ordered: list[Unit] = []
        band: list[Unit] = []
        multi = False

        def flush():
            nonlocal band, multi
            if not band:
                return
            left = [u for u in band if (u.bbox[0] + u.bbox[2]) / 2 < mid]
            right = [u for u in band if (u.bbox[0] + u.bbox[2]) / 2 >= mid]
            if left and right and len(band) >= 2:
                y_overlap = min(max(u.bbox[3] for u in left), max(u.bbox[3] for u in right)) - \
                    max(min(u.bbox[1] for u in left), min(u.bbox[1] for u in right))
                if y_overlap > 0:
                    multi = True
                    for u in left:
                        u.column = 1
                    for u in right:
                        u.column = 2
                    ordered.extend(sorted(left, key=lambda u: u.bbox[1]))
                    ordered.extend(sorted(right, key=lambda u: u.bbox[1]))
                    band = []
                    return
            ordered.extend(sorted(band, key=lambda u: (round(u.bbox[1], 1), u.bbox[0])))
            band = []

        for u in units:
            if id(u) in full_ids:
                flush()
                ordered.append(u)
            else:
                band.append(u)
        flush()
        return ordered, multi

    def merge_signal_only(self, units: list[Unit]) -> list[Unit]:
        """'WARNING' on its own line + following paragraph -> one warning unit."""
        out: list[Unit] = []
        skip = set()
        for i, u in enumerate(units):
            if i in skip:
                continue
            if u.extra.get("signal_only"):
                for j in range(i + 1, min(i + 3, len(units))):
                    nxt = units[j]
                    if nxt.block_type in (BlockType.PARAGRAPH, BlockType.LIST_ITEM) and nxt.status == "active":
                        u.text = f"{u.text.strip()} {nxt.text.strip()}"
                        u.bbox = (min(u.bbox[0], nxt.bbox[0]), min(u.bbox[1], nxt.bbox[1]),
                                  max(u.bbox[2], nxt.bbox[2]), max(u.bbox[3], nxt.bbox[3]))
                        u.extra.pop("signal_only", None)
                        skip.add(j)
                        break
            out.append(u)
        return out

    def is_blank_notice(self, text: str) -> bool:
        return bool(self.blank_re.search(text))


def _join_lines(lines: list[Line]) -> str:
    """Join lines with single spaces; keep end-of-line hyphens without adding a space.

    All characters are preserved exactly; only line-break whitespace is normalised.
    """
    out = ""
    for ln in lines:
        t = ln.text.strip()
        if not out:
            out = t
        elif out.endswith("-") and t[:1].islower():
            out += t
        else:
            out += " " + t
    return out
