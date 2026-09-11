# Example Results

Every number in this document was produced by executing
`app.evaluation.runner.run_all_experiments` against a throwaway SQLite database on the date
below. Nothing is illustrative, estimated or carried over from an earlier build. Where a
result is disappointing it is reported as it came out, and §7 and §8 discuss why.

---

## 0. Run provenance — read this before any table

| | |
|---|---|
| **Date of run** | 2026-09-10 |
| **Provider** | `mock` — `MockLLMProvider`, model id `mock-deterministic-rules`, rule set **`mock-rules-1.1`** |
| **Provider requested / resolved** | `mock` / `mock`; `fell_back_to_mock = false` |
| **`mock_seed`** | 1337 |
| **`mock_hallucination_rate`** | **0.0** for §§1–6 and 9; **1.0** for the fault-injection run in §7 |
| **`n` per condition** | **6** (six datasets); 18 assessments per suite |
| **Datasets** | DATASET-001 … DATASET-006; manifest SHA-256 `324af842dd6ae834…` |
| **Generator seed** | 20240630 |
| **Prompt version / fingerprint** | `1.0.0` / `bdba31700292781d` |
| **Engine / runner / app version** | 1.0.0 / 1.0.0 / 0.1.0 |
| **Retrieval** | HYBRID (BM25 + cosine, RRF k=60), `top_k=12`, `candidate_k=60`, `min_score=0.0` |
| **Embeddings** | local hashing vectoriser, 1024 dims (no network) |
| **Chunking** | `chunk_size=1200`, `chunk_overlap=180`, `table_rows_per_chunk=25` |
| **Citation match threshold** | 0.60 |
| **`force_human_review`** | true |
| **Python / DB** | 3.9.6 / SQLite |

> ### The sentence that governs every table below
>
> **`MockLLMProvider` contains no language model.** It is a deterministic rule-based program
> that reads chunk text, applies fixed auditing heuristics and emits the JSON shape a model
> would be asked to produce. Therefore: **these results measure the pipeline — retrieval,
> prompt scaffolding, output contract, citation validation, safety rails — and say nothing
> whatsoever about the capability of any language model.** The correct sentence for a
> write-up is *"the pipeline reached the planted conclusion in k of 6 cases"*, never *"the
> LLM was k/6 accurate"*. To say anything about a model, re-run this harness against
> `app.llm.openai_provider` and report those numbers separately.
>
> The rule set version matters and moves the numbers: it has already been bumped once
> (`mock-rules-1.0` → `mock-rules-1.1`), changing per-dataset outcomes. Any figure recorded
> against a different rule set is stale and must not be pooled with these.

### 0.1 Automatic caveats attached to every run

`compute_metrics` emitted the same four caveats for all three conditions, and they travel
with the numbers:

1. *n = 6 is below 30. Every proportion here has a confidence interval wider than the
   differences it would be used to compare; report these as descriptive figures for these
   runs, not as estimates of general performance.*
2. *Classes with fewer than 5 ground-truth cases: EFFECTIVE (support 1), POTENTIAL_DEFICIENCY
   (support 3), INSUFFICIENT_EVIDENCE (support 1), NOT_EFFECTIVE (support 1). Per-class
   precision and recall for these move by large steps with a single row.*
3. *Statuses absent from both ground truth and predictions, so untested here: NOT_APPLICABLE.*
4. *Latency p95 is computed from 6 observations; below 20 it is effectively the maximum, not
   a percentile.*

**No significance testing is offered.** None would be honest at this sample size.

---

## 1. The A / B / C comparison

*n = 6 per condition. Provider: mock (`mock-rules-1.1`), hallucination rate 0.0.*

