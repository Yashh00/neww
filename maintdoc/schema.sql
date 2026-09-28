-- maintdoc SQLite schema (schema_version 1)
-- All statements are idempotent (IF NOT EXISTS). Times are ISO-8601 UTC strings.
-- Nothing extracted from a source is ever deleted: rows are marked superseded/excluded.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- One row per CLI invocation
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    command      TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL DEFAULT 'running',   -- running | completed | failed | interrupted
    dry_run      INTEGER NOT NULL DEFAULT 0,
    config_hash  TEXT,
    tool_version TEXT,
    host         TEXT,
    stats_json   TEXT
);

-- Source register: one row per discovered PDF path
CREATE TABLE IF NOT EXISTS sources (
    source_id              TEXT PRIMARY KEY,           -- SRC-00001
    rel_path               TEXT NOT NULL UNIQUE,       -- relative to source_root, forward slashes
    abs_path               TEXT NOT NULL,
    filename               TEXT NOT NULL,
    file_size              INTEGER,
    mtime                  TEXT,
    sha256                 TEXT,
    previous_sha256        TEXT,
    status                 TEXT NOT NULL,              -- constants.SourceStatus
    status_detail          TEXT,
    duplicate_of           TEXT,                       -- source_id of identical file
    page_count             INTEGER,
    pdf_version            TEXT,
    is_encrypted           INTEGER NOT NULL DEFAULT 0,
    is_repaired            INTEGER NOT NULL DEFAULT 0,
    pdf_title              TEXT,
    pdf_author             TEXT,
    pdf_subject            TEXT,
    pdf_keywords           TEXT,
    pdf_creator            TEXT,
    pdf_producer           TEXT,
    pdf_created            TEXT,
    pdf_modified           TEXT,
    equipment              TEXT,
    model                  TEXT,
    component              TEXT,
    revision               TEXT,
    doc_date               TEXT,
    doc_number             TEXT,
    doc_title              TEXT,
    metadata_origin_json   TEXT,                       -- field -> {value, origin, evidence_id}
    extraction_status      TEXT NOT NULL DEFAULT 'pending',
    extracted_sha256       TEXT,
    extraction_config_hash TEXT,
    pages_extracted        INTEGER NOT NULL DEFAULT 0,
    ocr_pages              INTEGER NOT NULL DEFAULT 0,
    problem_pages          INTEGER NOT NULL DEFAULT 0,
    first_seen_at          TEXT NOT NULL,
    last_seen_at           TEXT NOT NULL,
    last_changed_at        TEXT,
    last_run_id            TEXT,
    present                INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_sources_sha ON sources(sha256);
CREATE INDEX IF NOT EXISTS idx_sources_status ON sources(status, extraction_status);

-- History of content versions per source (append-only)
CREATE TABLE IF NOT EXISTS source_versions (
    version_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   TEXT NOT NULL REFERENCES sources(source_id),
    sha256      TEXT,
    file_size   INTEGER,
    mtime       TEXT,
    change_type TEXT NOT NULL,        -- new | modified | missing | restored
    detected_at TEXT NOT NULL,
    run_id      TEXT
);
CREATE INDEX IF NOT EXISTS idx_versions_source ON source_versions(source_id);

-- Page register: every page of every extracted source gets exactly one row
CREATE TABLE IF NOT EXISTS pages (
    source_id           TEXT NOT NULL REFERENCES sources(source_id),
    page_no             INTEGER NOT NULL,          -- 1-based
    source_sha256       TEXT NOT NULL,
    width               REAL,
    height              REAL,
    rotation            INTEGER,
    native_chars        INTEGER,
    native_quality      REAL,
    image_count         INTEGER,
    image_coverage      REAL,
    drawing_count       INTEGER,
    extraction_method   TEXT,                      -- native | ocr | native+ocr | none
    extraction_status   TEXT NOT NULL,             -- constants.PageStatus
    status_detail       TEXT,
    ocr_confidence      REAL,
    ocr_word_count      INTEGER,
    ocr_low_conf_words  INTEGER,
    ocr_preprocess      TEXT,
    table_count         INTEGER NOT NULL DEFAULT 0,
    table_status        TEXT,
    figure_count        INTEGER NOT NULL DEFAULT 0,
    heading_count       INTEGER NOT NULL DEFAULT 0,
    warning_count       INTEGER NOT NULL DEFAULT 0,
    evidence_count      INTEGER NOT NULL DEFAULT 0,
    multi_column        INTEGER NOT NULL DEFAULT 0,
    page_text           TEXT,
    text_sha256         TEXT,
    needs_visual_check  INTEGER NOT NULL DEFAULT 0,
    visual_check_reason TEXT,
    error_count         INTEGER NOT NULL DEFAULT 0,
    duration_ms         INTEGER,
    processed_at        TEXT,
    run_id              TEXT,
    PRIMARY KEY (source_id, page_no)
);

-- Resumable extraction checkpoints (page-wise)
CREATE TABLE IF NOT EXISTS extraction_checkpoints (
    source_id        TEXT PRIMARY KEY REFERENCES sources(source_id),
    source_sha256    TEXT NOT NULL,
    config_hash      TEXT,
    page_count       INTEGER,
    last_page_done   INTEGER NOT NULL DEFAULT 0,
    state_json       TEXT,                         -- section stack etc. for resume
    status           TEXT,
    updated_at       TEXT,
    run_id           TEXT
);

-- Evidence register: every extract gets a unique, immutable evidence_id
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id           TEXT PRIMARY KEY,        -- EV-00001-P0003-007-3fa2c1
    source_id             TEXT NOT NULL REFERENCES sources(source_id),
    source_sha256         TEXT NOT NULL,
    page_no               INTEGER NOT NULL,
    seq                   INTEGER NOT NULL,        -- reading order on page
    block_type            TEXT NOT NULL,           -- constants.BlockType
    text                  TEXT NOT NULL,           -- original extracted text (never edited)
    norm_text             TEXT NOT NULL,
    norm_hash             TEXT NOT NULL,
    heading_level         INTEGER,
    section_path          TEXT,
    section_heading       TEXT,
    section_evidence_id   TEXT,
    bbox_x0 REAL, bbox_y0 REAL, bbox_x1 REAL, bbox_y1 REAL,
    bbox_approx           INTEGER NOT NULL DEFAULT 0,
    extraction_method     TEXT NOT NULL,           -- constants.ExtractionMethod
    ocr_confidence        REAL,
    font_size             REAL,
    is_bold               INTEGER,
    column_index          INTEGER,
    table_json            TEXT,
    figure_path           TEXT,
    related_evidence_id   TEXT,                    -- caption <-> figure/table link
    safety_level          TEXT,
    quality_score         REAL,
    quality_flags         TEXT,
    status                TEXT NOT NULL DEFAULT 'active',   -- active | excluded | superseded
    status_reason         TEXT,
    -- classification (validate step)
    chapter               TEXT,
    chapter_score         REAL,
    chapter_rule          TEXT,
    chapter_override      TEXT,
    classification_status TEXT NOT NULL DEFAULT 'pending',  -- pending | classified | ambiguous | unclassified | manual
    equipment             TEXT,
    model                 TEXT,
    component             TEXT,
    revision              TEXT,
    applicability_origin  TEXT,
    -- de-duplication (validate step)
    dup_role              TEXT,                    -- canonical | exact_duplicate | near_duplicate_merged
    canonical_evidence_id TEXT,
    dup_group_id          TEXT,
    in_manual             INTEGER NOT NULL DEFAULT 0,
    manual_exclusion      TEXT,
    quantities_done       INTEGER NOT NULL DEFAULT 0,
    -- review
    display_text          TEXT,                    -- reviewer-approved wording (original kept in text)
    edit_reason           TEXT,
    review_status         TEXT NOT NULL DEFAULT 'unreviewed',
    review_comment        TEXT,
    reviewed_by           TEXT,
    reviewed_at           TEXT,
    ocr_verified          INTEGER NOT NULL DEFAULT 0,
    approval_fingerprint  TEXT,
    created_at            TEXT NOT NULL,
    run_id                TEXT
);
CREATE INDEX IF NOT EXISTS idx_ev_source_page ON evidence(source_id, page_no, seq);
CREATE INDEX IF NOT EXISTS idx_ev_hash ON evidence(norm_hash);
CREATE INDEX IF NOT EXISTS idx_ev_status ON evidence(status, in_manual);
CREATE INDEX IF NOT EXISTS idx_ev_chapter ON evidence(chapter);
CREATE INDEX IF NOT EXISTS idx_ev_canonical ON evidence(canonical_evidence_id);
CREATE INDEX IF NOT EXISTS idx_ev_review ON evidence(review_status);

