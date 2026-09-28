"""Generate a synthetic maintenance-document corpus covering the edge cases the
processor must handle: digital manuals, a scanned (image-only) document,
an exact duplicate file, multi-revision documents with changed values,
conflicting documents for the same model, a different model that must NOT be
treated as conflicting, a two-column layout, corrupt, truncated and encrypted
PDFs, an empty page, gibberish text and unit problems.

All content is fictitious and generated locally.
"""

from __future__ import annotations

import io
import math
import random
import shutil
from pathlib import Path

import pymupdf

A4 = (595, 842)
MARGIN = 56


class _Writer:
    def __init__(self, header: str | None = None):
        self.doc = pymupdf.open()
        self.header = header
        self.page: pymupdf.Page | None = None
        self.y = 0.0

    def new_page(self) -> pymupdf.Page:
        self.page = self.doc.new_page(width=A4[0], height=A4[1])
        self.y = 72
        return self.page

    def _ensure(self, h: float) -> None:
        if self.page is None or self.y + h > A4[1] - 70:
            self.new_page()

    def text(self, text: str, size: float = 10, bold: bool = False, gap: float = 6, indent: float = 0,
             width: float | None = None, x0: float | None = None) -> None:
        font = "hebo" if bold else "helv"
        left = (x0 if x0 is not None else MARGIN) + indent
        w = width or (A4[0] - MARGIN - left)
        est = pymupdf.get_text_length(text, fontname=font, fontsize=size)
        lines = max(1, math.ceil(est / (w * 0.92))) + text.count("\n")
        h = lines * size * 1.35 + 4
        self._ensure(h)
        rect = pymupdf.Rect(left, self.y, left + w, self.y + h)
        rc = self.page.insert_textbox(rect, text, fontsize=size, fontname=font)
        if rc < 0:  # nothing was written because the box was too small: enlarge and retry
            rect.y1 += -rc + size
            self.page.insert_textbox(rect, text, fontsize=size, fontname=font)
        self.y = rect.y1 + gap

    def heading(self, text: str, level: int = 1) -> None:
        size = {1: 14, 2: 12, 3: 11}.get(level, 11)
        self.y += 4
        self.text(text, size=size, bold=True, gap=4)

    def table(self, rows: list[list[str]], widths: list[float], size: float = 9) -> None:
        row_h = size * 2.0
        h = row_h * len(rows)
        self._ensure(h + 10)
        x = MARGIN
        y = self.y
        total_w = sum(widths)
        for r_i, r in enumerate(rows):
            cx = x
            for c_i, cell in enumerate(r):
                font = "hebo" if r_i == 0 else "helv"
                self.page.insert_text((cx + 4, y + row_h * 0.68), cell, fontsize=size, fontname=font)
                cx += widths[c_i]
            y += row_h
        # grid
        for i in range(len(rows) + 1):
            self.page.draw_line((x, self.y + i * row_h), (x + total_w, self.y + i * row_h), width=0.8)
        cx = x
        for wdt in [0] + widths:
            cx += wdt
            self.page.draw_line((cx, self.y), (cx, self.y + h), width=0.8)
        self.y += h + 10

    def figure(self, caption: str, height: float = 150) -> None:
        self._ensure(height + 30)
        p = self.page
        top = self.y
        x0, x1 = MARGIN + 60, A4[0] - MARGIN - 60
        # simple hydraulic-circuit style vector diagram
        p.draw_rect(pymupdf.Rect(x0, top, x0 + 90, top + 50), width=1.2)
        p.draw_circle((x0 + 190, top + 25), 22, width=1.2)
        p.draw_rect(pymupdf.Rect(x1 - 110, top + 10, x1, top + 40), width=1.2)
        for i in range(6):
            p.draw_line((x0 + 90, top + 10 + i * 6), (x0 + 168, top + 10 + i * 6), width=0.6)
        p.draw_line((x0 + 212, top + 25), (x1 - 110, top + 25), width=1.0)
        p.draw_line((x0 + 45, top + 50), (x0 + 45, top + height - 20), width=1.0)
        p.draw_line((x0 + 45, top + height - 20), (x1 - 55, top + height - 20), width=1.0)
        p.draw_line((x1 - 55, top + height - 20), (x1 - 55, top + 40), width=1.0)
        for k in range(5):
            p.draw_circle((x0 + 100 + k * 40, top + height - 20), 5, width=0.8)
        self.y = top + height + 4
        self.text(caption, size=9, bold=False, gap=8)

    def finish(self, path: Path, footer_total: bool = True, encryption: dict | None = None,
               metadata: dict | None = None) -> None:
        n = self.doc.page_count
        for i, page in enumerate(self.doc):
            if self.header:
                page.insert_text((MARGIN, 40), self.header, fontsize=8, fontname="helv")
            if footer_total:
                page.insert_text((A4[0] / 2 - 25, A4[1] - 30), f"Page {i + 1} of {n}", fontsize=8,
                                 fontname="helv")
        if metadata:
            self.doc.set_metadata(metadata)
        path.parent.mkdir(parents=True, exist_ok=True)
        if encryption:
            self.doc.save(str(path), **encryption)
        else:
            self.doc.save(str(path), garbage=3, deflate=True)
        self.doc.close()


