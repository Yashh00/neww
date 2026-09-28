"""Selective local OCR with Tesseract and OpenCV preprocessing.

Tesseract runs locally (its LSTM recognition models ship with the local
installation; no network access). Pages are rendered with PyMuPDF, cleaned
with OpenCV (grayscale, denoise, deskew, binarisation), recognised with
``image_to_data`` and grouped into paragraph units with word-level confidence.
If the result is weak, alternative preprocessing variants are tried and the
variant with the best mean confidence is kept.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
import pymupdf

from maintdoc.constants import BlockType, ExtractionMethod
from maintdoc.extraction.layout import LIST_RE, NUMBERED_HEADING_RE, Unit

log = logging.getLogger(__name__)

WINDOWS_DEFAULTS = [r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"]


@dataclass
class OcrResult:
    units: list[Unit] = field(default_factory=list)
    confidence: float | None = None
    word_count: int = 0
    low_conf_words: int = 0
    preprocess: str = ""
    angle: float = 0.0
    rotation: int = 0
    error: str | None = None


def configure_tesseract(cfg) -> tuple[bool, str]:
    """Locate the tesseract binary. Returns (available, version-or-reason)."""
    import pytesseract
    cmd = (cfg.get("ocr.tesseract_cmd") or "").strip()
    if not cmd:
        found = shutil.which("tesseract")
        if not found and os.name == "nt":
            found = next((p for p in WINDOWS_DEFAULTS if Path(p).exists()), None)
        cmd = found or ""
    if not cmd:
        return False, "tesseract executable not found (install Tesseract or set ocr.tesseract_cmd)"
    pytesseract.pytesseract.tesseract_cmd = cmd
    tessdata = (cfg.get("ocr.tessdata_dir") or "").strip()
    if tessdata:
        os.environ["TESSDATA_PREFIX"] = tessdata
    return _probe(cmd, cfg.get("ocr.languages", "eng"))


@lru_cache(maxsize=4)
def _probe(cmd: str, languages: str) -> tuple[bool, str]:
    import pytesseract
    try:
        version = str(pytesseract.get_tesseract_version())
        installed = set(pytesseract.get_languages(config=""))
    except Exception as exc:  # noqa: BLE001
        return False, f"tesseract not usable: {exc}"
    missing = [lang for lang in languages.split("+") if lang and lang not in installed]
    if missing:
        return False, f"tesseract {version}: language pack(s) not installed: {', '.join(missing)}"
    return True, version


# --------------------------------------------------------------------------- preprocessing
def render_gray(page: pymupdf.Page, dpi: int) -> np.ndarray:
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    return img.copy()


def estimate_skew(gray: np.ndarray, max_deg: float) -> float:
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    # connect characters into text lines, then measure line orientation
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(15, gray.shape[1] // 60), 3))
    lines = cv2.dilate(bw, kernel, iterations=1)
    contours, _ = cv2.findContours(lines, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    angles, weights = [], []
    for c in contours:
        (cx, cy), (w, h), a = cv2.minAreaRect(c)
        if w < h:
            w, h = h, w
            a = a - 90
        if w < gray.shape[1] * 0.1 or h < 3:
            continue
        if a > 45:
            a -= 90
        elif a < -45:
            a += 90
        if abs(a) <= max_deg:
            angles.append(a)
            weights.append(w)
    if not angles:
        return 0.0
    return float(np.average(angles, weights=weights))


def rotate(gray: np.ndarray, angle: float) -> np.ndarray:
    h, w = gray.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=255)


def preprocess(gray: np.ndarray, opts: dict, variant: str | None = None) -> tuple[np.ndarray, str, float]:
    steps = []
    img = gray
    angle = 0.0
    if opts.get("clahe"):
        img = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(img)
        steps.append("clahe")
    denoise = opts.get("denoise", "median")
    if denoise == "median":
        img = cv2.medianBlur(img, 3)
        steps.append("median3")
    elif denoise == "nlmeans":
        img = cv2.fastNlMeansDenoising(img, h=12)
        steps.append("nlmeans")
    if opts.get("deskew", True):
        angle = estimate_skew(img, float(opts.get("max_deskew_degrees", 10.0)))
        if abs(angle) >= 0.2:
            img = rotate(img, angle)
            steps.append(f"deskew{angle:+.2f}")
    binarize = variant or opts.get("binarize", "otsu")
    if binarize == "otsu":
        _, img = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        steps.append("otsu")
    elif binarize == "adaptive":
        img = cv2.adaptiveThreshold(img, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 12)
        steps.append("adaptive")
    else:
        steps.append("gray")
    return img, "+".join(steps), angle


# --------------------------------------------------------------------------- recognition
def _detect_orientation(gray: np.ndarray, timeout: int) -> int:
    import pytesseract
    try:
        osd = pytesseract.image_to_osd(gray, output_type=pytesseract.Output.DICT, timeout=timeout)
        rot = int(osd.get("rotate", 0))
        conf = float(osd.get("orientation_conf", 0))
        return rot if rot in (90, 180, 270) and conf >= 2.0 else 0
    except Exception:  # noqa: BLE001 - OSD fails on sparse pages; orientation stays unchanged
        return 0


def _recognise(img: np.ndarray, cfg) -> dict:
    import pytesseract
    config = f"--oem {int(cfg.get('ocr.oem', 1))} --psm {int(cfg.get('ocr.psm', 3))}"
    return pytesseract.image_to_data(img, lang=cfg.get("ocr.languages", "eng"), config=config,
                                     output_type=pytesseract.Output.DICT,
                                     timeout=int(cfg.get("ocr.timeout_seconds", 120)))


def _units_from_data(data: dict, scale: float, approx: bool, cfg) -> tuple[list[Unit], float | None, int, int]:
    groups: dict[tuple[int, int], list[int]] = {}
    n = len(data.get("text", []))
    for i in range(n):
        word = (data["text"][i] or "").strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if not word or conf < 0:
            continue
        groups.setdefault((data["block_num"][i], data["par_num"][i]), []).append(i)
    units: list[Unit] = []
    total_chars = 0
    weighted = 0.0
    words = 0
    low_words = 0
    block_low = float(cfg.get("ocr.block_low_confidence", 60.0))
    segments: list[list[int]] = []
    for key in sorted(groups, key=lambda k: (min(data["top"][i] for i in groups[k]), k)):
        segments.extend(_split_paragraph(groups[key], data))
    for idx in segments:
        lines: dict[int, list[int]] = {}
        for i in idx:
            lines.setdefault(data["line_num"][i], []).append(i)
        line_texts = []
        for ln in sorted(lines, key=lambda l: min(data["top"][i] for i in lines[l])):
            ws = sorted(lines[ln], key=lambda i: data["left"][i])
            line_texts.append(" ".join(data["text"][i].strip() for i in ws))
        text = " ".join(line_texts).strip()
        if not text:
            continue
        confs = [float(data["conf"][i]) for i in idx]
        lens = [len(data["text"][i].strip()) for i in idx]
        uconf = sum(c * l for c, l in zip(confs, lens)) / max(1, sum(lens))
        words += len(idx)
        low_words += sum(1 for c in confs if c < block_low)
        total_chars += sum(lens)
        weighted += sum(c * l for c, l in zip(confs, lens))
        x0 = min(data["left"][i] for i in idx) / scale
        y0 = min(data["top"][i] for i in idx) / scale
        x1 = max(data["left"][i] + data["width"][i] for i in idx) / scale
        y1 = max(data["top"][i] + data["height"][i] for i in idx) / scale
        heights = [data["height"][i] / scale for i in idx]
        u = Unit(text=text, bbox=(x0, y0, x1, y1), method=ExtractionMethod.TESSERACT_OCR,
                 ocr_confidence=round(uconf, 1), bbox_approx=approx,
                 font_size=round(float(np.median(heights)) * 1.1, 1))
        if uconf < block_low:
            u.quality_flags.append("ocr_low_confidence")
        units.append(u)
    page_conf = (weighted / total_chars) if total_chars else None
    return units, (round(page_conf, 1) if page_conf is not None else None), words, low_words


def _split_paragraph(idx: list[int], data: dict) -> list[list[int]]:
    """Split a Tesseract paragraph into statement units.

    Tesseract often merges consecutive short lines (checklists, one sentence
    per line, heading + text) into one paragraph. A new unit starts when the
    vertical gap is large, the line starts with a list/heading/signal pattern,
    or the previous line ended a sentence well before the right margin.
    """
    by_line: dict[tuple, list[int]] = {}
    for i in idx:
        by_line.setdefault((data["block_num"][i], data["par_num"][i], data["line_num"][i]), []).append(i)
    lines = sorted(by_line.values(), key=lambda ws: min(data["top"][i] for i in ws))
    if len(lines) <= 1:
        return [idx]
    info = []
    for ws in lines:
        ws = sorted(ws, key=lambda i: data["left"][i])
        text = " ".join(data["text"][i].strip() for i in ws)
        top = min(data["top"][i] for i in ws)
        bottom = max(data["top"][i] + data["height"][i] for i in ws)
        left = min(data["left"][i] for i in ws)
        right = max(data["left"][i] + data["width"][i] for i in ws)
        info.append((ws, text, top, bottom, left, right))
    heights = sorted(b - t for _, _, t, b, _, _ in info)
    line_h = heights[len(heights) // 2] or 1
    max_right = max(r for *_, r in info)
    min_left = min(l for *_, l, _ in info)
    width = max(1, max_right - min_left)
    out: list[list[int]] = [list(info[0][0])]
    for prev, cur in zip(info, info[1:]):
        _, ptext, _, pbottom, _, pright = prev
        ws, text, top, _, _, _ = cur
        gap = top - pbottom
        new = (gap > 0.6 * line_h
               or bool(LIST_RE.match(text))
               or bool(NUMBERED_HEADING_RE.match(text)) and len(text.split()) <= 8
               or (ptext.rstrip().endswith((".", ":", "!", "?")) and text[:1].isupper()
                   and (pright - min_left) < 0.85 * width))
        if new:
            out.append(list(ws))
        else:
            out[-1].extend(ws)
    return out


def ocr_page(page: pymupdf.Page, cfg) -> OcrResult:
    """OCR a page. Never raises; failures are returned in ``OcrResult.error``."""
    dpi = int(cfg.get("ocr.dpi", 300))
    scale = dpi / 72.0
    opts = dict(cfg.get("ocr.preprocessing", {}) or {})
    timeout = int(cfg.get("ocr.timeout_seconds", 120))
    try:
        gray = render_gray(page, dpi)
        rotation = 0
        if cfg.get("ocr.detect_orientation", True):
            rotation = _detect_orientation(gray, timeout)
            if rotation:
                gray = np.ascontiguousarray(np.rot90(gray, k={90: 3, 180: 2, 270: 1}[rotation]))
        variants = [opts.get("binarize", "otsu")]
        retry_below = float(cfg.get("ocr.retry_below_confidence", 75.0))
        for alt in ("adaptive", "none", "otsu"):
            if alt not in variants:
                variants.append(alt)
        best: OcrResult | None = None
        for k, variant in enumerate(variants):
            img, desc, angle = preprocess(gray, opts, variant)
            data = _recognise(img, cfg)
            approx = bool(rotation) or abs(angle) >= 0.2
            units, conf, words, low = _units_from_data(data, scale, approx, cfg)
            res = OcrResult(units, conf, words, low, desc, angle, rotation)
            if best is None or (conf or 0) > (best.confidence or 0):
                best = res
            if conf is not None and conf >= retry_below:
                break
            if k >= 2:
                break
        assert best is not None
        _classify_ocr_units(best.units, cfg)
        return best
    except Exception as exc:  # noqa: BLE001 - OCR problems are recorded per page
        log.warning("OCR failed on page %s: %s", page.number + 1, exc)
        return OcrResult(error=str(exc))


def _classify_ocr_units(units: list[Unit], cfg) -> None:
    """Block types for OCR text (no font information): signal words, numbered/caps headings, lists."""
    import re
    from maintdoc.extraction.layout import LayoutAnalyzer
    la = LayoutAnalyzer(cfg, body_size=10.0, hf_templates=set())
    caption_re = re.compile(cfg.get("extraction.caption_pattern"), re.IGNORECASE)
    sizes = sorted(u.font_size for u in units if u.font_size)
    median = sizes[len(sizes) // 2] if sizes else 0
    for u in units:
        t = u.text.strip()
        sig, _ = la.signal_of(t)
        words = t.split()
        if sig:
            u.block_type = sig
            u.safety_level = sig
        elif caption_re.match(t) and len(t) < 200:
            u.block_type = BlockType.CAPTION
        elif LIST_RE.match(t) and not (NUMBERED_HEADING_RE.match(t) and len(words) <= 6 and not t.endswith(".")):
            u.block_type = BlockType.LIST_ITEM
        elif len(words) <= 10 and not t.endswith((".", ",", ";", ":")) and (
                NUMBERED_HEADING_RE.match(t) or (t.isupper() and len(t) > 3) or
                (median and u.font_size >= median * 1.3)):
            u.block_type = BlockType.HEADING
            m = NUMBERED_HEADING_RE.match(t)
            u.heading_level = min(6, m.group(1).count(".") + 1) if m else (
                1 if median and u.font_size >= median * 1.5 else 2)
