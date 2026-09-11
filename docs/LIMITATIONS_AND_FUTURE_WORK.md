# Limitations and Future Work

This document is deliberately the least flattering in the set. Its purpose is to state, in
one place, everything a thesis examiner would otherwise have to find for themselves — and
then to say what would have to be done about each.

The limitations are ordered by how much they bound the project's claims, not by how easy
they are to fix. §1 is the one that governs every number in
[`EXAMPLE_RESULTS.md`](EXAMPLE_RESULTS.md).

---

## 1. The evaluation was run without a language model

### 1.1 What this means

In the default and only fully-exercised configuration, `LLM_PROVIDER=mock` and the
"model" is `app.llm.mock_provider.MockLLMProvider` — an ordinary rule-based program of
roughly 2 600 lines that reads chunk text, applies fixed auditing heuristics and emits the
JSON shape a model would be asked to produce. It contains no model, learns nothing, and
touches no network.

Consequences, all of which bound every reported figure:

* **A/B/C differences measure the scaffolding, not model capability.** They show what
  retrieval, an explicit control requirement, an output contract and post-hoc validation do
  to a pipeline whose reasoning component is fixed and perfectly consistent. That is a real
  finding about architecture; it is not a finding about LLMs.
* **The accuracy figures are properties of the mock's rule set.** The rule set is versioned
  (`MOCK_RULES_VERSION`, currently `mock-rules-1.1`) precisely because editing it moves the
  results, and it has already moved them once during development.
* **Mode C is evaluated in a world with almost nothing to catch.** At
  `mock_hallucination_rate = 0.0` the provider never fabricates, never contradicts itself,
  never mis-reads a table and never drifts. This is why modes B and C scored identically on
  every accuracy and grounding metric — a genuine null result that reproduces, and one that
  tells us more about the provider than about the workflow.
* **The mock is more internally consistent than any real model.** It will never produce the
  fluent, plausible, subtly-wrong output that motivates the whole safety layer. The layer is
  therefore validated against a threat model that does not include its actual adversary.

### 1.2 Honest framing for a write-up

The correct sentence is *"the pipeline reached the planted conclusion in k of 6 cases"*.
The incorrect sentence is *"the LLM was k/6 accurate"*. Any claim of the second kind requires
re-running the harness against a real provider and reporting those numbers separately.

### 1.3 Future work — real model comparison

`app.llm.openai_provider.OpenAICompatibleProvider` already works against any
OpenAI-compatible endpoint (OpenAI, a local Ollama or vLLM server, an Azure-compatible
gateway) and the harness needs **no other change**: set `LLM_PROVIDER=openai`, `LLM_MODEL`,
`LLM_API_KEY` and optionally `LLM_BASE_URL`, then call `run_all_experiments` exactly as
before.

The comparison worth running, in priority order:

1. **One hosted model across A/B/C** on the same six datasets — the single highest-value
   missing experiment in the project. It converts every "the pipeline" sentence into a
   sentence about a model.
2. **Two or three models of different capability** (e.g. a small local model, a mid-tier
   hosted model, a frontier model) through mode C only. The question is not which scores
   highest but **whether the scaffolding's benefit is larger for weaker models** — if the
   workflow rescues a small local model that could run inside an audit firm's own network,
   that is a far more useful finding than a frontier model scoring well.
3. **Repeated runs at temperature 0 and above.** A real model is not deterministic even at
   `temperature=0`. Run each condition 5–10 times and report the *distribution* of outcomes,
   not one run. Variance across runs is itself a result: a tool whose conclusion on a control
   changes between runs is unusable in audit regardless of its mean accuracy, and the
   determinism the mock provides is hiding this entirely.
4. **The mock as an explicit control condition.** Reporting mock-vs-real on identical
   datasets isolates how much of the pipeline's behaviour comes from the scaffolding and how
   much from the model.

---

## 2. Limitations of the measurement

### 2.1 Synthetic data and external validity

