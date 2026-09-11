# Documentation index

Documentation for the **LLM-Assisted IT Audit Risk and Control Assessment System** — a university
research prototype. Start at [`../README.md`](../README.md) if you have not read it; it states what
the system is, what it explicitly does **not** do, and the safety rules with the module that
enforces each.

> Everything in this repository is **synthetic**. The control library, the evidence files, the
> account names and the audit findings are fabricated for research use and describe no real
> organisation, system, person or audit. The prototype has no authentication and no access
> control, and must not be pointed at real audit evidence.

---

## Pick a reading order

### “I have thirty minutes and I am marking this.”

1. [`../README.md`](../README.md) — §2 *What this is not*, then §3 *The research principle* with
   the worked MFA example. **10 min.**
2. [`EXAMPLE_RESULTS.md`](EXAMPLE_RESULTS.md) — §0 *Run provenance* and §1 *The A/B/C comparison*,
   including §1.1 on how to read the table honestly. **10 min.**
3. [`LIMITATIONS_AND_FUTURE_WORK.md`](LIMITATIONS_AND_FUTURE_WORK.md) — §6 *The one-paragraph
   honest summary*, then §1. **10 min.**

That is the whole claim, the evidence for it, and its boundaries. If you have another twenty
minutes, run [`QUICKSTART.md`](QUICKSTART.md) — it takes about five.

### “I want to see it work.”

1. [`QUICKSTART.md`](QUICKSTART.md) — install to generated report, about five minutes.
2. [`USER_GUIDE.md`](USER_GUIDE.md) — the operator's manual: every page of the console, how to
   read an assessment, and what the verification verdicts mean.
3. [`SETUP.md`](SETUP.md) — when you want a different provider, a different database, or the UI
   talking to the API over HTTP.

### “I am going to change the code.”

1. [`ARCHITECTURE.md`](ARCHITECTURE.md) — the layer map and the two data paths first; §7 *Design
   decisions worth defending* before you change any of them.
2. [`DATABASE_SCHEMA.md`](DATABASE_SCHEMA.md) — in particular *The AI record vs the human record*,
   which is the separation everything else depends on.
3. [`API.md`](API.md) — if you are touching a router or writing a client.
4. [`TESTING.md`](TESTING.md) — §*The research-critical tests* tells you which failures mean
   "you broke the research", not "you broke a test".
5. [`BUILD_SPEC.md`](BUILD_SPEC.md) — the contract the code was written against, including the
   Python 3.9 constraints.

### “I am writing up results from this.”

1. [`RESEARCH_NOTES.md`](RESEARCH_NOTES.md) — §0 *What you are allowed to claim*, then the
   experiment recipes. This is the operational guide.
2. [`EVALUATION_METHODOLOGY.md`](EVALUATION_METHODOLOGY.md) — the definition of every metric you
   will quote, and §9 *Threats to validity*.
3. [`SYNTHETIC_DATASETS.md`](SYNTHETIC_DATASETS.md) — what each case actually contains, so you can
   describe your instrument.
4. [`EXAMPLE_RESULTS.md`](EXAMPLE_RESULTS.md) — the reference figures your run should reproduce,
   and the provenance block your own results need to carry.
5. [`LIMITATIONS_AND_FUTURE_WORK.md`](LIMITATIONS_AND_FUTURE_WORK.md) — all of it, before you
   write a sentence with a number in it.

### “I am an auditor and I want to know whether to trust this.”

1. [`../README.md`](../README.md) §2 and §10 — what it refuses to do, and how each refusal is
   enforced in code rather than promised.
2. [`LIMITATIONS_AND_FUTURE_WORK.md`](LIMITATIONS_AND_FUTURE_WORK.md) §3 *Limitations of the
   system as an audit tool*.
3. Generate a report ([`QUICKSTART.md`](QUICKSTART.md) step 7) and read Section 10 of it. The
   limitations travel with the document, not with the documentation.

---

## The documents

Reading times are approximate; the two marked *reference* are meant to be searched, not read through.

