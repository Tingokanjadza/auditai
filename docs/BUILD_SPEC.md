# BUILD SPEC — llm-it-auditor (binding contract for all contributors)

Project root: `/Users/tingo/Documents/IT AUDIT TOOL`
Python: **3.9.6** (system). Virtualenv at `.venv` — run everything as `.venv/bin/python`.

## HARD RULES

1. **Python 3.9 syntax only.** No `list[str]` / `dict[str,int]` / `X | None` in *runtime-evaluated*
   positions. Use `typing.List/Dict/Optional/Tuple/Any/Sequence`. No `match`. Every new module starts
   with `from __future__ import annotations`. SQLAlchemy `Mapped[...]` annotations must use
   `typing` generics (already done in models.py).
2. **Never invent an API.** Read the foundation files before writing; import what exists, do not guess.
3. **No secrets in code.** Everything configurable comes from `app.config.get_settings()`.
4. **No network at import time.** The whole app must run fully offline with `LLM_PROVIDER=mock`.
5. **Only touch the files assigned to you.** Do not edit files owned by another task. If you need a
   change in someone else's file, note it in your return value instead.
6. **Synthetic data only.** Never generate content implying it is real organisational data.
7. Docstrings explain *why*, comments explain non-obvious *why*. Match the existing house style
   (see `app/schemas/assessment.py`). No decorative banners beyond the existing `# ---- name` style.
8. Verify your own work: run `.venv/bin/python -c "import <your module>"` and any snippet needed to
   prove it behaves. Fix what you break. Report honestly if something does not work.

## FOUNDATION (already written and tested — READ THESE FIRST, do not modify)

- `app/config.py` — `Settings`, `get_settings()`, `reload_settings()`, `BASE_DIR`,
  `SUPPORTED_UPLOAD_EXTENSIONS`. Note fields: `llm_provider`, `llm_model`, `llm_api_key`,
  `llm_base_url`, `embedding_provider`, `embedding_dim`, `retrieval_strategy`, `retrieval_top_k`,
  `retrieval_candidate_k`, `chunk_size`, `chunk_overlap`, `table_rows_per_chunk`,
  `max_evidence_chars`, `raw_evidence_char_budget`, `citation_match_threshold`,
  `mock_hallucination_rate`, `mock_seed`, `upload_dir`, `report_dir`, `synthetic_dir`,
  `evaluation_output_dir`, `force_human_review`, `default_auditor_name`.
- `app/schemas/enums.py` — `AssessmentStatus`, `RiskLevel`, `EvidenceSufficiency`, `ConfidenceLevel`,
  `HumanDecision`, `ProjectStatus`, `EvidenceType`, `ParseStatus`, `SourceType`, `ExperimentMode`,
  `RetrievalStrategy`, `CitationVerdict`, `ActivityAction`, plus `DEFICIENCY_STATUSES`,
  `HIGH_RISK_LEVELS`, `RISK_ORDER`. Every enum has `.values()` and tolerant `.coerce(raw, default)`.
- `app/schemas/assessment.py` — `EvidenceCitation`, `AssessmentOutput`, `assessment_json_schema()`,
  `ASSESSMENT_JSON_TEMPLATE`, `SufficiencyCheck` + `SUFFICIENCY_JSON_TEMPLATE`,
  `SelfCritique` + `SELF_CRITIQUE_JSON_TEMPLATE`, `NO_EVIDENCE_SENTINEL`.
- `app/database/base.py` — `Base`, `engine`, `SessionLocal`, `session_scope()`, `get_db()`,
  `init_db()`, `drop_all()`, `utcnow()`.
- `app/database/models.py` — 12 tables. Read it for exact column names.
- `app/llm/base.py` — `LLMProvider` (ABC: `complete(system, user, json_schema=None, temperature=0.0,
  max_tokens=2000, context=None) -> LLMResponse`, `embed`, `is_available`, `health`), `LLMResponse`,
  `LLMCallContext`, `LLMError`, `LLMNotConfiguredError`, `extract_json()`, `estimate_tokens()`.