The datasets are authored, so accuracy is accuracy **against a designed answer key, not
against an audited reality**. The planted conditions are unambiguous — one attribute, one
threshold, one clean exception rate, one source of truth per question — and nothing in the
suite contains contradictory artefacts, unreconciled totals, out-of-period evidence, or a
policy superseded by undocumented practice. A system that scores well here has been shown to
handle the easy case.

`SYNTHETIC_DATASETS.md` §8 enumerates precisely what the suite does and does not cover.

**The conflict of interest is real and is not fully mitigated.** The datasets and the system
share an author; the datasets were written knowing how the pipeline works. The partial
mitigations (ground truth declared before generation and mechanically re-derived from the
written bytes; the DATASET-003 false-positive trap retained rather than sanitised;
DATASET-006 added specifically to punish a deficiency-biased system) are not equivalent to an
independent test set.

**Future work:** a held-out set authored by someone other than the system's author, ideally a
practising IT auditor, and ideally written *before* seeing the system. Even six such cases
would be worth more than sixty self-authored ones.

### 2.2 n = 6

Four distinct controls; three status classes with a support of one each. Per-class precision
and recall move by whole steps with a single row — mode A's two zero rows in the confusion
matrix are each produced by exactly one misclassification. Every proportion has a confidence
interval far wider than the differences it is used to compare, and no significance testing is
offered because none would be honest.

**Future work:** the generator is declarative, so scaling the suite is a matter of writing
declarations, not code. A useful target is 40–60 cases: 8–12 controls × 4–6 evidence
conditions each (clean, minority exception, contradiction, attribute absent, out of period,
contradictory sources). At n ≈ 50 per-class supports reach the point where
`MIN_CLASS_SUPPORT = 5` stops firing and per-class figures start to mean something. That is
still descriptive, not inferential, but it is the difference between "one row moved" and "a
pattern".

### 2.3 A single, synthetic control library

All 14 controls in `data/controls/control_library.json` are IT general controls in seven
categories (Logical Access, Change Management, Vulnerability Management, Backup and Recovery,
Logging and Monitoring, Third-Party Access, Data Protection), written by the same author, in
one house style, with assessment criteria
deliberately phrased as explicitly testable statements with thresholds. Its own disclaimer
states the framework references are **illustrative only** and have not been reviewed against
or mapped to any published framework.

Two consequences. First, the system has never been tested against control text written by
someone else — vaguer objectives, criteria without thresholds, or the genuinely ambiguous
phrasing common in real control matrices, all of which the query builder and the prompt
depend on. Second, the evaluation covers only **4 of the 14** controls (CONTROL-001, -003,
-004, -005); ten are seeded, retrievable and completely untested.

**Future work:** test against a second control library in a different style, and against
control descriptions taken from a published framework rather than authored for the system.
Measure how much accuracy depends on criteria being pre-phrased as testable statements — if
the answer is "a lot", that is a significant usability finding, because real control matrices
are not written that way.

### 2.4 The retrieval metric is saturated

`retrieval_recall = 1.000` everywhere, because each evaluation project holds 7–12 chunks and
`top_k = 12`: in the reported run **the retriever ranked but never discarded a single chunk**
in any dataset. Nothing distinguishes good ranking from indiscriminate ranking, and no
retrieval claim can be made. In mode A the figure is not a retrieval measurement at all.

**Future work:** build evaluation projects that force selection — distractor documents,
several controls' evidence in one project, multi-hundred-page policy sets, near-duplicate
documents differing in one parameter. Then the existing metric becomes meaningful, and
`retrieval_strategy` (KEYWORD / VECTOR / HYBRID) and `top_k` become worthwhile independent
variables. This is the cheapest high-value improvement available.

### 2.5 Grounding checks are syntactic, and demonstrably so

A VERIFIED citation means the quoted characters exist in the chunk pointed at. It does not
mean the quote was read in context or that it supports the conclusion drawn from it. Because
`grounding_score` takes the *maximum* of a substring measure and an order-insensitive token
measure, the latter sets the floor: on this project's own fixtures a verbatim quote scores
1.00, a one-word substitution 1.00, a word-salad rearrangement 0.92, and a sentence reusing
the chunk's words to assert the **opposite** of the chunk 0.90 — all VERIFIED at a 0.60
threshold — while wholly invented text scores 0.36.

