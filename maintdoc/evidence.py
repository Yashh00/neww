"""Evidence identifiers and persistence helpers.

Evidence IDs are deterministic: ``EV-<source#>-P<page>-<seq>-<hash6>`` where
hash6 is derived from the source SHA-256, page, sequence, extraction method
and original text. Re-extracting an unchanged file with unchanged settings
reproduces the same IDs (review state is preserved); any change of the
source content produces new IDs, so old approvals can never silently carry
over to new content.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from maintdoc.utils import sha256_text

EVIDENCE_ID_RE = re.compile(r"\bEV-\d{5}-P\d{4}-\d{3,}-[0-9a-f]{6}\b")


def make_evidence_id(source_id: str, source_sha256: str, page_no: int, seq: int, method: str, text: str) -> str:
    num = int(source_id.split("-")[1])
    h = sha256_text(f"{source_sha256}|{page_no}|{seq}|{method}|{text}")[:6]
    return f"EV-{num:05d}-P{page_no:04d}-{seq:03d}-{h}"


def find_evidence_ids(text: str) -> list[str]:
    return EVIDENCE_ID_RE.findall(text or "")


EXTRACTION_FIELDS = (
    "evidence_id", "source_id", "source_sha256", "page_no", "seq", "block_type", "text", "norm_text", "norm_hash",
    "heading_level", "section_path", "section_heading", "section_evidence_id", "bbox_x0", "bbox_y0", "bbox_x1",
    "bbox_y1", "bbox_approx", "extraction_method", "ocr_confidence", "font_size", "is_bold", "column_index",
    "table_json", "figure_path", "related_evidence_id", "safety_level", "quality_score", "quality_flags", "status",
    "status_reason", "created_at", "run_id",
)
_UPDATABLE = ("seq", "block_type", "heading_level", "section_path", "section_heading", "section_evidence_id",
              "bbox_x0", "bbox_y0", "bbox_x1", "bbox_y1", "bbox_approx", "ocr_confidence", "font_size", "is_bold",
              "column_index", "table_json", "figure_path", "related_evidence_id", "safety_level", "quality_score",
              "quality_flags", "status", "status_reason")


def upsert_evidence(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    """Insert new evidence; for existing IDs refresh extraction metadata only (never text/review state)."""
    if not rows:
        return
    cols = ",".join(EXTRACTION_FIELDS)
    qs = ",".join("?" * len(EXTRACTION_FIELDS))
    upd = ",".join(f"{c}=excluded.{c}" for c in _UPDATABLE)
    conn.executemany(
        f"INSERT INTO evidence({cols}) VALUES({qs}) ON CONFLICT(evidence_id) DO UPDATE SET {upd}",
        [tuple(r.get(c) for c in EXTRACTION_FIELDS) for r in rows],
    )
