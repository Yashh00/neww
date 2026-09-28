from __future__ import annotations

from pathlib import Path

import pytest

from maintdoc.constants import BlockType, EvidenceStatus, ExtractionStatus, PageStatus
from maintdoc.db import open_db, rows
from maintdoc.extraction import pipeline
from maintdoc.extraction.quality import text_quality
from maintdoc.extraction.runner import plan_extraction, run_extraction
from maintdoc.inventory import run_inventory
from maintdoc.testing.corpus import make_corpus

from conftest import HAS_TESSERACT, ev_by_text, make_config, requires_tesseract, source_id


def test_every_page_of_every_extracted_source_has_a_status(processed):
    w = processed
    for s in rows(w.conn, "SELECT * FROM sources WHERE status='ok'"):
        pages = rows(w.conn, "SELECT page_no, extraction_status FROM pages WHERE source_id=? ORDER BY page_no",
                     (s["source_id"],))
        assert [p["page_no"] for p in pages] == list(range(1, s["page_count"] + 1))
        assert all(p["extraction_status"] in PageStatus.ALL for p in pages)


def test_empty_page_is_registered_not_discarded(processed):
    w = processed
    sid = source_id(w, "HP-200_Maintenance_Manual_RevB.pdf")
    p5 = rows(w.conn, "SELECT * FROM pages WHERE source_id=? AND page_no=5", (sid,))[0]
    assert p5["extraction_status"] == PageStatus.EMPTY
    assert w.one("SELECT COUNT(*) FROM extraction_errors WHERE source_id=? AND page_no=5 AND error_code='EMPTY_PAGE'",
                 (sid,)) == 1


def test_truncated_pdf_pages_are_flagged(processed):
    w = processed
    sid = source_id(w, "truncated_manual")
    st = {r["page_no"]: r["extraction_status"] for r in rows(w.conn, "SELECT * FROM pages WHERE source_id=?", (sid,))}
    assert st and any(v == PageStatus.EMPTY for v in st.values())
    assert w.one("SELECT extraction_status FROM sources WHERE source_id=?", (sid,)) == \
        ExtractionStatus.COMPLETE_WITH_ERRORS


def test_headings_sections_and_warnings(processed):
    w = processed
    step = ev_by_text(w, "Tighten the coupling bolts to 45 Nm")
    assert step["block_type"] == BlockType.LIST_ITEM
    assert step["section_path"].endswith("5.1 Replacing the pump coupling")
    assert step["bbox_x0"] is not None and step["bbox_y1"] > step["bbox_y0"]
    warn = ev_by_text(w, "WARNING: Depressurize")
    assert warn["block_type"] == BlockType.WARNING and warn["safety_level"] == "warning"
    caution = ev_by_text(w, "CAUTION: Hydraulic oil")
    assert caution["block_type"] == BlockType.CAUTION
    head = ev_by_text(w, "5.1 Replacing the pump coupling", "AND block_type='heading'")
    assert head["heading_level"] == 2


def test_headers_and_footers_are_kept_but_marked(processed):
    w = processed
    hf = w.ev("block_type='header_footer' AND text LIKE 'Page % of %'")
    assert hf and all(h["status"] == EvidenceStatus.ACTIVE for h in hf)
    assert all(h["in_manual"] == 0 for h in hf)


def test_tables_extracted_and_covered_text_kept_as_excluded(processed):
    w = processed
    t = ev_by_text(w, "Maximum operating pressure | 210 | bar", "AND block_type='table'")
    assert t["extraction_method"] in ("pdfplumber_table", "pymupdf_table")
    assert '"210"' in t["table_json"]
    covered = w.ev("related_evidence_id=? AND block_type='table_text'", (t["evidence_id"],))
    assert covered and all(c["status"] == EvidenceStatus.EXCLUDED for c in covered)
    cap = w.ev("text='Table 1 - Technical data' AND source_id=?", (t["source_id"],))[0]
    assert cap["related_evidence_id"] == t["evidence_id"]


def test_figure_detected_rendered_and_linked_to_caption(processed):
    w = processed
    fig = w.ev("block_type='figure' AND status='active'")
    assert fig
    f = fig[0]
    assert f["text"].startswith("[FIGURE] Figure 1")
    assert f["figure_path"] and Path(f["figure_path"]).exists()
    assert w.one("SELECT COUNT(*) FROM visual_checks WHERE evidence_id=? AND kind='figure'", (f["evidence_id"],)) == 1