- `app/rag/base.py` — `SourceLocator` (has `.render()`, `.to_dict()`), `ParsedChunk`,
  `RetrievedChunk`, `RetrievalResult`, `EmbeddingProvider` (ABC), `BaseRetriever` (ABC:
  `retrieve(queries, project_id, top_k, evidence_file_ids=None, min_score=0.0) -> RetrievalResult`).

## MODULE OWNERSHIP AND INTERFACES (the contract between tasks)

### `app/evidence/` — parsing
- `parsers.py`: `parse_file(path: str, filename: str = "") -> ParseResult` where
  `@dataclass ParseResult: chunks: List[ParsedChunk]; page_count: int; row_count: int;
  char_count: int; extra_metadata: Dict[str, Any]; warnings: List[str]`.
  Dispatch on extension: `.pdf` (pypdf, per page + section detection), `.docx` (python-docx,
  paragraphs with heading tracking + tables), `.csv`/`.xlsx`/`.xls` (pandas/openpyxl; **row numbers
  must be 1-based spreadsheet row numbers where row 1 = header**), `.txt`/`.md`/`.json`.
  Every chunk's `locator` must be fully populated. Tabular files additionally emit ONE
  `TABLE_SUMMARY` chunk per sheet (shape, columns, per-column value counts for low-cardinality
  columns) — this is what lets a model reason about a 100-row population without seeing all rows.
- `storage.py`: `save_upload(data: bytes, filename: str, project_id: int) -> StoredFile`
  (`@dataclass StoredFile: stored_path, sha256, size_bytes, extension, content_type`),
  `sha256_bytes`, `read_stored`, `delete_stored`. Files go under `settings.upload_dir/project_<id>/`.
- `service.py`: `ingest_file(session, project_id, data, filename, evidence_type, description,
  uploaded_by, is_synthetic=False) -> EvidenceFile` (saves, parses, persists `EvidenceChunk` rows
  with populated locator columns + `locator_text`, embeds via `app.rag.indexer.index_evidence_file`,
  updates counts and `parse_status`, writes an `ActivityLog` row). Also
  `delete_evidence(session, evidence_file_id)`, `get_chunk_context(session, chunk_id, window=1)`.

### `app/rag/` — retrieval
- `embeddings.py`: `LocalHashingEmbedding(EmbeddingProvider)` — sklearn `HashingVectorizer`
  (stateless, deterministic, `n_features=settings.embedding_dim`, L2-normalised, no network) and
  `OpenAIEmbedding(EmbeddingProvider)`. `get_embedding_provider() -> EmbeddingProvider` reads config
  and **falls back to local with a warning if the remote one is unavailable**.
  Helpers `encode_vector(List[float]) -> bytes` / `decode_vector(bytes, dim) -> np.ndarray` using
  float32 (must round-trip with `EvidenceChunk.embedding` / `.embedding_dim`).
- `indexer.py`: `index_evidence_file(session, evidence_file_id) -> int`,
  `reindex_project(session, project_id) -> int`.
- `query_builder.py`: `build_queries(control) -> List[str]` — turns a `Control` ORM row into several
  targeted retrieval queries (name+objective, each assessment criterion, expected-evidence terms,
  keywords). Must work with either a `Control` ORM object or a plain dict.
- `retriever.py`: `KeywordRetriever` (BM25, implemented directly — no extra dependency),
  `VectorRetriever` (cosine over stored vectors), `HybridRetriever` (reciprocal-rank fusion of both,
  `k=60`), and `get_retriever(session, strategy=None) -> BaseRetriever`. All must populate
  `RetrievedChunk.locator` from the stored chunk columns, set `keyword_score`/`vector_score`/`rank`,
  and dedupe by `chunk_id`. Retrieval must be diversity-aware: cap chunks per evidence file at
  `max(2, top_k // 2)` so one large file cannot crowd out a policy document.

