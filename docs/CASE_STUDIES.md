# Historical case studies

How this project tests itself against a *realistic* control failure without ever asserting anything
about a real organisation — what the mechanism is for, the three provenance classes and their exact
labels, how to add a case study, and the one rule that is not negotiable.

> **The rule.** Reconstructed evidence is **never** presented as a real organisation's evidence. The
> failure pattern is real, public and documented. The organisation, the accounts, the vendors, the
> dates, the reference numbers and every figure are invented, and every artefact says so — in the
> file itself, in the database row, on the screen and in the engagement description. This is not a
> legal precaution. It is the same principle the whole project exists to defend: **a claim you
> cannot source is a fabrication, however useful it would be.**

Companion documents:
[SYNTHETIC_DATASETS.md](SYNTHETIC_DATASETS.md) (the evaluation datasets, which are a different
thing — see [below](#a-case-study-is-not-an-evaluation-dataset)) ·
[EVALUATION_METHODOLOGY.md](EVALUATION_METHODOLOGY.md) ·
[SECURITY.md](SECURITY.md) ·
[USER_GUIDE.md](USER_GUIDE.md).

**Contents**

- [What the mechanism is for](#what-the-mechanism-is-for)
- [The three provenance classes](#the-three-provenance-classes)
- [The rule, and the reasoning behind it](#the-rule-and-the-reasoning-behind-it)
- [What ships: CASE-001](#what-ships-case-001)
- [Running a case study](#running-a-case-study)
- [How a manifest becomes an engagement](#how-a-manifest-becomes-an-engagement)
- [Adding a new case study](#adding-a-new-case-study)
- [Manifest field reference](#manifest-field-reference)
- [A case study is not an evaluation dataset](#a-case-study-is-not-an-evaluation-dataset)
- [Known gaps](#known-gaps)
- [Checklist before you commit a new case study](#checklist-before-you-commit-a-new-case-study)

---

## What the mechanism is for

The six synthetic datasets in `app/evaluation/datasets.py` are built so the correct answer is true
*by construction*: 90 of 100 accounts have MFA enabled, therefore the control is deficient, and no
argument is needed. That is exactly what a metric needs and exactly what makes them unconvincing as
a demonstration. They are clean, single-attribute, one-threshold cases. Real control failures are
none of those things.

A **case study** fills that gap. It is a declarative JSON manifest under `data/case_studies/` that
describes:

* a **publicly documented failure pattern** — the sequence of things that actually goes wrong, as
  described in national cyber-security advisories, standards guidance and annual industry breach
  studies;
* the **controls** it exercises;
* the **evidence artefacts** an auditor would have to test to detect it — policies, standards,
  account listings, vendor registers, alert-review exports;
* the conclusion the case author argues an auditor should reach, **with the reasoning**;
* the **sources** for the pattern, and an explicit policy about how those sources were recorded;
* the case study's own **limitations**.

`import_case_study()` turns that manifest into a real audit engagement: a project, its scoped
controls, and the generated files pushed through the same ingestion path as any upload — parsing,
chunking, locator construction and indexing included. A case study that bypassed ingestion would
demonstrate nothing about the pipeline it exists to exercise.

The result is a test case where the deficiency is genuinely *arguable*: several controls interact,
some expected evidence is present and some is absent, and a defensible auditor could disagree with
the case author about at least one conclusion. Which, as it turns out, is what happened — see
[What ships](#what-ships-case-001).

---

## The three provenance classes

Provenance answers **where an artefact came from**, which is a different question from
`EvidenceType`'s **what the artefact is**. A privileged account listing fabricated by this project's
generators and a privileged account listing exported from a client's identity provider are the same
*type* and are emphatically not the same *evidence*.

The vocabulary is `EvidenceProvenance` in `app/schemas/enums.py`, and it is stored as a column —
`EvidenceFile.provenance`, `String(32)`, `NOT NULL`, default and server-default `SYNTHETIC`, indexed,
placed directly after `evidence_type`. **The column is the load-bearing part**: a disclaimer in a
docstring protects nobody, and filenames get renamed.

| Value | Badge (`PROVENANCE_BADGES`) | Colour in the console | Meaning |
|---|---|---|---|
| `SYNTHETIC` | `SYNTHETIC` | grey — the quiet default | Fabricated by this project's own generators for testing and demonstration. Represents no real organisation, system, person, incident or audit. |
| `HISTORICAL_PUBLIC` | `HISTORICAL RECONSTRUCTION` | amber-orange — attention, not error | A synthetic reconstruction derived from publicly documented facts about a real, historically documented failure pattern. The *pattern* is real and publicly reported; the files, names, accounts, dates and figures are invented to illustrate it. **Not** the original organisation's evidence, never obtained from one, and never to be cited as though it were. |
| `ORGANISATIONAL` | `ORGANISATIONAL EVIDENCE` | **red** | Real evidence obtained from a real audited entity. Never present in this repository and never produced by any generator in it. |

### The exact disclosure sentences

These are `PROVENANCE_LABELS` in `app/schemas/enums.py`, defined **once** and reused verbatim by the
project description, every evidence file's description, the Evidence page banner and the upload
caption. A disclaimer that is re-worded per screen is a disclaimer that will eventually be dropped
from one of them.

> **`SYNTHETIC`** — "Synthetic test data generated by this research prototype. It represents no real
> organisation, system, person or audit."
>
> **`HISTORICAL_PUBLIC`** — "Synthetic research dataset derived from publicly documented historical
> facts. It reconstructs a publicly reported failure pattern using invented organisation and account
> names; it is not the original organisation's evidence."
>
> **`ORGANISATIONAL`** — "Real evidence supplied by an audited entity. Handle under the engagement's
> confidentiality terms."

### Why `ORGANISATIONAL` is drawn in red

It looks backwards for about two seconds, and then it does not. This prototype has no
authentication, no access control and no encryption at rest, and it writes evidence to local disk in
the clear. **Real audit evidence held here is a condition to flag, not a badge of quality**, and the
banner says so in those words. The member exists so that a deployment which one day does hold real
evidence can say so explicitly — so "is this real?" is always an answered question rather than an
assumption.

Violet is deliberately avoided for all three: it already means `INSUFFICIENT_EVIDENCE` throughout
the console, and that is the one distinction this application cannot afford to blur.

### Where provenance appears

1. **In the file itself.** Markdown and text artefacts open with a six-line notice (a blockquote in
   `.md`, so it renders as a callout). CSV exports carry a reserved first column, `Data_Origin`,
   whose value is `SYNTHETIC-HISTORICAL-RECONSTRUCTION` on **every record** — not a header comment,
   because `app/evidence/parsers.py` reads the first non-blank line as the column row and a `#`
   preamble would corrupt the export rather than annotate it. Carrying it as a field is strictly
   stronger anyway: it survives into every `TABLE_ROWS` chunk the model is shown and every row an
   auditor reads, not just the top of the file.
2. **On the evidence row**, as the `provenance` column, which is what every other surface reads.
3. **In the evidence description**, disclosure first. The stored description of CASE-001's account
   listing is, verbatim:

   > Synthetic research dataset derived from publicly documented historical facts. It reconstructs a
   > publicly reported failure pattern using invented organisation and account names; it is not the
   > original organisation's evidence. | Case study CASE-001 | Aldermere Regional Utilities
   > (fictional) does not exist | Reconstructed privileged account listing as at 30 June 2024,
   > showing enrolment status, account status and last successful logon for each privileged account.

   Disclosure leads because descriptions are truncated in table views, and a warning that only
   survives when the column is wide is not a warning.
4. **In the engagement description**, which opens with the same sentence and then sets out the
   pattern, the sources, the sourcing policy and the limitations.
5. **On the Evidence page**, in four places: a column on every table row, a counts badge row plus a
   banner above the list whenever anything non-synthetic is held, a full-width banner above the
   detail panel, and a badge with a compact banner on each ingestion result. There is also a
   provenance filter on the list.
6. **In the activity trail**, as `ActivityAction.CASE_STUDY_IMPORTED`, with the case id, the
   organisation label, the control refs, every file's hash and the disclaimer in the details payload
   — so the append-only trail states in its own vocabulary that this engagement's evidence is a
   reconstruction rather than something an auditor uploaded.

The one place it does **not** yet appear is the generated report. See [Known gaps](#known-gaps).

---

## The rule, and the reasoning behind it

**Reconstructed evidence is never presented as a real organisation's evidence.** Concretely, a case
study must never name a company, an incident, a regulator finding or a specific advisory as the
subject of its artefacts, and no file it produces may read as though it were exported from a named
organisation's systems.

The tempting version of this feature is the opposite: take a breach everyone has heard of,
reconstruct "its" account listing, and let the tool find the deficiency that was reported in the
press. It would be a better story. It is indefensible, for two reasons that have nothing to do with
legal caution.

**1. The evidence is not public.** What is public about any real incident is a *narrative*: press
coverage, an advisory, sometimes a regulatory finding. The account listings, the alert queues and
the vendor registers an auditor would actually test have never been published — no auditor outside
the affected organisation has seen them. Anything written under a real company's name would
therefore be invented detail attributed to a real organisation. That is fabrication, however well
intentioned, and it is **precisely the failure mode this entire project exists to detect**. A
dissertation that builds a hallucination detector on top of a fabricated premise has argued against
itself before the first result.

**2. A file that reads as real evidence will eventually be quoted as real evidence.** The
reconstruction is useful precisely because it is realistic. Realism plus a real name is how a
synthetic account listing ends up in somebody's slide deck as a fact about a named company — not
through malice, but through one screenshot separated from its context. The provenance column, the
in-file notices, the badges and the banners all exist because that separation is the normal fate of
a convincing artefact.

Naming no company costs the case study nothing: the pattern is what teaches, and the pattern is
generic. It removes every claim this project cannot source.

### Sourcing discipline

Each manifest carries a `sourcing_policy` string and a list of `public_sources`. The rule the shipped
case study states, and which any new one should follow:

> A URL is recorded only where the resource was retrieved and its title confirmed while the manifest
> was written, and only for durable publications of standards bodies and established industry
> reports. No URL is recorded for reporting about any specific incident, and no specific incident,
> company or regulator finding is named anywhere.

Every source carries an explicit `url_confirmed` boolean, so **a reader never has to infer which is
which**. A source described generically with an empty `url` is a deliberate choice — it is how this
project refuses to invent a citation — and it must be distinguishable from an oversight.

---

## What ships: CASE-001

`data/case_studies/case-001-privileged-remote-access-mfa.json`.

**Pattern:** credential-based intrusion through a remote-access account that was never enrolled in
multi-factor authentication.

**The documented sequence**, reconstructed as testable evidence: a policy requires MFA for
privileged access and requires any non-enrollable account to be recorded in an exception register
with a named approver and an expiry date; enforcement is applied through a platform policy scoped to
interactive administrator accounts, leaving service, break-glass, legacy maintenance and vendor
accounts outside its scope; no exception register is maintained, so the residual population is
neither approved nor tracked; one such remote-access account is dormant; an attacker authenticates
to it with a password alone; detection rules raise alerts on the resulting out-of-hours
administrative logon and privileged group change, but the alerts are never triaged; and third-party
access to the same environment runs partly on shared, non-expiring vendor credentials, so activity
cannot be attributed to an individual.

**The organisation is fictional:** Aldermere Regional Utilities, with vendors Calder Process
Systems, Halloway Metering, Brightpath Analytics, Orrin Controls and Tarrant Field Services. None
exists. Every account name, date, reference and figure is invented.

**Controls exercised** (four, from the shipped library):

| Control | Name | Expected status | Expected risk |
|---|---|---|---|
| `CONTROL-001` | Multi-Factor Authentication for Privileged Accounts | `POTENTIAL_DEFICIENCY` | HIGH |
| `CONTROL-002` | Dormant Account Management | `POTENTIAL_DEFICIENCY` | MEDIUM |
| `CONTROL-012` | Privileged Activity Monitoring and Alert Review | `NOT_EFFECTIVE` | HIGH |
| `CONTROL-013` | Third-Party Remote Access Authorisation | `NOT_EFFECTIVE` | HIGH |

**Evidence artefacts** (six):

| File | Format | Type | Role |
|---|---|---|---|
| `CS001_Remote_Access_Authentication_Policy.md` | text | `POLICY` | requirement |
| `CS001_Access_Management_Standard.md` | text | `STANDARD` | requirement |
| `CS001_Privileged_Account_Listing.csv` | table | `USER_LISTING` | population |
| `CS001_Third_Party_Remote_Access_Accounts.csv` | table | `USER_LISTING` | population |
| `CS001_Privileged_Alert_Review_Export.csv` | table | `LOG_EXTRACT` | operation |
| `CS001_Case_Study_Provenance_Notice.md` | text | `OTHER` | provenance |

**Sources:** four, three with confirmed URLs (NIST SP 800-63B; CISA's Cybersecurity Alerts &
Advisories; the annual Data Breach Investigations Report landing page) and one described generically
with `url_confirmed: false` — the body of public post-incident reporting the pattern is drawn from
in aggregate, for which no single URL can honestly be asserted.

### The result the tool actually produces, recorded rather than tuned away

With the offline mock provider, the system reaches the case author's expected conclusion on
`CONTROL-001` and reports a deficiency on `CONTROL-012`, but answers **`INSUFFICIENT_EVIDENCE` on
`CONTROL-002` and `CONTROL-013`**.

This was diagnosed rather than assumed. Retrieval surfaced the correct files in both cases. The
divergence comes from the sufficiency pre-check, which is *right* that the engagement supplies no
dormant-account report, no remediation export, no exception register, no vendor access-request
tickets and no remote session log — all of which those controls name in their expected evidence. So
the disagreement is about **how much of the expected evidence set must be present before a
deficiency may be asserted**, not about retrieval.

The expected outcomes were deliberately **not** rewritten to match what the tool produced, and the
evidence was deliberately not re-tuned until it agreed. Doing either would make this a test the
system had been taught to pass. The divergence and its cause are recorded in the manifest's
`limitations` array, so they travel with the case study rather than living in somebody's notes.

---

## Running a case study

There is no API endpoint and no console button yet (see [Known gaps](#known-gaps)); today the
feature is reachable from Python. The following was executed against a temporary SQLite database:

```python
from app.database.base import init_db, session_scope
from app.database.seed import bootstrap
from app.evaluation import case_studies

init_db()
with session_scope() as session:
    bootstrap(session)                       # the control library must exist first

    print(case_studies.case_study_ids())     # ['CASE-001']
    project = case_studies.import_case_study(
        session, "CASE-001", auditor_name="Your Name"
    )
    print(project.id, project.name)
```

Observed output, verbatim:

```
case studies available: ['CASE-001']
project id: 2 | name: CASE-001 - Privileged and third-party remote access without enforced multi-factor authentication
is_case_study_project: True
case id from scope_note: CASE-001
  CS001_Remote_Access_Authentication_Policy.md         POLICY       HISTORICAL_PUBLIC  chunks=  7
  CS001_Access_Management_Standard.md                  STANDARD     HISTORICAL_PUBLIC  chunks=  6
  CS001_Privileged_Account_Listing.csv                 USER_LISTING HISTORICAL_PUBLIC  chunks=  2
  CS001_Third_Party_Remote_Access_Accounts.csv         USER_LISTING HISTORICAL_PUBLIC  chunks=  2
  CS001_Privileged_Alert_Review_Export.csv             LOG_EXTRACT  HISTORICAL_PUBLIC  chunks=  2
  CS001_Case_Study_Provenance_Notice.md                OTHER        HISTORICAL_PUBLIC  chunks=  5
CSV first lines:
   Data_Origin,Account_Name,Account_Type,Department,MFA_Status,Account_Status,Last_Login,Created_Date,Export_Date
   SYNTHETIC-HISTORICAL-RECONSTRUCTION,adm.k.oyelaran,Domain Admin,IT Infrastructure,Enabled
assessment: POTENTIAL_DEFICIENCY | citations: 2 | grounding: 1.0
```

Then open the console (`.venv/bin/python run.py ui`), select the imported engagement in the sidebar,
and work it like any other: Evidence → Assessments (mode C) → Human Review → Reports.

### Useful helpers

| Function | Returns |
|---|---|
| `case_studies.case_study_ids()` | Every available case id. |
| `case_studies.list_case_studies()` | Loaded `CaseStudy` objects, sorted by id. A manifest that will not parse is logged as a warning and skipped, so one broken file never makes the feature unavailable. |
| `case_studies.case_study_summaries()` | The same, serialisable — ready for an API payload or a console picker. |
| `case_studies.get_case_study("CASE-001")` | One case study, or `UnknownCaseStudyError`. |
| `case_studies.build_case_study_files(case)` | The rendered bytes, without importing anything — useful for inspecting what *would* be written. |
| `case_studies.is_case_study_project(project)` | Whether a project came from an import. Tests the marker in `scope_note`, **not the name**, so renaming an engagement cannot make its evidence look organisational. |
| `case_studies.case_study_project_case_id(project)` | The `case_id` recorded in that scope note, or `""`. |
| `case_studies.set_evidence_provenance(session, file_id, prov)` | Corrects a provenance after ingestion. |
| `case_studies.reload_case_studies()` | Drops the cache after you edit a manifest in a running process. |

`import_case_study` **refuses** rather than silently duplicating when a project of the target name
already exists: two indistinguishable engagements in the picker would double every dashboard figure
drawn across projects. Pass `project_name=` to import a second copy deliberately.

---

## How a manifest becomes an engagement

```
data/case_studies/*.json
        │  _load_manifest()  — validates; every failure names the file and the field
        ▼
   CaseStudy  (dataclass: files, control_refs, expected_outcomes, public_sources, limitations)
        │  build_case_study_files()  — declarative content → bytes, deterministic
        ▼
   GeneratedFile[]  (notice prepended / Data_Origin column prepended)
        │  ingest_file()  — the same path an upload takes: hash, store, parse, chunk, embed
        ▼
   EvidenceFile rows
        │  set_evidence_provenance()  — provenance = HISTORICAL_PUBLIC
        ▼
   AuditProject (status FIELDWORK, is_demo=True, scope_note "CASE-STUDY | case_id=… | provenance=…")
        + scoped controls + ActivityLog(CASE_STUDY_IMPORTED)
```

**Determinism.** Content is declarative — document bodies as line arrays, exports as columns plus
rows — so what will be written is readable without running anything and no random number generator
sits between the declaration and the bytes. The CSV line terminator is pinned to `\n` because the
stored bytes are hashed and a platform-dependent line ending would make the same case study produce
a different SHA-256 on a different machine. Two imports produce byte-identical files.

**Failure policy during import.** A file that produces no chunks is logged loudly but does not roll
the import back: the engagement and its other evidence are real rows and are better kept than
discarded. A file with no chunks is invisible to retrieval, so the case study would silently
under-test its control — which is why the warning is loud.

---

## Adding a new case study

1. **Choose a pattern, not an incident.** It must be documented in public, generic across many
   organisations, and describable without naming anyone. If you cannot describe it without a company
   name, it is not ready to be a case study here.
2. **Invent an organisation** and use it consistently. Invent the vendors, the account handles, the
   ticket references and the dates too. Check that no name you invented is a real company — a quick
   search is enough, and finding out later is expensive.
3. **Decide which controls it exercises.** They must already exist in
   `data/controls/control_library.json`; the import refuses with a clear message if any `control_ref`
   is missing. Prefer several interacting controls over one, because that is the thing the
   evaluation datasets cannot give you.
4. **Write the evidence as data.** Requirement documents (`POLICY`, `STANDARD`) as `body` line
   arrays; populations and operational records (`USER_LISTING`, `LOG_EXTRACT`, `TICKET_EXPORT`, …) as
   `columns` + `rows`. Plant the conditions deliberately and make sure they are *findable*: the
   column names should match the retrieval keywords on the controls you named.
5. **Write `expected_outcomes` honestly, then leave them alone.** They are your reasoned judgement
   with the justification spelled out — including the specific rows and thresholds you are relying
   on. They are documentation of intent, not an answer key.
6. **Record the sources with `url_confirmed` set truthfully.** Retrieve each URL and confirm its
   title before you record it. If you cannot, describe the source generically with an empty `url`
   and `url_confirmed: false`. Never guess a URL.
7. **Write `limitations`.** At minimum: that the expected outcomes are judgement rather than ground
   truth, and how the planted conditions differ from real evidence. If you run the case study and
   the tool disagrees with you, **record the disagreement and its diagnosis there** — as CASE-001
   does — rather than adjusting either side until they match.
8. **Save it** as `data/case_studies/case-00N-short-slug.json`. Files are discovered by glob, so
   nothing needs registering.
9. **Check it loads and renders before importing anything:**

   ```python
   from app.evaluation import case_studies
   case_studies.reload_case_studies()
   case = case_studies.get_case_study("CASE-002")
   for generated in case_studies.build_case_study_files(case):
       print(generated.filename, generated.size_bytes)
       print(generated.content.decode("utf-8")[:400])
   ```

10. **Import into a scratch database first** (set `DATABASE_URL` to a temporary file), assess the
    controls in mode C, and read what the tool says before you decide the case study is finished.

---

## Manifest field reference

Validation is strict and every error names the file and the field. Anything marked **required**
raises `CaseStudyManifestError` if absent or blank.

### Top level

| Field | Type | Notes |
|---|---|---|
| `case_id` | string | **Required.** `CASE-001` style. Leads the default project name so two case studies can never collide. Duplicate ids across files: the first loaded wins, with a warning. |
| `title` | string | **Required.** |
| `provenance` | string | Must be exactly `HISTORICAL_PUBLIC`. A manifest claiming `SYNTHETIC` or `ORGANISATIONAL` — or misspelling it — is rejected rather than defaulted, because the whole point of the mechanism is material reconstructed from public history. |
| `schema_version` | string | Defaults to `"1.0"`. |
| `organisation_label` | string | The fictional organisation. Used in the in-file notices and the descriptions; include "(fictional)" in the label itself. |
| `audit_area` | string | Defaults to `"IT general controls"`. Truncated to 255 characters. |
| `period_start` / `period_end` | string | `YYYY-MM-DD`. Become the engagement's audit period. |
| `incident` | object | Free-form. `summary` is rendered into the project description; `pattern_name`, `documented_sequence` and `why_reconstructed` are conventional and are carried through `to_dict()`. |
| `sourcing_policy` | string | The rule you followed when recording sources. Rendered into the project description. |
| `public_sources` | array | See below. |
| `control_refs` | array of string | **Required, at least one.** Must exist in the control library at import time. |
| `files` | array | **Required, at least one.** Filenames must be unique within the manifest. |
| `expected_outcomes` | array | See below. |
| `limitations` | array of string | Rendered into the project description. |

### `files[]`

| Field | Type | Notes |
|---|---|---|
| `filename` | string | **Required.** Extension must match `format`: `.md`/`.txt` for `text`, `.csv` for `table`. |
| `format` | string | `text` or `table`. Defaults to `text`. Anything else is rejected. |
| `evidence_type` | string | **Required and validated** against `EvidenceType`: `POLICY`, `STANDARD`, `CONFIGURATION_EXPORT`, `SYSTEM_REPORT`, `USER_LISTING`, `TICKET_EXPORT`, `LOG_EXTRACT`, `SCREENSHOT_NARRATIVE`, `INTERVIEW_NOTES`, `OTHER`. An unknown value fails loudly rather than defaulting to `OTHER`, because retrieval and the prompt both use the type to tell a document that *states a requirement* from an export that *records what happened* — a silent default would change what the case study tests. |
| `role` | string | Free-form label (`requirement`, `population`, `operation`, `provenance` in CASE-001). Carried into the import summary; not interpreted. |
| `description` | string | Appended after the disclosure sentence in the stored evidence description. |
| `body` | array of string | **Required for `text`.** One entry per line. |
| `columns` | array of string | **Required for `table`.** Must **not** include `Data_Origin` — it is reserved and added automatically; declaring it is rejected before the row-width check, so you fix the right thing. |
| `rows` | array of array | **Required for `table`.** Every row must have exactly `len(columns)` cells; ragged rows are reported by row number. Cells are rendered as literal text (`None` → `""`, booleans → `true`/`false`), because an audit export is text and letting JSON numbers through would make `2024` and `"2024"` produce different files. |

### `expected_outcomes[]`

| Field | Type | Notes |
|---|---|---|
| `control_ref` | string | **Required.** |
| `expected_status` | string | **Required and validated**: `EFFECTIVE`, `POTENTIAL_DEFICIENCY`, `INSUFFICIENT_EVIDENCE`, `NOT_EFFECTIVE`, `NOT_APPLICABLE`. |
| `expected_risk` | string | `LOW`, `MEDIUM`, `HIGH`, `CRITICAL`, `NOT_RATED`. Unknown values fall back to `NOT_RATED`. |
| `justification` | string | Why. Name the rows, thresholds and dates you relied on — CASE-001's are worth copying as a model. |
| `evidence_pointers` | array of string | Human-readable pointers such as `"CS001_Privileged_Account_Listing.csv row 13: vpn.legacy.maint, Last_Login = 2024-01-16"`. |

### `public_sources[]`

| Field | Type | Notes |
|---|---|---|
| `title` | string | |
| `publisher` | string | |
| `url` | string | Empty for a generically described source. |
| `url_confirmed` | boolean | **Set it truthfully.** `true` only if you retrieved the resource and confirmed its title while writing the manifest. |
| `relevance` | string | What this source supports — and, where relevant, what it does *not*. |

---

## A case study is not an evaluation dataset

This distinction is the reason the module is deliberately **not** wired into
`app/evaluation/runner.py`, and it should be re-argued rather than assumed if anyone ever wires it
in.

| | `app/evaluation/datasets.py` | `data/case_studies/*.json` |
|---|---|---|
| Correct answer is… | **true by construction** (90 of 100 rows have the attribute) | **the case author's argument** |
| Used to compute accuracy, precision, recall, F1, kappa | **Yes** | **No** |
| Provenance of the evidence | `SYNTHETIC` | `HISTORICAL_PUBLIC` |
| Purpose | measurement | demonstration and stress-testing |
| Disagreement with the tool means… | a measurable error | a disagreement to record and diagnose |

`expected_outcomes` is documentation of intent, recorded **so that a reader can disagree with it**.
No accuracy, precision or recall figure reported anywhere in this project is computed over a case
study, and none should be: scoring a model against a conclusion that is itself an argument would
quietly convert an opinion into ground truth, which is exactly the move this project spends its
whole length arguing against.

---

## Known gaps

Recorded honestly; each names the file that must change.

1. **No API endpoint and no console button.** The importer is reachable from Python only. The module
   already ships `case_study_summaries()` and `CaseStudy.to_dict()` ready for a picker, and
   `is_case_study_project()` / `case_study_project_case_id()` so any consumer can identify such an
   engagement from its `scope_note` rather than by pattern-matching its name. What is missing is a
   router endpoint and a page control.
2. **`ingest_file` does not take a provenance argument**, so the importer writes it in a follow-up
   commit (`set_evidence_provenance`). Between the two commits the row carries the column default
   `SYNTHETIC`. That is the safe direction to be wrong in — an artefact under-trusted is a nuisance,
   an artefact over-trusted is a fabricated audit record — but the correct fix is one optional
   parameter on `app/evidence/service.ingest_file`, forwarded through
   `app/frontend/data_access.upload_evidence`.
3. **The REST API cannot carry or report provenance.** `app/api/routers/evidence.py` has no
   `provenance` form field and `EvidenceResponse` in `app/schemas/api.py` has no such attribute.
   Consequence with `USE_API=true`: every file reads as `SYNTHETIC` whatever it is. This is not
   papered over — the Evidence page detects that no record carries the key and shows an explicit
   warning instead of a falsely confident count, and it refuses to write provenance to the database
   in API mode (the database may not be that process's), telling the auditor the file is held at the
   wrong provenance.
4. **Reports do not disclose provenance.** `ReportEvidenceItem` in `app/audit/report.py` has
   `is_synthetic` but no `provenance`, so Section 4 (*Evidence Reviewed*) of a report generated over
   a case-study engagement does not say the evidence is a historical reconstruction. **This is the
   most important remaining gap**: the database, the console and the files themselves all disclose
   provenance, and the report — the artefact that actually leaves the building — currently does not.
   The fix is a `provenance` field on that dataclass, populated at construction and rendered with
   `app.schemas.enums.provenance_label()` beside each evidence row and in the Limitations section.
5. **`reconcile_schema` adds the `provenance` column to an existing database but not its index.**
   Documented in `app/database/migrations.py` and pinned by a test. It costs a scan, never
   correctness.
6. **A mock-provider quirk observed while diagnosing the CASE-001 divergence** (`app/llm/
   mock_provider.py`): the `missing_evidence` list for `CONTROL-002` and `CONTROL-013` both begin
   with a password-policy item that belongs to `CONTROL-005` and is irrelevant to either control. It
   looks like an unconditional entry rather than one derived from the control under test. Low
   severity, but it makes the missing-evidence list less trustworthy than it looks.

---

## Checklist before you commit a new case study

- [ ] No real company, incident, regulator finding or specific advisory is named anywhere in the
      manifest or in any generated file.
- [ ] The fictional organisation and every vendor name were checked against reality.
- [ ] `provenance` is `HISTORICAL_PUBLIC`, and the manifest loads without a warning.
- [ ] Every source's `url_confirmed` is truthful; every URL was actually retrieved and its title
      confirmed; no URL is asserted for incident reporting.
- [ ] `sourcing_policy` states the rule you followed.
- [ ] Every `control_ref` exists in the control library.
- [ ] `build_case_study_files()` runs, and you have **read** the rendered output — the notice is at
      the top of every document and `Data_Origin` leads every CSV record.
- [ ] Two renders produce byte-identical output (no sampling, no timestamps in the content).
- [ ] `expected_outcomes` carry justifications naming specific rows and thresholds.
- [ ] `limitations` says the expected outcomes are judgement, not ground truth — and records any
      disagreement between your judgement and what the tool produced, with its diagnosis.
- [ ] You imported it into a scratch database, assessed it in mode C, and read the result.
- [ ] `.venv/bin/python -m pytest tests -q` still passes.
