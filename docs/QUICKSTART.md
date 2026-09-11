# Quickstart

From a fresh clone to a completed control assessment, a recorded human review and a generated
audit report — offline, with no API key.

Every command and every quoted output below was executed against a clean copy of this repository
(CPython 3.9.6, macOS arm64, `LLM_PROVIDER=mock`). Where the output on your machine will
legitimately differ — timings, hashes, timestamps, ports — that is said.

**Total time: about five minutes, most of it the one-off `pip install`.**

> All evidence in this walkthrough is synthetic and describes no real organisation, system or
> person. Do not substitute real audit evidence: this prototype has no authentication, no access
> control and no encryption at rest.

---

## 0. Prerequisites

* **Python 3.9** (developed and verified on 3.9.6). Check with `python3 --version`.
* About 400 MB of disk for the virtual environment.
* No API key. No network access after the install step.

```bash
cd "/path/to/IT AUDIT TOOL"
```

All commands below are run from the repository root.

---

## 1. Install the dependencies

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

**What you should see** — a long `Collecting …` list ending in:

```
Successfully installed ... fastapi-0.128.8 ... numpy-2.0.2 ... pandas-2.3.3 ...
pydantic-2.13.5 ... scikit-learn-1.6.1 ... SQLAlchemy-2.0.52 ... streamlit-1.50.0 ...
```

*Measured: 52 s with a warm pip cache; first-ever install is longer because the wheels are
downloaded.* Every dependency is pinned in `requirements.txt`.

> Use `.venv/bin/python` explicitly for every command below (or activate the environment with
> `source .venv/bin/activate`). `run.py` also prefers `.venv/bin/python` internally when it spawns
> the server processes, so the two subcommands that start servers work either way.

---

## 2. Create the database and seed the control library

```bash
.venv/bin/python run.py init
```

**What you should see:**

```
==============================================================================
  Initialising database
==============================================================================
Database ready. Control library contains 14 controls.
```

*Measured: 0.8 s.*

This creates `data/audit.db` (SQLite, 12 tables) and loads the synthetic control library from
`data/controls/control_library.json`. It is **idempotent** — controls are upserted on their
business key, so running it again changes nothing.

Nothing here reaches the network. If you want to confirm the configuration first:

```bash
.venv/bin/python -c "from app.config import get_settings; print(get_settings().provider_summary())"
```

which prints `'llm_provider': 'mock'`, `'llm_api_key': '(not set)'`,
`'embedding_model': 'hashing-vectorizer'`.

---

## 3. Start the audit console

```bash
.venv/bin/python run.py ui
```

**What you should see:**

```
==============================================================================
  Streamlit interface -> http://localhost:8501
==============================================================================

  You can now view your Streamlit app in your browser.

  Local URL: http://localhost:8501
```

> **First-run only.** The very first `streamlit run` on a machine prints a welcome message and
> asks for an email address before it starts:
>
> ```
>       👋 Welcome to Streamlit!
>       ...
>       Email:
> ```
>
> **Press Enter** to skip it. Streamlit writes `~/.streamlit/credentials.toml` and never asks
> again. If you are launching from a script with no terminal attached, the process will exit at
> this prompt — create that file first, or start Streamlit with `--server.headless true`.

Open <http://localhost:8501>. You should get a dark audit console with a sidebar
(Overview / Engagement / Assessment / Research), a **MOCK PROVIDER** badge reading
*"Deterministic rule-based stand-in, not a language model"*, and, across the top:

> Every assessment in this application is AI-generated and requires auditor review. Nothing here
> is assurance or a statement of compliance.

Because the database is empty, the Dashboard shows a call to action:

> **Nothing to audit yet** — The demo engagement scopes five synthetic IT controls and ingests
> generated policy documents, configuration exports and user listings.

*(To use a different port: `STREAMLIT_PORT=8899 .venv/bin/python run.py ui`.)*

---

## 4. Load the demonstration evidence

Click **“Load demo project + synthetic evidence”**.

**What you should see** after it finishes:

```
Demo project ready: Privileged Access Management Audit. 9 file(s) ingested, 0 already present.
```

*Measured: about 40 s the first time.* Most of that is generating the evidence files, not
ingesting them; the generation is seeded, so a second run produces byte-identical files and
completes in a couple of seconds. The button is safe to press twice — a file already present under
the same name is skipped.