def _manual(path: Path, rev: str, date: str, coupling_nm: str, coupling_ftlb: str, filter_hours: str) -> None:
    w = _Writer(header=f"HP-200 Maintenance Manual - MM-HP200 Rev. {rev}")
    w.new_page()
    w.text("HP-200 Hydraulic Press", size=20, bold=True, gap=2)
    w.text("Maintenance Manual", size=15, bold=True, gap=6)
    w.text(f"Document No. MM-HP200    Revision: {rev}    Date: {date}", size=10, gap=14)
    w.heading("1 Scope")
    w.text("This manual applies to the HP-200 hydraulic press. It describes preventive maintenance, "
           "inspections and repair procedures for qualified maintenance personnel.")
    w.heading("2 Safety Instructions")
    w.text("WARNING: Depressurize the hydraulic system before opening any fitting. Residual pressure can "
           "cause serious injury.", bold=False)
    w.text("CAUTION: Hydraulic oil may be hot. Allow the oil to cool below 40 °C before draining.")
    w.text("Lock out and tag out the main power supply before starting any maintenance work.")
    w.new_page()
    w.heading("3 Technical Specifications")
    w.text("Table 1 - Technical data", size=9, gap=4)
    w.table([["Parameter", "Value", "Unit"],
             ["Maximum operating pressure", "210", "bar"],
             ["Oil tank capacity", "120", "L"],
             ["Motor power", "15", "kW"],
             ["Maximum oil temperature", "65", "°C"]], [230, 90, 90])
    w.heading("4 Preventive Maintenance")
    w.text(f"Replace the hydraulic filter element every {filter_hours} operating hours.")
    w.text("Check the hydraulic oil level daily before operation.")
    w.text("Lubricate the guide rails every 250 operating hours.")
    w.new_page()
    w.heading("5 Maintenance Procedures")
    w.heading("5.1 Replacing the pump coupling", level=2)
    w.text("WARNING: Switch off and lock out the main power supply before removing the coupling guard.")
    w.text("1. Switch off the press and depressurize the hydraulic system.")
    w.text("2. Remove the four coupling guard bolts.")
    w.text("3. Replace the coupling insert.")
    w.text(f"4. Tighten the coupling bolts to {coupling_nm} Nm ({coupling_ftlb} ft-lb).")
    w.text("5. Refit the coupling guard.")
    w.figure("Figure 1 - Hydraulic circuit overview")
    w.new_page()
    w.heading("6 Troubleshooting")
    w.text("Pump noise: check the oil level and clean the suction strainer.")
    w.text("Pressure too low: check the relief valve setting of 210 bar.")
    w.heading("7 Spare Parts")
    w.table([["Part No.", "Description", "Qty"],
             ["HF-1001", "Hydraulic filter element", "1"],
             ["SK-2002", "Seal kit, main cylinder", "1"],
             ["CI-3003", "Coupling insert", "1"]], [110, 230, 60])
    w.heading("8 Revision History")
    w.text("Rev. A 2020-01-10 Initial release.")
    w.text(f"Rev. {rev} {date} Updated maintenance data.")
    w.new_page()  # intentionally empty page (no text, no graphics)
    w.finish(path, metadata={"title": "HP-200 Maintenance Manual", "author": "Example Engineering",
                             "subject": f"MM-HP200 Rev. {rev}"})


def _service_bulletin_hp300(path: Path) -> None:
    w = _Writer()
    w.new_page()
    w.text("HP-300 Service Bulletin SB-017", size=16, bold=True, gap=4)
    w.text("Revision: 1    Date: 2023-02-01", gap=12)
    w.heading("1 Scope")
    w.text("This bulletin applies to the HP-300 hydraulic press only.")
    w.heading("2 Technical Specifications")
    w.text("Maximum operating pressure: 250 bar.")
    w.heading("3 Maintenance Procedures")
    w.text("Tighten the coupling bolts to 60 Nm.")
    w.finish(path, footer_total=False)


def _pump_sheet(path: Path) -> None:
    w = _Writer()
    w.new_page()
    w.text("HP-200 Hydraulic Pump Service Sheet", size=16, bold=True, gap=10)
    w.heading("1 Technical Specifications")
    w.text("Maximum operating pressure: 230 bar.")
    w.text("Tighten the pump mounting bolts to 85 Nm.")
    w.heading("2 Maintenance Procedures")
    w.text("Tighten the drain plug to a torque of 25.")
    w.text("Set the pump coupling torque to 40 bar.")
    w.text("Xqzvbn trkplm wqrtzx hjkgfd bnmvcx zxqwrt plkjhg")
    w.finish(path, footer_total=False)


