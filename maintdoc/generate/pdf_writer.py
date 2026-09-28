"""PDF manual writer (ReportLab platypus) with bookmarks, contents and citation index."""

from __future__ import annotations

import logging
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm
from reportlab.platypus import (BaseDocTemplate, Frame, Image, PageBreak, PageTemplate, Paragraph, Spacer, Table,
                                TableStyle)
from reportlab.platypus.tableofcontents import TableOfContents

from maintdoc.constants import TOOL_VERSION
from maintdoc.generate.fonts import register_fonts
from maintdoc.generate.manual import Item, Manual

log = logging.getLogger(__name__)

ADMONITION_COLORS = {"danger": "#f8d7da", "warning": "#ffe5cc", "caution": "#fff3cd", "notice": "#dbe9ff",
                     "note": "#eeeeee"}


def esc(text: str | None) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class _ManualDoc(BaseDocTemplate):
    def __init__(self, path: str, manual: Manual, font: str, **kw):
        super().__init__(path, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=22 * mm,
                         bottomMargin=20 * mm, title=f"{manual.title} ({manual.mode.upper()})",
                         author="maintdoc (rule-based, not released)", subject="Draft maintenance manual", **kw)
        self.manual = manual
        self.font = font
        self._seq = 0
        frame = Frame(self.leftMargin, self.bottomMargin, self.width, self.height, id="body")
        self.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=self._decorate)])

    def beforeDocument(self) -> None:
        # bookmark keys must be identical on every multiBuild pass or the TOC never converges
        self._seq = 0

    def _decorate(self, canv, doc) -> None:
        canv.saveState()
        canv.setFont(self.font, 7.5)
        banner = "DRAFT - NOT RELEASED" if self.manual.mode == "draft" else \
            "APPROVED CONTENT - NOT RELEASED UNTIL FORMAL ENGINEERING RELEASE"
        canv.setFillColor(colors.HexColor("#b00020"))
        canv.drawString(self.leftMargin, A4[1] - 12 * mm, banner)
        canv.setFillColor(colors.black)
        canv.drawRightString(A4[0] - self.rightMargin, A4[1] - 12 * mm, self.manual.title[:80])
        canv.drawString(self.leftMargin, 10 * mm, f"Generated {self.manual.generated_at} by maintdoc {TOOL_VERSION} "
                                                  f"- every technical statement carries an evidence citation")
        canv.drawRightString(A4[0] - self.rightMargin, 10 * mm, f"Page {doc.page}")
        canv.restoreState()

    def afterFlowable(self, flowable) -> None:
        if isinstance(flowable, Paragraph) and flowable.style.name in ("H1", "H2"):
            level = 0 if flowable.style.name == "H1" else 1
            text = flowable.getPlainText()
            key = f"bm{self._seq}"
            self._seq += 1
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(text[:120], key, level=level, closed=level > 0)
            self.notify("TOCEntry", (level, text, self.page, key))


def _styles(font: str, bold: str) -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    s = {
        "Title": ParagraphStyle("Title", parent=base["Title"], fontName=bold, fontSize=22, leading=27),
        "Sub": ParagraphStyle("Sub", parent=base["Normal"], fontName=bold, fontSize=13, leading=17,
                              alignment=TA_CENTER, textColor=colors.HexColor("#b00020")),
        "H1": ParagraphStyle("H1", parent=base["Heading1"], fontName=bold, fontSize=15, leading=19, spaceBefore=6,
                             spaceAfter=6),
        "H2": ParagraphStyle("H2", parent=base["Heading2"], fontName=bold, fontSize=11.5, leading=14, spaceBefore=8,
                             spaceAfter=2),
        "H3": ParagraphStyle("H3", parent=base["Heading3"], fontName=bold, fontSize=10, leading=12, spaceBefore=6),
        "Body": ParagraphStyle("Body", parent=base["Normal"], fontName=font, fontSize=9.5, leading=12.5,
                               spaceAfter=4),
        "Step": ParagraphStyle("Step", parent=base["Normal"], fontName=font, fontSize=9.5, leading=12.5,
                               leftIndent=10, spaceAfter=3),
        "Cite": ParagraphStyle("Cite", parent=base["Normal"], fontName=font, fontSize=7, leading=8.5,
                               textColor=colors.HexColor("#555555")),
        "Small": ParagraphStyle("Small", parent=base["Normal"], fontName=font, fontSize=7.5, leading=9),
        "Cell": ParagraphStyle("Cell", parent=base["Normal"], fontName=font, fontSize=8, leading=9.5),
        "CellB": ParagraphStyle("CellB", parent=base["Normal"], fontName=bold, fontSize=8, leading=9.5),
        "Disc": ParagraphStyle("Disc", parent=base["Normal"], fontName=bold, fontSize=9, leading=12,
                               textColor=colors.HexColor("#b00020")),
    }
    return s