### `app/llm/` — providers
- `mock_provider.py`: `MockLLMProvider(LLMProvider)`. **This is the offline demo brain and it must be
  genuinely good.** It never calls the network. Given `LLMCallContext` with the control and the
  retrieved chunks it must: read the actual chunk text, detect exception rows / configuration values
  / missing artefacts, choose a defensible `AssessmentStatus`, and emit **real citations pointing at
  real `chunk_id`s with verbatim quoted text copied out of those chunks**. It must return
  `INSUFFICIENT_EVIDENCE` when the expected evidence is genuinely absent (the DATASET-005 case).
  It handles three purposes (`context.purpose`): `"assessment"`, `"sufficiency"`, `"critique"`,
  returning the matching JSON template shape. Deterministic given `settings.mock_seed`.
  When `settings.mock_hallucination_rate > 0` it deliberately injects a fabricated citation or an
  unsupported numeric claim at that rate — a research lever for exercising the validator. Document
  clearly that the mock is a *deterministic rule-based stand-in*, not a language model, and that
  results obtained with it measure the pipeline, not model quality.
- `openai_provider.py`: `OpenAICompatibleProvider(LLMProvider)` using the `openai` SDK with
  `base_url` support, JSON-schema structured output when `settings.llm_use_json_mode`, graceful
  degradation to `response_format={"type":"json_object"}` then to plain text, retries with backoff,
  and real token accounting from `usage`.
- `factory.py`: `get_llm_provider(name=None, model=None) -> LLMProvider`,
  `available_providers() -> List[str]`, `provider_health() -> Dict[str, Any]`.
  Falls back to the mock with a clear warning if a real provider is selected but unconfigured.

### `app/audit/` — the audit brain
- `risk.py`: prototype risk model. `@dataclass RiskFactors: impact, likelihood, privilege_level,
  data_sensitivity, control_failure_severity` (each 1–5, with an `exception_ratio` input);
  `score_risk(control, status, exception_ratio=None, model_risk_level=None) -> RiskAssessment`
  (`@dataclass RiskAssessment: level: RiskLevel; score: float (0–100); factors: Dict[str, Any];
  rationale: str; band_thresholds`). Must be labelled everywhere as
  **"Prototype research risk scoring model — not an official industry framework."**
- `validators.py`: `validate_citations(output: AssessmentOutput, retrieval: RetrievalResult) ->
  ValidationReport` — for each citation resolve `chunk_id` in the retrieved set, then fuzzy-match
  `quoted_text` against that chunk (normalised token overlap / longest-common-substring ratio ≥
  `settings.citation_match_threshold` → VERIFIED, ≥ half → PARTIAL, chunk not in retrieved set or
  no textual support → FABRICATED). Also `detect_unsupported_claims(output, retrieval) -> List[str]`
  (numbers/percentages/counts in `assessment`+`finding`+`reasoning` that appear nowhere in the
  retrieved evidence) and `enforce_safety_rails(output, report) -> AssessmentOutput` which downgrades
  an unsupported EFFECTIVE/NOT_EFFECTIVE conclusion to INSUFFICIENT_EVIDENCE, forces
  `human_review_required=True`, and appends the `NO_EVIDENCE_SENTINEL` when nothing is cited.
  `@dataclass ValidationReport: citations: List[CitationCheck]; total, verified, partial,
  fabricated: int; grounding_rate: float; unsupported_claims: List[str]; rails_applied: List[str];
  to_dict()`.
