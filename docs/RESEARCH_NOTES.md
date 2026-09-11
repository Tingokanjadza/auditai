# Research Notes — using this system to produce dissertation results

A practical guide: which experiments to run, what to vary, how to get the data out, what to
report, how to run the human–AI agreement study, and — the section to read first — what the
evidence will and will not support.

Companion documents: [`EVALUATION_METHODOLOGY.md`](EVALUATION_METHODOLOGY.md) (definitions),
[`SYNTHETIC_DATASETS.md`](SYNTHETIC_DATASETS.md) (the cases),
[`EXAMPLE_RESULTS.md`](EXAMPLE_RESULTS.md) (a real run),
[`LIMITATIONS_AND_FUTURE_WORK.md`](LIMITATIONS_AND_FUTURE_WORK.md) (the bounds).

---

## 0. Before anything else: what you are allowed to claim

Write these three sentences on a sticky note and check every draft paragraph against them.

1. **With `LLM_PROVIDER=mock` there is no language model in the system.** Every figure
   describes the pipeline — retrieval, prompt scaffolding, output contract, validation, rails
   — and nothing about model capability. Say *"the pipeline reached the planted conclusion in
   k of 6 cases"*, never *"the LLM was k/6 accurate"*.
2. **n = 6.** Every proportion is descriptive of that run. No significance test is honest at
   this size, so do not run one, and do not write "significantly".
3. **The answer key was authored by the same person as the system.** Accuracy here is accuracy
   against a designed answer, not against an audited reality.

Every metrics blob this system produces carries a `caveats` list generated from the data
itself. **Reproduce those caveats wherever you reproduce the numbers.** They are in
`EvaluationRun.metrics["caveats"]` and they are the difference between a defensible results
chapter and an overclaiming one.

---

## 1. Setup, and one CLI trap

```bash
cd "/Users/tingo/Documents/IT AUDIT TOOL"
.venv/bin/python run.py init          # creates the DB and seeds the 14-control library
.venv/bin/python -m pytest tests -q   # confirm the build is healthy before measuring anything
```

> ### Two defects in `run.py evaluate` — verified, and they will mislead you
>
> **(a) It prints zeros.** After a successful run it prints
> `accuracy=0.000 macro_f1=0.000` for all three modes, every time. The cause is that it reads
> `metrics["accuracy"]` while the metrics blob stores accuracy at
> `metrics["classification"]["accuracy"]`. **The persisted numbers are correct; only the
> printout is wrong.** In the verification run, the CLI printed 0.000 / 0.000 / 0.000 while
> the database held 0.667 / 1.000 / 1.000.
>
> **(b) On a fresh database it fails silently-ish.** `run.py evaluate` calls `init_db()` but
> not the control-library seed, so on a database that has never had `run.py init` run against
> it, **every dataset errors with "Control 'CONTROL-001' not found"**, all three runs are
> recorded `FAILED` with accuracy 0.000, and the command then crashes with a
> `DetachedInstanceError` after the work is done. Always run `run.py init` first.
>
> Both are in `run.py`, which is outside this document's scope to change. Use the script in
> §2 instead, or read the numbers back from the database as §4 shows — never from the CLI's
> summary line.

---

## 2. Experiment 1 — the core A/B/C comparison (run this first)

This is the study. Everything else is an extension of it.

```python
# save as scripts/experiment_abc.py and run with .venv/bin/python
from app.database.base import SessionLocal, init_db
from app.database.seed import bootstrap
from app.evaluation.runner import (
    run_all_experiments, compare_runs, prediction_matrix, results_frame, run_results,
)
from app.evaluation.metrics import confusion_matrix_frame, per_class_frame
from app.llm.factory import get_llm_provider

init_db()
session = SessionLocal()
bootstrap(session)                       # idempotent; safe to call every time

print("provider health:", get_llm_provider().health())   # RECORD rules_version FROM HERE

runs = run_all_experiments(session, run_name="ABC baseline")
ids = [r.id for r in runs]

compare_runs(session, ids).T.to_csv("results/abc_comparison.csv")
prediction_matrix(session, ids).to_csv("results/abc_predictions.csv", index=False)
results_frame(session, ids).to_csv("results/abc_rows.csv", index=False)

for run in runs:
    rows = run_results(session, run.id)
    confusion_matrix_frame(rows).to_csv("results/confusion_%s.csv" % run.experiment_mode)
    per_class_frame(rows).to_csv("results/perclass_%s.csv" % run.experiment_mode)
    print(run.experiment_mode, run.metrics["classification"]["accuracy"],
          run.metrics["classification"]["macro_f1"])
    for caveat in run.metrics["caveats"]:
        print("  caveat:", caveat)
```

