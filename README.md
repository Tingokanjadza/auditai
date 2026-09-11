# LLM-Assisted IT Audit Risk and Control Assessment System

A university research prototype that uses retrieval-augmented generation to help a **human IT
auditor** test IT general controls against uploaded evidence — and that is built, throughout, to
make the difference between *what the evidence proves* and *what a language model inferred*
impossible to lose.

`Python 3.9 source` · `761 passing tests` · `works fully offline, no API key` ·
`optional Claude backend` · `MIT licence` · `all data synthetic`

> ## ⚠ Synthetic data only — do not point this at real audit evidence
>
> Every control, document, account name, ticket and configuration value in this repository is
> **fabricated** for research purposes. Nothing here describes a real organisation, system, person
> or audit.
>
> **Do not point this prototype at real, confidential or client audit evidence.** It has no
> authentication, no authorisation, no access control, no encryption at rest, no tamper protection
> and no data-retention controls. Uploaded files are written to the local filesystem in the clear
> and their text is written into a local database. With a hosted LLM provider configured, evidence
> text is transmitted to a third party. This applies with full force to anything you deploy: a
> cloud deployment of this repository is open to whoever finds the URL. It is a research
> instrument, not a tool fit to hold client data.

**In a hurry?** A clone, three commands and a browser: [Local setup](#4-local-setup). No API key,
no network access, about two minutes once the install has finished.

---

## Contents

| | |
|---|---|
| **What it is, before you run it** | [1. What this is](#1-what-this-is) · [2. What this is **not**](#2-what-this-is-not) · [3. The research principle](#3-the-research-principle) |
| **Getting it running** | [4. Local setup](#4-local-setup) · [5. Environment variables](#5-environment-variables) · [6. Claude API setup](#6-claude-api-setup) |
| **Putting it somewhere** | [7. GitHub setup](#7-github-setup) · [8. Cloud deployment](#8-cloud-deployment) · [9. Custom domain](#9-custom-domain) · [10. Troubleshooting](#10-troubleshooting) |
| **Reference** | [11. Feature tour](#11-feature-tour) · [12. Architecture](#12-architecture) · [13. Repository layout](#13-repository-layout) · [14. The evaluation component](#14-the-evaluation-component) · [15. Safety rules, and what enforces each](#15-safety-rules-and-what-enforces-each) · [16. Testing](#16-testing) · [17. Documentation index](#17-documentation-index) · [18. Limitations](#18-limitations) · [19. Academic use and citation](#19-academic-use-and-citation) |

---

## 1. What this is

An IT auditor testing a control — say, "every privileged account must use multi-factor
authentication" — collects evidence (a policy, an account export, a configuration screenshot),
reads it, and forms a conclusion. The reading is slow and the conclusion has to be defensible
line by line.

This system is a prototype of what a language model can and cannot safely contribute to that
task. It ingests evidence files, chunks and indexes them, retrieves the passages relevant to a
specific control, asks a model to assess the control against *only those passages* in a strict
JSON shape, then **mechanically re-checks every citation and every number** against the evidence
that was actually supplied before an auditor ever sees the answer. The auditor's decision is
recorded separately from the model's and is the only thing the system will treat as a conclusion.

### The research question

> Does adding retrieval and a structured audit workflow to a language model produce IT control
> assessments that are more **traceable** and more **honest about uncertainty** than an
> unstructured prompt over the same evidence — and can the difference be measured mechanically
> rather than asserted?

The system implements three experimental conditions over the same evidence, so the question is
answered by measurement rather than by demonstration:

| Condition | Pipeline |
|---|---|
| **A** — raw baseline | No retrieval. Raw file text, crudely truncated, a naive persona, no chunk IDs, no output schema, **no safety rails applied**. Deliberately weak; this is what the study measures *against*. |
| **B** — RAG | Hybrid retrieval + the full control requirement + a structured output schema. One model call. Citations are validated and recorded, but nothing is corrected. |
| **C** — RAG + workflow | Sufficiency pre-check → assessment → self-critique → citation validation → safety rails → prototype risk scoring → mandatory human-review gate. **This is the mode for normal use.** |

A is left unrailed on purpose. Railing the baseline would erase the very behaviour the experiment
exists to observe, and the A-vs-C comparison would be circular. See
[`app/audit/engine.py`](app/audit/engine.py) for the reasoning in full.

---

## 2. What this is *not*

Stated before anything else, because everything below only makes sense once these are accepted.

* **It does not replace the auditor.** No output of this system is an audit conclusion. Every
  assessment is flagged `human_review_required = True`, unconditionally, by
  [`app/audit/validators.py::enforce_safety_rails`](app/audit/validators.py) — there is no
  configuration that turns this off. A control with no recorded auditor decision appears in the
  generated report as `PENDING AUDITOR REVIEW` and is **excluded from every headline figure** in
  the executive summary.
* **It does not make final compliance decisions.** The AI record (`assessments`) and the human
  record (`human_reviews`) are separate database tables and are never merged. The report prints
  both, side by side, each with its own provenance label, and marks the pairs that disagree.
* **It never asserts legal or regulatory compliance.** The system prompt forbids it explicitly;
  the control library's framework references are labelled illustrative and unmapped; the report's
  Limitations section states that no legal, regulatory or certification opinion is expressed.
  "Compliant with ISO 27001" is not a sentence this system is permitted to produce.
* **It gives no assurance.** No opinion, no sign-off, no attestation. The generated document is a
  working paper for a human to correct and sign, or refuse to.
* **It cannot establish that evidence is authentic, complete or representative.** It records a
  SHA-256 of each file at upload. That shows the file has not changed *inside this system*. It
  says nothing about who produced it, whether it was edited beforehand, whether a listing is the
  complete population, or whether an extract covers the audit period.
* **Citation verification is textual, not semantic.** A `VERIFIED` verdict means the quoted
  characters exist in the chunk the model pointed at. It does **not** mean the quote supports the
  conclusion drawn from it. Text reassembled from a chunk's own vocabulary can score as verified
  while asserting something the chunk does not — measured and documented in
  [`app/audit/validators.py`](app/audit/validators.py).
* **The risk scores are not an industry framework.** They come from a prototype additive model
  with the author's own weights, labelled *"Prototype research risk scoring model — not an
  official industry framework"* on every artefact that shows one.
* **The default results say nothing about language models.** The offline provider is a
  deterministic rule-based program, not an LLM. See [§6](#6-claude-api-setup) and
  [§14](#14-the-evaluation-component).

---

## 3. The research principle

Four kinds of statement appear in an audit working paper, and the failure mode this system exists
to prevent is letting one drift into another.

| | Question it answers | Where it must live |
|---|---|---|
| **REQUIREMENT** | What does the policy or control *require*? | `control_requirement` — from the control definition and policy documents. A requirement is not an observation. |
| **EVIDENCE** | What does the supplied evidence *literally state*? | `assessment` + `evidence[]`, as verbatim quotations tagged with the `chunk_id` they were copied from. If you cannot point at a chunk and quote it, it is not proven. |
| **INFERENCE** | What travels even one step beyond the literal words? | `inferences`. "10 rows show FALSE, therefore 10 accounts lack MFA" is a small inference and is still labelled as one. |
| **HUMAN VERIFICATION** | What could not be established, and what must a reviewer check? | `human_verification_required` + `missing_evidence`. |

This is not a convention. It is the shape of
[`AssessmentOutput`](app/schemas/assessment.py), it is stated as a rule in
[`SYSTEM_PROMPT`](app/audit/prompts.py), it is the four-panel layout every assessment is displayed
in, and it is what the citation validator checks.

### The worked example: absence of evidence is not evidence of a deficiency

The single most damaging thing this system could do is turn a gap in an *export* into a finding
about a *control*. The system prompt carries this case as a few-shot anchor, and DATASET-005
tests it end to end.

**The evidence.** An MFA policy requiring a second factor on every privileged account, plus a
privileged user listing of 100 accounts whose columns are `User, Role, Department, Last_Login,
Account_Status` — **no MFA attribute at all**. (DATASET-001, -005 and -006 share the same policy
document byte for byte; only the operational evidence differs, so the only variable is the
presence of the attribute under test.)

**The correct answer is `INSUFFICIENT_EVIDENCE`** — not a deficiency, not a pass. The listing
proves a population of privileged accounts exists. It proves nothing whatsoever about MFA.

Both of the following were reproduced for this README against a clean temporary database
(`.venv/bin/python run.py evaluate`, offline provider, DATASET-005):

**Condition A (no retrieval, no schema, no rails) — wrong, and confidently so:**

```
status     : POTENTIAL_DEFICIENCY        (risk CRITICAL)
finding    : 2 record(s) do not meet the requirement for multi-factor
             authentication status.
citations  : 2, both PARTIAL - no resolvable chunk id
missing_evidence: []
```

It produced a count of failing records from a file that has no such column, and asked for
nothing.

**Condition C (retrieval + workflow + rails) — correct:**

```
status     : INSUFFICIENT_EVIDENCE       (risk HIGH)
assessment : The operational evidence supplied does not record multi-factor
             authentication status. The export reports the fields: User, Role,
             Department, Last_Login, Account_Status, Data_Origin. It covers 100
             records, but none of its fields state whether the control operated
             for them.
finding    : The control could neither be confirmed nor challenged: multi-factor
             authentication status is absent from the evidence provided.
missing_evidence:
  - A per-account multi-factor authentication status or enrolment export
    covering the in-scope account population
  - Privileged account listing ... showing ... MFA enrolment status (MFA_Status)
  - Configuration export or screenshot narrative of the conditional-access /
    MFA enforcement policy ...
citations  : 2, both VERIFIED (match score 1.00) against the stored chunks the
             model was shown  (chunk ids are per-database and will differ in yours)
```

"I cannot tell from this evidence" is a complete, correct, professional answer, and making it a
first-class outcome rather than a failure state is the point of the design. `INSUFFICIENT_EVIDENCE`
is counted separately from deficiencies in every figure the console shows.

---

## 4. Local setup

From a clone to a running demo. **No API key and no network are needed** — the default provider is
an offline deterministic rule engine.

### Prerequisites

| | |
|---|---|
| Python | **3.9+**. Developed, tested and verified on CPython **3.9.6** (macOS arm64); the source is written to 3.9 syntax and runs unchanged on newer interpreters, which is what the container and the PaaS blueprints use (3.12, see [`runtime.txt`](runtime.txt)). |
| Disk | ~400 MB for the virtual environment, plus a few MB of generated evidence. |
| Network | Needed once, for `pip install`. Nothing after that. |
| API key | None. |

### Clone, install, run

```bash
git clone <your-repository-url> "IT AUDIT TOOL"
cd "IT AUDIT TOOL"

# 1. install (about a minute with a warm pip cache)
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt

# 2. create the database and seed the 14-control synthetic library
.venv/bin/python run.py init
#    -> Database ready. Control library contains 14 controls (14 newly seeded).

# 3. start the audit console
.venv/bin/python run.py ui
#    -> http://localhost:8501
```

Then, in the browser:

1. The Dashboard opens empty with one button: **Load demo project + synthetic evidence**. Press
   it. It generates nine synthetic evidence files (DOCX, PDF, CSV, XLSX, TXT), ingests, chunks,
   hashes and indexes them, and scopes five controls into a *Privileged Access Management Audit*
   engagement. *Measured: 4.9 s headless on the reference machine; allow longer on the first
   press, which also imports the parsing stack.*
2. **Assessments** → select the in-scope controls → **Run** (mode C). *Measured: 2.5 s of engine
   time for all five controls with the offline provider.*
3. Open any result and read the four panels: the control **REQUIREMENT**, the cited **EVIDENCE**,
   the AI **INFERENCES**, and what needs **HUMAN VERIFICATION**.
4. **Human Review** → record a decision. **Reports** → **Generate**. Read Section 10 of the report
   before you show it to anyone.

> On the first ever `streamlit run` on a machine, Streamlit may ask for an email address before
> starting. Press **Enter** to skip; it never asks again. The deployment start script
> ([`scripts/start_ui.sh`](scripts/start_ui.sh)) passes `--server.headless true`, which suppresses
> the prompt entirely.

> **The console listens on every interface, not just loopback.**
> [`.streamlit/config.toml`](.streamlit/config.toml) sets `address = "0.0.0.0"` so the same file
> works in a container, and `run.py ui` inherits it — verified: the process listens on `*:8501`, so
> anyone on your network can reach an application that has no authentication. On an untrusted
> network, start it as `STREAMLIT_SERVER_ADDRESS=127.0.0.1 .venv/bin/python run.py ui` (verified:
> it then listens on `127.0.0.1:8501` only). The API already defaults to loopback (`API_HOST`).
> [`docs/SECURITY.md`](docs/SECURITY.md) records this and the other known gaps.

### Entry points

| Command | What it does |
|---|---|
| `.venv/bin/python run.py ui` | Streamlit console only. Talks to the service layer **in-process** — a complete application with no backend running. |
| `.venv/bin/python run.py api` | FastAPI backend only. Interactive docs at <http://127.0.0.1:8000/docs> (38 paths under `/api/v1`). |
| `.venv/bin/python run.py all` | Both, with Ctrl-C stopping the pair. This is the default with no argument. |
| `.venv/bin/python run.py init` | Create the database, seed the control library. Idempotent. |
| `.venv/bin/python run.py seed-demo` | Create the demo engagement and scope its five controls. **Does not ingest evidence** — the console button does that. |
| `.venv/bin/python run.py evaluate` | Run experiments A/B/C over the six synthetic datasets. *Measured: 9.3 s for all three conditions.* |
| `.venv/bin/python run.py reset` | **Destructive.** Drops every table, re-initialises, re-seeds the library. Asks for a typed `yes`. |
| `sh scripts/start_api.sh` / `sh scripts/start_ui.sh` | The deployment start commands: bind `0.0.0.0`, honour `$PORT`. Used by the Dockerfile, the Procfile and `render.yaml`. Use `run.py` locally instead — it keeps the API on localhost. |

**Step by step, from a fresh clone to a completed assessment, a recorded human review and a
generated report — with the exact output to expect at each step — is in
[`docs/QUICKSTART.md`](docs/QUICKSTART.md).**

---

## 5. Environment variables

Every setting has a working default, so the application runs with **no `.env` file at all**, fully
offline. To change anything:

```bash
cp .env.example .env      # then edit; .env is gitignored and must stay that way
```

Precedence, verified: **a variable exported in your shell beats `.env`, which beats the default.**
That is worth remembering when a value on the Settings page is not the one you wrote in `.env`.
Configuration is read at startup — restart after editing.

### Application

| Variable | Default | What it does |
|---|---|---|
| `ENVIRONMENT` | `development` | Free-text label, shown in `/health` and the report header. Set `production` on a deployment. |
| `DEBUG` | `false` | Extra detail in error output. Leave off anywhere others can reach. |
| `APP_NAME`, `APP_SHORT_NAME` | *(the project names)* | Display strings. |
| `APP_VERSION` | `0.1.0` | Stamped on every generated report and returned by `/health`. Bump it when results must be distinguishable. |
| `DEFAULT_AUDITOR_NAME` | `Research Auditor` | Pre-filled reviewer identity in the console sidebar. |

### Database and storage

| Variable | Default | What it does |
|---|---|---|
| `DATABASE_URL` | `sqlite:///<repo>/data/audit.db` | SQLAlchemy URL. The schema is portable: `postgresql+psycopg://user:pass@host:5432/itaudit` works, but the driver is **not installed by default** (see the commented line in [`requirements.txt`](requirements.txt)). |
| `DB_ECHO` | `false` | Log every SQL statement. Noisy; useful once. |
| `DATA_DIR` | `<repo>/data` | Parent of everything generated. |
| `UPLOAD_DIR` | `<repo>/data/uploads` | Stored evidence files, under `project_<id>/`. |
| `REPORT_DIR` | `<repo>/data/reports` | Generated working papers. |
| `SYNTHETIC_DIR` | `<repo>/data/synthetic` | Generated evaluation evidence. |
| `CONTROLS_DIR` | `<repo>/data/controls` | Holds `control_library.json` — the one tracked data file. |
| `SAMPLE_EVIDENCE_DIR` | `<repo>/data/sample_evidence` | Reserved for hand-authored samples. |
| `EVALUATION_OUTPUT_DIR` | `<repo>/data/evaluation` | Exported evaluation artefacts. |
| `MAX_UPLOAD_MB` | `50` | Larger uploads are refused with HTTP 413. Keep in step with `maxUploadSize` in [`.streamlit/config.toml`](.streamlit/config.toml), or one surface will accept what the other rejects. |

### LLM provider

| Variable | Default | What it does |
|---|---|---|
| `LLM_PROVIDER` | `mock` | `mock` (offline rule engine) · `claude`/`anthropic` (see [§6](#6-claude-api-setup)) · `openai` and its aliases (`azure`, `ollama`, `vllm`, `lmstudio`, `openrouter`, `groq`, `together`, `local` — all the same OpenAI-protocol class, distinguished by `LLM_BASE_URL`). An unrecognised value falls back to `mock` with a warning rather than failing to start. |
| `LLM_MODEL` | `gpt-4o-mini` | Model name for the OpenAI-compatible provider. Ignored by `mock` and by `claude` (which uses `ANTHROPIC_MODEL`). |
| `LLM_API_KEY` | *(unset)* | Key for the OpenAI-compatible provider. Masked everywhere it is displayed. |
| `LLM_BASE_URL` | *(unset)* | Any OpenAI-compatible endpoint, e.g. `http://localhost:11434/v1` for Ollama. Setting it alone is enough to count as configured — local servers need no key. |
| `LLM_TEMPERATURE` | `0.0` | Sampling temperature. Sent by the OpenAI provider; **deliberately never sent to Claude**, which rejects sampling parameters. |
| `LLM_MAX_TOKENS` | `2500` | Output ceiling per call. |
| `LLM_TIMEOUT` | `120` | Seconds per model call. |
| `LLM_MAX_RETRIES` | `2` | Retries with backoff on a transient provider error. |
| `LLM_USE_JSON_MODE` | `true` | Ask for schema-constrained output when the endpoint supports it, degrading gracefully when it does not. |
| `MOCK_HALLUCINATION_RATE` | `0.0` | **Research lever.** Fraction of mock assessments that deliberately fabricate a citation or an unsupported number, so the validation layer can be exercised and measured. Never anything but `0.0` outside an experiment. |
| `MOCK_SEED` | `1337` | Seed for the mock's deterministic behaviour. Record it with any result. |

### Claude (Anthropic)

| Variable | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | *(unset)* | The key. Without it, `LLM_PROVIDER=claude` falls back to the offline mock **with a visible notice** — it never fails silently and never fails hard. |
| `ANTHROPIC_MODEL` | `claude-opus-5` | Model id. |
| `ANTHROPIC_EFFORT` | `high` | Reasoning depth: `low` · `medium` · `high` · `xhigh` · `max`. An unrecognised value is clamped to `high` rather than sent (an invalid effort is a 400 mid-assessment). Use `low` for cheap bulk runs. |
| `ANTHROPIC_THINKING` | `true` | Adaptive thinking. Current models reject a fixed token budget, so depth is chosen by the model within the effort level. |
| `ANTHROPIC_BASE_URL` | *(unset)* | Only for Anthropic-compatible gateways. **If this is already exported in your shell** — common if you use other Anthropic tooling — it is picked up automatically. The Settings page shows the URL actually in use. |

### Embeddings

Separate from the LLM, and **local by default, so retrieval never needs the network** whichever
LLM provider is configured.

| Variable | Default | What it does |
|---|---|---|
| `EMBEDDING_PROVIDER` | `local` | `local` = stateless scikit-learn hashing vectoriser, deterministic, offline. `openai` = a remote embedding endpoint. A remote provider that is unavailable falls back to local with a warning. |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Remote model name; ignored when `local`. |
| `EMBEDDING_DIM` | `1024` | Vector width. **Changing it invalidates every stored vector** — re-index after a change. |
| `EMBEDDING_API_KEY`, `EMBEDDING_BASE_URL` | *(unset)* | Credentials for the remote embedding endpoint. |

### Retrieval

| Variable | Default | What it does |
|---|---|---|
| `RETRIEVAL_STRATEGY` | `HYBRID` | `KEYWORD` (BM25) · `VECTOR` (cosine) · `HYBRID` (reciprocal-rank fusion, k=60). |
| `RETRIEVAL_TOP_K` | `12` | Chunks handed to the model. Capped at `max(2, top_k // 2)` per evidence file so one big spreadsheet cannot crowd out the policy. |
| `RETRIEVAL_CANDIDATE_K` | `60` | Candidates considered before fusion and capping. |
| `RETRIEVAL_MIN_SCORE` | `0.0` | Floor below which a chunk is discarded. |
| `CHUNK_SIZE` | `1200` | Characters per text chunk. |
| `CHUNK_OVERLAP` | `180` | Overlap between adjacent chunks, so a sentence cut in half is still findable. |
| `TABLE_ROWS_PER_CHUNK` | `25` | Spreadsheet rows per `TABLE_ROWS` chunk. |
| `MAX_EVIDENCE_CHARS` | `24000` | Hard ceiling on evidence characters in one model call. |
| `RAW_EVIDENCE_CHAR_BUDGET` | `24000` | The equivalent budget for condition A, which gets raw untargeted text. |

### Assessment safety

| Variable | Default | What it does |
|---|---|---|
| `FORCE_HUMAN_REVIEW` | `true` | Human review is mandatory by design. Leaving this `true` is the only supported mode; rail 4 in [§15](#15-safety-rules-and-what-enforces-each) sets the flag unconditionally regardless. |
| `CITATION_MATCH_THRESHOLD` | `0.6` | Similarity at or above which a quotation counts as `VERIFIED`; at or above half of it, `PARTIAL`; below, `FABRICATED`. Changing it changes every grounding figure you report. |

### Services and transport

| Variable | Default | What it does |
|---|---|---|
| `API_HOST` | `127.0.0.1` | Bind address for `run.py api`. Loopback on purpose: the API has no authentication. (The deployment script binds `0.0.0.0`, because a container that binds loopback is reachable by nothing.) |
| `API_PORT` | `8000` | Port for `run.py api`, and the fallback for `scripts/start_api.sh` when `$PORT` is absent. |
| `API_BASE_URL` | `http://127.0.0.1:8000` | Where the **console** looks for the API when `USE_API=true`. This is the address actually called; `PUBLIC_API_URL` is only for display. |
| `API_REQUEST_TIMEOUT` | `300` | Console-to-API timeout. Assessment runs are synchronous, so this needs headroom. |
| `STREAMLIT_PORT` | `8501` | Port for `run.py ui`, and the fallback for `scripts/start_ui.sh`. |
| `USE_API` | `false` | `false` = the console calls the service layer in-process (one process, simplest, the default). `true` = it calls the FastAPI backend over HTTP. Verified both ways. **Trap: this one must be a real environment variable — putting it in `.env` does nothing.** The console reads `os.environ` directly, because it selects a *transport* rather than an application setting, and `pydantic-settings` loads `.env` into `Settings` without exporting it to the process. Verified: with `USE_API=true` in `.env`, `Settings.use_api` is `True` while the console's `use_api()` is still `False`. Locally, export it: `USE_API=true .venv/bin/python run.py ui`. `render.yaml`, `docker-compose.yml` and Streamlit Cloud secrets all set it as a real variable, so all three are correct. |

### Deployment and CORS

| Variable | Default | What it does |
|---|---|---|
| `PUBLIC_APP_URL` | *(unset)* | The console's public address. Published in the API's `/` payload and OpenAPI description, and **trusted by CORS**. |
| `PUBLIC_API_URL` | *(unset)* | The API's public address. Becomes the first entry in the OpenAPI `servers` block, so Swagger UI's "Try it out" targets the deployment instead of localhost. |
| `CORS_ALLOW_ORIGINS` | *(empty)* | Comma-separated origins permitted by the browser policy. Whitespace and trailing slashes are normalised, so `https://x.edu/` and `https://x.edu` are the same origin. |
| `CORS_RESTRICT_TO_CONFIGURED` | `false` | `false` (development) = loopback on any port **plus** the configured origins. `true` (production) = the configured origins and `PUBLIC_APP_URL` **only**; loopback is no longer trusted. With `true` and nothing configured, **no** cross-origin request is allowed and a warning says so — refusing traffic is the safer failure. |

> CORS constrains browsers, not attackers. It is not an access control and must never be mistaken
> for one. An internet-facing deployment of this prototype needs an authenticating proxy in front
> of it.

### Variables the platform sets (not application settings)

| Variable | Read by | What it does |
|---|---|---|
| `PORT` | `scripts/start_*.sh` | Injected by Render/Railway/Heroku/Fly. Takes precedence over `API_PORT`/`STREAMLIT_PORT`. |
| `HOST` | `scripts/start_*.sh` | Bind address; defaults to `0.0.0.0` in those scripts. |
| `WEB_CONCURRENCY` | `scripts/start_api.sh` | uvicorn workers, default `1`. **Raise it only together with a PostgreSQL `DATABASE_URL`** — concurrent writers on one SQLite file produce "database is locked", not throughput. |
| `FORWARDED_ALLOW_IPS` | `scripts/start_api.sh` | Which proxies uvicorn trusts for `X-Forwarded-*`; default `*`. |
| `PYTHON` | `scripts/start_*.sh` | Override the interpreter. Otherwise `.venv/bin/python` if present, else `python3`. |
| `APP_ROLE` | Docker `HEALTHCHECK` | `api` · `ui` · `all`. A health check is a separate process and cannot see the container's arguments, so when you override the command, set this to match. |
| `PYTHON_VERSION` | Render | Interpreter version; kept in step with [`runtime.txt`](runtime.txt). |
| `STREAMLIT_CLIENT_SHOW_ERROR_DETAILS` | Streamlit | Set to `none` on a deployment so an unhandled error cannot print filesystem paths into a public browser. |
| `STREAMLIT_BROWSER_GATHER_USAGE_STATS` | Streamlit | `false` everywhere here. |

---

## 6. Claude API setup

The default provider is **`mock`**: [`app/llm/mock_provider.py`](app/llm/mock_provider.py). Read
this before quoting any number this repository produces.

> ### The mock is not a language model
>
> It is a **deterministic rule-based program**. It reads the retrieved chunk text, applies
> hand-written auditing heuristics, and emits the JSON shape a model would be asked to produce —
> including real citations that point at real `chunk_id`s with text copied verbatim out of those
> chunks. It makes no network call and contains no model.
>
> Therefore **every default result in this repository measures the pipeline** — retrieval, prompt
> scaffolding, the output contract, citation validation, the rails, the workflow — **and says
> nothing whatsoever about the capability of any language model.** The honest sentence for a
> write-up is *"the pipeline reached the planted conclusion in k of 6 cases"*, never *"the LLM was
> k/6 accurate"*. Its rule set is versioned (`mock-rules-1.1`); results from different rule-set
> versions must not be pooled.

### Turning Claude on — two variables

```bash
# in .env  (or as environment variables / platform secrets)
LLM_PROVIDER=claude
ANTHROPIC_API_KEY=sk-ant-...
```

Get a key at **<https://console.anthropic.com/settings/keys>**. Everything else has a default:

| Setting | Default | Notes |
|---|---|---|
| `ANTHROPIC_MODEL` | `claude-opus-5` | |
| `ANTHROPIC_EFFORT` | `high` | `low`/`medium`/`high`/`xhigh`/`max`. Audit reasoning is non-trivial, hence `high`; drop to `low` for cheap bulk experiment runs. An invalid value is clamped to `high`, not forwarded. |
| `ANTHROPIC_THINKING` | `true` | Adaptive thinking — the model decides how much reasoning an assessment needs. |
| `ANTHROPIC_BASE_URL` | *(unset)* | Only for Anthropic-compatible gateways. |

Restart, then confirm on the **Settings** page or in the sidebar badge, which reads
**AI Provider: Claude** when Claude actually answered. Or ask the API:

```bash
curl -s http://127.0.0.1:8000/health | python3 -m json.tool | head -20
```

### The fallback guarantee

**A missing key degrades to the offline provider with a visible notice. It never fails, and it
never pretends.** Verified with `LLM_PROVIDER=claude` and no key present:

```
WARNING  LLM provider 'claude' was selected but ANTHROPIC_API_KEY is not set (or the
         'anthropic' package is missing). Falling back to the offline mock provider -
         any results produced now measure the pipeline, not a language model.

{'configured_provider': 'claude', 'active_provider': 'mock',
 'active_model': 'mock-deterministic-rules', 'fell_back_to_mock': True,
 'fallback_reason': "ANTHROPIC_API_KEY is not configured, or the 'anthropic' package
                     is not installed."}
```

That `fell_back_to_mock` flag is surfaced in `GET /health`, on the Settings page, and in the
sidebar badge — in **red**, naming both providers — because an experiment that silently ran on the
mock while its author believed it ran on Claude would be worthless.

With the key present, the same call reports `active_provider: claude`,
`active_model: claude-opus-5`, `fell_back_to_mock: False`.

### Two implementation notes worth knowing

* **`temperature` is never sent to Claude.** Current Claude models reject sampling parameters with
  a 400. `LLMProvider.complete()` still accepts `temperature` because other providers need it;
  [`app/llm/anthropic_provider.py`](app/llm/anthropic_provider.py) deliberately discards it and
  controls determinism through `effort` instead. Please do not "fix" this.
* **The SDK floor is deliberate.** `requirements.txt` declares `anthropic>=0.125` rather than an
  exact version: the SDK dropped Python 3.9 at its 1.0 release, so the resolver installs a 0.x on a 3.9
  interpreter and a 1.x on a 3.10+ deployment. The provider is written against the surface both
  support. The honest cost: a future major release could change that surface, and a fresh
  deployment would install something this code has not been tested against.

### Other providers

Any OpenAI-compatible endpoint works through `LLM_BASE_URL`, including local models that need no
key and no network egress:

```bash
LLM_PROVIDER=openai
LLM_BASE_URL=http://localhost:11434/v1     # Ollama
LLM_MODEL=llama3.1:8b
```

(Also vLLM, LM Studio, llama.cpp's server, OpenRouter and Azure-compatible gateways.) The same
fallback rule applies: unconfigured means mock, loudly.

**One thing to accept before running with any hosted model: evidence text leaves the machine.**
With synthetic data that is fine, and it is one more reason not to point this prototype at real
evidence. Assessment and evaluation runs are also **synchronous** — the six-dataset, three-condition
suite takes seconds against the mock and minutes against a hosted model.

---

## 7. GitHub setup

This directory is already a git repository with its history in place. Publishing it is a handful
of commands — but check the ignore rules first.

### Before you push: prove no secret is tracked

```bash
git check-ignore -v .env data/audit.db .venv
```

```
.gitignore:4:.env	.env
.gitignore:24:*.db	data/audit.db
.gitignore:14:.venv/	.venv
```

Three lines back means all three are ignored. `git add .env` is then refused outright:

```
The following paths are ignored by one of your .gitignore files:
.env
hint: Use -f if you really want to add them.
```

> **Never commit `.env`, and never use `git add -f` on it.** A key pushed to GitHub is public the
> moment it lands, even in a private repository that later becomes public, even if you delete it
> in the next commit — the object stays in the history. If it happens, revoke the key at the
> provider first and rewrite history second, in that order. `.env.example` is the file that is
> meant to be committed; it contains no values.
>
> Also gitignored, and correctly so: `data/uploads/`, `data/reports/`, `data/evaluation/`,
> `data/synthetic/`, every `*.db`, and `.streamlit/secrets.toml`. Only `data/**/.gitkeep` and
> `data/controls/control_library.json` are tracked under `data/`.

### Push it

```bash
git status                      # should be clean, or commit what you want to keep
git branch --show-current       # this repository ships on a feature branch
git branch -M main              # rename it to main, if that is your default

# create an empty repository on github.com first - no README, no .gitignore, no licence,
# because this repository already has all three - then:
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

```
 * [new branch]      main -> main
branch 'main' set up to track 'origin/main'.
```

*(Verified end to end against a local bare remote. 132 files are tracked.)*

### Before anyone else reads it

| | |
|---|---|
| **Name the licence holder.** | [`LICENSE`](LICENSE) is MIT with a placeholder holder line. Replace it with your own name — an MIT licence with a vague holder is weaker than one that names a person. |
| **Fill in the citation block.** | [§19](#19-academic-use-and-citation) has a BibTeX entry with `[Author]` and `[repository URL]` to replace. |
| **Decide whether the repository is public.** | It contains only synthetic data, so publishing is safe from a data point of view. What publishing does *not* do is make the deployed app safe — see [§8](#8-cloud-deployment). |
| **Keep the honesty machinery.** | The caveats, the `n` beside every rate, the provenance labels, the mock-provider notice. They are what stops a figure from this prototype being read as something it is not. |

---

## 8. Cloud deployment

> ### Read this before deploying anything
>
> **There is no authentication.** Anyone who finds the URL can upload evidence, read it back, run
> assessments, generate reports and delete projects. Deploy it with **synthetic data only**, or
> put an authenticating proxy or a private network in front of it. `CORS_RESTRICT_TO_CONFIGURED`
> constrains browsers; it does not constrain anyone determined.
>
> **Free tiers have ephemeral filesystems.** The SQLite database, the uploaded evidence and the
> generated reports are lost on every redeploy and on idle shutdown. That is acceptable for a demo
> and unacceptable for anything you need next week.

Three routes, easiest first. All three are described from the files that exist in this repository.
This section is the summary; [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) is the long form — the same
paths as numbered steps, plus PostgreSQL, TLS, a pre-deployment checklist, a verification procedure
and what was and was not proven by execution. [`docs/SECURITY.md`](docs/SECURITY.md) is its
companion and should be read **before**, not after.

| Route | Files | Gets you | Effort |
|---|---|---|---|
| **Streamlit Community Cloud** | [`requirements.txt`](requirements.txt), [`.streamlit/config.toml`](.streamlit/config.toml) | The console, one process, in-process service layer. No API. | Minutes |
| **Render** (or Railway/Heroku) | [`render.yaml`](render.yaml), [`Procfile`](Procfile), [`scripts/`](scripts), [`runtime.txt`](runtime.txt) | Console **and** API as two services, UI talking to API over HTTP. | ~30 min incl. a second pass for URLs |
| **Docker / compose** | [`Dockerfile`](Dockerfile), [`docker-compose.yml`](docker-compose.yml), [`.dockerignore`](.dockerignore) | The same two-service shape, anywhere that runs containers. | Depends on your host |

### 8.1 Streamlit Community Cloud — start here

The simplest deployment, and the closest to how the console is meant to be used: **one process,
`USE_API=false`, the service layer in-process.**

1. Push to GitHub ([§7](#7-github-setup)).
2. <https://share.streamlit.io> → **Create app** → deploy from GitHub, pick the repository and
   branch.
3. **Main file path**: `app/frontend/streamlit_app.py` — the entry script puts the repository root
   on `sys.path` itself, so `import app...` resolves with no extra configuration.
4. **Advanced settings → Python version**: choose **3.12**. The source is 3.9 syntax and runs
   unchanged on it. (`runtime.txt` is for Heroku-style platforms and Render; Community Cloud does
   not read it.)
5. **Advanced settings → Secrets** — only if you want a real model. Paste TOML:

   ```toml
   LLM_PROVIDER = "claude"
   ANTHROPIC_API_KEY = "sk-ant-..."
   ENVIRONMENT = "production"
   ```

   Streamlit loads secrets at **server start**, before your script runs, and copies each
   **top-level** scalar into `os.environ` — which is exactly where this application's
   configuration reads from. *(Verified against the installed Streamlit 1.50: loading a
   `secrets.toml` puts `ANTHROPIC_API_KEY` into `os.environ`.)* A value nested inside a `[table]`
   is **not** exported and will never be seen. `STREAMLIT_*` keys do not belong here either —
   Streamlit reads its own configuration before secrets are loaded; edit
   [`.streamlit/config.toml`](.streamlit/config.toml) and redeploy instead. With no secrets at all
   the app runs on the offline mock, which is a perfectly good demo.
6. **Deploy.** The app installs `requirements.txt`, starts, creates its database, seeds the
   14-control library, and opens on the empty Dashboard with the demo button.
7. **Restrict who can view it** in the app's settings. On every path in this section that is the
   only access control there is — the application itself has none.

**What to expect:**

| | |
|---|---|
| Theme | [`.streamlit/config.toml`](.streamlit/config.toml) is committed, so the dark audit-console theme deploys with the app. The platform manages the port and address itself. |
| Storage | **Ephemeral.** The database and any uploaded evidence vanish when the app sleeps or redeploys. Press the demo button again to rebuild the demo. |
| API | Not deployed. `USE_API` must stay `false` — there is no second process on this platform. The console is fully functional without it. |
| Resources | The community tier is small; the first demo load and the first assessment are noticeably slower than the local timings above. |
| Exposure | Public unless you restrict viewers in the app settings. Synthetic data only. |

### 8.2 Render (and Railway, Heroku, Fly)

[`render.yaml`](render.yaml) is a Blueprint declaring **two web services from one repository**:

| Service | Start command | Health check | Notes |
|---|---|---|---|
| `llm-it-auditor-api` | `sh scripts/start_api.sh` | `/health` | `CORS_RESTRICT_TO_CONFIGURED=true`, `FORCE_HUMAN_REVIEW=true`, `MOCK_HALLUCINATION_RATE=0.0` pinned so a deployment cannot quietly lose a safety rail. |
| `llm-it-auditor-ui` | `sh scripts/start_ui.sh` | `/_stcore/health` | `USE_API=true`, and **no `ANTHROPIC_API_KEY`** — with `USE_API=true` the console never calls a model itself, so the key does not belong in the process serving the browser. |

**Deploy:**

1. Render → **New** → **Blueprint** → point it at the repository. Both services build from
   `requirements.txt`.
2. The first deploy comes up with five values deliberately blank (`sync: false` in the blueprint,
   so Render asks rather than storing them in git): `ANTHROPIC_API_KEY`, `API_BASE_URL`,
   `PUBLIC_API_URL`, `PUBLIC_APP_URL`, `CORS_ALLOW_ORIGINS`. Render only assigns each service its
   hostname at creation time, and a blueprint cannot compose `https://` with a hostname.
3. Copy the two hostnames from the service pages and fill them in:

   | Service | Variable | Value |
   |---|---|---|
   | api | `PUBLIC_API_URL` | `https://llm-it-auditor-api.onrender.com` |
   | api | `PUBLIC_APP_URL` | `https://llm-it-auditor-ui.onrender.com` |
   | api | `CORS_ALLOW_ORIGINS` | `https://llm-it-auditor-ui.onrender.com` |
   | ui | `API_BASE_URL` | `https://llm-it-auditor-api.onrender.com` |
   | ui | `PUBLIC_API_URL` / `PUBLIC_APP_URL` | the same two, for display |

4. Redeploy both. **Until step 3 is done the API allows no cross-origin browser request at all** —
   the intended failure mode, since refusing traffic is safer than falling back to allowing
   everything.

**Persistence.** Render's free tier filesystem is ephemeral. `render.yaml` documents both fixes in
place, neither enabled by default: a paid disk mounted at
`/opt/render/project/src/data`, or PostgreSQL (`DATABASE_URL` with a `postgresql+psycopg://`
prefix, plus `psycopg[binary]>=3.1` in the build command — the schema is already portable).

**Railway, Heroku, Dokku** read [`Procfile`](Procfile) instead, which declares the same two
processes:

```
web: sh scripts/start_api.sh
ui:  sh scripts/start_ui.sh
```

A platform routes HTTP to exactly one process type per service, so the console and the API are two
deployments of this repository, not one. Both scripts read `$PORT` and bind `0.0.0.0`, so neither
needs a platform-specific flag. [`runtime.txt`](runtime.txt) pins `python-3.12.11`; if a platform
rejects that patch version, bump it there and in `render.yaml`'s `PYTHON_VERSION` **together**.

### 8.3 Docker and compose

[`Dockerfile`](Dockerfile) is a multi-stage build — dependencies compiled in a builder image, only
the finished virtualenv copied into the runtime image, which runs as an unprivileged uid 10001 and
carries no compiler and no pip cache. One image runs either service:

```bash
docker build -t llm-it-auditor .

docker run --rm -p 8501:8501 llm-it-auditor                          # console (default)
docker run --rm -p 8000:8000 -e APP_ROLE=api llm-it-auditor api      # API
docker run --rm -p 8501:8501 -p 8000:8000 -e APP_ROLE=all llm-it-auditor all
```

Anything that is not `api`/`ui`/`all` is executed verbatim, so `docker run <image> python run.py
init` still works. `APP_ROLE` exists because the `HEALTHCHECK` runs as its own process and cannot
see the container's arguments — set it to match whatever you pass as the command.

[`docker-compose.yml`](docker-compose.yml) brings up the deployed two-service shape locally, with
a named volume so evidence and reports survive a restart:

```bash
docker compose up --build     # console :8501, API docs :8000/docs
docker compose down           # stop, keep the data
docker compose down -v        # stop and delete the database, evidence and reports
```

It needs no configuration: `.env` is optional (`required: false`), the default provider is the
offline mock, and `ANTHROPIC_API_KEY` appears only as `${ANTHROPIC_API_KEY:-}` — passed through
from your shell if set, absent otherwise. No key is written in any file in this repository.
A commented PostgreSQL service at the bottom documents the four steps to switch.

> **Honesty note.** Docker is not installed on the machine this repository was built on, so
> `docker build` and `docker compose up` have **never been executed** here. The image definition
> was reviewed statically and the scripts it calls were executed outside a container (both start
> scripts verified: the API answered `/health`, the console answered `/_stcore/health`). Treat your
> first build as the first real test of the image: the pip install inside it, the wheels resolving
> on `python:3.12-slim-bookworm`, the `COPY --chown` ownership and the health check firing are all
> unproven by execution.

---

## 9. Custom domain

Say you want the console at `https://audit.example.edu` and the API at
`https://api.example.edu`.

**1. Point DNS at the platform.** In your DNS provider, add a `CNAME` for each subdomain to the
host the platform gives you (`<app>.onrender.com`, `<app>.streamlit.app`, …), then add the domain
in the platform's own settings page so it issues a certificate. An apex domain (`example.edu` with
no subdomain) usually needs an `A`/`ALIAS` record instead — follow the platform's instructions,
which differ.

**2. Tell the application its own addresses.** This is the part that is easy to forget, and the
symptom of forgetting it is a console that loads and then cannot talk to its API.

| Variable | Set on | Value | Why |
|---|---|---|---|
| `PUBLIC_APP_URL` | both services | `https://audit.example.edu` | Published in the API's `/` payload and description, and **trusted by CORS**, which is what lets the console's browser tab call the API. |
| `PUBLIC_API_URL` | both services | `https://api.example.edu` | Becomes the first `servers` entry in the OpenAPI document, so Swagger UI's "Try it out" hits the deployment rather than localhost. |
| `CORS_ALLOW_ORIGINS` | the API | `https://audit.example.edu` | Every browser origin allowed to call the API. Comma-separated for more than one. Trailing slashes and spaces are normalised, so `https://audit.example.edu/` works too. |
| `CORS_RESTRICT_TO_CONFIGURED` | the API | `true` | Stops trusting loopback and permits **only** the origins above. On a deployed host "localhost" is the server's own loopback and says nothing about who is calling. |
| `API_BASE_URL` | the console | `https://api.example.edu` | The address the console actually calls when `USE_API=true`. Distinct from `PUBLIC_API_URL`, which is only for display — inside Docker compose they are deliberately different (`http://api:8000` versus `http://localhost:8000`). |

**3. Redeploy, then confirm what the API believes.** The index route reports its effective policy;
this is real output from a locally started API with those variables set:

```bash
curl -s https://api.example.edu/ | python3 -m json.tool
```

```json
{
    "public_api_url": "https://api.example.edu",
    "console_url": "https://audit.example.edu",
    "cors": {
        "mode": "restricted",
        "allowed_origins": ["https://audit.example.edu"],
        "loopback_allowed": false
    },
    "authentication": "none"
}
```

`"mode": "restricted"` with your console listed is the state you want. An empty `allowed_origins`
with `"mode": "restricted"` means **every** cross-origin browser call will be refused — the logs
say so explicitly at startup.

> A custom domain adds TLS and a memorable address. It adds **no authentication**. If the
> application is going to live at a URL people can find, put an authenticating proxy (or your
> institution's SSO) in front of it, and keep the data synthetic.

---

## 10. Troubleshooting

| Symptom | What is actually happening | Fix |
|---|---|---|
| `Port 8501 is already in use` (Streamlit) or `[Errno 48] error while attempting to bind on address ('0.0.0.0', 8000): address already in use` (uvicorn) | A previous run is still alive. Streamlit exits; uvicorn logs *Application startup complete* first and then dies, which reads confusingly. | `lsof -ti:8501 \| xargs kill -9` (and `:8000`). Or run on another port: `PORT=8600 sh scripts/start_ui.sh`. |
| An assessment cites nothing; the retrieval panel is empty | Retrieval found nothing above the score floor — usually the control's **retrieval keywords** do not match the column headers in your export. | Confirm on the **Evidence** page that the file's parse status is `PARSED` and its chunk count is non-zero, then run the retrieval check [below](#a-diagnostic-worth-keeping) to see what retrieval actually returns. Fix the control's retrieval keywords on the **Controls** page: they should include the literal column names you expect (`MFA_Status`, `Last_Login`). |
| **Everything** returns `INSUFFICIENT_EVIDENCE` | Most often this is **correct** — you have uploaded policies but no system export, or an export that lacks the attribute under test. The system is built to say so rather than guess. | Read the *missing evidence* list on the assessment; it names the artefact it wants. Then check the evidence really contains that attribute. Upload **both** a policy and an export: a policy alone states a requirement, an export alone states a state, and testing a control needs both. If the evidence genuinely has the attribute, this is a retrieval problem — see the row above. |
| A `.xls` upload produces `Workbook could not be opened (ValueError: Excel file format cannot be determined, you must specify an engine manually.)` | The file is not a real BIFF workbook — most often an `.xlsx` or a CSV renamed to `.xls`. The file is recorded with the warning rather than silently dropped. | Re-save it properly as `.xlsx`. *(A genuine `.xlsx` renamed to `.xls` actually parses fine; `xlrd==2.0.2` is pinned for real legacy workbooks. If it is missing you get a different message naming `xlrd`.)* |
| Claude key set, but the badge still says **Mock** | Either the key is not reaching the process, or the fallback fired. The log line is decisive: *"LLM provider 'claude' was selected but ANTHROPIC_API_KEY is not set…"*. | Check, in order: (1) `LLM_PROVIDER=claude` as well as the key — the key alone changes nothing; (2) `.env` is in the **repository root**, not in `app/`; (3) the process was restarted — configuration is read at startup; (4) nothing in your shell overrides it — an exported `ANTHROPIC_BASE_URL` or `ANTHROPIC_MODEL` **beats `.env`**, and pointing at a gateway that rejects the key looks identical to a missing key; (5) on a platform, the secret is set on the *right service*. Then read the Settings page, which shows key presence, SDK version, effort, thinking and the **effective** base URL. |
| `database is locked` | Two processes are writing to one SQLite file — typically the console in-process (`USE_API=false`) at the same time as an API, or `WEB_CONCURRENCY` above 1. | Pick one writer: run the console in-process with no API, or export `USE_API=true` so the console goes through the API. Keep `WEB_CONCURRENCY=1` on SQLite. If you need real concurrency, move to PostgreSQL — the schema is already portable. |
| The demo looks stale: old projects, duplicated evidence, figures that do not match a fresh run | The database persists between runs, evaluation runs leave their throwaway projects behind (marked `is_demo`, excluded from operational figures but visible in the project selector), and the demo button skips files already ingested under the same name. | `.venv/bin/python run.py reset` — drops every table, re-initialises, re-seeds the library, asks for a typed `yes`. Then press the demo button again. To keep the database but remove one engagement, delete the project from the **Audit Projects** page; deletion cascades to its evidence, assessments and reviews. |
| `run.py evaluate` prints `accuracy=0.000` and the log says `Control 'CONTROL-001' not found.` | `evaluate` calls `init_db()` but does **not** seed the control library, so against a database that has never been initialised every dataset errors. Confirmed still present. | Always `.venv/bin/python run.py init` first. Then `evaluate` prints `A_RAW_LLM accuracy=0.667 … B_RAG accuracy=1.000 …`. |
| Results changed between two mock runs | With the mock provider they should not. | Check `MOCK_HALLUCINATION_RATE=0.0`, `MOCK_SEED` unchanged, and the prompt version on the Settings page. A changed mock rule-set version also changes results, and results from different versions must not be pooled. |
| A deployed console loads but every action errors | With `USE_API=true` the console is calling an API that it cannot reach, or that refuses its origin. | Check `API_BASE_URL` on the console service, then `curl https://<api>/health`, then `curl https://<api>/` and read the `cors` block — see [§9](#9-custom-domain). |
| `USE_API=true` seems to be ignored — the console still writes to the local database | It is in `.env`. The console reads `USE_API` from the process environment, not from `Settings`. | Export it instead: `USE_API=true .venv/bin/python run.py ui`. See the `USE_API` row in [§5](#services-and-transport). |
| Everything vanished after a redeploy | Free-tier filesystems are ephemeral. Nothing is wrong. | Attach a disk or move to PostgreSQL — see [§8.2](#82-render-and-railway-heroku-fly). |
| A deep link straight to one page (`/settings`) renders the page with no sidebar, no theme and a raw file-stem menu | Streamlit's automatic `pages/` discovery and the `st.navigation` shell both claim the URL; the shell only runs when the app is entered at `/`. Pre-existing app structure, not a deployment fault. | Open the app at its root and navigate from the sidebar. Share the root URL, not a page URL. |

### A diagnostic worth keeping

When an assessment cites nothing, this prints exactly what retrieval handed the model — score and
source locator per chunk — without running an assessment or calling a provider:

```bash
.venv/bin/python -c "
from app.database.base import session_scope
from app.rag.retriever import get_retriever
from app.rag.query_builder import build_queries
from app.audit.service import require_control
with session_scope() as s:
    control = require_control(s, 'CONTROL-001')
    result = get_retriever(s).retrieve(build_queries(control), project_id=1, top_k=5)
    for chunk in result.chunks:
        print(round(chunk.score, 3), '|', chunk.locator.render())
"
```

```
0.033 | Multi_Factor_Authentication_Policy_v3.docx - section '4. Evidence of operation' - paragraph 14
0.032 | Multi_Factor_Authentication_Policy_v3.docx - section '3. Exceptions' - paragraph 11
0.03  | Patch_Management_Standard_v2.pdf - page 1 - section 'Patch Management Standard'
...
```

No rows means retrieval, not the model, is the problem. Rows that are all policy documents and no
system export mean the export is not being matched — a keyword problem.

More symptom-level detail: [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) §Troubleshooting,
[`docs/SETUP.md`](docs/SETUP.md), and [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) §Troubleshooting for
anything that only goes wrong once the application is on a server.

---

## 11. Feature tour

The ten feature areas are the ten workspace pages. Each is listed with what it does, the modules
that implement it, and the API route that exposes the same capability.

**1. Dashboard** — engagement overview: controls in scope, assessed, effective, potential
deficiencies, insufficient evidence, high-risk findings, pending reviews, plus an attention queue
sorted most-severe-first and an evidence-coverage panel. Insufficient evidence is counted
*separately* from deficiencies everywhere, and every tile states its counting basis.
→ `app/frontend/pages/dashboard.py`, `app/audit/service.py::dashboard_stats` ·
`GET /api/v1/dashboard/stats`

**2. Audit Projects** — engagements with an audit area, period, auditor of record and status
(`PLANNING → FIELDWORK → REVIEW → COMPLETED → ARCHIVED`); scoping library controls into an
engagement. → `app/frontend/pages/projects.py`, `app/audit/service.py` ·
`GET|POST|PATCH|DELETE /api/v1/projects`

**3. Controls** — a 14-control synthetic library
([`data/controls/control_library.json`](data/controls/control_library.json), editable without
touching code) covering logical access, patching, change management, backup, logging, third-party
access and encryption. Each control carries an objective, assessment criteria, expected evidence,
retrieval keywords, inherent risk, privilege level and data sensitivity.
→ `app/frontend/pages/controls.py`, `app/database/seed.py` · `GET|POST /api/v1/controls`

**4. Evidence** — upload and provenance. Parses `.pdf` (pypdf, per page with section detection),
`.docx` (paragraphs with heading tracking, plus tables), `.csv`/`.xlsx`/`.xls` (pandas/openpyxl,
with **1-based spreadsheet row numbers**) and `.txt`/`.md`/`.json`. Every chunk keeps a fully
populated source locator (`page 4, section '4.2'`, `rows 2-101, columns: …`) so any quotation can
be walked back to its position in the original file. Tabular files additionally emit one
`TABLE_SUMMARY` chunk per sheet — shape, columns and per-column value counts — which is what lets
a model reason about a 100-row population without seeing all 100 rows. SHA-256 is recorded at
upload, and every file carries a **provenance** label (`SYNTHETIC`, `HISTORICAL_PUBLIC`,
`ORGANISATIONAL`) shown on the row, in a banner and on the ingestion result.
→ `app/evidence/{parsers,chunking,storage,service}.py` · `POST /api/v1/evidence/upload`

**5. Assessments** — run a control assessment under condition A, B or C and read where every part
of the answer came from. The detail view is the four-panel layout: **THE CONTROL REQUIRES / THE
EVIDENCE PROVES / THE AI INFERS / A HUMAN MUST VERIFY**, with each citation expandable to the real
stored chunk text and its locator, and each carrying its mechanical verdict (`VERIFIED`, `PARTIAL`,
`UNVERIFIED`, `FABRICATED`) and match score. A retrieval-transparency panel shows which chunks were
supplied to the model, in what rank order, and which were actually cited.
→ `app/frontend/pages/assessments.py`, `app/audit/engine.py`, `app/rag/retriever.py`,
`app/audit/prompts.py` · `POST /api/v1/assessments/run`

**6. Findings** — deficiencies across the engagement, ranked by the prototype risk model, with the
five named factors and the rationale that decomposes each score. Model-suggested risk can escalate
a band but never lower one. Exportable to CSV. → `app/frontend/pages/findings.py`,
`app/audit/risk.py` · `GET /api/v1/assessments/findings`

**7. Human Review** — the review queue. An auditor accepts, modifies, rejects or requests more
evidence, sets the final status and risk, and can flag an output as a fabrication (which requires a
note). The system computes `agreed_with_ai_status` / `agreed_with_ai_risk`, so AI–human agreement
is a measured quantity rather than an impression. Review duration is recorded as an explicitly
optional, explicitly wall-clock figure. → `app/frontend/pages/human_review.py`,
`app/audit/service.py::record_human_review` · `POST /api/v1/reviews`

**8. Evaluation** — the research harness: six synthetic datasets with known correct answers, the
A/B/C experiment runner, confusion matrices, per-class precision/recall/F1, deficiency-detection
FPR/FNR, citation/grounding/hallucination rates, retrieval recall, latency and token cost — every
figure printed next to its `n` and its automatically attached caveats. See
[§14](#14-the-evaluation-component). → `app/frontend/pages/evaluation.py`,
`app/evaluation/{datasets,generator,runner,metrics}.py` · `POST /api/v1/evaluation/run`

**9. Reports** — a ten-section working paper (Executive Summary, Audit Scope, Controls Tested,
Evidence Reviewed, AI Findings, Risk Ratings, Evidence References, Human Auditor Decisions,
Recommendations, Limitations) in Markdown or HTML. AI and auditor conclusions are printed side by
side and never merged; Section 4 prints the full SHA-256 of every evidence file; Section 7 resolves
every citation back to filename, locator and verbatim quote **with its verification verdict,
including the ones that failed**; Section 10 is part of the deliverable, not a bolted-on
disclaimer. → `app/frontend/pages/reports.py`, `app/audit/report.py` ·
`POST /api/v1/reports/generate`

**10. Settings** — the live configuration with every secret masked, the active provider and model
(including Claude's effort, adaptive thinking, SDK version and effective base URL), retrieval
parameters, thresholds and the research levers. Settings are read-only through the UI by design:
an unauthenticated endpoint that could repoint the model on a tool holding audit evidence would be
a security hole, and changing a provider mid-engagement would silently make earlier and later
assessments incomparable. → `app/frontend/pages/settings.py`, `app/config.py` ·
`GET /api/v1/settings`

**Beyond the ten:** [`app/evaluation/case_studies.py`](app/evaluation/case_studies.py) imports a
declarative manifest of a *publicly documented failure pattern* as a full engagement — fictional
organisation, real failure shape, every file labelled `HISTORICAL_PUBLIC`. One case ships
(`CASE-001`, privileged and third-party remote access without enforced MFA). It is reachable from
Python only today: `from app.evaluation import case_studies;
case_studies.import_case_study(session, "CASE-001")`. Its expected outcomes are the case author's
reasoned judgement, deliberately **not** wired into the metrics layer as an answer key. Full
treatment, including the rule about reconstructed evidence and how to add a case:
[`docs/CASE_STUDIES.md`](docs/CASE_STUDIES.md).

---

## 12. Architecture

```
 ┌──────────────────────────────┐        ┌──────────────────────────────────┐
 │  Streamlit audit console     │        │  FastAPI backend                 │
 │  app/frontend/ (10 pages)    │        │  app/api/ (9 routers, /api/v1)   │
 │  dark security-console UI    │        │  OpenAPI + Swagger at /docs      │
 └──────────────┬───────────────┘        └───────────────┬──────────────────┘
                │                                        │
                │     app/frontend/data_access.py        │
                └──── one facade, two transports ────────┘
                      (in-process by default; USE_API=true routes over HTTP)
                                     │
                     ┌───────────────▼────────────────┐
                     │      app/audit/service.py      │   the only data-access
                     │  projects · controls · scope   │   layer; no ORM in
                     │  evidence · assessments        │   routers or pages
                     │  reviews · reports · activity  │
                     └───────────────┬────────────────┘
                                     │
   ┌─────────────────────────────────┼─────────────────────────────────────┐
   │                                 │                                     │
┌──▼───────────────────┐  ┌──────────▼───────────────────┐  ┌──────────────▼─────────┐
│ app/evidence/        │  │ app/audit/engine.py          │  │ app/evaluation/        │
│  parsers  (pdf/docx/ │  │  ┌─────────────────────────┐ │  │  datasets   (6 cases,  │
│            csv/xlsx/ │  │  │ 1 sufficiency pre-check │ │  │              ground    │
│            txt/json) │  │  │ 2 retrieve  app/rag/    │ │  │              truth)    │
│  chunking (locators, │──┼─▶│ 3 prompt    app/audit/  │ │  │  generator  (real      │
│            table     │  │  │             prompts.py  │ │  │              files)    │
│            summaries)│  │  │ 4 model     app/llm/    │ │  │  runner     (A/B/C)    │
│  storage  (sha256)   │  │  │ 5 validate  validators  │ │  │  metrics    (defined   │
│  service  (ingest)   │  │  │ 6 rails     validators  │ │  │              per       │
└──────────────────────┘  │  │ 7 risk      risk.py     │ │  │              formula)  │
                          │  │ 8 human-review gate     │ │  │  case_studies          │
┌──────────────────────┐  │  └─────────────────────────┘ │  └────────────────────────┘
│ app/rag/             │  └──────────────┬───────────────┘
│  embeddings (local   │                 │
│    hashing, no net)  │  ┌──────────────▼───────────────┐   ┌────────────────────────┐
│  indexer             │  │ app/llm/factory.py           │   │ app/database/          │
│  query_builder       │  │   ├── mock_provider  offline │   │  SQLAlchemy 2.0        │
│  retriever  BM25 +   │  │   │   deterministic rules    │   │  12 tables + findings  │
│    cosine, RRF k=60, │  │   ├── anthropic_provider     │   │  view, SQLite          │
│    per-file cap      │  │   │   Claude (opus-5)        │   │  (PostgreSQL-portable) │
└──────────────────────┘  │   └── openai_provider        │   │  migrations · views    │
                          │       any OpenAI-compatible  │   │  seed: control library │
                          └──────────────────────────────┘   └────────────────────────┘
```

**Assessment flow, condition C:** build retrieval queries from the control → hybrid retrieve
(BM25 + cosine over locally computed vectors, fused by reciprocal rank with `k=60`, capped at
`max(2, top_k // 2)` chunks per file so one big spreadsheet cannot crowd out the policy) → ask the
model whether the evidence is *sufficient* → ask for the assessment as strict JSON with chunk-ID
citations → ask the model to critique its own answer → **mechanically** validate every citation and
number against the retrieved set → apply safety rails → score risk deterministically → persist with
`human_review_required = True`.

Three boundaries are load-bearing. The **service layer** is the single data-access layer, so the UI
and the API cannot drift apart. The **provider abstraction** (`app/llm/base.py`) means the offline
mock, Claude and any OpenAI-compatible endpoint are interchangeable at one configuration line. The
**AI record and the human record are separate tables**, and the difference between them is the
research measurement — which is also why there is no `findings` *table*: a finding is the
projection of an assessment joined to its latest human review, and
[`app/database/views.py`](app/database/views.py) makes it a SQL view rather than a third source of
truth that silently diverges.

Full detail, including every design decision and its counter-argument:
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## 13. Repository layout

```
.
├── README.md                     this file
├── LICENSE                       MIT - name the holder before publishing
├── run.py                        launcher: init | seed-demo | ui | api | all | evaluate | reset
├── requirements.txt              pinned, verified against CPython 3.9.6
├── runtime.txt                   python-3.12.11, for Heroku-style platforms and Render
├── pytest.ini                    --strict-markers; hermetic suite
├── .env.example                  every setting, documented; copy to .env (never commit .env)
├── .gitignore / .dockerignore    secrets, data, virtualenv - kept out of git and out of images
│
├── Dockerfile                    multi-stage; one image, roles api | ui | all; non-root
├── docker-compose.yml            the two-service shape locally, with a named data volume
├── Procfile                      web: API, ui: console - for Heroku/Railway/Dokku
├── render.yaml                   Render Blueprint: both services, safety rails pinned
├── scripts/start_api.sh          the single definition of the API start command ($PORT, 0.0.0.0)
├── scripts/start_ui.sh           the single definition of the console start command
├── .streamlit/config.toml        headless server + the console theme, in step with theme.py
│
├── app/
│   ├── config.py                 Settings (pydantic-settings), get_settings(), secret masking
│   ├── schemas/
│   │   ├── enums.py              the enums; tolerant .coerce() for model output
│   │   ├── assessment.py         AssessmentOutput - the four-way contract with the model
│   │   └── api.py                FastAPI request/response models
│   ├── database/
│   │   ├── base.py               engine, SessionLocal, session_scope(), init_db()
│   │   ├── models.py             12 tables (AI record and human record kept apart)
│   │   ├── migrations.py         additive reconcile_schema(): adds missing columns, never drops
│   │   ├── views.py              the `findings` SQL view (assessment ⋈ latest human review)
│   │   └── seed.py               idempotent control-library + demo-project seeding
│   ├── evidence/                 parsers · chunking · storage (sha256) · ingest service
│   ├── rag/                      embeddings · indexer · query_builder · retriever (BM25/vector/hybrid)
│   ├── llm/                      base (ABC) · mock_provider · anthropic_provider · openai_provider · factory
│   ├── audit/
│   │   ├── prompts.py            SYSTEM_PROMPT (the four-way rule + worked MFA example), PROMPT_VERSION
│   │   ├── engine.py             the orchestrator; defines conditions A / B / C
│   │   ├── validators.py         citation verification, unsupported-claim detection, safety rails
│   │   ├── risk.py               prototype 5-factor risk model with published weights
│   │   ├── report.py             the 10-section working paper (markdown | html)
│   │   └── service.py            shared data-access layer used by BOTH the UI and the API
│   ├── evaluation/               datasets · generator · runner (A/B/C) · metrics · case_studies
│   ├── api/                      FastAPI app + 9 routers under /api/v1 (38 paths)
│   └── frontend/                 Streamlit shell, theme, components, data_access facade, 10 pages
│
├── data/                         (contents gitignored; structure kept via .gitkeep)
│   ├── controls/control_library.json   the synthetic control library - the one tracked data file
│   ├── case_studies/*.json       historical case-study manifests
│   ├── audit.db                  SQLite database
│   ├── uploads/project_<id>/     stored evidence files
│   ├── synthetic/dataset-00N/    generated evaluation evidence
│   └── reports/                  generated working papers
│
├── docs/                         see §17
└── tests/                        18 modules, 761 tests, hermetic
```

About 40,000 lines of application code and 7,500 lines of tests.

---

## 14. The evaluation component

### What it measures

Six synthetic datasets, each authored so the correct conclusion is known before anything is run,
and each verified back against the bytes actually written to disk (ground truth asserted only in a
docstring is not ground truth):

| Dataset | Control | Planted condition | Correct answer |
|---|---|---|---|
| DATASET-001 | CONTROL-001 | 100 privileged accounts, 90 MFA-enabled / 10 disabled | `POTENTIAL_DEFICIENCY` |
| DATASET-002 | CONTROL-003 | 100 endpoints, 95 compliant / 5 missing critical patches | `POTENTIAL_DEFICIENCY` |
| DATASET-003 | CONTROL-005 | Policy requires 14 characters; configuration enforces 8 | `NOT_EFFECTIVE` |
| DATASET-004 | CONTROL-004 | 100 changes, 95 approved / 5 unapproved | `POTENTIAL_DEFICIENCY` |
| DATASET-005 | CONTROL-001 | MFA policy + account listing **with no MFA attribute** | `INSUFFICIENT_EVIDENCE` |
| DATASET-006 | CONTROL-001 | 100 accounts, all enrolled (added beyond the mandated five) | `EFFECTIVE` |

DATASET-006 is an addition by this implementation, flagged as such. Without a control whose correct
answer is `EFFECTIVE` there is no true-negative class, every deficiency-detection rate is
undefined, and a system that answered "deficiency" to everything would score 5/5.

Measured per condition: status accuracy, per-class and macro/weighted precision/recall/F1, the
confusion matrix, deficiency-detection FPR/FNR (in two framings, because counting an honest "I
cannot tell" as a missed deficiency depresses recall and both readings should be reported),
citation rate, grounding rate, fabrication rate, unsupported-claim rate, hallucination rate,
retrieval recall against declared marker strings, missing-evidence detection rate, latency, token
cost, and human–AI agreement (raw + Cohen's κ). Every metric carries a docstring stating its exact
formula and denominator; `compute_metrics` refuses to emit a figure without `n`, and attaches
caveats that travel with the numbers wherever they go.

### How to run it

```bash
.venv/bin/python run.py init                              # REQUIRED first - see §10
.venv/bin/python run.py evaluate                          # all three conditions
.venv/bin/python run.py evaluate --modes B_RAG C_RAG_WORKFLOW
```

or the **Evaluation** page in the console (confusion matrices, per-dataset prediction table, A/B/C
comparison, CSV export), or `POST /api/v1/evaluation/run`.

### What it found

Reproduced for this README against a clean temporary database (offline provider, `mock-rules-1.1`,
seed 1337, hallucination rate 0.0, n = 6 per condition, 9.3 s for all three conditions); the
figures match [`docs/EXAMPLE_RESULTS.md`](docs/EXAMPLE_RESULTS.md):

```
  A_RAW_LLM        accuracy=0.667  grounded=0.000  macro_f1=0.464  n=6
  B_RAG            accuracy=1.000  grounded=1.000  macro_f1=1.000  n=6
  C_RAG_WORKFLOW   accuracy=1.000  grounded=1.000  macro_f1=1.000  n=6
```

| | **A** raw | **B** RAG | **C** RAG + workflow |
|---|---:|---:|---:|
| Status accuracy | 0.667 (4/6) | 1.000 (6/6) | 1.000 (6/6) |
| Grounded accuracy (correct **and** backed by a verified citation) | **0.000** | 1.000 | 1.000 |
| Macro F1 | 0.464 | 1.000 | 1.000 |
| Missing-evidence detection | **0.000** (0/1) | 1.000 (1/1) | 1.000 (1/1) |
| Model calls / assessment | 1 | 1 | 3 |

Read honestly, and stated here as the documentation states it:

* **A → B is the real signal.** Two of six statuses change, both in the predicted direction, and
  traceability changes *categorically* rather than by degree: A's citations cannot be resolved to a
  chunk at all, because its prompt format has no chunk IDs to cite.
* **B = C on accuracy and grounding is a null result, and it reproduces.** It is not a wiring bug —
  the two conditions demonstrably execute different pipelines (1 call vs 3) — it is a property of a
  deterministic provider that fabricates nothing at rate 0.0. C's machinery had nothing to catch.
  Forcing it to (`MOCK_HALLUCINATION_RATE=1.0`) is the discriminating experiment, and there C's
  rails change the conclusion — see [§15](#15-safety-rules-and-what-enforces-each).
* **1.000 is not a claim that the pipeline is accurate.** It is six correct answers to six designed
  questions, against a rule-based provider, on deliberately unambiguous evidence, with an answer key
  written by the same author. The defensible reading is *"nothing in this suite caught B or C out"*,
  and the suite is small and easy.
* **n = 6.** Every proportion has a confidence interval wider than the differences it would be used
  to compare. No significance testing is offered, because none would be honest.
* **Retrieval recall of 1.000 is a ceiling artefact**, not a result: each evaluation project holds
  two or three files and `top_k=12` retrieves nearly all of them.
* **Latency figures compare Python pipelines, not inference.** Against the mock they contain no
  model inference at all and must never be presented as such.

Method: [`docs/EVALUATION_METHODOLOGY.md`](docs/EVALUATION_METHODOLOGY.md).
Full results: [`docs/EXAMPLE_RESULTS.md`](docs/EXAMPLE_RESULTS.md).

---

## 15. Safety rules, and what enforces each

Every rule below is enforced by code that can be read and re-run, not by a promise in a prompt.
Where a prompt instruction *is* the mechanism, that is said plainly — a prompt is a request, not a
guarantee.

| # | Rule | Enforced by | Mechanism |
|---|---|---|---|
| 1 | A citation may not point at a chunk that was never retrieved | `app/audit/validators.py::validate_citations` | The `chunk_id` is resolved against the `RetrievalResult` actually shown to the model. Unresolvable ⇒ `FABRICATED`, however plausible the quote reads. Plausibility is the failure mode being guarded against, so it is given no weight. |
| 2 | A quotation must really be in the chunk it claims | same | Normalised token-overlap and longest-common-substring similarity; ≥ `citation_match_threshold` (0.60) ⇒ `VERIFIED`, ≥ half ⇒ `PARTIAL`, else `FABRICATED`. |
| 3 | Numbers in the narrative must appear in the evidence | `validators.detect_unsupported_claims` | Every figure in `assessment` + `finding` + `reasoning` is looked for in the retrieved text (with date/identifier masking and derived-percentage checking). Unfound figures are recorded and pushed into `human_verification_required`. |
| 4 | An ungrounded conclusion may not stand | `validators.enforce_safety_rails` | `EFFECTIVE` or `NOT_EFFECTIVE` with **zero verified citations** is downgraded to `INSUFFICIENT_EVIDENCE`, confidence forced `LOW`. Both directions: an unsupported clean opinion and an unsupported accusation are equally unsafe. `POTENTIAL_DEFICIENCY` is deliberately left alone — it already asserts only a possibility. |
| 5 | Nothing is edited out to make the output look clean | same | Fabricated citations are **retained and flagged**, never deleted, and every intervention is recorded in `rails_applied` in plain language, persisted with the assessment and printed in the report. |
| 6 | Human review is mandatory | same (rail 4, unconditional) + `settings.force_human_review` | `human_review_required` is set true on every assessment, with no code path that sets it false. |
| 7 | An unreviewed assessment is not a conclusion | `app/audit/report.py` | Controls without a completed review are listed as `PENDING AUDITOR REVIEW` and excluded from every headline figure; the summary states its own counting basis so the exclusion is visible rather than silent. |
| 8 | The AI record and the human record never merge | `app/database/models.py` (`assessments` vs `human_reviews`), `app/database/views.py`, `app/audit/report.py` | Separate tables; the `findings` view is a read-only projection, not a third record. The report prints both side by side with explicit provenance labels and marks disagreements. |
| 9 | Risk is computed, not asserted by the model | `app/audit/risk.py` | Deterministic weighted model over five named 1–5 factors with published weights and band thresholds, plus a rationale decomposing the score. The model's own suggested band can only **escalate** a rating, never lower one. Labelled a prototype research model on every artefact. |
| 10 | Absence of evidence is not evidence of a deficiency | `app/schemas/enums.py`, `app/audit/prompts.py`, the mock's rules | `INSUFFICIENT_EVIDENCE` is a first-class status counted separately from deficiencies; the system prompt carries the MFA worked example as a few-shot anchor; DATASET-005 tests the behaviour end to end. |
| 11 | No legal or regulatory compliance statement | `app/audit/prompts.py` (hard rule), `app/audit/report.py` §10, control library `disclaimer` | Prompt-level for the model's own text; structural in the report, which states that no legal, regulatory or certification opinion is expressed and that framework references are unmapped illustrative pointers. |
| 12 | Evidence integrity and provenance are recorded, with their limits stated | `app/evidence/storage.py`, `app/schemas/enums.py` (provenance), `app/audit/report.py` §4 and §10 | SHA-256 over the stored bytes at upload, printed in full in the report — beside an explicit statement of what a hash does *not* establish (authorship, authenticity, completeness, pre-upload editing). Every file also carries a provenance label, surfaced on the Evidence page. |
| 13 | The provider in use is never misrepresented | `app/llm/factory.py`, `GET /health`, the sidebar badge, report header and §10 | A mock-produced report says so in its own Limitations section. A real provider that is unconfigured falls back to the mock **with a warning**, surfaced as `fell_back_to_mock` and shown in red naming both providers. |
| 14 | Failure is recorded, never swallowed | `app/audit/engine.py` | A provider error, unparseable JSON or a schema violation persists an assessment row with `INSUFFICIENT_EVIDENCE`, the error text, and human review required. A run that quietly loses a control is worse than one that records a failure an auditor can see. |
| 15 | No secret reaches a log, an API response or a screen | `app/frontend/pages/settings.py`, `app/config.py::provider_summary`, `app/api/routers/settings.py`, `app/llm/anthropic_provider.py` | The Settings page reduces any field whose name contains a secret word to `(configured)` / `(not set)` — presence only, no prefix, no length; `/health` and `/api/v1/settings` return `provider_summary()` / provider status, never the key; the Anthropic client never writes the key into a log line, an `LLMResponse` or an error message. **Known gap, verified:** `Settings.redacted_dict()` masks `llm_api_key` and `embedding_api_key` but **not** `anthropic_api_key`, so that function returns the Claude key in full to any caller. The console is safe because it redacts again by field name, but the gap is latent for the next consumer — the fix is one entry in `secret_fields`. See [`docs/SECURITY.md`](docs/SECURITY.md) *Known gaps*. |

### Shown, not promised

Rails 1–5 are demonstrable on demand. With the research lever at `MOCK_HALLUCINATION_RATE=1.0` the
mock deliberately injects a fabricated citation; running DATASET-003 under condition C then
produces — reproduced for this README:

```
predicted : INSUFFICIENT_EVIDENCE   (expected NOT_EFFECTIVE)
citations : 2 total - 1 VERIFIED, 1 FABRICATED
rails_applied:
  - critique_downgrade: the self-critique recommended INSUFFICIENT_EVIDENCE instead of
    NOT_EFFECTIVE and the citation validator independently agreed the conclusion was
    unsupported (1 citation(s) were fabricated). The downgrade was applied.
  - fabricated_citations: 1 citation(s) could not be resolved to the retrieved evidence
    (position 2). They are retained and flagged, not removed.
  - human_review_enforced: every assessment produced by this system requires auditor
    review before it can be relied upon.
```

The fabricated citation was caught by arithmetic over stored text, not by a model's opinion — and
that is what makes the hallucination rate a measurable property of the pipeline rather than a claim
about it.

**What none of this catches.** Rails 1–3 verify that quoted characters exist in the cited chunk.
They do not verify that the quote *means* what the assessment says it means. A sentence reassembled
from a chunk's own vocabulary — even one asserting the opposite of the chunk — scores as `VERIFIED`.
Semantic faithfulness is not tested anywhere in this system and is not claimed.

---

## 16. Testing

```bash
.venv/bin/python -m pytest tests -q
```

```
761 passed, 2 warnings in 64.25s
```

*(CPython 3.9.6, macOS arm64, offline provider. The two warnings are a deprecated Starlette status
constant and are not raised by this code.)*

18 modules covering configuration, enums and schemas, parsers, storage, retrieval, all three LLM
providers, validators, the risk model, the engine, the service layer, review, reporting, datasets,
metrics, the evaluation runner, schema migrations and the API. The suite is **hermetic by
construction**: `tests/conftest.py` redirects the database and every writable directory into a
temporary tree *before* `app.config` is first imported, `mock_seed` is pinned, and nothing makes a
network call. `--strict-markers` is on, so a mistyped marker fails rather than silently skipping.

Two honest notes. There is **no coverage measurement** (`coverage.py` is not among the pinned
dependencies), so behaviour coverage is by assertion, not by percentage. And **`app/frontend/` has
no automated tests**: the ten Streamlit pages and the data-access facade are verified by manual
exercise in a browser, not by the suite.

Because several tests assert exact statuses per dataset, an edit to
[`app/llm/mock_provider.py`](app/llm/mock_provider.py)'s rules will turn the suite red. That is the
correct trade — the tests exist to notice exactly that — but read such a failure as *"the rules
changed"*, not *"the test is flaky"*.

---

## 17. Documentation index

Start at [`docs/README.md`](docs/README.md), which gives reading orders for an examiner, a
developer, a researcher and an auditor. In brief:

| Document | What it is for |
|---|---|
| [`docs/QUICKSTART.md`](docs/QUICKSTART.md) | Fresh clone → completed assessment → human review → report, as numbered steps with the exact output to expect. |
| [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) | Operating the console page by page: reading an assessment, the citation verification verdicts, the three modes, the review actions, troubleshooting. |
| [`docs/IT_Audit_Tool_Overview_and_User_Guide.pdf`](docs/IT_Audit_Tool_Overview_and_User_Guide.pdf) | A 13-page illustrated overview and operator guide, written to be sent to a practising auditor as a request for review. The one document to hand to someone who will not clone the repository. |
| [`docs/SETUP.md`](docs/SETUP.md) | The full installation, configuration and operations reference: every setting, entry points, real providers, PostgreSQL, troubleshooting, security notes. |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | The long form of [§8](#8-cloud-deployment): four deployment paths as numbered steps, the pre-deployment checklist, PostgreSQL, custom domains and TLS, a deployed-instance variable reference, how to verify a deployment, and an explicit account of what was and was not proven by execution. |
| [`docs/SECURITY.md`](docs/SECURITY.md) | The security posture by mechanism: where secrets come from and what keeps them out of every output, what is implemented, what is **not**, the known gaps each with its exact fix, where an uploaded file actually ends up, and the threat model. Read it before deploying anything. |
| [`docs/CASE_STUDIES.md`](docs/CASE_STUDIES.md) | The historical case-study mechanism: the three evidence provenance classes and their exact disclosure sentences, the non-negotiable rule about reconstructed evidence, what `CASE-001` contains, how to add a case, the manifest field reference, and why a case study is **not** an evaluation dataset. |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | The system as implemented: layer map, both data paths, the three conditions, provider substitution, design decisions with their counter-arguments, architectural limits. |
| [`docs/DATABASE_SCHEMA.md`](docs/DATABASE_SCHEMA.md) | All 12 tables from the live metadata, the ER diagram, why the AI record and the human record are separate, and the SQLite → PostgreSQL path. |
| [`docs/API.md`](docs/API.md) | Every HTTP endpoint with request/response shapes, the error envelope, and the API's known limitations. |
| [`docs/SYNTHETIC_DATASETS.md`](docs/SYNTHETIC_DATASETS.md) | Each of the six datasets: how it is built, what is in it, what the correct answer is and **why**, and which capability it probes. |
| [`docs/EVALUATION_METHODOLOGY.md`](docs/EVALUATION_METHODOLOGY.md) | The measurement protocol: variables, ground truth, procedure, reproducibility, threats to validity. |
| [`docs/EXAMPLE_RESULTS.md`](docs/EXAMPLE_RESULTS.md) | Every figure with its provenance, including the fault-injection experiment and a full DATASET-005 walkthrough. |
| [`docs/RESEARCH_NOTES.md`](docs/RESEARCH_NOTES.md) | Using the system to produce dissertation results: which experiments to run, what to vary, what to report, and how to phrase a claim it supports. |
| [`docs/LIMITATIONS_AND_FUTURE_WORK.md`](docs/LIMITATIONS_AND_FUTURE_WORK.md) | The least flattering document in the set — read it before quoting any result. |
| [`docs/TESTING.md`](docs/TESTING.md) | What the suite covers, how it is isolated, the research-critical tests, and what is not covered. |
| [`docs/BUILD_SPEC.md`](docs/BUILD_SPEC.md) | The implementation contract the codebase was built against, including the Python 3.9 constraints. |

Deployment is also documented in the files themselves — [`Dockerfile`](Dockerfile),
[`docker-compose.yml`](docker-compose.yml), [`render.yaml`](render.yaml), [`Procfile`](Procfile),
[`.streamlit/config.toml`](.streamlit/config.toml) and [`.env.example`](.env.example) all carry
comments explaining *why*, not just *what*.

---

## 18. Limitations

The short list. The long list, with what would have to be done about each, is
[`docs/LIMITATIONS_AND_FUTURE_WORK.md`](docs/LIMITATIONS_AND_FUTURE_WORK.md).

* **The evaluation was run without a language model.** Every default figure comes from a
  deterministic rule-based stand-in and measures the pipeline only.
* **n = 6.** Descriptive figures, not statistics. One dataset changing its answer moves accuracy by
  17 percentage points.
* **The ground truth was authored by the same author as the system.** Accuracy here is accuracy
  against a designed answer key, not against an audited reality.
* **The planted conditions are unambiguous** — one attribute, one threshold, one clean exception
  rate. Real audit evidence is contradictory, partial, undated and inconsistent between systems.
  Nothing in this suite measures behaviour on that.
* **Retrieval recall is saturated** at the corpus size used, so it cannot distinguish good ranking
  from indiscriminate ranking.
* **Citation verification is textual, not semantic** ([§15](#15-safety-rules-and-what-enforces-each)).
* **No human-subject evaluation.** Whether an auditor is faster, more accurate or appropriately
  sceptical when using this tool is not measured, and the AI–human agreement machinery is exercised
  only against reviews recorded by the developer.
* **No security controls at all**: no authentication, no authorisation, no rate limiting, no
  encryption at rest. The API is open to anyone who can reach the port. Bind it to localhost, or put
  an authenticating proxy in front of it. A short list of *verified* gaps, each with its exact fix —
  including `redacted_dict()` not masking the Anthropic key and the console binding every interface
  — is in [`docs/SECURITY.md`](docs/SECURITY.md) *Known gaps*, written down rather than tidied away.
* **SQLite, single process**, unbounded list endpoints, synchronous assessment and evaluation runs.
  Fine at prototype scale, wrong at engagement scale.
* **Schema evolution is additive only.** `app/database/migrations.py` adds missing columns on
  startup; it never renames, retypes, drops or back-fills, and it does not create indexes on the
  columns it adds. Alembic is the upgrade path and is deliberately not used yet.
* **Evaluation runs leave their throwaway projects** in the database (marked `is_demo`) so their
  prompts and responses stay auditable. They are excluded from operational figures, but they do
  appear in the sidebar project selector.
* **The container image has never been built** on the author's machine — Docker is not installed
  there. See the note in [§8.3](#83-docker-and-compose).

---

## 19. Academic use and citation

This repository is the software artefact of a student research project, released under the
[MIT licence](LICENSE) for academic study, replication and criticism.

**If you reproduce results from it, report the configuration**, because the figures depend on it:
provider and model (`mock` / `mock-rules-1.1` by default), `MOCK_SEED`,
`MOCK_HALLUCINATION_RATE`, `PROMPT_VERSION`, the retrieval strategy and `RETRIEVAL_TOP_K`,
`CITATION_MATCH_THRESHOLD`, and the dataset generator seed. All of these are recorded in every
`EvaluationRun` and printed in the report header, so a stored run is self-describing. Results from
different mock rule-set versions or different prompt versions **must not be pooled**.

**Suggested citation** — replace the bracketed fields:

```bibtex
@software{llm_it_auditor_2026,
  title  = {LLM-Assisted IT Audit Risk and Control Assessment System:
            a research prototype for grounded, human-verified IT control assessment},
  author = {[Author]},
  year   = {2026},
  note   = {Research prototype. All evaluation data is synthetic; default results were
            produced by a deterministic rule-based provider and measure the pipeline,
            not language-model capability.},
  url    = {[repository URL]}
}
```

**If you build on it, please keep the honesty machinery.** The caveats attached by
`app.evaluation.metrics`, the `n` printed beside every rate, the provenance labels in the report,
the "Prototype research risk scoring model" tag and the mock-provider notice are not decoration —
they are what stops a figure from this prototype being read as something it is not.

---

*AI assists, the auditor decides.*
