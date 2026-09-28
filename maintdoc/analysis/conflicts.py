"""Context-aware conflict detection.

Detects inconsistent statements across (and within) sources and writes them to
the conflict register. It never chooses between inconsistent values,
revisions or equipment models; resolution is always a recorded human decision.

Rules
-----
* Quantities (torque, pressure, temperature, intervals, dimensions, ...):
  same parameter, same qualifier (max/min/nominal), matching subject, compatible
  applicability (different explicit models or components are never compared),
  converted values (or ranges) that do not overlap within tolerance.
* Dual-unit statements whose two values disagree ("45 Nm (30 ft-lb)").
* Part numbers: similar part descriptions with different part numbers.
* Procedures: same procedure topic in different sources with different step
  sequences (ignoring pure number changes, which are quantity conflicts).
* Safety: a warning present for a procedure in one source but missing in
  another, or similar safety statements that differ in numbers, negation or
  signal word.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import defaultdict
from typing import Any

from rapidfuzz import fuzz

from maintdoc import audit
from maintdoc.analysis.classify import applicability_compatible, split_multi
from maintdoc.analysis.dedupe import topic_key
from maintdoc.config import Config
from maintdoc.constants import BlockType, ConflictStatus, EvidenceStatus
from maintdoc.db import rows, transaction
from maintdoc.utils import dumps, loads, normalize_text, now_iso, sha256_text

log = logging.getLogger(__name__)

COMPARABLE = ("torque", "pressure", "temperature", "interval", "length", "volume", "mass", "force", "speed",
              "flow", "power", "voltage", "current", "frequency", "viscosity", "duration")
_PN_HEADER = re.compile(r"(?i)\b(part\s*(no|number|#)|p/n|pn|order\s*(no|number)|art(icle)?\.?\s*no|item\s*no)\b")
_DESC_HEADER = re.compile(r"(?i)\b(description|designation|name|bezeichnung|part name)\b")
_PN_INLINE = re.compile(r"(?i)\b(?:P/N|PN|part\s*(?:no\.?|number)|order\s*no\.?)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-./]{2,})")
_NUMS = re.compile(r"[-+]?\d+(?:[.,]\d+)*")


def _candidate_pairs(subjects: list[str | None]) -> list[tuple[int, int]]:
    """Index pairs sharing at least one subject token (inverted index instead of all pairs)."""
    index: dict[str, list[int]] = defaultdict(list)
    for i, subj in enumerate(subjects):
        for tok in set((subj or "").split()):
            index[tok].append(i)
    pairs: set[tuple[int, int]] = set()
    for members in index.values():
        if len(members) > 2000:
            log.debug("very common subject token; %d members", len(members))
        for x in range(len(members)):
            for y in range(x + 1, len(members)):
                pairs.add((members[x], members[y]))
    return sorted(pairs)


def _similar_pairs(texts: list[str], threshold: float, scorer=fuzz.token_sort_ratio,
                   chunk: int = 1024) -> list[tuple[int, int]]:
    """Index pairs (i<j) whose similarity >= threshold, computed with vectorised rapidfuzz.cdist."""
    import numpy as np
    from rapidfuzz import process
    out: list[tuple[int, int]] = []
    n = len(texts)
    for i0 in range(0, n, chunk):
        block = texts[i0:i0 + chunk]
        m = process.cdist(block, texts[i0:], scorer=scorer, score_cutoff=threshold, dtype=np.uint8, workers=-1)
        ii, jj = np.nonzero(m)
        for i, j in zip(ii.tolist(), jj.tolist()):
            a, b = i0 + i, i0 + j
            if b > a:
                out.append((a, b))
    return out


class _UF:
    def __init__(self):
        self.p: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def subject_match(a: str, b: str, threshold: float) -> bool:
    ta, tb = set((a or "").split()), set((b or "").split())
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    inter = ta & tb
    if len(inter) >= 2 and (inter == ta or inter == tb):
        return True
    return fuzz.token_sort_ratio(" ".join(sorted(ta)), " ".join(sorted(tb))) >= threshold


def _same_family(sa: dict, sb: dict) -> bool:
    if sa["source_id"] == sb["source_id"]:
        return False
    if sa.get("doc_number") and sa.get("doc_number") == sb.get("doc_number"):
        return True
    ta, tb = normalize_text(sa.get("doc_title") or ""), normalize_text(sb.get("doc_title") or "")
    return bool(ta) and ta == tb


def _interval(q: dict) -> tuple[float, float]:
    lo = q["canonical_min"] if q["canonical_min"] is not None else q["canonical_value"]
    hi = q["canonical_max"] if q["canonical_max"] is not None else q["canonical_value"]
    return (min(lo, hi), max(lo, hi))


def _values_conflict(a: dict, b: dict, rel: float, abs_tol: float) -> bool:
    a0, a1 = _interval(a)
    b0, b1 = _interval(b)
    tol = max(abs_tol, rel * max(abs(a0), abs(a1), abs(b0), abs(b1)))
    return a1 + tol < b0 or b1 + tol < a0


def _fmt_value(q: dict) -> str:
    return f"{q['raw_text']}" + (f" (= {q['canonical_value']:.4g} {q['canonical_unit']})"
                                 if q.get("canonical_value") is not None and q.get("unit_norm") != q.get("canonical_unit")
                                 else "")


class ConflictDetector:
    def __init__(self, conn: sqlite3.Connection, cfg: Config, run_id: str | None):
        self.conn = conn
        self.cfg = cfg
        self.run_id = run_id
        self.sev_map = dict(cfg.get("conflicts.severity", {}) or {})
        self.default_sev = cfg.get("conflicts.default_severity", "minor")
        self.blocking = set(cfg.get("conflicts.blocking_severities", ["critical"]) or [])
        self.subj_thr = float(cfg.get("conflicts.subject_similarity", 85.0))
        self.rel = float(cfg.get("conflicts.relative_tolerance", 0.005))
        self.abs = float(cfg.get("conflicts.absolute_tolerance", 1e-9))
        self.sources = {r["source_id"]: r for r in rows(conn, "SELECT * FROM sources")}
        self.found: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ helpers
    def _severity(self, ctype: str) -> str:
        return self.sev_map.get(ctype, self.default_sev)

    def _add(self, ctype: str, parameter: str | None, subject: str | None, evidence_ids: list[str],
             values: list[dict], description: str, rule: str, context: dict | None = None,
             revision_related: bool = False, severity: str | None = None) -> None:
        ids = sorted(set(evidence_ids))
        fp = sha256_text(f"{ctype}|{parameter}|{'|'.join(ids)}")
        sev = severity or self._severity(ctype)
        self.found[fp] = {"conflict_type": ctype, "parameter": parameter, "subject": subject, "evidence_ids": ids,
                          "values": values, "description": description, "rule": rule, "context": context or {},
                          "revision_related": revision_related, "severity": sev,
                          "blocking": int(sev in self.blocking)}

    def _src(self, sid: str) -> dict:
        return self.sources.get(sid, {})

    def _label(self, r: dict) -> str:
        s = self._src(r["source_id"])
        rev = f" Rev {s.get('revision')}" if s.get("revision") else ""
        return f"{r['evidence_id']} ({s.get('filename', r['source_id'])}{rev} p.{r['page_no']})"

    # ------------------------------------------------------------------ quantities
    def detect_quantities(self) -> None:
        q = ",".join("?" * len(COMPARABLE))
        obs = rows(self.conn, f"""
            SELECT q.*, e.source_id, e.page_no, e.equipment, e.model, e.component, e.revision, e.block_type,
                   e.section_heading
            FROM quantities q JOIN evidence e ON e.evidence_id = q.evidence_id
            WHERE e.status = 'active' AND (e.dup_role IS NULL OR e.dup_role = 'canonical')
              AND q.parameter IN ({q}) AND q.canonical_value IS NOT NULL AND q.alternate_of IS NULL
              AND q.conversion_status != 'not_convertible'""", COMPARABLE)
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for o in obs:
            qual = o["qualifier"] if o["qualifier"] in ("max", "min") else "nominal"
            basis = o["interval_basis"] if o["parameter"] == "interval" else None
            groups[(o["parameter"], qual, basis)].append(o)
        for (param, qual, basis), lst in groups.items():
            uf = _UF()
            pairs: list[tuple[dict, dict, str]] = []
            for i, j in _candidate_pairs([o["subject"] for o in lst]):
                a, b = lst[i], lst[j]
                if a["evidence_id"] == b["evidence_id"]:
                    continue
                if not subject_match(a["subject"], b["subject"], self.subj_thr):
                    continue
                ok, relation = applicability_compatible(a, b)
                if not ok:
                    continue  # different equipment/model: never compared, never merged
                ca, cb = split_multi(a.get("component")), split_multi(b.get("component"))
                if ca and cb and not (ca & cb) and a["subject"] != b["subject"]:
                    continue  # different components, unless the value subject is literally identical
                if not _values_conflict(a, b, self.rel, self.abs):
                    continue
                pairs.append((a, b, relation))
                uf.union(a["quantity_id"], b["quantity_id"])
            clusters: dict[str, list[tuple[dict, dict, str]]] = defaultdict(list)
            for a, b, rel in pairs:
                clusters[uf.find(a["quantity_id"])].append((a, b, rel))
            for plist in clusters.values():
                members: dict[str, dict] = {}
                relations = set()
                revision_related = False
                for a, b, rel in plist:
                    members[a["quantity_id"]] = a
                    members[b["quantity_id"]] = b
                    relations.add(rel)
                    if _same_family(self._src(a["source_id"]), self._src(b["source_id"])) and \
                            (self._src(a["source_id"]).get("revision") != self._src(b["source_id"]).get("revision")):
                        revision_related = True
                ms = sorted(members.values(), key=lambda m: (m["source_id"], m["page_no"]))
                vals = [{"evidence_id": m["evidence_id"], "source_id": m["source_id"],
                         "revision": self._src(m["source_id"]).get("revision"), "raw_text": m["raw_text"],
                         "canonical_value": m["canonical_value"], "canonical_min": m["canonical_min"],
                         "canonical_max": m["canonical_max"], "canonical_unit": m["canonical_unit"],
                         "conversion_status": m["conversion_status"], "model": m["model"]} for m in ms]
                unsafe = any(m["conversion_status"] in ("unsafe",) for m in ms)
                subject = ms[0]["subject"]
                desc = (f"{param.capitalize()}{' (' + qual + ')' if qual != 'nominal' else ''} for '{subject}': "
                        + " vs ".join(f"{_fmt_value(m)} [{self._label(m)}]" for m in ms))
                if revision_related:
                    desc += ". Values differ between revisions of the same document; no revision is chosen " \
                            "automatically - confirm which revision applies."
                if "model_unspecified_on_one_side" in relations:
                    desc += " Applicability (model) is not stated in all sources."
                if unsafe:
                    desc += " Comparison involves an unsafe/ambiguous unit conversion."
                ctype = param if param in self.sev_map else param
                self._add(ctype, param, subject, [m["evidence_id"] for m in ms], vals, desc,
                          "quantity_mismatch", {"qualifier": qual, "interval_basis": basis,
                                                "relations": sorted(relations)}, revision_related)

    def detect_dual_units(self) -> None:
        for r in rows(self.conn, """
                SELECT q.*, e.source_id, e.page_no FROM quantities q JOIN evidence e ON e.evidence_id=q.evidence_id
                WHERE e.status='active' AND q.alternate_of IS NOT NULL AND q.flags LIKE '%dual_unit_inconsistent%'"""):
            primary = self.conn.execute("SELECT raw_text FROM quantities WHERE quantity_id=?",
                                        (r["alternate_of"],)).fetchone()
            desc = (f"Value stated in two units that do not agree: '{primary[0] if primary else '?'}' vs "
                    f"'{r['raw_text']}' [{self._label(r)}]. Neither value is chosen automatically.")
            self._add("unit_inconsistency", r["parameter"], r["subject"], [r["evidence_id"]],
                      [{"evidence_id": r["evidence_id"], "raw_text": r["raw_text"], "flags": loads(r["flags"], [])}],
                      desc, "dual_unit_inconsistent")

    # ------------------------------------------------------------------ part numbers
    def _part_entries(self) -> list[dict]:
        out = []
        for e in rows(self.conn, "SELECT evidence_id, source_id, page_no, block_type, text, table_json, equipment, model, "
                                 "component FROM evidence WHERE status='active' AND (dup_role IS NULL OR "
                                 "dup_role='canonical') AND (block_type='table' OR text LIKE '%P/N%' OR text LIKE "
                                 "'%PN%' OR text LIKE '%art%no%')"):
            if e["block_type"] == BlockType.TABLE and e["table_json"]:
                tbl = loads(e["table_json"], [])
                if not tbl:
                    continue
                header = [(c or "") for c in tbl[0]]
                pn_col = next((i for i, h in enumerate(header) if _PN_HEADER.search(h)), None)
                desc_col = next((i for i, h in enumerate(header) if _DESC_HEADER.search(h)), None)
                if pn_col is None or desc_col is None:
                    continue
                for r in tbl[1:]:
                    if pn_col < len(r) and desc_col < len(r) and r[pn_col] and r[desc_col]:
                        out.append({**e, "pn": r[pn_col].strip(), "desc": r[desc_col].strip()})
            else:
                for m in _PN_INLINE.finditer(e["text"]):
                    desc = e["text"][:m.start()].strip(" :-(")[-80:]
                    if desc:
                        out.append({**e, "pn": m.group(1).rstrip(".,;)"), "desc": desc})
        return out

    def detect_part_numbers(self) -> None:
        thr = float(self.cfg.get("conflicts.part_description_similarity", 88.0))
        entries = self._part_entries()
        descs = [normalize_text(e["desc"]) for e in entries]
        for i, j in _similar_pairs(descs, thr):
            a, b = entries[i], entries[j]
            if a["evidence_id"] == b["evidence_id"] or a["pn"].upper() == b["pn"].upper():
                continue
            ok, rel = applicability_compatible(a, b)
            if not ok:
                continue
            desc = (f"Part '{a['desc']}' has different part numbers: {a['pn']} [{self._label(a)}] vs "
                    f"{b['pn']} [{self._label(b)}]")
            self._add("part_number", "part_number", normalize_text(a["desc"]), [a["evidence_id"], b["evidence_id"]],
                      [{"evidence_id": a["evidence_id"], "pn": a["pn"], "desc": a["desc"]},
                       {"evidence_id": b["evidence_id"], "pn": b["pn"], "desc": b["desc"]}],
                      desc, "part_number_mismatch", {"relation": rel})

    # ------------------------------------------------------------------ procedures & safety
    def detect_procedures(self) -> None:
        sections: dict[tuple[str, str], dict[str, Any]] = {}
        for e in rows(self.conn, "SELECT evidence_id, source_id, page_no, seq, block_type, text, norm_text, "
                                 "section_heading, section_evidence_id, safety_level, equipment, model, component "
                                 "FROM evidence WHERE status='active' AND section_evidence_id IS NOT NULL AND block_type "
                                 "IN ('list_item','danger','warning','caution','notice') ORDER BY source_id, page_no, seq"):
            key = (e["source_id"], e["section_evidence_id"])
            s = sections.setdefault(key, {"source_id": e["source_id"], "heading": e["section_heading"],
                                          "topic": topic_key(e["section_heading"]), "steps": [], "warnings": [],
                                          "equipment": e["equipment"], "model": e["model"],
                                          "component": e["component"]})
            if e["block_type"] == BlockType.LIST_ITEM:
                s["steps"].append(e)
            else:
                s["warnings"].append(e)
        procs = [s for s in sections.values() if len(s["steps"]) >= 2 or s["warnings"]]
        h_thr = float(self.cfg.get("conflicts.procedure_heading_similarity", 90.0))
        s_thr = float(self.cfg.get("conflicts.procedure_step_similarity", 70.0))
        strip = lambda t: _NUMS.sub("#", t)  # noqa: E731 - number changes are quantity conflicts
        procs = [p for p in procs if p["topic"]]
        for i, j in _similar_pairs([p["topic"] for p in procs], h_thr, scorer=fuzz.token_sort_ratio):
            a, b = procs[i], procs[j]
            if a["source_id"] == b["source_id"]:
                continue
            ok, _ = applicability_compatible(a, b)
            if not ok:
                continue
            # step sequences
            if len(a["steps"]) >= 2 and len(b["steps"]) >= 2:
                sa = [strip(x["norm_text"]) for x in a["steps"]]
                sb = [strip(x["norm_text"]) for x in b["steps"]]
                diffs = []
                if len(sa) != len(sb):
                    diffs.append(f"{len(sa)} vs {len(sb)} steps")
                for k, (x, y) in enumerate(zip(sa, sb), start=1):
                    if fuzz.ratio(x, y) < s_thr:
                        diffs.append(f"step {k} differs")
                if diffs:
                    desc = (f"Procedure '{a['heading']}' differs between sources: {', '.join(diffs[:6])}. "
                            f"Steps are not merged or re-ordered automatically. "
                            f"[{self._src(a['source_id']).get('filename')}] vs "
                            f"[{self._src(b['source_id']).get('filename')}]")
                    self._add("procedure", "procedure", a["topic"],
                              [x["evidence_id"] for x in a["steps"] + b["steps"]],
                              [{"source_id": a["source_id"], "steps": len(sa)},
                               {"source_id": b["source_id"], "steps": len(sb)}],
                              desc, "procedure_sequence_mismatch",
                              revision_related=_same_family(self._src(a["source_id"]), self._src(b["source_id"])))
            # warnings present in one procedure but not in the other
            if a["steps"] and b["steps"]:
                for x, y in ((a, b), (b, a)):
                    for w in x["warnings"]:
                        if not any(fuzz.ratio(w["norm_text"], v["norm_text"]) >= 90 for v in y["warnings"]):
                            desc = (f"Safety instruction present in one source but missing or different in the "
                                    f"other for procedure '{x['heading']}': \"{w['text'][:120]}\" "
                                    f"[{self._label(w)}] not found in "
                                    f"{self._src(y['source_id']).get('filename')}")
                            ids = [w["evidence_id"]] + [s["evidence_id"] for s in y["steps"][:1]] + \
                                  [v["evidence_id"] for v in y["warnings"]]
                            self._add("safety", "safety", x["topic"], ids,
                                      [{"evidence_id": w["evidence_id"], "text": w["text"][:200]}],
                                      desc, "warning_missing_in_other_source")

    def detect_safety_near_duplicates(self) -> None:
        for r in rows(self.conn, """
                SELECT n.*, a.block_type bta, b.block_type btb, a.text ta, b.text tb, a.source_id sa, b.source_id sb,
                       a.page_no pa, b.page_no pb, a.equipment ea, a.model ma, b.equipment eb, b.model mb
                FROM near_duplicates n JOIN evidence a ON a.evidence_id=n.evidence_a JOIN evidence b
                ON b.evidence_id=n.evidence_b WHERE n.status IN ('candidate','not_duplicate')
                AND a.status='active' AND b.status='active'"""):
            if r["bta"] not in BlockType.SAFETY and r["btb"] not in BlockType.SAFETY:
                continue
            flags = set(loads(r["guard_flags"], []))
            relevant = flags & {"numbers_differ", "negation_differs", "warning_or_signal_word_differs", "units_differ"}
            if not relevant:
                continue
            ok, _ = applicability_compatible({"equipment": r["ea"], "model": r["ma"]},
                                             {"equipment": r["eb"], "model": r["mb"]})
            if not ok:
                continue
            desc = (f"Similar safety statements differ ({', '.join(sorted(relevant))}): \"{r['ta'][:100]}\" "
                    f"[{r['evidence_a']}] vs \"{r['tb'][:100]}\" [{r['evidence_b']}]")
            self._add("safety", "safety", None, [r["evidence_a"], r["evidence_b"]],
                      [{"evidence_id": r["evidence_a"], "text": r["ta"][:200]},
                       {"evidence_id": r["evidence_b"], "text": r["tb"][:200]}],
                      desc, "similar_safety_statements_differ")

    # ------------------------------------------------------------------ persistence
    def persist(self) -> dict[str, int]:
        ts = now_iso()
        stats = {"detected": len(self.found), "new": 0, "reopened": 0, "stale": 0}
        with transaction(self.conn):
            existing = {r["fingerprint"]: r for r in rows(self.conn, "SELECT * FROM conflicts")}
            for fp, c in self.found.items():
                old = existing.get(fp)
                if old is None:
                    cid = "CF-" + fp[:10]
                    self.conn.execute(
                        "INSERT INTO conflicts(conflict_id, fingerprint, conflict_type, severity, blocking, parameter, "
                        "subject, context_json, evidence_ids, values_json, description, revision_related, status, "
                        "detection_rule, first_detected_at, last_detected_at, run_id) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (cid, fp, c["conflict_type"], c["severity"], c["blocking"], c["parameter"], c["subject"],
                         dumps(c["context"]), dumps(c["evidence_ids"]), dumps(c["values"]), c["description"],
                         int(c["revision_related"]), ConflictStatus.OPEN, c["rule"], ts, ts, self.run_id))
                    self.conn.executemany("INSERT OR IGNORE INTO conflict_evidence(conflict_id, evidence_id) VALUES(?,?)",
                                          [(cid, e) for e in c["evidence_ids"]])
                    audit.append(self.conn, "system", "conflict.detected", "conflict", cid,
                                 after={"type": c["conflict_type"], "severity": c["severity"],
                                        "evidence": c["evidence_ids"]}, run_id=self.run_id)
                    stats["new"] += 1
                else:
                    new_status = old["status"]
                    if old["status"] == ConflictStatus.STALE:
                        new_status = ConflictStatus.OPEN
                        stats["reopened"] += 1
                        audit.append(self.conn, "system", "conflict.reopened", "conflict", old["conflict_id"],
                                     before={"status": old["status"]}, after={"status": new_status},
                                     reason="re-detected", run_id=self.run_id)
                    self.conn.execute("UPDATE conflicts SET last_detected_at=?, description=?, values_json=?, "
                                      "severity=?, blocking=?, status=?, run_id=? WHERE conflict_id=?",
                                      (ts, c["description"], dumps(c["values"]), c["severity"], c["blocking"],
                                       new_status, self.run_id, old["conflict_id"]))
            for fp, old in existing.items():
                if fp not in self.found and old["status"] == ConflictStatus.OPEN:
                    self.conn.execute("UPDATE conflicts SET status=? WHERE conflict_id=?",
                                      (ConflictStatus.STALE, old["conflict_id"]))
                    audit.append(self.conn, "system", "conflict.stale", "conflict", old["conflict_id"],
                                 before={"status": old["status"]}, after={"status": ConflictStatus.STALE},
                                 reason="no longer detected (evidence changed)", run_id=self.run_id)
                    stats["stale"] += 1
        return stats


def run_conflict_detection(conn: sqlite3.Connection, cfg: Config, run_id: str | None = None) -> dict[str, int]:
    det = ConflictDetector(conn, cfg, run_id)
    det.detect_quantities()
    det.detect_dual_units()
    det.detect_part_numbers()
    det.detect_procedures()
    det.detect_safety_near_duplicates()
    stats = det.persist()
    by_type: dict[str, int] = defaultdict(int)
    for c in det.found.values():
        by_type[c["conflict_type"]] += 1
    stats.update({f"type_{k}": v for k, v in by_type.items()})
    return stats


def open_blocking_evidence(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """evidence_id -> list of open blocking conflict IDs."""
    out: dict[str, list[str]] = defaultdict(list)
    for r in conn.execute("SELECT ce.evidence_id, c.conflict_id FROM conflict_evidence ce JOIN conflicts c "
                          "ON c.conflict_id=ce.conflict_id WHERE c.status='open' AND c.blocking=1"):
        out[r[0]].append(r[1])
    return out