| Metric | **A** raw LLM | **B** RAG | **C** RAG + workflow |
|---|---:|---:|---:|
| Status accuracy | **0.667** (4/6) | **1.000** (6/6) | **1.000** (6/6) |
| Macro F1 | 0.464 | 1.000 | 1.000 |
| Weighted F1 | 0.595 | 1.000 | 1.000 |
| Macro precision | 0.438 | 1.000 | 1.000 |
| Macro recall | 0.500 | 1.000 | 1.000 |
| Deficiency precision | 0.750 | 1.000 | 1.000 |
| Deficiency recall | 0.750 | 1.000 | 1.000 |
| Deficiency FPR | 0.500 | 0.000 | 0.000 |
| Deficiency FNR | 0.250 | 0.000 | 0.000 |
| Balanced accuracy (binary) | 0.625 | 1.000 | 1.000 |
| Risk band agreement | 4/6 | 6/6 | 6/6 |
| Citation rate | 1.000 | 1.000 | 1.000 |
| Citations emitted (total) | 14 | 18 | 18 |
| **Grounding rate (strict, verified/total)** | **0.000** | **1.000** | **1.000** |
| Fabrication rate | 0.000 | 0.000 | 0.000 |
| Unsupported-claim rate | 0.000 | 0.000 | 0.000 |
| Hallucination rate | 0.000 | 0.000 | 0.000 |
| Clean rate | 1.000 | 1.000 | 1.000 |
| Mean citations / assessment | 2.33 | 3.00 | 3.00 |
| Retrieval / evidence recall | 1.000 (26/26) | 1.000 (26/26) | 1.000 (26/26) |
| Missing-evidence detection rate | **0.000** (0/1) | **1.000** (1/1) | **1.000** (1/1) |
| LLM calls / assessment | 1 | 1 | 3 |
| Latency mean / median / p95 (ms) | 83.7 / 73.5 / 131.5 | 81.0 / 84.0 / 95.8 | 132.0 / 131.5 / 167.5 |
| Mean total tokens / assessment | 5 465 | 10 284 | **29 591** |

### 1.1 How to read this table honestly

* **A → B is the study's real signal.** Two of six statuses change, both in the direction the
  design predicted, and the traceability figures change categorically rather than by degree.
* **B = C on every accuracy and grounding figure.** This is a **null result** and it
  reproduces. It is not a wiring bug — the two conditions demonstrably execute different
  pipelines (1 provider call vs 3; ~10.3k vs ~29.6k tokens; `rails_enforced` false vs true) —
  it is a property of a deterministic provider that fabricates nothing at rate 0.0. C's
  machinery had nothing to catch. §7 forces it to have something.
* **1.000 accuracy is not a claim that this pipeline is accurate.** It is 6 correct answers
  out of 6 designed questions, against a rule-based provider, on unambiguous evidence, with an
  answer key written by the same author. The honest reading is *"nothing in this suite caught
  B or C out"*, and the suite is small and easy.
* **Grounding rate 0.000 for A does not mean A invented everything.** See §5.1.
* **Retrieval recall 1.000 is a ceiling artefact.** See §6.

---

## 2. Per-dataset predictions vs ground truth

| Dataset | Control | Expected | **A** | **B** | **C** |
|---|---|---|---|---|---|
| DATASET-001 | CONTROL-001 | POTENTIAL_DEFICIENCY | POTENTIAL_DEFICIENCY ✓ | POTENTIAL_DEFICIENCY ✓ | POTENTIAL_DEFICIENCY ✓ |
| DATASET-002 | CONTROL-003 | POTENTIAL_DEFICIENCY | POTENTIAL_DEFICIENCY ✓ | POTENTIAL_DEFICIENCY ✓ | POTENTIAL_DEFICIENCY ✓ |
| DATASET-003 | CONTROL-005 | NOT_EFFECTIVE | **INSUFFICIENT_EVIDENCE ✗** | NOT_EFFECTIVE ✓ | NOT_EFFECTIVE ✓ |
| DATASET-004 | CONTROL-004 | POTENTIAL_DEFICIENCY | POTENTIAL_DEFICIENCY ✓ | POTENTIAL_DEFICIENCY ✓ | POTENTIAL_DEFICIENCY ✓ |
| DATASET-005 | CONTROL-001 | INSUFFICIENT_EVIDENCE | **POTENTIAL_DEFICIENCY ✗** | INSUFFICIENT_EVIDENCE ✓ | INSUFFICIENT_EVIDENCE ✓ |
| DATASET-006 | CONTROL-001 | EFFECTIVE | EFFECTIVE ✓ | EFFECTIVE ✓ | EFFECTIVE ✓ |

**DATASET-005 is the only case on which the three conditions disagree in status.** Remove it
and A scores 4/5 alongside B and C, and the study has no result. That is a statement about
how thin the evidence base is, not about how strong the effect is.

A's two errors point in **opposite directions**, which is worth more than the headline
accuracy figure:

* **DATASET-005 — over-call.** A converted an absent attribute into a finding
  (POTENTIAL_DEFICIENCY, risk CRITICAL 76.62). This is the failure mode the project exists to
  demonstrate, reproduced.
* **DATASET-003 — under-call.** A abstained on a case where the evidence is a plain
  contradiction. Its own words: *"The population summary reports values for the attribute
  under test that could not be read as either compliant or exceptional, so the exception count
  is unknown."* Without retrieval it receives one undifferentiated blob with no evidence-type
  labels and no table summary, so it cannot tell a policy statement from a configuration
  export and cannot perform the 8 < 14 comparison.

Both errors have the same root cause — no separation between *what is required* and *what
happened* — and it surfaces as over-confidence in one case and paralysis in the other. A
single accuracy number hides that entirely.

