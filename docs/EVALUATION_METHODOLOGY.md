# Evaluation Methodology

**Project:** LLM-Assisted IT Audit Risk and Control Assessment System (research prototype)
**Document status:** the measurement protocol. Anything reported in
[`EXAMPLE_RESULTS.md`](EXAMPLE_RESULTS.md) was produced by the procedure defined here.

This document states what is being measured, how, against what ground truth, and — at
length, because it matters more than the numbers — what the measurements cannot
establish. The single most important sentence in it is in §9.1: **with the offline
provider there is no language model in the system at all**, so every figure in the
default configuration describes the scaffolding around a model, not a model.

---

## 1. The research question

> Does surrounding a language model with retrieval over the actual evidence, an explicit
> control requirement, a structured output contract, mechanical citation verification and
> a human-review gate change **how an automated IT control assessment fails** — in
> particular, does it stop the system from stating a confident conclusion about a control
> when the evidence supplied never addressed that control?

Two things about this framing are deliberate.

**It is a question about failure modes, not about accuracy.** An audit tool that is right
90% of the time and confidently wrong the other 10% is not 90% as useful as a perfect one;
it is unusable, because the auditor cannot tell which decile they are reading. The
sub-question that carries the study is therefore narrower and testable: *when the evidence
does not address the control, does the system say so, or does it answer anyway?* That is
what DATASET-005 exists to measure (see [`SYNTHETIC_DATASETS.md`](SYNTHETIC_DATASETS.md)
§6).

**It is a question about the scaffolding, not the model.** The independent variable is the
orchestration around the provider, and the provider is held constant across conditions.
This is a strength of the design (the comparison is clean) and its central limitation
(§9.1): it means the study can say something about pipeline architecture and nothing about
model capability, unless and until the same harness is re-run against a real model.

### 1.1 Operational hypotheses

| # | Hypothesis | Where it is tested | Outcome in the reported run |
|---|---|---|---|
| H1 | The unscaffolded baseline (A) will convert an *absent* attribute into a *finding* about the control. | DATASET-005 | Supported. A answered POTENTIAL_DEFICIENCY. |
| H2 | Retrieval + explicit requirement + output contract (B) will recover the correct abstention. | DATASET-005 | Supported. B answered INSUFFICIENT_EVIDENCE. |
| H3 | The full workflow (C) will be more traceable than B — fewer fabricated citations, more verified quotes. | All six | **Not supported and not testable at the default setting.** B and C produced byte-identical grounding figures, because the offline provider fabricates nothing at `MOCK_HALLUCINATION_RATE=0.0`. See §9.4 and `EXAMPLE_RESULTS.md` §7. |
| H4 | C's safety rails will withdraw conclusions that cannot be grounded. | Fault-injection run | Supported, **and shown to cost accuracy**: C withdrew a *correct* NOT_EFFECTIVE on DATASET-003 because one citation in it was fabricated. See `EXAMPLE_RESULTS.md` §7. |

H4's result is the most interesting finding in the study and the least flattering, which is
why it is stated in the methodology rather than buried: the rails are not free, and the
trade they make (defensibility bought with recall) is a design choice a reader is entitled
to disagree with.

---

## 2. Design

A within-subjects comparison. Three experimental conditions are run over the same six
synthetic datasets, so every condition sees the same evidence and is scored against the
same answer key. `n = 6` per condition, 18 assessments per suite.

```
                        one suite = 3 conditions x 6 datasets
  generate + verify        ┌──────── A: no retrieval, weak prompt, unrailed
  the 6 datasets ONCE ───► ├──────── B: retrieval + requirement + schema, unrailed
  (same bytes for all)     └──────── C: sufficiency → assess → critique → validate → rails
```

Each `(dataset, condition)` pair is run in its **own throwaway audit project**
(`app.evaluation.runner._create_evaluation_project`). This is an experimental control, not
housekeeping: with one shared project, DATASET-001's MFA export would be retrievable while
assessing DATASET-005, and DATASET-005 is the case the study turns on. The datasets are
generated and verified once per suite and the same file paths are handed to all three
conditions (`run_all_experiments`), so parsing, chunking and embedding are identical and
any difference in the results is attributable to the condition.

The suite is executed by:

```python
from app.evaluation.runner import run_all_experiments
runs = run_all_experiments(session)          # modes A, B, C over all six datasets
```

---

## 3. Independent variable: the three conditions

The conditions are defined in `app.schemas.enums.ExperimentMode` and implemented in
`app.audit.engine.AssessmentEngine`. What follows is the exact difference, feature by
feature; nothing else differs.

