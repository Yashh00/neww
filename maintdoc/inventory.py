"""Source discovery and registration.

* recursive PDF discovery below ``paths.source_root``
* OneDrive online-only placeholder detection (never hydrates files)
* file integrity verification (header, EOF marker, open, encryption, page load)
* streaming SHA-256 hashing
* exact duplicate detection (identical SHA-256)
* incremental registration: unchanged files are skipped, changed files are
  re-registered and all dependent approvals are invalidated, missing files
  are marked missing (never deleted)
* document metadata: equipment, model, component, revision, date, number, title
"""

from __future__ import annotations

import datetime as _dt
import fnmatch
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymupdf

from maintdoc import audit
from maintdoc.analysis.classify import DocumentClassifier
from maintdoc.config import Config
from maintdoc.constants import ExtractionStatus, SourceStatus
from maintdoc.db import rows, transaction
from maintdoc.errors import record_error
from maintdoc.invalidation import supersede_source_versions
from maintdoc.onedrive import HYDRATE_HINT, detect_placeholder
from maintdoc.progress import Progress
from maintdoc.utils import dumps, now_iso, rel_posix, sha256_file

log = logging.getLogger(__name__)

pymupdf.TOOLS.mupdf_display_errors(False)
pymupdf.TOOLS.mupdf_display_warnings(False)
if hasattr(pymupdf, "no_recommend_layout"):
    pymupdf.no_recommend_layout()


@dataclass
class InventoryStats:
    discovered: int = 0
    new: int = 0
    modified: int = 0
    unchanged: int = 0
    touched_only: int = 0
    missing: int = 0
    restored: int = 0
    placeholders: int = 0
    corrupt: int = 0
    encrypted: int = 0
    empty: int = 0
    unreadable: int = 0
    duplicates: int = 0
    invalidated_approvals: int = 0
    by_status: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class IntegrityResult:
    status: str
    detail: str = ""
    errors: list[tuple[str, str, dict]] = field(default_factory=list)
    page_count: int | None = None
    pdf_version: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    is_encrypted: bool = False
    is_repaired: bool = False
    first_text: str = ""
    first_title: str | None = None


# --------------------------------------------------------------------------- discovery
def _excluded(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def discover(cfg: Config) -> list[Path]:
    """Recursively list candidate PDF files (deterministic order)."""
    root = cfg.source_root
    if not root.exists():
        raise FileNotFoundError(f"Source root does not exist: {root}")
    exts = {e.lower() for e in cfg.get("inventory.include_extensions", [".pdf"])}
    dir_ex = cfg.get("inventory.exclude_dir_patterns", [])
    file_ex = cfg.get("inventory.exclude_file_patterns", [])
    follow = bool(cfg.get("inventory.follow_symlinks", False))
    found: list[Path] = []
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = sorted(it, key=lambda e: e.name.lower())
        except OSError as exc:
            log.error("Cannot list directory %s: %s", d, exc)
            continue
        subdirs = []
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=follow):
                    if not _excluded(e.name, dir_ex):
                        subdirs.append(Path(e.path))
                elif e.is_file(follow_symlinks=follow):
                    if os.path.splitext(e.name)[1].lower() in exts and not _excluded(e.name, file_ex):
                        found.append(Path(e.path))
            except OSError as exc:
                log.error("Cannot stat %s: %s", e.path, exc)
        stack.extend(reversed(subdirs))
    return sorted(found, key=lambda p: rel_posix(p, root).lower())


# --------------------------------------------------------------------------- integrity
def _first_page_title(page: pymupdf.Page) -> str | None:
    best: tuple[float, str] | None = None
    try:
        d = page.get_text("dict")
    except Exception:  # noqa: BLE001 - title is optional
        return None
    for b in d.get("blocks", []):
        for line in b.get("lines", []):
            txt = "".join(s.get("text", "") for s in line.get("spans", [])).strip()
            if len(txt) < 4 or len(txt) > 160:
                continue
            size = max((s.get("size", 0) for s in line.get("spans", [])), default=0)
            if best is None or size > best[0] + 0.5:
                best = (size, txt)
    return best[1] if best else None