---

## 3. Confusion matrices

Rows = expected (ground truth); columns = predicted. `NOT_APPLICABLE` never appears in either
vector and is omitted.

### Mode A — `A_RAW_LLM`

| expected \ predicted | EFFECTIVE | POTENTIAL_DEFICIENCY | INSUFFICIENT_EVIDENCE | NOT_EFFECTIVE |
|---|---:|---:|---:|---:|
| EFFECTIVE | **1** | 0 | 0 | 0 |
| POTENTIAL_DEFICIENCY | 0 | **3** | 0 | 0 |
| INSUFFICIENT_EVIDENCE | 0 | 1 | **0** | 0 |
| NOT_EFFECTIVE | 0 | 0 | 1 | **0** |

Per class:

| Status | Precision | Recall | F1 | Support |
|---|---:|---:|---:|---:|
| EFFECTIVE | 1.000 | 1.000 | 1.000 | 1 |
| POTENTIAL_DEFICIENCY | 0.750 | 1.000 | 0.857 | 3 |
| INSUFFICIENT_EVIDENCE | 0.000 | 0.000 | 0.000 | 1 |
| NOT_EFFECTIVE | 0.000 | 0.000 | 0.000 | 1 |

Note how the two zero rows are produced by **one misclassification each**. This is what
`MIN_CLASS_SUPPORT` caveat #2 is warning about: at support 1, per-class recall is a coin
being reported to three decimal places.

### Modes B and C — identical

| expected \ predicted | EFFECTIVE | POTENTIAL_DEFICIENCY | INSUFFICIENT_EVIDENCE | NOT_EFFECTIVE |
|---|---:|---:|---:|---:|
| EFFECTIVE | **1** | 0 | 0 | 0 |
| POTENTIAL_DEFICIENCY | 0 | **3** | 0 | 0 |
| INSUFFICIENT_EVIDENCE | 0 | 0 | **1** | 0 |
| NOT_EFFECTIVE | 0 | 0 | 0 | **1** |

Precision = recall = F1 = 1.000 for all four classes.

### 3.1 The binary deficiency framing, both ways round

| | A | B | C |
|---|---:|---:|---:|
| **Framing 1 — INSUFFICIENT_EVIDENCE = negative** (`n=6`) | | | |
| TP / FP / TN / FN | 3 / 1 / 1 / 1 | 4 / 0 / 2 / 0 | 4 / 0 / 2 / 0 |
| Precision / Recall | 0.750 / 0.750 | 1.000 / 1.000 | 1.000 / 1.000 |
| Specificity / FPR / FNR | 0.500 / 0.500 / 0.250 | 1.000 / 0.000 / 0.000 | 1.000 / 0.000 / 0.000 |
| F1 / accuracy / balanced accuracy | 0.750 / 0.667 / 0.625 | 1.000 / 1.000 / 1.000 | 1.000 / 1.000 / 1.000 |
| **Framing 2 — abstentions excluded** | | | |
| n kept / considered (coverage) | 4 / 6 (**0.667**) | 5 / 6 (0.833) | 5 / 6 (0.833) |
| TP / FP / TN / FN | 3 / 0 / 1 / 0 | 4 / 0 / 1 / 0 | 4 / 0 / 1 / 0 |
| Precision / Recall / F1 | 1.000 / 1.000 / **1.000** | 1.000 / 1.000 / 1.000 | 1.000 / 1.000 / 1.000 |

**Mode A's binary F1 rises from 0.750 to 1.000 under framing 2 — because the framing deletes
the two rows where it was wrong.** Its coverage of 0.667 is what exposes that. This is the
concrete reason both framings must always be reported together, and why `coverage` is not
optional garnish. Quoting only framing 2 would let a baseline that mishandles the
missing-evidence case be presented as flawless.

---

## 4. Traceability and hallucination

*Denominators are named because the rates are meaningless without them.*

| | A | B | C |
|---|---:|---:|---:|
| Assessments with ≥ 1 citation | 6 / 6 | 6 / 6 | 6 / 6 |
| Total citations emitted | 14 | 18 | 18 |
| VERIFIED | **0** | 18 | 18 |
| PARTIAL | **14** | 0 | 0 |
| UNVERIFIED | 0 | 0 | 0 |
| FABRICATED | 0 | 0 | 0 |
| Strict grounding (VERIFIED / total) | **0.000** | **1.000** | **1.000** |
| UI grounding (PARTIAL at 0.5) | 0.500 | 1.000 | 1.000 |
| Fabrication rate | 0.000 | 0.000 | 0.000 |
| Unsupported numeric claims (total) | 0 | 0 | 0 |
| Assessments flagged as hallucinating | 0 / 6 | 0 / 6 | 0 / 6 |
| Named missing evidence (any) | **0 / 6** | 6 / 6 | 6 / 6 |
| Missing-evidence detection (ground-truth cases) | 0 / 1 | 1 / 1 | 1 / 1 |