-- Numeric values with units extracted from evidence text
CREATE TABLE IF NOT EXISTS quantities (
    quantity_id        TEXT PRIMARY KEY,           -- <evidence_id>#Q<n>
    evidence_id        TEXT NOT NULL REFERENCES evidence(evidence_id),
    raw_text           TEXT NOT NULL,              -- exactly as printed
    value_text         TEXT,
    value              REAL,
    value_min          REAL,
    value_max          REAL,
    tolerance          REAL,
    unit_raw           TEXT,
    unit_norm          TEXT,                       -- pint expression
    dimensionality     TEXT,
    parameter          TEXT,                       -- torque | pressure | temperature | interval | ...
    interval_basis     TEXT,                       -- operating_hours | calendar | ambiguous
    qualifier          TEXT,                       -- max | min | approx | nominal
    subject            TEXT,
    canonical_unit     TEXT,
    canonical_value    REAL,
    canonical_min      REAL,
    canonical_max      REAL,
    conversion_status  TEXT,                       -- ok | ok_with_assumption | unsafe | no_unit | not_convertible
    flags              TEXT,                       -- JSON list
    alternate_of       TEXT,                       -- quantity_id this is a dual-unit restatement of
    char_start         INTEGER,
    char_end           INTEGER
);
CREATE INDEX IF NOT EXISTS idx_q_evidence ON quantities(evidence_id);
CREATE INDEX IF NOT EXISTS idx_q_param ON quantities(parameter);