| Dimension | **A** `A_RAW_LLM` | **B** `B_RAG` | **C** `C_RAG_WORKFLOW` |
|---|---|---|---|
| Retrieval | **None.** Raw file text in upload order, truncated at `raw_evidence_char_budget` (24 000 chars). | Hybrid retrieval (BM25 + cosine, reciprocal-rank fusion, k=60), `top_k=12`. | Same as B. |
| System prompt | `RAW_BASELINE_SYSTEM_PROMPT` — 102 characters: *"You are an IT auditor. You review evidence and assess whether IT controls are working. Answer in JSON."* | `SYSTEM_PROMPT` — 7 526 characters: the four-way REQUIRES/PROVES/INFERS/HUMAN-VERIFIES rule, hard prohibitions on inventing evidence, and a worked example of the missing-attribute case. | Same as B. |
| Control record shown | Reference, name, objective only (`_baseline_control_payload`). Expected evidence and assessment criteria are **withheld**. | Full control: objective, description, risk addressed, expected evidence, all assessment criteria, framework references. | Same as B. |
| Chunk IDs in the prompt | No. Evidence is one undifferentiated blob with no source labels. | Yes: every chunk is rendered as `[chunk_id: N] SOURCE: <locator>`. | Same as B. |
| Output contract | None. No JSON schema is requested. | `assessment_json_schema()` requested as structured output. | Same as B. |
| LLM calls per assessment | 1 | 1 | 3 — sufficiency pre-check → assessment → self-critique |
| Citation validation | Run and **recorded**, never acted on. | Run and **recorded**, never acted on. | Run and **acted on**. |
| Safety rails | **Not applied.** | **Not applied.** | Applied: ungrounded EFFECTIVE/NOT_EFFECTIVE → INSUFFICIENT_EVIDENCE; fabricated citations flagged into `human_verification_required`; no-evidence sentinel; `human_review_required` forced true. |
| Prototype risk scoring | Applied (all modes, so the modes stay comparable). | Applied. | Applied. |

### 3.1 Why A and B are left unrailed

The rails in `app.audit.validators.enforce_safety_rails` downgrade an ungrounded
conclusion. Applying them to A would erase precisely what the experiment exists to observe
— how often an unscaffolded pipeline states a conclusion its evidence does not support —
and would make the comparison circular. So A's and B's conclusions are persisted exactly as
the provider produced them, the validator still runs so the ungroundedness is *measured*,
and `validation_report["engine"]["rails_enforced"]` records that nothing was applied. This
means the A-vs-C difference conflates two things (better inputs **and** post-hoc
correction); the A-vs-B difference isolates the inputs, and the B-vs-C difference isolates
the workflow and the rails. Report all three pairs, not just A-vs-C.

### 3.2 Two design decisions worth challenging

1. **A is validated against the text it was actually shown**, not against an empty set.
   A's prompt carries no chunk IDs, so every citation it makes is untraceable *by
   construction*; scoring it against nothing would mark all of them FABRICATED and report a
   100% hallucination rate that measures the prompt format rather than the answer. The
   engine therefore records the raw chunks that fitted in the budget as "evidence shown", so
   a quotation that genuinely occurs in the supplied files scores PARTIAL (real text,
   untraceable pointer) and only an invented one scores FABRICATED. The consequence is that
   **A's strict grounding rate is 0.000 by construction, not by measurement** — a reader must
   not read it as "A invented everything". See `EXAMPLE_RESULTS.md` §4.1.
2. **A's provider context carries a reduced control record.** Handing the provider the full
   control row through `LLMCallContext` while the prompt withholds it would give the baseline
   through the side door exactly what the prompt is meant to deny it.

---

## 4. Dependent variables