def test_two_column_reading_order(processed):
    w = processed
    sid = source_id(w, "Lubrication_Guide")
    texts = [r["text"] for r in rows(w.conn, "SELECT text FROM evidence WHERE source_id=? AND status='active' "
                                             "ORDER BY seq", (sid,))]
    order = [t.split(":")[0] for t in texts if "column" in t]
    assert order == ["Left column first", "Left column second", "Left column third",
                     "Right column first", "Right column second", "Right column third"]
    assert w.one("SELECT multi_column FROM pages WHERE source_id=?", (sid,)) == 1


def test_gibberish_is_flagged():
    score, flags = text_quality("Xqzvbn trkplm wqrtzx hjkgfd bnmvcx zxqwrt plkjhg")
    assert score < 0.45 and "implausible_words" in flags
    good, _ = text_quality("Tighten the coupling bolts to 45 Nm (33 ft-lb) using a torque wrench.")
    assert good > 0.8
    assert text_quality("(cid:12)(cid:44)(cid:77)(cid:12) (cid:33)(cid:12)")[0] < 0.45


def test_gibberish_evidence_has_error(processed):
    e = ev_by_text(processed, "Xqzvbn")
    assert processed.one("SELECT error_code FROM extraction_errors WHERE evidence_id=?", (e["evidence_id"],)) == \
        "GIBBERISH_TEXT"


def test_evidence_ids_unique_and_deterministic(own_sources):
    w = own_sources
    ids = [r[0] for r in w.conn.execute("SELECT evidence_id FROM evidence")]
    assert len(ids) == len(set(ids))
    before = {r[0] for r in w.conn.execute("SELECT evidence_id FROM evidence WHERE status!='superseded'")}
    run_extraction(w.conn, w.cfg, "RUN-force", force=True, workers=1, show_progress=False)
    after = {r[0] for r in w.conn.execute("SELECT evidence_id FROM evidence WHERE status!='superseded'")}
    assert before == after  # same content + settings -> same IDs, review state preserved


def test_unchanged_sources_are_skipped(processed):
    plan = plan_extraction(processed.conn, processed.cfg)
    assert not plan["extract"] and not plan["resume"]
    assert any(s["reason"] == "unchanged" for s in plan["skip"])


def test_resume_after_interruption(tmp_path, monkeypatch):
    make_corpus(tmp_path / "src", include_scanned=False)
    cfg = make_config(tmp_path, tmp_path / "src")
    conn = open_db(cfg.db_path)
    run_inventory(conn, cfg, "RUN-i", show_progress=False)
    sid = conn.execute("SELECT source_id FROM sources WHERE rel_path='HP-200/HP-200_Maintenance_Manual_RevB.pdf'"
                       ).fetchone()[0]
    real = pipeline.PageProcessor.process

    def crash_on_page_3(self, doc, getter, index):
        if index == 2:
            raise KeyboardInterrupt("simulated power loss")
        return real(self, doc, getter, index)

    monkeypatch.setattr(pipeline.PageProcessor, "process", crash_on_page_3)
    with pytest.raises(KeyboardInterrupt):
        pipeline.extract_source(cfg, sid, "RUN-crash")
    ck = rows(conn, "SELECT * FROM extraction_checkpoints WHERE source_id=?", (sid,))[0]
    assert ck["last_page_done"] == 2 and ck["status"] == "in_progress"
    monkeypatch.setattr(pipeline.PageProcessor, "process", real)
    plan = plan_extraction(conn, cfg, source_ids=[sid])
    assert plan["resume"] and plan["resume"][0]["resume_after_page"] == 2
    res = pipeline.extract_source(cfg, sid, "RUN-resume")
    assert res["resumed_from"] == 3
    assert conn.execute("SELECT COUNT(*) FROM pages WHERE source_id=?", (sid,)).fetchone()[0] == 5
    step = rows(conn, "SELECT section_path FROM evidence WHERE source_id=? AND text LIKE '4. Tighten%'", (sid,))[0]
    assert step["section_path"].endswith("5.1 Replacing the pump coupling")


