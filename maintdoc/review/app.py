"""Local Streamlit review workflow.

Launch with ``python -m maintdoc review`` (binds to 127.0.0.1, usage statistics
disabled). All state changes go through :mod:`maintdoc.review.service`, which
records every decision in the append-only decision table and the hash-chained
audit log.
"""

from __future__ import annotations

import argparse
import difflib
import html
import os
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

from maintdoc import audit
from maintdoc.config import Config
from maintdoc.constants import DRAFT_DISCLAIMER, UNCLASSIFIED_CHAPTER, ReviewStatus
from maintdoc.db import open_db, row, rows
from maintdoc.review import service
from maintdoc.review.render import SourceChanged, render_page, render_pdf_pages
from maintdoc.utils import loads

PAGE_SIZE = 200


# --------------------------------------------------------------------------- setup
def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default=os.environ.get("MAINTDOC_CONFIG", "config.yaml"))
    args, _ = p.parse_known_args(sys.argv[1:])
    return args


@st.cache_resource
def get_config(path: str) -> Config:
    return Config.load(path)


def conn_for(cfg: Config):
    return open_db(cfg.db_path)


def reviewer() -> str:
    return st.session_state.get("reviewer", "").strip()


def run_action(fn, *args, success: str = "Saved", **kwargs) -> bool:
    try:
        result = fn(*args, **kwargs)
    except service.ApprovalBlocked as exc:
        st.error("Approval blocked:\n\n" + "\n".join(f"- {r}" for r in exc.reasons))
        return False
    except service.ReviewError as exc:
        st.error(str(exc))
        return False
    if isinstance(result, dict) and result.get("blocked"):
        st.warning(f"{len(result['blocked'])} item(s) blocked: " +
                   "; ".join(f"{e}: {', '.join(r)}" for e, r in result["blocked"][:10]))
    st.success(success)
    return True


def page_image(conn, evidence_or_page: dict, zoom: float = 1.4, clip_to_bbox: bool = False) -> None:
    src = row(conn, "SELECT abs_path, sha256, present FROM sources WHERE source_id=?", (evidence_or_page["source_id"],))
    if not src or not src["present"]:
        st.warning("Source file not available")
        return
    bbox = None
    if evidence_or_page.get("bbox_x0") is not None:
        bbox = (evidence_or_page["bbox_x0"], evidence_or_page["bbox_y0"], evidence_or_page["bbox_x1"],
                evidence_or_page["bbox_y1"])
    expected = evidence_or_page.get("source_sha256")
    try:
        clip = None
        if clip_to_bbox and bbox:
            clip = (bbox[0] - 40, bbox[1] - 60, bbox[2] + 40, bbox[3] + 60)
        png = render_page(src["abs_path"], int(evidence_or_page["page_no"]), bbox, zoom=zoom,
                          expected_sha256=expected, clip=clip)
        st.image(png, caption=f"{evidence_or_page['source_id']} page {evidence_or_page['page_no']}"
                              + (" (region approximate)" if evidence_or_page.get("bbox_approx") else ""))
    except SourceChanged as exc:
        st.error(str(exc))
    except Exception as exc:  # noqa: BLE001 - display problem only
        st.warning(f"Cannot render page: {exc}")


def df(data: list[dict], height: int | None = None) -> None:
    if not data:
        st.info("Nothing to show")
        return
    kwargs = {"height": height} if height else {}
    st.dataframe(pd.DataFrame(data), width="stretch", hide_index=True, **kwargs)


