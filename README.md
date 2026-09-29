# AuditAI - LLM-assisted IT audit risk and control assessment (research prototype)

*LLM-Assisted IT Audit Risk and Control Assessment System.* A university research prototype that
uses retrieval-augmented generation to help a **human IT auditor** test IT general controls
against uploaded evidence - built, throughout, to keep *what the evidence proves* separate from
*what a language model inferred*.

`Python 3.9 source` · `works fully offline, no API key` · `optional Claude backend` ·
`MIT licence` · `all data synthetic`

> ## Synthetic data only - do not point this at real audit evidence
>
> Every control, document, account name, ticket and configuration value in this repository is
> **fabricated** for research purposes. Nothing here describes a real organisation, system, person
> or audit.
>
> **There is no authentication.** No authorisation, no access control, no encryption at rest, no
> tamper protection, no data-retention controls. Uploaded files are written to the local filesystem
> in the clear and their text into a local database. With a hosted LLM provider configured,
> evidence text is transmitted to a third party. A cloud deployment is open to whoever finds the
> URL. It is a research instrument, not a tool fit to hold client data.

## Run it now

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run.py          # console on http://127.0.0.1:8501; opens your browser
```

Then, in the browser, press **Try the demo audit** on the home page. It loads nine synthetic
evidence files into a demo audit with five controls in scope and lands you on **Assessments**.
From there:

1. **Assessments** - press **Run assessment (5 controls)** and watch the progress line
   ("Assessing CONTROL-001 (1 of 5)..."). Select a row to read the assessment screen.
2. **Review queue** - type **Your name** in the sidebar, then for each item choose **Accept
   finding**, **Modify finding**, **Reject finding** or **Request more evidence** and press
   **Record decision**.
3. **Reports** - press **Generate report**, then **Download report (HTML)**.
4. Read the report's Limitations section before showing it to anyone.

No API key and no network are needed: the default provider is an offline rule engine, labelled
**DEMO MODE** in the sidebar. `run.py init` is optional - the console creates its database and
seeds the control library itself on first start. Step-by-step with expected output:
[`docs/QUICKSTART.md`](docs/QUICKSTART.md).

---

## Contents

[What it is](#what-it-is) · [What it is not](#what-it-is-not) · [The research principle](#the-research-principle) ·
[Launcher and settings](#launcher-and-settings) · [Turning Claude on](#turning-claude-on) ·
[Feature tour](#feature-tour) · [Safety rules, and what enforces each](#safety-rules-and-what-enforces-each) ·
[Architecture](#architecture) · [Repository layout](#repository-layout) · [Evaluation](#evaluation) ·
[Deployment](#deployment) · [Testing](#testing) · [Documentation index](#documentation-index) ·
[Limitations](#limitations) · [Academic use and citation](#academic-use-and-citation)

---

## What it is

An IT auditor testing a control - say, "every privileged account must use multi-factor
authentication" - collects evidence, reads it, and forms a conclusion that has to be defensible
line by line.

This system is a prototype of what a language model can and cannot safely contribute to that
task. It ingests evidence files, chunks and indexes them, retrieves the passages relevant to a
specific control, asks a model to assess the control against *only those passages* in a strict
JSON shape, then **mechanically re-checks every citation and every number** against the evidence
that was actually supplied before an auditor sees the answer. The auditor's decision is recorded
separately from the model's and is the only thing the system treats as a conclusion.

**The research question.** Does adding retrieval and a structured audit workflow to a language
model produce IT control assessments that are more *traceable* and more *honest about uncertainty*
than an unstructured prompt over the same evidence - and can the difference be measured
mechanically rather than asserted?

Three experimental conditions run over the same evidence:

| Condition | Pipeline |
|---|---|
| **A** - raw baseline | No retrieval. Raw file text, crudely truncated, no chunk IDs, no output schema, **no safety rails**. Deliberately weak; what the study measures *against*. |
| **B** - RAG | Hybrid retrieval + the full control requirement + a structured output schema. One model call. Citations validated and recorded; nothing corrected. |
| **C** - RAG + workflow | Sufficiency pre-check → assessment → self-critique → citation validation → safety rails → prototype risk scoring → mandatory human-review gate. **The mode for normal use**; the console calls it "Full audit workflow (recommended)". |

A is left unrailed on purpose: railing the baseline would erase the behaviour the experiment
exists to observe. See [`app/audit/engine.py`](app/audit/engine.py).

## What it is not

* **It does not replace the auditor.** No output of this system is an audit conclusion. Every
  assessment is flagged `human_review_required = True`, unconditionally, by
  [`app/audit/validators.py::enforce_safety_rails`](app/audit/validators.py) - there is no
  configuration that turns this off. A control with no recorded auditor decision appears in the
  generated report as `PENDING AUDITOR REVIEW` and is **excluded from every headline figure**.
* **It does not make final compliance decisions.** The AI record (`assessments`) and the human
  record (`human_reviews`) are separate database tables and are never merged. The report prints
  both side by side, each with its own provenance label, and marks the pairs that disagree.
* **It never asserts legal or regulatory compliance.** The system prompt forbids it; the control
  library's framework references are labelled illustrative and unmapped; the report's Limitations
  section states that no legal, regulatory or certification opinion is expressed.
* **It gives no assurance.** No opinion, no sign-off, no attestation. The generated document is a
  working paper for a human to correct and sign, or refuse to.
* **It cannot establish that evidence is authentic, complete or representative.** It records a
  SHA-256 of each file at upload. That shows the file has not changed *inside this system*; it says
  nothing about who produced it, whether it was edited beforehand, or whether a listing is the
  complete population.
* **Citation verification is textual, not semantic.** A `VERIFIED` verdict means the quoted
  characters exist in the chunk the model pointed at. It does **not** mean the quote supports the
  conclusion drawn from it.
* **The risk scores are not an industry framework.** They come from a prototype additive model with
  the author's own weights, labelled *"Prototype research risk scoring model - not an official
  industry framework"* on every artefact that shows one.
* **The default results say nothing about language models.** The offline provider
  ([`app/llm/mock_provider.py`](app/llm/mock_provider.py)) is a deterministic rule-based program,
  not an LLM. Every default figure measures the pipeline. The honest sentence is *"the pipeline
  reached the planted conclusion in k of 6 cases"*, never *"the LLM was k/6 accurate"*.

## The research principle

Four kinds of statement appear in an audit working paper, and the failure mode this system exists
to prevent is letting one drift into another. The assessment screen shows them as four numbered
sections:

| Section on screen | Question it answers | Field |
|---|---|---|
| **1. What the control requires** | What does the policy or control *require*? | `control_requirement` - from the control library, not the model. |
| **2. What the evidence shows** | What does the evidence *literally state*? | `evidence[]` - verbatim quotations tagged with the `chunk_id` they came from, each re-checked. |
| **3. What the AI infers** | What travels beyond the literal words? | `inferences` - unproven by construction, labelled as such. |
| **4. What still needs verification** | What could not be established? | `human_verification_required` + `missing_evidence`. |

This is the shape of [`AssessmentOutput`](app/schemas/assessment.py), it is a rule in
[`SYSTEM_PROMPT`](app/audit/prompts.py), and it is what the citation validator checks.

**Absence of evidence is not evidence of a deficiency.** The most damaging thing this system
could do is turn a gap in an *export* into a finding about a *control*. DATASET-005 supplies an
MFA policy plus an account listing with **no MFA column**; the correct answer is
`INSUFFICIENT_EVIDENCE` - not a deficiency, not a pass - and the console shows it as "Cannot
conclude - the evidence does not permit a judgement", counted apart from deficiencies in every
figure. Condition A produces a confident deficiency with a fabricated count; condition C asks for
the missing export. The full walkthrough is in [`docs/EXAMPLE_RESULTS.md`](docs/EXAMPLE_RESULTS.md).

---

## Launcher and settings

| Command | What it does |
|---|---|
| `python run.py` (or `run.py ui`) | Starts the console on **http://127.0.0.1:8501** (loopback only) and opens the browser. `NO_BROWSER=1` suppresses the browser. The console creates the database and seeds the control library itself. |
| `python run.py all` | Console **and** FastAPI backend (docs at <http://127.0.0.1:8000/docs>); Ctrl-C stops both. |
| `python run.py api` | FastAPI backend only. |
| `python run.py init` | Create the database and seed the 14-control synthetic library. Idempotent; optional for the UI. |
| `python run.py seed-demo` | Create the demo audit and load its nine synthetic evidence files from the CLI - the same thing the **Try the demo audit** button does. |
| `python run.py evaluate` | Run experiments A/B/C over the six synthetic datasets. Seeds the control library first. |
| `python run.py reset` | **Destructive.** Drops every table, re-initialises, re-seeds the library. Asks for a typed `yes`. Files on disk are kept. |

`run.py` prefers `.venv/bin/python` when it exists, so `python run.py` works from the system
interpreter too. The deployment start scripts ([`scripts/`](scripts)) bind `0.0.0.0` and honour
`$PORT`; use `run.py` locally.

Every setting has a working default, so the application runs with no `.env` at all. To change
anything, `cp .env.example .env` and edit; a variable exported in the shell beats `.env`, which
beats the default. Configuration is read at startup - restart after editing. The settings most
people touch:

| Variable | Default | What it does |
|---|---|---|
| `LLM_PROVIDER` | `mock` | `mock` (offline rules, shown as **DEMO MODE**) · `claude` · `openai` and its aliases (any OpenAI-compatible endpoint via `LLM_BASE_URL`). An unrecognised value falls back to `mock` with a warning. |
| `ANTHROPIC_API_KEY` | *(unset)* | The Claude key. Without it, `LLM_PROVIDER=claude` falls back to the offline provider **with a red notice** in the sidebar. Masked everywhere it is displayed. |
| `ANTHROPIC_MODEL` | `claude-opus-5` | Claude model id. |
| `DEFAULT_AUDITOR_NAME` | *(empty)* | Pre-fills **Your name** in the sidebar. Left empty, the auditor must type a name before recording a decision. |
| `USE_API` | `false` | `true` makes the console call the FastAPI backend over HTTP instead of the service layer in-process. Must be a real environment variable - the console reads `os.environ`, so a value in `.env` alone does nothing. |
| `DATABASE_URL` | `sqlite:///./data/audit.db` | SQLAlchemy URL. PostgreSQL works but its driver is not installed by default (see [`requirements.txt`](requirements.txt)). |
| `MAX_UPLOAD_MB` | `50` | Larger uploads are refused. Keep in step with `maxUploadSize` in [`.streamlit/config.toml`](.streamlit/config.toml). |