def check_integrity(path: Path, cfg: Config) -> IntegrityResult:
    res = IntegrityResult(status=SourceStatus.OK)
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            head = fh.read(1024)
            fh.seek(max(0, size - 2048))
            tail = fh.read(2048)
    except OSError as exc:
        return IntegrityResult(SourceStatus.UNREADABLE, str(exc),
                               [("FILE_UNREADABLE", f"Cannot read file: {exc}", {})])
    if b"%PDF-" not in head:
        return IntegrityResult(SourceStatus.CORRUPT, "no %PDF- header",
                               [("NOT_A_PDF", "File has no PDF header in the first 1024 bytes", {})])
    if b"%%EOF" not in tail:
        res.errors.append(("PDF_NO_EOF", "No %%EOF marker near end of file (file may be truncated)", {}))
    pymupdf.TOOLS.reset_mupdf_warnings()
    try:
        doc = pymupdf.open(str(path))
    except Exception as exc:  # noqa: BLE001 - any parser failure means corrupt
        return IntegrityResult(SourceStatus.CORRUPT, f"open failed: {exc}",
                               [("CORRUPT_PDF", f"PDF could not be opened: {exc}",
                                 {"mupdf": pymupdf.TOOLS.mupdf_warnings()})] + res.errors)
    try:
        res.metadata = dict(doc.metadata or {})
        res.pdf_version = res.metadata.get("format")
        res.is_encrypted = bool(doc.is_encrypted)
        if doc.needs_pass:
            res.status = SourceStatus.ENCRYPTED
            res.detail = "password required"
            res.errors.append(("ENCRYPTED_PDF", "PDF requires a password; content not extracted",
                               {"encryption": res.metadata.get("encryption")}))
            return res
        res.is_repaired = bool(getattr(doc, "is_repaired", False))
        if res.is_repaired:
            res.errors.append(("PDF_REPAIRED", "PDF cross-reference table was repaired on open",
                               {"mupdf": pymupdf.TOOLS.mupdf_warnings()[:2000]}))
        res.page_count = doc.page_count
        if doc.page_count == 0:
            res.status = SourceStatus.EMPTY_DOCUMENT
            res.detail = "0 pages"
            res.errors.append(("EMPTY_DOCUMENT", "PDF contains no pages", {}))
            return res
        bad_pages = []
        if cfg.get("inventory.page_load_check", True):
            for i in range(doc.page_count):
                try:
                    doc.load_page(i)
                except Exception as exc:  # noqa: BLE001
                    bad_pages.append((i + 1, str(exc)))
        if bad_pages and len(bad_pages) == doc.page_count:
            res.status = SourceStatus.CORRUPT
            res.detail = "no page could be loaded"
            res.errors.append(("CORRUPT_PDF", "No page of the PDF could be loaded", {"pages": bad_pages[:20]}))
            return res
        for pno, msg in bad_pages:
            res.errors.append(("PAGE_LOAD_FAILED", f"Page {pno} could not be loaded: {msg}", {"page": pno}))
        texts = []
        for i in range(min(doc.page_count, int(cfg.get("metadata.scan_pages", 2)))):
            try:
                page = doc.load_page(i)
                texts.append(page.get_text("text"))
                if i == 0:
                    res.first_title = _first_page_title(page)
            except Exception:  # noqa: BLE001 - reported above
                continue
        res.first_text = "\n".join(texts)
    finally:
        doc.close()
    return res


# --------------------------------------------------------------------------- registration
def _mtime_iso(st: os.stat_result) -> str:
    return _dt.datetime.fromtimestamp(st.st_mtime, tz=_dt.timezone.utc).isoformat(timespec="seconds")


def _next_source_id(conn: sqlite3.Connection) -> str:
    r = conn.execute("SELECT MAX(CAST(SUBSTR(source_id, 5) AS INTEGER)) FROM sources").fetchone()
    return f"SRC-{(r[0] or 0) + 1:05d}"


def _meta_columns(integ: IntegrityResult, doc_meta) -> dict[str, Any]:
    m = integ.metadata or {}
    cols = {
        "page_count": integ.page_count, "pdf_version": integ.pdf_version,
        "is_encrypted": int(integ.is_encrypted), "is_repaired": int(integ.is_repaired),
        "pdf_title": m.get("title") or None, "pdf_author": m.get("author") or None,
        "pdf_subject": m.get("subject") or None, "pdf_keywords": m.get("keywords") or None,
        "pdf_creator": m.get("creator") or None, "pdf_producer": m.get("producer") or None,
        "pdf_created": m.get("creationDate") or None, "pdf_modified": m.get("modDate") or None,
    }
    if doc_meta is not None:
        for f in DocumentClassifier.FIELDS:
            cols[f] = doc_meta.values.get(f)
        origins = dict(doc_meta.origins)
        if doc_meta.ambiguous:
            origins["_ambiguous"] = doc_meta.ambiguous
        cols["metadata_origin_json"] = dumps(origins)
    return cols


