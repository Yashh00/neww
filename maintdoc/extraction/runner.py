"""Extraction orchestration: planning, unchanged-file skips and controlled parallelism."""

from __future__ import annotations

import logging
import multiprocessing as mp
import sqlite3
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any

from maintdoc.config import Config
from maintdoc.constants import ExtractionStatus, SourceStatus
from maintdoc.db import rows, transaction
from maintdoc.errors import record_error
from maintdoc.extraction.pipeline import extract_source
from maintdoc.logging_setup import start_queue_listener, worker_logging
from maintdoc.progress import Progress

log = logging.getLogger(__name__)


def plan_extraction(conn: sqlite3.Connection, cfg: Config, force: bool = False,
                    source_ids: list[str] | None = None, limit: int | None = None) -> dict[str, list[dict]]:
    """Decide per source: extract, resume or skip (with reason)."""
    cfg_hash = cfg.extraction_fingerprint()
    reextract_cfg = bool(cfg.get("extraction.reextract_on_config_change", True))
    plan: dict[str, list[dict]] = {"extract": [], "resume": [], "skip": []}
    q = "SELECT s.*, c.source_sha256 AS ck_sha, c.config_hash AS ck_cfg, c.last_page_done AS ck_page, " \
        "c.status AS ck_status FROM sources s LEFT JOIN extraction_checkpoints c ON c.source_id=s.source_id " \
        "WHERE s.present=1 ORDER BY s.source_id"
    for s in rows(conn, q):
        if source_ids and s["source_id"] not in source_ids:
            continue
        item = {"source_id": s["source_id"], "rel_path": s["rel_path"], "pages": s["page_count"]}
        if s["status"] != SourceStatus.OK:
            plan["skip"].append({**item, "reason": s["status"]})
            continue
        done = s["extraction_status"] in ExtractionStatus.DONE_OK and s["extracted_sha256"] == s["sha256"]
        cfg_same = s["extraction_config_hash"] == cfg_hash or not reextract_cfg
        if not force and done and cfg_same:
            plan["skip"].append({**item, "reason": "unchanged"})
            continue
        if not force and s["extraction_status"] == ExtractionStatus.FAILED and s["extracted_sha256"] is None \
                and s["last_run_id"] and cfg_same and s["ck_sha"] is None:
            plan["skip"].append({**item, "reason": "failed previously (use --force to retry)"})
            continue
        if (not force and s["ck_sha"] == s["sha256"] and s["ck_cfg"] == cfg_hash and s["ck_status"] == "in_progress"
                and (s["ck_page"] or 0) > 0):
            plan["resume"].append({**item, "resume_after_page": s["ck_page"]})
        else:
            reason = "new" if s["extracted_sha256"] is None else (
                "content changed" if s["extracted_sha256"] != s["sha256"] else
                "forced" if force else "extraction settings changed")
            plan["extract"].append({**item, "reason": reason})
    if limit:
        todo = (plan["resume"] + plan["extract"])[:limit]
        keep = {t["source_id"] for t in todo}
        plan["resume"] = [t for t in plan["resume"] if t["source_id"] in keep]
        plan["extract"] = [t for t in plan["extract"] if t["source_id"] in keep]
    return plan


def _task(cfg: Config, source_id: str, run_id: str, force: bool) -> dict[str, Any]:
    return extract_source(cfg, source_id, run_id, force)


def run_extraction(conn: sqlite3.Connection, cfg: Config, run_id: str, *, force: bool = False,
                   workers: int | None = None, source_ids: list[str] | None = None, limit: int | None = None,
                   show_progress: bool = True) -> dict[str, Any]:
    plan = plan_extraction(conn, cfg, force, source_ids, limit)
    todo = plan["resume"] + plan["extract"]
    workers = max(1, int(workers or cfg.get("extraction.workers", 1)))
    summary: dict[str, Any] = {"planned": len(todo), "skipped": len(plan["skip"]), "results": [],
                               "skip_reasons": {}}
    for s in plan["skip"]:
        summary["skip_reasons"][s["reason"]] = summary["skip_reasons"].get(s["reason"], 0) + 1
    log.info("Extraction plan: %d to process (%d resume), %d skipped", len(todo), len(plan["resume"]),
             len(plan["skip"]))
    if not todo:
        return summary
    prog = Progress(len(todo), "extract", enabled=show_progress)
    total_pages = sum(t.get("pages") or 0 for t in todo)
    log.info("Pages to process: %d with %d worker(s)", total_pages, min(workers, len(todo)))

    def handle(sid: str, result: dict | None, exc: BaseException | None) -> None:
        if exc is not None:
            log.error("Extraction of %s crashed: %s", sid, exc)
            with transaction(conn):
                record_error(conn, "PAGE_EXTRACTION_FAILED", f"Worker crashed: {type(exc).__name__}: {exc}",
                             stage="text", source_id=sid, run_id=run_id, key="worker_crash")
                conn.execute("UPDATE sources SET extraction_status=? WHERE source_id=? AND extraction_status=?",
                             (ExtractionStatus.FAILED, sid, ExtractionStatus.IN_PROGRESS))
            result = {"source_id": sid, "status": ExtractionStatus.FAILED, "error": str(exc)}
        summary["results"].append(result)
        prog.update(note=f"{sid} {result.get('status')} {result.get('pages', '')}p")

    if workers == 1 or len(todo) == 1:
        for t in todo:
            try:
                handle(t["source_id"], extract_source(cfg, t["source_id"], run_id, force), None)
            except KeyboardInterrupt:
                log.warning("Interrupted - completed pages are checkpointed; re-run to resume")
                raise
            except Exception as exc:  # noqa: BLE001
                handle(t["source_id"], None, exc)
    else:
        ctx = mp.get_context("spawn")
        manager = ctx.Manager()
        queue = manager.Queue()
        listener = start_queue_listener(queue)
        try:
            with ProcessPoolExecutor(max_workers=min(workers, len(todo)), mp_context=ctx,
                                     initializer=worker_logging, initargs=(queue,)) as ex:
                futures = {ex.submit(_task, cfg, t["source_id"], run_id, force): t["source_id"] for t in todo}
                try:
                    for fut in as_completed(futures):
                        sid = futures[fut]
                        try:
                            handle(sid, fut.result(), None)
                        except Exception as exc:  # noqa: BLE001
                            handle(sid, None, exc)
                except KeyboardInterrupt:
                    log.warning("Interrupted - cancelling queued documents; completed pages are checkpointed")
                    for f in futures:
                        f.cancel()
                    raise
        finally:
            listener.stop()
            manager.shutdown()
    prog.close()
    by_status: dict[str, int] = {}
    for r in summary["results"]:
        by_status[r.get("status", "?")] = by_status.get(r.get("status", "?"), 0) + 1
    summary["by_status"] = by_status
    return summary