Every other variable - embeddings, retrieval, chunking, thresholds, CORS, the research levers
`MOCK_HALLUCINATION_RATE` and `MOCK_SEED` - is documented with its default in
[`.env.example`](.env.example) and [`docs/SETUP.md`](docs/SETUP.md).

## Turning Claude on

```bash
# in .env
LLM_PROVIDER=claude
ANTHROPIC_API_KEY=sk-ant-...
# ANTHROPIC_MODEL=claude-opus-5     (optional)
```

Restart. The sidebar's provider block then names Claude; the **Settings → AI provider** tab shows
the model, effort, thinking mode, SDK version and effective base URL, with the key reported as
present or absent only. If the key is missing the sidebar shows, in red:

> Claude was configured but Demo mode answered. Add ANTHROPIC_API_KEY to .env and restart, or set
> LLM_PROVIDER=mock to run offline on purpose.

That state is also returned by `GET /health` as `fell_back_to_mock: true`. A run that silently
used the offline provider while its author believed it used Claude would be worthless, so the
fallback is never quiet. `temperature` is deliberately never sent to Claude; determinism is
controlled through `ANTHROPIC_EFFORT`. With any hosted model, evidence text leaves the machine -
one more reason to keep the data synthetic.

---

## Feature tour

The sidebar carries the **Current audit** selector, **Your name** (required before recording a
decision; a declared name, not an authenticated identity), the provider block (**DEMO MODE** for
the offline provider), an **AI provider settings** link and the line *AI assists, the auditor
decides.* Navigation is grouped as **Home** · **Audit**: Audit projects, Controls, Evidence,
Assessments, Review queue, Findings, Reports · **Learn**: How it works · **Research**:
Experiments · **Setup**: Settings. URL paths: `/projects` `/controls` `/evidence` `/assessments`
`/review` `/findings` `/reports` `/how-it-works` `/experiments` `/settings`. Above every Audit
page a compact strip shows where the current audit stands in the five steps.

