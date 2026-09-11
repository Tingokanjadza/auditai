# Database schema

Twelve tables, defined in `app/database/models.py` and created by `app/database/base.py::init_db()`.
The column tables below were generated from the live SQLAlchemy metadata compiled for the SQLite
dialect, so they are the schema as it exists, not as it was intended.

Default backend: **SQLite** at `data/audit.db`. The schema is written to be portable —
see [SQLite → PostgreSQL](#sqlite--postgresql).

**Contents**

- [Entity-relationship diagram](#entity-relationship-diagram)
- [Conventions that apply to every table](#conventions-that-apply-to-every-table)
- [The AI record vs the human record](#the-ai-record-vs-the-human-record)
- [Table reference](#table-reference)
- [JSON columns](#json-columns)
- [SQLite → PostgreSQL](#sqlite--postgresql)
- [Known schema limitations](#known-schema-limitations)

---

## Entity-relationship diagram

```mermaid
erDiagram
    AUDIT_PROJECTS   ||--o{ PROJECT_CONTROLS     : "scopes"
    CONTROLS         ||--o{ PROJECT_CONTROLS     : "is scoped by"
    AUDIT_PROJECTS   ||--o{ EVIDENCE_FILES       : "holds"
    EVIDENCE_FILES   ||--o{ EVIDENCE_CHUNKS      : "is parsed into"
    AUDIT_PROJECTS   ||--o{ EVIDENCE_CHUNKS      : "owns (denormalised)"
    AUDIT_PROJECTS   ||--o{ ASSESSMENTS          : "contains"
    CONTROLS         ||--o{ ASSESSMENTS          : "is assessed by"
    ASSESSMENTS      ||--o{ ASSESSMENT_CITATIONS : "cites"
    EVIDENCE_CHUNKS  ||--o{ ASSESSMENT_CITATIONS : "is cited by (nullable)"
    EVIDENCE_FILES   ||--o{ ASSESSMENT_CITATIONS : "is cited by (nullable)"
    ASSESSMENTS      ||--o{ HUMAN_REVIEWS        : "is decided on by"
    EVALUATION_RUNS  ||--o{ EVALUATION_RESULTS   : "scores"
    EVALUATION_RUNS  ||--o{ ASSESSMENTS          : "tags (nullable)"
    ASSESSMENTS      ||--o| EVALUATION_RESULTS   : "is measured as (nullable)"
    AUDIT_PROJECTS   ||--o{ AUDIT_REPORTS        : "produces"

    AUDIT_PROJECTS {
        int  id PK
        str  name
        str  audit_area
        str  status
        bool is_demo "overloaded: also marks evaluation projects"
    }
    CONTROLS {
        int  id PK
        str  control_id UK "business key, e.g. CONTROL-001"
        str  inherent_risk
        int  privilege_level "1-5, feeds the risk model"
        int  data_sensitivity "1-5, feeds the risk model"
        bool is_active "retire, never delete"
    }
    PROJECT_CONTROLS {
        int id PK
        int project_id FK
        int control_id FK
    }
    EVIDENCE_FILES {
        int  id PK
        int  project_id FK
        str  filename "the auditor's name; what citations show"
        str  stored_path "content-addressed, never returned by the API"
        str  sha256 "integrity anchor"
        str  parse_status
        bool is_synthetic
    }
    EVIDENCE_CHUNKS {
        int  id PK
        int  evidence_file_id FK
        int  project_id FK
        str  text
        str  source_type
        int  page_number "nullable"
        str  sheet_name "nullable"
        json row_numbers "1-based spreadsheet rows"
        str  locator_text "the one-line citation string"
        blob embedding "float32, nullable until indexed"
    }
    ASSESSMENTS {
        int  id PK "THE AI RECORD - never edited by a human"
        int  project_id FK
        int  control_id FK
        str  experiment_mode "A_RAW_LLM | B_RAG | C_RAG_WORKFLOW"
        str  status
        str  risk_level "from app.audit.risk, not from the model"
        bool human_review_required "always true"
        json validation_report "citation verdicts + rails applied"
        str  raw_response "verbatim model output"
        int  evaluation_run_id FK "nullable; set = research, not fieldwork"
    }
    ASSESSMENT_CITATIONS {
        int   id PK
        int   assessment_id FK
        int   chunk_id FK "NULL = could not be resolved = fabricated"
        str   quoted_text
        str   verdict "VERIFIED|PARTIAL|UNVERIFIED|FABRICATED"
        float match_score
    }
    HUMAN_REVIEWS {
        int   id PK "THE HUMAN RECORD - the authoritative one"
        int   assessment_id FK
        str   decision
        str   final_status
        bool  agreed_with_ai_status "derived on write"
        bool  agreed_with_ai_risk "derived on write"
        float review_seconds
        bool  flagged_hallucination
    }
    EVALUATION_RUNS {
        int  id PK
        str  experiment_mode
        json config "everything needed to reproduce the run"
        json metrics "the numbers, with their caveats"
    }
    EVALUATION_RESULTS {
        int  id PK
        int  run_id FK
        int  assessment_id FK "nullable"
        str  dataset_id
        str  expected_status "ground truth"
        str  predicted_status
        bool status_correct
    }
    AUDIT_REPORTS {
        int id PK
        int project_id FK
        str format "markdown | html"
        str content "the rendered document"
    }
    ACTIVITY_LOG {
        int  id PK "append-only; NO foreign keys by design"
        str  entity_type
        int  entity_id
        int  project_id
        str  action
        str  actor_type "AI | HUMAN"
    }
```

`ACTIVITY_LOG` is drawn unconnected on purpose: it has **no foreign keys**, so a trail entry
survives the deletion of the thing it describes. "Evidence file 14 was deleted by X" must remain
readable after evidence file 14 is gone.

---

## Conventions that apply to every table

| Convention | Where | Why |
|---|---|---|
| **Enum values are stored as `VARCHAR`, never as a database-native ENUM** | every `status`, `risk_level`, `verdict`, `decision`, `parse_status`, `evidence_type`, `experiment_mode`, `source_type` column | Portability (SQLite has no ENUM), and — more importantly — an unexpected model output can be *stored and inspected* rather than rejected at the database layer. The vocabulary is enforced in `app/schemas/enums.py`, whose `coerce()` degrades gracefully instead of raising. |
| **Timestamps are timezone-aware UTC** | `created_at`, `updated_at`, `uploaded_at`, `started_at`, `generated_at` | `app.database.base.utcnow()` returns `datetime.now(timezone.utc)`. On PostgreSQL these become `TIMESTAMP WITH TIME ZONE`; SQLite stores the ISO string. |
| **`created_at` / `updated_at` come from `TimestampMixin`** | `audit_projects`, `controls`, `project_controls`, `evidence_files` | `updated_at` carries `onupdate=utcnow`. Tables that are *append-only records of an event* (`assessments`, `human_reviews`, `assessment_citations`, `activity_log`, `evaluation_*`) deliberately have only `created_at`: there is no legitimate update to an event that already happened. |
| **An explicit constraint naming convention** | `NAMING_CONVENTION` in `app/database/base.py` | `pk_%(table_name)s`, `fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s`, `ix_%(column_0_label)s`, `uq_…`, `ck_…`. Without it, Alembic cannot autogenerate reversible migrations for unnamed constraints — this is groundwork for the PostgreSQL path, not decoration. |
| **`ON DELETE CASCADE` from a project; `ON DELETE SET NULL` to a citation target** | see each table | Deleting an engagement must remove its evidence, assessments, reviews and reports. But deleting a *chunk* must not delete the citation that pointed at it: the surviving row with `chunk_id IS NULL` is the record that something was cited which no longer resolves. |
| **Foreign keys are actually enforced on SQLite** | `_sqlite_pragmas` event hook in `base.py` sets `PRAGMA foreign_keys=ON` on every connection (plus WAL and `synchronous=NORMAL`) | SQLite ignores FK constraints unless asked. Without the pragma the cascades above would be decorative. |
| **JSON columns are reassigned, never mutated in place** | e.g. `EvidenceFile.extra_metadata` in `app/evidence/service.py` | A plain `JSON` column is not change-tracked in place; `row.extra_metadata["k"] = v` silently does not persist. |

---

## The AI record vs the human record

This separation is the point of the schema, so it is worth stating on its own.

```
   assessments                              human_reviews
   ┌──────────────────────────────┐         ┌──────────────────────────────┐
   │ status          POTENTIAL_…  │         │ decision        MODIFIED     │
   │ risk_level      CRITICAL     │  1    n │ final_status    POTENTIAL_…  │
   │ finding         "10 of 100…" ├────────►│ final_risk_level HIGH        │
   │ human_review_…  true         │         │ final_finding   "Confirmed…" │
   │ raw_response    <verbatim>   │         │ agreed_with_ai_status  true  │
   │ validation_re…  {verdicts}   │         │ agreed_with_ai_risk    false │
   └──────────────────────────────┘         │ review_seconds  240.0        │
     written once, by the engine            │ usefulness_rating  4         │
     NEVER updated by a human action        │ flagged_hallucination false  │
                                            └──────────────────────────────┘
                                              written by the auditor
                                              the authoritative record
```

**The rule.** `app/audit/service.py` contains no code path that edits an `Assessment` in response to
a human action. A reviewer's conclusion is always a new `HumanReview` row. `tests/test_review.py`
asserts this directly (`test_the_review_never_edits_the_ai_record`).

**Why this makes agreement measurable.** Because both conclusions survive, human/AI concordance is a
*query*, not an estimate:

```sql
SELECT COUNT(*) FILTER (WHERE r.agreed_with_ai_status) * 1.0 / COUNT(*)
FROM human_reviews r
WHERE r.decision <> 'PENDING';
```

`agreed_with_ai_status` and `agreed_with_ai_risk` are derived **on write** by
`service.record_human_review()`, which pins the definition down where it can be read:

- Agreement is about the **outcome**, not the process. A `MODIFIED` or `REJECTED` decision that
  still lands on the same status counts as agreement — the auditor rejected the *reasoning*, not the
  conclusion about the control's condition.
- Because the raw `decision` is stored beside the derived flags, a **stricter** definition
  ("`ACCEPTED` only") can be recomputed later from the same rows without re-reviewing anything. That
  is why both are kept rather than one.
- Omitting `final_status` carries the AI status over (the reviewer left the conclusion alone) —
  **except** for `MORE_EVIDENCE_REQUESTED`, which defaults to `INSUFFICIENT_EVIDENCE`, because a
  reviewer asking for more evidence has not concluded and recording the AI's status as theirs would
  inflate agreement with a decision they did not make.
- A `PENDING` row records that someone opened the review. It never counts as agreement and never
  counts as a completed review.

**Status and risk agreement are separate columns** because an auditor routinely keeps the conclusion
and re-rates its severity — exactly what the worked example above shows (status agreed, risk
lowered from CRITICAL to HIGH).

**Reviews accumulate; the latest wins.** `Assessment.latest_review` is the last row by `created_at`,
and `Assessment.is_reviewed` is true only when that row's decision is not `PENDING`. Nothing is
overwritten, so a change of mind is itself part of the record.

Three further columns exist purely so the study can be written up: `review_seconds` (an *upper
bound* on a per-visit wall-clock measurement, not task time — the UI lets a reviewer decline to
record one), `usefulness_rating` (1–5, clamped on write), and `flagged_hallucination` +
`hallucination_note` (the auditor's own judgement, kept separate from the mechanical verdicts in
`assessments.validation_report`).

---

## Table reference

### `audit_projects`

**Why it exists.** An audit engagement: the unit that owns scope, evidence, assessments and reports.
Everything cascades from here, which is what makes "delete this engagement" a single, complete
operation.

| Column | SQLite type | Null | Default | Key |
|---|---|---|---|---|
| `id` | INTEGER | no | autoincrement | PK |
| `name` | VARCHAR(255) | no | — | indexed |
| `audit_area` | VARCHAR(255) | no | — | — |
| `description` | TEXT | **yes** | — | — |
| `period_start` | DATETIME | **yes** | — | — |
| `period_end` | DATETIME | **yes** | — | — |
| `auditor_name` | VARCHAR(160) | no | `''` | — |
| `status` | VARCHAR(32) | no | `'PLANNING'` | — |
| `scope_note` | TEXT | **yes** | — | — |
| `is_demo` | BOOLEAN | no | `False` | — |
| `created_at` | DATETIME | no | `utcnow()` | — |
| `updated_at` | DATETIME | no | `utcnow()` / on update `utcnow()` | — |

**Indexes:** `ix_audit_projects_name` (name)

**Notes.** `period_start`/`period_end` are nullable because an engagement is routinely created
before its period is fixed; `AuditProject.period_label` renders `"? to ?"` rather than failing.
`is_demo` is **overloaded**: it marks both the seeded demonstration project and every throwaway
project the evaluation harness creates, because it is the only "not a real engagement" flag the
service layer offers (`list_projects(include_demo=False)` hides them). This is a recorded compromise
— see [Known schema limitations](#known-schema-limitations).

---

### `controls`

**Why it exists.** A *reusable* control definition, separate from any engagement, so the same
requirement can be tested across projects and so an assessment always points at a stable definition.
The library is loaded from `data/controls/control_library.json` (14 synthetic controls) rather than
from code, so a researcher can edit or replace the control set without touching the application and
the file can be cited as a study artefact.

| Column | SQLite type | Null | Default | Key |
|---|---|---|---|---|
| `id` | INTEGER | no | autoincrement | PK |
| `control_id` | VARCHAR(64) | no | — | UNIQUE, indexed |
| `name` | VARCHAR(255) | no | — | — |
| `objective` | TEXT | no | `''` | — |
| `description` | TEXT | no | `''` | — |
| `risk_addressed` | TEXT | no | `''` | — |
| `expected_evidence` | JSON | no | `list()` | — |
| `assessment_criteria` | JSON | no | `list()` | — |
| `framework_refs` | JSON | no | `list()` | — |
| `category` | VARCHAR(120) | no | `'General'` | — |
| `control_type` | VARCHAR(64) | no | `'Preventive'` | — |
| `control_frequency` | VARCHAR(64) | no | `'Continuous'` | — |
| `inherent_risk` | VARCHAR(32) | no | `'MEDIUM'` | — |
| `privilege_level` | INTEGER | no | `3` | — |
| `data_sensitivity` | INTEGER | no | `3` | — |
| `retrieval_keywords` | JSON | no | `list()` | — |
| `is_active` | BOOLEAN | no | `True` | — |
| `source` | VARCHAR(64) | no | `'synthetic-library'` | — |
| `created_at` | DATETIME | no | `utcnow()` | — |
| `updated_at` | DATETIME | no | `utcnow()` / on update `utcnow()` | — |

**Indexes:** `ix_controls_control_id` (control_id) UNIQUE

**Notes.** `control_id` is the *business* key ("CONTROL-001") and is **immutable** —
`service.update_control()` refuses to change it, because assessments and reports cite it. Seeding
upserts on this column, which is what makes `bootstrap()` idempotent by construction rather than by
a "have we run yet?" flag. `is_active` is a **soft delete**: retiring a control keeps past
assessments resolvable. `privilege_level` and `data_sensitivity` are 1–5 ladders read directly by
`app/audit/risk.py`. `framework_refs` are **informative cross-references only** — the library's own
`disclaimer` field says they have not been reviewed against or endorsed by any published framework.

---

### `project_controls`

**Why it exists.** Scope. A control is in scope for an engagement or it is not, and that decision is
audit-relevant in its own right (`scope_note` records why).

| Column | SQLite type | Null | Default | Key |
|---|---|---|---|---|
| `id` | INTEGER | no | autoincrement | PK |
| `project_id` | INTEGER | no | — | FK → `audit_projects.id` (CASCADE), indexed |
| `control_id` | INTEGER | no | — | FK → `controls.id` (CASCADE), indexed |
| `scope_note` | TEXT | **yes** | — | — |
| `created_at` | DATETIME | no | `utcnow()` | — |
| `updated_at` | DATETIME | no | `utcnow()` / on update `utcnow()` | — |

**Indexes:** `ix_project_controls_control_id` (control_id) · `ix_project_controls_project_id` (project_id)

**Unique constraints:** `project_control` (project_id, control_id)

**Notes.** The unique constraint is what makes `scope_controls()` idempotent — scoping the same
control twice is a no-op rather than a duplicate row. Un-scoping deletes only this association row;
past assessments of that control survive, because an assessment that happened cannot be un-happened.

---

### `evidence_files`

**Why it exists.** The artefact the auditor actually handed over, and the integrity claim about it.
Everything citable descends from a row here.

| Column | SQLite type | Null | Default | Key |
|---|---|---|---|---|
| `id` | INTEGER | no | autoincrement | PK |
| `project_id` | INTEGER | no | — | FK → `audit_projects.id` (CASCADE), indexed |
| `filename` | VARCHAR(512) | no | — | indexed |
| `stored_path` | VARCHAR(1024) | no | — | — |
| `extension` | VARCHAR(16) | no | `''` | — |
| `content_type` | VARCHAR(160) | no | `'application/octet-stream'` | — |
| `size_bytes` | INTEGER | no | `0` | — |
| `sha256` | VARCHAR(64) | no | `''` | indexed |
| `evidence_type` | VARCHAR(48) | no | `'OTHER'` | — |
| `description` | TEXT | **yes** | — | — |
| `uploaded_by` | VARCHAR(160) | no | `''` | — |
| `uploaded_at` | DATETIME | no | `utcnow()` | — |
| `parse_status` | VARCHAR(32) | no | `'PENDING'` | — |
| `parse_error` | TEXT | **yes** | — | — |
| `page_count` | INTEGER | no | `0` | — |
| `row_count` | INTEGER | no | `0` | — |
| `chunk_count` | INTEGER | no | `0` | — |
| `char_count` | INTEGER | no | `0` | — |
| `extra_metadata` | JSON | no | `dict()` | — |
| `is_synthetic` | BOOLEAN | no | `False` | — |
| `created_at` | DATETIME | no | `utcnow()` | — |
| `updated_at` | DATETIME | no | `utcnow()` / on update `utcnow()` | — |

**Indexes:** `ix_evidence_files_filename` (filename) · `ix_evidence_files_project_id` (project_id) ·
`ix_evidence_files_sha256` (sha256)

**Notes.** `filename` is the auditor's original name and is what appears in every citation;
`stored_path` points at the content-addressed file on disk and is **never returned by the API or the
UI** (`service.evidence_to_dict()` drops it — a server filesystem path is not the client's business).
`sha256` is indexed so a duplicate upload can be recognised. `parse_status` is `PARSED`, `FAILED`,
`UNSUPPORTED` or `PENDING`: a file the parser cannot read is still stored and hashed, because nothing
an auditor supplied may disappear silently. `extra_metadata` carries parser extras — sheet names,
headers, dtypes, PDF metadata, `warnings`, and `indexed_chunks`. `is_synthetic` marks generator
output so research data can never be mistaken for fieldwork.

---

### `evidence_chunks`

**Why it exists.** The citable unit. This table is what evidence traceability stands on: every chunk
knows the file it came from and, depending on format, its page, sheet, row numbers, columns and
section heading. A fragment that cannot say where it came from is not evidence an auditor can rely
on — and is exactly what a model can quote without anyone being able to check it.

| Column | SQLite type | Null | Default | Key |
|---|---|---|---|---|
| `id` | INTEGER | no | autoincrement | PK |
| `evidence_file_id` | INTEGER | no | — | FK → `evidence_files.id` (CASCADE), indexed |
| `project_id` | INTEGER | no | — | FK → `audit_projects.id` (CASCADE), indexed |
| `chunk_index` | INTEGER | no | `0` | — |
| `text` | TEXT | no | `''` | — |
| `char_count` | INTEGER | no | `0` | — |
| `token_estimate` | INTEGER | no | `0` | — |
| `source_type` | VARCHAR(32) | no | `'TEXT_BLOCK'` | — |
| `filename` | VARCHAR(512) | no | `''` | — |
| `page_number` | INTEGER | **yes** | — | — |
| `sheet_name` | VARCHAR(255) | **yes** | — | — |
| `row_start` | INTEGER | **yes** | — | — |
| `row_end` | INTEGER | **yes** | — | — |
| `row_numbers` | JSON | no | `list()` | — |
| `column_names` | JSON | no | `list()` | — |
| `section` | VARCHAR(512) | **yes** | — | — |
| `paragraph_index` | INTEGER | **yes** | — | — |
| `locator_text` | VARCHAR(512) | no | `''` | — |
| `extra_metadata` | JSON | no | `dict()` | — |
| `embedding` | BLOB | **yes** | — | — |
| `embedding_dim` | INTEGER | no | `0` | — |
| `embedding_model` | VARCHAR(160) | **yes** | — | — |
| `created_at` | DATETIME | no | `utcnow()` | — |

**Indexes:** `ix_evidence_chunks_evidence_file_id` (evidence_file_id) ·
`ix_evidence_chunks_file_idx` (evidence_file_id, chunk_index) ·
`ix_evidence_chunks_project_id` (project_id)

**Notes.**

- The **locator columns are nullable by format, not by accident**: a PDF chunk has `page_number` and
  no `sheet_name`; a spreadsheet chunk has `sheet_name`, `row_start`/`row_end`/`row_numbers` and no
  page. `SourceLocator.render()` composes whichever are present.
- `row_numbers` holds **1-based spreadsheet rows as a human sees them**, header on row 1. A citation
  saying "rows 14, 22, 31" must match what opening the file at those rows shows.
- `locator_text` is the denormalised one-line citation, rendered once at ingestion so the string the
  auditor reads, the string stored on `assessment_citations`, and the string shown to the model are
  literally the same string.
- `filename` and `project_id` are **denormalised** from the parent file so citation rendering and
  project-scoped retrieval need no join. The cost is that renaming a file would require touching
  chunks; nothing in the application renames files.
- `source_type` is one of `PDF_PAGE`, `DOCX_PARAGRAPH`, `DOCX_TABLE`, `TABLE_ROWS`, `TABLE_SUMMARY`,
  `TEXT_BLOCK`. `TABLE_SUMMARY` is special everywhere downstream: retrieval forces it in alongside
  any row chunk from the same file, and the prompt renderer never truncates it.
- `embedding` is float32 bytes (`np.float32` via `encode_vector`), **nullable** — a chunk is stored
  and keyword-searchable before it is embedded, and indexing failure never fails an upload.
  `embedding_dim` and `embedding_model` are stored so `indexer._is_current()` can tell whether a
  vector was produced by the current provider at the current dimension, and so a mixed-provider
  corpus is detectable rather than silently incomparable.

---

### `assessments`

**Why it exists.** **The AI record.** One row per (project, control, run). It holds the model's
conclusion, the deterministic risk rating, the full research telemetry, the verbatim model output
and the mechanical validation result — everything a researcher needs to audit the audit.

| Column | SQLite type | Null | Default | Key |
|---|---|---|---|---|
| `id` | INTEGER | no | autoincrement | PK |
| `project_id` | INTEGER | no | — | FK → `audit_projects.id` (CASCADE), indexed |
| `control_id` | INTEGER | no | — | FK → `controls.id` (CASCADE), indexed |
| `control_ref` | VARCHAR(64) | no | `''` | — |
| `experiment_mode` | VARCHAR(32) | no | `'C_RAG_WORKFLOW'` | — |
| `status` | VARCHAR(40) | no | `'INSUFFICIENT_EVIDENCE'` | — |
| `assessment` | TEXT | no | `''` | — |
| `finding` | TEXT | no | `''` | — |
| `risk` | TEXT | no | `''` | — |
| `risk_level` | VARCHAR(32) | no | `'NOT_RATED'` | — |
| `risk_score` | FLOAT | no | `0.0` | — |
| `risk_factors` | JSON | no | `dict()` | — |
| `evidence_sufficiency` | VARCHAR(32) | no | `'NONE'` | — |
| `missing_evidence` | JSON | no | `list()` | — |
| `reasoning` | TEXT | no | `''` | — |
| `recommendation` | TEXT | no | `''` | — |
| `inferences` | JSON | no | `list()` | — |
| `human_verification_required` | JSON | no | `list()` | — |
| `confidence` | VARCHAR(32) | no | `'LOW'` | — |
| `confidence_score` | FLOAT | no | `0.0` | — |
| `human_review_required` | BOOLEAN | no | `True` | — |
| `llm_provider` | VARCHAR(64) | no | `''` | — |
| `llm_model` | VARCHAR(160) | no | `''` | — |
| `prompt_tokens` | INTEGER | no | `0` | — |
| `completion_tokens` | INTEGER | no | `0` | — |
| `latency_ms` | INTEGER | no | `0` | — |
| `llm_calls` | INTEGER | no | `0` | — |
| `retrieval_strategy` | VARCHAR(32) | no | `''` | — |
| `retrieval_top_k` | INTEGER | no | `0` | — |
| `retrieved_chunk_ids` | JSON | no | `list()` | — |
| `retrieval_queries` | JSON | no | `list()` | — |
| `validation_report` | JSON | no | `dict()` | — |
| `raw_response` | TEXT | **yes** | — | — |
| `prompt_snapshot` | TEXT | **yes** | — | — |
| `error` | TEXT | **yes** | — | — |
| `evaluation_run_id` | INTEGER | **yes** | — | FK → `evaluation_runs.id` (SET NULL), indexed |
| `created_at` | DATETIME | no | `utcnow()` | — |

**Indexes:** `ix_assessments_control_id` (control_id) · `ix_assessments_evaluation_run_id`
(evaluation_run_id) · `ix_assessments_project_control` (project_id, control_id) ·
`ix_assessments_project_id` (project_id)

**Notes.**

- **No unique constraint on (project, control).** Re-running a control is a first-class action, and
  the experiments deliberately run modes A, B and C over the same control. Every read that needs
  "the current position" collapses to the **latest assessment per control**
  (`service._latest_per_control`), and `dashboard_stats()` publishes that as its counting basis.
- `human_review_required` is written as the literal `True` by the engine, never taken from the
  model's answer. It is enforced independently in the pydantic schema, in the safety rails and here.
- `risk_level` / `risk_score` / `risk_factors` come from `app/audit/risk.py`, **not** from the model.
  The model's own suggested band is retained inside `risk_factors` as an advisory signal.
- `control_ref` is denormalised so an export or a report can name the control without a join, and so
  a row remains readable if the control row is later renamed.
- `retrieval_strategy` is `"NONE"` for mode A — the explicit *absence* of retrieval, distinguishable
  from `KEYWORD`/`VECTOR`/`HYBRID` in every downstream table — and `retrieval_top_k` is `0` there.
- `raw_response` and `prompt_snapshot` are the reproducibility pair: the exact prompt and the exact
  answer. `error` is non-NULL on a run that failed, and such a row is still persisted with
  `status = INSUFFICIENT_EVIDENCE`, because a run that silently loses a control is worse.
- `validation_report` is `ValidationReport.to_dict()` plus an `engine` block; see
  [JSON columns](#json-columns).
- `evaluation_run_id` **non-NULL means "this is a research run, not fieldwork"**. Every operational
  query filters it out (`include_evaluation=False`). `ON DELETE SET NULL` rather than CASCADE: if a
  run row is removed, the assessments remain readable rather than vanishing.
- `confidence_score` is a display-only numeric rendering of the model's own confidence *label*
  (LOW 0.33 / MEDIUM 0.66 / HIGH 1.0). It is **not** a calibrated probability and must never be
  presented as one; it exists because sorting a float column is better than sorting a string.

---

### `assessment_citations`

**Why it exists.** Citations are first-class **rows**, not a JSON blob, with a foreign key to the
exact chunk used. That is what makes a finding traceable — and what makes a *fabricated* citation
detectable, because an unresolvable pointer is stored with `chunk_id IS NULL`.

| Column | SQLite type | Null | Default | Key |
|---|---|---|---|---|
| `id` | INTEGER | no | autoincrement | PK |
| `assessment_id` | INTEGER | no | — | FK → `assessments.id` (CASCADE), indexed |
| `chunk_id` | INTEGER | **yes** | — | FK → `evidence_chunks.id` (SET NULL), indexed |
| `evidence_file_id` | INTEGER | **yes** | — | FK → `evidence_files.id` (SET NULL) |
| `order_index` | INTEGER | no | `0` | — |
| `filename` | VARCHAR(512) | no | `''` | — |
| `locator_text` | VARCHAR(512) | no | `''` | — |
| `quoted_text` | TEXT | no | `''` | — |
| `relevance` | TEXT | no | `''` | — |
| `supports` | VARCHAR(64) | no | `''` | — |
| `verdict` | VARCHAR(32) | no | `'UNVERIFIED'` | — |
| `match_score` | FLOAT | no | `0.0` | — |
| `verification_note` | TEXT | no | `''` | — |
| `created_at` | DATETIME | no | `utcnow()` | — |

**Indexes:** `ix_assessment_citations_assessment_id` (assessment_id) ·
`ix_assessment_citations_chunk_id` (chunk_id)

**Notes.** `verdict` ∈ {`VERIFIED`, `PARTIAL`, `UNVERIFIED`, `FABRICATED`} and `match_score` is the
similarity in [0, 1]; `verification_note` is the plain-language reason, written so a reviewer never
has to infer why a verdict was reached. `filename` and `locator_text` are copied from the **stored
chunk** where one resolved — the model's own typed locator is not trusted over the provenance the
system recorded. A fabricated citation is retained and flagged rather than deleted: one that quietly
disappeared would be worse than one left visible and labelled.

Two counts fall straight out of this table, which is why the hallucination rate is a query rather
than a claim:

```sql
SELECT verdict, COUNT(*) FROM assessment_citations GROUP BY verdict;
SELECT COUNT(*) FROM assessment_citations WHERE chunk_id IS NULL;
```

---

### `human_reviews`

**Why it exists.** **The human record**, and the authoritative one. See
[The AI record vs the human record](#the-ai-record-vs-the-human-record).

| Column | SQLite type | Null | Default | Key |
|---|---|---|---|---|
| `id` | INTEGER | no | autoincrement | PK |
| `assessment_id` | INTEGER | no | — | FK → `assessments.id` (CASCADE), indexed |
| `reviewer_name` | VARCHAR(160) | no | `''` | — |
| `decision` | VARCHAR(40) | no | `'PENDING'` | — |
| `final_status` | VARCHAR(40) | no | `''` | — |
| `final_risk_level` | VARCHAR(32) | no | `'NOT_RATED'` | — |
| `final_finding` | TEXT | no | `''` | — |
| `final_recommendation` | TEXT | no | `''` | — |
| `comments` | TEXT | no | `''` | — |
| `requested_evidence` | JSON | no | `list()` | — |
| `agreed_with_ai_status` | BOOLEAN | no | `False` | — |
| `agreed_with_ai_risk` | BOOLEAN | no | `False` | — |
| `review_seconds` | FLOAT | no | `0.0` | — |
| `usefulness_rating` | INTEGER | **yes** | — | — |
| `flagged_hallucination` | BOOLEAN | no | `False` | — |
| `hallucination_note` | TEXT | no | `''` | — |
| `created_at` | DATETIME | no | `utcnow()` | — |

**Indexes:** `ix_human_reviews_assessment_id` (assessment_id)

**Notes.** `decision` ∈ {`PENDING`, `ACCEPTED`, `REJECTED`, `MODIFIED`, `MORE_EVIDENCE_REQUESTED`}.
`final_status` is `''` (not NULL) for a PENDING row: nothing has been concluded.
`usefulness_rating` is the only nullable non-key column here, because "the reviewer did not rate it"
and "the reviewer rated it 0" are different facts and 0 is not on the 1–5 scale.

---

### `evaluation_runs`

**Why it exists.** One experimental condition executed over a set of datasets, with everything
needed to reproduce it and the metric set it produced. A result that cannot be reproduced is not a
result.

| Column | SQLite type | Null | Default | Key |
|---|---|---|---|---|
| `id` | INTEGER | no | autoincrement | PK |
| `name` | VARCHAR(255) | no | `''` | — |
| `experiment_mode` | VARCHAR(32) | no | `''` | — |
| `llm_provider` | VARCHAR(64) | no | `''` | — |
| `llm_model` | VARCHAR(160) | no | `''` | — |
| `retrieval_strategy` | VARCHAR(32) | no | `''` | — |
| `dataset_ids` | JSON | no | `list()` | — |
| `config` | JSON | no | `dict()` | — |
| `metrics` | JSON | no | `dict()` | — |
| `notes` | TEXT | no | `''` | — |
| `status` | VARCHAR(32) | no | `'RUNNING'` | — |
| `started_at` | DATETIME | no | `utcnow()` | — |
| `finished_at` | DATETIME | **yes** | — | — |
| `duration_seconds` | FLOAT | no | `0.0` | — |

*(No indexes beyond the primary key: the table holds tens of rows, not thousands.)*

**Notes.** `llm_provider` / `llm_model` record the provider that **actually ran**, not the one
requested — a results table naming a hosted model after a silent fallback to the mock would be a
false statement. `status` ∈ {`RUNNING`, `COMPLETED`, `COMPLETED_WITH_ERRORS`, `FAILED`}; a run that
crashed mid-way is visible rather than absent. `config` and `metrics` are described under
[JSON columns](#json-columns).

---

### `evaluation_results`

**Why it exists.** One scored prediction: what the ground truth said, what the system said, and
every measurement taken on that case. `compute_metrics()` operates on these rows (or on plain dicts
with the same keys — it imports no models and opens no session).

| Column | SQLite type | Null | Default | Key |
|---|---|---|---|---|
| `id` | INTEGER | no | autoincrement | PK |
| `run_id` | INTEGER | no | — | FK → `evaluation_runs.id` (CASCADE), indexed |
| `assessment_id` | INTEGER | **yes** | — | FK → `assessments.id` (SET NULL) |
| `dataset_id` | VARCHAR(64) | no | `''` | — |
| `dataset_name` | VARCHAR(255) | no | `''` | — |
| `control_ref` | VARCHAR(64) | no | `''` | — |
| `expected_status` | VARCHAR(40) | no | `''` | — |
| `predicted_status` | VARCHAR(40) | no | `''` | — |
| `status_correct` | BOOLEAN | no | `False` | — |
| `expected_risk` | VARCHAR(32) | no | `''` | — |
| `predicted_risk` | VARCHAR(32) | no | `''` | — |
| `expected_evidence_hits` | INTEGER | no | `0` | — |
| `expected_evidence_total` | INTEGER | no | `0` | — |
| `citation_count` | INTEGER | no | `0` | — |
| `verified_citation_count` | INTEGER | no | `0` | — |
| `fabricated_citation_count` | INTEGER | no | `0` | — |
| `unsupported_claim_count` | INTEGER | no | `0` | — |
| `hallucination_detected` | BOOLEAN | no | `False` | — |
| `declared_missing_evidence` | BOOLEAN | no | `False` | — |
| `latency_ms` | INTEGER | no | `0` | — |
| `prompt_tokens` | INTEGER | no | `0` | — |
| `completion_tokens` | INTEGER | no | `0` | — |
| `error` | TEXT | **yes** | — | — |
| `detail` | JSON | no | `dict()` | — |
| `created_at` | DATETIME | no | `utcnow()` | — |

**Indexes:** `ix_evaluation_results_run_id` (run_id)

**Notes.** `assessment_id` is nullable and `SET NULL` so cleaning up an evaluation project does not
destroy the scored result. `status_correct` is stored **and recomputed** by
`app/evaluation/metrics.py` from the labels — a stored flag that disagreed with the labels must not
silently drive a metric. A dataset whose run crashed is recorded with `error` set and
`predicted_status = ''`, scored as a wrong answer; removing it would score the harness on the subset
that happened to work. `expected_evidence_hits` / `_total` are the retrieval/traceability measure
against the dataset's declared key evidence markers.

---

### `audit_reports`

**Why it exists.** A rendered report is a deliverable, not a view. Storing the text means the
document can be re-read exactly as issued, even if the underlying assessments are later re-run.

| Column | SQLite type | Null | Default | Key |
|---|---|---|---|---|
| `id` | INTEGER | no | autoincrement | PK |
| `project_id` | INTEGER | no | — | FK → `audit_projects.id` (CASCADE), indexed |
| `title` | VARCHAR(255) | no | `''` | — |
| `format` | VARCHAR(16) | no | `'markdown'` | — |
| `content` | TEXT | no | `''` | — |
| `stored_path` | VARCHAR(1024) | **yes** | — | — |
| `generated_by` | VARCHAR(160) | no | `''` | — |
| `summary_stats` | JSON | no | `dict()` | — |
| `generated_at` | DATETIME | no | `utcnow()` | — |

**Indexes:** `ix_audit_reports_project_id` (project_id)

**Notes.** The text is stored on the row **and** written to `settings.report_dir` — deliberate
duplication, because the row is what the UI and the API serve and a report whose only copy is a file
path stops being reproducible the moment the file moves. `stored_path` is nullable
(`generate_report(write_file=False)` renders without touching disk). `summary_stats` is a superset of
the figures printed in the document, so a table can be rebuilt without re-parsing the text.

---

### `activity_log`

**Why it exists.** An append-only trail of who did what, and — critically — **whether it was a human
or the AI**. It is what supports the evidence-integrity narrative and what lets a reader of the
database reconstruct the order in which an engagement actually happened.

| Column | SQLite type | Null | Default | Key |
|---|---|---|---|---|
| `id` | INTEGER | no | autoincrement | PK |
| `entity_type` | VARCHAR(64) | no | `''` | indexed |
| `entity_id` | INTEGER | **yes** | — | indexed |
| `project_id` | INTEGER | **yes** | — | indexed |
| `action` | VARCHAR(64) | no | `''` | — |
| `actor` | VARCHAR(160) | no | `''` | — |
| `actor_type` | VARCHAR(16) | no | `'HUMAN'` | — |
| `details` | JSON | no | `dict()` | — |
| `created_at` | DATETIME | no | `utcnow()` | — |

**Indexes:** `ix_activity_log_entity_id` (entity_id) · `ix_activity_log_entity_type` (entity_type) ·
`ix_activity_log_project_id` (project_id)

**Notes.** `entity_id` and `project_id` are **plain integers with no foreign key**, on purpose: a
trail entry must survive the deletion of the row it describes, and "evidence file 14 was deleted"
would otherwise be impossible to keep. `actor_type` is `"AI"` or `"HUMAN"`, which makes AI-vs-human
provenance queryable — an assessment run is logged with `actor_type="AI"` and
`actor="mock (mock-deterministic-rules)"`, a review with `actor_type="HUMAN"` and the reviewer's
name. `action` values come from `ActivityAction`: `PROJECT_CREATED`, `PROJECT_UPDATED`,
`CONTROL_LINKED`, `CONTROL_UNLINKED`, `EVIDENCE_UPLOADED`, `EVIDENCE_PARSED`, `EVIDENCE_DELETED`,
`ASSESSMENT_RUN`, `HUMAN_REVIEW_RECORDED`, `REPORT_GENERATED`, `EVALUATION_RUN`.

Nothing enforces append-only at the database level; it is a convention held by
`service.log_activity()` being the only writer and there being no update or delete path.

---

## JSON columns

Twenty-two columns are `JSON`. On SQLite they are TEXT; on PostgreSQL they are `JSON` (see the
migration note about `JSONB` below). They fall into three groups.

**1. Lists of strings — simple, and JSON only to avoid a join table for values nobody queries by.**

| Column | Contents |
|---|---|
| `controls.expected_evidence` | The artefacts an auditor would expect, one sentence each. Read by the mock provider and by `query_builder`. |
| `controls.assessment_criteria` | The explicit, testable criteria the evidence is judged against. |
| `controls.framework_refs` | Informative cross-references only. |
| `controls.retrieval_keywords` | Seeds for query expansion. |
| `assessments.missing_evidence` | What the model says is missing — the auditor's request list. |
| `assessments.inferences` | Statements the model derived rather than read. |
| `assessments.human_verification_required` | What a human must check; the rails append to this. |
| `assessments.retrieval_queries` | The expanded queries actually issued. |
| `assessments.retrieved_chunk_ids` | The chunks supplied to the model, in rank order. |
| `human_reviews.requested_evidence` | What the reviewer asked for. |
| `evidence_chunks.row_numbers`, `.column_names` | 1-based source rows; the columns present in that chunk. |
| `evaluation_runs.dataset_ids` | Which datasets the run covered. |

**2. Structured research records.**

| Column | Shape |
|---|---|
| `assessments.validation_report` | `ValidationReport.to_dict()` — `total`, `verified`, `partial`, `fabricated`, `unverified`, `grounding_rate`, `grounding_rate_strict`, `partial_credit_rate`, `partial_citation_credit`, `threshold`, `has_hallucination`, `unsupported_claims[]`, `rails_applied[]`, `retrieved_chunk_ids[]`, `status_downgraded`, `original_status`, `citations[]`, plus two `*_definition` strings — **and** an `engine` block added by `AssessmentEngine._validation_payload` carrying `engine_version`, `prompt_version`, `mode`, per-step telemetry, `rails_enforced` and the latency decomposition. Everything needed to re-score a stored assessment under a different weighting without re-running anything. |
| `assessments.risk_factors` | `RiskAssessment.to_dict()` — the five factor values, their weighted contributions, the raw and final score, every named adjustment (`insufficient_evidence_floor`, `effective_ceiling`, `model_escalation`, `model_disagreement_recorded`), the band thresholds, the model's suggested band, and `RISK_MODEL_LABEL`. |
| `evaluation_runs.config` | Provider and model as resolved, prompt version and fingerprint, retrieval strategy and `top_k`, embedding provider and dimension, chunk settings, mock seed and hallucination rate, dataset ids, generator seed, a fingerprint of the ground truth, and the app/engine/runner versions. Verified by `tests/test_evaluation_runner.py` to contain no secret. |
| `evaluation_runs.metrics` | `compute_metrics()` output: `n`, `n_scored`, `n_errors`, `classification` (per-class and macro/weighted precision/recall/F1, accuracy), `confusion_matrix` (+ its orientation), `deficiency_detection` in both framings, `evidence`, `timing`, `definitions`, and **`caveats`** — which must be reproduced wherever the numbers are. |
| `evaluation_results.detail` | Per-case extras, including `detail["status"]["model"]`: the model's unedited answer beside the post-rails one, so the effect of the rails is recoverable from the row. |
| `audit_reports.summary_stats` | A superset of the figures printed in the document. |
| `activity_log.details` | Per-action payload (control ref, mode, status, citation counts, latency, error…). |

**3. Parser and provenance extras.** `evidence_files.extra_metadata` (sheet names, headers, dtypes,
PDF metadata, `warnings`, `indexed_chunks`) and `evidence_chunks.extra_metadata` (per-chunk parser
facts, e.g. `bm25_score` when written by retrieval).

**Consequence to accept.** None of these are queryable with a portable index. Filtering on a value
inside `validation_report` means loading rows and filtering in Python — which is exactly what
`app/evaluation/metrics.py` does, and is fine at this scale. If a future version needs to query
inside them, that is the point at which PostgreSQL `JSONB` with a GIN index earns its keep.

---

## SQLite → PostgreSQL

**Design intent:** `app/database/base.py` says moving to PostgreSQL is a change of `DATABASE_URL`
only. That is true of the *schema*; the paragraphs below say exactly what is portable, what changes,
and what has not been exercised.

### What is already portable

- **No SQLite-only column types.** The whole schema is `INTEGER`, `VARCHAR(n)`, `TEXT`, `FLOAT`,
  `BOOLEAN`, `DATETIME`, `JSON`, `BLOB`.
- **No database-native ENUMs.** Every enum is a `VARCHAR`, so there is no type to create, alter or
  migrate when a status is added.
- **Timezone-aware datetimes.** `DateTime(timezone=True)` compiles to `TIMESTAMP WITH TIME ZONE`.
- **Named constraints.** The `NAMING_CONVENTION` means every index, FK, PK and unique constraint has
  a deterministic name Alembic can target.
- **Engine tuning is already conditional.** `_engine_kwargs()` applies
  `check_same_thread=False` + `timeout=30` for SQLite, and `pool_pre_ping=True`, `pool_size=10`,
  `max_overflow=20` otherwise. The `PRAGMA` hook returns immediately when `settings.is_sqlite` is
  false.

### What the compiled DDL actually changes

| SQLite | PostgreSQL | Consequence |
|---|---|---|
| `id INTEGER … PRIMARY KEY` | `id SERIAL NOT NULL … PRIMARY KEY` | None for application code. |
| `DATETIME` | `TIMESTAMP WITH TIME ZONE` | Real timezone semantics instead of ISO strings. |
| `BLOB` (`embedding`) | `BYTEA` | `encode_vector` / `decode_vector` round-trip unchanged. |
| `JSON` (stored as TEXT) | `JSON` | See the JSONB note below. |
| `VARCHAR(n)` lengths ignored | `VARCHAR(n)` **enforced** | The one real behavioural change. |

### The length-limit trap

SQLite ignores `VARCHAR(n)`; PostgreSQL raises on overflow. The application already truncates on the
way in — `app/evidence/service.py` defines `_MAX_FILENAME=512`, `_MAX_SHEET=255`, `_MAX_SECTION=512`,
`_MAX_LOCATOR=512` with the comment *"Column widths from `app.database.models`. SQLite ignores them;
PostgreSQL will not."*, and the engine slices `filename[:512]`, `locator_text[:512]` and
`supports[:64]` when writing citations. Before switching, grep for any *new* write path that does
not truncate; a 600-character section heading from an unusual PDF would insert happily on SQLite and
fail on PostgreSQL.

### Steps

1. `pip install "psycopg[binary]>=3.1"` — **it is not installed in this environment** (it is
   commented out in `requirements.txt`).
2. Create the database and role:
   ```sql
   CREATE DATABASE itaudit;
   CREATE USER audit WITH PASSWORD '…';
   GRANT ALL PRIVILEGES ON DATABASE itaudit TO audit;
   ```
3. Point the app at it:
   ```dotenv
   DATABASE_URL=postgresql+psycopg://audit:password@localhost:5432/itaudit
   ```
4. `.venv/bin/python run.py init` — `Base.metadata.create_all()` builds the schema, then the control
   library is seeded.
5. Re-run the suite (`.venv/bin/python -m pytest tests -q`) with `DATABASE_URL` pointed at a
   **throwaway** PostgreSQL database. `tests/conftest.py` currently redirects to a temporary SQLite
   file, so this requires editing the `_ENVIRONMENT` dict in that file — do it on a branch, and
   never against a database holding anything you want to keep, because `clean_database` drops and
   recreates every table per test.

### Migrating existing data

There is no migration tool in the repository. For a prototype-sized SQLite file the honest options
are: (a) start fresh on PostgreSQL and re-ingest the evidence — the files are still on disk under
`data/uploads/`, and re-ingestion re-derives every chunk, locator and vector deterministically; or
(b) write a one-off script that reads the ORM objects from one session and writes them to another
(`pgloader` will move the tables but not the `BLOB` → `BYTEA` and JSON-as-TEXT conversions cleanly).
Option (a) is recommended: it re-proves the pipeline rather than trusting a copy.

### The Alembic path

Alembic is **not installed** and there is no `alembic/` directory or `alembic.ini` — the prototype
creates its schema with `create_all()`. The groundwork is in place, so adopting it is mechanical:

```bash
.venv/bin/python -m pip install "alembic>=1.13"
.venv/bin/alembic init alembic
```

Then, in `alembic/env.py`:

```python
from app.config import get_settings
from app.database.base import Base
from app.database import models  # noqa: F401  - registers every mapper

config.set_main_option("sqlalchemy.url", get_settings().database_url)
target_metadata = Base.metadata          # carries NAMING_CONVENTION
```

Then `alembic revision --autogenerate -m "baseline"` and `alembic upgrade head`. Two things to know:

- Autogenerate produces a **baseline** matching today's models. Stamp an existing database with
  `alembic stamp head` rather than trying to apply the baseline to it.
- **Autogenerate does not see SQLite well.** SQLite cannot `ALTER TABLE … ALTER COLUMN`, so column
  changes require Alembic's batch mode (`with op.batch_alter_table(...)`). Generate migrations
  against PostgreSQL if that is the target; a migration that only ever runs on SQLite will need
  batch operations for anything beyond adding a column.
- Once Alembic owns the schema, `init_db()`'s `create_all()` should stop being called on startup
  (`app/api/main.py::lifespan` and `app/frontend/streamlit_app.py::_bootstrap`), or the two will
  compete over which one defines the schema.

### JSONB, if you want it

`Base.type_annotation_map` maps `Dict[str, Any]` to the generic `JSON` type, which is what keeps the
models backend-neutral. To get PostgreSQL `JSONB` (binary storage, GIN-indexable) either change that
map to `postgresql.JSONB` — which makes the models PostgreSQL-only — or use
`JSON().with_variant(postgresql.JSONB, "postgresql")`, which keeps SQLite working. Only do this if
something actually needs to query *inside* a JSON column; nothing does today.

---

## Known schema limitations

1. **"Evaluation project" is not modelled.** It is expressed by overloading `AuditProject.is_demo`
   plus a name prefix (`[EVALUATION]`) and a `scope_note` tag. A dedicated
   `AuditProject.is_evaluation` column would let `dashboard_stats()` filter cleanly and would stop
   `generate_report()` printing the demo-data disclaimer over research projects.
2. **`dashboard_stats()` does not exclude evaluation projects from three fields.** With
   `project_id=None`, `evidence_files`, `evidence_chunks` and `controls_in_scope` are counted over
   all rows. Assessment-level exclusion (status, findings, risk, latency, reviews) works correctly.
3. **No `CHECK` constraints.** Nothing at the database level stops
   `status = 'BANANA'` or `privilege_level = 99`. Validation lives in the enums, the pydantic
   schemas and `service.py` (`_clamp_score`, `_enum_value`). Deliberate — a model returning an
   unexpected label should be *stored and inspected* rather than rejected — but it does mean the
   database alone is not a guarantee of the vocabulary.
4. **No `updated_at` on `assessments`.** By design (the row is an event), but it means there is no
   database-level signal if something ever did update one.
5. **Vectors are opaque bytes.** Similarity search is a full scan in Python
   (`VectorRetriever._score` decodes every stored vector). Fine at prototype scale; a real corpus
   would want `pgvector` or an external index, at which point `embedding`/`embedding_dim` become the
   migration source.
6. **`AuditReport.content` duplicates the file on disk.** Intentional (see above), but a large
   project's report is stored twice and served inline in JSON with no streaming path.
7. **No soft delete anywhere except `controls.is_active`.** Deleting a project really does cascade
   away its evidence rows, assessments, citations, reviews and reports. The stored *files* remain on
   disk unless deletion goes through `evidence.service.delete_evidence()`.

---

*Companion documents:* [`ARCHITECTURE.md`](ARCHITECTURE.md) · [`API.md`](API.md) ·
[`SETUP.md`](SETUP.md) · [`TESTING.md`](TESTING.md)