The reported run contains a live instance: mode A on DATASET-005 cited two **policy
requirements** as `supports: exception`, at match score 1.00 each. Nothing in the validation
layer flags that, and nothing could, because in the narrow sense the citations are perfectly
grounded.

**A hallucination rate of 0.000 therefore does not mean the output is trustworthy.** It means
no citation was unresolvable and no quote was absent from the evidence.

**Future work:** an entailment check — does the cited chunk actually *support* the claim
attached to it? This is a natural-language-inference task and could be done with a small
local NLI model (no network, consistent with the offline constraint) or as a separate LLM
call scored against human labels. It would need its own validation set of
(claim, chunk, supports/refutes/neutral) triples, which does not exist. A cheaper partial
measure: check that a citation's declared `supports` role (`requirement` / `exception` /
`context`) is consistent with the *evidence type* of the chunk it points at — that alone
would have caught the DATASET-005 mode A failure above.

### 2.6 The unsupported-claim heuristic has known false positives and false negatives

`detect_unsupported_claims` flags numbers in the narrative that appear nowhere in the
retrieved evidence, its provenance, or the control definition. Its documented failure modes:

* **False positives — the common and annoying error.** A count the model derived *correctly*
  but the evidence never states literally is flagged. If a listing contains five non-compliant
  rows and no total anywhere, "5 accounts failed" is correct arithmetic and is reported as
  unsupported. Arithmetic over evidence is exactly what an auditor wants a tool to do. The
  trade is accepted deliberately: the check is a flag for human attention, never a verdict,
  and under-flagging would defeat its purpose. Percentage derivation from real counts is
  accepted (both `a/b` and its complement from the *same* pair), which recovers the most
  common case, but it requires a denominator of 10 or more and near-exact agreement, so a
  rounded percentage may still be flagged.
* **False negatives.** Any number that happens to occur somewhere in a large evidence set
  passes, whatever it was used to claim. With a hundred-row export in context, small integers
  are effectively unfalsifiable. This was observed directly during development: an invented
  "3 accounts" survived because a cited chunk happened to sit on page 3. **The check is a
  lower bound on unsupported numeric claims, never a clean bill.**
* **Out of scope entirely.** Non-numeric fabrication — an invented policy name, an invented
  approver, a wrong causal claim — is not detected at all.

**No error rate is claimed for either direction**, because the only populations it has been
run against are this project's own synthetic datasets, where the figures were chosen by the
same author who wrote the heuristic.

A further, narrower gap: the mode-C self-critique step performs its own advisory
unsupported-number check that compares bare digit runs without the date and identifier
masking the mechanical validator applies. The mock never writes thousands separators so it
does not currently misfire, but a real model writing `1,250` against evidence writing `1250`
would be flagged by the critique. The critique is advisory only — the mechanical validator is
what the study measures — but it should be aligned before a real-model run.

**Future work:** hand-label a few hundred numeric claims from real-model output as
supported/unsupported and report precision and recall of the heuristic against them. Only
then can a hallucination rate be quoted as a measurement rather than as a count of flags.

### 2.7 The fault-injection results are partly circular

`MOCK_HALLUCINATION_RATE = 1.0` makes the provider inject a fabricated citation or an
unsupported figure into every assessment. The faults are generated by the same author as the
detector, so a *detection rate* measured against them is close to circular. What that run
legitimately shows is the **behavioural** difference between modes B and C when a fault is
present, which is a property of the rails rather than of the detector's sensitivity — and the
100% injection rate is not a realistic operating point, so the recall/defensibility trade at a
realistic rate is unmeasured.

### 2.8 The prototype risk model is arbitrary by construction