def test_ocr_unavailable_is_an_explicit_page_status(tmp_path):
    make_corpus(tmp_path / "src", include_scanned=True)
    cfg = make_config(tmp_path, tmp_path / "src", ocr={"enabled": False})
    conn = open_db(cfg.db_path)
    run_inventory(conn, cfg, "RUN-i", show_progress=False)
    sid = conn.execute("SELECT source_id FROM sources WHERE rel_path LIKE 'Scanned/%'").fetchone()[0]
    pipeline.extract_source(cfg, sid, "RUN-noocr")
    page = rows(conn, "SELECT * FROM pages WHERE source_id=?", (sid,))[0]
    assert page["extraction_status"] == PageStatus.OCR_UNAVAILABLE
    err = rows(conn, "SELECT severity FROM extraction_errors WHERE source_id=? AND error_code='OCR_UNAVAILABLE'", (sid,))
    assert err and err[0]["severity"] == "critical"
    assert conn.execute("SELECT COUNT(*) FROM visual_checks WHERE source_id=? AND kind='ocr_page'", (sid,)
                        ).fetchone()[0] == 1


@requires_tesseract
def test_scanned_page_ocr(processed):
    w = processed
    sid = source_id(w, "Scanned/")
    page = rows(w.conn, "SELECT * FROM pages WHERE source_id=?", (sid,))[0]
    assert page["extraction_status"] == PageStatus.OK_OCR
    assert page["ocr_confidence"] > 80
    assert "deskew" in page["ocr_preprocess"]
    e = ev_by_text(w, "accumulator pre-charge pressure")
    assert e["extraction_method"] == "tesseract_ocr" and e["bbox_approx"] == 1
    assert "90 bar" in e["text"]
    # revision/date recovered from OCR text during validation, with provenance
    s = rows(w.conn, "SELECT revision, doc_date, metadata_origin_json FROM sources WHERE source_id=?", (sid,))[0]
    assert s["revision"] == "A" and s["doc_date"] == "2021-11-05"
    assert "page_text(ocr)" in s["metadata_origin_json"]


@requires_tesseract
def test_low_ocr_confidence_is_flagged(tmp_path):
    make_corpus(tmp_path / "src", include_scanned=True)
    cfg = make_config(tmp_path, tmp_path / "src", ocr={"page_low_confidence": 99.9, "retry_below_confidence": 0})
    conn = open_db(cfg.db_path)
    run_inventory(conn, cfg, "RUN-i", show_progress=False)
    sid = conn.execute("SELECT source_id FROM sources WHERE rel_path LIKE 'Scanned/%'").fetchone()[0]
    pipeline.extract_source(cfg, sid, "RUN-low")
    assert conn.execute("SELECT extraction_status FROM pages WHERE source_id=?", (sid,)).fetchone()[0] == \
        PageStatus.OCR_LOW_CONFIDENCE
    assert conn.execute("SELECT COUNT(*) FROM extraction_errors WHERE source_id=? AND error_code='OCR_LOW_CONFIDENCE'",
                        (sid,)).fetchone()[0] == 1


def test_parallel_extraction_matches_sequential(tmp_path):
    """Controlled parallelism (spawned worker processes) produces the same evidence."""
    make_corpus(tmp_path / "a" / "src", include_scanned=False)
    make_corpus(tmp_path / "b" / "src", include_scanned=False)
    cfg_a = make_config(tmp_path / "a", tmp_path / "a" / "src")
    cfg_b = make_config(tmp_path / "b", tmp_path / "b" / "src")
    ca, cb = open_db(cfg_a.db_path), open_db(cfg_b.db_path)
    for c, cfg in ((ca, cfg_a), (cb, cfg_b)):
        run_inventory(c, cfg, "RUN-i", show_progress=False)
    run_extraction(ca, cfg_a, "RUN-seq", workers=1, show_progress=False)
    res = run_extraction(cb, cfg_b, "RUN-par", workers=2, show_progress=False)
    assert res["by_status"]
    q = "SELECT source_id, page_no, seq, block_type, text FROM evidence ORDER BY 1,2,3"
    assert rows(ca, q) == rows(cb, q)


if not HAS_TESSERACT:  # pragma: no cover - informative marker in the test report
    def test_tesseract_missing_notice():
        pytest.skip("Tesseract not installed: OCR tests skipped (OCR_UNAVAILABLE path still tested)")