def _record_integrity_errors(conn, source_id: str, sha: str | None, integ: IntegrityResult, run_id: str) -> None:
    for code, msg, details in integ.errors:
        record_error(conn, code, msg, stage="integrity", source_id=source_id, source_sha256=sha,
                     page_no=details.get("page") if isinstance(details, dict) else None,
                     details=details, run_id=run_id)


def _record_metadata_errors(conn, cfg: Config, source_id: str, sha: str, doc_meta, run_id: str) -> None:
    if doc_meta is None:
        return
    for f in cfg.get("metadata.required_fields", []):
        if not doc_meta.values.get(f):
            record_error(conn, "MISSING_METADATA", f"Metadata field '{f}' could not be determined",
                         stage="metadata", source_id=source_id, source_sha256=sha, key=f,
                         details={"field": f})
    for f, vals in (doc_meta.ambiguous or {}).items():
        record_error(conn, "AMBIGUOUS_METADATA",
                     f"Several values found for '{f}': {', '.join(map(str, vals))[:200]}; "
                     f"used '{doc_meta.values.get(f)}' from {doc_meta.origins.get(f, {}).get('origin')}",
                     stage="metadata", source_id=source_id, source_sha256=sha, key=f,
                     details={"field": f, "candidates": vals}, run_id=run_id)


