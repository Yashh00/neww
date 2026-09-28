"""YAML configuration loading with defaults, path resolution and fingerprints."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

# Defaults for every setting. The shipped config.yaml documents and overrides
# these. Lists replace defaults; dictionaries are merged recursively.
DEFAULT_CONFIG: dict[str, Any] = {
    "project": {
        "name": "Maintenance Master Manual",
        "organisation": "",
        "manual_title": "Master Maintenance Manual",
    },
    "paths": {
        "source_root": "./sources",
        "work_dir": "./work",
        "output_dir": "./output",
        "log_dir": "./logs",
        "database": "./work/maintenance.db",
    },
    "inventory": {
        "include_extensions": [".pdf"],
        "exclude_dir_patterns": [".*", "~$*", "$RECYCLE.BIN", "__pycache__"],
        "exclude_file_patterns": ["~$*", ".~lock*", "*.tmp"],
        "follow_symlinks": False,
        "trust_size_and_mtime": True,
        "min_file_size_bytes": 64,
        "page_load_check": True,
    },
    "onedrive": {
        "detect_placeholders": True,
        "treat_zero_allocated_blocks_as_placeholder": True,
        "never_hydrate": True,
    },
    "metadata": {
        "scan_pages": 2,
        "revision_patterns": [r"(?i:\brev(?:ision)?)[\s._:\-]*([A-Z]{1,2}[0-9]{0,2}|[0-9]{1,3}(?:\.[0-9]{1,2})?)\b"],
        "date_patterns": [r"\b(\d{4}-\d{2}-\d{2})\b"],
        "doc_number_patterns": [],
        "title_max_chars": 160,
        "required_fields": ["equipment", "model", "revision", "doc_date"],
    },
    "extraction": {
        "workers": 2,
        "reextract_on_config_change": True,
        "header_footer_margin_ratio": 0.07,
        "header_footer_min_repeat_ratio": 0.3,
        "header_footer_min_pages": 3,
        "heading_size_ratio": 1.15,
        "heading_max_chars": 120,
        "multi_column_detection": True,
        "signal_words": {
            "danger": ["DANGER", "GEFAHR"],
            "warning": ["WARNING", "WARNUNG"],
            "caution": ["CAUTION", "VORSICHT"],
            "notice": ["NOTICE", "IMPORTANT", "ACHTUNG"],
            "note": ["NOTE", "HINWEIS"],
        },
        "caption_pattern": r"^(?:fig(?:ure)?\.?|abb(?:ildung)?\.?|diagram|illustration|drawing|table|tab\.)\s*[A-Z]?\d+[\w.\-]*",
        "blank_page_pattern": r"(?i)this\s+page\s+(?:is\s+)?intentionally\s+(?:left\s+)?blank",
        "gibberish_threshold": 0.45,
        "gibberish_min_chars": 20,
        "store_page_text": True,
    },
    "tables": {
        "mode": "auto",                 # auto | always | never
        "auto_min_line_segments": 4,
        "max_drawings_for_detection": 4000,
        "min_rows": 2,
        "min_cols": 2,
        "min_filled_ratio": 0.3,
        "fallback_pymupdf": True,
        "complex_table_min_cols": 7,
    },
    "figures": {
        "enabled": True,
        "render": True,
        "render_dpi": 150,
        "min_area_ratio": 0.02,
        "min_drawing_paths": 12,
        "caption_max_distance_pt": 60,
    },
    "ocr": {
        "enabled": True,
        "mode": "auto",                 # auto | always | never
        "tesseract_cmd": "",
        "tessdata_dir": "",
        "languages": "eng",
        "dpi": 300,
        "psm": 3,
        "oem": 1,
        "min_native_chars": 25,
        "min_image_coverage": 0.25,
        "min_vector_paths_for_ocr": 400,
        "mixed_page_ocr": True,
        "mixed_min_image_coverage": 0.35,
        "native_quality_threshold": 0.45,
        "page_low_confidence": 70.0,
        "block_low_confidence": 60.0,
        "retry_below_confidence": 75.0,
        "detect_orientation": True,
        "timeout_seconds": 120,
        "preprocessing": {
            "grayscale": True,
            "denoise": "median",        # none | median | nlmeans
            "deskew": True,
            "max_deskew_degrees": 10.0,
            "binarize": "otsu",         # none | otsu | adaptive
            "clahe": False,
        },
    },
    "classification": {
        "min_score": 2.0,
        "ambiguity_margin": 0.5,
        "weights": {
            "section_heading": 4.0,
            "ancestor_heading": 2.0,
            "own_heading": 5.0,
            "keyword": 1.0,
            "keyword_cap": 4.0,
            "pattern": 2.0,
            "block_type": 3.0,
        },
        "equipment": [],
        "components": [],
        "chapters": [
            {"id": "scope", "title": "Scope and Applicability"},
            {"id": "safety", "title": "Safety Instructions"},
            {"id": "specifications", "title": "Technical Specifications"},
            {"id": "components", "title": "Components and System Description"},
            {"id": "preventive_maintenance", "title": "Preventive Maintenance"},
            {"id": "inspections", "title": "Inspections and Checks"},
            {"id": "procedures", "title": "Maintenance Procedures"},
            {"id": "troubleshooting", "title": "Troubleshooting"},
            {"id": "repairs", "title": "Repairs and Overhaul"},
            {"id": "schedules", "title": "Maintenance Schedules and Intervals"},
            {"id": "parts", "title": "Spare Parts"},
            {"id": "tools", "title": "Tools and Consumables"},
            {"id": "diagrams", "title": "Diagrams and Illustrations"},
            {"id": "revision_references", "title": "Revision History and References"},
        ],
    },
    "dedupe": {
        "exact_context": "section",     # section | chapter | global
        "merge_across_models": False,
        "never_merge_safety_within_source": True,
        "near_threshold": 90.0,
        "near_min_chars": 25,
        "near_max_candidates": 50000,
        "near_chunk_size": 512,
    },
    "units": {
        "decimal_separator": "auto",    # auto | point | comma
        "context_window_chars": 80,
        "dual_unit_tolerance": 0.03,
        "canonical_units": {
            "torque": "N*m",
            "pressure": "bar",
            "temperature": "degC",
            "interval": "hour",
            "length": "mm",
            "volume": "L",
            "mass": "kg",
            "force": "N",
            "speed": "rpm",
            "flow": "L/min",
            "voltage": "V",
            "current": "A",
            "power": "kW",
            "frequency": "Hz",
            "viscosity": "mm**2/s",
        },
        "parameter_keywords": {},
    },
    "conflicts": {
        "subject_similarity": 85.0,
        "relative_tolerance": 0.005,
        "absolute_tolerance": 1e-9,
        "procedure_heading_similarity": 90.0,
        "procedure_step_similarity": 70.0,
        "part_description_similarity": 88.0,
        "severity": {
            "torque": "critical",
            "pressure": "critical",
            "temperature": "critical",
            "safety": "critical",
            "interval": "major",
            "part_number": "major",
            "procedure": "major",
            "unit_inconsistency": "major",
            "applicability": "major",
        },
        "default_severity": "minor",
        "blocking_severities": ["critical"],
        "stopwords": [],
        "synonyms": {},
    },
    "review": {
        "require_reviewer_name": True,
        "require_comment_for_rejection": True,
        "require_ocr_confirmation": True,
        "streamlit_port": 8501,
        "streamlit_address": "127.0.0.1",
    },
    "generation": {
        "modes": ["draft", "approved"],
        "block_approved_on_open_critical_conflicts": True,
        "include_unclassified_in_draft": True,
        "include_figures": True,
        "figure_max_width_cm": 15.0,
        "pdf_font_paths": [],
        "full_sha_in_index": True,
        "file_stem": "Master_Manual",
    },
    "verify": {
        "rehash_sources": True,
        "recheck_evidence_text": "approved",   # approved | all | none
        "expected_chapters": ["scope", "safety", "specifications", "preventive_maintenance",
                              "procedures", "troubleshooting", "parts"],
        "pdfplumber_page_count": True,
    },
    "export": {
        "max_cell_chars": 32000,
        "copy_database_to_output": True,
    },
    "logging": {
        "level": "INFO",
        "file_name": "maintdoc.log",
        "max_bytes": 10_000_000,
        "backup_count": 5,
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` into a copy of ``base``."""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _unknown_keys(defaults: dict, data: dict, prefix: str = "") -> list[str]:
    unknown = []
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if key not in defaults:
            unknown.append(dotted)
        elif isinstance(value, dict) and isinstance(defaults[key], dict) and defaults[key]:
            unknown.extend(_unknown_keys(defaults[key], value, dotted + "."))
    return unknown