**Point the run at a temporary database** (`DATABASE_URL`, `UPLOAD_DIR`, `REPORT_DIR`,
`EVALUATION_OUTPUT_DIR`) if you want the operational database left alone. Evaluation projects
otherwise persist — which is deliberate, because deleting them discards the prompt snapshots
and raw responses that make a result auditable — but they inflate three dashboard counters
until `cleanup_evaluation_projects(session)` is called, and they appear in the UI project
selector.

**Record, in your lab notebook, for every run:** the run ids, the `config` blob (it holds
provider, model, prompt fingerprint, dataset manifest hash, retrieval and chunking settings —
everything needed to re-run), and **`get_llm_provider().health()["rules_version"]`**, which the
config records as `null` because of a small bug (`build_run_config` reads it as an attribute;
the mock exposes it only through `health()`). The rule set is the single largest determinant of
an offline result.

---

## 3. What to vary, in priority order

Each of these is a second independent variable. Run one at a time, change nothing else, and
label every result set with its configuration.

### 3.1 Provider — the highest-value experiment available

```bash
LLM_PROVIDER=openai LLM_MODEL=<model> LLM_API_KEY=<key> .venv/bin/python scripts/experiment_abc.py
# or against a local server, no API key needed:
LLM_PROVIDER=openai LLM_MODEL=llama3.1 LLM_BASE_URL=http://localhost:11434/v1 ...
```

Nothing else changes: the harness, datasets and metrics are provider-agnostic. This is what
converts every "the pipeline" sentence in your thesis into a sentence about a model.

Check `run.config["llm"]["fell_back_to_mock"]` on every run. The factory falls back to the
mock when a real provider is unconfigured, and a results table that silently recorded mock
numbers under a model name would be a false statement.

**With a real provider, run each condition 5–10 times.** A model is not deterministic even at
`temperature=0`. Report the *distribution* — how many of N runs got each dataset right — not a
single run. Run-to-run instability on the same evidence is itself a headline result: a tool
whose conclusion on a control changes between runs is unusable in audit regardless of its mean
accuracy, and the mock's determinism hides this completely.

### 3.2 Model capability

Three models spanning a capability range (small local / mid-tier hosted / frontier), mode C
only. The interesting question is **not** which scores highest. It is whether the scaffolding's
benefit is *larger for weaker models* — if the workflow rescues a model small enough to run
inside an audit firm's own network, that is a far more useful finding than a frontier model
scoring well, because data residency is the binding constraint in real audit work.

### 3.3 The fault-injection lever

```bash
MOCK_HALLUCINATION_RATE=1.0 .venv/bin/python scripts/experiment_abc.py
```

The offline provider fabricates nothing at 0.0, so B and C are indistinguishable without this.
At 1.0 it injects a fabricated citation or an unsupported figure into every assessment, and the
difference between "records the problem" (B) and "acts on it" (C) becomes visible. Sweep
0.0 / 0.25 / 0.5 / 1.0 to show how the rails' behaviour scales.

**Label these results as fault injection, never pool them with the main suite, and never quote
the resulting hallucination rate as "the system's hallucination rate".** The faults come from
the same author as the detector; what the run legitimately shows is the *behavioural*
difference between B and C, not the detector's sensitivity.

### 3.4 Retrieval configuration

```bash
RETRIEVAL_STRATEGY=KEYWORD  # then VECTOR, then HYBRID
RETRIEVAL_TOP_K=4           # then 8, 12, 20
```

**Do not report this against the current datasets.** With 7–12 chunks per project and
`top_k=12`, the retriever currently supplies *every* chunk in every dataset — it ranks but
never discards. Any retrieval comparison would be measuring nothing. Enlarge the corpus first
(§6.2), then this becomes a real variable.

### 3.5 Citation threshold

`CITATION_MATCH_THRESHOLD` (default 0.60) is the measurement instrument for grounding. Sweep
0.4 / 0.5 / 0.6 / 0.7 / 0.8 and report grounding and fabrication rates as a function of it — a
sensitivity analysis showing the headline figure is not an artefact of one arbitrary constant
is cheap to produce and hard to argue with.

### 3.6 Prompt version

Any change to `app/audit/prompts.py` **must** bump `PROMPT_VERSION`. Results from different
prompt versions are different instruments and must not be pooled. Ablating one element of the
system prompt at a time (remove the four-way rule; remove the worked missing-attribute example;
remove the "never invent evidence" prohibition) and measuring the effect on DATASET-005 is a
genuinely interesting mini-study, and it is where the few-shot anchor's contribution could be
isolated.

---