All arithmetic is implemented in `app.evaluation.metrics`, which deliberately does not
delegate to scikit-learn so that an examiner can read the definition that was applied.
(sklearn is used as an oracle in that module's verification, not as the implementation.)

Notation: for a status class *c*, TP = predicted *c* and truly *c*; FP = predicted *c* but
truly something else; FN = truly *c* but predicted something else; TN = neither.

### 4.1 Classification (`classification_metrics`)

| Metric | Formula | Denominator |
|---|---|---|
| `accuracy` | correct / n_scored | rows with a ground-truth status |
| `precision[c]` | TP / (TP + FP) | predictions of *c* |
| `recall[c]` | TP / (TP + FN) | ground-truth instances of *c* (= `support[c]`) |
| `f1[c]` | 2·P·R / (P + R) | — |
| `macro_f1` | unweighted mean of `f1[c]` over observed labels | number of labels |
| `weighted_f1` | support-weighted mean of `f1[c]` | total support |

**Macro is the headline average for this study**, because the rare classes
(INSUFFICIENT_EVIDENCE, EFFECTIVE, NOT_EFFECTIVE — one instance each) are exactly the ones
the system is being tested on, and a support-weighted mean would let the three
POTENTIAL_DEFICIENCY cases dominate.

Two conventions:

* A row with a ground truth but **no prediction** (an engine crash) is scored as the label
  `(no prediction)`, not dropped. Dropping it would score the system only on the runs it
  completed, which flatters it exactly where it failed.
* Per-class rates with a zero denominator are reported as `0.0` (the `zero_division=0`
  convention) so macro averaging stays defined. This is the **only** place in the metrics
  layer where an undefined ratio becomes a number, and the output says so under
  `zero_division_convention`. Everywhere else an undefined rate is `null`, never `0.0`,
  because "0% of assessments cited evidence" and "no assessment could be scored" are
  different claims.

### 4.2 Binary deficiency detection (`deficiency_detection_metrics`)

The same predictions collapsed to the question an audit manager actually asks: *did the
tool raise a deficiency?*

* **POSITIVE** = the system concluded a deficiency exists = status in
  `{POTENTIAL_DEFICIENCY, NOT_EFFECTIVE}` (`app.schemas.enums.DEFICIENCY_STATUSES`).
* **NEGATIVE** = everything else.
* The same rule is applied to the ground-truth status to obtain the true class.

| Metric | Formula | Reading |
|---|---|---|
| `precision` | TP / (TP + FP) | of the deficiencies raised, how many were real |
| `recall` | TP / (TP + FN) | of the real deficiencies, how many were raised |
| `specificity` | TN / (TN + FP) | of the healthy controls, how many were left alone |
| `fpr` | FP / (FP + TN) = 1 − specificity | rate of sending an auditor after a control that was fine |
| `fnr` | FN / (FN + TP) = 1 − recall | rate of missing a real deficiency — **the dangerous error** |
| `balanced_accuracy` | (recall + specificity) / 2 | — |

`f1` is `null` whenever precision or recall is itself undefined: a system that never raised
a deficiency has no precision, and printing its F1 as 0.0 would read as a measured failure
rather than an absent measurement.

### 4.3 The treatment of INSUFFICIENT_EVIDENCE (stated once, applied everywhere)

This is the methodological decision most open to challenge, so both defensible framings are
computed and **both must be reported**.

**Framing 1 — `insufficient_evidence="negative"` (the default, reported as
`deficiency_detection`).** INSUFFICIENT_EVIDENCE counts as a NEGATIVE. When the ground truth
is a real deficiency and the system honestly answers "the evidence does not establish this",
that row is a **false negative**. Recall is therefore depressed by exactly the behaviour the
system is designed to exhibit. The framing is chosen anyway because, from the point of view
of an audit programme, an unraised deficiency is unraised regardless of how politely the
tool declined — and a framing that let abstention off the hook could report high recall for
a system that never concludes anything at all.

**Framing 2 — `insufficient_evidence="excluded"` (reported as
`deficiency_detection_excluding_insufficient`).** Any row whose ground truth *or* prediction
is INSUFFICIENT_EVIDENCE is removed as an abstention; the rest are scored as above, and
`coverage` reports the share of rows that survived. Coverage is not optional garnish — a
system that abstains on five of six cases and is right on the sixth would score perfectly
under this framing, and only `coverage = 0.167` reveals it.

**Neither framing alone is honest.** The first understates the system's discrimination; the
second overstates its usefulness by scoring it only where it chose to answer. In the
reported run this matters concretely: A's deficiency precision is 0.75 under framing 1 and
1.00 under framing 2 at `coverage = 0.667` — the second number is better *because two of A's
six answers were removed from the table*, including the one where it was most dangerously
wrong.

**INSUFFICIENT_EVIDENCE is also a full class in the multi-class table** (§4.1), where
getting it right is rewarded and getting it wrong is penalised symmetrically. That is where
the study's headline claim should be read from; the binary framings are supporting views.

### 4.4 Traceability and hallucination (`evidence_metrics`)

Every verdict below is produced by `app.audit.validators`, which is **mechanical**: it
asks the provider nothing and is reproducible from the stored assessment and the stored
chunks. That is what makes a hallucination rate a measurable property of the pipeline
rather than a claim about it.

**Per-citation verdicts** (`validate_citations`):

| Verdict | Condition |
|---|---|
| `FABRICATED` | the `chunk_id` is not in the retrieved set (scored on the pointer alone — the quote is not even examined), **or** it is and the quote scores below half the threshold |
| `VERIFIED` | quote matched its own chunk at ≥ `citation_match_threshold` (0.60) |
| `PARTIAL` | matched at ≥ half the threshold (0.30); **also** used when the quote is verifiable but no `chunk_id` was supplied — an untraceable citation can never be *verified*, only exonerated |
| `UNVERIFIED` | resolvable `chunk_id`, no quotation. An honest pointer with nothing to check; earns no grounding credit and is not counted as fabrication |

The match score is `max(longest_common_substring_ratio, token_overlap_ratio)`, both
computed after NFKC normalisation, casefolding and punctuation collapse.

**Assessment-level rates** (denominator = number of results, `n`):
`citation_rate` (≥ 1 citation), `fabricated_assessment_rate` (≥ 1 FABRICATED),
`unsupported_claim_rate` (≥ 1 unsupported number), `hallucination_rate` (either kind),
`clean_rate` (cited something, nothing fabricated, no unsupported number).

**Citation-level rates** (denominator = total citations across all results):

* `grounding_rate` = **sum(VERIFIED) / sum(all citations)** — strict; PARTIAL counts as 0.
  **This is the figure to quote.** It depends only on the 0.60 threshold and on no
  weighting choice.
* `fabrication_rate` = sum(FABRICATED) / sum(all citations).

> **Two grounding rates exist in this codebase and they are not the same number.**
> `app.audit.validators.ValidationReport.grounding_rate` — shown per assessment in the UI
> and printed in audit reports — credits a PARTIAL match at 0.5
> (`PARTIAL_CITATION_CREDIT`). The pooled research figure above credits it at 0. The
> per-assessment strict equivalent is exposed as
> `ValidationReport.grounding_rate_strict`. Any table that mixes them is wrong. In the
> reported run this is not cosmetic: mode A's UI grounding rate is 0.50 on every assessment
> and its research grounding rate is 0.000, because all 14 of its citations were PARTIAL.

**Unsupported numeric claims** (`detect_unsupported_claims`): every number in
`assessment` + `finding` + `reasoning` that appears nowhere in the retrieved evidence, its
provenance (filenames, page/sheet/row locators) or the control definition. Identifier-shaped
spans, cross-references, ordinals, list markers and locator references are masked out first;
dates are judged on their year; a percentage is accepted when it is derivable from two real
evidence counts, in either direction (`10 of 100` licenses both `10%` and `90%`).
`control_requirement` is excluded from the scan — letting the model's own restatement license
its own statistics would defeat the check. **This heuristic is a flag for human attention,
never a verdict**; its known false positives and false negatives are enumerated in
[`LIMITATIONS_AND_FUTURE_WORK.md`](LIMITATIONS_AND_FUTURE_WORK.md) §2.6.

**Retrieval / evidence coverage.** Each dataset declares `key_evidence_markers` — exact
strings a correct answer must have seen, which the generator *writes into* the documents, so
a marker is guaranteed to exist in the evidence.

* `retrieval_recall` = sum(markers surfaced) / sum(markers expected), pooled across datasets.
* `retrieval_recall_macro` = mean of the per-dataset ratios.

Matching is a literal substring test after collapsing whitespace runs (needed because PDF
and DOCX extraction re-wraps lines). Case is not folded and nothing is stemmed. **In mode A
this figure is not a retrieval measurement** — A performs no retrieval, so it measures what
share of the necessary evidence survived truncation into the prompt. It is comparable across
modes only as *evidence coverage*.

**Missing-evidence detection.** Restricted to rows whose *ground truth* is
INSUFFICIENT_EVIDENCE: the share where the system both concluded INSUFFICIENT_EVIDENCE **and**
named at least one missing artefact. Naming the gap is part of the requirement — a bare
"insufficient evidence" tells an auditor nothing about what to go and obtain — so a row that
abstains correctly but names nothing does not count. With one such case in the suite, this
"rate" has a denominator of 1 and is a binary observation wearing a percentage sign.

### 4.5 Cost (`timing_metrics`)

`latency_ms` is wall clock around one whole control assessment, taken with
`time.perf_counter`. Retrieval time and provider time are recorded separately and never
merged. Token counts are provider-reported, summed across steps.

> **Against the offline provider these are Python execution times, not inference times.**
> They compare *pipelines* — how much work the orchestration does — and say nothing about
> how long a real model would take. Presenting a mock-derived latency as an inference
> measurement would be a false statement. Token counts are real counts of the text that
> *would* be sent, and are the more transferable of the two figures.

`p95` is `numpy.percentile(values, 95)` with linear interpolation. At n = 6 it is
effectively the maximum; the metrics layer raises this automatically as a caveat.

### 4.6 Human–AI agreement (`agreement_metrics`)

Pairs are (what the AI concluded, what the auditor finally concluded), taken from
`HumanReview` rows. PENDING reviews are excluded by default — opening a review screen is not
a judgement — and `n_pending_excluded` reports how many rows that removed.

* `status_agreement` = pairs where `final_status == ai_status` / comparable pairs.
* `risk_agreement` = the same for risk band, reported **separately** because keeping a
  conclusion and re-rating its severity is a routine and distinct auditor act.
* `status_kappa`, `risk_kappa` = Cohen's κ = (p_o − p_e) / (1 − p_e), where p_e is the
  agreement expected if each rater assigned labels independently at their own observed
  marginal frequencies. Raw agreement alone is inflated whenever one label dominates.
* `modification_rate` = reviews with decision MODIFIED / completed reviews (a *process*
  measure), reported next to `status_change_rate` = 1 − `status_agreement` (an *outcome*
  measure). They are not complements: an auditor can record MODIFIED while keeping the status.

**κ is `None`, not 1.0, in the degenerate case** where both raters used a single identical
label: p_e = 1, the denominator is zero, and chance-corrected agreement is genuinely
undefined. Render it as "undefined". Below `MIN_KAPPA_PAIRS = 10` pairs the metrics layer
attaches a caveat saying the chance term is estimated from the same handful of observations
and the value is not interpretable.

**No agreement study has been conducted.** In the reported run there are zero `HumanReview`
rows, so every figure in this block is `null`. See
[`RESEARCH_NOTES.md`](RESEARCH_NOTES.md) §5 for how to run one, and §9.7 below for why its
absence bounds what the project can claim.

### 4.7 Automatic caveats

`compute_metrics` attaches a `caveats` list to every metrics blob, triggered by conditions in
the data rather than by an author remembering: n below `MIN_SAMPLE_SIZE = 30`; classes below
`MIN_CLASS_SUPPORT = 5`; κ on fewer than `MIN_KAPPA_PAIRS = 10` pairs; p95 from fewer than
`MIN_PERCENTILE_SAMPLE = 20` observations; statuses never observed; execution errors;
undefined rates. These are conventional rules of thumb, not tests — clearing them would not
make a result significant. They are stored in `EvaluationRun.metrics["caveats"]` and **must
be reproduced wherever the numbers are**.

---

## 5. Ground truth

### 5.1 How it is constructed

A dataset (`app.evaluation.datasets.SyntheticDataset`) is a **declaration**: it states the
population, the planted condition, which spreadsheet rows carry the exceptions, the exact
sentences retrieval must surface, and therefore what an auditor should conclude.
`app.evaluation.generator` then writes real `.csv` / `.xlsx` / `.docx` / `.pdf` / `.txt` /
`.json` files *from* that declaration, and `verify_dataset` re-parses the written bytes and
checks the declaration is still true of them.

This direction of dependency is the point. Ground truth asserted only in a docstring is not
ground truth; it has to be re-derivable from the files. `prepare_datasets(verify=True)` runs
that verification before every suite and **raises rather than evaluating** against a dataset
that has drifted — a silently drifted dataset would not fail a run, it would quietly change
what the run measured.

The answer key is fingerprinted: `build_run_config` stores a SHA-256 over the full dataset
manifest, so two runs whose expected statuses, populations or markers differ can be
identified as having measured different things.

### 5.2 Why synthetic data is appropriate here

1. **The correct answer is known and defensible.** Real audit evidence has an answer that is
   itself a professional judgement; measuring an automated system against a contested answer
   measures the disagreement as much as the system.
2. **One variable can be isolated.** DATASET-001, -005 and -006 share a byte-identical policy
   document and differ only in the operational evidence, producing three different correct
   answers from one unchanged requirement. That is the cleanest available demonstration that
   the system is reading the evidence rather than the control text — and it is what makes the
   DATASET-005 result interpretable at all.
3. **It can be published.** Real privileged-account listings and change-ticket exports cannot
   be released with a dissertation. Every file here is fabricated, says so in its own text,
   and carries a `Data_Origin = SYNTHETIC-RESEARCH-DATA` column or an equivalent notice.
4. **It is reproducible offline.** `GENERATOR_SEED = 20240630` is a module constant,
   deliberately *not* read from settings, so evaluation data cannot change because someone
   adjusted an unrelated runtime knob.

### 5.3 What synthetic data costs — external validity

This is the largest threat to the study and is stated without hedging.

* **The planted conditions are unambiguous.** One attribute, one threshold, one clean
  exception rate, one source of truth per question. Real audit evidence is contradictory,
  partial, undated, differently-named across systems, and frequently arrives as a screenshot
  of a screenshot. A system that scores well here has been shown to handle **the easy case**.
* **The evidence is internally consistent.** Nothing in the suite contains two documents that
  disagree, an export whose totals do not reconcile, or a policy superseded by an
  undocumented exception. Reconciliation is a large fraction of real fieldwork and none of it
  is exercised.
* **The exception rates are round and the populations are exactly 100.** Nothing tests
  behaviour at a rate that is arguably immaterial, at a population too small to conclude from,
  or where sampling would be required.
* **The generator and the evaluated system share an author.** The datasets were written
  knowing how the pipeline works. This is a real conflict of interest. Its mitigations are
  partial: the ground truth is declared before generation and mechanically re-derived from the
  bytes; DATASET-003 deliberately retains a false-positive trap (settings whose correct value
  is "Disabled") rather than sanitising it away; DATASET-006 exists specifically to punish a
  deficiency-biased system. None of that is the same as an independent test set, and no claim
  in this project should be read as if it were.
* **`expected_risk` is not externally validated.** It is the band the *prototype* risk model
  assigns given the known status and exception ratio. It is recorded so risk-band agreement
  can be measured at all; it is not evidence that the risk model is correct, and risk-band
  accuracy is largely downstream of status accuracy rather than an independent result.
* **n = 6.** Every proportion here has a confidence interval far wider than the differences it
  is used to compare. No significance test is offered, because none would be honest at this
  size. Figures are **descriptive of these runs**.

---

## 6. Procedure

For each condition, for each dataset, `run_experiment` performs:

1. Generate the dataset's files (once per suite) and verify them against their declaration.
2. Create an isolated audit project with only that dataset's control in scope.
3. Ingest every file through the **production** ingestion path — parse → chunk → embed →
   persist — with the declared `evidence_type` passed through (the retriever copies it onto
   every chunk and both the prompt and the provider use it to tell a document that *states a
   requirement* from an export that *records what happened*).
4. Assess the control in the given mode.
5. Score the answer against the declared ground truth into an `EvaluationResult` row,
   recording predicted status, the model's own pre-rails status, risk band, marker hits,
   citation verdict counts, unsupported-claim count, latency, tokens, and the full retrieval
   and sufficiency/critique detail.
6. Compute the metric set over all rows into `EvaluationRun.metrics`.

`predicted_status` is **the status the system stands behind** — post-rails in C, identical to
the provider's own answer in A and B. The unedited model status is kept in
`detail["status"]["model"]` so the effect of the rails is recoverable from the row rather
than inferred from the mode.

A dataset that raises is recorded as an errored row with an empty prediction and the run
continues; the metrics layer scores the empty prediction as a wrong answer.

Evaluation assessments carry `evaluation_run_id`, which `app.audit.service` filters on, so
they never contaminate operational dashboard figures. (One known gap: with
`project_id=None`, `dashboard_stats` counts `evidence_files`, `evidence_chunks` and
`controls_in_scope` over all rows with no such exclusion, so evaluation projects inflate
those three fields while they exist. `cleanup_evaluation_projects` is the remedy.)

---

## 7. Reproducibility

`build_run_config` stores, on every run, everything needed to re-run it:

| Captured | Why it is captured |
|---|---|
| Provider and model **as actually resolved**, plus what was *requested*, plus `fell_back_to_mock` | The factory silently falls back to the mock when a real provider is unconfigured. A table recording the requested provider in that case would be a false statement about how the numbers were obtained. |
| `prompt_version` + a fingerprint over the concatenated prompt texts + per-prompt SHA-256 and character counts | A prompt change is a change to the instrument. Results from different prompt versions must not be pooled. |
| `mock.seed`, `mock.hallucination_rate` | The two levers that determine the offline provider's behaviour. |
| Retrieval strategy, `top_k`, `candidate_k`, `min_score` | — |
| Embedding provider, model, dimension | — |
| `chunk_size`, `chunk_overlap`, `table_rows_per_chunk`, evidence budgets | Chunking changes what is retrievable. |
| `citation_match_threshold`, `force_human_review`, whether rails applied | The measurement threshold is part of the measurement. |
| Dataset ids, generator seed, **SHA-256 of the dataset manifest** | Pins the answer key as tightly as the fingerprint pins the instrument. |
| `runner_version`, `engine_version`, `app_version`, Python version, database backend | — |

No secret is recorded; the LLM base URL is reduced to a boolean because a self-hosted
endpoint can carry credentials in its userinfo.

**Determinism was verified, not assumed.** The full suite was executed twice, in two
independent temporary databases, and the two runs were compared column by column on
predicted status, risk band, risk score, citation counts, verdict counts, unsupported-claim
counts, marker hits and token counts across all 18 rows. The frames were identical. Only
`latency_ms` varies, as wall-clock measurement must.

**One reproducibility gap, in a file this document's author does not own.**
`build_run_config` records `rules_version` via `getattr(llm, "rules_version", None)`, but
`MockLLMProvider` exposes its rule-set version through `health()` rather than as an
attribute, so every stored config in this run reads `"rules_version": null`. The provider's
rule set is the single largest determinant of an offline result, and it has already been
bumped once (`mock-rules-1.0` → `mock-rules-1.1`, changing per-dataset outcomes). Until this
is fixed, **record the value from `get_llm_provider().health()["rules_version"]` by hand
alongside any result set** — it is `mock-rules-1.1` for everything reported here.

---

## 8. What is *not* varied, and could be

Held constant throughout: retrieval strategy (HYBRID), `top_k` (12), embedding provider
(local hashing vectoriser, 1024 dims), chunk size (1200/180), citation threshold (0.60),
temperature (0.0), the control library, and the provider. Each is a legitimate second
independent variable and each would need its own run; see `RESEARCH_NOTES.md` §3 for the
ones worth the compute.

---

## 9. Threats to validity

### 9.1 The one that governs everything: there is no language model in the default configuration

With `LLM_PROVIDER=mock`, `app.llm.mock_provider.MockLLMProvider` is an ordinary rule-based
program. It reads chunk text, applies fixed auditing heuristics, and emits the JSON shape a
model would be asked to produce. It contains no model and touches no network.

Consequences, all of which must travel with any figure:

* **A/B/C differences measure the scaffolding, not model capability.** They show what
  retrieval, an explicit requirement, an output contract and post-hoc validation do to a
  pipeline whose "reasoning" component is fixed and perfectly consistent. That is a real and
  reportable finding about architecture. It is not a finding about LLMs.
* **The accuracy figures are a property of the mock's rules.** The rule set is versioned
  (`MOCK_RULES_VERSION`) precisely because editing it moves the results; it already has
  moved them once.
* **The offline provider has no reasoning failures to correct.** It never contradicts itself,
  never drifts, never mis-reads a table, and at the default `mock_hallucination_rate = 0.0`
  never fabricates. Mode C's workflow is therefore evaluated in a world with almost nothing
  for it to catch, which is why H3 came out null and why the fault-injection run of §9.4
  exists.
* **Any claim about LLM performance requires re-running this harness against a real
  provider.** `app.llm.openai_provider.OpenAICompatibleProvider` works against any
  OpenAI-compatible endpoint; the harness needs no other change. Until that is done, the
  correct sentence is *"the pipeline reached the planted conclusion in k of 6 cases"*, never
  *"the LLM was k/6 accurate"*.

### 9.2 Construct validity

Accuracy against a designed answer key is not audit quality. The metric rewards emitting the
right status string; it does not check that the *finding text* is right, that the cited rows
are the right rows, that the recommendation is sensible, or that the reasoning would survive
a review-note challenge. A system could score 6/6 while writing findings an auditor would
reject wholesale.

### 9.3 The grounding checks are syntactic, and this is demonstrable

A VERIFIED citation means the quoted characters exist in the chunk pointed at. It does not
mean the quote was read in context or that it supports the conclusion drawn from it. Because
the score takes the *maximum* of two similarity measures, the order-insensitive one sets the
floor: measured on this project's own fixtures, a verbatim quote scores 1.00, a one-word
substitution 1.00, a word-salad rearrangement of the chunk's vocabulary 0.92, and a sentence
reusing the chunk's words to assert the **opposite** of the chunk 0.90 — all VERIFIED against
a 0.60 threshold — while wholly invented text scores 0.36.

The reported run contains a live instance. On DATASET-005, mode A cited two sentences as
*exception records* — "A single knowledge factor is never sufficient to authenticate a
privileged session…" and "An exception that has expired is treated as a control failure…" —
both of which are **requirements from the policy document**, quoted at 100% fidelity. The
validator correctly reports them as real text. It has no way to report that they are the
wrong *kind* of statement. Semantic faithfulness is not tested here and is not claimed
anywhere.

### 9.4 The fault-injection run, and what it does and does not license

Because the provider never fabricates at rate 0.0, the whole hallucination-detection layer
would be untested by the main suite. `MOCK_HALLUCINATION_RATE = 1.0` makes the provider
deliberately inject a fabricated citation or an unsupported figure into every assessment.
This is a **research lever for exercising the validator**, and results from it are labelled
as fault injection, never pooled with the main suite, and never quoted as "the system's
hallucination rate". Injected faults are generated by the same author as the detector, so a
detection rate measured against them is close to circular; what the run legitimately shows is
the *behavioural* difference between B and C when a fault is present, which is a property of
the rails and not of the detector's sensitivity.

### 9.5 The retrieval metric is saturated, and structurally so

`retrieval_recall = 1.000` in every mode and every dataset. The cause is not good ranking:
each evaluation project holds 7–12 chunks in total and `top_k = 12`, so in the reported run
**every chunk in the project was supplied to the model in every dataset** (chunks shown =
total candidates, for all six). The retriever ranked but never excluded anything. The figure
is a ceiling, not a result, and nothing in this suite distinguishes good ranking from
indiscriminate ranking. A corpus large enough to force selection is required before any
retrieval claim can be made.

### 9.6 The risk model is a prototype and its bands are the author's

`app.audit.risk` computes risk deterministically from five weighted 1–5 factors so that a
rating can be argued with. The weights, ladders and band thresholds are defensible choices
calibrated so the *ordering* of outcomes is sensible; they are not derived from or equivalent
to COBIT, ISO 27005, NIST SP 800-30 or FAIR, and every artefact carries
`RISK_MODEL_LABEL` saying so. Two consequences visible in the reported run:

* A 10% exception rate on CONTROL-001 (DATASET-001, POTENTIAL_DEFICIENCY) scores 79.75 and a
  direct policy contradiction (DATASET-003, NOT_EFFECTIVE) scores 82.12 — **both CRITICAL**.
  The top band saturates early on a high-privilege control, leaving little headroom to
  distinguish a minority exception rate from an outright failure. Whether that is correct for
  privileged access is arguable; that it compresses the scale is not.
* The model's *advisory* escalation path (a model-suggested band may raise the computed band
  by one) was **never exercised**: in all 18 assessments the provider suggested a band at or
  below the computed one. That code path is therefore untested by this evaluation.