| Document | Read | What it is |
|---|---|---|
| [`QUICKSTART.md`](QUICKSTART.md) | ~15 min, or ~5 min to run | The shortest path from a fresh clone to a completed assessment, a recorded human review and a generated report. Numbered steps, exact commands, the exact output to expect at each one. Every command in it was executed against a clean copy of the repository. |
| [`USER_GUIDE.md`](USER_GUIDE.md) | ~20 min | How to operate the console, page by page, and how to read what it returns: the four-way REQUIRES/PROVES/INFERS/VERIFIES split, the citation verification verdicts, the safety rails, the three assessment modes and which to use, the review actions, and a suggested working order for a real engagement. |
| [`SETUP.md`](SETUP.md) | ~15 min | Installation and operation in full: prerequisites, install, first run, all six entry points, the complete configuration reference, switching to a real or locally-hosted LLM, embeddings, PostgreSQL, running the UI against the API, troubleshooting, and security notes for handling evidence. |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | ~30 min | The system **as implemented**, not as intended: the layer map and why each boundary exists, the evidence-ingestion path, the control-assessment path step by step, the three experimental conditions, the provider abstraction and how to substitute a local model, seven design decisions with the counter-argument to each, and the known architectural limits. |
| [`DATABASE_SCHEMA.md`](DATABASE_SCHEMA.md) | reference | All twelve tables, generated from the live SQLAlchemy metadata so the columns are what exists rather than what was intended. ER diagram, the conventions every table follows, **why the AI record and the human record are separate tables**, the JSON columns, the SQLite → PostgreSQL path, and the schema's known limitations. |
| [`API.md`](API.md) | reference | The HTTP reference for the FastAPI backend: conventions, the error envelope, and every endpoint under `/api/v1` with its request and response shapes — projects, controls, evidence, assessments, reviews, dashboard, reports, evaluation, settings — plus the API's known limitations. (The app also serves live Swagger UI at `/docs`.) |
| [`SYNTHETIC_DATASETS.md`](SYNTHETIC_DATASETS.md) | ~20 min | The six evaluation cases, one section each: how the dataset is built, exactly what is in the generated files, what the correct answer is, **why** that is the correct answer, which marker strings retrieval must surface, and which capability the case probes. Closes with what the suite covers and what it does not. |
| [`EVALUATION_METHODOLOGY.md`](EVALUATION_METHODOLOGY.md) | ~25 min | The measurement protocol: the research question, the design, the independent variable (conditions A/B/C), every dependent variable with its exact definition, how ground truth is established, the procedure, what makes a run reproducible, what is deliberately held fixed, and the threats to validity. |
| [`EXAMPLE_RESULTS.md`](EXAMPLE_RESULTS.md) | ~25 min | Every figure the project reports, each with its provenance: the A/B/C comparison, per-dataset predictions, confusion matrices, traceability and hallucination rates, cost, why retrieval recall is a ceiling, **the fault-injection experiment that discriminates B from C**, a full DATASET-005 walkthrough, and a summary of what the results do and do not support. |
| [`RESEARCH_NOTES.md`](RESEARCH_NOTES.md) | ~15 min | The practical guide to producing dissertation results: what you are allowed to claim, the CLI traps, the core experiment, what to vary in priority order, how to get the data out, how to run a human–AI agreement study, what to report section by section, and a pre-submission checklist. |
| [`LIMITATIONS_AND_FUTURE_WORK.md`](LIMITATIONS_AND_FUTURE_WORK.md) | ~20 min | Deliberately the least flattering document in the set: everything an examiner would otherwise have to find for themselves, and what would have to be done about each. Read §6 if you read nothing else. |
| [`TESTING.md`](TESTING.md) | ~15 min | The 713-test suite: how to run it, how it is isolated from your database and the network, what each module covers, which tests are research-critical, the fixtures, how to add a test, and **what is not covered**. |
| [`BUILD_SPEC.md`](BUILD_SPEC.md) | ~10 min | The binding implementation contract the codebase was built against — hard rules (Python 3.9 syntax, no secrets in code, offline-first, synthetic data only), module ownership, and the interface between components. Historical: it specifies the build, and the other documents describe the result. |

---

## Which document owns which claim

If two documents ever disagree, the one named here is the source of truth and the other is stale.

| Claim | Owned by |
|---|---|
| What the system refuses to do, and how each refusal is enforced | [`../README.md`](../README.md) §2, §10 |
| How a request flows through the code | [`ARCHITECTURE.md`](ARCHITECTURE.md) |
| What a column means | [`DATABASE_SCHEMA.md`](DATABASE_SCHEMA.md) |
| What an endpoint returns | [`API.md`](API.md), and the live `/openapi.json` |
| What a configuration setting does | [`SETUP.md`](SETUP.md) and `.env.example` |
| The definition of a metric | [`EVALUATION_METHODOLOGY.md`](EVALUATION_METHODOLOGY.md) |
| The value of a metric | [`EXAMPLE_RESULTS.md`](EXAMPLE_RESULTS.md) |
| What a dataset contains and why its answer is correct | [`SYNTHETIC_DATASETS.md`](SYNTHETIC_DATASETS.md) |
| What a result does not support | [`LIMITATIONS_AND_FUTURE_WORK.md`](LIMITATIONS_AND_FUTURE_WORK.md) |
| What the tests guarantee | [`TESTING.md`](TESTING.md) |

Below the documentation sits the code, which outranks all of it. The module docstrings in
`app/audit/validators.py`, `app/audit/risk.py`, `app/audit/engine.py`, `app/audit/prompts.py` and
`app/evaluation/metrics.py` explain *why* each component behaves as it does and are worth reading
directly.

---

## Three things that are true of every document here

1. **All data is synthetic.** Every figure, file, account, ticket and finding is fabricated.
2. **The default results were not produced by a language model.** The offline provider is a
   deterministic rule-based stand-in. Results obtained with it measure the pipeline — retrieval,
   the output contract, citation validation, the rails, the workflow — and say nothing about the
   capability of any model. The correct phrasing is *"the pipeline reached the planted conclusion
   in k of 6 cases"*.
3. **n = 6.** Every proportion reported anywhere in this documentation is descriptive of those six
   cases. No significance testing is offered, because none would be honest at that sample size.