def run_inventory(conn: sqlite3.Connection, cfg: Config, run_id: str, *, rehash: bool = False,
                  limit: int | None = None, show_progress: bool = True) -> InventoryStats:
    stats = InventoryStats()
    root = cfg.source_root
    files = discover(cfg)
    if limit:
        files = files[:limit]
    stats.discovered = len(files)
    log.info("Discovered %d PDF file(s) under %s", len(files), root)
    doc_cls = DocumentClassifier(cfg)
    existing = {r["rel_path"]: r for r in rows(conn, "SELECT * FROM sources")}
    seen: set[str] = set()
    trust = bool(cfg.get("inventory.trust_size_and_mtime", True)) and not rehash
    zero_rule = bool(cfg.get("onedrive.treat_zero_allocated_blocks_as_placeholder", True))
    detect_ph = bool(cfg.get("onedrive.detect_placeholders", True))
    min_size = int(cfg.get("inventory.min_file_size_bytes", 64))
    prog = Progress(len(files), "inventory", enabled=show_progress)

    for path in files:
        rel = rel_posix(path, root)
        seen.add(rel)
        old = existing.get(rel)
        ts = now_iso()
        try:
            st = path.stat()
        except OSError as exc:
            _register_problem(conn, cfg, old, path, rel, SourceStatus.UNREADABLE, str(exc), None, run_id,
                              ("FILE_UNREADABLE", f"Cannot stat file: {exc}", {}))
            stats.unreadable += 1
            prog.update(note=rel[-60:])
            continue
        mtime = _mtime_iso(st)
        ph = detect_placeholder(st, zero_rule) if detect_ph else None
        if ph is not None and ph.is_placeholder:
            _register_problem(conn, cfg, old, path, rel, SourceStatus.OFFLINE_PLACEHOLDER, ph.reason, st, run_id,
                              ("OFFLINE_PLACEHOLDER", f"OneDrive online-only file ({ph.reason}). {HYDRATE_HINT}",
                               {"attributes": hex(ph.attributes)}))
            stats.placeholders += 1
            prog.update(note=rel[-60:])
            continue
        if (trust and old is not None and old["present"] and old["sha256"] and old["file_size"] == st.st_size
                and old["mtime"] == mtime and old["status"] in (SourceStatus.OK, SourceStatus.DUPLICATE,
                                                                SourceStatus.CORRUPT, SourceStatus.ENCRYPTED,
                                                                SourceStatus.EMPTY_DOCUMENT)):
            with transaction(conn):
                conn.execute("UPDATE sources SET last_seen_at=?, last_run_id=? WHERE source_id=?",
                             (ts, run_id, old["source_id"]))
            stats.unchanged += 1
            prog.update(note=rel[-60:])
            continue
        try:
            sha = sha256_file(path)
        except OSError as exc:
            _register_problem(conn, cfg, old, path, rel, SourceStatus.UNREADABLE, str(exc), st, run_id,
                              ("FILE_UNREADABLE", f"Cannot read file for hashing: {exc}", {}))
            stats.unreadable += 1
            prog.update(note=rel[-60:])
            continue

        if old is not None and old["sha256"] == sha and old["present"] and old["status"] not in (
                SourceStatus.UNREADABLE, SourceStatus.OFFLINE_PLACEHOLDER, SourceStatus.MISSING):
            with transaction(conn):
                conn.execute("UPDATE sources SET mtime=?, file_size=?, last_seen_at=?, last_run_id=?, abs_path=? "
                             "WHERE source_id=?", (mtime, st.st_size, ts, run_id, str(path), old["source_id"]))
            stats.touched_only += 1
            stats.unchanged += 1
            prog.update(note=rel[-60:])
            continue

        if st.st_size < min_size:
            integ = IntegrityResult(SourceStatus.CORRUPT, f"file too small ({st.st_size} bytes)",
                                    [("CORRUPT_PDF", f"File too small to be a PDF ({st.st_size} bytes)", {})])
        else:
            integ = check_integrity(path, cfg)
        doc_meta = None
        if integ.status == SourceStatus.OK:
            doc_meta = doc_cls.classify(rel, integ.metadata, integ.first_text, integ.first_title)
        cols = _meta_columns(integ, doc_meta)
        ext_status = ExtractionStatus.PENDING if integ.status == SourceStatus.OK else ExtractionStatus.NOT_APPLICABLE

        with transaction(conn):
            if old is None:
                source_id = _next_source_id(conn)
                conn.execute(
                    "INSERT INTO sources(source_id, rel_path, abs_path, filename, file_size, mtime, sha256, status, "
                    "status_detail, extraction_status, first_seen_at, last_seen_at, last_changed_at, last_run_id, "
                    "present) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                    (source_id, rel, str(path), path.name, st.st_size, mtime, sha, integ.status, integ.detail,
                     ext_status, ts, ts, ts, run_id))
                conn.execute("INSERT INTO source_versions(source_id, sha256, file_size, mtime, change_type, "
                             "detected_at, run_id) VALUES(?,?,?,?,?,?,?)",
                             (source_id, sha, st.st_size, mtime, "new", ts, run_id))
                audit.append(conn, "system", "source.registered", "source", source_id,
                             after={"rel_path": rel, "sha256": sha, "status": integ.status}, run_id=run_id)
                stats.new += 1
            else:
                source_id = old["source_id"]
                change = "restored" if not old["present"] else "modified"
                conn.execute(
                    "UPDATE sources SET abs_path=?, filename=?, file_size=?, mtime=?, previous_sha256=sha256, "
                    "sha256=?, status=?, status_detail=?, duplicate_of=NULL, extraction_status=?, "
                    "last_seen_at=?, last_changed_at=?, last_run_id=?, present=1 WHERE source_id=?",
                    (str(path), path.name, st.st_size, mtime, sha, integ.status, integ.detail, ext_status,
                     ts, ts, run_id, source_id))
                conn.execute("INSERT INTO source_versions(source_id, sha256, file_size, mtime, change_type, "
                             "detected_at, run_id) VALUES(?,?,?,?,?,?,?)",
                             (source_id, sha, st.st_size, mtime, change, ts, run_id))
                if old["sha256"] and old["sha256"] != sha:
                    record_error(conn, "SOURCE_CHANGED",
                                 f"Content changed ({old['sha256'][:12]} -> {sha[:12]}); dependent approvals "
                                 f"invalidated and source queued for re-extraction",
                                 stage="inventory", source_id=source_id, source_sha256=sha,
                                 details={"old_sha256": old["sha256"], "new_sha256": sha}, run_id=run_id)
                counts = supersede_source_versions(conn, source_id, sha, f"source file changed ({change})", run_id)
                stats.invalidated_approvals += counts.get("approvals", 0)
                audit.append(conn, "system", f"source.{change}", "source", source_id,
                             before={"sha256": old["sha256"], "status": old["status"]},
                             after={"sha256": sha, "status": integ.status, "invalidated": counts}, run_id=run_id)
                if change == "restored":
                    stats.restored += 1
                else:
                    stats.modified += 1
            sets = ", ".join(f"{k}=?" for k in cols)
            conn.execute(f"UPDATE sources SET {sets} WHERE source_id=?", (*cols.values(), source_id))
            _record_integrity_errors(conn, source_id, sha, integ, run_id)
            _record_metadata_errors(conn, cfg, source_id, sha, doc_meta, run_id)
        if integ.status == SourceStatus.CORRUPT:
            stats.corrupt += 1
        elif integ.status == SourceStatus.ENCRYPTED:
            stats.encrypted += 1
        elif integ.status == SourceStatus.EMPTY_DOCUMENT:
            stats.empty += 1
        elif integ.status == SourceStatus.UNREADABLE:
            stats.unreadable += 1
        prog.update(note=rel[-60:])
    prog.close()

    # missing files (never deleted from the register)
    for rel, old in existing.items():
        if rel in seen or not old["present"]:
            continue
        if limit:
            continue  # partial scans cannot conclude that a file is missing
        ts = now_iso()
        with transaction(conn):
            conn.execute("UPDATE sources SET present=0, status=?, status_detail=?, extraction_status=?, "
                         "last_changed_at=?, last_run_id=? WHERE source_id=?",
                         (SourceStatus.MISSING, "file no longer found under source root",
                          ExtractionStatus.NOT_APPLICABLE, ts, run_id, old["source_id"]))
            conn.execute("INSERT INTO source_versions(source_id, sha256, file_size, mtime, change_type, "
                         "detected_at, run_id) VALUES(?,?,?,?,?,?,?)",
                         (old["source_id"], old["sha256"], old["file_size"], old["mtime"], "missing", ts, run_id))
            record_error(conn, "SOURCE_MISSING", f"Registered file no longer present: {rel}", stage="inventory",
                         source_id=old["source_id"], source_sha256=old["sha256"], run_id=run_id)
            counts = supersede_source_versions(conn, old["source_id"], None, "source file missing", run_id)
            stats.invalidated_approvals += counts.get("approvals", 0)
            audit.append(conn, "system", "source.missing", "source", old["source_id"],
                         before={"status": old["status"]}, after={"status": SourceStatus.MISSING, **counts},
                         run_id=run_id)
        stats.missing += 1

    stats.duplicates = refresh_duplicates(conn, run_id)
    for r in conn.execute("SELECT status, COUNT(*) n FROM sources GROUP BY status"):
        stats.by_status[r["status"]] = r["n"]
    return stats


