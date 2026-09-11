# Setup and operation

Everything below was run on the machine this repository lives on (macOS, CPython 3.9.6). Commands
assume the repository root as the working directory.

**Contents**

- [Prerequisites](#prerequisites)
- [Install](#install)
- [First run](#first-run)
- [Entry points](#entry-points)
- [Configuration reference](#configuration-reference)
- [Switching to a real LLM provider](#switching-to-a-real-llm-provider)
- [Embeddings](#embeddings)
- [Pointing at PostgreSQL](#pointing-at-postgresql)
- [Running the UI against the API](#running-the-ui-against-the-api)
- [Troubleshooting](#troubleshooting)
- [Security notes for handling audit evidence](#security-notes-for-handling-audit-evidence)

---

## Prerequisites

| | |
|---|---|
| **Python** | **3.9.6** — the target runtime. The code is written to 3.9 syntax throughout (`typing.List`/`Optional`, no PEP 604 `X \| None`, no `match`), so a newer interpreter will also work, but 3.9 is what everything was verified on. Anything **older than 3.9 will not work**. |
| **OS** | macOS or Linux. Nothing is platform-specific, but the shell commands below are POSIX. |
| **Disk** | ~400 MB for the virtualenv (pandas, numpy, scikit-learn and the Streamlit stack dominate), plus whatever the evidence occupies. |
| **Network** | Needed **once**, to install dependencies. After that the system runs fully offline with `LLM_PROVIDER=mock`. |
| **Not required** | Docker, Node, a database server, an API key, an internet connection at run time. |

Check your interpreter first:

```bash
python3 -V        # expect: Python 3.9.6
```

---

## Install

```bash
cd "/Users/tingo/Documents/IT AUDIT TOOL"     # or wherever you cloned it
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

**Always invoke the interpreter as `.venv/bin/python`.** Every command in this project's docs does
so rather than relying on an activated shell, because a half-activated environment is the most
common cause of "it works for me".

Verify the install:

```bash
.venv/bin/python -c "import app.config, app.database.models, app.audit.engine; print('imports ok')"
.venv/bin/python -m pytest tests -q
```

The suite takes about a minute and should end `713 passed`. It is hermetic: it redirects the
database and every writable directory into a temporary tree before the first `app.*` import, and it
cannot touch `data/audit.db`. See [`TESTING.md`](TESTING.md).

### What is installed

Pinned in `requirements.txt`, all verified against CPython 3.9.6:

| Area | Packages |
|---|---|
| API | `fastapi==0.128.8`, `uvicorn[standard]==0.39.0`, `python-multipart==0.0.20` |
| UI | `streamlit==1.50.0`, `plotly==7.0.0`, `altair==5.5.0` |
| Config & validation | `pydantic==2.13.5`, `pydantic-settings==2.11.0`, `python-dotenv==1.2.1` |
| Persistence | `SQLAlchemy==2.0.52` |
| Document parsing | `pypdf==6.18.0`, `python-docx==1.2.0`, `openpyxl==3.1.5`, `pandas==2.3.3`, `numpy==2.0.2` |
| Retrieval | `scikit-learn==1.6.1` (the `HashingVectorizer` behind the local embedding provider) |
| LLM | `openai==2.48.0`, `httpx==0.28.1`, `requests==2.32.5` |
| Reporting | `Jinja2==3.1.6`, `reportlab==5.0.1` (used only to generate synthetic PDF *evidence*) |
| Testing | `pytest==8.4.2` |

`psycopg` and `alembic` are listed as commented-out lines for the PostgreSQL path and are **not
installed**.

---

## First run

No configuration is required. Every setting has a working default and the system starts fully
offline against the deterministic mock provider.

```bash
.venv/bin/python run.py init        # create data/audit.db and seed the 14-control library
.venv/bin/python run.py seed-demo   # + create the demo project and its synthetic evidence
.venv/bin/python run.py ui          # open the audit console at http://localhost:8501
```

`run.py init` prints `Database ready. Control library contains 14 controls.`

Optionally create a `.env` to change anything:

```bash
cp .env.example .env
```

`.env` is in `.gitignore`. **Never commit one containing a real API key.**

---

## Entry points

### `run.py` — the launcher

| Command | What it does |
|---|---|
| `run.py` or `run.py all` | Starts the FastAPI backend **and** the Streamlit UI, forwarding Ctrl-C to both. Sets `USE_API=false` by default so the UI stays self-sufficient if the API dies. |
| `run.py api` | FastAPI only → `http://127.0.0.1:8000/docs` |
| `run.py ui` | Streamlit only → `http://localhost:8501`. **This alone is a complete application** — the UI calls the service layer in-process and needs no backend. |
| `run.py init` | `init_db()` + seed the control library. Idempotent. |
| `run.py seed-demo` | The above, plus the demo project and generated synthetic evidence. Idempotent. |
| `run.py evaluate [--modes A_RAW_LLM B_RAG C_RAG_WORKFLOW]` | Runs the research experiments over the synthetic datasets and prints accuracy / macro-F1 / n per mode. |
| `run.py reset` | **DESTRUCTIVE.** Drops every table, recreates the schema and re-seeds the library. Requires typing `yes`. |

`run.py` prefers `.venv/bin/python` automatically, so `python3 run.py ui` works too.

### Direct invocation

```bash
# API
.venv/bin/python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000
# add --reload while developing

# UI
.venv/bin/python -m streamlit run app/frontend/streamlit_app.py --server.port 8501

# Tests
.venv/bin/python -m pytest tests -q
.venv/bin/python -m pytest tests -q -m "not slow"
```

### Where things end up

| Path | Contents |
|---|---|
| `data/audit.db` | SQLite database (plus `-wal` / `-shm` — WAL is enabled) |
| `data/uploads/project_<id>/` | Stored evidence bytes, content-addressed as `<stem>_<hash12><ext>` |
| `data/reports/` | Generated `.md` / `.html` reports |
| `data/synthetic/dataset-00N/` | Generated synthetic evidence for the evaluation datasets |
| `data/evaluation/` | Evaluation output directory |
| `data/controls/control_library.json` | The 14-control synthetic library — **read-only study data, and the only `data/` path not gitignored** |

---

## Configuration reference

Every value is read by `app/config.py` from the environment or `.env`, and every one has a default.
`get_settings()` is LRU-cached; `reload_settings()` re-reads it.

### Application

| Variable | Default | Notes |
|---|---|---|
| `ENVIRONMENT` | `development` | Label only. |
| `DEBUG` | `false` | |

### Database

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `sqlite:///<repo>/data/audit.db` | Any SQLAlchemy URL. See [PostgreSQL](#pointing-at-postgresql). |
| `DB_ECHO` | `false` | Log every SQL statement. Very noisy; useful once. |

### LLM provider

| Variable | Default | Notes |
|---|---|---|
| `LLM_PROVIDER` | `mock` | `mock` or `openai`. Aliases resolving to `openai`: `ollama`, `vllm`, `lmstudio`, `openrouter`, `azure`, `together`, `groq`, `local`, `openai-compatible`. An unrecognised value falls back to `mock` with a warning. |
| `LLM_MODEL` | `gpt-4o-mini` | Ignored by the mock. |
| `LLM_API_KEY` | *(unset)* | Never logged; shown only as a masked fingerprint. |
| `LLM_BASE_URL` | *(unset)* | Any OpenAI-compatible `/v1` endpoint. |
| `LLM_TEMPERATURE` | `0.0` | |
| `LLM_MAX_TOKENS` | `2500` | |
| `LLM_TIMEOUT` | `120` | Seconds, per provider call. |
| `LLM_MAX_RETRIES` | `2` | Transient errors only; a 4xx is raised immediately. |
| `LLM_USE_JSON_MODE` | `true` | Ask for schema-constrained output where supported. Degrades in steps. |
| `MOCK_HALLUCINATION_RATE` | `0.0` | **Research lever.** Fraction of mock assessments that deliberately inject a fabricated citation or an unsupported figure, so the validator can be exercised offline. Clamped to [0, 1]. Keep at 0 for normal use. |
| `MOCK_SEED` | `1337` | Makes the mock deterministic. |

### Embeddings

| Variable | Default | Notes |
|---|---|---|
| `EMBEDDING_PROVIDER` | `local` | `local` (stateless hashing vectoriser, no network) or `openai`. |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Ignored when `local`. |
| `EMBEDDING_DIM` | `1024` | Changing it invalidates every stored vector — re-index. |
| `EMBEDDING_API_KEY`, `EMBEDDING_BASE_URL` | *(unset)* | |

### Retrieval

| Variable | Default | Notes |
|---|---|---|
| `RETRIEVAL_STRATEGY` | `HYBRID` | `KEYWORD`, `VECTOR` or `HYBRID`. An unknown value falls back to `HYBRID` with a warning. |
| `RETRIEVAL_TOP_K` | `12` | Chunks handed to the model, before table summaries are added on top. |
| `RETRIEVAL_CANDIDATE_K` | `60` | Candidates considered before fusion. |
| `CHUNK_SIZE` | `1200` | Characters. |
| `CHUNK_OVERLAP` | `180` | |
| `TABLE_ROWS_PER_CHUNK` | `25` | |
| `MAX_EVIDENCE_CHARS` | `24000` | Hard ceiling on evidence in one prompt. |
| `RAW_EVIDENCE_CHAR_BUDGET` | `24000` | Experiment A's untargeted blob budget. |

### Assessment safety

| Variable | Default | Notes |
|---|---|---|
| `FORCE_HUMAN_REVIEW` | `true` | Leaving this true is the supported mode. Note that the flag is **reported** (in `/health`-adjacent settings payloads, the Settings page and every evaluation run's config) but is **never consulted by the engine or the validators**: human review is enforced unconditionally in the pydantic schema, in `enforce_safety_rails()` and at persistence. Setting it `false` therefore does not turn the gate off — it only makes the system misdescribe itself, which is why it should be left alone. |
| `CITATION_MATCH_THRESHOLD` | `0.6` | VERIFIED at or above; PARTIAL at or above half. |

### Services

| Variable | Default | Notes |
|---|---|---|
| `API_HOST` | `127.0.0.1` | **Leave it on loopback.** There is no authentication. |
| `API_PORT` | `8000` | |
| `API_BASE_URL` | `http://127.0.0.1:8000` | Used by the UI when `USE_API=true`. |
| `STREAMLIT_PORT` | `8501` | |
| `MAX_UPLOAD_MB` | `50` | Larger uploads are rejected with HTTP 413. |
| `DEFAULT_AUDITOR_NAME` | `Research Auditor` | Used when an actor is not supplied. |
| `USE_API` | `false` | **Not a `Settings` field** — it is read straight from the environment by `app/frontend/data_access.py`, because it selects a *transport* and the service layer should not care. |

---

## Switching to a real LLM provider

One class (`OpenAICompatibleProvider`) serves OpenAI and every gateway that speaks the same
protocol. Switching is a `.env` change; no code changes.

### OpenAI

```dotenv
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o-mini
LLM_API_KEY=sk-your-key-here
# LLM_BASE_URL is left unset — the SDK default is used
LLM_USE_JSON_MODE=true
```

### Ollama (local, open-weight)

```dotenv
LLM_PROVIDER=openai
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=llama3.1:8b
LLM_API_KEY=ollama
LLM_USE_JSON_MODE=true
LLM_TIMEOUT=300
```

`LLM_API_KEY` must be **non-empty** even though Ollama ignores its value: the OpenAI SDK refuses to
construct a client without one, and `Settings.llm_configured` requires a key *or* a base URL. Any
placeholder works. Start the server first:

```bash
ollama serve
ollama pull llama3.1:8b
```

An 8B model on consumer hardware is considerably slower than a hosted one — raise `LLM_TIMEOUT`, and
expect a whole-suite evaluation run to take minutes rather than seconds.

### vLLM (local, GPU)

```dotenv
LLM_PROVIDER=openai
LLM_BASE_URL=http://localhost:8000/v1
LLM_MODEL=meta-llama/Meta-Llama-3.1-8B-Instruct
LLM_API_KEY=vllm
LLM_USE_JSON_MODE=true
LLM_TIMEOUT=300
```

```bash
python -m vllm.entrypoints.openai.api_server \
  --model meta-llama/Meta-Llama-3.1-8B-Instruct --port 8000
```

⚠️ vLLM's default port is also **8000**, which collides with this project's `API_PORT`. Move one of
them — e.g. `--port 8001` on vLLM with `LLM_BASE_URL=http://localhost:8001/v1`, or `API_PORT=8010`
here.

### LM Studio / OpenRouter / an internal gateway

Identical shape: set `LLM_PROVIDER=openai`, point `LLM_BASE_URL` at the server's `/v1`, and supply
whatever key it wants (LM Studio: `http://localhost:1234/v1`).

### Always verify which provider actually answered

The factory **falls back to the mock** when a real provider is selected but has neither a key nor a
base URL. The fallback is loud, but you must look:

```bash
curl -s $API/health | .venv/bin/python -m json.tool | grep -A3 fell_back_to_mock
```

```json
"fell_back_to_mock": false,
"fallback_reason": "",
```

If `fell_back_to_mock` is `true`, the numbers you are about to record measure the rule engine, not
the model. The Streamlit sidebar shows the same thing as a badge, every evaluation run records the
provider that *actually ran* in `EvaluationRun.llm_provider`, and every generated report prints it.

### If a compatible endpoint rejects structured output

The provider degrades in steps — JSON-schema → `{"type": "json_object"}` → plain completion — and
records which mode worked on the response (`raw["response_format_used"]`). If a server misbehaves in
a way the ladder does not catch, set `LLM_USE_JSON_MODE=false`; `extract_json()` still recovers a
JSON object from fenced or prose-wrapped output.

---

## Embeddings

The default (`local`) makes no network call and has no fitted state, which is what keeps a chunk
embedded today comparable with a query embedded weeks later.

To use a remote embedding endpoint:

```dotenv
EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_API_KEY=sk-your-key-here
EMBEDDING_DIM=1536
```

**Changing the provider, the model or the dimension invalidates every stored vector.** Nothing does
this automatically; re-index each project:

```bash
.venv/bin/python -c "
from app.database.base import session_scope
from app.rag.indexer import reindex_project
with session_scope() as s:
    print(reindex_project(s, 1), 'vectors rewritten for project 1')
"
```

`reindex_project()` is unconditional by design: honouring an "already indexed" check would leave the
project holding a mixture of incomparable vectors. Keyword (BM25) retrieval keeps working
throughout, including on chunks with no vector at all.

---

## Pointing at PostgreSQL

1. Install the driver — **it is not in the virtualenv**:
   ```bash
   .venv/bin/python -m pip install "psycopg[binary]>=3.1"
   ```
2. Create the database:
   ```sql
   CREATE DATABASE itaudit;
   CREATE USER audit WITH PASSWORD 'change-me';
   GRANT ALL PRIVILEGES ON DATABASE itaudit TO audit;
   ```
3. Configure:
   ```dotenv
   DATABASE_URL=postgresql+psycopg://audit:change-me@localhost:5432/itaudit
   ```
4. Build the schema and seed:
   ```bash
   .venv/bin/python run.py init
   ```

The schema is portable by construction — no SQLite-only types, no native ENUMs, timezone-aware
timestamps, named constraints. `app/database/base.py` already swaps engine options by backend
(`pool_pre_ping`, `pool_size=10`, `max_overflow=20` instead of SQLite's `check_same_thread`), and the
SQLite `PRAGMA` hook is a no-op elsewhere.

**The one behavioural difference:** SQLite ignores `VARCHAR(n)`; PostgreSQL enforces it. The
ingestion path already truncates (`filename` 512, `sheet_name` 255, `section` 512, `locator_text`
512, `supports` 64). Any *new* write path must do the same. Details, including migrating existing
data and the Alembic path, are in [`DATABASE_SCHEMA.md`](DATABASE_SCHEMA.md#sqlite--postgresql).

**Honest caveat:** the PostgreSQL path is supported by design but **has not been exercised in this
environment** — no PostgreSQL server was available and the driver is not installed. Everything
reported in this project was produced on SQLite.

---

## Running the UI against the API

By default the Streamlit app calls `app.audit.service` **in-process**, so it works with no backend
running. To route it through HTTP instead:

```bash
# terminal 1
.venv/bin/python -m uvicorn app.api.main:app --port 8000

# terminal 2
USE_API=true API_BASE_URL=http://127.0.0.1:8000 \
  .venv/bin/python -m streamlit run app/frontend/streamlit_app.py
```

Two things to know:

- **Both processes must share `DATABASE_URL`.** The demo-data seeding button writes to the database
  directly even in API mode (the API deliberately exposes no endpoint that fabricates evidence
  rows), so pointing them at different databases would seed the wrong one.
- **The two transports are not perfectly information-equivalent.** The API deliberately withholds
  filesystem paths and the connection string, so the Settings page shows "not available from the
  API" for those; and the API's chunk payload omits `has_embedding`, so a per-chunk "indexed" badge
  disappears. Everything else is the same.

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'app'`**
You are running a script from outside the repository root, or with the system interpreter. Run from
the root with `.venv/bin/python`, or set `PYTHONPATH=/path/to/repo`. `run.py` and
`streamlit_app.py` both insert the root on `sys.path` themselves.

**`ModuleNotFoundError: No module named 'fastapi'` (or `streamlit`, `pandas`, …)**
The virtualenv is not the interpreter being used. Check with
`.venv/bin/python -c "import sys; print(sys.executable)"`.

**The UI says the provider is `mock` but I configured OpenAI**
Neither `LLM_API_KEY` nor `LLM_BASE_URL` is set, so the factory fell back. `GET /health` →
`provider.fallback_reason` names it. Also check that `.env` is in the **repository root**
(`app/config.py` resolves it relative to `BASE_DIR`, not to your shell's cwd).

**`sqlite3.OperationalError: database is locked`**
Two processes are writing at once, or a long transaction is open. WAL and a 30-second busy timeout
are already enabled. Stop the extra process; if it persists, move to PostgreSQL — SQLite is a
single-writer database.

**Port already in use**
`API_PORT=8010 .venv/bin/python run.py api`, or `--server.port 8502` for Streamlit. Note the vLLM
collision on 8000 mentioned above.

**An upload returns HTTP 413**
Over `MAX_UPLOAD_MB` (default 50). Raise it, or split the file.

**An upload succeeds but `parse_status` is `UNSUPPORTED` or `FAILED`**
`UNSUPPORTED` = the extension is not in `.pdf .docx .csv .xlsx .xls .txt .md .json`; the bytes are
still stored and hashed. `FAILED` = the parser produced no chunks; the reason is in `parse_error`
and in `extra_metadata.warnings` (encrypted PDF, empty sheet, binary content behind a text
extension). Nothing an auditor supplied is ever silently dropped.

**Evidence uploaded, but retrieval finds nothing**
Check `chunks_embedded` on `GET /api/v1/evidence/{id}`. If it is 0, indexing failed — the warning is
in `extra_metadata.warnings`. Keyword retrieval still works; re-index as shown
[above](#embeddings). Also confirm the evidence is on the **project you are assessing**: retrieval
is project-scoped.

**Every assessment comes back `INSUFFICIENT_EVIDENCE`**
Usually correct rather than broken — the evidence genuinely does not report the attribute under
test. Check `missing_evidence` on the assessment, which names what to obtain. If
`validation_report.rails_applied` contains `no_evidence_retrieved`, nothing was retrieved at all:
no evidence is attached to that project, or it is not indexed.

**The Streamlit navigation shows only "Get started"**
No page module was found under `app/frontend/pages/`. A page is only registered if its filename
matches `streamlit_app.PAGE_SPECS` exactly; any other filename is silently skipped so a missing page
cannot crash the shell.

**A test fails after I edited `app/llm/mock_provider.py`**
Expected. Several engine and dataset tests assert exact statuses per dataset, and the mock's rules
are what produce them. Read the failure as "the rules changed", not "the test is flaky", and bump
`MOCK_RULES_VERSION` so old results are not pooled with new ones.

**A failing test run leaves directories behind**
By design: `pytest_sessionfinish` removes the temporary tree only on a green run, so a failure can
be inspected. Clean up with `rm -rf $TMPDIR/itaudit-tests-*`.

**I want to start completely over**
`.venv/bin/python run.py reset` (type `yes`). This drops every table. Stored evidence files under
`data/uploads/` are **not** deleted by it; remove them by hand if you want a truly clean slate.

---

## Security notes for handling audit evidence

This is a **research prototype**. It is intended to run on one researcher's machine, on
**synthetic data only**. Read the following as constraints, not as advice on hardening it.

### What the system does protect

- **Evidence integrity.** Every upload is SHA-256 hashed on receipt, the hash is stored on the row,
  and stored filenames are content-addressed (`<stem>_<hash12><ext>`). Two different files sharing a
  name cannot overwrite each other. `storage.verify_stored()` re-hashes a stored file so the
  integrity claim can be demonstrated rather than asserted.
- **Path safety.** Uploaded filenames are untrusted input and are never used as a path component.
  `safe_filename()` reduces `../../etc/passwd` to `passwd`; `save_upload()` refuses to write outside
  the project's upload directory; `delete_stored()` refuses any path outside `upload_dir`.
- **Secret hygiene.** No secret is hard-coded. API keys come from the environment, are masked to a
  fingerprint (`sk-...abcd (len 51)`) anywhere they are displayed, and are never written to the
  database, to a report, to an evaluation run's `config`, or to a log line. `.env` is gitignored.
- **No exfiltration by default.** With `LLM_PROVIDER=mock` and `EMBEDDING_PROVIDER=local` **no
  evidence leaves the machine**: there is no network call anywhere in the assessment path.
- **Configuration is read-only over HTTP.** `POST /api/v1/settings` always refuses, precisely
  because an unauthenticated endpoint that could repoint the model client would expose the evidence
  the system holds.
- **The API discloses no filesystem paths and no connection string.** `stored_path` is stripped from
  every evidence payload; `GET /api/v1/settings` reports `database_backend: "sqlite"` rather than
  the URL, because a connection string can carry credentials.

### What it does not protect — read before using any real document

1. **There is no authentication, authorisation or rate limiting.** Anyone who can reach the port can
   read every project, download every report and upload evidence. Keep `API_HOST=127.0.0.1`.
2. **Evidence is stored unencrypted on disk**, and its text is stored again in the database as
   chunks. There is no encryption at rest and no key management. Full-disk encryption on the host is
   the only protection.
3. **Sending evidence to a hosted model sends it to a third party.** The moment `LLM_PROVIDER=openai`
   points at a hosted endpoint, retrieved chunks — real text from the uploaded documents — are
   transmitted to that provider and become subject to its retention and training policies. For
   anything sensitive, use a locally hosted model (Ollama/vLLM) or stay on the mock. This is a
   decision to make deliberately, not one to discover afterwards.
4. **Prompts and raw responses are persisted.** `Assessment.prompt_snapshot` contains the evidence
   text that was sent to the model, and `raw_response` contains the model's answer. This is
   deliberate — it is what makes a run auditable — but it means the database holds a second copy of
   the evidence, and anyone with the file has both.
5. **Deleting a project does not delete its files.** The cascade removes rows, not the bytes under
   `data/uploads/`. Use `DELETE /api/v1/evidence/{id}` (or the UI's evidence deletion) to remove
   stored bytes.
6. **No PII handling of any kind.** No redaction, no masking, no data-subject tooling, no retention
   policy. The synthetic data uses fabricated handles (`svc-account-014`, `p.adeyemi`) precisely so
   none of this is needed.
7. **CORS allows any loopback origin.** That is mitigation for local development, not security, and
   is irrelevant to anything that connects directly to the port.
8. **Reports embed evidence.** A generated report quotes cited evidence verbatim and prints every
   file's SHA-256. Treat a generated report as being as sensitive as the evidence it was built from.
9. **The activity trail is a convention, not a guarantee.** `activity_log` is append-only because
   nothing writes an update, not because the database prevents one.

### Operating rules for this prototype

- Use **synthetic or fully anonymised data only**. Every generated file in this repository declares
  itself as synthetic in its own text.
- Bind to localhost. Do not put it behind a public reverse proxy "just to try it".
- Keep `LLM_PROVIDER=mock` unless you have consciously decided that the evidence in the database may
  be sent to the endpoint you have configured.
- Keep `FORCE_HUMAN_REVIEW=true`. Every AI output is a draft for an auditor, and the system is built
  so that nothing else is possible.
- If you enable `MOCK_HALLUCINATION_RATE > 0`, set it back to `0.0` afterwards. It exists to
  exercise the validator; assessments produced with it on contain deliberately fabricated citations.

---

*Companion documents:* [`ARCHITECTURE.md`](ARCHITECTURE.md) ·
[`DATABASE_SCHEMA.md`](DATABASE_SCHEMA.md) · [`API.md`](API.md) · [`TESTING.md`](TESTING.md)
