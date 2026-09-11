# LLM-Assisted IT Audit Risk and Control Assessment System

A university research prototype that uses retrieval-augmented generation to help a **human IT
auditor** test IT general controls against uploaded evidence — and that is built, throughout, to
make the difference between *what the evidence proves* and *what a language model inferred*
impossible to lose.

> ## ⚠ Synthetic data only
>
> Every control, document, account name, ticket and configuration value in this repository is
> **fabricated** for research purposes. Nothing here describes a real organisation, system, person
> or audit.
>
> **Do not point this prototype at real, confidential or client audit evidence.** It has no
> authentication, no authorisation, no access control, no encryption at rest, no audit-proof
> tamper protection and no data-retention controls. Uploaded files are written to the local
> filesystem in the clear and their text is written into a local SQLite database. With a hosted
> LLM provider configured, evidence text is transmitted to a third party. It is a research
> instrument, not a tool fit to hold client data.

---

## Contents

1. [What this is](#1-what-this-is)
2. [What this is **not**](#2-what-this-is-not)
3. [The research principle: REQUIRES / PROVES / INFERS / HUMAN-VERIFIES](#3-the-research-principle)
4. [Quickstart](#4-quickstart--three-commands)
5. [Feature tour](#5-feature-tour)
6. [Architecture](#6-architecture)
7. [Repository layout](#7-repository-layout)
8. [Using a real LLM provider](#8-using-a-real-llm-provider)
9. [The evaluation component](#9-the-evaluation-component)
10. [Safety rules, and how each one is enforced](#10-safety-rules-and-how-each-one-is-enforced)
11. [Testing](#11-testing)
12. [Documentation index](#12-documentation-index)
13. [Limitations](#13-limitations)
14. [Academic use and citation](#14-academic-use-and-citation)

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

The system implements three experimental conditions over the same evidence so the question can be
answered by measurement rather than by demonstration:

| Condition | Pipeline |
|---|---|
| **A** — raw baseline | No retrieval. Raw file text, crudely truncated, a naive persona, no chunk IDs, no output schema, **no safety rails applied**. Deliberately weak; this is what the study measures *against*. |
| **B** — RAG | Hybrid retrieval + the full control requirement + a structured output schema. One model call. Citations are validated and recorded, but nothing is corrected. |
| **C** — RAG + workflow | Sufficiency pre-check → assessment → self-critique → citation validation → safety rails → prototype risk scoring → mandatory human-review gate. |

A is left unrailed on purpose. Railing the baseline would erase the very behaviour the experiment
exists to observe, and the A-vs-C comparison would be circular. See
[`app/audit/engine.py`](app/audit/engine.py) for the reasoning in full.

---

## 2. What this is *not*

Stated first, because everything below only makes sense once these are accepted.

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
  deterministic rule-based program, not an LLM. See [§8](#8-using-a-real-llm-provider).

---

## 3. The research principle

Four kinds of statement appear in an audit working paper, and the failure mode this system exists
to prevent is letting one drift into another:

| | Question it answers | Where it must live |
|---|---|---|
| **REQUIRES** | What does the policy or control *require*? | `control_requirement` — from the control definition and policy documents. A requirement is not an observation. |
| **PROVES** | What does the supplied evidence *literally state*? | `assessment` + `evidence[]`, as verbatim quotations tagged with the `chunk_id` they were copied from. If you cannot point at a chunk and quote it, it is not proven. |
| **INFERS** | What travels even one step beyond the literal words? | `inferences`. "10 rows show FALSE, therefore 10 accounts lack MFA" is a small inference and is still labelled. |
| **HUMAN-VERIFIES** | What could not be established, and what must a reviewer check? | `human_verification_required` + `missing_evidence`. |

This is not a convention. It is the shape of
[`AssessmentOutput`](app/schemas/assessment.py), it is stated as a rule in
[`SYSTEM_PROMPT`](app/audit/prompts.py), it is the four-column panel every assessment is displayed
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

Both of the following are real, reproducible output from this repository
(`.venv/bin/python run.py evaluate`, offline provider, DATASET-005):

**Condition A (no retrieval, no schema, no rails) — wrong, and confidently so:**

```
status   : POTENTIAL_DEFICIENCY        (risk CRITICAL)
finding  : 2 record(s) do not meet the requirement for multi-factor
           authentication status.
citations: 2, both PARTIAL - no resolvable chunk id
missing_evidence: []
```

It produced a count of failing records from a file that has no such column, and asked for
nothing.

**Condition C (retrieval + workflow + rails) — correct:**

```
status   : INSUFFICIENT_EVIDENCE       (risk HIGH)
assessment: The operational evidence supplied does not record multi-factor
            authentication status. The export reports the fields: User, Role,
            Department, Last_Login, Account_Status, Data_Origin. It covers 100
            records, but none of its fields state whether the control operated
            for them.
finding  : The control could neither be confirmed nor challenged: multi-factor
           authentication status is absent from the evidence provided.
missing_evidence:
  - A per-account multi-factor authentication status or enrolment export
    covering the in-scope account population
  - Privileged account listing ... showing ... MFA enrolment status (MFA_Status)
  - Exception register listing approved MFA exemptions with the approver, ...
human_verification_required:
  - Request a per-account MFA status export ... and re-perform this test
  - Confirm with the system owner which system of record holds MFA status
citations: 2, both VERIFIED (match score 1.00) against stored chunks 193, 194
```

"I cannot tell from this evidence" is a complete, correct, professional answer, and making it a
first-class outcome rather than a failure state is the point of the design. `INSUFFICIENT_EVIDENCE`
is counted separately from deficiencies in every figure the console shows.

---

## 4. Quickstart — three commands

Requires **Python 3.9** (developed and verified on CPython 3.9.6, macOS arm64). **No API key and
no network are needed** — the default provider is offline.

```bash
# 1. install (about 1 minute)
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt

# 2. create the database and seed the 14-control synthetic library
.venv/bin/python run.py init

# 3. start the audit console
.venv/bin/python run.py ui
```

Open <http://localhost:8501> and click **“Load demo project + synthetic evidence”**. That
generates nine synthetic evidence files (DOCX, PDF, CSV, XLSX, TXT), ingests, chunks and indexes
them (~40 s the first time), after which **Assessments → Run assessment** assesses all five
scoped controls in about four seconds.

> On the **first ever** `streamlit run` on a machine, Streamlit asks for an email address before
> starting. Press **Enter** to skip it; it never asks again.

`run.py api` starts the FastAPI backend alone (interactive docs at
<http://localhost:8000/docs>); `run.py all` starts both.

**Step-by-step, from a fresh clone to a completed assessment, a recorded human review and a
generated report — with the exact output to expect at each step — is in
[`docs/QUICKSTART.md`](docs/QUICKSTART.md).** Every command in it has been executed against a
clean copy of this repository.

---

## 5. Feature tour

The ten feature areas are the ten workspace pages named in the build spec. Each is listed with
what it does, the modules that implement it, and the API route that exposes the same capability.

**1. Dashboard** — engagement overview: controls in scope, assessed, effective, potential
deficiencies, insufficient evidence, high-risk findings, pending reviews, plus an
attention queue sorted most-severe-first and an evidence-coverage panel. Insufficient evidence is
counted *separately* from deficiencies everywhere, and every tile states its counting basis.
→ `app/frontend/pages/dashboard.py`, `app/audit/service.py::dashboard_stats` ·
`GET /api/v1/dashboard/stats`

**2. Audit Projects** — engagements with an audit area, period, auditor of record and status
(`PLANNING → FIELDWORK → REVIEW → COMPLETED → ARCHIVED`); scoping library controls into an
engagement. → `app/frontend/pages/audit_projects.py`, `app/audit/service.py` ·
`GET|POST|PATCH|DELETE /api/v1/projects`

**3. Controls** — a 14-control synthetic library (`data/controls/control_library.json`, editable
without touching code) covering logical access, patching, change management, backup, logging,
third-party access and encryption. Each control carries an objective, assessment criteria,
expected evidence, retrieval keywords, inherent risk, privilege level and data sensitivity.
→ `app/frontend/pages/controls.py`, `app/database/seed.py` · `GET|POST /api/v1/controls`

**4. Evidence** — upload and provenance. Parses `.pdf` (pypdf, per page with section detection),
`.docx` (paragraphs with heading tracking, plus tables), `.csv`/`.xlsx`/`.xls` (pandas/openpyxl,
with **1-based spreadsheet row numbers**) and `.txt`/`.md`/`.json`. Every chunk keeps a fully
populated source locator (`page 4, section '4.2'`, `rows 2-101, columns: …`) so any quotation can
be walked back to its position in the original file. Tabular files additionally emit one
`TABLE_SUMMARY` chunk per sheet — shape, columns and per-column value counts — which is what lets
a model reason about a 100-row population without seeing all 100 rows. SHA-256 is recorded at
upload. → `app/evidence/{parsers,chunking,storage,service}.py` ·
`POST /api/v1/evidence/upload`

**5. Assessments** — run a control assessment under condition A, B or C and read where every part
of the answer came from. The detail view is the four-column panel: **THE CONTROL REQUIRES / THE
EVIDENCE PROVES / THE AI INFERS / A HUMAN MUST VERIFY**, with each citation expandable to the real
stored chunk text and its locator, and each carrying its mechanical verdict (`VERIFIED`,
`PARTIAL`, `FABRICATED`) and match score. A retrieval-transparency panel shows which chunks were
supplied to the model, in what rank order, and which were actually cited.
→ `app/frontend/pages/assessments.py`, `app/audit/engine.py`, `app/rag/retriever.py`,
`app/audit/prompts.py` · `POST /api/v1/assessments/run`

**6. Findings** — deficiencies across the engagement, ranked by the prototype risk model, with the
five named factors and the rationale that decomposes each score. Model-suggested risk can escalate
a band but never lower one. → `app/frontend/pages/findings.py`, `app/audit/risk.py` ·
`GET /api/v1/assessments/findings`

**7. Human Review** — the review queue. An auditor accepts, modifies, rejects or requests more
evidence, sets the final status and risk, and can flag an output as a fabrication (which requires
a note). The system computes `agreed_with_ai_status` / `agreed_with_ai_risk`, so AI–human
agreement is a measured quantity rather than an impression. Review duration is recorded as an
explicitly optional, explicitly wall-clock figure. → `app/frontend/pages/human_review.py`,
`app/audit/service.py::record_human_review` · `POST /api/v1/reviews`

**8. Evaluation** — the research harness: six synthetic datasets with known correct answers, the
A/B/C experiment runner, confusion matrices, per-class precision/recall/F1, deficiency-detection
FPR/FNR, citation/grounding/hallucination rates, retrieval recall, latency and token cost — every
figure printed next to its `n` and its automatically-attached caveats. See
[§9](#9-the-evaluation-component). → `app/frontend/pages/evaluation.py`,
`app/evaluation/{datasets,generator,runner,metrics}.py` · `POST /api/v1/evaluation/run`

**9. Reports** — a ten-section working paper (Executive Summary, Audit Scope, Controls Tested,
Evidence Reviewed, AI Findings, Risk Ratings, Evidence References, Human Auditor Decisions,
Recommendations, Limitations) in Markdown or HTML. AI and auditor conclusions are printed side by
side and never merged; Section 4 prints the full SHA-256 of every evidence file; Section 7
resolves every citation back to filename, locator and verbatim quote **with its verification
verdict, including the ones that failed**; Section 10 is part of the deliverable, not a bolted-on
disclaimer. → `app/frontend/pages/reports.py`, `app/audit/report.py` ·
`POST /api/v1/reports/generate`

**10. Settings** — the live configuration, with every secret masked, the active provider and
model, retrieval parameters, thresholds and the research levers (`mock_hallucination_rate`,
`mock_seed`). Settings are read-only through the UI by design: changing a provider mid-engagement
would silently make earlier and later assessments incomparable. → `app/frontend/pages/settings.py`,
`app/config.py` · `GET /api/v1/settings`

---

## 6. Architecture

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
                          │  │ 8 human-review gate     │ │  └────────────────────────┘
┌──────────────────────┐  │  └─────────────────────────┘ │
│ app/rag/             │  └──────────────┬───────────────┘
│  embeddings (local   │                 │
│    hashing, no net)  │  ┌──────────────▼───────────────┐   ┌────────────────────────┐
│  indexer             │  │ app/llm/factory.py           │   │ app/database/          │
│  query_builder       │  │   ├── mock_provider  offline │   │  SQLAlchemy 2.0        │
│  retriever  BM25 +   │  │   │   deterministic rules    │   │  12 tables, SQLite     │
│    cosine, RRF k=60, │  │   └── openai_provider        │   │  (PostgreSQL-portable) │
│    per-file cap      │  │       any OpenAI-compatible  │   │  seed: control library │
└──────────────────────┘  └──────────────────────────────┘   └────────────────────────┘
```

**Assessment flow, condition C:** build retrieval queries from the control → hybrid retrieve
(BM25 + cosine over locally-computed vectors, fused by reciprocal rank with `k=60`, capped at
`max(2, top_k // 2)` chunks per file so one big spreadsheet cannot crowd out the policy) → ask the
model whether the evidence is *sufficient* → ask for the assessment as strict JSON with chunk-ID
citations → ask the model to critique its own answer → **mechanically** validate every citation
and number against the retrieved set → apply safety rails → score risk deterministically →
persist with `human_review_required = True`.

Two boundaries are load-bearing. The **service layer** is the single data-access layer, so the UI
and the API cannot drift apart. The **provider abstraction** (`app/llm/base.py`) means the offline
mock and a hosted model are interchangeable at one configuration line.

Full detail, including every design decision and its counter-argument:
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## 7. Repository layout

```
.
├── README.md                     this file
├── run.py                        launcher: init | seed-demo | ui | api | all | evaluate | reset
├── requirements.txt              pinned, verified against CPython 3.9.6
├── .env.example                  every setting, documented; copy to .env (never commit .env)
├── pytest.ini                    --strict-markers; hermetic suite
│
├── app/
│   ├── config.py                 Settings (pydantic-settings), get_settings(), secret masking
│   ├── schemas/
│   │   ├── enums.py              13 enums; tolerant .coerce() for model output
│   │   ├── assessment.py         AssessmentOutput - the four-way contract with the model
│   │   └── api.py                FastAPI request/response models
│   ├── database/
│   │   ├── base.py               engine, SessionLocal, session_scope(), init_db()
│   │   ├── models.py             12 tables (AI record and human record kept apart)
│   │   └── seed.py               idempotent control-library + demo-project seeding
│   ├── evidence/                 parsers · chunking · storage (sha256) · ingest service
│   ├── rag/                      embeddings · indexer · query_builder · retriever (BM25/vector/hybrid)
│   ├── llm/                      base (ABC) · mock_provider · openai_provider · factory
│   ├── audit/
│   │   ├── prompts.py            SYSTEM_PROMPT (the four-way rule + worked MFA example), PROMPT_VERSION
│   │   ├── engine.py             the orchestrator; defines conditions A / B / C
│   │   ├── validators.py         citation verification, unsupported-claim detection, safety rails
│   │   ├── risk.py               prototype 5-factor risk model with published weights
│   │   ├── report.py             the 10-section working paper (markdown | html)
│   │   └── service.py            shared data-access layer used by BOTH the UI and the API
│   ├── evaluation/               datasets · generator · runner (A/B/C) · metrics
│   ├── api/                      FastAPI app + 9 routers under /api/v1
│   └── frontend/                 Streamlit shell, theme, components, data_access facade, 10 pages
│
├── data/                         (gitignored, created on first run)
│   ├── controls/control_library.json   the synthetic control library - the one tracked file
│   ├── audit.db                  SQLite database
│   ├── uploads/project_<id>/     stored evidence files
│   ├── synthetic/dataset-00N/    generated evaluation evidence
│   └── reports/                  generated working papers
│
├── docs/                         see §12
└── tests/                        17 modules, 713 tests, hermetic
```

---

## 8. Using a real LLM provider

The default provider is **`mock`**: `MockLLMProvider` in
[`app/llm/mock_provider.py`](app/llm/mock_provider.py). Read this before quoting any number this
repository produces.

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
> k/6 accurate"*.
>
> The mock exists so the whole system is demonstrable, reproducible and testable offline, and so
> the validation layer can be exercised deliberately: `MOCK_HALLUCINATION_RATE` makes it fabricate
> a citation or an unsupported number at a chosen rate, which is how the anti-hallucination layer
> is shown to work rather than promised to (see [§10](#10-safety-rules-and-how-each-one-is-enforced)).
> Its rule set is versioned (`mock-rules-1.1`); results from different rule-set versions must not
> be pooled.

To use a real model, copy `.env.example` to `.env` and set:

```bash
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o-mini
LLM_API_KEY=sk-...          # never commit this file
```

`openai_provider` speaks to **any OpenAI-compatible endpoint** via `LLM_BASE_URL`, so a locally
hosted model needs no key and no network egress:

```bash
LLM_PROVIDER=openai
LLM_BASE_URL=http://localhost:11434/v1     # Ollama
LLM_MODEL=llama3.1:8b
```

(Also works with vLLM, LM Studio, llama.cpp's server, OpenRouter and Azure-compatible gateways.)
The provider requests JSON-schema-constrained output when the endpoint supports it and degrades
gracefully to `json_object` mode and then to plain text; it retries with backoff and records real
token usage. If a real provider is selected but not configured, the factory **falls back to the
mock with a visible warning** rather than failing silently — `GET /health` and the UI provider
badge both report `fell_back_to_mock`.

Embeddings are separate and default to a local, stateless scikit-learn hashing vectoriser
(`EMBEDDING_PROVIDER=local`), so **retrieval never requires network access** regardless of which
LLM provider is configured.

Two things to know before running with a hosted model. First, **evidence text leaves the
machine** — with synthetic data that is fine, and it is one more reason not to point this
prototype at real evidence. Second, assessment and evaluation runs are **synchronous**: the whole
six-dataset, three-condition suite takes seconds against the mock and minutes against a hosted
model.

---

## 9. The evaluation component

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

DATASET-006 is an addition by this implementation, flagged as such. Without a control whose
correct answer is `EFFECTIVE` there is no true-negative class, every deficiency-detection rate is
undefined, and a system that answered "deficiency" to everything would score 5/5.

Measured per condition: status accuracy, per-class and macro/weighted precision/recall/F1, the
confusion matrix, deficiency-detection FPR/FNR (in two framings, because counting an honest "I
cannot tell" as a missed deficiency depresses recall and both readings should be reported),
citation rate, grounding rate, fabrication rate, unsupported-claim rate, hallucination rate,
retrieval recall against declared marker strings, missing-evidence detection rate, latency,
token cost, and human–AI agreement (raw + Cohen's κ). Every metric carries a docstring stating
its exact formula and denominator; `compute_metrics` refuses to emit a figure without `n`, and
attaches caveats that travel with the numbers wherever they go.

### How to run it

```bash
.venv/bin/python run.py evaluate                          # all three conditions
.venv/bin/python run.py evaluate --modes B_RAG C_RAG_WORKFLOW
```

or the **Evaluation** page in the UI (which renders the confusion matrices, the per-dataset
prediction table and the A/B/C comparison, and exports CSV), or
`POST /api/v1/evaluation/run`.

> **Two known defects in `run.py`** (not in the evaluation code itself; both are confirmed in
> [`docs/RESEARCH_NOTES.md`](docs/RESEARCH_NOTES.md) §1):
>
> 1. `cmd_evaluate` prints its summary line from `metrics["accuracy"]`, but `compute_metrics`
>    nests those figures under `metrics["classification"]`. The CLI therefore prints
>    `accuracy=0.000 macro_f1=0.000` for every condition while the **stored results are correct**.
>    Read the figures from the Evaluation page, from `compare_runs`, or from
>    `run.metrics["classification"]` — never from that line.
> 2. `run.py evaluate` calls `init_db()` but not the control-library seed, so against a database
>    that has never had `run.py init` run on it every dataset errors with *"Control 'CONTROL-001'
>    not found"*. **Always run `run.py init` first.**

### What it found

Reproduced independently for this README against a clean database (offline provider,
`mock-rules-1.1`, seed 1337, hallucination rate 0.0, n = 6 per condition); the figures match
[`docs/EXAMPLE_RESULTS.md`](docs/EXAMPLE_RESULTS.md) exactly:

| | **A** raw | **B** RAG | **C** RAG + workflow |
|---|---:|---:|---:|
| Status accuracy | 0.667 (4/6) | 1.000 (6/6) | 1.000 (6/6) |
| Macro F1 | 0.464 | 1.000 | 1.000 |
| Grounding rate (verified / total citations) | **0.000** | 1.000 | 1.000 |
| Missing-evidence detection | **0.000** (0/1) | 1.000 (1/1) | 1.000 (1/1) |
| Model calls / assessment | 1 | 1 | 3 |
| Mean tokens / assessment | 5 465 | 10 284 | 29 591 |

Read honestly, and stated here as the documentation states it:

* **A → B is the real signal.** Two of six statuses change, both in the predicted direction, and
  traceability changes *categorically* rather than by degree: A's citations cannot be resolved to
  a chunk at all, because its prompt format has no chunk IDs to cite.
* **B = C on accuracy and grounding is a null result, and it reproduces.** It is not a wiring
  bug — the two conditions demonstrably execute different pipelines (1 call vs 3, ~10.3k vs
  ~29.6k tokens, rails off vs on) — it is a property of a deterministic provider that fabricates
  nothing at rate 0.0. C's machinery had nothing to catch. Forcing it to (`MOCK_HALLUCINATION_RATE=1.0`)
  is the discriminating experiment, and there C's rails change the conclusion.
* **1.000 is not a claim that the pipeline is accurate.** It is six correct answers to six
  designed questions, against a rule-based provider, on deliberately unambiguous evidence, with an
  answer key written by the same author. The defensible reading is *"nothing in this suite caught
  B or C out"*, and the suite is small and easy.
* **n = 6.** Every proportion has a confidence interval wider than the differences it would be
  used to compare. No significance testing is offered, because none would be honest.
* **Retrieval recall of 1.000 is a ceiling artefact**, not a result: each evaluation project holds
  two or three files and `top_k=12` retrieves nearly all of them.
* **Latency figures compare Python pipelines, not inference.** Against the mock they contain no
  model inference at all and must never be presented as such.

Method: [`docs/EVALUATION_METHODOLOGY.md`](docs/EVALUATION_METHODOLOGY.md).
Full results: [`docs/EXAMPLE_RESULTS.md`](docs/EXAMPLE_RESULTS.md).

---

## 10. Safety rules, and how each one is enforced

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
| 6 | Human review is mandatory | same (rail 4, unconditional) + `settings.force_human_review` | `human_review_required` is set true on every assessment with no code path that sets it false. |
| 7 | An unreviewed assessment is not a conclusion | `app/audit/report.py` | Controls without a completed review are listed as `PENDING AUDITOR REVIEW` and excluded from every headline figure; the summary states its own counting basis so the exclusion is visible rather than silent. |
| 8 | The AI record and the human record never merge | `app/database/models.py` (`assessments` vs `human_reviews`), `app/audit/report.py` | Separate tables; the report prints both side by side with explicit provenance labels and marks disagreements. The UI shows the same split and banners every AI output. |
| 9 | Risk is computed, not asserted by the model | `app/audit/risk.py` | Deterministic weighted model over five named 1–5 factors with published weights and band thresholds, plus a rationale decomposing the score. The model's own suggested band can only **escalate** a rating, never lower one. Labelled a prototype research model on every artefact. |
| 10 | Absence of evidence is not evidence of a deficiency | `app/schemas/enums.py`, `app/audit/prompts.py`, the mock's rules | `INSUFFICIENT_EVIDENCE` is a first-class status counted separately from deficiencies; the system prompt carries the MFA worked example as a few-shot anchor; DATASET-005 tests the behaviour end to end. |
| 11 | No legal or regulatory compliance statement | `app/audit/prompts.py` (hard rule), `app/audit/report.py` §10, control library `disclaimer` | Prompt-level for the model's own text; structural in the report, which states that no legal, regulatory or certification opinion is expressed and that framework references are unmapped illustrative pointers. |
| 12 | Evidence integrity is recorded, and its limits stated | `app/evidence/storage.py`, `app/audit/report.py` §4 and §10 | SHA-256 over the stored bytes at upload, printed in full in the report — beside an explicit statement of what a hash does *not* establish (authorship, authenticity, completeness, pre-upload editing). |
| 13 | The provider in use is never misrepresented | `app/llm/factory.py`, `GET /health`, the UI badge, report header and §10 | A mock-produced report says so in its own Limitations section. A real provider that is unconfigured falls back to the mock **with a warning**, surfaced as `fell_back_to_mock`. |
| 14 | Failure is recorded, never swallowed | `app/audit/engine.py` | A provider error, unparseable JSON or a schema violation persists an assessment row with `INSUFFICIENT_EVIDENCE`, the error text, and human review required. A run that quietly loses a control is worse than one that records a failure an auditor can see. |

### Shown, not promised

Rails 1–5 are demonstrable on demand. With the research lever at
`MOCK_HALLUCINATION_RATE=1.0` the mock deliberately injects a fabricated citation; running
DATASET-003 under condition C then produces, verifiably:

```
predicted: INSUFFICIENT_EVIDENCE   (original model status: NOT_EFFECTIVE)
citations: 2 total - 1 VERIFIED (1.00), 1 FABRICATED (0.09)
rails_applied:
  - critique_downgrade: the self-critique recommended INSUFFICIENT_EVIDENCE instead of
    NOT_EFFECTIVE and the citation validator independently agreed the conclusion was
    unsupported (1 citation(s) were fabricated).
  - fabricated_citations: 1 citation(s) could not be resolved to the retrieved evidence
    (position 2). They are retained and flagged, not removed.
  - human_review_enforced: every assessment produced by this system requires auditor
    review before it can be relied upon.
```

The fabricated citation was caught by arithmetic over stored text, not by a model's opinion — and
that is what makes the hallucination rate a measurable property of the pipeline rather than a
claim about it.

**What none of this catches.** Rails 1–3 verify that quoted characters exist in the cited chunk.
They do not verify that the quote *means* what the assessment says it means. A sentence
reassembled from a chunk's own vocabulary — even one asserting the opposite of the chunk — scores
as `VERIFIED`. Semantic faithfulness is not tested anywhere in this system and is not claimed.

---

## 11. Testing

```bash
.venv/bin/python -m pytest tests -q          # 713 tests, ~58 s
.venv/bin/python -m pytest tests -m "not slow"
```

16 modules covering configuration, enums and schemas, parsers, storage, retrieval, both LLM
providers, validators, the risk model, the engine, the service layer, review, reporting, datasets,
metrics, the evaluation runner and the API. The suite is **hermetic by construction**:
`tests/conftest.py` redirects the database and every writable directory into a temporary tree
*before* `app.config` is first imported, `mock_seed` is pinned, and nothing makes a network call.
`--strict-markers` is on, so a mistyped marker fails rather than silently skipping.

Two honest notes. There is **no coverage measurement** (`coverage.py` is not among the pinned
dependencies), so behaviour coverage is by assertion, not by percentage. And **`app/frontend/` has
no tests**: the ten Streamlit pages and the ~90-function data-access facade are verified by manual
exercise, not by the suite.

Because several tests assert exact statuses per dataset, an edit to
`app/llm/mock_provider.py`'s rules will turn the suite red. That is the correct trade — the tests
exist to notice exactly that — but read such a failure as *"the rules changed"*, not *"the test is
flaky"*.

---

## 12. Documentation index

Start at [`docs/README.md`](docs/README.md), which gives reading orders for an examiner, a
developer and an auditor. In brief:

| Document | What it is for |
|---|---|
| [`docs/QUICKSTART.md`](docs/QUICKSTART.md) | Fresh clone → completed assessment → human review → report, as numbered steps with the exact output to expect. |
| [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) | Operating the console page by page: reading an assessment, the citation verification verdicts, the three modes, the review actions, troubleshooting. |
| [`docs/SETUP.md`](docs/SETUP.md) | The full installation, configuration and operations reference: every setting, entry points, real providers, PostgreSQL, troubleshooting, security notes. |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | The system as implemented: layer map, both data paths, the three conditions, provider substitution, design decisions with their counter-arguments, architectural limits. |
| [`docs/DATABASE_SCHEMA.md`](docs/DATABASE_SCHEMA.md) | All 12 tables from the live metadata, the ER diagram, why the AI record and the human record are separate, and the SQLite → PostgreSQL path. |
| [`docs/API.md`](docs/API.md) | Every HTTP endpoint with request/response shapes, the error envelope, and the API's known limitations. |
| [`docs/SYNTHETIC_DATASETS.md`](docs/SYNTHETIC_DATASETS.md) | Each of the six datasets: how it is built, what is in it, what the correct answer is and **why**, and which capability it probes. |
| [`docs/EVALUATION_METHODOLOGY.md`](docs/EVALUATION_METHODOLOGY.md) | The measurement protocol: variables, ground truth, procedure, reproducibility, threats to validity. |
| [`docs/EXAMPLE_RESULTS.md`](docs/EXAMPLE_RESULTS.md) | Every figure with its provenance, including the fault-injection experiment and a full DATASET-005 walkthrough. |
| [`docs/RESEARCH_NOTES.md`](docs/RESEARCH_NOTES.md) | Using the system to produce dissertation results: which experiments to run, what to vary, what to report, and how to phrase a claim it supports. |
| [`docs/LIMITATIONS_AND_FUTURE_WORK.md`](docs/LIMITATIONS_AND_FUTURE_WORK.md) | The least flattering document in the set — read it before quoting any result. |
| [`docs/TESTING.md`](docs/TESTING.md) | What the suite covers, how it is isolated, the research-critical tests, and what is not covered. |
| [`docs/BUILD_SPEC.md`](docs/BUILD_SPEC.md) | The implementation contract the codebase was built against. |

---

## 13. Limitations

The short list. The long list, with what would have to be done about each, is
[`docs/LIMITATIONS_AND_FUTURE_WORK.md`](docs/LIMITATIONS_AND_FUTURE_WORK.md).

* **The evaluation was run without a language model.** Every default figure comes from a
  deterministic rule-based stand-in and measures the pipeline only.
* **n = 6.** Descriptive figures, not statistics. One dataset changing its answer moves accuracy
  by 17 percentage points.
* **The ground truth was authored by the same author as the system.** Accuracy here is accuracy
  against a designed answer key, not against an audited reality.
* **The planted conditions are unambiguous** — one attribute, one threshold, one clean exception
  rate. Real audit evidence is contradictory, partial, undated and inconsistent between systems.
  Nothing in this suite measures behaviour on that.
* **Retrieval recall is saturated** at the corpus size used, so it cannot distinguish good ranking
  from indiscriminate ranking.
* **Citation verification is textual, not semantic** (§10).
* **No human-subject evaluation.** Whether an auditor is faster, more accurate or appropriately
  sceptical when using this tool is not measured, and the AI–human agreement machinery is
  exercised only against reviews recorded by the developer.
* **No security controls at all**: no authentication, no authorisation, no rate limiting, no
  encryption at rest. The API is open to anyone who can reach the port. Bind it to localhost.
* **SQLite, single process**, unbounded list endpoints, synchronous assessment and evaluation
  runs. Fine at prototype scale, wrong at engagement scale.
* **Evaluation runs leave their throwaway projects** in the database (marked `is_demo`) so their
  prompts and responses stay auditable. They are excluded from operational figures, but they do
  appear in the sidebar project selector.

---

## 14. Academic use and citation

This repository is the software artefact of a student research project. It is released for
academic study, replication and criticism.

**If you reproduce results from it, report the configuration**, because the figures depend on it:
provider and model (`mock` / `mock-rules-1.1` by default), `mock_seed`, `mock_hallucination_rate`,
`PROMPT_VERSION`, the retrieval strategy and `top_k`, `citation_match_threshold`, and the dataset
generator seed. All of these are recorded in every `EvaluationRun` and printed in the report
header, so a stored run is self-describing. Results from different mock rule-set versions or
different prompt versions **must not be pooled**.

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

**Licence:** not yet specified. Contact the author before redistribution.

---

*AI assists, the auditor decides.*
