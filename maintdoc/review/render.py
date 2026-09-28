"""Render source PDF pages (with the cited region highlighted) for visual review."""

from __future__ import annotations

from pathlib import Path

import pymupdf

from maintdoc.utils import sha256_file


class SourceChanged(RuntimeError):
    pass


def render_page(path: str | Path, page_no: int, bbox: tuple | None = None, zoom: float = 1.6,
                expected_sha256: str | None = None, clip: tuple | None = None) -> bytes:
    """PNG bytes of a page; raises SourceChanged if the file no longer matches the evidence hash."""
    path = Path(path)
    if expected_sha256 and sha256_file(path) != expected_sha256:
        raise SourceChanged(f"{path.name} changed since extraction - evidence cannot be shown against it")
    with pymupdf.open(str(path)) as doc:
        page = doc.load_page(page_no - 1)
        if bbox and all(v is not None for v in bbox):
            r = pymupdf.Rect(bbox) + (-3, -3, 3, 3)
            annot_shape = page.new_shape()
            annot_shape.draw_rect(r)
            annot_shape.finish(color=(0.9, 0.1, 0.1), width=2)
            annot_shape.commit(overlay=True)
        kwargs = {"matrix": pymupdf.Matrix(zoom, zoom)}
        if clip:
            kwargs["clip"] = pymupdf.Rect(clip) & page.rect
        return page.get_pixmap(**kwargs).tobytes("png")


def render_pdf_pages(path: str | Path, first: int = 1, count: int = 3, zoom: float = 1.0) -> list[bytes]:
    out = []
    with pymupdf.open(str(path)) as doc:
        for i in range(first - 1, min(doc.page_count, first - 1 + count)):
            out.append(doc.load_page(i).get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png"))
    return out
