# maintdoc — local, rule-based maintenance PDF processor

`maintdoc` turns a folder of ~300 industrial maintenance PDFs (manuals, service bulletins,
checklists, scans — typically a locally synchronised OneDrive folder) into:

* a **source register**, **page register** and **evidence register** in SQLite, in which every
  extracted text block, table, warning, caption and figure has a unique, deterministic
  **evidence ID** tied to the source file (SHA‑256), page, section, coordinates, extraction
  method and review status;
* a **conflict register** (torque, pressure, temperature, intervals, part numbers, procedures,
  safety instructions) and an **extraction error register**;
* a **draft** and an **approved** master manual (DOCX + PDF) in which *every technical
  statement carries its evidence citation*;
* verification reports, Excel registers and a processing summary.

> **Important — read this first**
>
> * Processing is **100 % local and deterministic**: no generative AI, no LLM APIs, no cloud
>   services and no network access. OCR uses a **local Tesseract** installation (which
>   internally uses trained recognition models shipped with Tesseract).
> * Rule-based automation **cannot establish semantic completeness or safety certification**.
>   All outputs — including the "approved" manual — are **drafts until qualified engineering
>   review and formal release**, which happens outside this tool.
> * The tool never invents maintenance steps, never paraphrases technical content and never
>   rewrites unsafe or ambiguous text. Reviewer wording edits are stored *next to* the immutable
>   original and must preserve every number and unit.

---

## Contents