The **Evidence** page now lists nine files across five formats, all `PARSED` (36 chunks in total):

| File | Type | Chunks |
|---|---|---|
| `Multi_Factor_Authentication_Policy_v3.docx` | POLICY | 5 |
| `Privileged_Accounts_MFA_Export_2024-06-30.csv` | USER_LISTING | 5 |
| `Patch_Management_Standard_v2.pdf` | STANDARD | 2 |
| `Endpoint_Patch_Compliance_Export_2024-06-30.xlsx` | SYSTEM_REPORT | 5 |
| `Password_Policy_v4.txt` | POLICY | 6 |
| `Domain_Password_Settings_Export_2024-06-30.csv` | CONFIGURATION_EXPORT | 2 |
| `Screenshot_Narrative_Domain_Password_Settings.txt` | SCREENSHOT_NARRATIVE | 1 |
| `Change_Management_Policy_v5.docx` | POLICY | 5 |
| `Change_Tickets_Export_2024Q2.xlsx` | TICKET_EXPORT | 5 |

Each file carries a SHA-256 taken at upload (yours will match these, since the generator is
deterministic) and each chunk carries a source locator such as
`section '4. Evidence of operation' - paragraph 14` or
`rows 2-101 - columns: Account_Name, Account_Type, …`. Expand a file and open the chunk browser to
see the text a model will actually be shown.

---

## 5. Run the assessments

Go to **Assessments**. The panel is pre-filled with all five scoped controls and
**Experiment C — LLM + RAG + structured workflow + human review**. Click
**“Run assessment (5 control(s))”**.

*Measured: under 5 seconds for all five controls against the offline provider.*

**What you should see** — five results, statuses and risk bands exactly as below (the offline
provider is deterministic, so these reproduce):

| Control | | AI status | Risk |
|---|---|---|---|
| CONTROL-001 | Multi-Factor Authentication for Privileged Accounts | `POTENTIAL_DEFICIENCY` | CRITICAL (79.75) |
| CONTROL-002 | Dormant Account Management | `INSUFFICIENT_EVIDENCE` | HIGH (54.75) |
| CONTROL-003 | Security Patch Management | `POTENTIAL_DEFICIENCY` | HIGH |
| CONTROL-004 | Change Management Approval | `POTENTIAL_DEFICIENCY` | HIGH |
| CONTROL-005 | Password Policy Enforcement | `NOT_EFFECTIVE` | CRITICAL (82.12) |

Two of these are worth pausing on.

**CONTROL-002 came back `INSUFFICIENT_EVIDENCE`, and that is the correct answer.** The demo
engagement contains no dormant-account report. The system does not treat that silence as a
failure; it says the control could neither be confirmed nor challenged and names what it would
need. Insufficient evidence is counted separately from deficiencies in every figure this console
shows.

**CONTROL-005 came back `NOT_EFFECTIVE`, not `POTENTIAL_DEFICIENCY`**, because the configuration
export contradicts the policy outright: `Minimum_Password_Length = 8` against a documented
requirement of 14.

Open any result (**“Open an assessment”**) to see the four-column panel the whole design rests on:

```
THE CONTROL REQUIRES   |  THE EVIDENCE PROVES   |  THE AI INFERS  |  A HUMAN MUST VERIFY
(control library)      |  (verbatim + verified) |  (labelled)     |  (open questions)
```

For CONTROL-001 the middle column carries two citations, both `VERIFIED` at match score 1.00:

```
Multi_Factor_Authentication_Policy_v3.docx - section '4. Evidence of operation' - paragraph 14
  "The privileged account listing produced for audit must report the multi-factor
   authentication enrolment status of every account in the population."

Privileged_Accounts_MFA_Export_2024-06-30.csv - 'Population summary' - rows 2-101
  "MFA_Status: Enabled = 90 (90.0%) | Disabled = 10 (10.0%)"

2 of 2 citations verified against their chunk, 0 partial, 0 fabricated.
```

and the fourth column already contains the auditor's work list — *"Inspect each exception record
in the source system and confirm it is a genuine exception rather than an export artefact"*,
*"Establish whether any exception is covered by a documented and approved exemption"* — together
with the corroborating evidence that was never supplied (the MFA enforcement rule export, the
exception register, the authentication log extract).