- `prompts.py`: `SYSTEM_PROMPT` (the auditing-assistant persona and the four-way
  REQUIRES/PROVES/INFERS/HUMAN-VERIFIES rule, explicit "never invent evidence or citations",
  "you are not authorised to declare legal or regulatory compliance"), plus
  `build_assessment_prompt(control, retrieval, mode, project=None) -> str`,
  `build_raw_prompt(control, raw_texts, project=None) -> str` (Experiment A — no chunk IDs, no
  citation scaffolding, minimal control context: this is the deliberately weaker baseline),
  `build_sufficiency_prompt(...)`, `build_critique_prompt(...)`, and
  `render_evidence_block(retrieval, max_chars) -> str` which prints each chunk as
  `[chunk_id: N] SOURCE: <locator>` followed by the text.
- `engine.py`: `AssessmentEngine(session, llm=None, retriever=None, settings=None)` with
  `assess_control(project_id, control_id_or_ref, mode=ExperimentMode.C_RAG_WORKFLOW,
  persist=True, evaluation_run_id=None) -> AssessmentRunResult` and
  `assess_project(project_id, mode, control_refs=None) -> List[AssessmentRunResult]`.
  `@dataclass AssessmentRunResult: assessment_id: Optional[int]; output: AssessmentOutput;
  retrieval: RetrievalResult; validation: ValidationReport; risk: RiskAssessment; latency_ms;
  llm_calls; prompt_tokens; completion_tokens; raw_response; prompt_snapshot; error;
  sufficiency: Optional[SufficiencyCheck]; critique: Optional[SelfCritique]; mode`.
  Mode behaviour: **A** = no retrieval, raw truncated file text, weak prompt, no citation
  scaffolding, no validator rails (record what the model did, unedited — this is the baseline the
  research is measuring *against*); **B** = retrieval + full control + structured schema + citation
  validation recorded; **C** = sufficiency pre-check → assessment → self-critique → citation
  validation → safety rails → prototype risk scoring → persisted with `human_review_required=True`.
  Persist to `Assessment` + `AssessmentCitation` (+ `ActivityLog`).
- `report.py`: `generate_report(session, project_id, generated_by, fmt="markdown") -> AuditReport`
  producing the 10 mandated sections, explicitly separating **AI-generated assessment** from
  **Final auditor assessment**, with an evidence-reference appendix and an explicit Limitations
  section. Support `fmt in {"markdown","html"}`; write the file under `settings.report_dir`.
- `service.py`: the shared, UI-agnostic data-access layer used by BOTH FastAPI and Streamlit.
  Project CRUD, control CRUD/library listing, scoping controls to projects, evidence listing,
  assessment listing/filtering, `record_human_review(...)` (computes `agreed_with_ai_status` /
  `agreed_with_ai_risk`), `dashboard_stats(project_id=None) -> Dict[str, Any]` (total controls,
  assessed, effective, potential deficiencies, insufficient evidence, high-risk findings, pending
  human reviews, agreement rate), and `log_activity(...)`.

### `app/evaluation/`
- `datasets.py`: the five mandated synthetic datasets as declarative `SyntheticDataset` records with
  `dataset_id`, `name`, `control_ref`, `expected_status`, `expected_risk`, `files` (generated),
  `key_evidence_markers` (strings that a correct retrieval must surface — used for the retrieval /
  traceability metric), `notes`, `rationale`.
  DATASET-001 MFA 100 privileged accounts, 90 enabled / 10 disabled → POTENTIAL_DEFICIENCY.
  DATASET-002 patching 100 endpoints, 95 compliant / 5 missing critical patches → POTENTIAL_DEFICIENCY.
  DATASET-003 password policy requires min length 14, configuration shows 8 → NOT_EFFECTIVE.
  DATASET-004 change management 100 tickets, 95 approved / 5 unapproved → POTENTIAL_DEFICIENCY.
  DATASET-005 MFA policy + privileged user list **with no MFA column at all** → INSUFFICIENT_EVIDENCE.
  Also include a **DATASET-006 clean control** (all 100 accounts compliant → EFFECTIVE) so the
  confusion matrix has a true-negative class; document it as an addition beyond the mandated five.