def _register_problem(conn: sqlite3.Connection, cfg: Config, old: dict | None, path: Path, rel: str, status: str,
                      detail: str, st: os.stat_result | None, run_id: str, error: tuple[str, str, dict]) -> None:
    """Register a file whose content cannot be read (placeholder / unreadable) without touching it."""
    ts = now_iso()
    size = st.st_size if st else None
    mtime = _mtime_iso(st) if st else None
    with transaction(conn):
        if old is None:
            source_id = _next_source_id(conn)
            conn.execute(
                "INSERT INTO sources(source_id, rel_path, abs_path, filename, file_size, mtime, status, status_detail, "
                "extraction_status, first_seen_at, last_seen_at, last_run_id, present) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (source_id, rel, str(path), path.name, size, mtime, status, detail,
                 ExtractionStatus.NOT_APPLICABLE, ts, ts, run_id))
            conn.execute("INSERT INTO source_versions(source_id, sha256, file_size, mtime, change_type, detected_at, "
                         "run_id) VALUES(?,?,?,?,?,?,?)", (source_id, None, size, mtime, "new", ts, run_id))
            audit.append(conn, "system", "source.registered", "source", source_id,
                         after={"rel_path": rel, "status": status}, run_id=run_id)
            sha = None
        else:
            source_id = old["source_id"]
            sha = old["sha256"]
            # content cannot be verified; keep the last known hash but block processing
            conn.execute("UPDATE sources SET status=?, status_detail=?, file_size=?, mtime=?, last_seen_at=?, "
                         "last_run_id=?, present=1 WHERE source_id=?",
                         (status, detail, size, mtime, ts, run_id, source_id))
            if old["status"] != status:
                audit.append(conn, "system", "source.status", "source", source_id,
                             before={"status": old["status"]}, after={"status": status, "detail": detail},
                             run_id=run_id)
        code, msg, details = error
        record_error(conn, code, msg, stage="inventory", source_id=source_id, source_sha256=sha,
                     details=details, run_id=run_id, key=mtime)


