# HTTP API reference

FastAPI application: `app/api/main.py`. Interactive documentation is served by the app itself at
**`/docs`** (Swagger UI), **`/redoc`**, and **`/openapi.json`**.

> ## ⚠️ There is no authentication
>
> **This research prototype has no login, no authorisation and no rate limiting. Every endpoint —
> including evidence upload, evidence download through a report, and assessment execution — is open
> to anyone who can reach the port.**
>
> The API holds audit evidence. Bind it to `127.0.0.1` (the default `API_HOST`), use **synthetic
> data only**, and never expose it on a shared or public network. `GET /health` reports
> `"authentication": "none"`, `GET /` repeats the warning, and `GET /api/v1/settings` carries it in
> `security_note` — three places, because a reader who only opens `/docs` must not be left to
> discover it.
>
> Adding authentication is out of scope for the prototype and is listed as future work. If this
> system were ever deployed beyond a researcher's laptop it would need, at minimum: authentication,
> per-project authorisation, rate limiting on the assessment and evaluation endpoints (both are
> synchronous and expensive), TLS, and an audit log of API access distinct from the application's
> own `activity_log`.

**Contents**

- [Conventions](#conventions)
- [Errors](#errors)
- [Endpoint index](#endpoint-index)
- [System](#system)
- [Projects](#projects)
- [Controls](#controls)
- [Evidence](#evidence)
- [Assessments](#assessments)
- [Reviews](#reviews)
- [Dashboard](#dashboard)
- [Reports](#reports)
- [Evaluation](#evaluation)
- [Settings](#settings)
- [Known API limitations](#known-api-limitations)

---

## Conventions

| | |
|---|---|
| **Base URL** | `http://127.0.0.1:8000` by default (`API_HOST`, `API_PORT`). Every business endpoint is under **`/api/v1`**; `/`, `/health` and the docs routes are not. |
| **Content type** | `application/json` in and out, except `POST /api/v1/evidence/upload` (`multipart/form-data`) and `GET /api/v1/reports/{id}/download` (`text/markdown` or `text/html`). |
| **Timestamps** | ISO-8601 strings, UTC. |
| **Repeatable filters** | Several list endpoints take a repeatable query parameter: `?status=FIELDWORK&status=REVIEW`. |
| **Enum values** | Exactly as defined in `app/schemas/enums.py`, upper case with underscores. An invalid value is a 422 from FastAPI's own validation. |
| **Long-running work** | Assessments and evaluation runs are **synchronous** — the request blocks until finished. Against the mock provider an assessment takes ~0.1–0.2 s; against a hosted model, seconds to minutes. `settings.api_request_timeout` (default 300 s) is the timeout the project assumes. |
| **CORS** | `allow_origin_regex=^https?://(localhost\|127\.0\.0\.1\|\[::1\])(:\d+)?$`, credentials off. Loopback origins only, so a page on the internet cannot drive the API through a researcher's browser — which is mitigation, not security, given there is no auth. |
| **Provenance** | Every assessment payload carries `"source": "AI-generated"` and `"requires_human_review": true`; every review payload carries `"source": "Human auditor"`. |
| **Startup** | The app creates the schema and seeds the control library on startup (idempotent). A seeding failure is logged and swallowed so the process still serves. |

### Curl examples in this document

All examples below were executed against a running instance with `LLM_PROVIDER=mock` and a
temporary database. Responses are real output, abbreviated where marked. Set:

```bash
export API=http://127.0.0.1:8000
```

---

## Errors

**Every** 4xx and 5xx response — including an unhandled exception and an unknown route — uses one
envelope, registered once in `app/api/main.py`:

```json
{
  "error": {
    "type": "NotFoundError",
    "message": "Audit project 987654 not found.",
    "status_code": 404,
    "path": "/api/v1/projects/987654",
    "detail": null
  }
}
```

| Status | `error.type` | Raised by |
|---|---|---|
| `400` | `InvalidInputError` | `app.audit.service` rejected a value (blank name, unknown enum, unknown update field). |
| `400` | `EvidenceIngestError` | The upload could not be accepted at all. **Nothing was stored**, so the client can safely retry. |
| `404` | `NotFoundError` | A referenced project, control, assessment, evidence file, chunk, report or run does not exist. |
| `404` | `HTTPException` | Unknown route; also an unknown evaluation run id, because that router raises `HTTPException` directly rather than going through the service layer. |
| `413` | `EvidenceTooLargeError` | Upload exceeds `MAX_UPLOAD_MB` (default 50). The body is read in 1 MB blocks and rejected as soon as the limit is passed. |
| `422` | `RequestValidationError` | Pydantic rejected the request shape. `detail` carries FastAPI's per-field error list. |
| `500` | *(actual exception class)* | Unhandled. The message is deliberately generic — an exception string can carry a filesystem path — and the traceback goes to the server log. **Note:** two caller errors land here rather than on 400, because the evaluation runner raises exception types the app does not map. See [`POST /api/v1/evaluation/run`](#post-apiv1evaluationrun--201). |

Verified examples:

```bash
$ curl -s $API/api/v1/projects/987654
{"error":{"type":"NotFoundError","message":"Audit project 987654 not found.","status_code":404,"path":"/api/v1/projects/987654","detail":null}}

$ curl -s -X POST $API/api/v1/projects -H 'Content-Type: application/json' -d '{"name":"  ","audit_area":"IAM"}'
{"error":{"type":"InvalidInputError","message":"Project name is required.","status_code":400,"path":"/api/v1/projects","detail":null}}

$ curl -s -X POST $API/api/v1/projects -H 'Content-Type: application/json' -d '{"audit_area":"IAM"}'
{"error":{"type":"RequestValidationError","message":"Request validation failed.","status_code":422,
 "path":"/api/v1/projects","detail":[{"type":"missing","loc":["body","name"],"msg":"Field required",
 "input":{"audit_area":"IAM"}}]}}

$ curl -s $API/api/v1/nope
{"error":{"type":"HTTPException","message":"Not Found","status_code":404,"path":"/api/v1/nope","detail":null}}
```

**Testing note.** Starlette re-raises after the global `Exception` handler runs, so a test using
`fastapi.testclient.TestClient` with the default `raise_server_exceptions=True` sees the exception
rather than the 500 body. Use `TestClient(app, raise_server_exceptions=False)` to assert on the
envelope.

---

## Endpoint index

Generated by walking `app.api.main.app.routes` — 47 business endpoints plus 4 documentation routes.

| Method | Path | Success | Purpose |
|---|---|---|---|
| GET | `/` | 200 | Service metadata and the security warning |
| GET | `/health` | 200 | Liveness, database reachability, provider status |
| GET | `/docs` · `/redoc` · `/openapi.json` · `/docs/oauth2-redirect` | 200 | Interactive documentation |
| POST | `/api/v1/projects` | **201** | Create an audit project |
| GET | `/api/v1/projects` | 200 | List audit projects |
| GET | `/api/v1/projects/{project_id}` | 200 | Get one project |
| PATCH | `/api/v1/projects/{project_id}` | 200 | Update a project |
| DELETE | `/api/v1/projects/{project_id}` | 200 | Delete a project (cascades) |
| GET | `/api/v1/projects/{project_id}/controls` | 200 | List the controls in scope |
| POST | `/api/v1/projects/{project_id}/controls` | 200 | Put library controls in scope |
| DELETE | `/api/v1/projects/{project_id}/controls/{control_ref}` | 200 | Remove a control from scope |
| GET | `/api/v1/controls` | 200 | List control-library entries |
| POST | `/api/v1/controls` | **201** | Add a control to the library |
| GET | `/api/v1/controls/categories` | 200 | Distinct categories |
| GET | `/api/v1/controls/{control_ref}` | 200 | Get one control |
| PATCH | `/api/v1/controls/{control_ref}` | 200 | Update a control |
| POST | `/api/v1/controls/{control_ref}/deactivate` | 200 | Retire a control (soft delete) |
| POST | `/api/v1/controls/{control_ref}/activate` | 200 | Return a retired control |
| POST | `/api/v1/evidence/upload` | **201** | Upload, hash, parse, chunk and index one file |
| GET | `/api/v1/evidence` | 200 | List evidence files |
| GET | `/api/v1/evidence/stats` | 200 | Evidence totals for one project |
| GET | `/api/v1/evidence/chunks/{chunk_id}` | 200 | Read a cited chunk with its neighbours |
| GET | `/api/v1/evidence/{evidence_file_id}` | 200 | One evidence file with chunk-level facts |
| GET | `/api/v1/evidence/{evidence_file_id}/chunks` | 200 | The chunks parsed out of one file |
| DELETE | `/api/v1/evidence/{evidence_file_id}` | 200 | Delete a file, its chunks and its bytes |
| POST | `/api/v1/assessments/run` | 200 | **Assess one control (synchronous)** |
| GET | `/api/v1/assessments` | 200 | List assessments |
| GET | `/api/v1/assessments/findings` | 200 | Latest assessments concluding on a deficiency |
| GET | `/api/v1/assessments/{assessment_id}` | 200 | One assessment with its citations |
| POST | `/api/v1/reviews` | **201** | Record a human review |
| GET | `/api/v1/reviews` | 200 | List human reviews |
| GET | `/api/v1/reviews/pending` | 200 | The review queue |
| GET | `/api/v1/dashboard/stats` | 200 | Headline figures + their definitions |
| GET | `/api/v1/dashboard/status-breakdown` | 200 | Status counts |
| GET | `/api/v1/dashboard/risk-breakdown` | 200 | Risk-level counts |
| GET | `/api/v1/dashboard/activity` | 200 | Recent activity trail |
| POST | `/api/v1/reports/generate` | **201** | Generate an audit report (synchronous) |
| GET | `/api/v1/reports` | 200 | List generated reports |
| GET | `/api/v1/reports/{report_id}` | 200 | One report including its rendered text |
| GET | `/api/v1/reports/{report_id}/download` | 200 | Download as an attachment |
| GET | `/api/v1/evaluation/datasets` | 200 | The six synthetic datasets |
| POST | `/api/v1/evaluation/run` | **201** | Run an experiment (synchronous) |
| GET | `/api/v1/evaluation/runs` | 200 | List experiment runs |
| GET | `/api/v1/evaluation/runs/{run_id}` | 200 | One run with its scored results |
| GET | `/api/v1/evaluation/compare` | 200 | Compare runs side by side |
| GET | `/api/v1/settings` | 200 | Configuration, secrets masked |
| GET | `/api/v1/settings/providers` | 200 | LLM provider status |
| POST | `/api/v1/settings` | 200 | Propose a change — **always refused** |

---

## System

### `GET /`

Service metadata and where the documentation lives.

```bash
curl -s $API/
```

```json
{"name":"LLM-Assisted IT Audit Risk and Control Assessment System","version":"0.1.0",
 "api_prefix":"/api/v1","docs":"/docs","openapi":"/openapi.json","health":"/health",
 "authentication":"none",
 "warning":"No authentication. This research prototype has no login, no authorisation and no rate limiting. Every endpoint is open to anyone who can reach the port. Bind it to localhost, use synthetic data only, and do not deploy it on a shared or public network."}
```

### `GET /health`

Liveness, database reachability and which provider would serve the next assessment. Provider status
is derived **from configuration only — no network call is made**, so this is safe to poll and cannot
leak the existence of a key to a remote endpoint. The API key appears only as a masked fingerprint.

**Response:** `HealthResponse` — `status` (`"ok"` / `"degraded"`), `app_name`, `app_version`,
`environment`, `database_ok`, `database_error`, `authentication`, `provider` (`ProviderHealth`),
`checked_at`.

```bash
curl -s $API/health
```

```jsonc
{
  "status": "ok",
  "app_name": "LLM-Assisted IT Audit Risk and Control Assessment System",
  "app_version": "0.1.0",
  "environment": "development",
  "database_ok": true,
  "database_error": "",
  "authentication": "none",
  "provider": {
    "configured_provider": "mock",
    "active_provider": "mock",
    "active_model": "mock-deterministic-rules",
    "fell_back_to_mock": false,
    "fallback_reason": "",
    "available_providers": ["mock", "openai"],
    "providers": {
      "mock": {
        "provider": "mock", "model": "mock-deterministic-rules", "available": true,
        "offline": true, "rules_version": "mock-rules-1.1",
        "hallucination_rate": 0.0, "seed": 1337,
        "note": "Produced by the offline deterministic rule-based provider, not by a language model. Results demonstrate the pipeline (retrieval, citation validation, workflow), not LLM capability."
      },
      "openai": { "available": false, "api_key_configured": false, "sdk_importable": true, "...": "..." }
    },
    "settings": { "llm_provider": "mock", "llm_api_key": "(not set)", "...": "..." }
  },
  "checked_at": "2026-09-11T00:02:24.685054+00:00"
}
```

**`fell_back_to_mock: true` is the field that matters for research integrity.** It means a real
provider was configured but could not be used, and the mock answered instead. Check it before
recording any result.

`database_ok: false` returns `status: "degraded"` with the error in `database_error` — still HTTP
200, because the endpoint succeeded in reporting a problem.

---

## Projects

### `POST /api/v1/projects` → **201**

**Body** `ProjectCreateRequest`: `name`\*, `audit_area`\*, `description`, `period_start`,
`period_end` (date or datetime string), `auditor_name`, `status` (`ProjectStatus`), `scope_note`,
`is_demo`, `control_refs[]` (scoped immediately), `actor` (recorded in the activity trail).

**Response** `ProjectResponse`: the row plus `period_label`, `controls_in_scope`, `evidence_files`,
`assessments`, `control_refs[]`.

```bash
curl -s -X POST $API/api/v1/projects \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "Q2 Privileged Access Review",
    "audit_area": "Identity and Access Management",
    "description": "Synthetic engagement for documentation.",
    "period_start": "2024-04-01",
    "period_end": "2024-06-30",
    "auditor_name": "Research Auditor",
    "control_refs": ["CONTROL-001"],
    "actor": "Research Auditor"
  }'
```

```json
{
  "id": 2,
  "name": "Q2 Privileged Access Review",
  "audit_area": "Identity and Access Management",
  "description": "Synthetic engagement for documentation.",
  "period_start": "2024-04-01T00:00:00",
  "period_end": "2024-06-30T00:00:00",
  "auditor_name": "Research Auditor",
  "status": "PLANNING",
  "scope_note": "",
  "is_demo": false,
  "created_at": "2026-09-10T23:45:27.745241+00:00",
  "updated_at": "2026-09-10T23:45:27.745247+00:00",
  "period_label": "2024-04-01 to 2024-06-30",
  "controls_in_scope": 1,
  "evidence_files": 0,
  "assessments": 0,
  "control_refs": ["CONTROL-001"]
}
```

**Errors:** 400 if `name` or `audit_area` is blank; **404** if a `control_ref` does not exist
(`{"type":"NotFoundError","message":"Control 'CONTROL-NOPE' not found."}` — the project row is
created first and then scoping fails, so a 404 here can leave the project behind); 422 on a missing
field or an unknown `status`.

### `GET /api/v1/projects`

**Query:** `status` (repeatable `ProjectStatus`), `include_demo` (default `true` — `false` hides the
seeded demo project **and** every evaluation-harness project), `search` (substring on name or audit
area). Newest first.

```bash
curl -s "$API/api/v1/projects?status=PLANNING&include_demo=false"
```

### `GET /api/v1/projects/{project_id}` · `PATCH` · `DELETE`

`PATCH` body `ProjectUpdateRequest` — only `name`, `audit_area`, `description`, `period_start`,
`period_end`, `auditor_name`, `status`, `scope_note` (plus `actor`). Only fields actually present in
the body are applied (`model_dump(exclude_unset=True)`).

⚠️ **Over HTTP, an unknown field is silently ignored** (verified: `PATCH` with `{"nonsense": "x"}`
returns **200** and changes nothing). Every request model sets `extra="ignore"` so a newer client
stays compatible with an older server. The *service layer* underneath takes the opposite view —
`service.update_project()` raises `InvalidInputError` for a field outside its whitelist, on the
principle that quietly dropping a field the caller asked for is the worse failure — but the API
schema drops the key before it ever gets there. The two layers disagree; the in-process Streamlit
facade sees the strict behaviour and an HTTP client sees the lenient one.

```bash
curl -s -X PATCH $API/api/v1/projects/2 \
  -H 'Content-Type: application/json' \
  -d '{"status": "FIELDWORK", "actor": "Research Auditor"}'
# → {"id":2, ..., "status":"FIELDWORK", "updated_at":"2026-09-10T23:56:49.502753+00:00", ...}

curl -s -X DELETE $API/api/v1/projects/5
# → {"deleted":true,"id":5,"message":"Project and its dependent records were deleted."}
```

**`DELETE` cascades** to evidence rows, chunks, assessments, citations, reviews and reports. Stored
bytes under `data/uploads/` are **not** removed by this route — use
`DELETE /api/v1/evidence/{id}` for that.

### `GET|POST /api/v1/projects/{project_id}/controls`, `DELETE …/{control_ref}`

Scope management. All three return `ScopeResponse`: `project_id`, `changed`,
`scoped_control_refs[]`, `controls_in_scope`, `message`. `POST` body: `control_refs[]`\*,
`scope_note`, `actor`. Scoping is **idempotent** — re-scoping the same control sets
`changed: false`.

```bash
curl -s -X POST $API/api/v1/projects/2/controls \
  -H 'Content-Type: application/json' \
  -d '{"control_refs": ["CONTROL-002"], "scope_note": "Added for documentation.", "actor": "Research Auditor"}'
# → {"project_id":2,"changed":true,"scoped_control_refs":["CONTROL-001","CONTROL-002"],
#     "controls_in_scope":2,"message":"Scoped 1 control(s)."}

curl -s -X DELETE "$API/api/v1/projects/2/controls/CONTROL-002?actor=Research%20Auditor"
# → {"project_id":2,"changed":true,"scoped_control_refs":["CONTROL-001"],
#     "controls_in_scope":1,"message":"Control removed from scope."}
```

Un-scoping removes the association only. **Past assessments of that control survive** — an
assessment that happened cannot be un-happened.

---

## Controls

`{control_ref}` accepts either the business reference (`CONTROL-001`) or the numeric primary key.

### `GET /api/v1/controls`

**Query:** `category` (repeatable), `active_only` (default `true`), `search` (reference, name or
objective), `project_id` (narrow to a project's scope).

```bash
curl -s "$API/api/v1/controls?search=password"
# → CONTROL-001 Multi-Factor Authentication for Privileged Accounts | Logical Access
#   CONTROL-005 Password Policy Enforcement                        | Logical Access
```

**Response** `ControlResponse[]`: the full definition including `expected_evidence[]`,
`assessment_criteria[]`, `framework_refs[]`, `inherent_risk`, `privilege_level`, `data_sensitivity`,
`retrieval_keywords[]`, `is_active`, `source`, `scoped_project_count`.

> `framework_refs` are **informative cross-references only**. They have not been reviewed against or
> endorsed by any published framework, and they are never a basis for a compliance statement.

### `GET /api/v1/controls/categories`

```bash
curl -s $API/api/v1/controls/categories
# ["Backup and Recovery","Change Management","Data Protection","Logging and Monitoring",
#  "Logical Access","Third-Party Access","Vulnerability Management"]
```

### `GET /api/v1/controls/{control_ref}`

```bash
curl -s $API/api/v1/controls/CONTROL-001
```

```jsonc
{
  "id": 1,
  "control_id": "CONTROL-001",
  "name": "Multi-Factor Authentication for Privileged Accounts",
  "objective": "Ensure that every account holding administrative or otherwise privileged access to in-scope systems can only authenticate with a second factor, so that a stolen or guessed password alone cannot grant privileged access.",
  "category": "Logical Access",
  "inherent_risk": "HIGH",
  "privilege_level": 5,
  "data_sensitivity": 4,
  "is_active": true,
  "scoped_project_count": 2
  // …expected_evidence[], assessment_criteria[], framework_refs[], retrieval_keywords[], timestamps
}
```

### `POST /api/v1/controls` → **201**

**Body** `ControlCreateRequest`: `control_id`\*, `name`\*, plus every optional field above.
`privilege_level` and `data_sensitivity` are clamped to 1–5. A duplicate `control_id` is a 400.

```bash
curl -s -X POST $API/api/v1/controls \
  -H 'Content-Type: application/json' \
  -d '{
    "control_id": "CONTROL-900",
    "name": "Documentation example control",
    "objective": "Demonstrate the control-library endpoints.",
    "expected_evidence": ["A configuration export showing the setting"],
    "assessment_criteria": ["The setting is enabled for every in-scope system"],
    "category": "Logical Access",
    "inherent_risk": "MEDIUM",
    "privilege_level": 3,
    "data_sensitivity": 3,
    "retrieval_keywords": ["example", "documentation"],
    "actor": "Research Auditor"
  }'
# → {"id":15,"control_id":"CONTROL-900", ..., "is_active":true,"source":"user-defined","scoped_project_count":0}
```

### `PATCH /api/v1/controls/{control_ref}`

Every field except `control_id`, which is **immutable** — assessments and reports cite it.

```bash
curl -s -X PATCH $API/api/v1/controls/CONTROL-900 \
  -H 'Content-Type: application/json' -d '{"category": "Change Management", "actor": "Research Auditor"}'
```

### `POST /api/v1/controls/{control_ref}/deactivate` · `/activate`

**Soft delete.** There is no `DELETE` for a control: past assessments reference the row, and
removing it would break the evidence trail.

```bash
curl -s -X POST "$API/api/v1/controls/CONTROL-900/deactivate?actor=Research%20Auditor"   # is_active → false
curl -s -X POST "$API/api/v1/controls/CONTROL-900/activate?actor=Research%20Auditor"     # is_active → true
```

---

## Evidence

### `POST /api/v1/evidence/upload` → **201**

`multipart/form-data`. Stores the bytes, hashes them (SHA-256), parses the file into
locator-bearing chunks and embeds those chunks — **synchronously** — then returns the parsed
metadata.

| Form field | Type | Notes |
|---|---|---|
| `project_id`\* | int ≥ 1 | |
| `file`\* | file | |
| `evidence_type` | `EvidenceType` | default `OTHER` |
| `description` | str | free-text provenance note |
| `uploaded_by` | str | recorded in the activity trail |
| `is_synthetic` | bool | mark generated research data so it is never mistaken for fieldwork |

Supported extensions: `.pdf .docx .csv .xlsx .xls .txt .md .json`. **A file with any other extension
is still stored and hashed** and comes back with `parse_status: "UNSUPPORTED"` rather than being
discarded. A file that parses to no chunks comes back `FAILED` with the reason in `parse_error`.

```bash
curl -s -X POST $API/api/v1/evidence/upload \
  -F "project_id=2" \
  -F "file=@data/synthetic/dataset-001/Privileged_Accounts_MFA_Export_2024-06-30.csv" \
  -F "evidence_type=USER_LISTING" \
  -F "description=Identity provider export of privileged accounts" \
  -F "uploaded_by=Research Auditor" \
  -F "is_synthetic=true"
```

```jsonc
{
  "id": 2,
  "project_id": 2,
  "filename": "Privileged_Accounts_MFA_Export_2024-06-30.csv",
  "extension": ".csv",
  "content_type": "text/csv",
  "size_bytes": 12947,
  "size_kb": 12.6,
  "sha256": "f16e1a5eccd11f3f5c8f132998df1daadd60dea52522bcf405bec751de0129ca",
  "sha256_short": "f16e1a5eccd1",
  "evidence_type": "USER_LISTING",
  "description": null,
  "uploaded_by": "Research Auditor",
  "uploaded_at": "2026-09-10T23:45:47.776625",
  "parse_status": "PARSED",
  "parse_error": null,
  "page_count": 0,
  "row_count": 100,
  "chunk_count": 5,
  "char_count": 20360,
  "chunks_stored": 5,
  "chunks_embedded": 5,
  "chunks_by_source_type": { "TABLE_ROWS": 4, "TABLE_SUMMARY": 1 },
  "distinct_pages": 0,
  "sheet_names": [],
  "is_synthetic": true,
  "extra_metadata": {
    "parser": "pandas", "encoding": "utf-8",
    "sheets": [{"sheet": "", "header_row": 1, "data_rows": 100, "first_data_row": 2, "last_data_row": 101,
                "columns": ["Account_Name", "Account_Type", "Department", "Privilege_Level",
                            "MFA_Status", "MFA_Method", "Last_Login", "Export_Date", "Data_Origin"],
                "blank_rows_skipped": []}],
    "warnings": [], "indexed_chunks": 5
  }
}
```

`chunks_embedded` is the honest answer to *"is this file actually searchable by the vector
retriever yet?"* — indexing is best-effort and a failure is recorded in
`extra_metadata.warnings` rather than failing the upload.

**Errors:** 400 (no filename in the part; project does not exist — nothing is stored),
**413** (over `MAX_UPLOAD_MB`; the body is read in 1 MB blocks so an oversized file is rejected
early), 422 (missing form field).

### `GET /api/v1/evidence`

**Query:** `project_id`, `evidence_type` (repeatable), `parse_status` (repeatable), `search`
(filename substring). Newest first. **`stored_path` is never returned** — a server filesystem path
is not the client's business; the SHA-256 is the integrity anchor.

```bash
curl -s "$API/api/v1/evidence?project_id=2&parse_status=PARSED"
```

### `GET /api/v1/evidence/stats?project_id=2`

```json
{"evidence_files": 2, "evidence_chunks": 10, "total_bytes": 50736, "total_mb": 0.05}
```

### `GET /api/v1/evidence/{evidence_file_id}`

`EvidenceDetailResponse` — the list fields plus `chunks_stored`, `chunks_embedded`,
`chunks_by_source_type`, `distinct_pages`, `sheet_names[]`.

### `GET /api/v1/evidence/{evidence_file_id}/chunks`

**Query:** `limit` (default 50, max 500), `offset` (default 0). Ordered by `chunk_index`.

```bash
curl -s "$API/api/v1/evidence/2/chunks?limit=2&offset=1"
# chunk 7  index 1  TABLE_ROWS  4081 chars  "…csv - rows 2, 3, 4, …"
# chunk 8  index 2  TABLE_ROWS  4098 chars  "…csv - rows 27, 28, …"
```

Each `ChunkResponse` carries `chunk_id`, `chunk_index`, `text`, `char_count`, `source_type`,
`filename`, `locator_text`, `locator` (the structured `SourceLocatorModel`) and `extra_metadata`.

### `GET /api/v1/evidence/chunks/{chunk_id}`

**This is what makes a citation clickable.** Returns the chunk plus its neighbours, so a reviewer can
read a quoted fragment in context. **Query:** `window` (default 1, 0–10).

```bash
curl -s "$API/api/v1/evidence/chunks/6?window=1"
```

```jsonc
{
  "found": true,
  "chunk_id": 6,
  "evidence_file_id": 2,
  "project_id": 2,
  "filename": "Privileged_Accounts_MFA_Export_2024-06-30.csv",
  "evidence_type": "USER_LISTING",
  "chunk_index": 0,
  "source_type": "TABLE_SUMMARY",
  "locator_text": "Privileged_Accounts_MFA_Export_2024-06-30.csv - section 'Population summary' - rows 2-101 - columns: Account_Name, Account_Type, Department, Privilege_Level, MFA_Status, MFA_Method, Last_Login, Export_Date",
  "text": "TABLE SUMMARY - Privileged_Accounts_MFA_Export_2024-06-30.csv\nPopulation: 100 data row(s) x 9 column(s). Header on spreadsheet row 1; data on spreadsheet rows 2-101.\n…",
  "chunk":  { "…": "the chunk itself" },
  "before": [],
  "after":  [ { "chunk_id": 7, "…": "…" } ],
  "chunks": [ "…", "…" ],
  "context_text": "…the window joined by \\n\\n---\\n\\n…"
}
```

A missing chunk returns **404**, not `found: false`.

### `DELETE /api/v1/evidence/{evidence_file_id}`

Removes the row, its chunks **and the stored bytes**. Idempotent in effect; the deletion itself is
written to the activity trail before the row disappears.

```bash
curl -s -X DELETE $API/api/v1/evidence/7
# → {"deleted":true,"id":7,"message":"Evidence file, chunks and stored bytes were removed."}
```

---

## Assessments

> Everything under this tag is **AI-generated and is not an audit conclusion**. Payloads carry
> `"source": "AI-generated"` and `"requires_human_review": true`.

### `POST /api/v1/assessments/run`

Runs one control assessment end to end, **synchronously**: retrieve → prompt → model → parse →
validate citations → (mode C) safety rails → risk score → persist.

**Body** `AssessmentRunRequest`:

| Field | Type | Default | Notes |
|---|---|---|---|
| `project_id`\* | int | | |
| `control_ref`\* | str | | `CONTROL-001` or a numeric id |
| `mode` | `ExperimentMode` | `C_RAG_WORKFLOW` | `A_RAW_LLM` \| `B_RAG` \| `C_RAG_WORKFLOW` |
| `persist` | bool | `true` | `false` runs and measures without writing anything |

**Response** `AssessmentRunResponse` (200, not 201 — a `persist=false` run creates nothing):
`persisted`, `assessment` (the full `AssessmentDetailResponse`, `null` when `persist=false`), `run`
(`RunTelemetry`), `notice`.

```bash
curl -s -X POST $API/api/v1/assessments/run \
  -H 'Content-Type: application/json' \
  -d '{"project_id": 2, "control_ref": "CONTROL-001", "mode": "C_RAG_WORKFLOW", "persist": true}'
```

```jsonc
{
  "persisted": true,
  "notice": "AI-generated assessment. It is a research prototype's suggestion, not an audit conclusion, and requires review by a qualified auditor before any reliance.",
  "run": {
    "assessment_id": 1, "control_ref": "CONTROL-001", "project_id": 2,
    "mode": "C_RAG_WORKFLOW", "status": "POTENTIAL_DEFICIENCY",
    "risk_level": "CRITICAL", "risk_score": 79.75,
    "latency_ms": 175, "retrieval_ms": 22, "llm_ms": 109, "llm_calls": 3,
    "prompt_tokens": 30500, "completion_tokens": 2369,
    "rails_enforced": true, "error": "",
    "retrieval": {
      "strategy": "HYBRID",
      "chunk_ids": [5, 4, 3, 1, 2, 6, 9, 10, 8, 7],
      "queries": ["Multi-Factor Authentication for Privileged Accounts Ensure that every account …", "…"],
      "notes": ["Reciprocal-rank fusion (k=60) of 10 keyword and 10 vector candidates over 10 chunks."]
    },
    "validation": {
      "total": 5, "verified": 5, "partial": 0, "fabricated": 0, "unverified": 0,
      "grounding_rate": 1.0, "grounding_rate_strict": 1.0, "threshold": 0.6,
      "has_hallucination": false, "status_downgraded": false,
      "original_status": "POTENTIAL_DEFICIENCY",
      "rails_applied": ["human_review_enforced: every assessment produced by this system requires auditor review before it can be relied upon."],
      "citations": [ "…per-citation verdicts and notes…" ]
    },
    "output": { "…the AssessmentOutput the system stands behind…" },
    "steps": [ "…one entry per step; see below…" ]
  },
  "assessment": { "id": 1, "status": "POTENTIAL_DEFICIENCY", "citation_count": 5, "…": "…" }
}
```

`run.steps` in full, from a separate mode-C run over the same project (per-step latencies vary
between runs; these are from one execution against the mock):

```json
[
 {"name": "retrieval", "kind": "retrieval", "latency_ms": 16, "ok": true,
  "detail": {"strategy": "HYBRID", "queries": 10, "chunks": 10, "candidates": 10}},
 {"name": "sufficiency", "kind": "llm", "latency_ms": 20, "ok": true,
  "prompt_tokens": 9622, "completion_tokens": 321,
  "detail": {"provider": "mock", "model": "mock-deterministic-rules", "prompt_chars": 30962,
             "finish_reason": "stop", "can_conclude": true, "evidence_sufficiency": "SUFFICIENT"}},
 {"name": "assessment", "kind": "llm", "latency_ms": 19, "ok": true,
  "prompt_tokens": 9582, "completion_tokens": 1982,
  "detail": {"provider": "mock", "model": "mock-deterministic-rules", "prompt_chars": 30804,
             "finish_reason": "stop"}},
 {"name": "critique", "kind": "llm", "latency_ms": 19, "ok": true,
  "prompt_tokens": 11296, "completion_tokens": 66,
  "detail": {"provider": "mock", "model": "mock-deterministic-rules", "prompt_chars": 37660,
             "finish_reason": "stop", "overstated_conclusion": false,
             "unsupported_claims": 0, "fabricated_citations": 0}}
]
```

Reading `run.validation` and `run.steps` is how a research run is measured. Mode A records
`"strategy": "NONE"` with a note saying no retrieval was performed, one `llm` step, and
`rails_enforced: false`.

**Latency, honestly:** `llm_ms` and `retrieval_ms` are kept apart so a workflow's inference cost is
never inflated by its retrieval cost, and `latency_ms` is wall clock around the whole assessment.
Against the mock these compare Python executing a rule engine — they are **not** inference-time
measurements.

**Errors:** 404 (unknown project or control), 422 (unknown `mode`). A provider failure is **not** an
error response: it returns 200 with `run.error` populated, `status: "INSUFFICIENT_EVIDENCE"` and a
persisted row saying so, because an audit run that quietly loses a control is worse than one that
records a failure an auditor can see.

### `GET /api/v1/assessments`

**Query:** `project_id`, `control_ref`, `status` (repeatable), `risk_level` (repeatable), `mode`
(repeatable), `reviewed` (`true` = a human decision exists; `false` = the review queue),
`include_evaluation` (default **false**), `latest_per_control` (default false), `limit` (default
100, max 1000), `offset`.

```bash
curl -s "$API/api/v1/assessments?project_id=2&latest_per_control=true"
# id 1 | CONTROL-001 | C_RAG_WORKFLOW | POTENTIAL_DEFICIENCY | CRITICAL 79.75 | citations 5 | reviewed true
```

`include_evaluation=false` is the default because harness assessments are research runs, not
fieldwork. Set it `true` only when you mean to look at experiment output.

### `GET /api/v1/assessments/findings`

The latest assessment per control whose status is `POTENTIAL_DEFICIENCY` or `NOT_EFFECTIVE`.
**Query:** `project_id`, `high_risk_only` (keep only HIGH and CRITICAL), `include_evaluation`.

### `GET /api/v1/assessments/{assessment_id}`

`AssessmentDetailResponse` — 45 fields: the conclusion, the narrative, the deterministic risk block
(`risk_level`, `risk_score`, `risk_factors`), `inferences[]`, `human_verification_required[]`,
`missing_evidence[]`, the telemetry, `validation_report`, `raw_response`, `prompt_snapshot`,
`latest_review` and `citations[]`.

Each `CitationResponse` adds `chunk_text` (the real text of the resolved chunk) and
`chunk_resolved`, so a client can show the quotation beside its source without a second call:

```bash
curl -s $API/api/v1/assessments/1
# citations[0] → {
#   "id": 1, "chunk_id": 5, "verdict": "VERIFIED", "match_score": 1.0, "supports": "requirement",
#   "filename": "Multi_Factor_Authentication_Policy_v3.docx",
#   "locator_text": "Multi_Factor_Authentication_Policy_v3.docx - section '4. Evidence of operation' - paragraph 14",
#   "quoted_text": "The privileged account listing produced for audit must report the multi-factor authentication enrolment status of every …",
#   "verification_note": "Quote matched chunk 5 at 100% (threshold 60%).",
#   "chunk_resolved": true, "chunk_text": "…the full text of chunk 5…"
# }
```

`chunk_id: null` with `chunk_resolved: false` is the fabricated-citation signal.

---

## Reviews

### `POST /api/v1/reviews` → **201**

Records the auditor's decision **as a new row**. It never edits the AI record — that pair is what
makes human/AI agreement measurable.

**Body** `ReviewCreateRequest`: `assessment_id`\*, `decision`\* (`HumanDecision`), `reviewer_name`,
`final_status`, `final_risk_level`, `final_finding`, `final_recommendation`, `comments`,
`requested_evidence[]`, `review_seconds`, `usefulness_rating` (1–5, clamped),
`flagged_hallucination`, `hallucination_note`.

```bash
curl -s -X POST $API/api/v1/reviews \
  -H 'Content-Type: application/json' \
  -d '{
    "assessment_id": 1,
    "reviewer_name": "Research Auditor",
    "decision": "MODIFIED",
    "final_status": "POTENTIAL_DEFICIENCY",
    "final_risk_level": "HIGH",
    "final_finding": "Ten privileged accounts are not enrolled; confirmed against the source export.",
    "comments": "Risk band lowered after discussion with the control owner.",
    "review_seconds": 240,
    "usefulness_rating": 4
  }'
```

```json
{
  "id": 1, "assessment_id": 1, "reviewer_name": "Research Auditor",
  "decision": "MODIFIED",
  "final_status": "POTENTIAL_DEFICIENCY", "final_risk_level": "HIGH",
  "final_finding": "Ten privileged accounts are not enrolled; confirmed against the source export.",
  "final_recommendation": "", "comments": "Risk band lowered after discussion with the control owner.",
  "requested_evidence": [],
  "agreed_with_ai_status": true,
  "agreed_with_ai_risk": false,
  "review_seconds": 240.0, "usefulness_rating": 4,
  "flagged_hallucination": false, "hallucination_note": "",
  "created_at": "2026-09-10T23:46:11.807049+00:00",
  "source": "Human auditor"
}
```

Note the derived flags: the auditor **kept the AI's status** (agreement) and **changed its risk
band** (disagreement). The two are separate judgements and are stored separately.

Semantics worth knowing before using these rows as data:

- Agreement is about the **outcome**. A `MODIFIED` or `REJECTED` decision landing on the same status
  still counts as `agreed_with_ai_status: true`; `decision` is stored so a stricter definition can
  be recomputed later.
- Omitting `final_status` carries the AI status over — **except** for `MORE_EVIDENCE_REQUESTED`,
  which defaults to `INSUFFICIENT_EVIDENCE`.
- `decision: "PENDING"` records that someone opened the review. It never counts as agreement and
  never counts as completed.

**Errors:** 404 (unknown assessment). An unknown `decision`, `final_status` or `final_risk_level` is
**422**, not 400: those fields are typed as enums on the request model, so FastAPI rejects them
before the service layer's own `InvalidInputError` (which would be a 400) can fire. The 400 path
exists and is reachable when the service is called directly, e.g. from the Streamlit facade.

### `GET /api/v1/reviews`

**Query:** `project_id`, `assessment_id`, `reviewer_name` (exact), `include_pending` (default true).

### `GET /api/v1/reviews/pending`

The review queue: the **latest** assessment per control with no completed decision. Computed
latest-first and *then* filtered, so re-running a control leaves one queue item rather than
resurrecting the superseded assessment. Returns `AssessmentSummaryResponse[]`.

```bash
curl -s "$API/api/v1/reviews/pending?project_id=2"   # → []  (the one assessment has been reviewed)
```

---

## Dashboard

### `GET /api/v1/dashboard/stats`

**Query:** `project_id` (omit for all projects).

```bash
curl -s "$API/api/v1/dashboard/stats?project_id=2"
```

```jsonc
{
  "project_id": 2,
  "generated_at": "2026-09-10T23:46:11.979190+00:00",
  "total_controls": 14, "controls_in_scope": 1, "controls_assessed": 1,
  "effective": 0, "potential_deficiencies": 1, "not_effective": 0,
  "insufficient_evidence": 0, "not_applicable": 0,
  "high_risk_findings": 1,
  "pending_human_reviews": 0, "completed_reviews": 1,
  "human_ai_agreement_rate": 1.0, "human_ai_risk_agreement_rate": 0.0,
  "evidence_files": 2, "evidence_chunks": 10,
  "avg_latency_ms": 175.0,
  "citation_grounding_rate": 1.0, "citations_total": 5, "citations_verified": 5,
  "assessments_counted": 1, "flagged_hallucinations": 0,
  "definitions": {
    "counting_basis": "Status counts, risk counts and latency are computed over the latest assessment per control, excluding assessments produced by the evaluation harness.",
    "high_risk_findings": "Latest assessments whose status is POTENTIAL_DEFICIENCY or NOT_EFFECTIVE and whose risk level is HIGH or CRITICAL.",
    "pending_human_reviews": "Latest assessments with no human review decision other than PENDING.",
    "human_ai_agreement_rate": "Completed reviews whose final status equals the AI status, divided by completed reviews. A MODIFIED or REJECTED decision that lands on the same status counts as agreement: the metric is about the conclusion, not the editing.",
    "citation_grounding_rate": "Citations with verdict VERIFIED divided by all citations on the counted assessments. PARTIAL matches do not count as grounded.",
    "avg_latency_ms": "Mean end-to-end latency of the counted assessments that recorded a latency."
  }
}
```

**Every response ships its own `definitions` block** so a figure cannot be quoted without its
counting basis. Rates are 0.0 when the denominator is zero — never "100 % of nothing". Note that
`citation_grounding_rate` here counts **VERIFIED only**, which is the same definition as
`grounding_rate_strict` on an assessment's validation report, and *not* the same as that report's
half-credit `grounding_rate`. A page showing both must label which is which.

⚠️ **Known defect:** with `project_id` omitted, `evidence_files`, `evidence_chunks` and
`controls_in_scope` are counted over **all** rows with no evaluation-harness exclusion, so
evaluation projects inflate those three fields while they exist. Status, findings, risk, latency and
review figures are correctly excluded.

### `GET /api/v1/dashboard/status-breakdown` · `/risk-breakdown`

```bash
curl -s "$API/api/v1/dashboard/status-breakdown?project_id=2"
# {"project_id":2,
#  "counts":{"EFFECTIVE":0,"POTENTIAL_DEFICIENCY":1,"INSUFFICIENT_EVIDENCE":0,"NOT_EFFECTIVE":0,"NOT_APPLICABLE":0},
#  "basis":"Latest assessment per control, excluding assessments produced by the evaluation harness."}
```

Both always return **every** member of the enum, including zeros, so a chart cannot silently omit a
category.

### `GET /api/v1/dashboard/activity`

**Query:** `project_id`, `entity_type`, `action` (repeatable), `limit` (default 50, max 500).
Newest first.

```bash
curl -s "$API/api/v1/dashboard/activity?project_id=2&limit=4"
# 23:46:21 HUMAN REPORT_GENERATED       report        Research Auditor
# 23:46:11 HUMAN HUMAN_REVIEW_RECORDED  assessment    Research Auditor
# 23:45:59 AI    ASSESSMENT_RUN         assessment    mock (mock-deterministic-rules)
# 23:45:47 HUMAN EVIDENCE_PARSED        evidence_file Research Auditor
```

`actor_type` is `"AI"` or `"HUMAN"`, which is what makes provenance queryable.

---

## Reports

### `POST /api/v1/reports/generate` → **201**

Builds the ten-section report, renders it, writes it under `settings.report_dir` and persists the
row. **Synchronous.**

**Body** `ReportGenerateRequest`: `project_id`\*, `format` (`markdown` | `html`, default
`markdown`), `title`, `generated_by`, `include_evaluation` (default false).

```bash
curl -s -X POST $API/api/v1/reports/generate \
  -H 'Content-Type: application/json' \
  -d '{"project_id": 2, "format": "markdown", "generated_by": "Research Auditor"}'
```

```jsonc
{
  "id": 1, "project_id": 2,
  "title": "IT Audit Report - Q2 Privileged Access Review",
  "format": "markdown", "generated_by": "Research Auditor",
  "content_chars": 24350, "has_file": true,
  "download_url": "/api/v1/reports/1/download",
  "generated_at": "2026-09-10T23:46:21.388567+00:00",
  "content": "# IT Audit Report - Q2 Privileged Access Review\n\n> **Provenance notice.** …",
  "summary_stats": {
    "llm_providers": ["mock"], "llm_models": ["mock-deterministic-rules"],
    "used_mock_provider": true, "prompt_version": "1.0.0",
    "experiment_modes": ["C_RAG_WORKFLOW"], "retrieval_strategies": ["HYBRID"],
    "controls_in_scope": 1, "controls_assessed": 1, "controls_pending_review": 0,
    "controls_concluded_by_auditor": 1, "controls_status_divergent": 0,
    "confirmed_deficiencies": 1, "confirmed_high_risk_findings": 1,
    "human_ai_status_agreement_rate": 1.0,
    "citations_total": 5, "citation_verdicts": {"VERIFIED": 5, "PARTIAL": 0, "…": 0}
  }
}
```

The ten sections are Executive Summary · Audit Scope · Controls Tested · Evidence Reviewed · AI
Findings · Risk Ratings · Evidence References · Human Auditor Decisions · Recommendations ·
Limitations. For every control the document prints the AI conclusion and the auditor's conclusion
**side by side** under the literal labels
`AI-generated assessment (not a final audit conclusion)` and `Final auditor assessment`; a control
with no completed review is `PENDING AUDITOR REVIEW` and is **excluded from every headline figure**.
`used_mock_provider: true` is printed in the document itself, not only in this payload.

**Errors:** 404 (unknown project), 400 (unsupported `format` — `pdf`, `docx` and friends are refused
loudly rather than silently rendered as markdown).

### `GET /api/v1/reports` · `GET /api/v1/reports/{report_id}`

The list omits `content` (`ReportSummaryResponse`); the single-report route includes it.

### `GET /api/v1/reports/{report_id}/download`

Returns the rendered text as an attachment.

```bash
curl -s -D - -o report.md $API/api/v1/reports/1/download | head -5
# HTTP/1.1 200 OK
# content-disposition: attachment; filename="audit_report_p2_q2-privileged-access-review_20260910T234621Z.md"
# content-length: 24350
# content-type: text/markdown; charset=utf-8
```

`text/markdown; charset=utf-8` or `text/html; charset=utf-8` depending on the stored format.

---

## Evaluation

The research harness. Requires `app.evaluation.runner`; if that module is unavailable the run
endpoint returns **503** rather than pretending.

### `GET /api/v1/evaluation/datasets`

The six synthetic datasets with their declared ground truth.

```bash
curl -s $API/api/v1/evaluation/datasets
```

```jsonc
[{
  "dataset_id": "DATASET-001",
  "name": "MFA on privileged accounts - 10 of 100 accounts not enrolled",
  "control_ref": "CONTROL-001",
  "expected_status": "POTENTIAL_DEFICIENCY",
  "expected_risk": "CRITICAL",
  "expected_finding": "10 of the 100 privileged accounts in the identity provider export show MFA_Status = Disabled, and no exception register was supplied to cover them.",
  "expected_missing_evidence": false,
  "mandated": true,
  "files": [
    {"filename": "Multi_Factor_Authentication_Policy_v3.docx", "extension": ".docx",
     "evidence_type": "POLICY", "role": "requirement", "description": "…"},
    {"filename": "Privileged_Accounts_MFA_Export_2024-06-30.csv", "extension": ".csv",
     "evidence_type": "USER_LISTING", "role": "population", "description": "…"}
  ],
  "key_evidence_markers": ["…strings a correct retrieval must surface…"],
  "population": {"total": 100, "exceptions": 10, "attribute": "MFA_Status", "…": "…"},
  "exception_rows": [14, 22, 31, 38, 47, 55, 63, 71, 86, 97],
  "exception_ratio": 0.1,
  "notes": "…", "rationale": "…", "seed": 20240630
}, "…five more…"]
```

| Dataset | Control | Planted condition | Expected status | Mandated |
|---|---|---|---|---|
| `DATASET-001` | CONTROL-001 | 100 privileged accounts, 10 with MFA disabled | `POTENTIAL_DEFICIENCY` | yes |
| `DATASET-002` | CONTROL-003 | 100 endpoints, 5 missing critical patches | `POTENTIAL_DEFICIENCY` | yes |
| `DATASET-003` | CONTROL-005 | Policy requires min length 14; configuration shows 8 | `NOT_EFFECTIVE` | yes |
| `DATASET-004` | CONTROL-004 | 100 change tickets, 5 unapproved | `POTENTIAL_DEFICIENCY` | yes |
| `DATASET-005` | CONTROL-001 | Privileged listing with **no MFA column at all** | `INSUFFICIENT_EVIDENCE` | yes |
| `DATASET-006` | CONTROL-001 | 100 accounts, all enrolled, plus enforcement config | `EFFECTIVE` | **no** — added so the confusion matrix has a true-negative class |

### `POST /api/v1/evaluation/run` → **201**

Runs one experimental condition over a set of datasets: each dataset gets its **own throwaway
project**, evidence is generated and ingested, every control is assessed, and predictions are scored
against the declared ground truth. **Synchronous.**

**Body** `EvaluationRunRequest`: `mode` (`ExperimentMode`, default `C_RAG_WORKFLOW`),
`dataset_ids[]` (null = all six), `run_name`.

```bash
curl -s -X POST $API/api/v1/evaluation/run \
  -H 'Content-Type: application/json' \
  -d '{"mode": "B_RAG", "dataset_ids": ["DATASET-001", "DATASET-005"], "run_name": "docs smoke"}'
```

```jsonc
{
  "id": 1, "name": "docs smoke", "experiment_mode": "B_RAG",
  "llm_provider": "mock", "llm_model": "mock-deterministic-rules",
  "retrieval_strategy": "HYBRID",
  "dataset_ids": ["DATASET-001", "DATASET-005"],
  "status": "COMPLETED", "duration_seconds": 0.357, "result_count": 2,
  "config":  { "…everything needed to reproduce the run; contains no secret…" },
  "metrics": {
    "n": 2, "n_scored": 2, "n_errors": 0,
    "classification": {"accuracy": 1.0, "macro_f1": 1.0, "per_class": {"…": "…"}, "…": "…"},
    "confusion_matrix": {"POTENTIAL_DEFICIENCY": {"POTENTIAL_DEFICIENCY": 1, "INSUFFICIENT_EVIDENCE": 0}, "…": "…"},
    "confusion_matrix_orientation": "rows = expected (ground truth), columns = predicted",
    "deficiency_detection": {"…": "…"}, "evidence": {"…": "…"}, "timing": {"…": "…"},
    "caveats": [
      "n = 2 is below 30. Every proportion here has a confidence interval wider than the differences it would be used to compare; report these as descriptive figures for these runs, not as estimates of general performance.",
      "Classes with fewer than 5 ground-truth cases: POTENTIAL_DEFICIENCY (support 1), INSUFFICIENT_EVIDENCE (support 1). …",
      "Statuses absent from both ground truth and predictions, so untested here: EFFECTIVE, NOT_EFFECTIVE, NOT_APPLICABLE.",
      "Latency p95 is computed from 2 observations; below 20 it is effectively the maximum, not a percentile."
    ]
  },
  "results": [ "…one EvaluationResultResponse per dataset…" ]
}
```

**`metrics.caveats` must be reproduced wherever these numbers are.** The harness emits them
automatically so a figure cannot travel without its health warning.

`llm_provider` and `llm_model` record the provider that **actually ran** — a results table naming a
hosted model after a silent fallback to the mock would be a false statement.

**Errors:** 422 (unknown `mode`), 503 (`app.evaluation.runner` unavailable).

⚠️ **An unknown dataset id or an empty `dataset_ids` list returns 500, not 400.** Verified:

```bash
curl -s -X POST $API/api/v1/evaluation/run -H 'Content-Type: application/json' \
  -d '{"mode":"B_RAG","dataset_ids":["DATASET-999"]}'
# → 500 {"error":{"type":"UnknownDatasetError","message":"Internal server error. The traceback was written to the server log.","status_code":500,…}}

curl -s -X POST $API/api/v1/evaluation/run -H 'Content-Type: application/json' \
  -d '{"mode":"B_RAG","dataset_ids":[]}'
# → 500 {"error":{"type":"ValueError","message":"Internal server error. …","status_code":500,…}}
```

The runner raises `UnknownDatasetError` (a `KeyError` subclass) and `ValueError`, neither of which
has a registered handler, so the global `Exception` handler catches them. The `error.type` names the
real class, so the cause is diagnosable, but a caller mistake is reported as a server fault and the
useful message ("Available datasets: DATASET-001 … DATASET-006") reaches only the server log. The
fix belongs in the router or in the runner: translate these to
`app.audit.service.InvalidInputError`, which already maps to 400.

### `GET /api/v1/evaluation/runs` · `GET /api/v1/evaluation/runs/{run_id}`

**Query on the list:** `mode`, `limit` (default 50, max 500). Newest first. The list omits
`results`; the detail route includes them.

```bash
curl -s "$API/api/v1/evaluation/runs?limit=5"
# id 1 | "docs smoke" | B_RAG | COMPLETED | 2 results | 0.357 s

curl -s $API/api/v1/evaluation/runs/1
# results[0] → {"dataset_id":"DATASET-001","expected_status":"POTENTIAL_DEFICIENCY",
#               "predicted_status":"POTENTIAL_DEFICIENCY","status_correct":true,
#               "citation_count":5,"verified_citation_count":5,"fabricated_citation_count":0,
#               "hallucination_detected":false,"declared_missing_evidence":true,
#               "expected_evidence_hits":5,"expected_evidence_total":5,"latency_ms":86}
```

### `GET /api/v1/evaluation/compare`

**Query:** `run_ids` (**required, repeatable**): `?run_ids=1&run_ids=2&run_ids=3`.

```bash
curl -s "$API/api/v1/evaluation/compare?run_ids=1"
```

```jsonc
{
  "run_ids": [1],
  "source": "app.evaluation.runner.compare_runs",
  "warning": "Smallest run has 2 scored cases; below 30 these rates are descriptive of this suite only and carry no statistical weight.",
  "rows": [{
    "run_id": 1, "run_name": "docs smoke", "status": "COMPLETED", "n": 2,
    "accuracy": 1.0, "macro_f1": 1.0, "weighted_f1": 1.0,
    "deficiency_precision": 1.0, "deficiency_recall": 1.0, "deficiency_fpr": 0.0, "deficiency_fnr": 0.0,
    "citation_rate": 1.0, "grounding_rate": 1.0, "fabrication_rate": 0.0,
    "unsupported_claim_rate": 0.0, "hallucination_rate": 0.0,
    "retrieval_recall": 1.0, "missing_evidence_detection_rate": 1.0,
    "latency_median_ms": 84.5, "latency_p95_ms": 85.85, "latency_mean_ms": 84.5,
    "mean_total_tokens": 10989.5, "caveats": 4
  }]
}
```

**Cells can be `NaN`** (e.g. `deficiency_fpr` when no negative case was run). A client must render
`NaN` as "n/a", never as a number. **Errors:** 404 for an unknown run id.

---

## Settings

### `GET /api/v1/settings`

Configuration with **every secret masked**: the API key is a fingerprint, and neither filesystem
paths nor the database URL are disclosed (`database_backend` is reduced to `"sqlite"` /
`"postgresql"`, because a connection string can carry credentials).

```bash
curl -s $API/api/v1/settings
```

```jsonc
{
  "app_name": "LLM-Assisted IT Audit Risk and Control Assessment System",
  "app_short_name": "LLM IT Auditor", "app_version": "0.1.0",
  "environment": "development", "debug": false,
  "provider": {
    "llm_provider": "mock", "llm_model": "gpt-4o-mini",
    "llm_base_url": "(provider default)", "llm_api_key": "(not set)", "llm_configured": true,
    "embedding_provider": "local", "embedding_model": "hashing-vectorizer",
    "retrieval_strategy": "HYBRID", "retrieval_top_k": 12, "mock_hallucination_rate": 0.0
  },
  "limits": {
    "max_upload_mb": 50, "max_upload_bytes": 52428800,
    "supported_upload_extensions": [".pdf", ".docx", ".csv", ".xlsx", ".xls", ".txt", ".md", ".json"],
    "max_evidence_chars": 24000, "api_request_timeout_seconds": 300
  },
  "force_human_review": true,
  "citation_match_threshold": 0.6,
  "default_auditor_name": "Research Auditor",
  "database_backend": "sqlite",
  "security_note": "This prototype has no authentication, no authorisation and no rate limiting. Run it on localhost with synthetic data only. …"
}
```

A configured key renders as `sk-...abcd (len 51)` — enough for an operator to confirm *which* key is
set, never enough to use it.

### `GET /api/v1/settings/providers`

The `ProviderHealth` block on its own. Configuration-only; makes no network call.

### `POST /api/v1/settings`

**Always refused**, and explains why. Returns 200 with `applied: false` rather than 403, because the
request was understood and answered.

```bash
curl -s -X POST $API/api/v1/settings \
  -H 'Content-Type: application/json' \
  -d '{"changes": {"llm_provider": "openai"}, "reason": "switch to a hosted model"}'
```

```json
{
  "applied": false,
  "message": "Configuration is read-only over HTTP. Nothing was changed. This API is unauthenticated, and an endpoint that could repoint the model client or the storage locations would expose the evidence this system holds.",
  "requested_keys": ["llm_provider"],
  "how_to_change": "Edit .env (or the process environment) and restart the API. See .env.example for every supported variable."
}
```

---

## Known API limitations

1. **No authentication, authorisation or rate limiting** — see the banner at the top. This is the
   single most important limitation and it is not mitigated anywhere.
2. **Assessments and evaluation runs are synchronous.** With a hosted model a whole-suite evaluation
   run is minutes long and will exceed a default 60-second client or proxy timeout. There is no job
   queue, no polling endpoint and no progress stream.
3. **Most list endpoints are unpaginated.** `/assessments`, `/evidence/{id}/chunks`,
   `/dashboard/activity` and `/evaluation/runs` take `limit`/`offset`; `/projects`, `/controls`,
   `/evidence`, `/reviews` and `/reports` return the whole set because the underlying service
   functions take no limit. Fine at prototype scale, wrong at a few thousand rows.
4. **Lists are plain arrays**, with no `{items, total}` envelope — so a client cannot tell a
   truncated page from a complete set on the endpoints that do paginate.
5. **Report bodies are returned inline in JSON** (24 kB markdown / ~43 kB HTML in the examples
   above). There is no streaming path for a large project.
6. **Upload buffers the whole file in memory** up to `max_upload_bytes` (default 50 MB). The read is
   chunked so an oversized upload is rejected after ~1 MB, but a permitted upload is fully in RAM —
   `app.evidence.service.ingest_file` takes `bytes`, so streaming to disk would need a change there.
7. **`/health` only checks the database.** It would report `status: "ok"` with an empty control
   library; a `controls_seeded` count would close that gap.
8. **`AssessmentDetailResponse` carries no nested control object.** A client that wants the control
   objective, assessment criteria and expected evidence beside the model's own restatement must
   fetch `GET /api/v1/controls/{control_ref}` separately. The Streamlit pages do exactly that.
9. **One router touches the ORM directly.** `app/api/routers/evaluation.py` uses `session.get` and a
   `select` in `_require_run`/`list_runs` because `app.audit.service` has no `EvaluationRun`
   accessors. Reads only, and documented in the file, but it breaks the "no ORM in routers" rule.
10. **Two caller errors surface as HTTP 500.** An unknown dataset id or an empty `dataset_ids` list
    on `POST /api/v1/evaluation/run` raises `UnknownDatasetError` / `ValueError` from
    `app/evaluation/runner.py`, neither of which is mapped, so the generic handler answers 500. The
    envelope still names the real exception class, but the explanatory message
    ("Available datasets: …") never reaches the client.
11. **Request models ignore unknown fields, and the service layer does not.** `extra="ignore"` on
    every request model means a mistyped key on a `PATCH` returns 200 having changed nothing, while
    the same call through the in-process facade raises. Deliberate on the API side (client/server
    version skew), but the two front ends genuinely behave differently here.
12. **A partially applied create can leave a row behind.** `POST /api/v1/projects` with a
    `control_refs` entry that does not exist returns 404 — **after** the project row has been
    committed. Verified: the project count increases by one across the failed request. The create
    and the scoping are not one transaction.
13. **The chunk payload shape is stated in two places.** `app/api/routers/evidence.py` re-states the
    nine-key chunk shape that `app.evidence.service._chunk_dict` produces, because that helper is
    private. If one changes, the two can drift. Over HTTP the chunk payload also omits
    `has_embedding`, which the in-process facade supplies.

---

*Companion documents:* [`ARCHITECTURE.md`](ARCHITECTURE.md) ·
[`DATABASE_SCHEMA.md`](DATABASE_SCHEMA.md) · [`SETUP.md`](SETUP.md) · [`TESTING.md`](TESTING.md)
