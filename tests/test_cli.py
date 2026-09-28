from __future__ import annotations

import sqlite3

import yaml

from maintdoc.cli import main
from maintdoc.reporting.export import FILES
from maintdoc.testing.corpus import make_corpus

from conftest import HAS_TESSERACT, REPO


def _config(tmp_path):
    data = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    data["paths"] = {"source_root": str(tmp_path / "sources"), "work_dir": str(tmp_path / "work"),
                     "output_dir": str(tmp_path / "output"), "log_dir": str(tmp_path / "logs"),
                     "database": str(tmp_path / "work" / "maintenance.db")}
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return str(p)


def test_cli_run_all_and_commands(tmp_path, capsys):
    make_corpus(tmp_path / "sources", include_scanned=HAS_TESSERACT)
    cfg = _config(tmp_path)
    base = ["-c", cfg, "--log-level", "ERROR"]
    # dry-run changes nothing
    assert main(base + ["run-all", "--dry-run"]) == 0
    assert not (tmp_path / "output" / "Source_Register.xlsx").exists()
    # full pipeline with two worker processes; review pending -> exit code 3 (incomplete, not failed)
    assert main(base + ["run-all", "--workers", "2"]) == 3
    for name in FILES.values():
        assert (tmp_path / "output" / name).exists(), name
    assert (tmp_path / "output" / "Master_Manual_DRAFT.pdf").exists()
    assert (tmp_path / "output" / "Master_Manual_DRAFT.docx").exists()
    assert (tmp_path / "logs" / "maintdoc.log").exists()
    # second run: nothing re-extracted (unchanged-file skip)
    capsys.readouterr()
    assert main(base + ["extract", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert '"extract": []' in out
    # individual commands
    for cmd in (["inventory"], ["extract"], ["validate"], ["generate", "--mode", "draft"], ["verify", "--no-rehash"],
                ["export"], ["status"]):
        code = main(base + cmd)
        assert code in (0, 3), (cmd, code)
    conn = sqlite3.connect(tmp_path / "work" / "maintenance.db")
    commands = {r[0] for r in conn.execute("SELECT command FROM runs WHERE status='completed'")}
    assert {"run-all", "inventory", "extract", "validate", "generate", "verify", "export"} <= commands


def test_lock_prevents_concurrent_runs(tmp_path):
    make_corpus(tmp_path / "sources", include_scanned=False)
    cfg = _config(tmp_path)
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    (tmp_path / "work" / ".maintdoc.lock").write_text('{"pid": %d, "command": "extract"}' % __import__("os").getpid())
    assert main(["-c", cfg, "--log-level", "ERROR", "inventory"]) == 4