### 4.1 Mode A's grounding rate of 0.000 is structural, not a fabrication finding

All 14 of A's citations are PARTIAL for exactly one reason: **A's prompt carries no chunk
IDs, so A cannot supply one.** `validate_citations` treats a quote with no `chunk_id` as
PARTIAL when the text is found in the evidence and FABRICATED only when it is not. Zero of
A's citations were fabricated: every quote it produced genuinely occurs in the files it was
shown. What A cannot do is make any of them *traceable*.

This is a finding about citation scaffolding, and it is the cleanest one in the study: adding
chunk IDs to the prompt moved strict grounding from 0.000 to 1.000 with no change to the
provider. But the sentence must be written carefully. **"Mode A has a grounding rate of zero"
is true and highly misleading if left unqualified.** The correct statement is: *mode A
produced no verifiable citations because the baseline prompt provides nothing to verify
against; none of its citations were invented.*

### 4.2 The two grounding rates in this codebase are not the same number

`ValidationReport.grounding_rate` (shown in the UI, printed in audit reports) credits a
PARTIAL at 0.5. The pooled research figure credits it at 0. On mode A these give **0.500 and
0.000** on the same 14 citations. Quote `grounding_rate_strict` in a write-up and say which
one a table is using. On B and C they coincide at 1.000 because there are no PARTIALs.

### 4.3 The mechanical check cannot see meaning — a live example

On DATASET-005, mode A cited two passages as **exception records**:

> *"A single knowledge factor is never sufficient to authenticate a privileged session,
> whether the session originates inside or outside the corporate network."*
> `relevance: An individual record that does not meet the control requirement.`
> `supports: exception` — match score **1.00**

> *"An exception that has expired is treated as a control failure until the account is
> enrolled or the exception is formally renewed."*
> `supports: exception` — match score **1.00**

Both are **requirements from the MFA policy document**, quoted with perfect fidelity. The
validator's job is to confirm that quoted characters exist in the evidence, and it does that
correctly. It has no way to report that these are the wrong *kind* of statement, and no
grounding metric in this project would flag them. This is the concrete meaning of "the
grounding checks are syntactic" (`EVALUATION_METHODOLOGY.md` §9.3), and it is why a
hallucination rate of 0.000 must never be read as "the output is trustworthy".

---

## 5. Cost

| | A | B | C |
|---|---:|---:|---:|
| Provider calls / assessment | 1 | 1 | 3 (sufficiency, assessment, critique) |
| Latency mean (ms) | 83.7 | 81.0 | 132.0 |
| Latency median (ms) | 73.5 | 84.0 | 131.5 |
| Latency min–max (ms) | 53–147 | 50–96 | 82–170 |
| Prompt tokens, mean | 4 615 | 8 716 | 27 628 |
| Completion tokens, mean | 850 | 1 568 | 1 963 |
| **Total tokens, mean** | **5 465** | **10 284** | **29 591** |
| Total tokens, whole suite | 32 790 | 61 704 | 177 548 |

Per-dataset latency (ms): A = 147, 53, 70, 85, 77, 70 · B = 89, 77, 50, 79, 96, 95 ·
C = 140, 123, 82, 117, 160, 170.

Mean time split by phase (ms):

| Phase | A | B | C |
|---|---:|---:|---:|
| Evidence gathering (retrieval in B/C; reading stored chunks in A) | 1.3 | 17.2 | 16.2 |
| Provider calls, summed | 41.8 | 24.8 | 68.3 |
| Total assessment wall clock | 83.7 | 81.0 | 132.0 |

> **These are Python execution times against an offline rule engine, not inference times.**
> They compare how much work each orchestration does and must never be presented as a
> measurement of how long a model takes. A ≈ B on total wall clock even though A performs no
> retrieval, because A's saving there (1.3 ms vs 17.2 ms) is offset by a slower single
> provider call (41.8 ms vs 24.8 ms) — the baseline path hands the provider one large
> undifferentiated text blob instead of ranked chunks. Both components are recorded separately
> and are never merged, so a workflow's inference cost is not inflated by its retrieval cost.

**The token figures are the transferable cost result.** They are real counts of the text the
pipeline would send to a provider, and they are the number that would appear on an invoice.
**C costs 5.4× mode A and 2.9× mode B in tokens for, in this run, zero accuracy gain over B.**
Whether C's rails are worth that is a judgement a reader should be allowed to make with the
number in front of them, and §7 supplies the other half of the trade.

