"""Regression tests for large outputs (hundreds of revisions citing the same statement, huge tables)."""

from __future__ import annotations

import time

import pymupdf
from docx import Document

from maintdoc.generate.docx_writer import write_docx
from maintdoc.generate.manual import Chapter, Citation, Item, Manual, Topic
from maintdoc.generate.pdf_writer import write_pdf
from maintdoc.verify import _body_text, squash

from conftest import REPO


def _manual() -> Manual:
    cites = [Citation(f"EV-{i:05d}-P0001-008-abcdef", f"SRC-{i:05d}", f"manual_{i}.pdf", 1, "2 Safety", "0" * 64,
                      "pymupdf_text", None, "unreviewed", "B") for i in range(1, 401)]
    warn = Item("EV-00001-P0001-008-abcdef", "admonition", "warning",
                "WARNING: Depressurize the hydraulic system before opening any fitting.", "same", cites,
                safety_level="warning")
    rows = [["Part No.", "Description", "Qty"]] + [[f"P-{i:05d}", f"Spare part {i}", "1"] for i in range(1000)]
    table = Item("EV-00001-P0002-001-abcdef", "table", "table", "tbl", "tbl", cites[:1], table_rows=rows)
    ch = Chapter("safety", "Safety Instructions", [Topic("2 Safety", ["EV-00001-P0001-001-abcdef"], "HP-200",
                                                         [warn, table])])
    index = {c.evidence_id: c for c in cites}
    return Manual("draft", "Scale test", "", "2026-01-01T00:00:00+00:00", "RUN-x", "cfg", [ch], [], index, [],
                  {"items": 2}, [])


def test_long_citation_lists_and_big_tables(tmp_path):
    from maintdoc.config import Config
    cfg = Config.load(REPO / "config.yaml")
    m = _manual()
    t0 = time.monotonic()
    pdf = write_pdf(m, tmp_path / "m.pdf", cfg)
    docx = write_docx(m, tmp_path / "m.docx", cfg)
    assert time.monotonic() - t0 < 120  # linear-time table construction
    with pymupdf.open(str(pdf)) as d:
        body = squash("\n".join(_body_text(p) for p in d))
    assert squash(m.chapters[0].topics[0].items[0].citation_text()) in body  # 400 citations, across pages
    assert "P-00999" in body
    assert any(len(t.rows) == 1001 for t in Document(str(docx)).tables)
