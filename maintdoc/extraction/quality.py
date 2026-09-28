"""Heuristic text-quality scoring used to detect broken text layers and gibberish.

The score (0..1) penalises: '(cid:n)' glyph references, replacement and
private-use characters, control characters, tokens without vowels or with
long consonant runs, and very low letter ratios. It is a heuristic flag for
human review, not a linguistic judgement.
"""

from __future__ import annotations

import re
import unicodedata

_CID = re.compile(r"\(cid:\d+\)")
_TOKEN = re.compile(r"[^\W\d_]+", re.UNICODE)
_VOWELS = set("aeiouyäöüàáâãåæèéêëìíîïòóôõøùúûýÿœAEIOUYÄÖÜ")
_CONSONANT_RUN = re.compile(r"[bcdfghjklmnpqrstvwxz]{5,}", re.IGNORECASE)
# common abbreviations in maintenance texts that have no vowels
_OK_NOVOWEL = {"nm", "psi", "kpa", "mpa", "rpm", "hp", "kw", "mm", "cm", "km", "ml", "pn", "qty", "nr", "st",
               "ltd", "lbs", "lb", "ft", "hz", "kv", "mv", "vdc", "vac", "gpm", "cst", "pcs", "cw", "ccw", "dn",
               "pvc", "ptfe", "npt", "bsp", "tbd", "sn", "dwg", "rh", "lh", "hmi", "plc", "gnd", "pwr", "nbr", "fkm"}


def text_quality(text: str) -> tuple[float, list[str]]:
    """Return (score 0..1, flags)."""
    if not text or not text.strip():
        return 0.0, ["empty"]
    flags: list[str] = []
    n = len(text)
    cid = len(_CID.findall(text))
    stripped = _CID.sub("", text)
    repl = stripped.count("�")
    pua = sum(1 for c in stripped if 0xE000 <= ord(c) <= 0xF8FF)
    ctrl = sum(1 for c in stripped if unicodedata.category(c) == "Cc" and c not in "\n\r\t")
    letters = sum(1 for c in stripped if c.isalpha())
    visible = sum(1 for c in stripped if not c.isspace()) or 1
    penalty = 0.0
    if cid:
        flags.append("cid_glyphs")
        penalty += min(1.0, cid * 8 / n)
    if repl or pua:
        flags.append("replacement_or_private_use_chars")
        penalty += min(1.0, (repl + pua) * 3 / visible)
    if ctrl:
        flags.append("control_chars")
        penalty += min(0.5, ctrl * 3 / visible)
    tokens = [t for t in _TOKEN.findall(stripped) if len(t) >= 3]
    if tokens:
        bad = 0
        for t in tokens:
            low = t.lower()
            if low in _OK_NOVOWEL or t.isupper() and len(t) <= 5:
                continue
            is_latin = all(ord(c) < 0x250 for c in t)
            if not is_latin:
                continue
            if not any(c in _VOWELS for c in t) or _CONSONANT_RUN.search(t):
                bad += 1
            elif len(t) > 3 and sum(1 for c in t[1:] if c.isupper()) >= 2 and not t.isupper():
                bad += 1  # rAnDoM case
        bad_ratio = bad / len(tokens)
        if bad_ratio > 0.25:
            flags.append("implausible_words")
        penalty += bad_ratio * 1.2
    letter_ratio = letters / visible
    if visible > 20 and letter_ratio < 0.25:
        flags.append("few_letters")
        penalty += (0.25 - letter_ratio) * 1.5
    score = max(0.0, min(1.0, 1.0 - penalty))
    return round(score, 3), flags


def is_gibberish(text: str, threshold: float, min_chars: int) -> tuple[bool, float, list[str]]:
    score, flags = text_quality(text)
    return (len(text.strip()) >= min_chars and score < threshold), score, flags
