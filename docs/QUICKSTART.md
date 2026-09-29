# Quickstart

From a fresh clone to a generated audit report in the browser, offline, with no API key. One
path, in order. Every button and field is quoted exactly as the console shows it.

> All evidence in this walkthrough is synthetic and describes no real organisation, system or
> person. Do not substitute real audit evidence: this prototype has **no authentication, no
> access control and no encryption at rest**.

---

## 0. Prerequisites

* **Python 3.9 to 3.12.** Check with `python3 --version`. Python 3.13 is not supported: the
  pinned `numpy==2.0.2` has no wheels for it. The code is written to 3.9 syntax.
* Roughly 400 MB of disk for the virtual environment.
* No API key. No network access after the install step.

All commands are run from the repository root:

```bash
cd "/path/to/IT AUDIT TOOL"
```

---

## 1. Install

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Every dependency is pinned in `requirements.txt` (the one exception, `anthropic`, is a floor
rather than a pin; [`SETUP.md`](SETUP.md) says why). Use `.venv/bin/python` for every command
below, or `source .venv/bin/activate` first. `run.py` prefers `.venv/bin/python` when it spawns
the server, so `python run.py` works either way once the environment exists.

---

## 2. Start the console

```bash
.venv/bin/python run.py
```

`python run.py` with no argument starts the audit console (the same as `run.py ui`), bound to
`127.0.0.1`, and opens your browser a moment later. The terminal prints:

```
==============================================================================
  AuditAI console
==============================================================================
  Console : http://127.0.0.1:8501
  Demo mode - offline rule-based assistant, no API key needed
  First time? Press 'Try the demo audit' on the home page.
  Press Ctrl-C to stop.
```

If the browser does not open, go to <http://127.0.0.1:8501> yourself. Set `NO_BROWSER=1` to
stop the launcher opening one; set `STREAMLIT_PORT` in `.env` if 8501 is taken (the launcher
refuses to start on a busy port and says so).

There is nothing to initialise first. The console creates the database and seeds the control
library itself on its first run, so `run.py init` is optional.

**What you should see.** The **AuditAI** brand in the sidebar and, on the page, a greeting
(*Good morning, Auditor*) above two boxes: **Start an audit** and **Try the demo audit**. In
the sidebar:

* **Current audit** — reads *No audit project yet* until one exists, with a link *Start an
  audit or try the demo*.
* **Your name** — with the note *Enter your name before recording a review*.
* A **DEMO MODE** badge with the line *Offline rule-based assistant - not an AI language
  model. Results measure the workflow, not model quality.*, and an **AI provider settings**
  link.
* The footer: *AI assists, the auditor decides.*

Below the two boxes the page shows **Your audit overview** (Active audits / Awaiting review),
then, once an audit exists, the five-step strip — **1 Controls** *Choose the controls*,
**2 Evidence** *Add evidence*, **3 Assess** *Run the AI assessment*, **4 Review** *Record your
decisions*, **5 Report** *Generate the report* — then **What do you need to do?**, **Requires
your attention** and a collapsed **Figures and activity for this audit** expander.

**Type your name into "Your name" now.** Every decision you record is attributed to it, and the
**Record decision** button stays disabled until a name is present.

---

## 3. Load the demo audit

Press **Try the demo audit**.

A status box reads *Loading the demo audit...* and lists each file as it is generated and
indexed, then *Demo audit ready: 9 file(s) loaded*. The console switches the sidebar to
**Privileged Access Management Audit (demo)** and lands on **Assessments** with the message
*Demo audit ready: 9 files loaded. Next: press Run assessment.*

The demo audit puts five controls in scope (CONTROL-001 to CONTROL-005) and ingests nine
synthetic files:

| File | Evidence type |
|---|---|
| `Multi_Factor_Authentication_Policy_v3.docx` | Policy |
| `Privileged_Accounts_MFA_Export_2024-06-30.csv` | User listing |
| `Patch_Management_Standard_v2.pdf` | Standard |
| `Endpoint_Patch_Compliance_Export_2024-06-30.xlsx` | System report |
| `Password_Policy_v4.txt` | Policy |
| `Domain_Password_Settings_Export_2024-06-30.csv` | Configuration export |
| `Screenshot_Narrative_Domain_Password_Settings.txt` | Screenshot narrative |
| `Change_Management_Policy_v5.docx` | Policy |
| `Change_Tickets_Export_2024Q2.xlsx` | Ticket export |

The generator is seeded, so a second load produces the same bytes and skips files already
present. Once loaded, the Home button reads **Open the demo audit** instead. The same loader is
available from the terminal as `.venv/bin/python run.py seed-demo`.

If you want to look at the files first, open **Evidence**: every file shows its parse status and
the number of **Passages** — the pieces of text the AI can quote. Select a row and the
**Passages** section shows exactly what is stored, with the locator a citation will point at.

---

## 4. Run the assessment

You are on **Assessments**. The run panel at the top reads *5 controls in scope · 0 assessed ·
5 not yet assessed*.

1. **Which controls** is set to **Not yet assessed** (the alternatives are **All in scope** and
   **Choose...**). Leave it.
2. Leave **Research options** collapsed. It holds the **Pipeline** choice, which defaults to
   **Full audit workflow (recommended)**; the other two entries are the research baselines.
3. Press **Run assessment (5 controls)**.

A status box reports *Assessing CONTROL-001 (1 of 5)...* and so on, then *Assessed 5 of 5*,
with one line per control giving its conclusion and risk band. The page reruns with the
message *Assessed 5 controls. Every result needs your review.* and two links, **Review now**
and **See findings**.

**What you should see** in the **Results** table (one row per control, most recent
assessment). The offline provider is deterministic, so these reproduce:

| Control | AI conclusion |
|---|---|
| CONTROL-001 Multi-Factor Authentication for Privileged Accounts | Potential deficiency |
| CONTROL-002 Dormant Account Management | Insufficient evidence |
| CONTROL-003 Security Patch Management | Potential deficiency |
| CONTROL-004 Change Management Approval | Potential deficiency |
| CONTROL-005 Password Policy Enforcement | Not effective |

Every row's **Auditor decision** column reads *Awaiting decision*, and **Fabricated quotes**
is 0 throughout. **Show every run** and **Show research columns** widen the table; the
**Filter** popover narrows it.

Two rows are worth pausing on. **CONTROL-002 is "Insufficient evidence", and that is the right
answer**: the demo contains no dormant-account report, and the system says the control could
not be tested rather than calling it a deficiency. Insufficient evidence is counted separately
from deficiencies everywhere in the console. **CONTROL-005 is "Not effective"** because the
configuration export contradicts the policy outright.

### Read one assessment

The first new result is already open below the table (select any row to open another). The
assessment screen is the same wherever you meet it:

* A header with the control's name, the badges (conclusion, risk band, evidence sufficiency)
  and one banner: **AI-generated - requires auditor review**.
* **What did we find?** — the AI's finding in its own words, unedited.
* **1. What the control requires** — from the control library, not from the AI.
* **2. What the evidence shows** — the quotations, each mechanically re-checked against the
  stored passage. Press **Show the source passage** on a citation to see the rows or
  paragraph it came from, with the quoted words highlighted.
* **3. What the AI infers** — the AI's conclusions; nothing here is established by the quotes.
* **4. What still needs verification** — the points a person must check and the evidence the
  AI says it did not have.
* **Auditor review** — the decision form (next step).
* **Details for researchers (risk arithmetic, retrieval, validation, prompt)** — folded away.

---

## 5. Record a decision

You can record the decision on the assessment screen you are looking at, or from the
**Review queue** page, which shows the same screen for each item awaiting a decision, with an
**Assessment to review** picker, *Item 1 of 5 awaiting a decision* and a **Skip to next**
button. The steps are the same either way.

1. Under **Auditor review**, choose **Your decision**: **Accept finding**, **Modify finding**,
   **Reject finding** or **Request more evidence**.
