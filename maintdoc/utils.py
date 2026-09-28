"""Small shared helpers: hashing, text normalisation, IDs, time and JSON."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import secrets
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

HASH_CHUNK = 1024 * 1024


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def new_run_id(command: str) -> str:
    ts = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    return f"RUN-{ts}-{command[:8]}-{secrets.token_hex(2)}"


def sha256_file(path: str | os.PathLike, chunk: int = HASH_CHUNK) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(obj: Any) -> str:
    return sha256_text(json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False))


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False)


def loads(text: str | None, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_QUOTES = {ord("‘"): "'", ord("’"): "'", ord("‚"): "'", ord("‛"): "'",
           ord("“"): '"', ord("”"): '"', ord("„"): '"', ord("«"): '"', ord("»"): '"'}
_SPACES = re.compile(r"\s+")
_SOFT = dict.fromkeys(map(ord, "­​‌‍﻿"), None)


def normalize_text(text: str) -> str:
    """Normalisation used for exact-duplicate hashing.

    Deliberately conservative: digits, decimal separators, signs, units and
    symbols are preserved so that differing numbers never hash identically.
    Only Unicode compatibility forms, dash/quote variants, soft characters,
    whitespace and letter case are normalised.
    """
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = t.translate(_SOFT).translate(_DASHES).translate(_QUOTES)
    # Rejoin words hyphenated across line breaks ("hydrau-\nlic")
    t = re.sub(r"(?<=[a-z])-\n(?=[a-z])", "", t)
    t = _SPACES.sub(" ", t).strip().lower()
    return t


def norm_hash(text: str) -> str:
    return sha256_text(normalize_text(text))


def collapse_ws(text: str) -> str:
    return _SPACES.sub(" ", text or "").strip()


_NUM_TOKEN = re.compile(r"(?<![A-Za-z])[-+−]?\d+(?:[.,]\d+)*")


def numeric_tokens(text: str) -> Counter:
    """Multiset of numeric tokens exactly as printed (sign normalised)."""
    t = unicodedata.normalize("NFKC", text or "").translate(_DASHES)
    tokens = []
    for m in _NUM_TOKEN.finditer(t):
        tok = m.group(0).lstrip("+")
        # a leading '-' is only a sign when not preceded by an alphanumeric (e.g. "HP-200")
        if tok.startswith("-") and m.start() > 0 and t[m.start() - 1].isalnum():
            tok = tok[1:]
        tokens.append(tok)
    return Counter(tokens)


def short_sha(sha: str | None, n: int = 12) -> str:
    return (sha or "")[:n]


def safe_filename(name: str, max_len: int = 80) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return cleaned[:max_len] or "file"


def chunked(seq: list, size: int) -> Iterable[list]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def rel_posix(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def truncate(text: str | None, limit: int) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 40)] + " ...[truncated; full text in maintenance.db]"


def bbox_tuple(b) -> tuple[float, float, float, float]:
    return (float(b[0]), float(b[1]), float(b[2]), float(b[3]))


def bbox_area(b) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def bbox_intersection(a, b) -> float:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return (x1 - x0) * (y1 - y0)


def bbox_center_inside(inner, outer, pad: float = 1.0) -> bool:
    cx, cy = (inner[0] + inner[2]) / 2, (inner[1] + inner[3]) / 2
    return outer[0] - pad <= cx <= outer[2] + pad and outer[1] - pad <= cy <= outer[3] + pad


def bbox_union(boxes) -> tuple[float, float, float, float] | None:
    boxes = list(boxes)
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))
