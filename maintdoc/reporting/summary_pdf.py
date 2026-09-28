"""Processing_Summary.pdf - one-document overview of a processing campaign."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from maintdoc.config import Config
from maintdoc.constants import DRAFT_DISCLAIMER, TOOL_VERSION
from maintdoc.generate.fonts import register_fonts
from maintdoc.generate.pdf_writer import esc
from maintdoc.utils import now_iso

COLOR = {"pass": "#C6EFCE", "fail": "#FFC7CE", "warn": "#FFEB9C", "pending_human": "#BDD7EE"}


def write_summary_pdf(conn: sqlite3.Connection, cfg: Config, path: Path, report=None) -> Path:
    font, bold, _ = register_fonts(cfg.get("generation.pdf_font_paths", []))
    base = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=base["Heading1"], fontName=bold, fontSize=15)
    h2 = ParagraphStyle("h2", parent=base["Heading2"], fontName=bold, fontSize=11)
    body = ParagraphStyle("b", parent=base["Normal"], fontName=font, fontSize=9, leading=11.5)
    cell = ParagraphStyle("c", parent=base["Normal"], fontName=font, fontSize=7.5, leading=9)
    disc = ParagraphStyle("d", parent=body, fontName=bold, textColor=colors.HexColor("#b00020"))
    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=16 * mm,
                            bottomMargin=16 * mm, title="Processing Summary", author="maintdoc")
    width = doc.width

    def table(data: list[list], widths: list[float] | None = None, status_col: int | None = None) -> Table:
        t = Table([[Paragraph(esc(str(v if v is not None else "")), cell) for v in r] for r in data],
                  colWidths=widths or [width / max(1, len(data[0]))] * len(data[0]), repeatRows=1)
        style = [("GRID", (0, 0), (-1, -1), 0.3, colors.grey), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dddddd")),
                 ("VALIGN", (0, 0), (-1, -1), "TOP")]
        if status_col is not None:
            for i, r in enumerate(data[1:], start=1):
                c = COLOR.get(str(r[status_col]).lower())
                if c:
                    style.append(("BACKGROUND", (status_col, i), (status_col, i), colors.HexColor(c)))
        t.setStyle(TableStyle(style))
        return t

    def grp(sql: str) -> list[list]:
        return [list(r) for r in conn.execute(sql)]

    story = [Paragraph("Processing Summary", h1),
             Paragraph(f"{esc(cfg.get('project.name', ''))} - generated {now_iso()} by maintdoc {TOOL_VERSION}", body),
             Paragraph(f"Source root: {esc(str(cfg.source_root))}", body), Spacer(1, 4),
             Paragraph(esc(DRAFT_DISCLAIMER), disc), Spacer(1, 8)]
    if report is not None:
        story += [Paragraph(f"Verification result: {esc(report.overall)}", h2),
                  table([["#", "Acceptance criterion", "Status", "Detail"]] +
                        [[a["no"], a["criterion"], a["status"], a["detail"]] for a in report.acceptance],
                        [width * x for x in (0.05, 0.35, 0.13, 0.47)], status_col=2), Spacer(1, 8)]
    story += [Paragraph("Sources", h2),
              table([["Status", "Extraction status", "Files"]] +
                    grp("SELECT status, extraction_status, COUNT(*) FROM sources GROUP BY 1,2 ORDER BY 1,2")),
              Paragraph("Pages", h2),
              table([["Page extraction status", "Method", "Pages"]] +
                    grp("SELECT extraction_status, extraction_method, COUNT(*) FROM pages GROUP BY 1,2 ORDER BY 1,2")),
              Paragraph("OCR", h2),
              table([["OCR pages", "Mean confidence", "Min confidence", "Pages below threshold"]] +
                    [[r[0], f"{r[1]:.1f}" if r[1] is not None else "-", f"{r[2]:.1f}" if r[2] is not None else "-", r[3]]
                     for r in conn.execute(
                        "SELECT COUNT(*), AVG(ocr_confidence), MIN(ocr_confidence), SUM(extraction_status="
                        "'ocr_low_confidence') FROM pages WHERE extraction_method LIKE '%ocr%'")]),
              Paragraph("Evidence and review", h2),
              table([["Block type", "Status", "Items"]] +
                    grp("SELECT block_type, status, COUNT(*) FROM evidence GROUP BY 1,2 ORDER BY 1,2")),
              Spacer(1, 4),
              table([["Manual statements by chapter", "Review status", "Items"]] +
                    grp("SELECT COALESCE(chapter_override, chapter), review_status, COUNT(*) FROM evidence WHERE "
                        "status='active' AND in_manual=1 GROUP BY 1,2 ORDER BY 1,2")),
              Paragraph("Duplicates", h2),
              table([["Kind", "Status", "Count"]] +
                    grp("SELECT 'exact group', 'automatic (same text, same context)', COUNT(*) FROM duplicate_groups") +
                    grp("SELECT 'near-duplicate candidate', status, COUNT(*) FROM near_duplicates GROUP BY 2")),
              Paragraph("Conflicts", h2),
              table([["Type", "Severity", "Status", "Count"]] +
                    grp("SELECT conflict_type, severity, status, COUNT(*) FROM conflicts GROUP BY 1,2,3 ORDER BY 2,1")),
              Paragraph("Extraction error register", h2),
              table([["Error code", "Severity", "Status", "Count"]] +
                    grp("SELECT error_code, severity, status, COUNT(*) FROM extraction_errors GROUP BY 1,2,3 "
                        "ORDER BY 2,1")),
              Paragraph("Visual checks", h2),
              table([["Kind", "Status", "Count"]] +
                    grp("SELECT kind, status, COUNT(*) FROM visual_checks GROUP BY 1,2 ORDER BY 1,2")),
              Paragraph("Generated outputs (latest)", h2),
              table([["Kind", "Mode", "Status", "Items", "Path"]] +
                    grp("SELECT kind, mode, status, item_count, path FROM generated_outputs WHERE output_id IN "
                        "(SELECT MAX(output_id) FROM generated_outputs GROUP BY kind, mode) ORDER BY mode, kind"),
                    [width * x for x in (0.15, 0.1, 0.1, 0.08, 0.57)]),
              Paragraph("Runs", h2),
              table([["Run", "Command", "Started", "Status", "Dry run"]] +
                    grp("SELECT run_id, command, started_at, status, dry_run FROM runs ORDER BY started_at DESC "
                        "LIMIT 15"))]
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.build(story)
    return path