## 4. Getting the data out

Everything is in the database and everything comes out as a DataFrame.

| Want | Call | Shape |
|---|---|---|
| A/B/C comparison table | `runner.compare_runs(session, ids)` | one row per run, 23 columns; `.T` for a thesis table |
| Per-dataset predicted vs expected | `runner.prediction_matrix(session, ids)` | one row per dataset, one column per run |
| Flat per-result export | `runner.results_frame(session, ids)` | one row per (run, dataset), 23 columns |
| Confusion matrix | `metrics.confusion_matrix_frame(rows)` | expected × predicted |
| Per-class P/R/F1/support | `metrics.per_class_frame(rows)` | one row per status |
| Everything for one run | `run.metrics` | nested dict, JSON-serialisable, includes `caveats` |
| Full provenance for one run | `run.config` | nested dict |

Beyond the frames, `EvaluationResult.detail` holds per-row material that most write-ups will
want at least once: the model's own pre-rails status, the rails applied, the risk factor
decomposition, per-marker retrieval hits with the chunk ids that matched, the sufficiency
pre-check, the self-critique, retrieval queries and timings. `Assessment.raw_response` and
`Assessment.prompt_snapshot` hold the verbatim exchange — that is the material for a
qualitative appendix.

Rendered **audit reports** (10 mandated sections, AI assessment separated from final auditor
assessment, evidence appendix, explicit limitations) come from
`app.audit.report.generate_report(session, project_id, generated_by, fmt="markdown"|"html")`;
the returned `AuditReport` carries `content` and `stored_path`. One rendered report belongs in
an appendix — it is the artefact an auditor would actually receive.

**Handle `NaN` correctly on the way out.** `compare_runs` returns `NaN` for an undefined rate
(a zero denominator), and the metrics layer returns `None` for the same thing. Render both as
"n/a" or "undefined", **never as 0**. "0% of assessments cited evidence" and "no assessment
could be scored for citations" are different claims, and printing the second as the first is a
false statement.

---

## 5. The human–AI agreement study

Nothing in this area has been measured (`EXAMPLE_RESULTS.md` §9: zero reviews, every figure
`null`). The mechanism exists and works; the data does not. This is the most valuable study
still available to you.

### 5.1 Get the data in

Through the UI: **Human Review** page — open a pending assessment, record a decision
(`ACCEPTED` / `MODIFIED` / `REJECTED` / `MORE_EVIDENCE_REQUESTED`), a final status, a final
risk band, comments, and optionally a usefulness rating and a hallucination flag with a note.

Programmatically:

```python
from app.audit.service import record_human_review, list_reviews
record_human_review(
    session, assessment_id, reviewer_name="Auditor 3",
    decision="MODIFIED", final_status="POTENTIAL_DEFICIENCY", final_risk_level="HIGH",
    final_finding="...", comments="...", review_seconds=184.0,
    usefulness_rating=4, flagged_hallucination=False,
)
```

### 5.2 Compute the statistics

```python
from app.evaluation.metrics import agreement_metrics
stats = agreement_metrics(list_reviews(session, include_pending=False))
```

Report: `n_completed`, `n_status_pairs`, `status_agreement`, `status_kappa`, `risk_agreement`,
`risk_kappa`, `modification_rate`, `acceptance_rate`, and the `decision_counts` breakdown.
Report status and risk agreement **separately** — an auditor routinely keeps a conclusion and
re-rates its severity, and a single blended number hides exactly that.

### 5.3 Four things to get right, or the statistic is worthless

1. **κ can legitimately be `None`.** If both raters used a single identical label, expected
   chance agreement is 1, the correction divides by zero, and κ is undefined. Report it as
   undefined; do not print 1.0 (which would claim perfect chance-corrected agreement from data
   containing no information about disagreement) and do not print 0.0.
2. **Below 10 pairs κ is not interpretable** and the metrics layer says so in a caveat. Aim for
   at least 30 review pairs. With 3 reviewers × 10 assessments you have 30.
3. **You need an inter-rater baseline among the humans.** κ between an auditor and the AI is
   uninterpretable without knowing κ between two auditors on the same cases. Have at least two
   auditors review an overlapping subset and report human–human κ alongside human–AI κ. If two
   qualified auditors agree at κ = 0.5, an AI agreeing at κ = 0.5 is performing at human level,
   and reporting the second without the first is meaningless.
4. **Do not use the evaluation datasets as study material.** They are unambiguous by design, so
   a participant who disagrees with the tool is almost certainly wrong, and you would be
   measuring participant error rather than agreement. Build separate cases with genuine
   ambiguity for the study.

### 5.4 The measure that matters more than agreement

