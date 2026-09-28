"""Generation orchestration: build -> write DOCX/PDF -> manifest -> register outputs."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from maintdoc import audit
from maintdoc.config import Config
from maintdoc.constants import VisualCheckStatus
from maintdoc.db import transaction
from maintdoc.errors import record_error
from maintdoc.generate.docx_writer import write_docx
from maintdoc.generate.manual import GenerationBlocked, build_manual, manifest
from maintdoc.generate.pdf_writer import write_pdf
from maintdoc.utils import now_iso, sha256_file, sha256_text

log = logging.getLogger(__name__)


def output_paths(cfg: Config, mode: str) -> dict[str, Path]:
    stem = cfg.get("generation.file_stem", "Master_Manual")
    out = cfg.output_dir
    tag = mode.upper()
    return {"docx": out / f"{stem}_{tag}.docx", "pdf": out / f"{stem}_{tag}.pdf",
            "manifest": out / f"{stem}_{tag}_manifest.json"}


def _register(conn: sqlite3.Connection, kind: str, mode: str | None, path: Path | None, status: str,
              detail: str | None, run_id: str, items: int | None = None) -> None:
    conn.execute("INSERT INTO generated_outputs(kind, mode, path, sha256, item_count, status, detail, created_at, run_id) "
                 "VALUES(?,?,?,?,?,?,?,?,?)",
                 (kind, mode, str(path) if path else "", sha256_file(path) if path and path.exists() else None, items,
                  status, detail, now_iso(), run_id))


def generate_mode(conn: sqlite3.Connection, cfg: Config, mode: str, run_id: str, dry_run: bool = False) -> dict[str, Any]:
    paths = output_paths(cfg, mode)
    try:
        manual = build_manual(conn, cfg, mode, run_id)
    except GenerationBlocked as exc:
        log.warning("%s manual not generated: %s", mode, exc)
        if not dry_run:
            with transaction(conn):
                _register(conn, "manual", mode, None, "blocked", "; ".join(exc.reasons)[:2000], run_id)
                audit.append(conn, "system", "generation.blocked", "manual", mode, after={"reasons": exc.reasons},
                             run_id=run_id)
                # stale outputs of a blocked mode must not look current
                for p in paths.values():
                    if p.exists():
                        blocked = p.with_name(p.stem + "_OUTDATED" + p.suffix)
                        p.replace(blocked)
        return {"mode": mode, "status": "blocked", "reasons": exc.reasons}
    result: dict[str, Any] = {"mode": mode, "status": "generated", "stats": manual.stats,
                              "withheld": len(manual.withheld)}
    if dry_run:
        result["status"] = "dry_run"
        return result
    errors: list[str] = []
    written: dict[str, str] = {}
    for kind, writer in (("docx", write_docx), ("pdf", write_pdf)):
        try:
            writer(manual, paths[kind], cfg)
            written[kind] = str(paths[kind])
        except Exception as exc:  # noqa: BLE001 - recorded as generation error
            log.exception("%s %s generation failed", mode, kind)
            errors.append(f"{kind}: {exc}")
    man = manifest(manual, written)
    paths["manifest"].write_text(json.dumps(man, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    with transaction(conn):
        for kind in ("docx", "pdf"):
            status = "generated" if kind in written else "failed"
            _register(conn, f"manual_{kind}", mode, paths[kind] if kind in written else None, status,
                      None if kind in written else "; ".join(errors), run_id, manual.stats["items"])
        _register(conn, "manifest", mode, paths["manifest"], "generated", None, run_id, manual.stats["items"])
        for e in errors:
            record_error(conn, "GENERATION_ERROR", f"{mode} manual: {e}", stage="generation", run_id=run_id,
                         key=f"{mode}|{e[:80]}")
        for w in manual.withheld:
            if "no citation" in w["reason"]:
                record_error(conn, "GENERATION_UNCITED_ITEM", f"{mode}: {w['evidence_id']} withheld (no citation)",
                             stage="generation", evidence_id=w["evidence_id"], run_id=run_id)
        if "pdf" in written:
            sha = sha256_file(paths["pdf"])
            conn.execute(
                "INSERT OR IGNORE INTO visual_checks(check_id, target_type, output_path, output_sha256, kind, reason, "
                "status, created_at) VALUES(?,?,?,?,?,?,?,?)",
                ("VC-OUT-" + sha256_text(f"{paths['pdf']}|{sha}")[:14], "generated_output", str(paths["pdf"]), sha,
                 "generated_manual", f"visual check of generated {mode} manual layout, tables and figures",
                 VisualCheckStatus.PENDING, now_iso()))
            # checks for older versions of this output are superseded
            conn.execute("UPDATE visual_checks SET status=? WHERE target_type='generated_output' AND output_path=? "
                         "AND output_sha256 != ? AND status='pending'",
                         (VisualCheckStatus.INVALIDATED, str(paths["pdf"]), sha))
        audit.append(conn, "system", "generation.completed", "manual", mode,
                     after={"outputs": written, "stats": manual.stats, "errors": errors}, run_id=run_id)
    result.update({"outputs": written, "manifest": str(paths["manifest"]), "errors": errors})
    return result


def run_generate(conn: sqlite3.Connection, cfg: Config, run_id: str, modes: list[str] | None = None,
                 dry_run: bool = False) -> dict[str, Any]:
    modes = modes or list(cfg.get("generation.modes", ["draft", "approved"]))
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    return {m: generate_mode(conn, cfg, m, run_id, dry_run) for m in modes}
