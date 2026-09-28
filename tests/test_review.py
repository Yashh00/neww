from __future__ import annotations

import sqlite3

import pytest

from maintdoc import audit
from maintdoc.approval import approval_blockers
from maintdoc.db import rows
from maintdoc.review import service
from maintdoc.review.service import ApprovalBlocked, ReviewError

from conftest import REVIEWER, ev_by_text, resolve_all_conflicts


def canonical(w, fragment):
    e = ev_by_text(w, fragment, "AND in_manual=1")
    return e["evidence_id"]


def pass_page_checks(w, evidence_id):
    e = w.ev("evidence_id=?", (evidence_id,))[0]
    for v in rows(w.conn, "SELECT check_id FROM visual_checks WHERE source_id=? AND page_no=? AND status='pending'",
                  (e["source_id"], e["page_no"])):
        service.decide_visual_check(w.conn, w.cfg, v["check_id"], "passed", REVIEWER, "ok")


def test_reviewer_name_is_mandatory(ws):
    eid = canonical(ws, "Check the hydraulic oil level daily")
    with pytest.raises(ReviewError):
        service.approve_evidence(ws.conn, ws.cfg, eid, "  ")


def test_simple_approval_is_audited(ws):
    eid = canonical(ws, "Check the hydraulic oil level daily")
    fp = service.approve_evidence(ws.conn, ws.cfg, eid, REVIEWER, "ok")
    e = ws.ev("evidence_id=?", (eid,))[0]
    assert e["review_status"] == "approved" and e["approval_fingerprint"] == fp and e["reviewed_by"] == REVIEWER
    d = rows(ws.conn, "SELECT * FROM review_decisions WHERE entity_id=? AND decision='approve'", (eid,))
    assert d and d[0]["audit_id"]
    assert audit.verify_chain(ws.conn)[0]


def test_unresolved_critical_conflict_blocks_approval(ws):
    eid = canonical(ws, "coupling bolts to 50 Nm")
    pass_page_checks(ws, eid)
    blockers = approval_blockers(ws.conn, ws.cfg, eid)
    assert any("unresolved critical conflict" in b for b in blockers)
    with pytest.raises(ApprovalBlocked):
        service.approve_evidence(ws.conn, ws.cfg, eid, REVIEWER)
    resolve_all_conflicts(ws)
    service.approve_evidence(ws.conn, ws.cfg, eid, REVIEWER, "after conflict decision")
    # the non-authoritative revision value was rejected by the conflict decision - never silently chosen
    other = ev_by_text(ws, "coupling bolts to 45 Nm", "AND in_manual=1")
    assert other["review_status"] == "rejected" and "CF-" in other["review_comment"]


def test_conflict_resolution_requires_justification(ws):
    cid = ws.one("SELECT conflict_id FROM conflicts WHERE status='open' LIMIT 1")
    with pytest.raises(ReviewError):
        service.resolve_conflict(ws.conn, ws.cfg, cid, "not_a_conflict", REVIEWER, "")
    with pytest.raises(ReviewError):
        service.resolve_conflict(ws.conn, ws.cfg, cid, "select_authoritative", REVIEWER, "reason", [])


def test_ocr_statement_requires_confirmation(ws):
    e = ws.ev("extraction_method='tesseract_ocr' AND in_manual=1 AND status='active'")
    if not e:
        pytest.skip("no OCR evidence (Tesseract missing)")
    eid = e[0]["evidence_id"]
    pass_page_checks(ws, eid)
    assert any("OCR text must be confirmed" in b for b in approval_blockers(ws.conn, ws.cfg, eid))
    service.set_ocr_verified(ws.conn, ws.cfg, eid, True, REVIEWER, "matches scan")
    assert not any("OCR" in b for b in approval_blockers(ws.conn, ws.cfg, eid))


def test_visual_check_is_mandatory_for_figures(ws):
    fig = ws.ev("block_type='figure' AND in_manual=1 AND status='active'")[0]
    assert any("visual check" in b for b in approval_blockers(ws.conn, ws.cfg, fig["evidence_id"]))
    vc = ws.one("SELECT check_id FROM visual_checks WHERE evidence_id=?", (fig["evidence_id"],))
    with pytest.raises(ReviewError):
        service.decide_visual_check(ws.conn, ws.cfg, vc, "failed", REVIEWER, "")  # failing needs a comment
    service.decide_visual_check(ws.conn, ws.cfg, vc, "passed", REVIEWER, "diagram matches source")
    assert not any("visual check" in b for b in approval_blockers(ws.conn, ws.cfg, fig["evidence_id"]))