**Home** - a greeting, then two doors. **Start an audit** opens the **New audit** dialog (Audit
name, Audit area, Controls to test, **Start with the standard set of five controls**, More
details, **Create audit**) and lands on Evidence. **Try the demo audit** loads the nine synthetic
files and lands on Assessments; once loaded the button reads **Open the demo audit**. Below:
**Your audit overview** (Active audits / Awaiting review), the five-step strip - 1 Controls
"Choose the controls", 2 Evidence "Add evidence", 3 Assess "Run the AI assessment", 4 Review
"Record your decisions", 5 Report "Generate the report" - **What do you need to do?**, the
**Requires your attention** queue (most severe first, each with a **Review** button), and the
**Figures and activity for this audit** expander holding the tiles, charts, evidence coverage and
activity trail. Insufficient evidence is counted separately from deficiencies in every figure.
→ [`app/frontend/views/home.py`](app/frontend/views/home.py)

**Audit projects** - the audits: name, area, period, status, the controls in scope, the full
five-step strip, and the gated delete (a confirmation checkbox, then **Delete this audit project**;
deletion removes the audit's evidence files as well as its rows).
→ [`app/frontend/views/audit_projects.py`](app/frontend/views/audit_projects.py)

**Controls** - the 14-control synthetic library
([`data/controls/control_library.json`](data/controls/control_library.json)): objective,
assessment criteria, expected evidence, retrieval keywords, inherent risk.
→ [`app/frontend/views/controls.py`](app/frontend/views/controls.py)

**Evidence** - **Upload evidence**: pick files, answer **Where does this file come from?**
(synthetic test data, historical reconstruction, or real organisational evidence - the last is
what this prototype must not be given), choose the **Evidence type**, press **Upload and index**.
After the upload, one result line per file ("parsed, n passages from n pages"), any parser
warnings, a provenance banner for non-synthetic files, and next-step links. Parses `.pdf`,
`.docx`, `.csv`/`.xlsx`/`.xls`, `.txt`/`.md`/`.json`; SHA-256 is recorded at upload and every
chunk keeps a source locator so a quotation can be walked back to its page, section or row.
→ [`app/frontend/views/evidence.py`](app/frontend/views/evidence.py), `app/evidence/`

**Assessments** - choose **Which controls** (Not yet assessed / All in scope / Choose...); the
**Research options** expander holds the pipeline choice, default **Full audit workflow
(recommended)**; press **Run assessment (n controls)**. Progress is live, one line per control.
The results table shows the latest run per control (**Show every run** and **Show research
columns** toggles, a **Filter** popover); select a row to open the assessment screen. Nothing runs
until the button is pressed. → [`app/frontend/views/assessments.py`](app/frontend/views/assessments.py)

**The assessment screen** (shared by Assessments and Review queue, from
[`app/frontend/assessment_view.py`](app/frontend/assessment_view.py)) - header badges, exactly one
AI disclaimer banner, any fabricated citation or rail intervention called out before anything
else, then **What did we find?** and the four sections **1. What the control requires**, **2. What
the evidence shows** (each quote with its verdict and a **Show the source passage** expander),
**3. What the AI infers**, **4. What still needs verification**. Under **Auditor review** the
decision form ([`app/frontend/review_form.py`](app/frontend/review_form.py)): **Accept finding** /
**Modify finding** / **Reject finding** / **Request more evidence**, **Your conclusion**, **Your
risk level**, finding, recommendation, comments, an optional fabrication flag, then **Record
decision**. Requesting more evidence locks the conclusion to Insufficient evidence and the risk to
Not rated and never counts as agreement. Everything a researcher needs is folded under **Details
for researchers (risk arithmetic, retrieval, validation, prompt)**.

**Review queue** - the **Assessment to review** picker with "Item i of n awaiting a decision" and
**Skip to next**, the same assessment screen, then tabs **Completed reviews (n)** (with CSV export)
and **Agreement so far** (AI-human agreement on conclusion and risk, always with its denominator).
→ [`app/frontend/views/human_review.py`](app/frontend/views/human_review.py),
`app/audit/service.py::record_human_review`

**Findings** - deficiencies across the audit, latest per control first, ranked by the prototype
risk model with the five named factors; **Download the register (CSV)**.
→ [`app/frontend/views/findings.py`](app/frontend/views/findings.py), [`app/audit/risk.py`](app/audit/risk.py)

**Reports** - a pre-flight that says what the document will be able to conclude (with a **Review
them now** link while decisions are outstanding), **Generate report** (HTML default, Markdown
optional), **Download report (HTML)**, and **Previous reports (n)**. The working paper prints AI
and auditor conclusions side by side, the SHA-256 of every file, every citation with its verdict
including the failed ones, and a Limitations section that is part of the deliverable.
→ [`app/frontend/views/reports.py`](app/frontend/views/reports.py), [`app/audit/report.py`](app/audit/report.py)

**How it works** - the plain-language explanation of the pipeline and the rails, for a first-time
auditor. → [`app/frontend/views/how_it_works.py`](app/frontend/views/how_it_works.py)

**Experiments** - the research harness: the A/B/C runner over the six datasets, confusion
matrices, per-dataset predictions and CSV export. See [Evaluation](#evaluation).
→ [`app/frontend/views/evaluation.py`](app/frontend/views/evaluation.py), `app/evaluation/`

**Settings** - three tabs. **AI provider**: configured versus active provider, model, key
presence, the "Switch on Claude" box. **Demo data & reset**: reload the demo, and the gated
**Delete all audit data** form. **For the record**: the full configuration with every secret
masked, the prompt version, retrieval parameters and thresholds. Settings are read-only through
the UI by design. → [`app/frontend/views/settings.py`](app/frontend/views/settings.py)

The same capabilities are exposed by the FastAPI backend under `/api/v1`
([`docs/API.md`](docs/API.md)).

---

## Safety rules, and what enforces each

Every rule below is enforced by code that can be read and re-run, not by a promise in a prompt.
Where a prompt instruction *is* the mechanism, that is said plainly - a prompt is a request, not a
guarantee.

| # | Rule | Enforced by | Mechanism |
|---|---|---|---|
| 1 | A citation may not point at a chunk that was never retrieved | `app/audit/validators.py::validate_citations` | The `chunk_id` is resolved against the `RetrievalResult` actually shown to the model. Unresolvable ⇒ `FABRICATED`, however plausible the quote reads. Plausibility is the failure mode being guarded against, so it is given no weight. |
| 2 | A quotation must really be in the chunk it claims | same | Normalised token-overlap and longest-common-substring similarity; ≥ `citation_match_threshold` (0.60) ⇒ `VERIFIED`, ≥ half ⇒ `PARTIAL`, else `FABRICATED`. |
| 3 | Numbers in the narrative must appear in the evidence | `validators.detect_unsupported_claims` | Every figure in `assessment` + `finding` + `reasoning` is looked for in the retrieved text (with date/identifier masking and derived-percentage checking). Unfound figures are recorded and pushed into `human_verification_required`. |
| 4 | An ungrounded conclusion may not stand | `validators.enforce_safety_rails` | `EFFECTIVE` or `NOT_EFFECTIVE` with **zero verified citations** is downgraded to `INSUFFICIENT_EVIDENCE`, confidence forced `LOW`. Both directions: an unsupported clean opinion and an unsupported accusation are equally unsafe. `POTENTIAL_DEFICIENCY` is deliberately left alone - it already asserts only a possibility. |
| 5 | Nothing is edited out to make the output look clean | same | Fabricated citations are **retained and flagged**, never deleted, and every intervention is recorded in `rails_applied` in plain language, persisted with the assessment and printed in the report. |
| 6 | Human review is mandatory | same (rail 4, unconditional) + `settings.force_human_review` | `human_review_required` is set true on every assessment, with no code path that sets it false. |
| 7 | An unreviewed assessment is not a conclusion | `app/audit/report.py` | Controls without a completed review are listed as `PENDING AUDITOR REVIEW` and excluded from every headline figure; the summary states its own counting basis so the exclusion is visible rather than silent. |
| 8 | The AI record and the human record never merge | `app/database/models.py` (`assessments` vs `human_reviews`), `app/database/views.py`, `app/audit/report.py` | Separate tables; the `findings` view is a read-only projection, not a third record. The report prints both side by side with explicit provenance labels and marks disagreements. |
| 9 | Risk is computed, not asserted by the model | `app/audit/risk.py` | Deterministic weighted model over five named 1-5 factors with published weights and band thresholds, plus a rationale decomposing the score. The model's own suggested band can only **escalate** a rating, never lower one. Labelled a prototype research model on every artefact. |
| 10 | Absence of evidence is not evidence of a deficiency | `app/schemas/enums.py`, `app/audit/prompts.py`, the mock's rules | `INSUFFICIENT_EVIDENCE` is a first-class status counted separately from deficiencies; the system prompt carries the MFA worked example as a few-shot anchor; DATASET-005 tests the behaviour end to end. |
| 11 | No legal or regulatory compliance statement | `app/audit/prompts.py` (hard rule), `app/audit/report.py` §10, control library `disclaimer` | Prompt-level for the model's own text; structural in the report, which states that no legal, regulatory or certification opinion is expressed and that framework references are unmapped illustrative pointers. |
| 12 | Evidence integrity and provenance are recorded, with their limits stated | `app/evidence/storage.py`, `app/schemas/enums.py` (provenance), `app/audit/report.py` §4 and §10 | SHA-256 over the stored bytes at upload, printed in full in the report - beside an explicit statement of what a hash does *not* establish (authorship, authenticity, completeness, pre-upload editing). Every file also carries a provenance label, surfaced on the Evidence page. |
| 13 | The provider in use is never misrepresented | `app/llm/factory.py`, `GET /health`, the sidebar badge, report header and §10 | A mock-produced report says so in its own Limitations section. A real provider that is unconfigured falls back to the mock **with a warning**, surfaced as `fell_back_to_mock` and shown in red naming both providers. |
| 14 | Failure is recorded, never swallowed | `app/audit/engine.py` | A provider error, unparseable JSON or a schema violation persists an assessment row with `INSUFFICIENT_EVIDENCE`, the error text, and human review required. A run that quietly loses a control is worse than one that records a failure an auditor can see. |
| 15 | No secret reaches a log, an API response or a screen | `app/frontend/views/settings.py`, `app/config.py::provider_summary`, `app/api/routers/settings.py`, `app/llm/anthropic_provider.py` | The Settings page reduces any field whose name contains a secret word to `(configured)` / `(not set)` - presence only, no prefix, no length; `/health` and `/api/v1/settings` return `provider_summary()` / provider status, never the key; the Anthropic client never writes the key into a log line, an `LLMResponse` or an error message. `Settings.redacted_dict()` masks `llm_api_key`, `embedding_api_key` and `anthropic_api_key` (`SECRET_FIELDS` in `app/config.py`). |

Two further rails live in the console itself: every model string is HTML-escaped before it is
rendered, and no assessment, ingestion, deletion or reset runs without a button press.

**Shown, not promised.** With the research lever `MOCK_HALLUCINATION_RATE=1.0` the offline
provider deliberately injects a fabricated citation; running DATASET-003 under condition C then
records the citation as `FABRICATED`, downgrades the conclusion to `INSUFFICIENT_EVIDENCE`, and
lists both interventions in `rails_applied`. The fabricated citation is caught by arithmetic over
stored text, not by a model's opinion. **What none of this catches:** rails 1-3 verify that quoted
characters exist in the cited chunk, not that the quote *means* what the assessment says it means.
Semantic faithfulness is not tested anywhere in this system and is not claimed.

---

## Architecture

```
 Streamlit console (app/frontend/)          FastAPI backend (app/api/, /api/v1)
            └──── app/frontend/data_access.py: one facade, two transports ────┘
                  (in-process by default; USE_API=true routes over HTTP)
                                    │
                         app/audit/service.py   the only data-access layer
                                    │
   app/evidence/ (parse, chunk, hash)  ·  app/rag/ (local embeddings, BM25 + cosine, RRF)
   app/audit/engine.py: sufficiency check → retrieve → prompt → model (app/llm/) →
     validate → rails → risk → human-review gate      ·  app/evaluation/ (A/B/C harness)
   app/database/ (SQLAlchemy 2.0, SQLite, PostgreSQL-portable)
```

Three boundaries are load-bearing. The **service layer** is the single data-access layer, so the
UI and the API cannot drift apart. The **provider abstraction** (`app/llm/base.py`) makes the
offline mock, Claude and any OpenAI-compatible endpoint interchangeable at one configuration line.
The **AI record and the human record are separate tables**; a finding is a SQL view over an
assessment joined to its latest review, not a third source of truth. Full detail, every design
decision and its counter-argument: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Repository layout

```
run.py                       launcher: ui (default) | all | api | init | seed-demo | evaluate | reset
requirements.txt · runtime.txt · pytest.ini · .env.example · LICENSE (MIT - name the holder)
Dockerfile · docker-compose.yml · Procfile · render.yaml · scripts/   deployment (docs/DEPLOYMENT.md)
.streamlit/config.toml       headless server + console theme
app/
  config.py                  Settings (pydantic-settings), secret masking, provider_summary()
  schemas/                   enums · assessment.py (AssessmentOutput) · api.py
  database/                  base · models · migrations · views (findings) · seed (library, demo evidence)
  evidence/  rag/  llm/      parse/chunk/hash · embeddings/retrieval · mock/anthropic/openai providers
  audit/                     prompts · engine (A/B/C) · validators (rails) · risk · report · service
  evaluation/  api/          datasets · generator · runner · metrics · case_studies · FastAPI routers
  frontend/
    streamlit_app.py         shell: navigation, sidebar, bootstrap
    views/                   home · audit_projects · controls · evidence · assessments · human_review
                             · findings · reports · how_it_works · evaluation · settings
    assessment_view.py       the plain-language assessment screen (shared by two views)
    review_form.py           the auditor decision form
    components.py · theme.py · state.py · data_access.py · api_client.py
data/                        gitignored except controls/control_library.json and .gitkeep files
docs/  tests/                the documentation index below; the hermetic test suite
```

The view directory is `views/`, not `pages/`: Streamlit treats a `pages/` folder as its legacy
automatic multipage mechanism and would register every file a second time.

---

## Evaluation

Six synthetic datasets, each authored so the correct conclusion is known before anything is run
and verified back against the bytes written to disk:

| Dataset | Control | Planted condition | Correct answer |
|---|---|---|---|
| DATASET-001 | CONTROL-001 | 100 privileged accounts, 90 MFA-enabled / 10 disabled | `POTENTIAL_DEFICIENCY` |
| DATASET-002 | CONTROL-003 | 100 endpoints, 95 compliant / 5 missing critical patches | `POTENTIAL_DEFICIENCY` |
| DATASET-003 | CONTROL-005 | Policy requires 14 characters; configuration enforces 8 | `NOT_EFFECTIVE` |
| DATASET-004 | CONTROL-004 | 100 changes, 95 approved / 5 unapproved | `POTENTIAL_DEFICIENCY` |
| DATASET-005 | CONTROL-001 | MFA policy + account listing **with no MFA attribute** | `INSUFFICIENT_EVIDENCE` |
| DATASET-006 | CONTROL-001 | 100 accounts, all enrolled | `EFFECTIVE` |

```bash
.venv/bin/python run.py evaluate                            # all three conditions
.venv/bin/python run.py evaluate --modes B_RAG C_RAG_WORKFLOW
```

or the **Experiments** page. Measured per condition: status accuracy, per-class and macro
precision/recall/F1, confusion matrix, deficiency-detection FPR/FNR, citation, grounding,
fabrication and unsupported-claim rates, retrieval recall, missing-evidence detection, latency,
token cost and human-AI agreement. Every metric carries its formula, its `n` and automatically
attached caveats.

Read honestly: A → B is the real signal (traceability changes categorically, because A's prompt
has no chunk IDs to cite); B = C against the offline provider is a reproducible null result,
because a provider that fabricates nothing gives C's machinery nothing to catch; `n = 6`, so no
proportion supports a significance claim; and none of it says anything about a language model.
Method: [`docs/EVALUATION_METHODOLOGY.md`](docs/EVALUATION_METHODOLOGY.md). Figures with
provenance: [`docs/EXAMPLE_RESULTS.md`](docs/EXAMPLE_RESULTS.md). How to turn the harness into
dissertation results: [`docs/RESEARCH_NOTES.md`](docs/RESEARCH_NOTES.md).

## Deployment

Three routes exist - Streamlit Community Cloud (console only, one process), Render/Railway/Heroku
([`render.yaml`](render.yaml), [`Procfile`](Procfile): console and API as two services) and
Docker ([`Dockerfile`](Dockerfile), [`docker-compose.yml`](docker-compose.yml)). All of them are
**unauthenticated**: anyone who finds the URL can upload, read, assess, report and delete. Deploy
with synthetic data only, or put an authenticating proxy in front. Free tiers have ephemeral
filesystems. Numbered steps, PostgreSQL, custom domains, CORS, a checklist and an honest account
of what was and was not proven by execution: [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md). Read
[`docs/SECURITY.md`](docs/SECURITY.md) first.

Before publishing the repository: `git check-ignore -v .env data/audit.db .venv` should list all
three; never `git add -f .env`; name the licence holder in [`LICENSE`](LICENSE); fill in the
citation block below.

## Testing

```bash
.venv/bin/python -m pytest
```

The suite covers configuration, enums and schemas, parsers, storage, retrieval, all three LLM
providers, validators, the risk model, the engine, the service layer, review, reporting, datasets,
metrics, the evaluation runner, schema migrations, the API, the demo seeding, evidence provenance,
and contract tests over the frontend modules and views (labels, escaping, rail wording). It is
hermetic: [`tests/conftest.py`](tests/conftest.py) redirects the database and every writable
directory into a temporary tree before `app.config` is first imported, the mock seed is pinned,
and nothing makes a network call. There is no coverage measurement, and the views are exercised
through their contracts rather than a browser. Because several tests assert exact statuses per
dataset, editing the offline provider's rules turns the suite red - read that as *"the rules
changed"*, not as flakiness. More: [`docs/TESTING.md`](docs/TESTING.md).

## Documentation index

Start at [`docs/README.md`](docs/README.md), which gives reading orders for an examiner, a
developer, a researcher and an auditor.

| Document | What it is for |
|---|---|
| [`docs/QUICKSTART.md`](docs/QUICKSTART.md) | Fresh clone → demo audit → assessment → decision → report, as numbered steps with the exact output to expect. |
| [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) | Operating the console page by page: reading an assessment, the verification verdicts, the decisions, troubleshooting. |
| [`docs/IT_Audit_Tool_Overview_and_User_Guide.pdf`](docs/IT_Audit_Tool_Overview_and_User_Guide.pdf) | An illustrated overview for someone who will not clone the repository. |
| [`docs/SETUP.md`](docs/SETUP.md) | Every setting, entry point, provider and database option. |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | The deployment paths as numbered steps, PostgreSQL, TLS, checklist, verification. |
| [`docs/SECURITY.md`](docs/SECURITY.md) | Security posture by mechanism, known gaps with their fixes, threat model. |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Layer map, both data paths, the three conditions, design decisions with counter-arguments. |
| [`docs/DATABASE_SCHEMA.md`](docs/DATABASE_SCHEMA.md) | The tables, the ER diagram, why the AI and human records are separate. |
| [`docs/API.md`](docs/API.md) | Every HTTP endpoint with request/response shapes. |
| [`docs/SYNTHETIC_DATASETS.md`](docs/SYNTHETIC_DATASETS.md) | Each dataset: how it is built, the correct answer and why. |
| [`docs/EVALUATION_METHODOLOGY.md`](docs/EVALUATION_METHODOLOGY.md) | Variables, ground truth, procedure, threats to validity. |
| [`docs/EXAMPLE_RESULTS.md`](docs/EXAMPLE_RESULTS.md) | Every figure with its provenance, the fault-injection experiment, the DATASET-005 walkthrough. |
| [`docs/RESEARCH_NOTES.md`](docs/RESEARCH_NOTES.md) | Producing dissertation results: what to run, vary, report, and how to phrase a claim. |
| [`docs/CASE_STUDIES.md`](docs/CASE_STUDIES.md) | The historical case-study mechanism and its provenance rules. |
| [`docs/LIMITATIONS_AND_FUTURE_WORK.md`](docs/LIMITATIONS_AND_FUTURE_WORK.md) | The least flattering document in the set - read it before quoting any result. |
| [`docs/TESTING.md`](docs/TESTING.md) | What the suite covers, how it is isolated, what is not covered. |
| [`docs/BUILD_SPEC.md`](docs/BUILD_SPEC.md) | The implementation contract, including the Python 3.9 constraints. |

## Limitations

The short list; the long one is [`docs/LIMITATIONS_AND_FUTURE_WORK.md`](docs/LIMITATIONS_AND_FUTURE_WORK.md).

* **The evaluation was run without a language model**; every default figure measures the pipeline.
* **n = 6.** Descriptive figures, not statistics.
* **The ground truth was authored by the same author as the system**, and the planted conditions
  are unambiguous. Real audit evidence is contradictory, partial and inconsistent.
* **Retrieval recall is saturated** at the corpus size used. **Citation verification is textual,
  not semantic.**
* **No human-subject evaluation.** Whether an auditor is faster, more accurate or appropriately
  sceptical with this tool is not measured.
* **No security controls at all**: no authentication, authorisation, rate limiting or encryption at
  rest. The launcher binds the console to loopback; the deployment scripts do not. Known gaps and
  their fixes: [`docs/SECURITY.md`](docs/SECURITY.md).
* **SQLite, single process**, synchronous runs, additive-only schema migration.
* **Evaluation runs leave their throwaway projects** in the database (marked demo) so their prompts
  and responses stay auditable; they appear in the **Current audit** selector.
* **The container image has not been built** on the author's machine
  ([`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)).

## Academic use and citation

This repository is the software artefact of a student research project, released under the
[MIT licence](LICENSE) for academic study, replication and criticism.

**If you reproduce results from it, report the configuration**: provider and model
(`mock` / `mock-rules-1.1` by default), `MOCK_SEED`, `MOCK_HALLUCINATION_RATE`, `PROMPT_VERSION`,
the retrieval strategy and `RETRIEVAL_TOP_K`, `CITATION_MATCH_THRESHOLD`, and the dataset
generator seed. All are recorded in every `EvaluationRun` and printed in the report header.
Results from different mock rule-set versions or prompt versions **must not be pooled**.

```bibtex
@software{auditai_2026,
  title  = {AuditAI: LLM-Assisted IT Audit Risk and Control Assessment System -
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
`app.evaluation.metrics`, the `n` beside every rate, the provenance labels in the report, the
"Prototype research risk scoring model" tag and the Demo mode notice are not decoration - they are
what stops a figure from this prototype being read as something it is not.

---

*AI assists, the auditor decides.*