`app.audit.risk` computes risk deterministically from five weighted 1–5 factors, with an
explicit floor for INSUFFICIENT_EVIDENCE and a ceiling for EFFECTIVE, so that a rating can be
recomputed and argued with rather than being an unstable number from a model. That
architecture is defensible. **The specific numbers are not derived from anything.** The
weights (30/25/20/15/10), the 1–5 ladders, the band thresholds (25/50/75) and the exception
uplift slope are the author's own choices, calibrated so the *ordering* of outcomes is
sensible. They are not derived from, endorsed by, or equivalent to COBIT, ISO 27005, NIST SP
800-30 or FAIR, and every artefact the module produces carries `RISK_MODEL_LABEL` saying so.

Two specific weaknesses visible in the reported run:

* **The top band saturates.** A 10% exception rate on a high-privilege control (DATASET-001,
  POTENTIAL_DEFICIENCY) scores 79.75 and an outright policy contradiction (DATASET-003,
  NOT_EFFECTIVE) scores 82.12 — **both CRITICAL**. Whether "10% of domain administrators
  without MFA" deserves the top band is arguable; that the scale leaves almost no headroom
  above it is not.
* **The model-escalation path is untested.** A model-suggested band may raise the computed
  band by one, but in all 18 assessments the provider suggested a band at or below the
  computed one, so that branch never executed in this evaluation.

Because `expected_risk` in each dataset is *the band this model assigns given the known
status*, risk-band accuracy is largely downstream of status accuracy and is **not an
independent result**.

**Future work:** elicit factor weights and band thresholds from several practising IT
auditors (a simple pairwise-comparison or Delphi exercise would do) and report the spread —
if experienced auditors disagree materially about the weights, that is itself a finding about
automated risk rating, and a more honest output than a single number. Compare the additive
model against a multiplicative one on the same cases. And separate `expected_risk` from the
model under test so risk agreement becomes a real measurement.

### 2.9 Latency figures are not inference times

Reported latencies are Python execution times against an offline rule engine. They compare
how much work each orchestration does and say nothing about how long a real model would take.
The token counts are the transferable cost figure — they are real counts of text that would be
sent — and they show mode C costing **5.4× mode A** and **2.9× mode B** per assessment.

### 2.10 Construct validity: the metric is not audit quality

Accuracy against the answer key rewards emitting the right status string. It does not check
that the finding text is right, that the cited rows are the right rows, that the
recommendation is sensible, or that the reasoning would survive a review-note challenge. A
system could score 6/6 while writing findings an auditor would reject wholesale. Nothing in
this project measures the *quality* of the working-paper text it produces.

**Future work:** a rubric-scored review of the narrative output by qualified auditors —
finding accuracy, recommendation usefulness, clarity, appropriateness of hedging — scored
blind against a human-written baseline for the same evidence.

---

## 3. Limitations of the system as an audit tool

### 3.1 No auditor has used it

This is the largest gap after §1. The system is built on the premise that an AI assessment is
working-paper *input* requiring auditor review; `force_human_review` is true, every assessment
carries `human_review_required = True`, every screen carries an "AI-generated — requires
auditor review" banner, and the review workflow records decisions, final statuses, timings and
a hallucination flag. **None of it has been exercised by a qualified auditor.** In the reported
run there are zero `HumanReview` rows and every agreement figure is `null`.

The project can therefore claim that the *mechanism* exists and is enforced. It cannot claim
that auditors find the output useful, that it saves time, that it improves conclusions, or —
most importantly — that the human-review gate actually works as a safeguard.

### 3.2 Automation bias is the untested risk that could outweigh the benefit

A system that produces fluent, well-cited, confident-looking assessments may make a reviewer
*less* critical, not more. A four-way panel that separates REQUIRES / PROVES / INFERS /
HUMAN-VERIFIES is designed to resist that, but design intent is not evidence. It is entirely
possible that the safety scaffolding *increases* automation bias by making the output look
more rigorous than it is — the citation panel in particular signals verification, while
`EXAMPLE_RESULTS.md` §4.3 shows a case where every citation is VERIFIED and the reasoning is
nonsense.

**This is the most important untested claim in the project, and it points against the
system.**

**Future work — the auditor user study.** Realistic and worth doing at modest scale
(8–15 participants):

* **Design:** within-subjects, counterbalanced. Each participant assesses the same set of
  controls, half unaided and half with the tool, or compares mode B output against mode C
  output on the same evidence.
