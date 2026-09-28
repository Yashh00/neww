from __future__ import annotations

import os
import stat
from types import SimpleNamespace

import pytest

from maintdoc.config import DEFAULT_CONFIG, Config
from maintdoc.onedrive import (FILE_ATTRIBUTE_OFFLINE, FILE_ATTRIBUTE_PINNED, FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
                               FILE_ATTRIBUTE_RECALL_ON_OPEN, SF_DATALESS, detect_placeholder)

from conftest import REPO


def test_shipped_config_loads_and_has_all_chapters():
    cfg = Config.load(REPO / "config.yaml")
    ids = [c["id"] for c in cfg.chapters()]
    for required in ("scope", "safety", "specifications", "components", "preventive_maintenance", "inspections",
                     "procedures", "troubleshooting", "repairs", "schedules", "parts", "tools", "diagrams",
                     "revision_references"):
        assert required in ids
    assert cfg.get("ocr.mode") == "auto"
    assert cfg.db_path.name == "maintenance.db"


def test_defaults_merge_and_validation(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("extraction:\n  workers: 3\n", encoding="utf-8")
    cfg = Config.load(p)
    assert cfg.get("extraction.workers") == 3
    assert cfg.get("ocr.dpi") == DEFAULT_CONFIG["ocr"]["dpi"]  # default kept
    p.write_text("extraction:\n  workers: 0\n", encoding="utf-8")
    with pytest.raises(ValueError):
        Config.load(p)
    p.write_text("ocr:\n  mode: sometimes\n", encoding="utf-8")
    with pytest.raises(ValueError):
        Config.load(p)


def test_relative_paths_resolve_against_config_dir(tmp_path):
    p = tmp_path / "sub" / "c.yaml"
    p.parent.mkdir()
    p.write_text("paths:\n  source_root: ./docs\n", encoding="utf-8")
    cfg = Config.load(p)
    assert cfg.source_root == (tmp_path / "sub" / "docs").resolve()


def test_extraction_fingerprint_changes_with_ocr_settings():
    cfg = Config.load(REPO / "config.yaml")
    other = cfg.with_overrides({"ocr": {"dpi": 400}})
    assert cfg.extraction_fingerprint() != other.extraction_fingerprint()
    same = cfg.with_overrides({"extraction": {"workers": 8}})  # workers do not change extraction output
    assert cfg.extraction_fingerprint() == same.extraction_fingerprint()


def _stat(size=1000, blocks=8, attrs=0, flags=0):
    return SimpleNamespace(st_size=size, st_blocks=blocks, st_file_attributes=attrs, st_flags=flags,
                           st_mode=stat.S_IFREG | 0o644)


@pytest.mark.parametrize("attrs", [FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS, FILE_ATTRIBUTE_RECALL_ON_OPEN,
                                   FILE_ATTRIBUTE_OFFLINE])
def test_windows_cloud_attributes_are_placeholders(attrs):
    info = detect_placeholder(_stat(attrs=attrs))
    assert info.is_placeholder and "Windows" in info.reason


def test_pinned_local_file_is_not_placeholder():
    info = detect_placeholder(_stat(attrs=FILE_ATTRIBUTE_PINNED))
    assert not info.is_placeholder and info.pinned


def test_macos_dataless_and_zero_blocks():
    assert detect_placeholder(_stat(flags=SF_DATALESS)).is_placeholder
    assert detect_placeholder(_stat(blocks=0)).is_placeholder
    assert not detect_placeholder(_stat(blocks=0), zero_blocks_rule=False).is_placeholder
    assert not detect_placeholder(_stat(size=0, blocks=0)).is_placeholder


def test_real_local_file_is_not_placeholder(tmp_path):
    f = tmp_path / "x.pdf"
    f.write_bytes(os.urandom(5000))
    assert not detect_placeholder(f.stat()).is_placeholder