Expand **Safety rails applied** at the top of the panel. On a clean run it reads:

```
human_review_enforced: every assessment produced by this system requires auditor review
before it can be relied upon.
```

---

## 6. Record the human review

Nothing above is an audit conclusion yet. Go to **Human Review** — the queue shows
**AWAITING REVIEW 5**.

1. Pick an assessment from **“Assessment to review”** (it opens with the most severe).
2. Read the four-column panel and the citations. This is the point of the exercise: the panel is
   laid out so you check the model's quotations against the evidence, not its prose against your
   intuition.
3. Choose a decision — **Accept finding** / Modify finding / Reject finding / Request more
   evidence. **Final status** and **Final risk level** are pre-filled with the AI's proposal;
   change them if you disagree.
4. Type a comment, e.g. *"Quotations checked back to the configuration export; the configured
   value of 8 is below the policy value of 14."*
5. Click **Record decision**.

**What you should see:** the queue drops to **AWAITING REVIEW 4**, **REVIEWS COMPLETED 1**, and
**STATUS AGREEMENT 100%** (`n = 1`). The decision is stored in a separate `human_reviews` row —
the AI's assessment is never overwritten — and the system computes `agreed_with_ai_status` and
`agreed_with_ai_risk` for you, so agreement is a measured quantity rather than an impression.

If you flag an output as a suspected fabrication, the form requires a note before it will submit.

---

## 7. Generate the report

Go to **Reports**, leave the format as **Markdown**, and click **Generate report**.

**What you should see first** — a warning that is the system working correctly:

> 4 of the assessed control(s) have no auditor decision. They will appear in the report as pending
> review and will be excluded from every conclusive figure in it, which is the correct treatment —
> not an omission.

The report is written to `data/reports/` and shown in the page. The file from this walkthrough:

```
data/reports/audit_report_p1_privileged-access-management-audit_20260910T235654Z.md
795 lines, 57 KB
```

**Check these four things, in this order** — they are the design claims made visible:

1. **The executive summary counts only what an auditor concluded.**

   ```
   This project covers 5 control(s): 5 assessed by the system, 1 with an auditor's decision
   recorded, 4 still PENDING AUDITOR REVIEW, and 0 not assessed at all.

   Counting basis. Conclusive figures count only controls where an auditor has recorded a
   decision other than PENDING. ...
   ```

   The four unreviewed controls are listed separately, with their AI status marked
   *"(advisory)"*.

2. **Section 4 prints the full SHA-256 of every evidence file**, its size, upload time and parse
   status — and Section 10 states exactly what a hash does *not* establish.

3. **Section 7 resolves every citation** back to filename, locator and verbatim quotation, with
   its mechanical verification verdict beside it — including any that failed.

4. **Section 10, Limitations, is part of the document**, not a footer. It states that the
   assessments are machine-generated and advisory; that **they were not produced by a language
   model at all** when the mock is in use; that evidence authenticity, population completeness and
   period coverage cannot be established; that citation verification is textual, not semantic;
   that no legal or regulatory opinion is expressed; and that the risk model is a prototype
   research construct.

Generate the HTML format too if you want the printable version. Reports are kept, so any figure
quoted in a write-up can be traced back to the document it came from.

---

## You are done

You have run the full loop: **ingest → retrieve → assess → mechanically validate → risk-score →
human review → report.** Stop the server with `Ctrl-C`.

---

## Optional next steps

### Run the research experiments

```bash
.venv/bin/python run.py evaluate
```

*Measured: 8.6 s for all three conditions over six datasets (18 assessments).* It creates an
isolated project per dataset, ingests the generated files, assesses, scores against ground truth,
and persists an `EvaluationRun` with its metrics.

> **Two known defects in `run.py`**, both verified, both outside the evaluation code:
>
> 1. The summary line it prints reads `accuracy=0.000 macro_f1=0.000` for every condition. It is
>    reading `metrics["accuracy"]`, but `compute_metrics` nests those figures under
>    `metrics["classification"]`. **The stored results are correct** — only the printed line is
>    wrong.
> 2. `run.py evaluate` creates the schema but does not seed the control library. Against a
>    database that has never had `run.py init` run on it, every dataset errors with
>    *"Control 'CONTROL-001' not found"* and the run is recorded as failed. You did step 2, so you
>    are fine — but do not skip it.
>
> Read the real figures on the **Evaluation** page, or with:
>
> ```bash
> .venv/bin/python -c "
> from app.database.base import session_scope
> from app.evaluation import runner
> with session_scope() as s:
>     print(runner.compare_runs(s, [r.id for r in runner.list_runs(s)])
>           [['run_id','n','accuracy','macro_f1','grounding_rate']].to_string(index=False))
> "
> ```