2. **Your conclusion** and **Your risk level** are pre-filled only for *Accept finding*, because
   that is what accepting means. *Modify* and *Reject* leave them blank for you to choose.
   *Request more evidence* locks them to *Insufficient evidence* and *Not rated*.
3. Write a **Comments** line, e.g. *Quotations checked against the export; the configured
   value is below the policy value.* **Your finding** and **Your recommendation** may be left
   blank, in which case the AI text stands unchanged and is recorded as such.
4. Press **Record decision**. (If it is disabled, the sidebar **Your name** field is empty.)

**What you should see.** The message *Decision recorded: Accepted for CONTROL-00n*, the AI
proposal and your conclusion side by side, and, on the Review queue, the counter down to
*Item 1 of 4 awaiting a decision*. The tabs **Completed reviews (1)** and **Agreement so
far** below the queue show the history and the agreement figures with their denominator.

Your decision is a separate record. The AI assessment is never edited, which is what makes
agreement between the two measurable. A **Request more evidence** decision never counts as
agreement.

---

## 6. Generate the report

Open **Reports**.

Before the form, a pre-flight notice says *4 assessed control(s) have no auditor decision yet.
They will appear as 'Pending auditor review' and are excluded from every conclusive figure.*
with a **Review them now** link. That is the system working correctly: an AI proposal nobody
has reviewed is not a conclusion.

Leave **Format** on **HTML (.html)** and press **Generate report**.

**What you should see.** *Report #1 generated.*, then the report itself with a **Download
report (HTML)** button and the path it was also saved to under `data/reports/`. Earlier
reports are kept under **Previous reports (n)**.

Read four things in the document:

1. The **executive summary** counts only controls with an auditor decision; the four unreviewed
   controls are listed as *PENDING AUDITOR REVIEW*, with the AI status marked advisory.
2. **Section 4, Evidence Reviewed**, prints the SHA-256 of every file.
3. The **evidence references** resolve every citation to filename, locator and verbatim
   quotation, with its verification verdict beside it.
4. **Section 10, Limitations**, is part of the document. It states that the assessments are
   machine-generated and advisory, and, when Demo mode answered, that they were produced by
   the offline stand-in and not by a language model.

Stop the server with `Ctrl-C`.

---

## Where next

* **Start an audit of your own.** On Home, **Start an audit** opens the **New audit** dialog:
  **Audit name**, **Audit area**, **Controls to test** (or tick **Start with the standard set
  of five controls**), **More details**, then **Create audit**, which lands on **Evidence**.
  Upload files with **Upload and index**, then run the assessment as above.
* [`USER_GUIDE.md`](USER_GUIDE.md) — every page of the console, the assessment screen in
  detail, the four decisions, and a glossary.
* [`SETUP.md`](SETUP.md) — every `run.py` subcommand, every setting, switching on Claude or an
  OpenAI-compatible endpoint, PostgreSQL, and troubleshooting.
* **Use a real model.** Add `LLM_PROVIDER=claude` and `ANTHROPIC_API_KEY=...` to `.env` and
  restart; see the Claude section of [`SETUP.md`](SETUP.md). If the key is missing, the sidebar
  shows a red *Claude was configured but Demo mode answered* alert with the fix.
* **Run the research experiments.** `.venv/bin/python run.py evaluate` runs conditions A/B/C
  over the synthetic datasets; the **Experiments** page shows the comparison. Read
  [`EXAMPLE_RESULTS.md`](EXAMPLE_RESULTS.md) and
  [`LIMITATIONS_AND_FUTURE_WORK.md`](LIMITATIONS_AND_FUTURE_WORK.md) before quoting any figure.
* **Run the test suite.** `.venv/bin/python -m pytest tests -q`. The suite redirects the
  database and every writable directory into a temporary tree and makes no network call.
  See [`TESTING.md`](TESTING.md).
* [`../README.md`](../README.md) — what the system is, what it is not, and the safety rules with
  the module that enforces each.
