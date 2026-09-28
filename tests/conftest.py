"""Shared fixtures: a synthetic corpus processed once per session, cloned per test."""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from maintdoc.config import Config
from maintdoc.db import backup_to, open_db, rows
from maintdoc.testing.corpus import make_corpus

REPO = Path(__file__).resolve().parents[1]


def tesseract_available() -> bool:
    try:
        from maintdoc.extraction.ocr import configure_tesseract
        return configure_tesseract(Config.load(REPO / "config.yaml"))[0]
    except Exception:  # noqa: BLE001
        return False


HAS_TESSERACT = tesseract_available()
requires_tesseract = pytest.mark.skipif(not HAS_TESSERACT, reason="Tesseract OCR not installed")


def make_config(base: Path, source_root: Path, **overrides) -> Config:
    ov = {"paths": {"source_root": str(source_root), "work_dir": str(base / "work"),
                    "output_dir": str(base / "output"), "log_dir": str(base / "logs"),
                    "database": str(base / "work" / "maintenance.db")},
          "extraction": {"workers": 1}}
    for k, v in overrides.items():
        ov.setdefault(k, {}).update(v)
    cfg = Config.load(REPO / "config.yaml", overrides=ov)
    cfg.ensure_dirs()
    return cfg


@dataclass
class Workspace:
    cfg: Config
    conn: sqlite3.Connection
    base: Path
    corpus: dict[str, Path]

    def ev(self, where: str, params=()) -> list[dict]:
        return rows(self.conn, f"SELECT * FROM evidence WHERE {where}", params)

    def one(self, sql: str, params=()):
        r = self.conn.execute(sql, params).fetchone()
        return r[0] if r else None


def process(cfg: Config, run_id: str = "RUN-test") -> sqlite3.Connection:
    from maintdoc.analysis.validate import run_validate
    from maintdoc.extraction.runner import run_extraction
    from maintdoc.inventory import run_inventory
    conn = open_db(cfg.db_path)
    run_inventory(conn, cfg, run_id, show_progress=False)
    run_extraction(conn, cfg, run_id, workers=1, show_progress=False)
    run_validate(conn, cfg, run_id, show_progress=False)
    return conn


@pytest.fixture(scope="session")
def processed(tmp_path_factory) -> Workspace:
    base = tmp_path_factory.mktemp("processed")
    corpus = make_corpus(base / "sources")
    cfg = make_config(base, base / "sources")
    conn = process(cfg)
    return Workspace(cfg, conn, base, corpus)


@pytest.fixture()
def ws(processed: Workspace, tmp_path: Path) -> Workspace:
    """Independent clone of the processed workspace (database + outputs); sources are shared read-only."""
    cfg = make_config(tmp_path, processed.cfg.source_root)
    backup_to(processed.conn, cfg.db_path)
    conn = open_db(cfg.db_path)
    yield Workspace(cfg, conn, tmp_path, processed.corpus)
    conn.close()


@pytest.fixture()
def own_sources(tmp_path: Path) -> Workspace:
    """Fresh corpus + processed workspace that the test may modify (sources included)."""
    corpus = make_corpus(tmp_path / "sources", include_scanned=HAS_TESSERACT)
    cfg = make_config(tmp_path, tmp_path / "sources")
    conn = process(cfg)
    yield Workspace(cfg, conn, tmp_path, corpus)
    conn.close()


def ev_by_text(w: Workspace, fragment: str, extra: str = "") -> dict:
    r = w.ev(f"status='active' AND text LIKE ? {extra} ORDER BY source_id, page_no, seq", (f"%{fragment}%",))
    assert r, f"no evidence containing {fragment!r}"
    return r[0]


def source_id(w: Workspace, name_fragment: str) -> str:
    sid = w.one("SELECT source_id FROM sources WHERE rel_path LIKE ?", (f"%{name_fragment}%",))
    assert sid, name_fragment
    return sid


def copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst)


REVIEWER = "Test Engineer"


def resolve_all_conflicts(w: Workspace) -> None:
    """Record engineering decisions for every open conflict (test data: newest revision / manual table wins)."""
    from maintdoc.review import service
    from maintdoc.utils import loads
    for c in rows(w.conn, "SELECT * FROM conflicts WHERE status='open'"):
        ids = loads(c["evidence_ids"])
        info = {r["evidence_id"]: r for r in rows(
            w.conn, f"SELECT e.evidence_id, s.revision, s.filename FROM evidence e JOIN sources s USING(source_id) "
                    f"WHERE evidence_id IN ({','.join('?' * len(ids))})", ids)}
        auth = [i for i in ids if info[i]["revision"] == "C"] or \
               [i for i in ids if "Pump" not in info[i]["filename"]] or ids[:1]
        service.resolve_conflict(w.conn, w.cfg, c["conflict_id"], "select_authoritative", REVIEWER,
                                 "test decision: current revision applies", auth)


def complete_review(w: Workspace) -> dict:
    """Simulate a complete human review: conflicts, visual checks, OCR confirmation, approvals."""
    from maintdoc.review import service
    resolve_all_conflicts(w)
    for v in rows(w.conn, "SELECT check_id FROM visual_checks WHERE status='pending' AND target_type='source_page'"):
        service.decide_visual_check(w.conn, w.cfg, v["check_id"], "passed", REVIEWER, "compared with source")
    for e in w.ev("extraction_method='tesseract_ocr' AND status='active' AND in_manual=1"):
        service.set_ocr_verified(w.conn, w.cfg, e["evidence_id"], True, REVIEWER, "checked against scan")
    ids = [r[0] for r in w.conn.execute(
        "SELECT evidence_id FROM evidence WHERE status='active' AND in_manual=1 AND (dup_role IS NULL OR "
        "dup_role='canonical') AND review_status NOT IN ('approved','rejected')")]
    res = service.bulk_approve(w.conn, w.cfg, ids, REVIEWER, "test approval")
    for eid, reasons in res["blocked"]:
        service.reject_evidence(w.conn, w.cfg, eid, REVIEWER, "not usable: " + reasons[0])
    return res


def pass_output_checks(w: Workspace) -> None:
    from maintdoc.review import service
    for v in rows(w.conn, "SELECT check_id FROM visual_checks WHERE status='pending'"):
        service.decide_visual_check(w.conn, w.cfg, v["check_id"], "passed", REVIEWER, "layout checked")