def _two_column(path: Path) -> None:
    w = _Writer()
    w.new_page()
    w.text("HP-200 Lubrication Guide", size=16, bold=True, gap=4)
    w.text("Revision: 2    Date: 2022-09-30", gap=10)
    top = w.y
    col_w = (A4[0] - 2 * MARGIN - 24) / 2
    w.heading("1 Lubrication Points")
    left_texts = ["Left column first: grease the guide rails with lithium grease.",
                  "Left column second: clean the grease nipples before greasing.",
                  "Left column third: remove excess grease after lubrication."]
    right_texts = ["Right column first: check the central lubrication reservoir weekly.",
                   "Right column second: refill the reservoir with EP2 grease.",
                   "Right column third: record the lubrication in the maintenance log."]
    y_start = w.y
    for t in left_texts:
        w.text(t, width=col_w, x0=MARGIN)
    w.y = y_start
    for t in right_texts:
        w.text(t, width=col_w, x0=MARGIN + col_w + 24)
    _ = top
    w.finish(path, footer_total=False)


def _scanned(path: Path, rotate_deg: float = 0.8, noise: bool = True, dpi: int = 200) -> None:
    """Render a digital page to an image and wrap it in an image-only PDF."""
    tmp = _Writer()
    tmp.new_page()
    tmp.text("HP-200 Inspection Checklist", size=18, bold=True, gap=6)
    tmp.text("Revision: A    Date: 2021-11-05", size=12, gap=12)
    tmp.heading("1 Weekly Inspections")
    tmp.text("Inspect all hydraulic hoses for leaks weekly.", size=12)
    tmp.text("Check the accumulator pre-charge pressure: 90 bar.", size=12)
    tmp.text("Check the coupling bolts torque: 45 Nm.", size=12)
    tmp.text("Inspect the press frame for cracks.", size=12)
    pix = tmp.doc[0].get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    tmp.doc.close()
    from PIL import Image
    img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    if rotate_deg:
        img = img.rotate(rotate_deg, expand=False, fillcolor=255, resample=Image.BICUBIC)
    if noise:
        rnd = random.Random(42)
        px = img.load()
        for _ in range(img.width * img.height // 400):
            x, y = rnd.randrange(img.width), rnd.randrange(img.height)
            px[x, y] = rnd.choice((0, 90, 180))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    doc = pymupdf.open()
    page = doc.new_page(width=A4[0], height=A4[1])
    page.insert_image(page.rect, stream=buf.getvalue())
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path), deflate=True)
    doc.close()


def make_corpus(root: Path, include_scanned: bool = True) -> dict[str, Path]:
    """Create the synthetic corpus below ``root`` and return the key paths."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    p: dict[str, Path] = {}
    p["rev_b"] = root / "HP-200" / "HP-200_Maintenance_Manual_RevB.pdf"
    _manual(p["rev_b"], "B", "2022-03-15", "45", "33", "500")
    p["rev_c"] = root / "HP-200" / "Rev_C" / "HP-200_Maintenance_Manual_RevC.pdf"
    _manual(p["rev_c"], "C", "2023-06-01", "50", "37", "1000")
    p["duplicate"] = root / "HP-200" / "copies" / "HP-200_Maintenance_Manual_RevB_copy.pdf"
    p["duplicate"].parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(p["rev_b"], p["duplicate"])
    p["hp300"] = root / "HP-300" / "HP-300_Service_Bulletin_SB-017.pdf"
    _service_bulletin_hp300(p["hp300"])
    p["pump"] = root / "Pump" / "HP-200_Pump_Service_Sheet.pdf"
    _pump_sheet(p["pump"])
    p["two_column"] = root / "HP-200" / "HP-200_Lubrication_Guide_Rev2.pdf"
    _two_column(p["two_column"])
    if include_scanned:
        p["scanned"] = root / "Scanned" / "HP-200_Inspection_Checklist_scan.pdf"
        _scanned(p["scanned"])
    p["corrupt"] = root / "Corrupt" / "damaged_manual.pdf"
    p["corrupt"].parent.mkdir(parents=True, exist_ok=True)
    rnd = random.Random(7)
    p["corrupt"].write_bytes(b"%PDF-1.7\n" + bytes(rnd.randrange(256) for _ in range(4000)))
    p["truncated"] = root / "Corrupt" / "truncated_manual.pdf"
    data = p["rev_b"].read_bytes()
    p["truncated"].write_bytes(data[: len(data) // 3])
    p["encrypted"] = root / "Restricted" / "secured_manual.pdf"
    w = _Writer()
    w.new_page()
    w.text("Confidential HP-200 overhaul instructions")
    w.finish(p["encrypted"], footer_total=False,
             encryption={"encryption": pymupdf.PDF_ENCRYPT_AES_256, "user_pw": "secret", "owner_pw": "owner"})
    return p


if __name__ == "__main__":  # pragma: no cover
    import sys
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "sample_sources")
    for k, v in make_corpus(out).items():
        print(f"{k:12s} {v}")