* **Measures:**
  * *Time on task* per control, unaided vs assisted. The system already records
    `review_seconds` per review — but see the honest caveat in §4.3 below.
  * *Conclusion agreement* with the tool (`status_agreement`, Cohen's κ) and with other
    auditors (inter-rater reliability among the humans, which is the missing baseline: κ
    between auditor and AI is uninterpretable without knowing κ between two auditors).
  * *Error detection*: seed a known-bad assessment (a fabricated citation, an overstated
    conclusion, the DATASET-005 mode A output) and measure what proportion of reviewers catch
    it. This is the direct automation-bias measure and the single most valuable number the
    study could produce.
  * *Trust and usefulness*, on a short validated scale, before and after.
  * *Which panel they actually read*, from UI instrumentation, versus which they say they read.
* **Note that the evaluation datasets are unsuitable as study material** — they are
  unambiguous by design, so a participant disagreeing with the tool is almost certainly
  wrong. Study material needs genuine ambiguity.

### 3.3 No evidence-authenticity verification

The system verifies that a citation points at evidence it was given and that the quote is in
it. It does **not** verify that the evidence is genuine, complete, unedited, or generated by
the system it claims to come from. An evidence file's SHA-256 is computed and stored at
ingestion, so tampering *after* upload is detectable — but a spreadsheet edited before it was
uploaded is indistinguishable from an authentic export, and that is the realistic threat. The
system correctly pushes this to `human_verification_required` on every assessment ("Confirm
the supplied extract is the complete population for the audit period and was generated
directly from the source system, not edited after export") rather than pretending to test it.

**Future work:** direct system integration (read the account listing from the identity
provider's API rather than from an uploaded CSV) is the only real answer; it removes the
question rather than answering it. Short of that: digital signing of exports at source, or
reconciliation against a second independent source, would both be measurable improvements and
neither is implemented.

### 3.4 No period-coverage testing

Several controls' assessment criteria require the evidence to be dated inside the audit
period, and the generated evidence always is. The system *flags* period coverage for human
verification but never *tests* it: there is no comparison of an export date against
`AuditProject.period_start` / `period_end`, and no dataset plants an out-of-period export, so
that branch is untested and arguably not implemented. This is a genuine audit requirement
(evidence dated after the period end does not evidence the period) and its absence is a real
functional gap, not just an evaluation gap.

**Future work:** add an out-of-period dataset variant (the same clean DATASET-006 evidence
dated three months after period end, whose correct answer is INSUFFICIENT_EVIDENCE for the
period under audit) and a mechanical date check in the engine. This is cheap and would
immediately add a fifth ground-truth class of failure.

### 3.5 No evidence-to-control mapping

The data model has no relationship between an evidence file and a control. `EvidenceFile`
belongs to a project; relevance is decided by retrieval at assessment time. Consequences:
evidence coverage is an *inference* from what an assessment managed to cite, an unassessed
control is reported as "coverage unknown" rather than "no evidence", and there is no way to
ask "which controls does this document support?" without running an assessment.

### 3.6 No multi-control reasoning

Each assessment is one control against one evidence set. Real ITGC conclusions are
interdependent: an unapproved change matters more when segregation of duties has also failed;
a dormant-account finding changes the significance of an access-review finding; a compensating
control in one area can mitigate a deficiency in another. Nothing in this system reasons
across controls, and nothing in the evaluation tests it.

**Future work:** a control-dependency graph in the library (`mitigates`, `depends_on`,
`compensates_for`), an aggregation pass that reasons over the assessed set, and datasets whose
correct answer for one control depends on the outcome of another. This is a substantial piece
of work and probably the most interesting direction the project could take.

### 3.7 No sampling methodology

Every dataset presents the whole population. Real fieldwork frequently requires a sample —
sized, selected and defended — and the system has no notion of sampling, sample size, sampling
risk, or projecting an observed error rate onto a population with a confidence interval. When
the mock reports an exception ratio it is a whole-population proportion, not an estimate.

**Future work:** attribute-sampling support (population size, tolerable deviation rate,
expected deviation rate → sample size), selection from an ingested population with a
reproducible seed, and projection of observed exceptions with a stated confidence level. This
is well-defined, standard, and would make the tool materially more usable — and would give the
risk model a principled `exception_ratio` uncertainty rather than a point estimate.

### 3.8 Single-user, single-process, SQLite

The default database is SQLite; the Streamlit UI and FastAPI API share one file. There is
**no authentication, authorisation or rate limiting anywhere** — the API is open to anyone who
can reach the port, which is stated in the OpenAPI description, in `/health` and in
`/api/v1/settings`, but is genuinely true. There is no user model, so `reviewer_name` is a
free-text string, `actor` on an activity log is unverified, and an "audit trail" that anyone
can write any name into is not an audit trail in the professional sense. Concurrent review of
the same assessment is untested: the service appends a new `HumanReview` row rather than
updating, and what that does to the pending queue and the agreement figures has not been
checked.

**Future work:** PostgreSQL (`app.database.base` is written for it — no SQLite-only types, no
native enums, an explicit naming convention for Alembic, so it is a `DATABASE_URL` change plus
migrations), real authentication with roles (preparer / reviewer / read-only), signed review
records, and a concurrency test for two reviewers on one assessment.

### 3.9 No integration with the systems auditors actually use

Evidence arrives by file upload and results leave as Markdown or HTML. Real engagements live
in GRC platforms (AuditBoard, Workiva, ServiceNow IRM, TeamMate) and evidence lives in
ticketing systems, identity providers and cloud configuration APIs. Every integration point is
a manual file copy.

**Future work:** an export in a GRC-importable shape; read connectors for the highest-value
evidence sources (identity provider account listings, change tickets, cloud configuration
snapshots), which would also address §3.3 by removing the upload step; and a webhook so an
assessment can be triggered when new evidence lands.

---

## 4. Limitations of the software itself

These are smaller, but a thesis artefact should be honest about the state of its own code.

### 4.1 Test coverage is by behaviour, not by line

The suite under `tests/` covers the mandated behaviours and four core behaviours are
mutation-proven, but **no line-coverage figure was measured** (coverage.py is not in the
dependency set). There will be branches in ~19 000 lines that nothing exercises — the risk
model's escalation path (§2.8) is one confirmed example. `app/frontend/` has no automated tests
at all: ten page modules and a ~90-function data-access facade are unverified by the suite.

### 4.2 Known defects not fixed at the time of writing

* `app.audit.service.dashboard_stats(project_id=None)` counts `evidence_files`,
  `evidence_chunks` and `controls_in_scope` over all rows with no evaluation exclusion, so
  evaluation projects inflate exactly those three fields while they exist. Status counts,
  findings, risk, latency and review figures are correctly excluded via
  `Assessment.evaluation_run_id`. Remedy: `cleanup_evaluation_projects`, or a dedicated
  `AuditProject.is_evaluation` column (the harness currently overloads `is_demo`, which is the
  only "not a real engagement" flag the service layer exposes, and which also causes evaluation
  projects to appear in the UI project selector and to print the demo-data disclaimer in
  reports).
* `build_run_config` records the provider's rule-set version via `getattr(llm,
  "rules_version", None)`; the mock exposes it through `health()` instead, so every stored run
  config reads `"rules_version": null`. Since the rule set is the single largest determinant
  of an offline result, **record it by hand** until this is fixed.
* Per-chunk retrieval scores are not persisted. `Assessment` stores `retrieved_chunk_ids` and
  `retrieval_queries` but no keyword/vector/fusion scores, so the UI can show rank order and
  which chunks were cited, but not why a chunk ranked where it did. Any future work on
  retrieval quality (§2.4) needs this added in `app.audit.engine` first.
* One known false "missing evidence": in DATASET-003 the supplied screenshot narrative is
  classified by the provider as a *requirement* document because it reads as prose rather than
  as a key/value export, so an expected artefact that *was* supplied is still listed as missing.
  It errs toward asking the auditor for something they already hold, which is the safer
  direction, but it is a defect.

### 4.3 `review_seconds` is wall clock, not task time

The review timer runs from when an item was last opened, includes time away from the screen,
and restarts if the item is reopened or the browser reloads. The UI makes it visible and
lets a reviewer decline to record it. Any write-up quoting `review_seconds` must describe it
as **an upper bound on a per-visit measurement**, note that some reviews will carry no
duration at all, and not present it as time on task. A user study (§3.2) should measure timing
externally rather than relying on it.

### 4.4 Prototype-scale API and UI

List endpoints for projects, controls, evidence, reviews and reports are unbounded (no
pagination). Assessment and evaluation runs are synchronous, so a whole-suite run against a
hosted model would exceed a default 60-second client timeout. Uploads are buffered in memory
up to `max_upload_bytes`. Report bodies are returned inline in JSON. None of this matters at
six datasets and a handful of controls; all of it matters at a few thousand rows.

---

## 5. Future work, ranked by research value per unit of effort

| # | Work | Effort | Why it matters |
|---|---|---|---|
| 1 | **Run A/B/C against one real model** | Low — config only | Converts every "the pipeline" claim into a claim about a model. The single largest gap. |
| 2 | **Repeated runs to measure variance** | Low | A tool whose conclusion changes between runs is unusable in audit. Currently invisible behind the mock's determinism. |
| 3 | **Enlarge the dataset suite to ~50 cases** | Medium — declarative | Makes per-class figures mean something; adds out-of-period, contradictory-source and ambiguous cases. |
| 4 | **A corpus large enough to force retrieval selection** | Medium | Unsaturates the retrieval metric and makes `retrieval_strategy` / `top_k` real variables. |
| 5 | **Auditor user study with an error-detection task** | Medium–high | The only way to test the human-review gate, and the only way to measure automation bias. |
| 6 | **Adversarial / misleading evidence datasets** | Medium | Where an audit tool is most needed and entirely untested: favourable-but-irrelevant evidence, plausible-but-wrong totals, superseded policies. |
| 7 | **Inter-rater reliability baseline among human auditors** | Medium | Without it, human–AI κ is uninterpretable. |
| 8 | **Entailment / `supports`-role checking** | High | Closes the gap between syntactic grounding and semantic faithfulness — the deepest technical limitation. |
| 9 | **Sampling methodology** | Medium | Standard, well-defined, and makes the tool usable on populations too large to test in full. |
| 10 | **Multi-control dependency reasoning** | High | The most interesting extension; requires a dependency graph, an aggregation pass and new datasets. |
| 11 | **Elicit risk weights from practising auditors** | Low–medium | Replaces the arbitrariness of §2.8 with a measured spread, and the spread is itself a finding. |
| 12 | **PostgreSQL, authentication, multi-user workflow** | Medium | Engineering rather than research, but a prerequisite for any field deployment or realistic user study. |
| 13 | **GRC integration and evidence connectors** | High | Addresses evidence authenticity (§3.3) by removing the manual upload step. |

---

## 6. The one-paragraph honest summary

This project builds a working, fully offline pipeline that ingests real evidence files,
retrieves over them, produces a structured control assessment with verbatim citations,
mechanically re-checks every citation and every number against the evidence, withdraws
conclusions it cannot ground, computes an explainable risk rating, and forces human review on
everything. On six authored synthetic datasets it demonstrates that adding retrieval, an
explicit control requirement and citation scaffolding changes two of six conclusions in the
predicted direction — including, crucially, turning a confident false finding into a correct
refusal to conclude on evidence that never addressed the control — and moves strict citation
grounding from 0.000 to 1.000. It also shows, against its own interest, that the full
workflow's safety rails cost one correct conclusion in six when faults are injected, which is
a trade of recall for defensibility rather than an improvement. **It does not show anything
about language models, because there was no language model in the evaluated configuration; it
does not show anything about retrieval quality, because the retriever never had to discard a
chunk; and it does not show anything about whether auditors would use, trust, or be misled by
the result, because no auditor has seen it.** Those three sentences bound everything the
project can currently claim, and each of them names a specific, tractable experiment that
would remove the bound.
