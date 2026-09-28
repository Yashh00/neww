from __future__ import annotations

import pytest

from maintdoc.analysis.classify import (ChapterClassifier, DocumentClassifier, applicability_compatible,
                                        normalize_date)
from maintdoc.analysis.conflicts import subject_match
from maintdoc.analysis.dedupe import guard_flags, topic_key
from maintdoc.config import Config
from maintdoc.db import rows
from maintdoc.utils import loads, norm_hash, normalize_text

from conftest import REPO, ev_by_text, source_id


@pytest.fixture(scope="module")
def cfg():
    return Config.load(REPO / "config.yaml")


# --------------------------------------------------------------------------- classification
@pytest.mark.parametrize("text,block,heading,expected", [
    ("Replace the filter element every 500 operating hours.", "paragraph", "4 Preventive Maintenance",
     "preventive_maintenance"),
    ("WARNING: Depressurize before opening any fitting.", "warning", "2 Safety Instructions", "safety"),
    ("WARNING: Lock out the main power supply.", "warning", "5.1 Replacing the pump coupling", "procedures"),
    ("Pump noise: check the oil level.", "paragraph", "6 Troubleshooting", "troubleshooting"),
    ("HF-1001 | Hydraulic filter element | 1", "table", "7 Spare Parts", "parts"),
    ("Some isolated sentence without context.", "paragraph", None, "unclassified"),
])
def test_chapter_rules(cfg, text, block, heading, expected):
    res = ChapterClassifier(cfg).classify(text, block, heading, heading)
    assert res.chapter == expected, res


def test_warning_without_context_goes_to_safety(cfg):
    assert ChapterClassifier(cfg).classify("DANGER: High voltage.", "danger", None, None).chapter == "safety"


def test_document_metadata_from_filename_and_text(cfg):
    dc = DocumentClassifier(cfg)
    m = dc.classify("HP-200/Rev_C/HP-200_Maintenance_Manual_RevC.pdf", {}, "Document No. MM-HP200 Date: 2023-06-01")
    assert m.values["model"] == "HP-200" and m.values["revision"] == "C"
    assert m.origins["revision"]["origin"] == "filename"
    assert m.values["doc_number"] == "MM-HP200" and m.values["doc_date"] == "2023-06-01"
    assert normalize_date("15.03.2022") == ("2022-03-15", False)
    assert normalize_date("03/04/2022")[1] is True  # ambiguous day/month order is flagged


def test_applicability_never_mixes_models():
    assert applicability_compatible({"model": "HP-200"}, {"model": "HP-300"}) == (False, "different_model")
    assert applicability_compatible({"model": "HP-200; HP-300"}, {"model": "HP-300"})[0]
    assert applicability_compatible({"model": None}, {"model": "HP-300"}) == (True, "model_unspecified_on_one_side")


def test_pipeline_classification(processed):
    assert ev_by_text(processed, "Replace the hydraulic filter element every 500")["chapter"] == "preventive_maintenance"
    assert ev_by_text(processed, "WARNING: Switch off")["chapter"] == "procedures"
    assert ev_by_text(processed, "This bulletin applies")["model"] == "HP-300"


# --------------------------------------------------------------------------- duplicates
def test_normalisation_never_equates_different_numbers():
    assert norm_hash("Tighten to 45 Nm.") != norm_hash("Tighten to 4.5 Nm.")
    assert norm_hash("Tighten to 45 Nm.") != norm_hash("Tighten to -45 Nm.")
    assert norm_hash("Tighten  to 45\nNm.") == norm_hash("tighten to 45 nm.")
    assert normalize_text("hydrau-\nlic") == "hydraulic"
    assert topic_key("5.1 Replacing the pump coupling") == topic_key("6.2  Replacing the Pump Coupling")