def _cite_markup(item: Item) -> str:
    return f' <font size="7" color="#555555">{esc(item.citation_text())}</font>'


def _flags_markup(item: Item) -> str:
    notes = list(item.flags) + [f"CONFLICT {c}" for c in item.conflicts]
    if not notes:
        return ""
    return f' <font size="7" color="#b00020">{{{esc("; ".join(notes))}}}</font>'


TABLE_CHUNK_ROWS = 150


def _table(rows: list[list], widths: list[float], st: dict, header: bool = True, font_small: bool = False) -> list:
    """Table flowables. Long tables are split into chunks (header repeated) because splitting a
    single huge ReportLab table across pages is quadratic."""
    cell = st["Small"] if font_small else st["Cell"]
    style = TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#888888")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8") if header else colors.white),
        ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ])

    def para(c, bold: bool) -> Paragraph:
        return Paragraph(esc("" if c is None else str(c)), st["CellB"] if bold else cell)

    head = [para(c, True) for c in rows[0]] if header and rows else None
    body = rows[1:] if head is not None else rows
    out = []
    for i in range(0, max(1, len(body)), TABLE_CHUNK_ROWS):
        chunk = [[para(c, False) for c in r] for r in body[i:i + TABLE_CHUNK_ROWS]]
        data = ([head] if head is not None else []) + chunk
        if not data:
            continue
        t = Table(data, colWidths=widths, repeatRows=1 if head is not None else 0, splitInRow=1)
        t.setStyle(style)
        out.append(t)
    return out


def _item_flowables(item: Item, st: dict, width: float, cfg) -> list:
    out: list = []
    if item.kind == "admonition":
        # a styled paragraph (not a one-cell table) so very long citation lists can split across pages
        color = ADMONITION_COLORS.get(item.safety_level or item.block_type, "#eeeeee")
        strong = item.block_type in ("danger", "warning")
        style = ParagraphStyle(f"Adm_{item.block_type}", parent=st["Body"], backColor=colors.HexColor(color),
                               borderColor=colors.HexColor("#b00020" if strong else "#888888"),
                               borderWidth=1.0 if strong else 0.6, borderPadding=5, spaceBefore=6, spaceAfter=8,
                               leftIndent=5, rightIndent=5)
        out.append(Paragraph(esc(item.text) + _cite_markup(item) + _flags_markup(item), style))
    elif item.kind == "table" and item.table_rows:
        ncols = max(len(r) for r in item.table_rows) or 1
        rows = [list(r) + [None] * (ncols - len(r)) for r in item.table_rows]
        if item.caption:
            out.append(Paragraph(esc(item.caption), st["H3"]))
        out.extend(_table(rows, [width / ncols] * ncols, st))
        out.append(Paragraph(esc(item.citation_text()) + _flags_markup(item), st["Cite"]))
        out.append(Spacer(1, 5))
    elif item.kind == "figure":
        path = item.figure_path
        if path and Path(path).exists():
            try:
                img = Image(path)
                max_w = min(width, float(cfg.get("generation.figure_max_width_cm", 15.0)) * cm)
                scale = min(1.0, max_w / img.imageWidth, (9 * cm) / img.imageHeight)
                img.drawWidth, img.drawHeight = img.imageWidth * scale, img.imageHeight * scale
                out.append(img)
            except Exception as exc:  # noqa: BLE001
                out.append(Paragraph(f"[Figure image could not be embedded: {esc(str(exc))}]", st["Cite"]))
        else:
            out.append(Paragraph("[Figure image not available - see source page]", st["Cite"]))
        out.append(Paragraph(esc(item.caption or "(figure without caption)") + _cite_markup(item) +
                             _flags_markup(item), st["Small"]))
        out.append(Paragraph("Figure reproduced from the source page; human visual check required.", st["Cite"]))
        out.append(Spacer(1, 6))
    else:
        style = st["Step"] if item.kind == "list_item" else st["Body"]
        out.append(Paragraph(esc(item.text) + _cite_markup(item) + _flags_markup(item), style))
    return out


