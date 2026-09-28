"""Unicode font registration for ReportLab (degree, plus-minus, superscripts, etc.)."""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_CANDIDATES = [
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/calibri.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]
_BOLD_NAMES = {"arial.ttf": "arialbd.ttf", "calibri.ttf": "calibrib.ttf", "DejaVuSans.ttf": "DejaVuSans-Bold.ttf",
               "Arial.ttf": "Arial Bold.ttf"}
_REGISTERED: tuple[str, str, bool] | None = None


def register_fonts(candidates: list[str] | None = None) -> tuple[str, str, bool]:
    """Return (regular font name, bold font name, unicode_capable)."""
    global _REGISTERED
    if _REGISTERED:
        return _REGISTERED
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for cand in list(candidates or []) + DEFAULT_CANDIDATES:
        p = Path(cand)
        if not p.exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont("MDoc", str(p)))
            bold = p.with_name(_BOLD_NAMES.get(p.name, p.name))
            pdfmetrics.registerFont(TTFont("MDoc-Bold", str(bold if bold.exists() else p)))
            addMapping("MDoc", 0, 0, "MDoc")
            addMapping("MDoc", 1, 0, "MDoc-Bold")
            addMapping("MDoc", 0, 1, "MDoc")
            addMapping("MDoc", 1, 1, "MDoc-Bold")
            _REGISTERED = ("MDoc", "MDoc-Bold", True)
            log.debug("PDF font: %s", p)
            return _REGISTERED
        except Exception as exc:  # noqa: BLE001 - try next candidate
            log.warning("Cannot use font %s: %s", p, exc)
    log.warning("No Unicode TrueType font found; falling back to Helvetica (symbols like >= may not render). "
                "Set generation.pdf_font_paths in config.yaml.")
    _REGISTERED = ("Helvetica", "Helvetica-Bold", False)
    return _REGISTERED