def test_wording_edit_enforces_numeric_identity_and_signal_words(ws):
    eid = canonical(ws, "Lubricate the guide rails every 250 operating hours")
    with pytest.raises(ReviewError, match="numeric identity"):
        service.edit_wording(ws.conn, ws.cfg, eid, "Lubricate the guide rails every 500 operating hours.", REVIEWER,
                             "typo")
    service.edit_wording(ws.conn, ws.cfg, eid, "Lubricate the guide rails at intervals of 250 operating hours.",
                         REVIEWER, "clarity")
    e = ws.ev("evidence_id=?", (eid,))[0]
    assert e["text"] == "Lubricate the guide rails every 250 operating hours."  # original immutable
    assert e["display_text"].startswith("Lubricate the guide rails at intervals")
    warn = canonical(ws, "WARNING: Depressurize")
    with pytest.raises(ReviewError, match="signal word"):
        service.edit_wording(ws.conn, ws.cfg, warn, "Depressurize the hydraulic system before opening any fitting. "
                             "Residual pressure can cause serious injury.", REVIEWER, "shorter")


def test_edit_and_reclassify_invalidate_approval(ws):
    eid = canonical(ws, "Check the hydraulic oil level daily")
    service.approve_evidence(ws.conn, ws.cfg, eid, REVIEWER)
    service.edit_wording(ws.conn, ws.cfg, eid, "Check the hydraulic oil level daily, before operation.", REVIEWER,
                         "comma")
    assert ws.ev("evidence_id=?", (eid,))[0]["review_status"] == "unreviewed"
    service.approve_evidence(ws.conn, ws.cfg, eid, REVIEWER)
    service.reclassify(ws.conn, ws.cfg, eid, "inspections", REVIEWER, "belongs to daily checks")
    e = ws.ev("evidence_id=?", (eid,))[0]
    assert e["review_status"] == "invalidated" and e["chapter_override"] == "inspections"


def test_unclassified_statement_cannot_be_approved(ws):
    eid = canonical(ws, "Check the hydraulic oil level daily")
    service.reclassify(ws.conn, ws.cfg, eid, "unclassified", REVIEWER, "unclear where this belongs")
    assert any("unclassified" in b for b in approval_blockers(ws.conn, ws.cfg, eid))
    with pytest.raises(ApprovalBlocked):
        service.approve_evidence(ws.conn, ws.cfg, eid, REVIEWER)
    with pytest.raises(ReviewError):
        service.reclassify(ws.conn, ws.cfg, eid, "no_such_chapter", REVIEWER, "typo")


def test_gibberish_cannot_be_approved_as_is(ws):
    eid = canonical(ws, "Xqzvbn")
    assert any("quality" in b for b in approval_blockers(ws.conn, ws.cfg, eid))


def test_near_duplicate_merge_is_refused_when_values_differ(ws):
    pid = ws.one("SELECT pair_id FROM near_duplicates WHERE merge_allowed=0 LIMIT 1")
    with pytest.raises(ReviewError, match="Merge not allowed"):
        service.decide_near_duplicate(ws.conn, ws.cfg, pid, "confirmed_duplicate", REVIEWER, "looks same")
    service.decide_near_duplicate(ws.conn, ws.cfg, pid, "not_duplicate", REVIEWER, "different values")
    assert ws.one("SELECT status FROM near_duplicates WHERE pair_id=?", (pid,)) == "not_duplicate"


def test_audit_and_decisions_are_append_only(ws):
    eid = canonical(ws, "Check the hydraulic oil level daily")
    service.approve_evidence(ws.conn, ws.cfg, eid, REVIEWER)
    for sql in ("UPDATE audit_log SET actor='x'", "DELETE FROM audit_log",
                "UPDATE review_decisions SET reviewer='x'", "DELETE FROM review_decisions",
                f"DELETE FROM evidence WHERE evidence_id='{eid}'",
                f"UPDATE evidence SET text='tampered' WHERE evidence_id='{eid}'"):
        with pytest.raises(sqlite3.DatabaseError):
            ws.conn.execute(sql)


def test_hash_chain_detects_tampering(ws, tmp_path):
    ok, bad, n = audit.verify_chain(ws.conn)
    assert ok and n > 0
    copy = sqlite3.connect(":memory:")
    ws.conn.backup(copy)
    copy.execute("DROP TRIGGER audit_log_no_update")
    copy.execute("UPDATE audit_log SET actor='mallory' WHERE audit_id=(SELECT MIN(audit_id) FROM audit_log)")
    copy.row_factory = sqlite3.Row
    ok, bad, _ = audit.verify_chain(copy)
    assert not ok and bad is not None


def test_failed_visual_check_demotes_page_statements(ws):
    vc = rows(ws.conn, "SELECT * FROM visual_checks WHERE kind='multi_column'")[0]
    eid = ws.ev("source_id=? AND page_no=? AND in_manual=1 AND status='active'", (vc["source_id"], vc["page_no"]))[0]
    service.decide_visual_check(ws.conn, ws.cfg, vc["check_id"], "failed", REVIEWER, "columns interleaved")
    assert ws.ev("evidence_id=?", (eid["evidence_id"],))[0]["review_status"] == "needs_revision"