Expected (offline provider, `mock-rules-1.1`, seed 1337, n = 6; runs are listed newest first, so
run 1 is condition A, run 2 is B and run 3 is C):

```
 run_id  n  accuracy  macro_f1  grounding_rate
      3  6  1.000000  1.000000             1.0     <- C, RAG + workflow
      2  6  1.000000  1.000000             1.0     <- B, RAG
      1  6  0.666667  0.464286             0.0     <- A, raw baseline
```

The **Evaluation** page renders the confusion matrices, per-class figures, the per-dataset
prediction table and the A/B/C comparison, each printed next to its `n` and its automatic caveats.
Read [`docs/EXAMPLE_RESULTS.md`](EXAMPLE_RESULTS.md) before quoting any of it — in particular, B =
C here is a **null result** that reproduces, and 1.000 is six correct answers to six designed
questions, not a claim that the pipeline is accurate.

### See the anti-hallucination rails actually fire

```bash
MOCK_HALLUCINATION_RATE=1.0 .venv/bin/python run.py evaluate --modes C_RAG_WORKFLOW
```

The mock then deliberately fabricates a citation on every assessment. On DATASET-003 the validator
catches it and the rails withdraw the conclusion:

```
predicted: INSUFFICIENT_EVIDENCE   (original model status: NOT_EFFECTIVE)
citations: 2 total - 1 VERIFIED (1.00), 1 FABRICATED (0.09)
rails_applied:
  - critique_downgrade: ... the citation validator independently agreed the conclusion
    was unsupported (1 citation(s) were fabricated).
  - fabricated_citations: 1 citation(s) could not be resolved to the retrieved evidence
    (position 2). They are retained and flagged, not removed.
  - human_review_enforced: ...
```

### Start the REST API

```bash
.venv/bin/python run.py api          # then open http://localhost:8000/docs
```

Interactive OpenAPI docs, and `GET /health` reports the database and the resolved provider with
the API key masked. `run.py all` starts the API and the UI together (verified: both answering in
about 5 s).

The Streamlit app talks to the service layer **in process** by default and needs no backend. To
route it through HTTP instead, set `USE_API=true` (and `API_BASE_URL` if the port is not the
default) before starting the UI.

> The API has **no authentication, no authorisation and no rate limiting**. It says so in `GET /`,
> in `GET /health` and in the OpenAPI description. Bind it to localhost.

### Run the same loop with no browser

Save this as `demo_run.py` **in the repository root** (Python puts the script's own directory on
the import path, so it will not find `app` from elsewhere):

```python
"""End-to-end with no browser: ingest -> assess -> review -> report."""
from app.database.base import init_db, session_scope
from app.database.seed import bootstrap
from app.evaluation.datasets import generate_dataset, get_dataset
from app.evidence.service import ingest_file
from app.audit.engine import AssessmentEngine
from app.audit import report, service
from app.schemas.enums import ExperimentMode, HumanDecision

init_db()
with session_scope() as session:
    project_id = bootstrap(session)["demo_project_id"]

    dataset = get_dataset("DATASET-001")
    for path in generate_dataset("DATASET-001"):
        ingest_file(
            session, project_id, path.read_bytes(), path.name,
            evidence_type=dataset.file(path.name).evidence_type,
            uploaded_by="Research Auditor", is_synthetic=True,
        )

    result = AssessmentEngine(session).assess_control(
        project_id, "CONTROL-001", mode=ExperimentMode.C_RAG_WORKFLOW
    )
    print("status   :", result.output.status.value)
    print("risk     :", result.risk.level.value, round(result.risk.score, 2))
    print("citations:", result.validation.verified, "verified /", result.validation.total)

    review = service.record_human_review(
        session, result.assessment_id, "Research Auditor", HumanDecision.ACCEPTED,
        final_status=result.output.status.value,
        final_risk_level=result.risk.level.value,
        comments="Quotations traced back to the export; conclusion accepted.",
    )
    print("review   :", review.decision, "| agreed:", review.agreed_with_ai_status)

    doc = report.generate_report(session, project_id, generated_by="Research Auditor")
    print("report   :", doc.stored_path)
```