# --------------------------------------------------------------------------- pages
def page_dashboard(conn, cfg: Config) -> None:
    st.header("Dashboard")
    st.warning(DRAFT_DISCLAIMER)
    d = service.dashboard(conn)
    cols = st.columns(4)
    cols[0].metric("Sources", sum(d["sources"].values()))
    cols[1].metric("Pages", sum(d["pages"].values()))
    cols[2].metric("Manual statements", sum(d["statements"].values()))
    cols[3].metric("Open critical conflicts", d["conflicts_open"].get("critical", 0))
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Sources by status")
        st.json(d["sources"])
        st.subheader("Extraction status")
        st.json(d["extraction"])
        st.subheader("Pages by extraction status")
        st.json(d["pages"])
    with c2:
        st.subheader("Statements by review status")
        st.json(d["statements"])
        st.subheader("Open conflicts by severity")
        st.json(d["conflicts_open"])
        st.subheader("Open errors by severity")
        st.json(d["errors_open"])
        st.subheader("Visual checks")
        st.json(d["visual_checks"])
    ok, bad, n = audit.verify_chain(conn)
    (st.success if ok else st.error)(f"Audit chain {'intact' if ok else 'BROKEN at entry ' + str(bad)} ({n} entries)")


def page_sources(conn, cfg: Config) -> None:
    st.header("Source register")
    data = rows(conn, "SELECT source_id, rel_path, status, extraction_status, page_count, equipment, model, component, "
                      "revision, doc_date, doc_number, duplicate_of, problem_pages, ocr_pages, substr(sha256,1,16) sha "
                      "FROM sources ORDER BY source_id")
    df(data, 400)
    ids = [d["source_id"] for d in data]
    if not ids:
        return
    sid = st.selectbox("Source", ids)
    s = row(conn, "SELECT * FROM sources WHERE source_id=?", (sid,))
    st.write(f"**{s['rel_path']}** - SHA-256 `{s['sha256']}`")
    st.json(loads(s["metadata_origin_json"], {}))
    st.subheader("Pages")
    df(rows(conn, "SELECT page_no, extraction_status, extraction_method, native_chars, ocr_confidence, table_count, "
                  "figure_count, multi_column, visual_check_reason, error_count FROM pages WHERE source_id=? "
                  "ORDER BY page_no", (sid,)), 300)
    st.subheader("Errors")
    df(rows(conn, "SELECT error_id, page_no, error_code, severity, status, message FROM extraction_errors WHERE "
                  "source_id=? ORDER BY page_no", (sid,)))


