"""Exact duplicate removal and near-duplicate review candidates.

Exact duplicates
    Identical *normalised* text (see :func:`maintdoc.utils.normalize_text`, which
    preserves every digit, sign and unit) within the same context: the same
    section topic and the same equipment/model applicability. One item becomes
    the canonical statement; the others stay in the evidence register and are
    cited alongside it. Safety statements repeated within one source are never
    collapsed (each occurrence stays in its procedural context).

Near duplicates
    RapidFuzz similarity candidates for *human review only*. Guard checks record
    why a merge would be unsafe (different numbers, units, warnings, negation,
    applicability, revision, step numbers, part numbers). A merge can only be
    confirmed by a reviewer and only when no guard fires.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import Counter, defaultdict
from typing import Any

import numpy as np
from rapidfuzz import fuzz, process

from maintdoc.analysis.classify import split_multi
from maintdoc.config import Config
from maintdoc.constants import BlockType, EvidenceStatus
from maintdoc.db import rows, transaction
from maintdoc.utils import normalize_text, now_iso, numeric_tokens, sha256_text

log = logging.getLogger(__name__)

_HEAD_NUM = re.compile(r"^\s*(?:\d{1,2}(?:\.\d{1,2}){0,4}\.?|[A-Z]\.|[IVX]+\.)\s+")
_NEGATION = re.compile(r"(?i)\b(not|never|no|don't|do not|must not|shall not|without|nicht|kein|niemals)\b")
_SIGNAL = re.compile(r"\b(DANGER|WARNING|CAUTION|NOTICE|NOTE|IMPORTANT|GEFAHR|WARNUNG|VORSICHT|ACHTUNG|HINWEIS)\b")
_PART = re.compile(r"\b[A-Z]{1,4}-?\d{3,}[A-Z0-9\-]*\b|\b\d{3,}-\d{2,}[A-Z0-9\-]*\b")
_STEP = re.compile(r"^\s*(?:step\s*)?\(?(\d{1,3})[.)]")
_UNIT_TOKENS = re.compile(r"(?i)(?<=\d)\s*(n·?m|nm|ft-?lbs?|in-?lbs?|bar|psi|kpa|mpa|°c|°f|mm|cm|m|in|l|ml|kg|g|lbs?|"
                          r"h|hrs?|hours?|min|s|rpm|v|a|kw|w|hz|%)\b")


def topic_key(section_heading: str | None) -> str:
    """Section heading without numbering, normalised (renumbered revisions still match)."""
    if not section_heading:
        return ""
    return normalize_text(_HEAD_NUM.sub("", section_heading))


def applicability_key(r: dict, cfg: Config) -> str:
    eq = ";".join(sorted(split_multi(r.get("equipment"))))
    if cfg.get("dedupe.merge_across_models", False):
        return eq
    return eq + "|" + ";".join(sorted(split_multi(r.get("model"))))


def effective_chapter(r: dict) -> str | None:
    return r.get("chapter_override") or r.get("chapter")


def run_exact_dedupe(conn: sqlite3.Connection, cfg: Config) -> dict[str, int]:
    types = BlockType.DEDUPE_TYPES
    q = ",".join("?" * len(types))
    items = rows(conn, f"SELECT evidence_id, source_id, page_no, seq, block_type, norm_hash, section_heading, chapter, "
                       f"chapter_override, equipment, model, dup_role, canonical_evidence_id FROM evidence "
                       f"WHERE status=? AND block_type IN ({q}) ORDER BY source_id, page_no, seq",
                 (EvidenceStatus.ACTIVE, *types))
    ctx_mode = cfg.get("dedupe.exact_context", "section")
    protect_safety = bool(cfg.get("dedupe.never_merge_safety_within_source", True))
    active_ids = {r["evidence_id"] for r in items}
    groups: dict[tuple, list[dict]] = defaultdict(list)
    occurrence: Counter = Counter()
    preserved_near = 0
    for r in items:
        if r["dup_role"] == "near_duplicate_merged" and r["canonical_evidence_id"] in active_ids:
            preserved_near += 1
            continue  # reviewer-confirmed merge stays
        if ctx_mode == "section":
            ctx = topic_key(r["section_heading"])
        elif ctx_mode == "chapter":
            ctx = effective_chapter(r) or ""
        else:
            ctx = ""
        key = (r["norm_hash"], ctx, applicability_key(r, cfg))
        if protect_safety and r["block_type"] in BlockType.SAFETY:
            occ_key = (key, r["source_id"])
            occurrence[occ_key] += 1
            key = key + (occurrence[occ_key],)
        groups[key].append(r)
    stats = {"groups": 0, "duplicates": 0, "near_merged_preserved": preserved_near}
    ts = now_iso()
    # canonical preference: undamaged, cleanly extracted sources first, then register order
    src_rank = {r["source_id"]: (int(r["is_repaired"] or 0), int(r["extraction_status"] != "complete"))
                for r in rows(conn, "SELECT source_id, is_repaired, extraction_status FROM sources")}
    with transaction(conn):
        conn.execute("DELETE FROM duplicate_groups WHERE kind='exact'")
        updates = []
        for key, members in groups.items():
            if len(members) == 1:
                r = members[0]
                if r["dup_role"] is not None or r["canonical_evidence_id"] is not None:
                    updates.append((None, None, None, r["evidence_id"]))
                continue
            members.sort(key=lambda m: (src_rank.get(m["source_id"], (1, 1)), m["source_id"], m["page_no"], m["seq"]))
            canon = members[0]
            gid = "DG-" + sha256_text("|".join(map(str, key)))[:12]
            updates.append(("canonical", None, gid, canon["evidence_id"]))
            for m in members[1:]:
                updates.append(("exact_duplicate", canon["evidence_id"], gid, m["evidence_id"]))
            conn.execute("INSERT OR REPLACE INTO duplicate_groups(group_id, kind, norm_hash, canonical_id, member_count, "
                         "context_key, updated_at) VALUES(?,?,?,?,?,?,?)",
                         (gid, "exact", key[0], canon["evidence_id"], len(members), "|".join(map(str, key[1:])), ts))
            stats["groups"] += 1
            stats["duplicates"] += len(members) - 1
        conn.executemany("UPDATE evidence SET dup_role=?, canonical_evidence_id=?, dup_group_id=? WHERE evidence_id=?",
                         updates)
        # near-merged items whose canonical disappeared fall back to standalone statements
        conn.execute("UPDATE evidence SET dup_role=NULL, canonical_evidence_id=NULL WHERE dup_role='near_duplicate_merged' "
                     "AND canonical_evidence_id NOT IN (SELECT evidence_id FROM evidence WHERE status='active')")
    return stats


# --------------------------------------------------------------------------- near duplicates
def guard_flags(a: dict, b: dict, cfg: Config) -> list[str]:
    """Reasons why two similar statements must NOT be merged."""
    flags = []
    ta, tb = a["text"], b["text"]
    if numeric_tokens(ta) != numeric_tokens(tb):
        flags.append("numbers_differ")
    ua = Counter(m.group(1).lower() for m in _UNIT_TOKENS.finditer(ta))
    ub = Counter(m.group(1).lower() for m in _UNIT_TOKENS.finditer(tb))
    if ua != ub:
        flags.append("units_differ")
    if (a["block_type"] in BlockType.ADMONITIONS) != (b["block_type"] in BlockType.ADMONITIONS) or \
            set(_SIGNAL.findall(ta)) != set(_SIGNAL.findall(tb)) or a.get("safety_level") != b.get("safety_level"):
        flags.append("warning_or_signal_word_differs")
    if Counter(m.lower() for m in _NEGATION.findall(ta)) != Counter(m.lower() for m in _NEGATION.findall(tb)):
        flags.append("negation_differs")
    if applicability_key(a, cfg) != applicability_key(b, cfg) or \
            (a.get("component") or "") != (b.get("component") or ""):
        flags.append("applicability_differs")
    if a["source_id"] != b["source_id"] and (a.get("revision") or "") != (b.get("revision") or ""):
        flags.append("revision_differs")
    sa, sb = _STEP.match(ta), _STEP.match(tb)
    if (sa or sb) and (not sa or not sb or sa.group(1) != sb.group(1)):
        flags.append("procedure_step_differs")
    if set(_PART.findall(ta)) != set(_PART.findall(tb)):
        flags.append("part_numbers_differ")
    if a["block_type"] != b["block_type"]:
        flags.append("block_type_differs")
    return flags


def _family(block_type: str) -> str:
    if block_type in BlockType.ADMONITIONS:
        return "safety"
    if block_type == BlockType.TABLE:
        return "table"
    return "text"


def run_near_dedupe(conn: sqlite3.Connection, cfg: Config, run_id: str | None = None) -> dict[str, int]:
    thr = float(cfg.get("dedupe.near_threshold", 90.0))
    min_chars = int(cfg.get("dedupe.near_min_chars", 25))
    max_cand = int(cfg.get("dedupe.near_max_candidates", 50000))
    chunk = int(cfg.get("dedupe.near_chunk_size", 512))
    types = BlockType.DEDUPE_TYPES
    q = ",".join("?" * len(types))
    items = rows(conn, f"SELECT e.evidence_id, e.source_id, e.page_no, e.block_type, e.text, e.norm_text, e.norm_hash, "
                       f"e.safety_level, e.equipment, e.model, e.component, e.revision FROM evidence e "
                       f"WHERE e.status=? AND e.block_type IN ({q}) AND (e.dup_role IS NULL OR e.dup_role='canonical') "
                       f"AND LENGTH(e.norm_text) >= ?", (EvidenceStatus.ACTIVE, *types, min_chars))
    by_family: dict[str, list[dict]] = defaultdict(list)
    for r in items:
        by_family[_family(r["block_type"])].append(r)
    found: dict[str, dict[str, Any]] = {}
    truncated = False
    for fam, lst in by_family.items():
        lst.sort(key=lambda r: len(r["norm_text"]))
        texts = [r["norm_text"] for r in lst]
        lengths = np.array([len(t) for t in texts])
        min_ratio = thr / (200.0 - thr)  # Indel ratio bound: shorter/longer >= thr/(200-thr)
        for i0 in range(0, len(lst), chunk):
            block = texts[i0:i0 + chunk]
            lo = i0
            hi_len = lengths[min(i0 + chunk, len(lst)) - 1] / min_ratio
            hi = int(np.searchsorted(lengths, hi_len, side="right"))
            cand = texts[lo:hi]
            if not cand:
                continue
            scores = process.cdist(block, cand, scorer=fuzz.ratio, score_cutoff=thr, dtype=np.uint8, workers=-1)
            ii, jj = np.nonzero(scores)
            for i, j in zip(ii.tolist(), jj.tolist()):
                a_idx, b_idx = i0 + i, lo + j
                if b_idx <= a_idx:
                    continue
                a, b = lst[a_idx], lst[b_idx]
                if a["norm_hash"] == b["norm_hash"]:
                    continue  # identical text in different contexts: not a near duplicate
                pair_ids = sorted([a["evidence_id"], b["evidence_id"]])
                pid = "ND-" + sha256_text("|".join(pair_ids))[:16]
                found[pid] = {"a": a if a["evidence_id"] == pair_ids[0] else b,
                              "b": b if a["evidence_id"] == pair_ids[0] else a, "score": float(scores[i, j])}
                if len(found) >= max_cand:
                    truncated = True
                    break
            if truncated:
                break
        if truncated:
            log.warning("Near-duplicate candidate limit %d reached; raise dedupe.near_max_candidates", max_cand)
            break
    ts = now_iso()
    from maintdoc.utils import dumps
    with transaction(conn):
        existing = {r["pair_id"]: r for r in rows(conn, "SELECT pair_id, status FROM near_duplicates")}
        for pid, c in found.items():
            flags = guard_flags(c["a"], c["b"], cfg)
            if pid in existing:
                conn.execute("UPDATE near_duplicates SET score=?, guard_flags=?, merge_allowed=?, "
                             "status=CASE WHEN status='stale' THEN 'candidate' ELSE status END WHERE pair_id=?",
                             (c["score"], dumps(flags), int(not flags), pid))
            else:
                conn.execute("INSERT INTO near_duplicates(pair_id, evidence_a, evidence_b, score, scorer, guard_flags, "
                             "merge_allowed, status, created_at, run_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (pid, c["a"]["evidence_id"], c["b"]["evidence_id"], c["score"], "rapidfuzz.fuzz.ratio",
                              dumps(flags), int(not flags), "candidate", ts, run_id))
        # candidates no longer found (e.g. evidence changed) become stale unless decided
        for pid, r in existing.items():
            if pid not in found and r["status"] == "candidate":
                conn.execute("UPDATE near_duplicates SET status='stale' WHERE pair_id=?", (pid,))
    return {"candidates": len(found), "truncated": int(truncated),
            "merge_blocked": sum(1 for c in found.values() if guard_flags(c["a"], c["b"], cfg))}
