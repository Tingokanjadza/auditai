# Testing

**713 tests across 16 modules.** Configuration: `pytest.ini`. Fixtures and isolation:
`tests/conftest.py`.

```
$ .venv/bin/python -m pytest tests -q
........................................................................ [ 10%]
   …
713 passed, 2 warnings in 56.74s
```

*(The two warnings are Starlette deprecating `HTTP_422_UNPROCESSABLE_ENTITY` and
`HTTP_413_REQUEST_ENTITY_TOO_LARGE` in favour of their renamed constants. They come from the
framework, not from this application. `pytest.ini` shows warnings rather than silencing them,
filtering only the third-party ones this project cannot fix.)*

**Contents**

- [Running the suite](#running-the-suite)
- [How the suite is isolated](#how-the-suite-is-isolated)
- [What each module covers](#what-each-module-covers)
- [The research-critical tests](#the-research-critical-tests)
- [Fixtures](#fixtures)
- [Adding a test](#adding-a-test)
- [What is NOT covered](#what-is-not-covered)

---

## Running the suite

```bash
.venv/bin/python -m pytest tests -q                  # everything (~57 s)
.venv/bin/python -m pytest tests -q -m "not slow"    # 709 tests
.venv/bin/python -m pytest tests -q -m slow          # 4 tests, ~7 s
.venv/bin/python -m pytest tests/test_engine.py -v   # one module, verbose
.venv/bin/python -m pytest tests -q -k "insufficient_evidence"
.venv/bin/python -m pytest tests -x --lf             # stop at first failure, rerun last failures
```

`pytest.ini` sets `--strict-markers` — a mistyped marker is a test that silently never runs the way
it was meant to, so it is an error instead.

### About the `slow` marker

Four tests carry `@pytest.mark.slow`: three whole-suite evaluation runs in
`tests/test_evaluation_runner.py` and one end-to-end evaluation run over HTTP in
`tests/test_api.py`.

**Measured on this machine:** the whole suite 56.74 s · `-m "not slow"` (709 tests) 41.71 s ·
`-m slow` (4 tests) 6.91 s. So deselecting the slow tests saves roughly a quarter of the wall clock,
not most of it — the remaining cost is spread across the session-scoped `generated_datasets` fixture
(which writes real `.docx`, `.xlsx` and `.pdf` bytes) and the per-test drop/create of the schema,
neither of which a marker can skip. For a tight development loop, `-k` or a single module is a much
bigger win than `-m "not slow"`.

### Temporary directories

`pytest_sessionfinish` removes the run's temporary tree **only when the run was green**, so a
failure can be inspected afterwards. A failing run therefore leaves a directory behind:

```bash
ls -d $TMPDIR/itaudit-tests-*      # inspect
rm -rf $TMPDIR/itaudit-tests-*     # clean up
```

---

## How the suite is isolated

This matters more than any individual test: the suite ingests files, writes reports and seeds a
control library, and none of that may touch the developer's data.

**The redirection happens at module scope in `conftest.py`, above the imports.** `app.config`
builds its `Settings` at import time and `app.database.base` creates the engine from it, also at
import time — so by the time any test module has said `import app`, the database URL and every
writable directory are already fixed. A fixture would be too late. `conftest.py` therefore:

1. Raises immediately if `app.config` is already in `sys.modules` (which would mean something
   imported the app before the redirection).
2. Creates `TMP_ROOT = tempfile.mkdtemp(prefix="itaudit-tests-")` and points `DATABASE_URL`,
   `DATA_DIR`, `UPLOAD_DIR`, `REPORT_DIR`, `SYNTHETIC_DIR`, `EVALUATION_OUTPUT_DIR` and
   `SAMPLE_EVIDENCE_DIR` inside it.
3. Sets `LLM_PROVIDER=mock`, `EMBEDDING_PROVIDER=local`, `MOCK_HALLUCINATION_RATE=0.0`,
   `MOCK_SEED=1337`, `USE_API=false`.
4. **Removes** `LLM_API_KEY`, `LLM_BASE_URL`, `EMBEDDING_API_KEY` and `EMBEDDING_BASE_URL` from the
   environment — removed rather than blanked, because an empty string is still "set" to some
   libraries and a developer's real key must not reach a provider constructor even by accident.

`controls_dir` is deliberately **not** redirected: the control library JSON is read-only study data
that the seeding path needs, and copying it would test a copy rather than the file the application
ships.

**None of this is taken on trust.** `tests/test_config.py` asserts it:

| Test | Asserts |
|---|---|
| `test_engine_url_points_inside_the_temporary_tree` | The **live SQLAlchemy engine** — not merely the settings object — is redirected. |
| `test_real_project_database_is_never_the_target` | `data/audit.db` is not what the suite writes to. |
| `test_writable_directories_are_inside_the_temporary_tree` | Parametrised over all five writable path settings. |
| `test_real_upload_directory_is_not_written_to` | |
| `test_control_library_directory_is_deliberately_not_redirected` | The one exception is intentional. |
| `test_no_llm_credentials_are_configured` | A real key in the developer's environment cannot reach a provider. |

**No network, proved rather than assumed.** A session-scoped autouse fixture
(`no_remote_llm_client`) monkey-patches `openai.OpenAI` to raise `RemoteClientConstructed`. Removing
the API key already means the factory returns the mock — but that is a fact about *configuration*.
This closes the remaining gap: if any code path ever reaches a real client constructor, the suite
fails at that line with a named error instead of silently attempting a network call.
`tests/test_llm_providers.py::test_the_guard_against_building_a_remote_client_is_armed` checks that
the guard is actually armed.

**Verified directly while writing this document:** `shasum -a 256 data/audit.db` was identical
before and after a test run.

---

## What each module covers

| Module | Tests | Covers |
|---|---|---|
| `test_config.py` | 31 | Settings parsing, validators (provider names lower-cased, strategy upper-cased, hallucination rate clamped to a probability), secret masking, `ensure_directories()` — **and the proof that the suite cannot touch real data**. |
| `test_enums_schemas.py` | 80 | Every enum round-trips through `coerce()`; unmappable input returns the conservative default rather than raising. The `AssessmentOutput` contract: tolerant coercion of a chatty or malformed model answer, and the rule that **a model cannot waive human review**. |
| `test_parsers.py` | 34 | Document parsing and the locator convention the whole citation trail rests on: 1-based spreadsheet rows, exactly one `TABLE_SUMMARY` chunk per sheet, PDF page numbers, DOCX heading tracking, and that `parse_file` never raises on malformed input (parametrised over eight broken files). |
| `test_storage.py` | 32 | SHA-256 integrity, content addressing, and path safety — a traversing upload name cannot escape the upload directory, identical bytes collapse onto one file, different bytes under one name never overwrite each other. |
| `test_retrieval.py` | 44 | Query construction from a control (ORM row or dict), all three retrievers, the per-file diversity cap, forced table summaries, deterministic local embeddings, vector round-trip through the database, and indexing/re-indexing semantics. |
| `test_llm_providers.py` | 44 | The mock is deterministic, labels itself a rule-based stand-in, answers in the right shape for each workflow purpose, **quotes text that is literally in the chunk it cites**, and never cites a chunk it was not given. The hallucination lever actually injects something; the default injects nothing. Plus `extract_json` against seven real-world malformed shapes. |
| `test_validators.py` | 45 | The anti-hallucination layer: the four citation verdicts, the numeric-claim scanner (including identifiers and cross-references that must **not** be read as statistics), the three grounding rates, and the safety rails. |
| `test_risk.py` | 49 | The prototype risk model, **checked by hand**: factor derivation, per-factor contributions, band thresholds, the `EFFECTIVE` ceiling, the `INSUFFICIENT_EVIDENCE` floor, the exception-rate uplift and its cap, and that the model may escalate by exactly one band and never lower one. |
| `test_engine.py` | 45 | The orchestrator, and most of [the research-critical tests](#the-research-critical-tests): the four headline cases, mode differences, call counts, workflow order, latency decomposition, failure recording, and persistence. |
| `test_service.py` | 59 | The shared data layer: filter semantics for both front ends, whitelisted updates, soft-deleted controls, idempotent scoping, evidence listing and stats, and `dashboard_stats`'s counting basis with its `definitions` block. |
| `test_review.py` | 37 | Human review and the agreement flags the human/AI study is measured on — case by case, including the two defaults that shape the metric. |
| `test_report.py` | 43 | The ten mandated sections in both renderers, the two provenance labels verbatim, pending controls excluded from headline figures, the risk-model label, the limitations section, the evidence appendix with resolvable locators, and mock-provider disclosure. |
| `test_datasets.py` | 62 | That the **files on disk still satisfy the declared ground truth** — planted counts, exception rows and every key evidence marker re-derived with the production parser. Ground truth asserted only in a docstring is not ground truth. |
| `test_metrics.py` | 47 | The evaluation arithmetic against hand-computed values, and the degenerate cases where a metric silently starts lying: an empty denominator must be `None` and not `0.0`; Cohen's kappa must be `None` and not `1.0` when both raters used one category. |
| `test_evaluation_runner.py` | 31 | The experiment procedure: per-dataset project isolation, run tagging, a crashed dataset scored as wrong rather than dropped, reproducible run config with no secret in it, and cleanup that removes only harness projects. |
| `test_api.py` | 30 | The HTTP surface end to end, the error envelope for every status code, and what the API is allowed to disclose — no secrets, no filesystem paths, no connection string. |

---

## The research-critical tests

These are the ones a thesis examiner would ask about. Each asserts a property the research claims
rest on, and each would fail loudly if that property regressed.

### 1. "I cannot tell" must be reachable, and must name what is missing

```
tests/test_engine.py::test_a_listing_with_no_mfa_column_yields_insufficient_evidence[B_RAG|C_RAG_WORKFLOW]
tests/test_engine.py::test_the_missing_mfa_artefact_is_named_not_merely_implied[...]
```

**DATASET-005** — an MFA policy plus a privileged-user listing with **no MFA column at all**. The
status must be `INSUFFICIENT_EVIDENCE`, and `missing_evidence` must **name** the absent artefact. A
bare "insufficient evidence" tells an auditor nothing about what to go and obtain, so the second
test exists separately from the first.

**Why it matters:** this is the case the whole project turns on — the failure mode is not being
wrong, it is being confident about evidence that never addressed the question.

### 2. The same control, the same policy, three different correct answers

```
tests/test_engine.py::test_ten_of_a_hundred_disabled_yields_a_potential_deficiency_with_verified_citations[...]
tests/test_engine.py::test_a_configured_value_below_its_policy_threshold_is_not_effective[...]
tests/test_datasets.py::test_three_datasets_hold_the_requirement_fixed_and_vary_only_the_evidence
```

DATASET-001 (10 of 100 disabled → `POTENTIAL_DEFICIENCY`, with **verified** citations), DATASET-005
(`INSUFFICIENT_EVIDENCE`) and DATASET-006 (all compliant → `EFFECTIVE`) all test **CONTROL-001
against the same policy document, byte for byte**. Only the operational evidence differs.
DATASET-003 adds a different shape of failure: a threshold comparison (policy requires 14,
configuration shows 8) → `NOT_EFFECTIVE`.

**Why it matters:** it is the cleanest available demonstration that the system is reading the
*evidence* rather than paraphrasing the *control text*.

### 3. The supplied file must not be reported as missing

```
tests/test_engine.py::test_the_supplied_listing_is_not_reported_as_missing_evidence[...]
```

Asking the auditor for the file they just uploaded destroys trust in the tool faster than a wrong
status does.

### 4. The rails catch an ungrounded conclusion — in mode C, and deliberately not in mode B

```
tests/test_engine.py::test_a_fabricated_citation_is_recorded_as_fabricated
tests/test_engine.py::test_mode_c_withdraws_an_effective_conclusion_built_on_a_fabricated_citation
tests/test_engine.py::test_mode_b_is_deliberately_left_unrailed
tests/test_validators.py::test_an_ungrounded_effective_conclusion_is_withdrawn
tests/test_validators.py::test_an_ungrounded_accusation_is_withdrawn_too
tests/test_validators.py::test_a_hedged_flag_is_left_alone
```

A `ScriptedLLM` returns an `EFFECTIVE` conclusion resting on a citation to a chunk that was never
retrieved. Mode C withdraws it to `INSUFFICIENT_EVIDENCE`; mode B records the same fabrication and
**leaves the conclusion standing**.

**Why it matters:** the difference between those two outcomes *is* the experiment. Railing the
baseline would erase what the study measures. The validator tests add the two halves of the same
rule: an unsupported clean opinion **and** an unsupported accusation are both withdrawn, while
`POTENTIAL_DEFICIENCY` is left alone because suppressing a hedged flag would hide the signal an
auditor most needs.

### 5. Human review cannot be waived

```
tests/test_enums_schemas.py::test_a_model_cannot_waive_human_review_by_setting_the_flag
tests/test_enums_schemas.py::test_human_review_required_is_true_whatever_the_model_sends[False|"false"|"no"|0|None|"absolutely not"]
tests/test_engine.py::test_human_review_is_forced_even_when_the_model_says_otherwise
tests/test_validators.py::test_human_review_can_never_be_waived
tests/test_validators.py::test_the_human_review_rail_is_recorded_even_on_a_clean_assessment
```

Three independent enforcement points, tested at each: the pydantic validator, the safety rails, and
the engine's persistence. The scripted model answers `human_review_required: false` in **every**
mode and is overruled in every one.

**Why it matters:** this is the property whose failure would invalidate the ethical frame of the
whole project. One enforcement point is one point of regression.

### 6. A fabricated citation is kept and labelled, never deleted

```
tests/test_validators.py::test_fabricated_citations_are_flagged_and_kept_not_deleted
tests/test_validators.py::test_the_rails_never_mutate_the_model_answer
```

The rails return a corrected **copy**; the model's unedited answer survives as the audit trail. A
hallucination rate is only checkable if what the model actually said still exists.

### 7. The AI record and the human record never merge

```
tests/test_review.py::test_the_review_never_edits_the_ai_record
tests/test_review.py::test_rejected_that_lands_on_the_same_status_is_recorded_as_agreement
tests/test_review.py::test_asking_for_more_evidence_is_not_a_conclusion
tests/test_review.py::test_a_pending_review_never_counts_as_agreement
tests/test_review.py::test_status_and_risk_agreement_are_recorded_separately
```

These pin down the *definition* of agreement, which is a research decision rather than an
implementation detail: agreement is about the **outcome**, so a `MODIFIED` or `REJECTED` decision
landing on the same status counts; `MORE_EVIDENCE_REQUESTED` with no explicit status does **not**
inherit the AI's; a `PENDING` row is not a judgement.

### 8. Risk is computed, and the arithmetic is checked by hand

```
tests/test_risk.py::test_factor_derivation_matches_the_published_formula
tests/test_risk.py::test_the_headline_case_scores_and_bands_as_hand_computed
tests/test_risk.py::test_an_unverifiable_control_is_never_reported_as_low_risk[high|low]
tests/test_risk.py::test_the_model_may_escalate_by_exactly_one_band
tests/test_risk.py::test_the_model_can_never_lower_a_rating
tests/test_risk.py::test_every_rating_is_labelled_as_a_prototype
```

The expected factor values, contributions and bands are written out in the test rather than compared
against whatever the code returned. The `INSUFFICIENT_EVIDENCE` floor is tested against both a
high-exposure and a low-exposure control, because the point is that abstention is never rewarded
with a LOW rating.

### 9. The modes differ in exactly the documented ways

```
tests/test_engine.py::test_mode_a_performs_no_retrieval_and_records_that_fact
tests/test_engine.py::test_mode_a_is_still_measured_against_the_text_it_was_shown
tests/test_engine.py::test_the_workflow_costs_exactly_the_calls_it_claims[A=1|B=1|C=3]
tests/test_engine.py::test_the_workflow_steps_run_in_the_documented_order
tests/test_engine.py::test_the_unrailed_modes_report_the_model_answer_verbatim[A|B]
tests/test_engine.py::test_latency_separates_retrieval_from_inference
```

If A silently retrieved, or C stopped running its pre-check, every comparative figure in the write-up
would be meaningless. `test_mode_a_is_still_measured_against_the_text_it_was_shown` guards the
deliberate decision to validate A against the raw chunks it was shown rather than against an empty
set — scoring it against nothing would report a 100 % hallucination rate that measures the prompt
format rather than the model.

### 10. Failure is recorded, never lost

```
tests/test_engine.py::test_every_mode_persists_an_assessment_when_the_provider_fails[A|B|C]
tests/test_engine.py::test_a_failed_run_says_so_in_words_an_auditor_reads
tests/test_engine.py::test_the_technical_error_is_kept_out_of_the_narrative_fields
tests/test_engine.py::test_assess_project_records_a_failure_per_control_and_keeps_going
tests/test_evaluation_runner.py::test_a_dataset_that_crashes_is_recorded_as_a_wrong_answer_not_dropped
```

A `BrokenLLM` provider fails every call. Every mode still persists a row carrying the error, and one
bad control never ends a project run. In the harness, a crashed dataset counts **against** accuracy
rather than disappearing from the denominator — removing failed rows would score the system on the
subset that happened to work.

### 11. Ground truth is re-derivable from the bytes

```
tests/test_datasets.py::test_the_declaration_is_re_derivable_from_what_was_written[DATASET-001…006]
tests/test_datasets.py::test_every_key_evidence_marker_is_literally_in_its_own_document[...]
tests/test_datasets.py::test_the_exception_rows_are_where_the_dataset_says_they_are
tests/test_datasets.py::test_dataset_005_contains_no_mfa_token_anywhere
tests/test_datasets.py::test_the_generator_is_deterministic
tests/test_datasets.py::test_the_confusion_matrix_has_a_true_negative_class
```

The generated files are re-parsed with the **production parser** and checked against the
declarations. `DATASET-005` is scanned for ten forbidden substrings (`mfa`, `two-factor`, `2fa`,
`authenticator`, …) — if one leaked in, the case the study turns on would quietly stop being the
case it claims to be. And without an `EFFECTIVE` dataset, "answer `POTENTIAL_DEFICIENCY` to
everything" would score 100 % accuracy, which
`test_the_confusion_matrix_has_a_true_negative_class` exists to prevent.

### 12. The metrics do not lie on degenerate input

```
tests/test_metrics.py::test_a_missing_prediction_is_labelled_rather_than_dropped_from_the_denominator
tests/test_metrics.py::test_kappa_is_undefined_when_both_raters_used_one_category
tests/test_metrics.py::test_a_system_that_never_raises_a_deficiency_has_no_precision_not_zero
tests/test_metrics.py::test_every_metric_over_an_empty_input_is_none_or_zero_never_a_rate
tests/test_metrics.py::test_compute_metrics_warns_when_the_sample_is_too_small_to_mean_much
tests/test_metrics.py::test_the_default_framing_counts_abstention_as_a_miss
```

Reporting F1 as `0.0` would read as a *measured failure* rather than an *absent measurement*, and
kappa of `1.0` when chance already predicts everything would be simply false.
`test_the_default_framing_counts_abstention_as_a_miss` is the honesty check on the system's own
favourite behaviour: an honest abstention on a real deficiency counts as a **miss** by default.

### 13. The report keeps AI and human separate, visibly

```
tests/test_report.py::test_the_two_provenance_labels_appear_verbatim[markdown|html]
tests/test_report.py::test_an_unreviewed_control_is_marked_pending_not_concluded[...]
tests/test_report.py::test_the_ai_conclusion_and_the_auditors_are_both_printed
tests/test_report.py::test_only_auditor_decisions_count_towards_confirmed_findings
tests/test_report.py::test_the_mock_provider_is_disclosed_in_the_report[...]
tests/test_report.py::test_the_limitations_section_states_what_the_report_cannot_establish[...]
```

The labels are asserted **verbatim** because a reader learns to scan for those exact strings and a
near-miss variant would defeat that. Most tests are parametrised over both renderers: a section that
quietly appears in one and not the other is exactly the drift the design prevents.

### 14. Research runs never contaminate an auditor's figures

```
tests/test_evaluation_runner.py::test_each_dataset_gets_its_own_isolated_project
tests/test_evaluation_runner.py::test_every_assessment_is_tagged_with_its_run
tests/test_service.py::test_evaluation_assessments_are_excluded_from_operational_figures
tests/test_report.py::test_evaluation_runs_are_excluded_by_default
tests/test_evaluation_runner.py::test_the_run_config_records_the_provider_that_actually_ran
```

Per-dataset isolation is the **experimental control**, not tidiness: one shared project would let
DATASET-001's MFA export be retrieved while assessing DATASET-005. And a results table naming the
*requested* provider after a silent fallback to the mock would be a false statement, which the last
test prevents.

### 15. The API cannot leak what it holds

```
tests/test_api.py::test_settings_are_disclosed_without_secrets_or_paths
tests/test_api.py::test_configuration_cannot_be_changed_over_http
tests/test_api.py::test_provider_status_makes_no_network_call
tests/test_api.py::test_the_openapi_document_states_that_there_is_no_authentication
tests/test_api.py::test_the_full_audit_workflow_end_to_end
```

The last one walks the entire path an auditor would: create a project, scope a control, upload
evidence, run an assessment, read the citations back to their chunk, record a review, generate a
report and download it. If it passes, the routers are wired to the same service layer the Streamlit
app uses.

---

## Fixtures

Defined in `tests/conftest.py`.

### Database

| Fixture | Scope | Gives you |
|---|---|---|
| `clean_database` | function | Every table dropped and recreated. Dropping rather than truncating keeps the fixture honest about schema changes. |
| `db_session` | function | A `Session` on an empty database. |
| `seeded_session` | function | A `Session` on a database holding the full 14-control library. |
| `control` | function | `CONTROL-001`, the MFA control three of the six datasets are written against. |
| `project` | function | A project with `CONTROL-001` in scope and no evidence yet. |

### Evidence

| Fixture | Scope | Gives you |
|---|---|---|
| `sample_files` | session | Four real files written once: a 50-account CSV with exactly five MFA exceptions at rows **7, 14, 23, 38, 45**; a policy `.txt`; a policy `.docx` with a heading and a table; a two-sheet config `.xlsx`. Their numbers are fixed **in `conftest.py`** as module constants (`FIXTURE_CSV_ROWS`, `FIXTURE_CSV_EXCEPTIONS`, `FIXTURE_CSV_EXCEPTION_ROWS`, `FIXTURE_POLICY_SENTENCE`, `FIXTURE_CONFIG_FIRST_ROW`) so a test asserts against a count it can read in that file. |
| `ingested_project` | function | `project` with all four artefacts stored, parsed, chunked and indexed, each with its declared evidence type. The correct conclusion for `CONTROL-001` over this evidence is `POTENTIAL_DEFICIENCY` (5 of 50 not enrolled). |
| `generated_datasets` | session | Every synthetic dataset written to disk once, keyed by dataset id. Session-scoped because writing `.docx`/`.xlsx`/`.pdf` bytes six times is the most expensive thing the suite does — and the bytes are deterministic. |
| `dataset_project` | function | A **factory**: `dataset_project("DATASET-005") -> (dataset, project)`. Each dataset gets its own project, exactly as the harness does it. |

### Providers

| Fixture | Gives you |
|---|---|
| `mock_llm` | A `MockLLMProvider` bound to the test settings. |
| `broken_llm` | A provider that raises `LLMError` on every call, for the "failure is recorded, not swallowed" tests. Counts its calls. |
| `scripted_llm` | The `ScriptedLLM` **class**: construct it with `{purpose: payload}` to reproduce a specific model behaviour exactly (a fabricated citation, an overstated conclusion). It records the purposes it was asked for, which is how call order is asserted. |
| `engine_factory` | `_make(llm=None, retriever=None) -> AssessmentEngine` bound to the test session. |
| `no_remote_llm_client` | Session autouse. Makes constructing a real OpenAI client raise. Yields the exception type so a test can assert the guard is armed. |

### End-to-end

| Fixture | Gives you |
|---|---|
| `completed_assessment` | One persisted mode-C assessment over the fixture evidence. |
| `completed_review` | An auditor decision accepting that assessment unchanged. |
| `api_client` | A `TestClient` over a freshly created schema. The app's own lifespan seeds the library and the demo project, so this is the state a real deployment has on first boot. |
| `api_session` | A direct `Session`, for asserting what the API actually wrote. |
| `settings`, `tmp_root` | The live settings object; the temporary tree every writable path lives under. |

---

## Adding a test

1. **Put it in the module that owns the behaviour** — the sixteen modules mirror the application
   packages. A new endpoint goes in `test_api.py`; new grounding logic goes in `test_validators.py`.

2. **Name the test as the claim it makes.** The house style is a full sentence:
   `test_an_ungrounded_effective_conclusion_is_withdrawn`, not `test_rails_2`. A reader scanning
   `pytest -v` output should be able to read the specification off the test names.

3. **Write the docstring as the *why*,** one line, only when the name cannot carry it:

   ```python
   def test_a_hedged_flag_is_left_alone():
       """POTENTIAL_DEFICIENCY already asserts only a possibility; suppressing it would
       hide the signal an auditor most needs to see."""
   ```

4. **Start from a fixture, never from the real database.** Use `seeded_session` /
   `ingested_project` / `dataset_project`. Never construct a `Session` from
   `app.database.base.SessionLocal` in a test module outside the fixtures — the redirection makes
   that safe today, but the fixtures are what keep tests independent of one another.

5. **Assert against a value you can justify in the test**, not against whatever the code returns.
   `test_risk.py` and `test_metrics.py` are the models here: the expected numbers are computed by
   hand in a comment and then asserted.

6. **Use `ScriptedLLM` to reproduce a model behaviour**, rather than trying to coax the mock into it:

   ```python
   def test_something(engine_factory, ingested_project, scripted_llm):
       llm = scripted_llm({
           "sufficiency": {"can_conclude": True, "evidence_sufficiency": "SUFFICIENT"},
           "assessment": {"status": "EFFECTIVE", "evidence": [{"chunk_id": 9999, "quoted_text": "…"}]},
           "critique": {"overstated_conclusion": False},
       })
       result = engine_factory(llm=llm).assess_control(
           ingested_project.id, "CONTROL-001", mode=ExperimentMode.C_RAG_WORKFLOW, persist=False
       )
       assert result.output.status is AssessmentStatus.INSUFFICIENT_EVIDENCE
   ```

7. **Mark it `@pytest.mark.slow` only if it runs a whole evaluation suite.** `--strict-markers` means
   a typo is an error, and new markers must be registered in `pytest.ini`.

8. **Never make a network call, and never write outside the temporary tree.** Both are structurally
   prevented, and both would be caught — but a test that tries is a design mistake, not a failure to
   configure.

9. **Run the whole suite before you finish.** Several tests assert exact statuses per dataset, so a
   change to the mock provider's rules, the prompts, or the retrieval defaults can turn the suite red
   without anything being *wrong* — that is the suite doing its job. Read such a failure as "the
   instrument changed", re-derive the expected values, and bump `MOCK_RULES_VERSION` /
   `PROMPT_VERSION` so old results are not pooled with new ones.

---

## What is NOT covered

Stated plainly, because a coverage claim that hides its gaps is worse than no claim.

1. **No line-coverage figure.** `coverage.py` is not in the installed dependency set and was not
   added (the build spec forbids adding dependencies without a strong reason). What can be said is
   that the mandated behaviours are genuinely asserted and that the core ones are covered by the
   tests listed above. What **cannot** be said is what percentage of the code executes —
   `app/` is about 36 900 lines, of which roughly 25 100 sit outside `app/frontend/` and are the
   part this suite exercises at all. There will be branches nothing reaches.

2. **`app/frontend/` has no tests at all.** Ten page modules and `data_access.py`'s 70 public
   functions — about 11 800 lines — are unverified by this suite. Streamlit pages are not straightforwardly
   unit-testable and the frontend is not in the build spec's `tests/` list. Streamlit's `AppTest`
   harness would make this possible; the work was done ad hoc during development and never ported
   into `tests/`. **This is the largest single gap.**

3. **`USE_API=true` is not exercised.** Every test runs the service layer in-process. The HTTP
   transport is tested through `TestClient` in `test_api.py`, but the *combination* (Streamlit
   talking to a running FastAPI backend) is not.

4. **No test runs against a real language model.** Everything is the deterministic mock, by
   necessity — a test suite that needed an API key would not be hermetic. So the suite verifies the
   **pipeline** and says nothing about model capability. `app/llm/openai_provider.py`'s retry,
   backoff and structured-output-degradation ladder is exercised only for the paths reachable
   without a client.

5. **No test runs against PostgreSQL.** `conftest.py` redirects to temporary SQLite. The schema is
   written to be portable and the DDL compiles cleanly for the PostgreSQL dialect, but
   `VARCHAR(n)` enforcement, connection pooling and any dialect-specific behaviour are untested.

6. **Retrieval quality is not measured, only retrieval *correctness*.** Each evaluation project holds
   2–3 files (10–14 chunks) and `top_k=12` retrieves nearly all of them, so `retrieval_recall` is
   saturated at 1.000. The tests confirm the right chunks come back; they **cannot** distinguish good
   ranking from indiscriminate ranking. Anyone quoting a retrieval figure must report it as a
   ceiling. Fixing this needs a much larger synthetic corpus, not a new test.

7. **Semantic faithfulness of citations is not tested, because it is not implemented.**
   `test_validators.py` covers exactly what `validators.py` claims: that quoted characters exist in
   the cited chunk. A quote that is real but rearranged to assert the opposite of its source scores
   as VERIFIED, which the module's own docstring measures and states. No test asserts otherwise,
   and none should.

8. **Concurrency is untested.** Two browser tabs reviewing the same assessment, or two processes
   writing at once, are not covered. The service layer appends rather than updates, so a concurrent
   review produces two `HumanReview` rows — what that does to the queue and the agreement figures is
   not asserted anywhere.

9. **Performance is not tested.** There is no benchmark, no load test and no assertion about
   latency. The timing figures the harness records compare Python executing a rule engine and are
   not inference measurements.

10. **One known defect is deliberately not encoded as a test.** `service.dashboard_stats()` does not
    exclude evaluation projects from `evidence_files`, `evidence_chunks` and `controls_in_scope`.
    The suite is **silent** on it rather than wrong about it:
    `test_service.py::test_evaluation_assessments_are_excluded_from_operational_figures` asserts the
    assessment-level exclusion (which works) and stops there. When the defect is fixed, that test is
    where the assertion belongs.

11. **The `slow` marker partitions less than its name suggests.** See
    [About the `slow` marker](#about-the-slow-marker): four tests carry it and deselecting them saves
    about a quarter of the runtime; most of the cost is fixture setup no marker can skip.

---

*Companion documents:* [`ARCHITECTURE.md`](ARCHITECTURE.md) ·
[`DATABASE_SCHEMA.md`](DATABASE_SCHEMA.md) · [`API.md`](API.md) · [`SETUP.md`](SETUP.md)