def page_statements(conn, cfg: Config) -> None:
    st.header("Statement review")
    chapters = [c["id"] for c in cfg.chapters()] + [UNCLASSIFIED_CHAPTER]
    f1, f2, f3, f4 = st.columns(4)
    ch = f1.selectbox("Chapter", ["(all)"] + chapters)
    status = f2.selectbox("Review status", ["(all)"] + list(ReviewStatus.ALL))
    sources = ["(all)"] + [r[0] for r in conn.execute("SELECT source_id FROM sources ORDER BY source_id")]
    src = f3.selectbox("Source", sources)
    search = f4.text_input("Text contains")
    where = ["e.status='active'", "e.in_manual=1"]
    params: list = []
    if ch != "(all)":
        where.append("COALESCE(e.chapter_override, e.chapter)=?")
        params.append(ch)
    if status != "(all)":
        where.append("e.review_status=?")
        params.append(status)
    if src != "(all)":
        where.append("e.source_id=?")
        params.append(src)
    if search:
        where.append("(e.text LIKE ? OR e.display_text LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]
    data = rows(conn, f"SELECT e.evidence_id, COALESCE(e.chapter_override, e.chapter) chapter, e.block_type, "
                      f"e.review_status, e.source_id, e.page_no, e.extraction_method, substr(COALESCE(e.display_text, "
                      f"e.text),1,120) text FROM evidence e WHERE {' AND '.join(where)} "
                      f"ORDER BY e.source_id, e.page_no, e.seq LIMIT {PAGE_SIZE}", params)
    df(data, 300)
    if not data:
        return
    ids = [d["evidence_id"] for d in data]
    sel = st.selectbox("Statement", ids, key="stmt_sel")
    ctx = service.statement_context(conn, cfg, sel)
    e = ctx["evidence"]
    left, right = st.columns([3, 2])
    with left:
        st.markdown(f"**{e['evidence_id']}** - {e['block_type']} - chapter `{e.get('chapter_override') or e['chapter']}` "
                    f"({e['classification_status']}; {e['chapter_rule']})")
        st.markdown(f"Section: *{e['section_path'] or '-'}*")
        st.markdown(f"Applicability: equipment `{e['equipment']}`, model `{e['model']}`, component `{e['component']}`, "
                    f"revision `{e['revision']}`")
        st.text_area("Original extracted text (immutable)", e["text"], height=120, disabled=True)
        if e["display_text"]:
            st.text_area("Reviewer wording", e["display_text"], height=100, disabled=True)
        st.caption(f"Method {e['extraction_method']}; OCR confidence {e['ocr_confidence']}; "
                   f"SHA-256 {e['source_sha256'][:16]}...; review {e['review_status']} by {e['reviewed_by']}")
        if e["table_json"]:
            st.dataframe(pd.DataFrame(loads(e["table_json"], [])), hide_index=True)
        if e["figure_path"] and Path(e["figure_path"]).exists():
            st.image(e["figure_path"], caption="Extracted figure region")
        st.subheader("Citations")
        df(ctx["cited"])
        if ctx["quantities"]:
            st.subheader("Values")
            df(ctx["quantities"])
        if ctx["conflicts"]:
            st.subheader("Conflicts")
            df(ctx["conflicts"])
        if ctx["blockers"]:
            st.error("Approval blocked:\n\n" + "\n".join(f"- {b}" for b in ctx["blockers"]))
        else:
            st.success("No approval blockers")
        st.subheader("Decision")
        comment = st.text_input("Comment", key=f"c_{sel}")
        b1, b2, b3, b4 = st.columns(4)
        if b1.button("Approve", disabled=bool(ctx["blockers"]), key=f"a_{sel}"):
            if run_action(service.approve_evidence, conn, cfg, sel, reviewer(), comment or None, success="Approved"):
                st.rerun()
        if b2.button("Reject", key=f"r_{sel}"):
            if run_action(service.reject_evidence, conn, cfg, sel, reviewer(), comment, success="Rejected"):
                st.rerun()
        if b3.button("Needs revision", key=f"n_{sel}"):
            if run_action(service.request_revision, conn, cfg, sel, reviewer(), comment):
                st.rerun()
        if b4.button("Reopen", key=f"o_{sel}"):
            if run_action(service.reopen_evidence, conn, cfg, sel, reviewer(), comment):
                st.rerun()
        if e["extraction_method"] == "tesseract_ocr":
            verified = st.checkbox("OCR text verified against page image", value=bool(e["ocr_verified"]),
                                   key=f"ov_{sel}")
            if verified != bool(e["ocr_verified"]):
                if run_action(service.set_ocr_verified, conn, cfg, sel, verified, reviewer(), comment or None):
                    st.rerun()
        with st.expander("Edit wording (numbers and units must stay identical)"):
            new = st.text_area("Wording", e["display_text"] or e["text"], key=f"w_{sel}")
            reason = st.text_input("Reason for wording change", key=f"wr_{sel}")
            if st.button("Save wording", key=f"ws_{sel}"):
                if run_action(service.edit_wording, conn, cfg, sel, new, reviewer(), reason):
                    st.rerun()
        with st.expander("Reclassify chapter"):
            new_ch = st.selectbox("Chapter", chapters, index=chapters.index(e.get("chapter_override") or e["chapter"])
                                  if (e.get("chapter_override") or e["chapter"]) in chapters else 0, key=f"ch_{sel}")
            reason = st.text_input("Reason", key=f"chr_{sel}")
            if st.button("Save chapter", key=f"chs_{sel}"):
                if run_action(service.reclassify, conn, cfg, sel, new_ch, reviewer(), reason):
                    st.rerun()
        st.subheader("Decision history")
        df(ctx["decisions"])
    with right:
        page_image(conn, e)
    with st.expander("Bulk approve the listed statements (each is checked individually)"):
        bc = st.text_input("Bulk approval comment", key="bulk_c")
        if st.button("Approve all listed without blockers"):
            if run_action(service.bulk_approve, conn, cfg, ids, reviewer(), bc, success="Bulk approval finished"):
                st.rerun()


def page_duplicates(conn, cfg: Config) -> None:
    st.header("Near-duplicate candidates (never merged automatically)")
    status = st.selectbox("Status", ["candidate", "confirmed_duplicate", "not_duplicate", "stale", "(all)"])
    q = "SELECT pair_id, evidence_a, evidence_b, score, guard_flags, merge_allowed, status FROM near_duplicates"
    data = rows(conn, q + ("" if status == "(all)" else " WHERE status=?") + " ORDER BY score DESC LIMIT 500",
                () if status == "(all)" else (status,))
    df(data, 250)
    if not data:
        return
    pid = st.selectbox("Pair", [d["pair_id"] for d in data])
    p = row(conn, "SELECT * FROM near_duplicates WHERE pair_id=?", (pid,))
    a = row(conn, "SELECT * FROM evidence WHERE evidence_id=?", (p["evidence_a"],))
    b = row(conn, "SELECT * FROM evidence WHERE evidence_id=?", (p["evidence_b"],))
    flags = loads(p["guard_flags"], [])
    if flags:
        st.error("Merge forbidden: " + ", ".join(flags))
    c1, c2 = st.columns(2)
    for col, e in ((c1, a), (c2, b)):
        with col:
            st.markdown(f"**{e['evidence_id']}** ({e['source_id']} p.{e['page_no']}, rev {e['revision']}, "
                        f"model {e['model']})")
            st.text_area("Text", e["text"], disabled=True, key=f"t_{e['evidence_id']}")
            page_image(conn, e, zoom=1.0, clip_to_bbox=True)
    diff = difflib.ndiff(a["text"].split(), b["text"].split())
    st.markdown(" ".join(
        f"<span style='background:#fdd'>{html.escape(t[2:])}</span>" if t.startswith("- ") else
        f"<span style='background:#dfd'>{html.escape(t[2:])}</span>" if t.startswith("+ ") else html.escape(t[2:])
        for t in diff if not t.startswith("? ")), unsafe_allow_html=True)
    comment = st.text_input("Comment", key=f"ndc_{pid}")
    b1, b2 = st.columns(2)
    if b1.button("Confirm duplicate (merge citations)", disabled=bool(flags)):
        if run_action(service.decide_near_duplicate, conn, cfg, pid, "confirmed_duplicate", reviewer(), comment):
            st.rerun()
    if b2.button("Not a duplicate (keep both)"):
        if run_action(service.decide_near_duplicate, conn, cfg, pid, "not_duplicate", reviewer(), comment):
            st.rerun()


def page_conflicts(conn, cfg: Config) -> None:
    st.header("Conflict register")
    c1, c2 = st.columns(2)
    status = c1.selectbox("Status", ["open", "resolved", "not_a_conflict", "stale", "(all)"])
    sev = c2.selectbox("Severity", ["(all)", "critical", "major", "minor"])
    where, params = [], []
    if status != "(all)":
        where.append("status=?")
        params.append(status)
    if sev != "(all)":
        where.append("severity=?")
        params.append(sev)
    data = rows(conn, "SELECT conflict_id, conflict_type, severity, blocking, status, revision_related, "
                      "substr(description,1,160) description FROM conflicts" +
                (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY blocking DESC, severity", params)
    df(data, 250)
    if not data:
        return
    cid = st.selectbox("Conflict", [d["conflict_id"] for d in data])
    c = row(conn, "SELECT * FROM conflicts WHERE conflict_id=?", (cid,))
    st.markdown(f"**{cid}** - {c['conflict_type']} / {c['severity']} / {c['status']}")
    st.write(c["description"])
    st.info("The tool never chooses between conflicting values, revisions or models. Record the engineering decision.")
    df(loads(c["values_json"], []))
    ids = loads(c["evidence_ids"], [])
    for eid in ids:
        e = row(conn, "SELECT * FROM evidence WHERE evidence_id=?", (eid,))
        with st.expander(f"{eid} - {e['source_id']} p.{e['page_no']} rev {e['revision']}: {e['text'][:90]}"):
            st.text(e["text"])
            page_image(conn, e, zoom=1.0, clip_to_bbox=True)
    if c["status"] in ("open",):
        rtype = st.radio("Resolution", service.RESOLUTIONS, horizontal=True)
        auth = st.multiselect("Authoritative evidence (others will be rejected)", ids) \
            if rtype == "select_authoritative" else None
        comment = st.text_area("Engineering justification (required)", key=f"cfc_{cid}")
        if st.button("Record resolution"):
            if run_action(service.resolve_conflict, conn, cfg, cid, rtype, reviewer(), comment, auth):
                st.rerun()
    elif c["status"] in ("resolved", "not_a_conflict"):
        st.write(f"Resolved by {c['resolved_by']} at {c['resolved_at']}: {c['resolution_type']} - {c['resolution']}")
        comment = st.text_input("Reason to reopen", key=f"cro_{cid}")
        if st.button("Reopen"):
            if run_action(service.reopen_conflict, conn, cfg, cid, reviewer(), comment):
                st.rerun()


def page_errors(conn, cfg: Config) -> None:
    st.header("Extraction error register / OCR review")
    codes = ["(all)"] + [r[0] for r in conn.execute("SELECT DISTINCT error_code FROM extraction_errors ORDER BY 1")]
    c1, c2 = st.columns(2)
    code = c1.selectbox("Error code", codes)
    status = c2.selectbox("Status", ["open", "acknowledged", "resolved", "wont_fix", "superseded", "(all)"])
    where, params = [], []
    if code != "(all)":
        where.append("error_code=?")
        params.append(code)
    if status != "(all)":
        where.append("status=?")
        params.append(status)
    data = rows(conn, "SELECT error_id, source_id, page_no, evidence_id, error_code, severity, status, "
                      "substr(message,1,140) message FROM extraction_errors" +
                (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY error_id LIMIT 1000", params)
    df(data, 300)
    if not data:
        return
    eid = st.selectbox("Error", [d["error_id"] for d in data])
    err = row(conn, "SELECT * FROM extraction_errors WHERE error_id=?", (eid,))
    st.write(err["message"])
    if err["details_json"]:
        st.json(loads(err["details_json"], {}))
    if err["source_id"] and err["page_no"]:
        page = row(conn, "SELECT * FROM pages WHERE source_id=? AND page_no=?", (err["source_id"], err["page_no"]))
        target = {"source_id": err["source_id"], "page_no": err["page_no"], "source_sha256": err["source_sha256"]}
        if err["evidence_id"]:
            ev = row(conn, "SELECT * FROM evidence WHERE evidence_id=?", (err["evidence_id"],))
            if ev:
                target = ev
        c1, c2 = st.columns(2)
        with c1:
            if page:
                st.caption(f"Page status {page['extraction_status']}, method {page['extraction_method']}, "
                           f"OCR confidence {page['ocr_confidence']}")
                st.text_area("Extracted page text", page["page_text"] or "", height=400, disabled=True)
        with c2:
            page_image(conn, target, zoom=1.2)
    new = st.selectbox("Set status", ["acknowledged", "resolved", "wont_fix", "open"])
    comment = st.text_input("Comment", key=f"ec_{eid}")
    if st.button("Update error status"):
        if run_action(service.set_error_status, conn, cfg, eid, new, reviewer(), comment):
            st.rerun()


def page_visual(conn, cfg: Config) -> None:
    st.header("Mandatory human visual checks")
    st.caption("Complex layouts, figures/diagrams, OCR pages, table failures and generated manuals must be "
               "compared visually by a qualified person.")
    status = st.selectbox("Status", ["pending", "failed", "passed", "invalidated", "(all)"])
    data = rows(conn, "SELECT check_id, target_type, kind, source_id, page_no, evidence_id, output_path, status, reason "
                      "FROM visual_checks" + ("" if status == "(all)" else " WHERE status=?") +
                " ORDER BY source_id, page_no LIMIT 1000", () if status == "(all)" else (status,))
    df(data, 250)
    if not data:
        return
    vid = st.selectbox("Check", [d["check_id"] for d in data])
    v = row(conn, "SELECT * FROM visual_checks WHERE check_id=?", (vid,))
    st.write(f"**{v['kind']}**: {v['reason']}")
    if v["target_type"] == "source_page":
        target = {"source_id": v["source_id"], "page_no": v["page_no"], "source_sha256": v["source_sha256"]}
        if v["evidence_id"]:
            ev = row(conn, "SELECT * FROM evidence WHERE evidence_id=?", (v["evidence_id"],))
            if ev:
                target = ev
                if ev.get("figure_path") and Path(ev["figure_path"]).exists():
                    st.image(ev["figure_path"], caption="Extracted figure (as used in the manual)")
        c1, c2 = st.columns(2)
        with c1:
            page_image(conn, target, zoom=1.2)
        with c2:
            ev_rows = rows(conn, "SELECT seq, block_type, status, substr(text,1,100) text FROM evidence WHERE "
                                 "source_id=? AND source_sha256=? AND page_no=? ORDER BY seq",
                           (v["source_id"], v["source_sha256"], v["page_no"]))
            st.caption("Extracted units in reading order")
            df(ev_rows, 400)
    else:
        st.write(f"Generated output: `{v['output_path']}`")
        if v["output_path"] and Path(v["output_path"]).exists() and v["output_path"].lower().endswith(".pdf"):
            first = st.number_input("First page", min_value=1, value=1)
            for png in render_pdf_pages(v["output_path"], int(first), 3):
                st.image(png)
    comment = st.text_input("Comment (required when failing)", key=f"vc_{vid}")
    b1, b2 = st.columns(2)
    if b1.button("Visual check PASSED"):
        if run_action(service.decide_visual_check, conn, cfg, vid, "passed", reviewer(), comment or None):
            st.rerun()
    if b2.button("Visual check FAILED"):
        if run_action(service.decide_visual_check, conn, cfg, vid, "failed", reviewer(), comment):
            st.rerun()


def page_audit(conn, cfg: Config) -> None:
    st.header("Immutable audit history")
    ok, bad, n = audit.verify_chain(conn)
    (st.success if ok else st.error)(f"Hash chain {'intact' if ok else 'BROKEN at ' + str(bad)} - {n} entries")
    q = st.text_input("Filter entity id")
    data = rows(conn, "SELECT audit_id, ts, actor, action, entity_type, entity_id, reason, substr(after_json,1,200) "
                      "after FROM audit_log" + (" WHERE entity_id LIKE ?" if q else "") +
                " ORDER BY audit_id DESC LIMIT 1000", (f"%{q}%",) if q else ())
    df(data, 500)
    st.subheader("Review decisions")
    df(rows(conn, "SELECT decision_id, created_at, reviewer, entity_type, entity_id, decision, comment FROM "
                  "review_decisions ORDER BY decision_id DESC LIMIT 1000"), 400)


PAGES = {
    "Dashboard": page_dashboard,
    "Sources": page_sources,
    "Statements": page_statements,
    "Near duplicates": page_duplicates,
    "Conflicts": page_conflicts,
    "Errors & OCR": page_errors,
    "Visual checks": page_visual,
    "Audit history": page_audit,
}


def main() -> None:
    st.set_page_config(page_title="maintdoc review", layout="wide")
    args = _args()
    cfg = get_config(args.config)
    st.sidebar.title("maintdoc review")
    st.sidebar.text_input("Reviewer name (required)", key="reviewer")
    if not reviewer():
        st.sidebar.warning("Enter your name - every decision is recorded with it.")
    choice = st.sidebar.radio("Section", list(PAGES))
    st.sidebar.caption(f"Database: {cfg.db_path}")
    st.sidebar.caption("All outputs are drafts until qualified engineering review and release.")
    conn = conn_for(cfg)
    try:
        PAGES[choice](conn, cfg)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