High agreement is **not** unambiguously good. A reviewer who accepts everything produces
`status_agreement = 1.0` and κ = undefined, and that is the automation-bias failure, not a
success.

**Seed a known-bad assessment and measure what proportion of reviewers catch it.** The best
available material is mode A's output on DATASET-005 (`EXAMPLE_RESULTS.md` §8.9): a
CRITICAL-rated finding against a control, on evidence that never mentions that control's
attribute, quantified to one decimal place, citing two policy *requirements* as if they were
exception records — every quote genuine, every quote verified, the conclusion nonsense. If
reviewers accept it, the human-review gate does not work, and that is the single most important
result the project could produce. Report it whichever way it comes out.

Design notes for a small study (8–15 participants is realistic and publishable at
dissertation scale): within-subjects and counterbalanced; measure time on task externally with
a stopwatch rather than trusting `review_seconds` (it is wall clock from when the item was last
opened, includes time away from the screen, restarts on reload, and a reviewer may decline to
record it — describe it only as an upper bound on a per-visit measurement); instrument which
panel participants actually open versus which they say they used; and take a short trust and
usefulness measure before and after.

---

## 6. Strengthening the evidence base

### 6.1 More datasets

`app.evaluation.datasets` is declarative — adding a case is writing a `SyntheticDataset` and a
generator function, not changing the harness. The gaps worth filling first, because each adds a
*capability* the suite currently cannot test at all:

* **Out-of-period evidence.** Clean DATASET-006 evidence dated after the period end; correct
  answer INSUFFICIENT_EVIDENCE for the period under audit. Cheap, and it tests a genuine audit
  requirement the system currently only flags for humans.
* **Contradictory sources.** Two artefacts that disagree about the same fact; correct answer is
  to report the contradiction, not to pick a side.
* **Immaterial exception rate.** 1 of 100 rather than 10 of 100 — tests whether the system
  hedges proportionately or treats every non-zero rate identically.
* **Genuinely ambiguous case.** The correct answer is "an auditor could defensibly conclude
  either way, and here is why". This is the hardest and most interesting case in audit and the
  suite contains none.
* **Adversarial evidence.** Favourable-but-irrelevant artefacts supplied in place of the ones
  requested — the situation in which an audit tool is most needed and is entirely untested.

Ten more cases roughly triples per-class support and moves the study from "one row changed" to
"a pattern".

### 6.2 A corpus that forces retrieval to choose

Put several controls' evidence, plus distractor documents, into one project so that `top_k`
becomes a binding constraint. Only then does `retrieval_recall` measure ranking, and only then
is §3.4 worth running.

---

## 7. What to report, section by section

| Thesis section | What to put in it |
|---|---|
| Methodology | `EVALUATION_METHODOLOGY.md` §§3–4 (conditions and exact metric formulae), §4.3 (both INSUFFICIENT_EVIDENCE framings and why both), §5 (ground truth), §7 (reproducibility). |
| Datasets | `SYNTHETIC_DATASETS.md` §1.1 (the held-fixed variable), the six case descriptions, and §8 (coverage *and* the gaps). Say explicitly that DATASET-005 is the discriminating case and DATASET-006 is an addition. |
| Results | The A/B/C table with **n and provider in the caption**; the prediction matrix; all three confusion matrices; the traceability table with denominators; the cost table; the fault-injection table clearly labelled. Every table carries its caveats. |
| Qualitative | One full worked assessment showing the four-way separation and the evidence trail (`EXAMPLE_RESULTS.md` §8), and the mode A vs mode C contrast on the same evidence (§8.9). This is more persuasive than any accuracy figure. |
| Discussion | The null result (B = C at rate 0.0) and why it is a property of the provider. The rails' cost (C loses a correct conclusion under injection) as a **trade**, presented from both sides. The saturated retrieval metric as a ceiling. |
| Limitations | `LIMITATIONS_AND_FUTURE_WORK.md` §§1–4, condensed but not softened. |
| Future work | §5 of the same document, ranked. |
| Appendices | One rendered audit report; the control library JSON with its disclaimer; the six datasets' manifest; one full `run.config` blob; one `prompt_snapshot` and its `raw_response`. |

### 7.1 Caption every table

> *Table N. A/B/C comparison over the six synthetic datasets. n = 6 per condition. Provider:
> `MockLLMProvider` (deterministic rule-based stand-in, rule set `mock-rules-1.1`, seed 1337,
> hallucination rate 0.0); prompt version 1.0.0, fingerprint `bdba31700292781d`. These figures
> measure the pipeline, not language-model capability.*

Without that caption the table is an overclaim. With it, it is a legitimate research result.