### 9.7 No human in the loop yet

The system's central safety claim is that it prepares working-paper input for an auditor
rather than issuing opinions. Every assessment is forced to `human_review_required = True`
and the UI carries an "AI-generated — requires auditor review" banner throughout. But **no
qualified auditor has reviewed any output**, no agreement study has been run, and there is no
inter-rater reliability baseline against which κ could be interpreted even if there were.
The project can therefore claim that the *mechanism* for human review exists and is enforced;
it cannot claim anything about whether auditors find the output useful, whether it changes
their time on task, or whether it induces automation bias — which is the risk that most
plausibly outweighs the benefit. See `LIMITATIONS_AND_FUTURE_WORK.md` §3.2.

### 9.8 Internal validity: what is well controlled

For completeness and balance, the things that are *not* threats: the same generated bytes are
used by all three conditions in a suite; each dataset is isolated in its own project; the
provider, embeddings and chunking are identical across conditions; the run is deterministic
and was verified so; the answer key and the prompts are both fingerprinted; and errored rows
are scored as wrong rather than dropped. Any difference between the A, B and C rows of a
results table is attributable to the condition.

---

## 10. Reading order

* [`SYNTHETIC_DATASETS.md`](SYNTHETIC_DATASETS.md) — what each case is and why its ground
  truth is what it is.
* [`EXAMPLE_RESULTS.md`](EXAMPLE_RESULTS.md) — the numbers from an actual run, with a worked
  end-to-end walkthrough of one assessment.
* [`LIMITATIONS_AND_FUTURE_WORK.md`](LIMITATIONS_AND_FUTURE_WORK.md) — the honest bounds and
  what to do next.
* [`RESEARCH_NOTES.md`](RESEARCH_NOTES.md) — how to use the system to produce dissertation
  results.