---

## 6. Retrieval recall is saturated and must be reported as a ceiling

`retrieval_recall = 1.000` (26 of 26 markers) in all three conditions. This is **not** a
ranking result.

| Dataset | Chunks in project | Chunks supplied to the model | Retriever excluded |
|---|---:|---:|---:|
| DATASET-001 | 10 | 10 | 0 |
| DATASET-002 | 7 | 7 | 0 |
| DATASET-003 | 9 | 9 | 0 |
| DATASET-004 | 10 | 10 | 0 |
| DATASET-005 | 10 | 10 | 0 |
| DATASET-006 | 12 | 12 | 0 |

With `top_k = 12` and a corpus of 7–12 chunks, **the retriever ranked but never discarded
anything.** Nothing in this suite distinguishes good ranking from indiscriminate ranking, and
no retrieval claim can be made from it. In mode A the same figure is not a retrieval
measurement at all — A does no retrieval, so 26/26 means the whole evidence set fitted inside
the 24 000-character raw budget.

Fixing this needs a materially larger synthetic corpus (distractor documents, several
controls' evidence in one project), not a change to the metric.

---

## 7. The discriminating experiment: fault injection

Because the offline provider fabricates nothing at rate 0.0, the entire hallucination-handling
layer would go untested and B and C would be indistinguishable. Re-running the suite with
**`MOCK_HALLUCINATION_RATE = 1.0`** forces a fabricated citation or an unsupported figure into
every assessment.

*n = 6 per condition. Provider: mock, hallucination rate **1.0**. Reported separately and
never pooled with §1.*

| Metric | A | B | **C** |
|---|---:|---:|---:|
| Status accuracy | 0.667 | **1.000** | **0.833** |
| Macro F1 | 0.464 | 1.000 | 0.667 |
| Deficiency precision / recall | 0.750 / 0.750 | 1.000 / 1.000 | **1.000 / 0.750** |
| Deficiency FPR / FNR | 0.500 / 0.250 | 0.000 / 0.000 | **0.000 / 0.250** |
| Strict grounding rate | 0.000 | 0.895 (17/19) | 0.895 (17/19) |
| Fabrication rate | 0.000 | 0.105 (2/19) | 0.105 (2/19) |
| Unsupported-claim rate | 0.333 | 0.667 | 0.667 |
| Hallucination rate | 0.333 | **1.000** | **1.000** |
| Mean total tokens | 5 467 | 10 306 | 29 670 |

### 7.1 What separates B from C

B and C have **identical** grounding, fabrication and hallucination figures — the validator
runs in both. The difference is entirely in what is done about it:

| Dataset | Model's own status | B stands behind | C stands behind | C's rails |
|---|---|---|---|---|
| DATASET-001 | POTENTIAL_DEFICIENCY | same | same | unsupported figure flagged |
| DATASET-002 | POTENTIAL_DEFICIENCY | same | same | unsupported figure flagged |
| **DATASET-003** | **NOT_EFFECTIVE** | **NOT_EFFECTIVE ✓** | **INSUFFICIENT_EVIDENCE ✗** | **critique downgrade applied** |
| DATASET-004 | POTENTIAL_DEFICIENCY | same | same | fabricated citation flagged |
| DATASET-005 | INSUFFICIENT_EVIDENCE | same | same | unsupported figure flagged |
| DATASET-006 | EFFECTIVE | same | same | unsupported figure flagged |

C's rail on DATASET-003, verbatim from the stored record:

> `critique_downgrade: the self-critique recommended INSUFFICIENT_EVIDENCE instead of
> NOT_EFFECTIVE and the citation validator independently agreed the conclusion was
> unsupported (1 citation(s) were fabricated). The downgrade was applied.`
> `fabricated_citations: 1 citation(s) could not be resolved to the retrieved evidence
> (position 2). They are retained and flagged, not removed.`

### 7.2 The finding, stated against the project's own interest

**Mode C's safety rails withdrew a conclusion that was correct.** Accuracy fell from 1.000
(B) to 0.833 (C); deficiency FNR rose from 0.000 to 0.250. The conclusion was right; one of
the two citations supporting it was fabricated; the workflow refused to stand behind it.

Two readings are available and a dissertation should present both:

* **Against C:** the workflow introduced a false negative that the simpler pipeline did not
  have. In audit terms it suppressed a real control failure. On this suite, the rails made
  the system *less* accurate.
* **For C:** the withdrawn conclusion rested partly on a citation that does not exist. B
  reported the same fabricated citation in its validation record and asserted the conclusion
  anyway. C is the only condition whose *output* — not just its metadata — reflects that its
  own evidence was defective, and it named exactly which citation and why. An auditor reading
  C's output is told to go and re-test; an auditor reading B's is told a control has failed,
  on evidence one item of which is invented.

Both readings are true simultaneously. **The rails trade recall for defensibility, and this
run quantifies the trade at one false negative in six on a suite engineered to trigger them
on every assessment.** That is the most defensible claim the project can make about mode C
and it is a claim about a design trade-off, not a claim of superiority. It is also
demonstrated at an injected fault rate of 100%, which is not a realistic operating point;
the trade at a realistic rate is unmeasured.

### 7.3 What this run does not license

The faults are generated by the same author as the detector, so a detection rate measured
against them is close to circular. What is legitimate is the *behavioural* comparison between
B and C when a fault is present. Nothing here measures whether the validator would catch
faults produced by a real model, whose fabrications would not be drawn from the same
generator.

Note also that mode A shows a **lower** hallucination rate (0.333) than B and C (1.000) under
injection. This is not evidence that A hallucinates less. A takes a different provider path
(no chunk IDs, no source labels), the injected fabrications are shaped differently there, and
A's citations cannot be resolved to chunk IDs at all so a fabricated pointer has nothing to
fail against. A cross-mode hallucination-rate comparison under injection is not meaningful and
should not be made.

---

## 8. Worked walkthrough: DATASET-005 under mode C, end to end

The discriminating case, traced from bytes on disk to the persisted assessment. Assessment
id 17, project 18, run 3.

### 8.1 Evidence ingested

| File | Type | SHA-256 (prefix) | Size | Parse | Chunks |
|---|---|---|---|---|---|
| `Multi_Factor_Authentication_Policy_v3.docx` | POLICY | `0c5ad952c4abdbc0` | 37 789 B | PARSED | 5 × `DOCX_PARAGRAPH` |
| `Privileged_User_Listing_2024-06-30.csv` | USER_LISTING | `1ba5c800b35dd97e` | 9 269 B | PARSED | 1 × `TABLE_SUMMARY` + 4 × `TABLE_ROWS`, 100 data rows |

### 8.2 Retrieval

Strategy HYBRID; 10 queries built from the control (objective, keywords, each assessment
criterion, expected-evidence terms, risk statement); 10 candidates; **all 10 chunks supplied**
— ids `[157, 156, 155, 154, 153, 158, 162, 159, 161, 160]`.

All 5 key evidence markers surfaced (5/5): `Account_Status`, `Last_Login`, `p.adeyemi` (from
the listing) and both policy sentences (from chunks 155 and 157).

### 8.3 Step 1 — sufficiency pre-check (LLM call 1 of 3; 32 ms, 8 878 prompt / 327 completion tokens)

```
evidence_sufficiency : INSUFFICIENT
can_conclude         : false
present_evidence     : Multi_Factor_Authentication_Policy_v3.docx (requirement)
                       Privileged_User_Listing_2024-06-30.csv (observation)
                       Expected artefact identified: Authentication or MFA policy stating
                       which classes of account are required to use a second factor.
rationale            : "The retrieved evidence does not record multi-factor authentication
                        status for the population in scope. It can establish what is
                        required, but not what happened, so no conclusion on the control's
                        operation can be drawn from it."
```

The pre-check does not replace the assessment. Its finding is prepended to the assessment
prompt, so the assessment is written knowing a separate step already judged the attribute
absent.

### 8.4 Step 2 — assessment (LLM call 2; 31 ms, 9 265 / 1 592 tokens)

### 8.5 Step 3 — self-critique (LLM call 3; 30 ms, 10 168 / 66 tokens)

```
overstated_conclusion : false
unsupported_claims    : []
fabricated_citations  : []
recommended_status    : null
notes                 : "All 2 citation(s) resolve to supplied chunks and quote them
                         verbatim, and every figure in the narrative occurs in the evidence."
```

### 8.6 The four-way separation, as persisted

**① REQUIRES — from the control library, not from the model.** The requirement is a fact
about the audit programme; letting the model's restatement stand in for it would let the
reference move with the output it is meant to be checked against.

> **CONTROL-001 Multi-Factor Authentication for Privileged Accounts.** *Objective:* ensure
> every account holding administrative or otherwise privileged access can only authenticate
> with a second factor.
> *Criterion 2 (the operative one):* "The privileged account listing contains an MFA enrolment
> status column (for example MFA_Status) and an export date inside the audit period; **a
> listing with no enrolment status column cannot evidence this control and the conclusion must
> be INSUFFICIENT_EVIDENCE rather than EFFECTIVE.**"

**② PROVES — cited evidence with its verification verdict, never the model's prose.**

| # | Chunk | Locator | Quote | Verdict | Score |
|---|---|---|---|---|---|
| 1 | 157 | `Multi_Factor_Authentication_Policy_v3.docx` — section *"4. Evidence of operation"* — paragraph 14 | *"The privileged account listing produced for audit must report the multi-factor authentication enrolment status of every account in the population."* | **VERIFIED** | 1.00 |
| 2 | 158 | `Privileged_User_Listing_2024-06-30.csv` — *Population summary* — rows 2–101 — columns: User, Role, Department, Last_Login, Account_Status, Data_Origin | *"TABLE SUMMARY … Population: 100 data row(s) × 6 column(s). Header on spreadsheet row 1; data on spreadsheet rows 2-101…"* | **VERIFIED** | 1.00 |

Citation 1 is the **requirement**; citation 2 is the **operational evidence**, cited
precisely for what it *does not* contain. Both resolve to retrieved chunk ids and both quote
their chunk verbatim. `grounding_rate = grounding_rate_strict = 1.000`, `fabricated = 0`,
`unsupported_claims = []`.

Chunk 157's stored text, for verification by eye:

```
4. Evidence of operation

The privileged account listing produced for audit must report the multi-factor
authentication enrolment status of every account in the population.

The listing must state the date on which it was extracted from the identity provider,
and that date must fall inside the audit period. …
```

**③ INFERS — the model's conclusion, not established by the quotes above.**

```
status               : INSUFFICIENT_EVIDENCE
confidence           : MEDIUM (0.66)
evidence_sufficiency : INSUFFICIENT
assessment : "The operational evidence supplied does not record multi-factor
              authentication status. The export reports the fields: User, Role,
              Department, Last_Login, Account_Status, Data_Origin. It covers 100
              records, but none of its fields state whether the control operated
              for them."
finding    : "The control could neither be confirmed nor challenged: multi-factor
              authentication status is absent from the evidence provided."
inference  : "The absence of multi-factor authentication status from this export is
              not evidence that the control failed; it is evidence that the export
              does not report it. No conclusion about the control's operation is
              drawn either way."
```

That inference is the sentence the whole design is built to produce. Note also that every
integer in the narrative (100, 6) occurs literally in the cited table summary — the provider
puts derived figures such as percentages in `inferences`, never in the narrative, which is
what keeps the honest path clean under `detect_unsupported_claims`.

**④ HUMAN VERIFIES — what the system says it cannot settle.**

Missing evidence named (5):

1. A per-account MFA status or enrolment export covering the in-scope account population
2. Privileged account listing from the identity provider showing MFA enrolment status
3. Configuration export or screenshot narrative of the conditional-access / MFA enforcement rule
4. Exception register listing approved MFA exemptions with approver, compensating control and expiry
5. Authentication log extract evidencing second-factor challenges for a sample of privileged logons

Human verification required (4): request the enrolment export and re-perform; confirm with the
system owner which system of record holds MFA status; confirm the extract is the complete
population and was generated directly from the source system; confirm the extract date falls
inside the audit period.

### 8.7 Risk — computed, not taken from the model

```
Prototype research risk scoring model - not an official industry framework.
Assessment status: INSUFFICIENT_EVIDENCE
  control failure severity : 3.0/5  [weight 30%]  15.0 points
  likelihood               : 3.0/5  [weight 25%]  12.5 points
  impact                   : 4.2/5  [weight 20%]  16.0 points
  privilege level          : 5.0/5  [weight 15%]  15.0 points
  data sensitivity         : 4.0/5  [weight 10%]   7.5 points
Weighted total: 66.0 / 100  →  band HIGH (LOW 0-24.9, MEDIUM 25-49.9, HIGH 50-74.9, CRITICAL 75-100)
Model-suggested band for comparison: HIGH.
```

"We cannot tell whether this control operates" is itself a risk position, so
INSUFFICIENT_EVIDENCE carries a MEDIUM floor and a mid-range severity/likelihood rather than
being rated LOW for lack of a finding.

### 8.8 Rails and the gate

```
rails_applied : ["human_review_enforced: every assessment produced by this system
                 requires auditor review before it can be relied upon."]
status_downgraded : false        human_review_required : true
```

Nothing needed correcting; the unconditional human-review rail still fired, as it does on
every assessment in every mode.

### 8.9 The same evidence under mode A, for contrast

| | Mode A | Mode C |
|---|---|---|
| Status | **POTENTIAL_DEFICIENCY** | INSUFFICIENT_EVIDENCE |
| Risk | **CRITICAL, 76.62** | HIGH, 66.00 |
| Finding | *"2 record(s) do not meet the requirement for multi-factor authentication status."* | *"The control could neither be confirmed nor challenged: multi-factor authentication status is absent from the evidence provided."* |
| Missing evidence named | **none** | 5 items |
| Citations | 2, both PARTIAL, both **policy sentences labelled `supports: exception`** | 2, both VERIFIED, correctly labelled `requirement` and `context` |
| Inference recorded | *"the exceptions represent approximately 2.0% of the summarised population"* | *"The absence … is not evidence that the control failed"* |

Mode A produced a **CRITICAL-rated finding against a control on evidence that never mentioned
that control's attribute**, quantified it to one decimal place, cited two policy requirements
as if they were exception records, and named nothing for the auditor to obtain. Every
individual quote it used is real. This single comparison is the clearest statement of the
problem the project addresses, and it is also the clearest statement of the limits of
mechanical grounding checks: nothing in the validation layer flagged A's output as
hallucinated, because in the narrow sense it was not.

---

## 9. Human–AI agreement: not measured

| Figure | Value |
|---|---|
| `n_reviews`, `n_completed`, `n_status_pairs` | 0 |
| `status_agreement`, `risk_agreement` | `null` |
| `status_kappa`, `risk_kappa` | `null` |
| `modification_rate`, `acceptance_rate` | `null` |

**No qualified auditor has reviewed any output of this system.** The review mechanism exists,
is enforced (`human_review_required = true` on all 18 assessments), records timings and
decisions, and computes agreement and Cohen's κ when data exists — but there is no data, and
every figure is correctly reported as undefined rather than as zero.

Two consequences a write-up must respect: the project cannot claim anything about auditor
trust, usefulness, time on task or automation bias; and κ would in any case be uninterpretable
below `MIN_KAPPA_PAIRS = 10` pairs, and undefined outright if an auditor accepted every
conclusion (both raters using a single identical label gives p_e = 1 and a zero denominator).
See `RESEARCH_NOTES.md` §5 for the study design that would fix this.

---

## 10. Reproducibility of these figures

The full suite was executed **twice**, in two independent temporary databases, and the runs
compared column by column across all 18 rows on: predicted status, risk band, risk score,
citation count, verified count, fabricated count, unsupported-claim count, marker hits, prompt
tokens and completion tokens. **The two frames were identical.** Only `latency_ms` differs, as
wall-clock measurement must.

To reproduce:

```bash
cd "/Users/tingo/Documents/IT AUDIT TOOL"
.venv/bin/python - <<'PY'
from app.database.base import SessionLocal, init_db
from app.database.seed import bootstrap
from app.evaluation.runner import run_all_experiments, compare_runs, prediction_matrix
init_db(); s = SessionLocal(); bootstrap(s)
runs = run_all_experiments(s, run_name="reproduction")
ids = [r.id for r in runs]
print(compare_runs(s, ids).T.to_string())
print(prediction_matrix(s, ids).to_string())
PY
```

Point `DATABASE_URL`, `UPLOAD_DIR`, `REPORT_DIR` and `EVALUATION_OUTPUT_DIR` at a temporary
directory first if the operational database should be left alone (evaluation projects
otherwise persist and inflate three dashboard counters until
`cleanup_evaluation_projects` is called).

**Record `get_llm_provider().health()["rules_version"]` by hand** alongside any result set:
`build_run_config` reads `rules_version` as an attribute, the mock exposes it only through
`health()`, so the stored config reads `null`. It is `mock-rules-1.1` for everything here.

---

## 11. Summary of what these results support and do not support

**Supported by this run:**

* Adding retrieval, an explicit control requirement and a structured output contract changed
  two of six conclusions, in the predicted direction, including the missing-evidence case the
  study is built around.
* Adding chunk IDs and citation scaffolding moved strict citation grounding from 0.000 to
  1.000 with no change to the provider.
* Only the scaffolded conditions named what evidence was missing (6/6 vs 0/6).
* The full workflow's safety rails demonstrably act on a detected fabrication where the
  single-call condition merely records it — **and cost one correct conclusion in six for
  doing so.**
* The pipeline is deterministic and reproducible under a fixed seed.

**Not supported by this run, and not to be claimed:**

* Anything about language-model capability. There is no language model in it.
* That mode C is more accurate than mode B. It is not, here; it is equal at rate 0.0 and
  *worse* under fault injection.
* That the system detects hallucination. It detects *unresolvable citations and quotes absent
  from the evidence*, which is a narrower thing, and §4.3 shows a case it cannot see.
* Anything about retrieval quality. The retriever never discarded a chunk.
* Anything about auditor usefulness, trust, time on task or automation bias.
* Any statistical claim whatsoever. n = 6.