-- Exact duplicate groups (recomputed each validate) and near-duplicate review candidates
CREATE TABLE IF NOT EXISTS duplicate_groups (
    group_id        TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,                 -- exact
    norm_hash       TEXT,
    canonical_id    TEXT,
    member_count    INTEGER,
    context_key     TEXT,
    updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS near_duplicates (
    pair_id        TEXT PRIMARY KEY,               -- ND-<hash>
    evidence_a     TEXT NOT NULL,
    evidence_b     TEXT NOT NULL,
    score          REAL NOT NULL,
    scorer         TEXT,
    guard_flags    TEXT,                           -- JSON list of reasons merging is forbidden
    merge_allowed  INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'candidate',  -- candidate | confirmed_duplicate | not_duplicate | stale
    decided_by     TEXT,
    decided_at     TEXT,
    comment        TEXT,
    created_at     TEXT NOT NULL,
    run_id         TEXT
);
CREATE INDEX IF NOT EXISTS idx_nd_status ON near_duplicates(status);

-- Conflict register
CREATE TABLE IF NOT EXISTS conflicts (
    conflict_id      TEXT PRIMARY KEY,             -- CF-<hash>
    fingerprint      TEXT NOT NULL UNIQUE,
    conflict_type    TEXT NOT NULL,                -- torque | pressure | temperature | interval | part_number | procedure | safety | ...
    severity         TEXT NOT NULL,
    blocking         INTEGER NOT NULL DEFAULT 0,
    parameter        TEXT,
    subject          TEXT,
    context_json     TEXT,
    evidence_ids     TEXT NOT NULL,                -- JSON list
    values_json      TEXT,
    description      TEXT NOT NULL,
    revision_related INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'open',
    resolution_type  TEXT,
    resolution       TEXT,
    authoritative_ids TEXT,
    resolved_by      TEXT,
    resolved_at      TEXT,
    detection_rule   TEXT,
    first_detected_at TEXT NOT NULL,
    last_detected_at TEXT NOT NULL,
    run_id           TEXT
);
CREATE INDEX IF NOT EXISTS idx_conf_status ON conflicts(status, severity);

-- Link table for fast lookup evidence -> conflicts
CREATE TABLE IF NOT EXISTS conflict_evidence (
    conflict_id TEXT NOT NULL REFERENCES conflicts(conflict_id),
    evidence_id TEXT NOT NULL,
    PRIMARY KEY (conflict_id, evidence_id)
);
CREATE INDEX IF NOT EXISTS idx_ce_evidence ON conflict_evidence(evidence_id);

-- Extraction / validation error register
CREATE TABLE IF NOT EXISTS extraction_errors (
    error_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint   TEXT NOT NULL UNIQUE,
    source_id     TEXT,
    source_sha256 TEXT,
    page_no       INTEGER,
    evidence_id   TEXT,
    stage         TEXT NOT NULL,                   -- inventory | integrity | text | table | ocr | quality | metadata | classification | units | generation | verification
    error_code    TEXT NOT NULL,
    severity      TEXT NOT NULL,
    message       TEXT NOT NULL,
    details_json  TEXT,
    status        TEXT NOT NULL DEFAULT 'open',    -- open | acknowledged | resolved | wont_fix | superseded
    status_by     TEXT,
    status_at     TEXT,
    status_comment TEXT,
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    run_id        TEXT
);
CREATE INDEX IF NOT EXISTS idx_err_source ON extraction_errors(source_id, page_no);
CREATE INDEX IF NOT EXISTS idx_err_code ON extraction_errors(error_code, status);

-- Mandatory human visual checks (complex layouts, figures, OCR pages, generated output)
CREATE TABLE IF NOT EXISTS visual_checks (
    check_id      TEXT PRIMARY KEY,
    target_type   TEXT NOT NULL,                   -- source_page | generated_output
    source_id     TEXT,
    source_sha256 TEXT,
    page_no       INTEGER,
    evidence_id   TEXT,
    output_path   TEXT,
    output_sha256 TEXT,
    kind          TEXT NOT NULL,                   -- figure | complex_table | multi_column | ocr_page | table_failure | graphic_only | generated_manual
    reason        TEXT,
    status        TEXT NOT NULL DEFAULT 'pending', -- pending | passed | failed | invalidated
    reviewer      TEXT,
    reviewed_at   TEXT,
    comment       TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vc_status ON visual_checks(status);

-- Review decisions (append-only; enforced by triggers)
CREATE TABLE IF NOT EXISTS review_decisions (
    decision_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type   TEXT NOT NULL,                   -- evidence | near_duplicate | conflict | error | visual_check | source
    entity_id     TEXT NOT NULL,
    decision      TEXT NOT NULL,                   -- approve | reject | needs_revision | edit_wording | reclassify | invalidate | resolve | ...
    reviewer      TEXT NOT NULL,
    comment       TEXT,
    fingerprint   TEXT,
    details_json  TEXT,
    created_at    TEXT NOT NULL,
    audit_id      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_rd_entity ON review_decisions(entity_type, entity_id);

-- Immutable audit history with hash chain (append-only; enforced by triggers)
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    entity_type TEXT,
    entity_id   TEXT,
    before_json TEXT,
    after_json  TEXT,
    reason      TEXT,
    run_id      TEXT,
    prev_hash   TEXT NOT NULL,
    entry_hash  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_type, entity_id);

CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS review_decisions_no_update BEFORE UPDATE ON review_decisions
BEGIN SELECT RAISE(ABORT, 'review_decisions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS review_decisions_no_delete BEFORE DELETE ON review_decisions
BEGIN SELECT RAISE(ABORT, 'review_decisions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS source_versions_no_delete BEFORE DELETE ON source_versions
BEGIN SELECT RAISE(ABORT, 'source_versions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS evidence_no_delete BEFORE DELETE ON evidence
BEGIN SELECT RAISE(ABORT, 'evidence rows are never deleted; mark superseded instead'); END;
-- The original extracted text of an evidence row is immutable
CREATE TRIGGER IF NOT EXISTS evidence_text_immutable BEFORE UPDATE OF text, source_id, source_sha256, page_no, extraction_method ON evidence
WHEN OLD.text IS NOT NEW.text OR OLD.source_id IS NOT NEW.source_id OR OLD.source_sha256 IS NOT NEW.source_sha256
     OR OLD.page_no IS NOT NEW.page_no OR OLD.extraction_method IS NOT NEW.extraction_method
BEGIN SELECT RAISE(ABORT, 'evidence original text and provenance are immutable'); END;

-- Generated documents (manuals, reports)
CREATE TABLE IF NOT EXISTS generated_outputs (
    output_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,                   -- manual_docx | manual_pdf | manifest | register_xlsx | summary_pdf | database
    mode          TEXT,                            -- draft | approved
    path          TEXT NOT NULL,
    sha256        TEXT,
    item_count    INTEGER,
    status        TEXT NOT NULL,                   -- generated | blocked | failed
    detail        TEXT,
    created_at    TEXT NOT NULL,
    run_id        TEXT
);

-- Verification results (one row per check per run)
CREATE TABLE IF NOT EXISTS verification_results (
    result_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    check_group TEXT NOT NULL,
    check_name  TEXT NOT NULL,
    target      TEXT,
    status      TEXT NOT NULL,                     -- pass | fail | warn | pending_human | skipped
    detail      TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vr_run ON verification_results(run_id);