# Sections whose values may contain arbitrary user keys
_FREEFORM_SECTIONS = {"units.canonical_units", "units.parameter_keywords", "conflicts.severity",
                      "conflicts.synonyms", "extraction.signal_words"}


class Config:
    """Configuration wrapper with dotted access and path resolution."""

    def __init__(self, data: dict, path: Path | None = None):
        self.data = data
        self.path = Path(path).resolve() if path else None
        self.base_dir = self.path.parent if self.path else Path.cwd()

    @classmethod
    def load(cls, path: str | os.PathLike | None = None, overrides: dict | None = None) -> "Config":
        user: dict = {}
        cfg_path = Path(path) if path else None
        if cfg_path is not None:
            if not cfg_path.exists():
                raise FileNotFoundError(f"Configuration file not found: {cfg_path}")
            with open(cfg_path, "r", encoding="utf-8") as fh:
                user = yaml.safe_load(fh) or {}
            if not isinstance(user, dict):
                raise ValueError(f"Configuration file {cfg_path} must contain a YAML mapping")
            for key in _unknown_keys(DEFAULT_CONFIG, user):
                if not any(key.startswith(s + ".") for s in _FREEFORM_SECTIONS):
                    log.warning("Unknown configuration key ignored or unused: %s", key)
        data = deep_merge(DEFAULT_CONFIG, user)
        if overrides:
            data = deep_merge(data, overrides)
        cfg = cls(data, cfg_path)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        workers = self.get("extraction.workers")
        if not isinstance(workers, int) or workers < 1:
            raise ValueError("extraction.workers must be an integer >= 1")
        for key in ("ocr.mode", "tables.mode"):
            if self.get(key) not in ("auto", "always", "never"):
                raise ValueError(f"{key} must be one of auto|always|never")
        if self.get("dedupe.exact_context") not in ("section", "chapter", "global"):
            raise ValueError("dedupe.exact_context must be section|chapter|global")
        ids = [c.get("id") for c in self.get("classification.chapters", [])]
        if len(ids) != len(set(ids)) or any(not i for i in ids):
            raise ValueError("classification.chapters must have unique, non-empty ids")
        thr = float(self.get("dedupe.near_threshold"))
        if not 50 <= thr <= 100:
            raise ValueError("dedupe.near_threshold must be between 50 and 100")

    # ------------------------------------------------------------------ access
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def resolve_path(self, dotted: str) -> Path:
        raw = self.get(dotted)
        if raw is None:
            raise KeyError(dotted)
        p = Path(os.path.expandvars(os.path.expanduser(str(raw))))
        if not p.is_absolute():
            p = self.base_dir / p
        return p.resolve()

    @property
    def db_path(self) -> Path:
        return self.resolve_path("paths.database")

    @property
    def source_root(self) -> Path:
        return self.resolve_path("paths.source_root")

    @property
    def work_dir(self) -> Path:
        return self.resolve_path("paths.work_dir")

    @property
    def output_dir(self) -> Path:
        return self.resolve_path("paths.output_dir")

    @property
    def log_dir(self) -> Path:
        return self.resolve_path("paths.log_dir")

    @property
    def figures_dir(self) -> Path:
        return self.work_dir / "figures"

    def ensure_dirs(self) -> None:
        for p in (self.work_dir, self.output_dir, self.log_dir, self.figures_dir, self.db_path.parent):
            p.mkdir(parents=True, exist_ok=True)

    def fingerprint(self, *sections: str) -> str:
        """Stable hash of selected configuration sections (all if none given)."""
        payload = {s: self.get(s) for s in sections} if sections else self.data
        blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def extraction_fingerprint(self) -> str:
        from maintdoc.constants import EXTRACTOR_VERSION
        payload = {
            "v": EXTRACTOR_VERSION,
            "extraction": {k: v for k, v in self.get("extraction").items() if k != "workers"},
            "tables": self.get("tables"),
            "figures": self.get("figures"),
            "ocr": {k: v for k, v in self.get("ocr").items() if k not in ("tesseract_cmd", "tessdata_dir", "timeout_seconds")},
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

    def chapters(self) -> list[dict]:
        return list(self.get("classification.chapters", []))

    def chapter_title(self, chapter_id: str | None) -> str:
        for c in self.chapters():
            if c.get("id") == chapter_id:
                return c.get("title", chapter_id)
        if chapter_id == "unclassified" or not chapter_id:
            return "Unclassified Statements (Review Required)"
        return str(chapter_id)

    def with_overrides(self, overrides: dict) -> "Config":
        cfg = Config(deep_merge(self.data, overrides), None)
        cfg.path = self.path
        cfg.base_dir = self.base_dir
        return cfg

    def to_dict(self) -> dict:
        return copy.deepcopy(self.data)