def refresh_duplicates(conn: sqlite3.Connection, run_id: str | None = None) -> int:
    """Exact file duplicates: identical SHA-256 among present, readable sources.

    The lowest source_id is the primary; the others are 'duplicate' and are not
    extracted. They remain in the register and are cited alongside the primary.
    """
    n = 0
    with transaction(conn):
        groups: dict[str, list[dict]] = {}
        # primary = registered first (stable across runs), then shallowest path, then name
        for r in rows(conn, "SELECT source_id, sha256, status, extraction_status, duplicate_of, "
                            "(SELECT COALESCE(v.run_id, v.detected_at) FROM source_versions v "
                            " WHERE v.source_id=s.source_id ORDER BY v.version_id LIMIT 1) AS first_run "
                            "FROM sources s WHERE present=1 AND sha256 IS NOT NULL AND status IN (?, ?) "
                            "ORDER BY first_run, LENGTH(rel_path) - LENGTH(REPLACE(rel_path, '/', '')), "
                            "rel_path",
                      (SourceStatus.OK, SourceStatus.DUPLICATE)):
            groups.setdefault(r["sha256"], []).append(r)
        for sha, members in groups.items():
            primary = members[0]
            if primary["status"] == SourceStatus.DUPLICATE:
                conn.execute("UPDATE sources SET status=?, duplicate_of=NULL, extraction_status=? WHERE source_id=?",
                             (SourceStatus.OK, ExtractionStatus.PENDING, primary["source_id"]))
                audit.append(conn, "system", "source.primary_of_duplicates", "source", primary["source_id"],
                             after={"sha256": sha}, run_id=run_id)
            for dup in members[1:]:
                n += 1
                if dup["status"] != SourceStatus.DUPLICATE or dup["duplicate_of"] != primary["source_id"]:
                    conn.execute("UPDATE sources SET status=?, duplicate_of=?, status_detail=?, extraction_status=? "
                                 "WHERE source_id=?",
                                 (SourceStatus.DUPLICATE, primary["source_id"],
                                  f"identical SHA-256 to {primary['source_id']}", ExtractionStatus.SKIPPED_DUPLICATE,
                                  dup["source_id"]))
                    # any evidence previously extracted from this file is now cited via the primary
                    supersede_source_versions(conn, dup["source_id"], "__duplicate__",
                                              f"exact duplicate of {primary['source_id']}", run_id)
                    audit.append(conn, "system", "source.duplicate", "source", dup["source_id"],
                                 after={"duplicate_of": primary["source_id"], "sha256": sha}, run_id=run_id)
    return n


def plan_inventory(conn: sqlite3.Connection, cfg: Config) -> dict[str, list[str]]:
    """Dry-run preview without hashing: which files look new/changed/unchanged/missing."""
    root = cfg.source_root
    files = discover(cfg)
    existing = {r["rel_path"]: r for r in rows(conn, "SELECT * FROM sources")}
    plan: dict[str, list[str]] = {"new": [], "possibly_changed": [], "unchanged": [], "placeholder": [],
                                  "missing": []}
    zero_rule = bool(cfg.get("onedrive.treat_zero_allocated_blocks_as_placeholder", True))
    seen = set()
    for p in files:
        rel = rel_posix(p, root)
        seen.add(rel)
        st = p.stat()
        if detect_placeholder(st, zero_rule).is_placeholder:
            plan["placeholder"].append(rel)
        elif rel not in existing:
            plan["new"].append(rel)
        elif existing[rel]["file_size"] != st.st_size or existing[rel]["mtime"] != _mtime_iso(st):
            plan["possibly_changed"].append(rel)
        else:
            plan["unchanged"].append(rel)
    plan["missing"] = [r for r, o in existing.items() if r not in seen and o["present"]]
    return plan
