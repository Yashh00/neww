"""Figure (raster image / vector diagram) region detection and rendering."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from maintdoc.constants import ExtractionMethod
from maintdoc.utils import bbox_area, bbox_intersection

log = logging.getLogger(__name__)


@dataclass
class FigureRegion:
    bbox: tuple[float, float, float, float]
    method: str
    items: int


def image_regions(page: pymupdf.Page) -> list[tuple[float, float, float, float]]:
    out = []
    try:
        for info in page.get_image_info():
            b = info.get("bbox")
            if b:
                r = pymupdf.Rect(b) & page.rect
                if not r.is_empty:
                    out.append((r.x0, r.y0, r.x1, r.y1))
    except Exception as exc:  # noqa: BLE001
        log.debug("image info failed: %s", exc)
    return out


def image_coverage(page: pymupdf.Page, regions) -> float:
    area = page.rect.width * page.rect.height or 1
    # union approximation on a coarse grid to avoid double counting overlaps
    if not regions:
        return 0.0
    step = 8.0
    covered = 0
    total = 0
    y = page.rect.y0
    while y < page.rect.y1:
        x = page.rect.x0
        while x < page.rect.x1:
            total += 1
            if any(r[0] <= x <= r[2] and r[1] <= y <= r[3] for r in regions):
                covered += 1
            x += step
        y += step
    return covered / total if total else min(1.0, sum(bbox_area(r) for r in regions) / area)


def find_figures(page: pymupdf.Page, drawings: list[dict], images, table_bboxes, cfg,
                 skip_full_page_images: bool = False) -> list[FigureRegion]:
    if not cfg.get("figures.enabled", True):
        return []
    page_area = page.rect.width * page.rect.height or 1
    min_area = float(cfg.get("figures.min_area_ratio", 0.02)) * page_area
    regions: list[FigureRegion] = []
    for b in images:
        if bbox_area(b) < min_area:
            continue
        if skip_full_page_images and bbox_area(b) > 0.8 * page_area:
            continue
        regions.append(FigureRegion(b, ExtractionMethod.PYMUPDF_IMAGE, 1))
    if drawings:
        try:
            clusters = page.cluster_drawings(drawings=drawings)
        except Exception as exc:  # noqa: BLE001
            log.debug("cluster_drawings failed: %s", exc)
            clusters = []
        min_paths = int(cfg.get("figures.min_drawing_paths", 12))
        for c in clusters:
            b = (c.x0, c.y0, c.x1, c.y1)
            if bbox_area(b) < min_area:
                continue
            if any(bbox_intersection(b, t) > 0.5 * bbox_area(b) for t in table_bboxes):
                continue  # this vector cluster is a table grid
            # line paths have degenerate (zero-area) rects, so test coordinate overlap directly
            n = sum(1 for d in drawings if d.get("rect") is not None and _overlaps(d["rect"], b))
            if n < min_paths:
                continue
            regions.append(FigureRegion(b, ExtractionMethod.PYMUPDF_DRAWING, n))
    return _merge(regions)


def _overlaps(r, b, tol: float = 1.0) -> bool:
    return r[0] <= b[2] + tol and r[2] >= b[0] - tol and r[1] <= b[3] + tol and r[3] >= b[1] - tol


def _merge(regions: list[FigureRegion]) -> list[FigureRegion]:
    merged: list[FigureRegion] = []
    for r in sorted(regions, key=lambda x: -bbox_area(x.bbox)):
        for m in merged:
            if bbox_intersection(r.bbox, m.bbox) > 0.3 * min(bbox_area(r.bbox), bbox_area(m.bbox)):
                m.bbox = (min(m.bbox[0], r.bbox[0]), min(m.bbox[1], r.bbox[1]),
                          max(m.bbox[2], r.bbox[2]), max(m.bbox[3], r.bbox[3]))
                m.items += r.items
                break
        else:
            merged.append(FigureRegion(r.bbox, r.method, r.items))
    return sorted(merged, key=lambda x: (x.bbox[1], x.bbox[0]))


def render_region(page: pymupdf.Page, bbox, out_path: Path, dpi: int = 150, pad: float = 4.0) -> Path:
    clip = pymupdf.Rect(bbox) + (-pad, -pad, pad, pad)
    clip &= page.rect
    pix = page.get_pixmap(clip=clip, dpi=dpi)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(out_path))
    return out_path
