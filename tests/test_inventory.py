from __future__ import annotations

from pathlib import Path

from maintdoc import inventory as inv
from maintdoc.constants import ExtractionStatus, SourceStatus
from maintdoc.db import open_db, rows
from maintdoc.onedrive import PlaceholderInfo
from maintdoc.testing.corpus import make_corpus

from conftest import make_config, source_id


def test_every_discovered_pdf_has_a_status(processed):
    srcs = rows(processed.conn, "SELECT * FROM sources")
    assert len(srcs) == len(inv.discover(processed.cfg))
    for s in srcs:
        assert s["status"] in SourceStatus.ALL
        assert s["extraction_status"] in ExtractionStatus.FINAL, s


def test_integrity_statuses(processed):
    w = processed
    st = {r["rel_path"]: r for r in rows(w.conn, "SELECT * FROM sources")}
    assert st["Corrupt/damaged_manual.pdf"]["status"] == SourceStatus.CORRUPT
    assert st["Restricted/secured_manual.pdf"]["status"] == SourceStatus.ENCRYPTED
    assert st["Corrupt/truncated_manual.pdf"]["is_repaired"] == 1
    codes = {r[0] for r in w.conn.execute("SELECT error_code FROM extraction_errors")}
    assert {"CORRUPT_PDF", "ENCRYPTED_PDF", "PDF_REPAIRED", "PDF_NO_EOF"} <= codes


def test_exact_duplicate_file_detection(processed):
    w = processed
    dup = rows(w.conn, "SELECT * FROM sources WHERE status='duplicate'")
    assert len(dup) == 1
    d = dup[0]
    assert d["rel_path"].endswith("RevB_copy.pdf")
    primary = rows(w.conn, "SELECT * FROM sources WHERE source_id=?", (d["duplicate_of"],))[0]
    assert primary["sha256"] == d["sha256"]
    assert primary["rel_path"] == "HP-200/HP-200_Maintenance_Manual_RevB.pdf"  # shallower path is primary
    assert d["extraction_status"] == ExtractionStatus.SKIPPED_DUPLICATE


def test_metadata_extraction(processed):
    w = processed
    rev_c = rows(w.conn, "SELECT * FROM sources WHERE rel_path LIKE '%RevC.pdf'")[0]
    assert (rev_c["model"], rev_c["revision"], rev_c["doc_date"], rev_c["doc_number"]) == \
        ("HP-200", "C", "2023-06-01", "MM-HP200")
    hp300 = rows(w.conn, "SELECT * FROM sources WHERE rel_path LIKE 'HP-300%'")[0]
    assert hp300["model"] == "HP-300" and hp300["doc_number"] == "SB-017"
    pump = source_id(w, "Pump_Service_Sheet")
    missing = {r[0] for r in w.conn.execute("SELECT details_json FROM extraction_errors WHERE source_id=? AND "
                                            "error_code='MISSING_METADATA'", (pump,))}
    assert any("revision" in m for m in missing) and any("doc_date" in m for m in missing)


def test_incremental_skip_and_change_detection(tmp_path):
    make_corpus(tmp_path / "src", include_scanned=False)
    cfg = make_config(tmp_path, tmp_path / "src")
    conn = open_db(cfg.db_path)
    s1 = inv.run_inventory(conn, cfg, "RUN-1", show_progress=False)
    assert s1.new == s1.discovered
    s2 = inv.run_inventory(conn, cfg, "RUN-2", show_progress=False)
    assert s2.new == 0 and s2.unchanged == s2.discovered
    # modify one file
    target = tmp_path / "src" / "HP-300" / "HP-300_Service_Bulletin_SB-017.pdf"
    target.write_bytes(target.read_bytes() + b"\n% appended\n")
    s3 = inv.run_inventory(conn, cfg, "RUN-3", show_progress=False)
    assert s3.modified == 1
    sid = conn.execute("SELECT source_id FROM sources WHERE rel_path LIKE 'HP-300%'").fetchone()[0]
    versions = rows(conn, "SELECT change_type FROM source_versions WHERE source_id=? ORDER BY version_id", (sid,))
    assert [v["change_type"] for v in versions] == ["new", "modified"]
    assert conn.execute("SELECT COUNT(*) FROM extraction_errors WHERE source_id=? AND error_code='SOURCE_CHANGED'",
                        (sid,)).fetchone()[0] == 1
    # delete a file -> missing, never removed from the register
    target.unlink()
    s4 = inv.run_inventory(conn, cfg, "RUN-4", show_progress=False)
    assert s4.missing == 1
    r = rows(conn, "SELECT status, present FROM sources WHERE source_id=?", (sid,))[0]
    assert r == {"status": SourceStatus.MISSING, "present": 0}


def test_offline_placeholder_is_never_opened(tmp_path, monkeypatch):
    """OneDrive cloud-only files are reported but never hashed or opened (that would need network access)."""
    make_corpus(tmp_path / "src", include_scanned=False)
    cfg = make_config(tmp_path, tmp_path / "src")
    conn = open_db(cfg.db_path)
    cloud = tmp_path / "src" / "HP-300" / "HP-300_Service_Bulletin_SB-017.pdf"
    cloud_ino = cloud.stat().st_ino
    real_detect, real_hash, real_integrity = inv.detect_placeholder, inv.sha256_file, inv.check_integrity

    def fake_detect(st, rule=True):
        if st.st_ino == cloud_ino:
            return PlaceholderInfo(True, "simulated RECALL_ON_DATA_ACCESS")
        return real_detect(st, rule)

    def guarded_hash(path, *a, **k):
        assert Path(path) != cloud, "placeholder must not be read"
        return real_hash(path, *a, **k)

    def guarded_integrity(path, c):
        assert Path(path) != cloud, "placeholder must not be opened"
        return real_integrity(path, c)

    monkeypatch.setattr(inv, "detect_placeholder", fake_detect)
    monkeypatch.setattr(inv, "sha256_file", guarded_hash)
    monkeypatch.setattr(inv, "check_integrity", guarded_integrity)
    stats = inv.run_inventory(conn, cfg, "RUN-p", show_progress=False)
    assert stats.placeholders == 1
    r = rows(conn, "SELECT status, extraction_status FROM sources WHERE filename=?", (cloud.name,))[0]
    assert r["status"] == SourceStatus.OFFLINE_PLACEHOLDER
    assert r["extraction_status"] == ExtractionStatus.NOT_APPLICABLE
    assert conn.execute("SELECT COUNT(*) FROM extraction_errors WHERE error_code='OFFLINE_PLACEHOLDER'").fetchone()[0] == 1


def test_dry_run_inventory_changes_nothing(processed, tmp_path):
    from maintdoc.cli import main
    import yaml
    cfg_file = tmp_path / "cfg.yaml"
    data = processed.cfg.to_dict()
    cfg_file.write_text(yaml.safe_dump(data), encoding="utf-8")
    before = processed.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    assert main(["-c", str(cfg_file), "--log-level", "ERROR", "inventory", "--dry-run"]) == 0
    after = processed.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    assert before == after