### 7.2 Report the null and negative results

The temptation is to lead with "modes B and C achieved 100% accuracy". Resist it. The
defensible narrative is:

* A → B changed two of six conclusions in the predicted direction, and changed strict citation
  grounding categorically (0.000 → 1.000).
* B = C on accuracy and grounding at the default setting. **This is a null result and it
  reproduces.** It is a property of a deterministic provider with nothing to catch, not a
  wiring bug — the two conditions demonstrably run different pipelines (1 vs 3 provider calls;
  ~10.3k vs ~29.6k tokens).
* Under fault injection, C's rails withdrew a **correct** conclusion, costing accuracy to buy
  defensibility. That is a design trade, presented from both sides, not a failure and not a win.
* Retrieval recall of 1.000 is a ceiling: the retriever never discarded a chunk.

A results chapter that reports those four things honestly is far stronger than one reporting
1.000 accuracy, because the first is checkable and the second invites an examiner to ask what
was measured.

---

## 8. Claims: supported, unsupported, and how to phrase them

| Claim | Status | Phrase it as |
|---|---|---|
| Retrieval + explicit requirement + output contract changed the conclusion on the missing-evidence case | **Supported** | "On DATASET-005, the unscaffolded baseline concluded POTENTIAL_DEFICIENCY where the scaffolded conditions correctly concluded INSUFFICIENT_EVIDENCE and named five missing artefacts." |
| Citation scaffolding makes output traceable | **Supported** | "Adding chunk identifiers moved strict citation grounding from 0.000 to 1.000 with no change to the provider; none of the baseline's citations were fabricated, but none could be verified." |
| The system verifies its own citations mechanically | **Supported** | "Every citation is re-resolved to a retrieved chunk and every quote re-matched against that chunk's stored text, reproducibly and without asking the model." |
| The workflow acts on detected fabrication where the single-call condition only records it | **Supported (fault injection)** | "Under injected faults, mode C withdrew a conclusion resting on a fabricated citation; mode B recorded the same fabrication and asserted the conclusion anyway." |
| The pipeline is reproducible | **Supported** | "Two independent executions produced identical status, risk, citation and token figures across all 18 assessments." |
| Mode C is more accurate than mode B | **Not supported** | Equal at the default setting; *worse* under fault injection. Say so. |
| The system detects hallucination | **Overstated** | "It detects unresolvable citations and quotations absent from the evidence" — and cite §4.3, where two policy requirements were cited as exception records at 100% match. |
| Retrieval quality is good | **Not supported** | The retriever supplied every chunk in every dataset. Report the metric as a ceiling. |
| The LLM performs well on audit tasks | **Not supported** | There was no LLM. Do not write this sentence in any form. |
| Human review catches AI errors | **Not supported** | No auditor has reviewed any output. The mechanism exists and is enforced; its efficacy is untested. |
| The risk model rates controls correctly | **Not supported** | "A prototype scoring model with published weights that produces an explainable, recomputable rating"; the weights are the author's own and the expected bands come from the same model. |
| The tool is usable in practice | **Not supported** | No user study, no authentication, SQLite, no GRC integration. |

---

## 9. Pre-submission checklist

- [ ] Every table caption states **n**, the provider, the model, the rule-set/prompt version.
- [ ] Both INSUFFICIENT_EVIDENCE framings reported, with `coverage` for the excluded one.
- [ ] The `caveats` list reproduced alongside the figures.
- [ ] `grounding_rate_strict` used throughout, and any table using the 0.5-credit UI figure
      says so explicitly.
- [ ] Undefined rates shown as "n/a", never as 0.
- [ ] The mock-provider disclosure appears in the methodology, in the results, **and** in the
      abstract or conclusion — not only in the limitations chapter.
- [ ] Retrieval recall labelled as a ceiling.
- [ ] The risk model labelled "prototype research risk scoring model — not an official industry
      framework" wherever a band appears.
- [ ] The B = C null result reported, not omitted.
- [ ] Mode C's accuracy loss under fault injection reported, not omitted.
- [ ] Every dataset stated to be synthetic; no output presented as describing a real
      organisation.
- [ ] The rule-set version recorded by hand from `health()` for every result set.
- [ ] `pytest tests -q` green against the same commit the results came from, and the commit
      recorded.

---

## 10. The two-sentence version, if you only remember one thing

**You have built and measured a pipeline that refuses to conclude when its evidence does not
address the question, and that mechanically re-checks every citation it makes — and you have
measured it honestly enough to show where that refusal costs you a correct answer.** What you
have not measured is a language model, a retriever under load, or an auditor, and the value of
the work rests on saying so as clearly as you say the rest.