def test_exact_duplicates_across_revisions_keep_all_citations(processed):
    w = processed
    step1 = w.ev("status='active' AND text LIKE '1. Switch off the press%' ORDER BY source_id")
    roles = {e["source_id"]: e["dup_role"] for e in step1}
    canon = [e for e in step1 if e["dup_role"] == "canonical"]
    assert len(canon) == 1 and len(step1) >= 2
    others = [e for e in step1 if e["dup_role"] == "exact_duplicate"]
    assert all(o["canonical_evidence_id"] == canon[0]["evidence_id"] for o in others), roles
    assert all(o["in_manual"] == 0 and "cited via" in o["manual_exclusion"] for o in others)


def test_different_values_are_not_merged(processed):
    w = processed
    b = ev_by_text(w, "coupling bolts to 45 Nm")
    c = ev_by_text(w, "coupling bolts to 50 Nm")
    assert b["dup_role"] != "exact_duplicate" and c["dup_role"] != "exact_duplicate"
    pair = rows(w.conn, "SELECT * FROM near_duplicates WHERE (evidence_a=? AND evidence_b=?) OR (evidence_a=? AND "
                        "evidence_b=?)", (b["evidence_id"], c["evidence_id"], c["evidence_id"], b["evidence_id"]))
    assert pair and pair[0]["merge_allowed"] == 0
    assert {"numbers_differ", "revision_differs"} <= set(loads(pair[0]["guard_flags"]))


def test_guard_flags(cfg):
    base = {"source_id": "S1", "block_type": "paragraph", "safety_level": None, "equipment": "E", "model": "M",
            "component": None, "revision": "B"}
    a = dict(base, text="Do not open the valve while pressurised.")
    b = dict(base, text="Open the valve while pressurised.")
    assert "negation_differs" in guard_flags(a, b, cfg)
    w1 = dict(base, text="WARNING: Hot surface.", block_type="warning", safety_level="warning")
    w2 = dict(base, text="CAUTION: Hot surface.", block_type="caution", safety_level="caution")
    assert "warning_or_signal_word_differs" in guard_flags(w1, w2, cfg)
    m = dict(base, text="Oil level check.", model="M2")
    assert "applicability_differs" in guard_flags(dict(base, text="Oil level check."), m, cfg)


# --------------------------------------------------------------------------- conflicts
def _conflicts(w, ctype):
    return rows(w.conn, "SELECT * FROM conflicts WHERE conflict_type=? AND status='open'", (ctype,))


def test_torque_revision_conflict_is_critical_and_blocking(processed):
    cf = _conflicts(processed, "torque")
    assert len(cf) == 1
    c = cf[0]
    assert c["severity"] == "critical" and c["blocking"] == 1 and c["revision_related"] == 1
    assert "no revision is chosen automatically" in c["description"]
    raw = {v["raw_text"] for v in loads(c["values_json"])}
    assert {"45 Nm", "50 Nm"} <= raw


def test_pressure_conflict_same_model_other_document(processed):
    cf = _conflicts(processed, "pressure")
    assert len(cf) == 1 and cf[0]["severity"] == "critical"
    assert "230 bar" in cf[0]["description"] and "210 bar" in cf[0]["description"]


def test_interval_conflict_is_major(processed):
    cf = _conflicts(processed, "interval")
    assert len(cf) == 1 and cf[0]["severity"] == "major" and cf[0]["blocking"] == 0


def test_different_model_is_never_a_conflict(processed):
    w = processed
    hp300 = source_id(w, "HP-300")
    ids = {r[0] for r in w.conn.execute("SELECT evidence_id FROM evidence WHERE source_id=?", (hp300,))}
    linked = {r[0] for r in w.conn.execute("SELECT evidence_id FROM conflict_evidence")}
    assert not ids & linked  # 250 bar / 60 Nm for HP-300 do not conflict with HP-200 values


def test_subject_match():
    assert subject_match("bolt coupling", "bolt coupling", 85)
    assert subject_match("element filter", "element filter hydraulic", 85)
    assert not subject_match("pressure", "accumulator pre-charge pressure", 85)
    assert not subject_match("maximum pressure", "minimum temperature", 85)