```bash
.venv/bin/python demo_run.py
```

**Verified output:**

```
status   : POTENTIAL_DEFICIENCY
risk     : CRITICAL 79.75
citations: 5 verified / 5
review   : ACCEPTED | agreed: True
report   : .../data/reports/audit_report_p1_privileged-access-management-audit_...md
```

To keep it out of your main database, point it at a scratch one:

```bash
DATABASE_URL="sqlite:///$PWD/scratch.db" UPLOAD_DIR="$PWD/scratch/uploads" \
REPORT_DIR="$PWD/scratch/reports" .venv/bin/python demo_run.py
```

*(Verified: the report is written to the overridden directory.)*

### Run the test suite

```bash
.venv/bin/python -m pytest tests -q
```

**Verified:** `713 passed, 2 warnings in 57.90s`. The two warnings are Starlette deprecations for
HTTP status-code constants, raised by a dependency and not by this project. The suite is hermetic:
it redirects the database and every writable directory into a temporary tree before `app.config`
is imported, and makes no network call — your `data/audit.db` is untouched.

### Start over

```bash
.venv/bin/python run.py reset          # prompts: type 'yes' to confirm
```

Drops every table and re-seeds the control library. It does **not** delete files under
`data/uploads/`, `data/reports/` or `data/synthetic/`; remove those by hand if you want a
genuinely clean tree.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Streamlit stops at `Email:` and never starts | First-ever Streamlit run on this machine. Press **Enter**. With no terminal attached the process exits — create `~/.streamlit/credentials.toml` containing `[general]` / `email = ""`, or pass `--server.headless true`. |
| `ModuleNotFoundError: No module named 'app'` | You ran a script from outside the repository root. Put the script in the root, or run `python -m` from the root. |
| `Address already in use` | Something else holds 8501/8000. Use `STREAMLIT_PORT=8899 .venv/bin/python run.py ui` or `API_PORT=8010 .venv/bin/python run.py api`. |
| Dashboard is empty after loading the demo | Check the **Audit project** selector in the sidebar — it may be on a different engagement. After an evaluation run the selector also lists the `[EVALUATION] …` throwaway projects; pick *Privileged Access Management Audit*. |
| `run.py evaluate` prints `accuracy=0.000` | Known `run.py` defect; the stored results are correct. See the box above. |
| `SyntaxError` on startup | Wrong interpreter. This project targets Python 3.9; run everything through `.venv/bin/python`. |
| Everything is slower than the timings here | The first demo load generates PDF/DOCX/XLSX files and the first Streamlit page paint compiles the theme. Subsequent runs are much faster. Timings here are from Apple Silicon with the offline provider; a hosted model turns seconds into minutes. |

---

## Where to go next

* [`../README.md`](../README.md) — what the system is, what it is not, and the safety rules with
  the module that enforces each.
* [`SETUP.md`](SETUP.md) — the full installation, configuration and operations reference: every
  setting, PostgreSQL, running the UI against the API, and security notes. This quickstart is the
  fast path; SETUP is the complete one.
* [`ARCHITECTURE.md`](ARCHITECTURE.md) — how a request actually flows through the layers.
* [`API.md`](API.md) — every HTTP endpoint with request and response shapes.
* [`TESTING.md`](TESTING.md) — what the suite covers, how it is isolated, and what it does not
  cover.
* [`RESEARCH_NOTES.md`](RESEARCH_NOTES.md) — which experiments to run for a dissertation, what to
  vary, and how to phrase the claims.
* [`SYNTHETIC_DATASETS.md`](SYNTHETIC_DATASETS.md) — what is inside each dataset and why its
  answer is the correct one.
* [`EXAMPLE_RESULTS.md`](EXAMPLE_RESULTS.md) — every figure with its provenance and its caveats.
* [`LIMITATIONS_AND_FUTURE_WORK.md`](LIMITATIONS_AND_FUTURE_WORK.md) — read this before quoting
  any result.