1. [How it works](#1-how-it-works)
2. [Installation (Windows)](#2-installation-windows)
3. [Tesseract OCR setup](#3-tesseract-ocr-setup)
4. [OneDrive synchronisation](#4-onedrive-synchronisation)
5. [Configuration](#5-configuration)
6. [Usage](#6-usage)
7. [Review workflow](#7-review-workflow)
8. [Outputs](#8-outputs)
9. [Acceptance criteria and exit codes](#9-acceptance-criteria-and-exit-codes)
10. [Database schema](#10-database-schema)
11. [Performance for 300+ documents](#11-performance-for-300-documents)
12. [Testing](#12-testing)
13. [Limitations](#13-limitations)
14. [Troubleshooting](#14-troubleshooting)

---

## 1. How it works

```
 OneDrive folder (local)                                    work/maintenance.db (SQLite, WAL)
 ────────────────────────                                   ─────────────────────────────────
 *.pdf ──► inventory ──► extract ──► validate ──► review ──► generate ──► verify ──► export
           │            │            │            │          │            │          │
           │ SHA-256    │ PyMuPDF    │ YAML rules │Streamlit │ DOCX + PDF │ checks + │ Excel registers
           │ integrity  │ pdfplumber │ Pint units │ (local)  │ draft &    │ accept-  │ Processing_Summary.pdf
           │ placeholder│ Tesseract  │ RapidFuzz  │ audit    │ approved   │ ance     │ maintenance.db copy
           │ duplicates │ OpenCV     │ conflicts  │ log      │ manuals    │ criteria │
```

| Stage | What happens | Module |
|---|---|---|
| **inventory** | Recursive discovery, OneDrive placeholder detection (never hydrates files), integrity checks (header, EOF, open, encryption, page loading), streaming SHA‑256, exact duplicate files, incremental registration, document metadata (equipment, model, component, revision, date, number, title) with provenance. A changed file immediately invalidates every dependent approval. | `inventory.py`, `onedrive.py`, `analysis/classify.py` |
| **extract** | Page-wise streaming with checkpoints. Native text via PyMuPDF (headings, section hierarchy, lists, DANGER/WARNING/CAUTION/NOTE, captions, headers/footers, column-aware reading order, coordinates), tables via pdfplumber (PyMuPDF fallback), raster and vector figures (cropped images), selective Tesseract OCR with OpenCV preprocessing for scanned / broken-text pages. Every page gets an extraction status; nothing is silently discarded. | `extraction/*` |
| **validate** | Chapter classification (15 logical chapters), evidence-level applicability, numeric values and units (Pint), exact de-duplication, near-duplicate review candidates, conflict detection, approval re-validation. | `analysis/*` |
| **review** | Local Streamlit app: statements, duplicates, OCR/errors, classification, conflicts, wording edits, approvals, visual checks, immutable audit history. | `review/*` |
| **generate** | Draft and approved manuals from the *same* structured evidence (only the inclusion filter differs). | `generate/*` |
| **verify** | Sources, pages, evidence re-check against the PDFs, citation integrity, numeric identity, expected sections, tables, generation errors, audit chain, human visual checks → acceptance criteria. | `verify.py` |
| **export** | Excel registers, validation report, processing summary PDF, database snapshot. | `reporting/*` |

### Evidence IDs

```
EV-00012-P0034-007-3fa2c1
   │     │     │   └── first 6 hex of SHA-256(source SHA-256 | page | sequence | method | original text)
   │     │     └────── reading-order sequence on the page
   │     └──────────── PDF page (1-based)
   └────────────────── source number (SRC-00012)
```

IDs are deterministic: re-extracting an unchanged file with unchanged settings reproduces the
same IDs (review state is kept); any content change yields new IDs, so an approval can never
silently carry over to changed content.

### Key safety rules implemented

* **Exact duplicates** (identical normalised text — digits, signs and units are preserved by the
  normalisation) are merged only within the same section topic and equipment/model
  applicability; all citations are retained. Safety statements repeated within one source are
  never collapsed (they stay in their procedural context).
* **Near duplicates** (RapidFuzz) are *review candidates only*. Guard checks forbid a merge when
  numbers, units, warnings/signal words, negation, applicability, revision, step numbers or part
  numbers differ — even a reviewer cannot merge them.
* **Conflicts** are detected per parameter, qualifier (max/min/nominal), subject and applicability.
  Different explicit equipment models are *never* compared or merged. Revisions of the same
  document with different values are flagged; **no revision or value is chosen automatically**.
* **Approvals** are bound to a fingerprint of the text, wording, chapter, applicability, source
  SHA‑256 and every cited evidence item. Any change invalidates the approval (logged).
* Unresolved **critical conflicts block approval** of the statements involved and block
  generation of the approved manual.
* OCR-derived statements need explicit **OCR confirmation** against the page image; figures,
  complex tables, multi-column pages, OCR pages and generated manuals need a **human visual
  check**.

---

## 2. Installation (Windows)

Requirements: Windows 10/11, **Python 3.11 or newer** (python.org installer, tick *Add python.exe
to PATH*), Tesseract (next section), ~2 GB free disk for 300 documents.

### Option A — batch launcher (recommended)

1. Copy/clone this folder to a local, **non-OneDrive** location, e.g. `C:\Tools\maintdoc`.
2. Edit `config.yaml` → `paths.source_root` (your OneDrive folder, see §4).
3. Double-click `run_maintdoc.bat`. On first start it creates `.venv` and installs
   `requirements.txt`, then shows a menu. You can also pass commands:
   `run_maintdoc.bat run-all`, `run_maintdoc.bat review`, …

### Option B — manual

```bat
cd C:\Tools\maintdoc
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m maintdoc --help
```

### Offline installation (air-gapped PCs)

On a PC with internet access and the same Python version:

```bat
py -3.11 -m pip download -r requirements.txt -d wheels
```

Copy the `wheels` folder next to `run_maintdoc.bat`; the launcher then installs with
`pip install --no-index --find-links wheels` (no network). Internet is **never** needed for
processing.

Linux/macOS work the same way (`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`).

---

## 3. Tesseract OCR setup

OCR is only used for pages without a usable text layer (scans, broken font encodings, image-only
pages). Without Tesseract the tool still runs, but such pages get the page status
`ocr_unavailable` and a **critical** `OCR_UNAVAILABLE` error — they are never silently skipped.

1. Install Tesseract 5 for Windows (UB Mannheim build: <https://github.com/UB-Mannheim/tesseract/wiki>).
   Default path: `C:\Program Files\Tesseract-OCR\tesseract.exe` (detected automatically).
2. During installation select the language packs you need (e.g. *German* for `deu`).
3. If installed elsewhere, set `ocr.tesseract_cmd` in `config.yaml`.
4. Set `ocr.languages`, e.g. `"eng+deu"`. Missing language packs are reported as OCR unavailable.
5. Check: `"C:\Program Files\Tesseract-OCR\tesseract.exe" --version` and
   `run_maintdoc.bat extract --dry-run`.

Linux: `sudo apt install tesseract-ocr tesseract-ocr-eng` · macOS: `brew install tesseract`.

OCR pipeline per page: render at `ocr.dpi` (300) → grayscale → median/NL-means denoise → deskew
(text-line orientation, ±10°) → Otsu/adaptive binarisation → optional orientation detection
(OSD) → `image_to_data` with word confidences. If the mean confidence is below
`ocr.retry_below_confidence`, alternative binarisations are tried and the best result is kept.
Pages below `ocr.page_low_confidence` get `ocr_low_confidence` + an `OCR_LOW_CONFIDENCE` error;
blocks below `ocr.block_low_confidence` are flagged individually.

---

## 4. OneDrive synchronisation

1. In OneDrive, sync the SharePoint/Teams library or folder that contains the manuals.
2. Right-click the synced folder → **Always keep on this device**. Wait until all files show the
   solid green tick (downloaded).
3. Set `paths.source_root` to the local path, e.g.
   `C:/Users/<you>/OneDrive - <Company>/Maintenance/Manuals` (forward slashes are fine;
   environment variables such as `%OneDriveCommercial%/Maintenance` are expanded).
4. Keep `work/`, `output/` and `logs/` **outside** OneDrive (the defaults are next to the tool).
   A SQLite database must not be synchronised while it is in use.

**Online-only placeholders** (cloud icon) are detected from file attributes
(`FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS`, `RECALL_ON_OPEN`, `OFFLINE`; macOS dataless; zero
allocated blocks). They are **never opened** (that would trigger a download = network access);
they get the status `offline_placeholder` and an `OFFLINE_PLACEHOLDER` error with instructions.
After making them available offline, simply re-run `inventory`.

The inventory is incremental: files with unchanged size and modification time are not re-hashed
(`inventory --rehash` or `verify` re-hash everything). Renamed/moved files appear as a new source
plus a *missing* one (content is recognised by SHA‑256; approvals are conservatively invalidated).

---

## 5. Configuration

Everything is in `config.yaml` (fully commented). The most important sections:

| Section | Purpose |
|---|---|
| `paths` | source root (OneDrive), work/output/log folders, database |
| `inventory`, `onedrive` | include/exclude patterns, incremental hashing, placeholder rules |
| `metadata` | regexes for revision, date and document number; required fields (missing → error register) |
| `extraction` | worker processes, header/footer detection, heading detection, signal words, caption pattern, gibberish threshold |
| `tables`, `figures` | table detection mode and plausibility, figure detection and rendering |
| `ocr` | Tesseract path, languages, DPI, trigger thresholds, confidence thresholds, preprocessing |
| `classification` | **equipment + models (regexes), components, 15 chapters** with heading keywords, keywords, regexes, block types and weights |
| `dedupe` | exact-duplicate context, near-duplicate threshold |
| `units` | decimal separator policy, canonical units per parameter, parameter keywords |
| `conflicts` | subject similarity, tolerances, severity per conflict type, blocking severities, subject stop words and synonyms |
| `review`, `generation`, `verify`, `export`, `logging` | review rules, manual layout/fonts, expected sections, export limits |

Adapt `classification.equipment` to your fleet, e.g.

```yaml
classification:
  equipment:
    - name: "Hydraulic Press"
      keywords: ["hydraulic press"]
      models:
        - name: "HP-200"
          patterns: ['\bHP[-\s]?200\b']
```

Changing extraction/OCR/table/figure settings automatically re-extracts affected documents on the
next `extract` (the settings are fingerprinted). Changing classification or unit settings only
requires `validate`.

---

## 6. Usage

```text
python -m maintdoc [-c config.yaml] [--log-level INFO] <command> [options]
(on Windows: run_maintdoc.bat <command> [options])
```

| Command | Description |
|---|---|
| `inventory [--rehash] [--limit N] [--dry-run]` | discover, verify, hash and register PDFs |
| `extract [--workers N] [--force] [--source SRC-00001] [--limit N] [--dry-run]` | page-level extraction (resumable; unchanged files skipped) |
| `validate [--no-export] [--dry-run]` | classification, values/units, duplicates, conflicts, approval re-validation; writes the four registers |
| `review [--port 8501] [--no-browser]` | start the local review app (bound to 127.0.0.1, telemetry off) |
| `generate [--mode draft\|approved\|both] [--dry-run]` | master manuals (DOCX + PDF + manifest) |
| `verify [--no-rehash] [--no-export] [--dry-run]` | verification + acceptance criteria + all exports |
| `export` | registers, summary PDF and database snapshot |
| `run-all [--workers N] [--force] [--limit N] [--dry-run]` | inventory → extract → validate → generate → verify → export |
| `status` | counts and last verification result |
| `make-test-corpus DIR` | synthetic test PDFs (digital, scanned, duplicate, conflicting, corrupt, multi-revision) |

Typical campaign:

```bat
run_maintdoc.bat run-all --dry-run      & rem preview: files, changes, pages to process
run_maintdoc.bat run-all                & rem full processing (resumable)
run_maintdoc.bat review                 & rem human review in the browser
run_maintdoc.bat generate               & rem regenerate manuals after review
run_maintdoc.bat verify                 & rem acceptance criteria + reports
```

**Dry-run** never changes the workspace: `inventory`, `validate` and `run-all` run against a
temporary copy of the database; `extract` shows the plan; `generate` builds the manual model
without writing; `verify` reports without storing results.

**Resumability:** each page is committed in its own transaction together with a checkpoint. After
Ctrl+C, a crash or a power cut, re-run the same command — completed pages are not processed again.
A workspace lock prevents two runs from processing the same workspace concurrently (the review app
may stay open; SQLite WAL mode allows concurrent reading).

**Logging:** `logs/maintdoc.log` (rotating, DEBUG level incl. worker processes); console level via
`--log-level`. Every command is recorded in the `runs` table.

---

## 7. Review workflow

Start with `run_maintdoc.bat review` (opens <http://127.0.0.1:8501>). Enter your **reviewer name**
in the sidebar — every decision is recorded with it.

| Page | Purpose |
|---|---|
| Dashboard | counts, open conflicts/errors, audit-chain status |
| Sources | source register, metadata provenance, pages and errors per document |
| Statements | filter by chapter/status/source/text; original text, citations (incl. duplicates), values, conflicts, **approval blockers**, rendered source page with the cited region highlighted; approve / reject / needs revision / reopen; **edit wording** (numbers & units must stay identical; signal words cannot be removed); **reclassify**; **OCR verified** tick-box; bulk approval (each item still checked individually) |
| Near duplicates | side-by-side diff with guard flags; *confirm duplicate* is disabled when a guard fires |
| Conflicts | all values with sources and page images; resolution types: select authoritative evidence (others are rejected), distinct applicability, accepted variance, not a conflict — **a written engineering justification is mandatory** |
| Errors & OCR | error register with page image and extracted/OCR text; acknowledge / resolve / won't fix |
| Visual checks | mandatory checks of figures, complex tables, multi-column pages, OCR pages and generated manuals; *failed* checks demote the affected statements to *needs revision* |
| Audit history | hash-chained audit log and all review decisions (append-only, enforced by database triggers) |

A statement can only be approved when **all** blockers are cleared: evidence active and
canonical, source present and unchanged, chapter assigned, numeric identity of reviewer wording,
all cited evidence active, **no open blocking conflict**, OCR confirmed (OCR evidence), text
quality acceptable, and the relevant **visual checks passed**.

Approvals are invalidated automatically (and logged) when the source file changes or disappears,
the page is re-extracted with a different result, the wording or chapter changes, cited
duplicates change, a conflict is reopened or a new blocking conflict appears.

The review app has no user authentication; it binds to `127.0.0.1` only. Use Windows accounts
and file permissions to control access to the workspace.

---

## 8. Outputs

All in `paths.output_dir` (default `output/`):

| File | Content |
|---|---|
| `Source_Register.xlsx` | sources (status, SHA‑256, page count, metadata + provenance), version history, page register, status summary |
| `Evidence_Register.xlsx` | every evidence item (original text, wording, chapter, applicability, method, OCR confidence, SHA‑256, bbox, review status), values/units, duplicate groups, near duplicates, review decisions, visual checks |
| `Conflict_Register.xlsx` | conflicts with severity/blocking/status/resolution and one row per involved evidence item |
| `Extraction_Error_Register.xlsx` | all errors (corrupt PDFs, empty pages, OCR, tables, gibberish, metadata, unclassified statements, units, generation) + summary + code list |
| `Validation_Report.xlsx` | acceptance criteria, summary and every verification check by group |
| `Processing_Summary.pdf` | campaign overview: acceptance criteria, sources, pages, OCR, evidence, review, duplicates, conflicts, errors, visual checks, outputs, runs |
| `maintenance.db` | consistent snapshot of the SQLite database (full history) |
| `Master_Manual_DRAFT.docx/.pdf` | all non-rejected statements, labelled with review status, OCR origin and open conflicts; unclassified statements; open conflicts appendix |
| `Master_Manual_APPROVED.docx/.pdf` | only statements whose approval is still valid; refused while blocking conflicts are open (stale files are renamed `*_OUTDATED`) |
| `*_manifest.json` | machine-readable record of every rendered item (text, numbers, citations) used by `verify` |

Manual structure: title page with status banner and disclaimer → contents → document control →
chapters *Scope, Safety, Specifications, Components, Preventive maintenance, Inspections,
Procedures, Troubleshooting, Repairs, Schedules, Spare parts, Tools, Diagrams, Revision history &
references* (plus *Unclassified* in the draft) → Appendix A conflicts → Appendix B withheld
items → Appendix C citation index (evidence ID → source, file, page, section, method, review
status, full SHA‑256). Within a chapter, statements are grouped by section topic and
applicability; shared statements of several revisions appear once with all citations, while
differing statements from different revisions are shown **side by side with their source
labels** — never merged or re-ordered.

---

## 9. Acceptance criteria and exit codes

`verify` evaluates (see `Validation_Report.xlsx` → *Acceptance_Criteria*):

1. Every discovered PDF has a processing status.
2. Every page has an extraction status.
3. All approved claims have valid source citations.
4. All approved numbers are checked against the evidence (numeric identity + re-extraction).
5. Unresolved critical conflicts block approval (and the approved manual).
6. Source changes invalidate dependent approvals (files are re-hashed).
7. All errors and review decisions are logged (audit hash chain, append-only triggers).
8. Mandatory human visual checks are completed.
9. All manual statements are reviewed.
10. Formal engineering release — **always pending**: the tool cannot release documents.

Overall result: `FAIL`, `INCOMPLETE - HUMAN REVIEW PENDING` or
`PASS (content verified; NOT RELEASED)`.

Exit codes: `0` ok · `1` error · `2` verification failed · `3` verification incomplete
(human review pending) · `4` workspace locked · `130` interrupted (resumable).

---

## 10. Database schema

The complete, commented schema is in [`maintdoc/schema.sql`](maintdoc/schema.sql). Tables:

| Table | Purpose |
|---|---|
| `sources`, `source_versions` | source register and append-only content history |
| `pages`, `extraction_checkpoints` | page register (one row per page) and resumable checkpoints |
| `evidence` | evidence register (original text immutable; never deleted — superseded/excluded instead) |
| `quantities` | numeric values with original text, units, conversions and flags |
| `duplicate_groups`, `near_duplicates` | exact duplicate groups and near-duplicate review candidates |
| `conflicts`, `conflict_evidence` | conflict register |
| `extraction_errors` | error register (fingerprinted; reviewer status preserved) |
| `visual_checks` | mandatory human visual checks |
| `review_decisions`, `audit_log` | append-only decisions and hash-chained audit trail |
| `generated_outputs`, `verification_results`, `runs`, `meta` | output register, verification history, runs |

Integrity is enforced in the database: triggers reject `UPDATE`/`DELETE` on `audit_log` and
`review_decisions`, deletion of evidence and changes to an evidence item's original text or
provenance.

---

## 11. Performance for 300+ documents

* `extraction.workers` (or `--workers N`): documents are processed in parallel worker processes;
  each worker streams its document page by page. Start with *CPU cores - 1* (OCR is CPU heavy).
* Measured on a 4-core machine with a synthetic 300-document / 1,500-page corpus (300 revisions
  of the same manual - a worst case for duplicates and conflicts): complete `run-all` in about
  **80 s** (extraction ~10 s, validation ~10 s, manual generation ~40 s, verification + exports ~15 s).
  Real documents are denser and often scanned; OCR dominates at roughly 1-4 s per page at 300 DPI
  per worker. Plan for minutes to a few hours depending on the share of scanned pages.
* Reduce OCR cost with `ocr.dpi: 200` for clean scans; `ocr.mode: auto` (default) only OCRs pages
  that need it.
* `tables.mode: auto` only runs table detection on pages with ruling lines; very complex vector
  pages are skipped (logged + visual check) above `tables.max_drawings_for_detection`.
* Near-duplicate search uses length-banded, vectorised RapidFuzz `cdist`; conflict detection uses
  inverted indexes, so neither compares all pairs of statements.
* Long tables (e.g. a citation index with thousands of rows) are written in linear time in both
  DOCX and PDF.
* Excel exports stream rows (write-only mode) and split sheets above Excel's row limit.

---

## 12. Testing

```bat
run_maintdoc.bat          & rem menu option T
.venv\Scripts\python.exe -m pytest -q
```

The test suite generates a synthetic corpus (`maintdoc/testing/corpus.py`) with a digital manual,
its next revision with changed torque/interval values, a byte-identical copy, a service bulletin
for a different model, a conflicting pump sheet with gibberish text and unit errors, a two-column
guide, a scanned (image-only, rotated, noisy) checklist, a corrupt file, a truncated file and an
encrypted file. It covers inventory, placeholders, integrity, duplicates, extraction statuses,
tables, figures, reading order, OCR, crash/resume, parallel equivalence, units, classification,
de-duplication guards, conflicts, review rules, audit immutability and tamper detection,
generation, verification, source-change invalidation, exports, CLI and the review UI.
OCR tests are skipped automatically when Tesseract is not installed.

Try the tool on the synthetic corpus:

```bat
run_maintdoc.bat make-test-corpus sample_sources
rem set paths.source_root: ./sample_sources in config.yaml, then
run_maintdoc.bat run-all
```

---

## 13. Limitations

This is deterministic, heuristic document processing. Be aware of what it **cannot** do:

* **No semantic understanding.** Classification, subject matching, applicability and conflict
  detection use keywords, regexes and string similarity. They produce false positives (extra
  conflicts to dismiss) and false negatives (conflicts phrased too differently are not found).
  Absence of a detected conflict is not proof of consistency.
* **No completeness guarantee.** Verification proves that what was extracted is cited, traceable
  and numerically identical to the source — not that every relevant piece of information in the
  PDFs was recognised, classified correctly or is technically correct.
* **Layout heuristics.** Reading order (multi-column), heading levels, header/footer and caption
  detection can fail on unusual layouts; tables with merged cells or without ruling lines may be
  extracted as plain text. Such pages are flagged for mandatory visual checks, but review them.
* **OCR** errors (confusions such as 0/O, 1/l, decimal points) can change numbers. OCR evidence
  must be confirmed against the page image before approval; OCR bounding boxes are approximate.
* **Figures** are reproduced as images; their content is not interpreted.
* **Units:** ambiguous notations (decimal comma vs thousands separator, gauge vs absolute
  pressure, US vs imperial gallons, calendar months) are flagged, and such conversions are never
  used to decide consistency silently.
* The review app has no authentication or electronic signatures; it is not a validated
  (e.g. GxP/21 CFR Part 11) system. Formal release must follow your organisation's procedures.

---

## 14. Troubleshooting

| Symptom | Fix |
|---|---|
| `offline_placeholder` sources | Make the folder *Always keep on this device*, wait for sync, re-run `inventory`. |
| `OCR_UNAVAILABLE` errors | Install Tesseract / language packs, set `ocr.tesseract_cmd`, re-run `extract` (pages are re-processed because the result differs). |
| `ENCRYPTED_PDF` | Obtain an unprotected copy; password-protected PDFs are never cracked. |
| `CORRUPT_PDF` / `PDF_REPAIRED` | Replace the file; repaired files are processed but their pages need a visual check. |
| Workspace locked (exit 4) | Another run is active. If it crashed, the stale lock is removed automatically when its process no longer exists. |
| Approved manual not generated | Resolve the open critical conflicts listed in the console / `Conflict_Register.xlsx`. |
| Symbols (≥, µ, °) missing in PDF | Set `generation.pdf_font_paths` to a Unicode TrueType font (Arial/DejaVu). |
| Verification `FAIL` after editing a PDF | Expected: run `inventory` → `extract` → `validate` → review → `generate` → `verify`. |

Full diagnostics are in `logs/maintdoc.log`.
