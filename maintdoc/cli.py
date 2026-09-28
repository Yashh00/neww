"""Command line interface.

    python -m maintdoc [-c config.yaml] <command> [options]

Commands: inventory, extract, validate, review, generate, verify, export,
run-all, status, make-test-corpus. Every pipeline command records a run,
logs to logs/maintdoc.log and holds a workspace lock.

Exit codes: 0 ok; 1 error; 2 verification failed; 3 verification incomplete
(human review / visual checks pending); 4 workspace locked.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from maintdoc import __version__
from maintdoc.config import Config
from maintdoc.constants import DRAFT_DISCLAIMER
from maintdoc.db import finish_run, open_db, start_run
from maintdoc.locking import LockError, RunLock
from maintdoc.logging_setup import setup_logging
from maintdoc.utils import new_run_id

log = logging.getLogger("maintdoc")


def _print(title: str, data: Any) -> None:
    print(f"\n== {title} ==")
    if isinstance(data, (dict, list)):
        print(json.dumps(data, indent=2, default=str, ensure_ascii=False))
    else:
        print(data)


def _load(args) -> Config:
    cfg = Config.load(args.config)
    if getattr(args, "source_root", None):
        cfg = cfg.with_overrides({"paths": {"source_root": args.source_root}})
    if getattr(args, "workers", None):
        cfg = cfg.with_overrides({"extraction": {"workers": int(args.workers)}})
    cfg.ensure_dirs()
    setup_logging(cfg.log_dir, args.log_level or cfg.get("logging.level", "INFO"),
                  cfg.get("logging.file_name", "maintdoc.log"), int(cfg.get("logging.max_bytes", 10_000_000)),
                  int(cfg.get("logging.backup_count", 5)))
    return cfg


def _run(cfg: Config, command: str, dry_run: bool, fn: Callable[[Any, str], Any], lock: bool = True) -> Any:
    """Open DB, record the run, hold the lock, execute ``fn(conn, run_id)``."""
    run_id = new_run_id(command)
    ctx = RunLock(cfg.work_dir / ".maintdoc.lock", command) if lock else _NullCtx()
    with ctx:
        conn = open_db(cfg.db_path)
        try:
            start_run(conn, run_id, command, dry_run, cfg.fingerprint()[:16])
            log.info("%s started (run %s%s)", command, run_id, ", DRY RUN" if dry_run else "")
            try:
                result = fn(conn, run_id)
            except KeyboardInterrupt:
                finish_run(conn, run_id, "interrupted")
                log.warning("%s interrupted - progress up to the last completed page is saved", command)
                raise
            except Exception:
                finish_run(conn, run_id, "failed")
                log.exception("%s failed", command)
                raise
            stats = ({k: v for k, v in result.items() if not str(k).startswith("_")} if isinstance(result, dict)
                     else {"result": str(result)[:500]})
            finish_run(conn, run_id, "completed", stats)
            log.info("%s completed (run %s)", command, run_id)
            return result
        finally:
            conn.close()


class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# --------------------------------------------------------------------------- commands
def cmd_inventory(args) -> int:
    from maintdoc.dryrun import scratch_workspace
    from maintdoc.inventory import run_inventory
    cfg = _load(args)
    if args.dry_run:
        with scratch_workspace(cfg) as scratch:
            stats = _run(scratch, "inventory", True, lambda c, r: run_inventory(
                c, scratch, r, rehash=args.rehash, limit=args.limit).as_dict(), lock=False)
        _print("Inventory preview (dry run - nothing was changed)", stats)
        return 0
    stats = _run(cfg, "inventory", False, lambda c, r: run_inventory(c, cfg, r, rehash=args.rehash,
                                                                      limit=args.limit).as_dict())
    _print("Inventory", stats)
    return 0


def cmd_extract(args) -> int:
    from maintdoc.extraction.runner import plan_extraction, run_extraction
    cfg = _load(args)
    if args.dry_run:
        conn = open_db(cfg.db_path)
        plan = plan_extraction(conn, cfg, args.force, args.source or None, args.limit)
        pages = sum((p.get("pages") or 0) for p in plan["extract"] + plan["resume"])
        _print("Extraction plan (dry run)", {"extract": plan["extract"], "resume": plan["resume"],
                                             "skip_count": len(plan["skip"]), "pages_to_process": pages})
        return 0
    res = _run(cfg, "extract", False, lambda c, r: run_extraction(
        c, cfg, r, force=args.force, workers=args.workers, source_ids=args.source or None, limit=args.limit))
    _print("Extraction", {k: v for k, v in res.items() if k != "results"})
    return 0


def _validate(cfg: Config, conn, run_id: str, export: bool) -> dict:
    from maintdoc.analysis.validate import run_validate
    from maintdoc.reporting.export import run_export
    stats = run_validate(conn, cfg, run_id)
    if export:
        stats["exports"] = run_export(conn, cfg, run_id, include=("source_register", "evidence_register",
                                                                  "conflict_register", "error_register"))
    return stats


def cmd_validate(args) -> int:
    from maintdoc.dryrun import scratch_workspace
    cfg = _load(args)
    if args.dry_run:
        with scratch_workspace(cfg) as scratch:
            stats = _run(scratch, "validate", True, lambda c, r: _validate(scratch, c, r, False), lock=False)
        _print("Validation preview (dry run - nothing was changed)", stats)
        return 0
    stats = _run(cfg, "validate", False, lambda c, r: _validate(cfg, c, r, not args.no_export))
    _print("Validation", stats)
    return 0


def cmd_generate(args) -> int:
    from maintdoc.generate.run import run_generate
    cfg = _load(args)
    modes = None if args.mode == "both" else [args.mode]
    res = _run(cfg, "generate", args.dry_run, lambda c, r: run_generate(c, cfg, r, modes, dry_run=args.dry_run),
               lock=not args.dry_run)
    _print("Generation" + (" (dry run)" if args.dry_run else ""), res)
    return 0


def _verify(cfg: Config, conn, run_id: str, rehash: bool | None, export: bool, persist: bool = True):
    from maintdoc.reporting.export import run_export
    from maintdoc.verify import run_verify
    rep = run_verify(conn, cfg, run_id, rehash=rehash, persist=persist)
    exports = run_export(conn, cfg, run_id, report=rep) if export else {}
    return rep, exports


def _verify_exit(rep) -> int:
    if rep.overall.startswith("FAIL"):
        return 2
    if rep.overall.startswith("INCOMPLETE"):
        return 3
    return 0


def _print_verify(rep, exports) -> None:
    _print("Acceptance criteria", [f"{a['no']:>2}. [{a['status'].upper():13s}] {a['criterion']} - {a['detail']}"
                                   for a in rep.acceptance])
    _print("Verification checks", rep.counts())
    fails = [f"{c.group}/{c.name} {c.target}: {c.detail}" for c in rep.checks if c.status == "fail"][:25]
    if fails:
        _print("First failing checks", fails)
    if exports:
        _print("Outputs", exports)
    print(f"\nOVERALL: {rep.overall}\n{DRAFT_DISCLAIMER}")


def cmd_verify(args) -> int:
    cfg = _load(args)
    rehash = False if args.no_rehash else None
    if args.dry_run:
        conn = open_db(cfg.db_path)
        rep, exports = _verify(cfg, conn, new_run_id("verify"), rehash, export=False, persist=False)
    else:
        rep, exports = _run(cfg, "verify", False, lambda c, r: _verify(cfg, c, r, rehash, not args.no_export))
    _print_verify(rep, exports)
    return _verify_exit(rep)


def cmd_export(args) -> int:
    from maintdoc.reporting.export import run_export
    cfg = _load(args)
    res = _run(cfg, "export", False, lambda c, r: run_export(c, cfg, r))
    _print("Exported (run 'verify' to refresh Validation_Report.xlsx)", res)
    return 0


def cmd_run_all(args) -> int:
    from maintdoc.dryrun import scratch_workspace
    from maintdoc.extraction.runner import plan_extraction, run_extraction
    from maintdoc.generate.run import run_generate
    from maintdoc.inventory import run_inventory
    cfg = _load(args)
    if args.dry_run:
        with scratch_workspace(cfg) as scratch:
            def preview(c, r):
                inv = run_inventory(c, scratch, r, limit=args.limit).as_dict()
                plan = plan_extraction(c, scratch, args.force, None, args.limit)
                return {"inventory": inv, "extract": [(p["source_id"], p["rel_path"], p.get("reason"))
                                                      for p in plan["extract"] + plan["resume"]],
                        "skip": len(plan["skip"]),
                        "pages_to_process": sum((p.get("pages") or 0) for p in plan["extract"] + plan["resume"])}
            res = _run(scratch, "run-all", True, preview, lock=False)
        _print("run-all preview (dry run - nothing was changed)", res)
        return 0

    def pipeline(conn, run_id):
        out: dict[str, Any] = {}
        print("[1/6] inventory", flush=True)
        out["inventory"] = run_inventory(conn, cfg, run_id, limit=args.limit).as_dict()
        print("[2/6] extract", flush=True)
        ext = run_extraction(conn, cfg, run_id, force=args.force, workers=args.workers, limit=args.limit)
        out["extract"] = {k: v for k, v in ext.items() if k != "results"}
        print("[3/6] validate", flush=True)
        out["validate"] = _validate(cfg, conn, run_id, export=False)
        print("[4/6] generate", flush=True)
        out["generate"] = run_generate(conn, cfg, run_id)
        print("[5/6] verify", flush=True)
        from maintdoc.verify import run_verify
        rep = run_verify(conn, cfg, run_id)
        print("[6/6] export", flush=True)
        from maintdoc.reporting.export import run_export
        exports = run_export(conn, cfg, run_id, report=rep)
        out["verify"] = {"overall": rep.overall, "counts": rep.counts()}
        out["_rep"], out["_exports"] = rep, exports
        return out
    res = _run(cfg, "run-all", False, pipeline)
    rep, exports = res.pop("_rep"), res.pop("_exports")
    _print("run-all", {k: v for k, v in res.items()})
    _print_verify(rep, exports)
    return _verify_exit(rep)


def cmd_review(args) -> int:
    cfg = _load(args)
    app = Path(__file__).with_name("review") / "app.py"
    cfg_path = str(cfg.path) if cfg.path else "config.yaml"
    address = args.address or cfg.get("review.streamlit_address", "127.0.0.1")
    port = str(args.port or cfg.get("review.streamlit_port", 8501))
    cmd = [sys.executable, "-m", "streamlit", "run", str(app), "--server.address", address, "--server.port", port,
           "--server.headless", "true" if args.no_browser else "false", "--browser.gatherUsageStats", "false",
           "--client.toolbarMode", "minimal",  # no "Deploy" (cloud publishing) button in an offline tool
           "--", "--config", cfg_path]
    env = dict(os.environ, MAINTDOC_CONFIG=cfg_path)
    print(f"Starting local review server on http://{address}:{port}  (Ctrl+C to stop)")
    try:
        return subprocess.call(cmd, env=env)
    except KeyboardInterrupt:
        return 0


def cmd_status(args) -> int:
    from maintdoc.review.service import dashboard
    cfg = _load(args)
    conn = open_db(cfg.db_path)
    _print("Status", dashboard(conn))
    last = conn.execute("SELECT run_id, created_at FROM verification_results ORDER BY result_id DESC LIMIT 1").fetchone()
    if last:
        acc = conn.execute("SELECT check_name, target, status, detail FROM verification_results WHERE run_id=? AND "
                           "check_group='acceptance' ORDER BY result_id", (last[0],)).fetchall()
        _print(f"Last verification ({last[1]})", [f"[{a[2].upper()}] {a[1]} - {a[3]}" for a in acc])
    runs = conn.execute("SELECT run_id, command, status, started_at FROM runs ORDER BY started_at DESC LIMIT 8").fetchall()
    _print("Recent runs", [dict(r) for r in runs])
    return 0


def cmd_make_test_corpus(args) -> int:
    from maintdoc.testing.corpus import make_corpus
    paths = make_corpus(Path(args.directory), include_scanned=not args.no_scanned)
    _print("Synthetic test corpus created", {k: str(v) for k, v in paths.items()})
    return 0


# --------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="maintdoc", description="Local rule-based maintenance PDF processor "
                                "(no AI, no cloud, no network). All outputs are drafts until engineering release.")
    p.add_argument("-c", "--config", default=os.environ.get("MAINTDOC_CONFIG", "config.yaml"),
                   help="configuration file (default: config.yaml)")
    p.add_argument("--log-level", default=None, help="console log level (DEBUG, INFO, WARNING)")
    p.add_argument("--version", action="version", version=f"maintdoc {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("inventory", help="discover, hash, verify and register source PDFs")
    s.add_argument("--dry-run", action="store_true", help="preview on a scratch copy; change nothing")
    s.add_argument("--rehash", action="store_true", help="hash every file even if size/mtime are unchanged")
    s.add_argument("--limit", type=int, help="process only the first N files (testing)")
    s.add_argument("--source-root", help="override paths.source_root")
    s.set_defaults(func=cmd_inventory)

    s = sub.add_parser("extract", help="page-level extraction with OCR fallback (resumable)")
    s.add_argument("--dry-run", action="store_true", help="show what would be processed")
    s.add_argument("--workers", type=int, help="parallel worker processes")
    s.add_argument("--force", action="store_true", help="re-extract even unchanged sources")
    s.add_argument("--source", action="append", help="only this source ID (repeatable)")
    s.add_argument("--limit", type=int)
    s.set_defaults(func=cmd_extract)

    s = sub.add_parser("validate", help="classify, extract values, de-duplicate, detect conflicts")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--no-export", action="store_true", help="do not write the Excel registers")
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("review", help="start the local Streamlit review app")
    s.add_argument("--port", type=int)
    s.add_argument("--address")
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(func=cmd_review)

    s = sub.add_parser("generate", help="generate draft/approved master manuals (DOCX + PDF)")
    s.add_argument("--mode", choices=["draft", "approved", "both"], default="both")
    s.add_argument("--dry-run", action="store_true", help="build the manual model only; write nothing")
    s.set_defaults(func=cmd_generate)

    s = sub.add_parser("verify", help="verify sources, evidence and generated outputs; acceptance criteria")
    s.add_argument("--no-rehash", action="store_true", help="skip SHA-256 re-hashing of sources")
    s.add_argument("--no-export", action="store_true")
    s.add_argument("--dry-run", action="store_true", help="report only; do not store results or write files")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("export", help="write registers, Processing_Summary.pdf and maintenance.db snapshot")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("run-all", help="inventory -> extract -> validate -> generate -> verify -> export")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--workers", type=int)
    s.add_argument("--force", action="store_true")
    s.add_argument("--limit", type=int)
    s.add_argument("--source-root", help="override paths.source_root")
    s.set_defaults(func=cmd_run_all)

    s = sub.add_parser("status", help="show register counts and the last verification result")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("make-test-corpus", help="create synthetic test PDFs (digital, scanned, duplicate, "
                                                "conflicting, corrupt, multi-revision)")
    s.add_argument("directory")
    s.add_argument("--no-scanned", action="store_true")
    s.set_defaults(func=cmd_make_test_corpus)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except LockError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 4
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Completed work is saved; re-run the command to resume.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - top-level guard; details are in the log file
        log.exception("Unhandled error")
        print(f"ERROR: {type(exc).__name__}: {exc} (see log file for details)", file=sys.stderr)
        return 1