def write_pdf(manual: Manual, path: Path, cfg) -> Path:
    font, bold, unicode_ok = register_fonts(cfg.get("generation.pdf_font_paths", []))
    st = _styles(font, bold)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.pdf")
    doc = _ManualDoc(str(tmp), manual, font)
    width = doc.width
    story: list = []
    mode_txt = "DRAFT - NOT RELEASED" if manual.mode == "draft" else \
        "APPROVED CONTENT - NOT RELEASED UNTIL FORMAL ENGINEERING RELEASE"
    story += [Spacer(1, 3 * cm), Paragraph(esc(manual.title), st["Title"]), Paragraph(mode_txt, st["Sub"]),
              Spacer(1, 1 * cm)]
    if manual.organisation:
        story.append(Paragraph(esc(manual.organisation), st["Body"]))
    story += [Paragraph(f"Generated: {esc(manual.generated_at)} - maintdoc {TOOL_VERSION} - run {esc(manual.run_id)} "
                        f"- configuration {esc(manual.config_hash)}", st["Body"]),
              Spacer(1, 0.6 * cm), Paragraph(esc(manual.disclaimer), st["Disc"]), Spacer(1, 0.6 * cm)]
    stats_rows = [["Measure", "Value"]] + [[k.replace("_", " "), str(v)] for k, v in manual.stats.items()]
    story += [*_table(stats_rows, [width * 0.6, width * 0.4], st), PageBreak()]
    toc = TableOfContents()
    toc.levelStyles = [ParagraphStyle("T1", fontName=bold, fontSize=10, leading=13, leftIndent=0),
                       ParagraphStyle("T2", fontName=font, fontSize=8.5, leading=10.5, leftIndent=14)]
    story += [Paragraph("Contents", st["H3"]), toc, PageBreak()]
    # document control
    story.append(Paragraph("Document control", st["H1"]))
    story.append(Paragraph("Each statement below is reproduced verbatim from its source (or in reviewer wording that "
                           "preserves every number and unit) and is followed by its evidence citation "
                           "[EV-source-page-sequence-hash]. The citation index at the end maps each citation to file, "
                           "page, section, SHA-256 and extraction method. Figures and complex layouts require a human "
                           "visual check against the source pages.", st["Body"]))
    if manual.mode == "draft":
        story.append(Paragraph("Labels in braces {...} mark review status, OCR origin and open conflicts. "
                               "Unreviewed content must not be used.", st["Body"]))
    for n, ch in enumerate(manual.chapters, start=1):
        story.append(PageBreak())
        story.append(Paragraph(f"{n}. {esc(ch.title)}", st["H1"]))
        if not ch.topics:
            story.append(Paragraph("No approved content for this chapter." if manual.mode == "approved"
                                   else "No content was classified into this chapter.", st["Body"]))
        for t in ch.topics:
            story.append(Paragraph(esc(t.heading), st["H2"]))
            story.append(Paragraph(f"Applicability: {esc(t.applicability)} - section heading evidence: "
                                   f"{esc(', '.join(t.heading_citations) or 'none')}", st["Cite"]))
            last_src = None
            for item in t.items:
                if item.source_label != last_src and len({i.source_label for i in t.items}) > 1:
                    story.append(Paragraph(f"Source: {esc(item.source_label)}", st["Cite"]))
                    last_src = item.source_label
                story.extend(_item_flowables(item, st, width, cfg))
        for gt in ch.generated_tables:
            story.append(Paragraph(esc(gt["title"]), st["H3"]))
            cols = gt["columns"]
            rows = [cols] + [[r.get(c, "") for c in cols] for r in gt["rows"]]
            story.extend(_table(rows, [width / len(cols)] * len(cols), st, font_small=True))
    # appendices
    story.append(PageBreak())
    story.append(Paragraph("Appendix A - Conflicts" + (" (open - no value has been chosen automatically)"
                                                      if manual.mode == "draft" else " and engineering decisions"),
                           st["H1"]))
    if manual.conflicts:
        rows = [["Conflict", "Type / severity", "Status", "Description", "Resolution"]]
        for c in manual.conflicts:
            rows.append([c["conflict_id"], f"{c['conflict_type']} / {c['severity']}", c["status"], c["description"],
                         f"{c.get('resolution_type') or ''} {c.get('resolution') or ''} {c.get('resolved_by') or ''}"])
        story.extend(_table(rows, [width * x for x in (0.13, 0.14, 0.1, 0.43, 0.2)], st, font_small=True))
    else:
        story.append(Paragraph("None.", st["Body"]))
    story.append(Paragraph("Appendix B - Withheld items", st["H1"]))
    if manual.withheld:
        rows = [["Evidence", "Reason"]] + [[w["evidence_id"], w["reason"]] for w in manual.withheld]
        story.extend(_table(rows, [width * 0.3, width * 0.7], st, font_small=True))
    else:
        story.append(Paragraph("None.", st["Body"]))
    story.append(Paragraph("Appendix C - Citation index", st["H1"]))
    rows = [["Evidence ID", "Source / file", "Page", "Section", "Method / OCR", "Review", "SHA-256"]]
    for eid in sorted(manual.citation_index):
        c = manual.citation_index[eid]
        rows.append([eid, f"{c.source_id} {c.filename}" + (f" Rev {c.revision}" if c.revision else ""), str(c.page_no),
                     (c.section or "")[:120], f"{c.method}" + (f" {c.ocr_confidence}%" if c.ocr_confidence else ""),
                     c.review_status, c.sha256 if cfg.get("generation.full_sha_in_index", True) else c.sha256[:16]])
    story.extend(_table(rows, [width * x for x in (0.2, 0.2, 0.05, 0.17, 0.1, 0.08, 0.2)], st, font_small=True))
    doc.multiBuild(story)
    tmp.replace(path)
    return path