- `generator.py`: deterministic generators writing real .csv/.xlsx/.txt/.docx/.pdf files into
  `settings.synthetic_dir`. Fixed seed. Never produce anything resembling real personal data
  (use `svc-account-014`, `p.adeyemi`-style synthetic handles and state clearly they are fabricated).
- `metrics.py`: `compute_metrics(results: List[EvaluationResult-like]) -> Dict[str, Any]` with
  per-class + macro precision/recall/F1, accuracy, confusion matrix, and — defined explicitly for the
  binary "deficiency detected" framing — FPR/FNR. Plus citation/traceability rate, hallucination
  rate, unsupported-claim rate, missing-evidence-detection rate, mean/median latency,
  human–AI agreement (raw + Cohen's kappa). Every metric must carry a docstring stating its exact
  definition; the report must state n and warn when n is too small for the statistic to mean much.
- `runner.py`: `run_experiment(session, mode, dataset_ids=None, llm=None, run_name="") ->
  EvaluationRun`; creates an isolated evaluation project per dataset, ingests generated files,
  assesses, scores against ground truth, persists `EvaluationRun`/`EvaluationResult`, cleans up.
  `run_all_experiments(session, modes=...)` compares A/B/C. Also
  `compare_runs(session, run_ids) -> pandas.DataFrame`.

### `app/api/`
- `schemas/api.py`: request/response Pydantic models.
- `main.py`: FastAPI app, CORS, `/health`, `/api/v1` routers, startup `init_db()` + seeding,
  OpenAPI metadata and tags, a global exception handler.
- `routers/`: `projects.py`, `controls.py`, `evidence.py` (upload via `UploadFile`),
  `assessments.py`, `reviews.py`, `dashboard.py`, `reports.py`, `evaluation.py`, `settings.py`.
  All routers delegate to `app.audit.service` — no business logic in routers.

### `app/frontend/` — Streamlit
- **Single-process by default**: `app/frontend/data_access.py` exposes one facade used by all pages.
  It calls `app.audit.service` in-process by default; when `settings`-driven `USE_API` env is true it
  calls the FastAPI backend over HTTP instead (`api_client.py`). The app must be fully usable with
  `streamlit run` alone, with no backend running.
- `streamlit_app.py` (entry, nav, session state), `theme.py` (dark security-console CSS,
  status/risk colour tokens, metric cards, badges), `components.py` (status badge, risk badge,
  evidence viewer/expander, citation card, AI-vs-human diff, empty states).
- `pages/` — 10 pages: Dashboard, Audit Projects, Controls, Evidence, Assessments, Findings,
  Human Review, Evaluation, Reports, Settings. Professional audit-console look, NOT a chat UI.
  The Assessment detail view must show, side by side: the control REQUIREMENT, the cited EVIDENCE
  (expandable to the real chunk text with its locator), the AI INFERENCES, and what needs HUMAN
  VERIFICATION. Every AI output must carry a visible "AI-generated — requires auditor review" banner.

### `tests/`
`conftest.py` (temp SQLite per test session, seeded controls, sample evidence fixtures) plus
`test_config.py`, `test_enums_schemas.py`, `test_parsers.py`, `test_storage.py`, `test_retrieval.py`,
`test_llm_providers.py`, `test_validators.py`, `test_risk.py`, `test_engine.py`, `test_service.py`,
`test_review.py`, `test_report.py`, `test_datasets.py`, `test_metrics.py`, `test_evaluation_runner.py`,
`test_api.py` (FastAPI `TestClient`). Tests must be hermetic: no network, no writes outside tmp.

## COMMANDS
- Run tests: `.venv/bin/python -m pytest tests -q`
- API: `.venv/bin/python -m uvicorn app.api.main:app --port 8000`
- UI: `.venv/bin/python -m streamlit run app/frontend/streamlit_app.py`
- Both: `.venv/bin/python run.py`
