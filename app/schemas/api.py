"""Request and response models for the HTTP API.

These models exist for one reason: the OpenAPI document has to be true. The routers
delegate to :mod:`app.audit.service`, which returns plain dictionaries, and FastAPI
filters every response through the declared ``response_model`` - so a field this module
forgets to declare is a field the API silently stops returning. Each response model here
therefore mirrors, key for key, the dictionary produced by the service helper that feeds
it (``project_to_dict``, ``control_to_dict``, ``evidence_metadata``,
``assessment_to_dict``, ``citation_to_dict``, ``review_to_dict``, ``dashboard_stats``,
``get_chunk_context``). Where a helper's key is absent from a model, the payload loses
it; where a model invents a key, the schema lies. Both are checked by the API tests.

Two conventions worth stating:

* **Timestamps are strings.** ``Base.to_dict`` renders every ``datetime`` with
  ``.isoformat()`` before it ever reaches this layer, so the response models type those
  fields as ``str`` rather than ``datetime``. Declaring them as ``datetime`` would be a
  second, redundant parse of a value that is already ISO-8601 text.
* **JSON columns stay ``Dict[str, Any]``.** ``validation_report``, ``risk_factors``,
  ``extra_metadata``, ``summary_stats`` and ``config`` are open-ended by design - they
  carry whatever the producing module recorded. Pinning a shape here would either drop
  keys or go stale; the docstring points at the module that owns the shape instead.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.enums import (
    AssessmentStatus,
    CitationVerdict,
    ConfidenceLevel,
    EvidenceProvenance,
    EvidenceSufficiency,
    EvidenceType,
    ExperimentMode,
    HumanDecision,
    ParseStatus,
    ProjectStatus,
    RiskLevel,
)


class ApiModel(BaseModel):
    """Base for every model in this module.

    ``extra="ignore"`` on requests means an unknown key is dropped rather than rejected,
    which keeps a newer client compatible with an older server. On responses it has no
    effect: FastAPI serialises by field.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


# ---- errors
class ErrorBody(ApiModel):
    """The body of the single error envelope used by every failure path."""

    type: str = Field(description="Exception class name, e.g. 'NotFoundError'.")
    message: str = Field(description="Human-readable explanation, safe to display.")
    status_code: int = Field(description="HTTP status code, repeated for clients that only read the body.")
    path: str = Field(default="", description="Request path that produced the error.")
    detail: Optional[Any] = Field(
        default=None,
        description="Structured detail when there is any (e.g. request validation errors).",
    )


class ErrorResponse(ApiModel):
    """Every 4xx and 5xx response from this API has exactly this shape."""

    error: ErrorBody


# ---- health and settings
class ProviderSummary(ApiModel):
    """Mirrors ``Settings.provider_summary()``. ``llm_api_key`` is always masked."""

    llm_provider: str
    llm_model: str
    llm_base_url: str
    llm_api_key: str = Field(
        description="Masked fingerprint such as 'sk-...abcd (len 51)', or '(not set)'. Never the key itself."
    )
    llm_configured: bool
    embedding_provider: str
    embedding_model: str
    retrieval_strategy: str
    retrieval_top_k: int
    mock_hallucination_rate: float
    #: Present since the Claude provider was added; older API builds omit them.
    anthropic_model: str = ""
    anthropic_api_key: str = Field(default="(not set)", description="Masked fingerprint or '(not set)'. Never the key itself.")


class ProviderHealth(ApiModel):
    """Mirrors ``app.llm.factory.provider_health()`` - configuration only, no network call."""

    configured_provider: str
    active_provider: str
    active_model: str
    fell_back_to_mock: bool = Field(
        description="True when a real provider was selected but is unconfigured, so the offline mock is answering."
    )
    fallback_reason: str = ""
    available_providers: List[str] = Field(default_factory=list)
    providers: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict,
        description="Per-provider health as returned by each provider's own health(); see app.llm.base.",
    )
    settings: ProviderSummary


class HealthResponse(ApiModel):
    status: str = Field(description="'ok' when the process is serving and the database answered.")
    app_name: str
    app_version: str
    environment: str
    database_ok: bool
    database_error: str = ""
    authentication: str = Field(
        description="Always 'none' - this prototype has no authentication and must not be exposed off localhost."
    )
    provider: ProviderHealth
    checked_at: str


class LimitsSummary(ApiModel):
    """Operational limits a client needs before it uploads or waits on a run."""

    max_upload_mb: int
    max_upload_bytes: int
    supported_upload_extensions: List[str]
    max_evidence_chars: int
    api_request_timeout_seconds: int


class SettingsResponse(ApiModel):
    """Read-only configuration view. Secrets are masked; no filesystem paths, no DSN."""

    app_name: str
    app_short_name: str
    app_version: str
    environment: str
    debug: bool
    provider: ProviderSummary
    limits: LimitsSummary
    force_human_review: bool
    citation_match_threshold: float
    default_auditor_name: str
    database_backend: str = Field(description="'sqlite' or the SQLAlchemy dialect name - never the full URL.")
    security_note: str


class SettingsUpdateRequest(ApiModel):
    """A proposed configuration change. Accepted for documentation, never applied."""

    changes: Dict[str, Any] = Field(
        default_factory=dict, description="Setting names and values the caller would like to apply."
    )
    reason: str = Field(default="", description="Optional note recorded in the response only.")


class SettingsUpdateResponse(ApiModel):
    applied: bool = Field(description="Always false. This endpoint is deliberately a no-op.")
    message: str
    requested_keys: List[str] = Field(default_factory=list)
    how_to_change: str


# ---- projects
class ProjectCreateRequest(ApiModel):
    name: str = Field(min_length=1, max_length=255)
    audit_area: str = Field(min_length=1, max_length=255)
    description: str = ""
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None
    auditor_name: str = Field(default="", description="Defaults to settings.default_auditor_name when empty.")
    status: Optional[ProjectStatus] = None
    scope_note: str = ""
    is_demo: bool = False
    control_refs: List[str] = Field(
        default_factory=list, description="Library controls to scope immediately, e.g. ['CONTROL-001']."
    )
    actor: str = Field(default="", description="Name recorded in the activity trail for this action.")


class ProjectUpdateRequest(ApiModel):
    """Only the fields an auditor owns. Omitted fields are left untouched."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    audit_area: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None
    auditor_name: Optional[str] = None
    status: Optional[ProjectStatus] = None
    scope_note: Optional[str] = None
    actor: str = ""


class ProjectResponse(ApiModel):
    """Mirrors ``service.project_to_dict``."""

    id: int
    name: str
    audit_area: str
    description: Optional[str] = None
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    auditor_name: str = ""
    status: ProjectStatus = ProjectStatus.PLANNING
    scope_note: Optional[str] = None
    is_demo: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    period_label: str = ""
    controls_in_scope: int = 0
    evidence_files: int = 0
    assessments: int = 0
    control_refs: List[str] = Field(default_factory=list)


class DeleteResponse(ApiModel):
    deleted: bool
    id: int
    message: str = ""


class ScopeControlsRequest(ApiModel):
    control_refs: List[str] = Field(
        min_length=1, description="Control references or numeric ids, e.g. ['CONTROL-001', 'CONTROL-004']."
    )
    scope_note: str = ""
    actor: str = ""


class ScopeResponse(ApiModel):
    """Result of a scope/unscope action, with the resulting scope so a client can refresh."""

    project_id: int
    changed: bool
    scoped_control_refs: List[str] = Field(default_factory=list)
    controls_in_scope: int = 0
    message: str = ""


# ---- control library
class ControlCreateRequest(ApiModel):
    control_id: str = Field(min_length=1, max_length=64, description="Unique reference, e.g. 'CONTROL-015'.")
    name: str = Field(min_length=1, max_length=255)
    objective: str = ""
    description: str = ""
    risk_addressed: str = ""
    expected_evidence: List[str] = Field(default_factory=list)
    assessment_criteria: List[str] = Field(default_factory=list)
    framework_refs: List[str] = Field(
        default_factory=list, description="Informative cross-references only; this prototype asserts no official mapping."
    )
    category: str = "General"
    control_type: str = "Preventive"
    control_frequency: str = "Continuous"
    inherent_risk: RiskLevel = RiskLevel.MEDIUM
    privilege_level: int = Field(default=3, ge=1, le=5)
    data_sensitivity: int = Field(default=3, ge=1, le=5)
    retrieval_keywords: List[str] = Field(default_factory=list)
    is_active: bool = True
    actor: str = ""


class ControlUpdateRequest(ApiModel):
    """``control_id`` is absent on purpose: it is the citation key printed in every
    assessment and report, so renaming it would orphan history."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    objective: Optional[str] = None
    description: Optional[str] = None
    risk_addressed: Optional[str] = None
    expected_evidence: Optional[List[str]] = None
    assessment_criteria: Optional[List[str]] = None
    framework_refs: Optional[List[str]] = None
    category: Optional[str] = None
    control_type: Optional[str] = None
    control_frequency: Optional[str] = None
    inherent_risk: Optional[RiskLevel] = None
    privilege_level: Optional[int] = Field(default=None, ge=1, le=5)
    data_sensitivity: Optional[int] = Field(default=None, ge=1, le=5)
    retrieval_keywords: Optional[List[str]] = None
    is_active: Optional[bool] = None
    actor: str = ""


class ControlResponse(ApiModel):
    """Mirrors ``service.control_to_dict``."""

    id: int
    control_id: str
    name: str
    objective: str = ""
    description: str = ""
    risk_addressed: str = ""
    expected_evidence: List[str] = Field(default_factory=list)
    assessment_criteria: List[str] = Field(default_factory=list)
    framework_refs: List[str] = Field(default_factory=list)
    category: str = "General"
    control_type: str = "Preventive"
    control_frequency: str = "Continuous"
    inherent_risk: RiskLevel = RiskLevel.MEDIUM
    privilege_level: int = 3
    data_sensitivity: int = 3
    retrieval_keywords: List[str] = Field(default_factory=list)
    is_active: bool = True
    source: str = ""
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    scoped_project_count: int = 0


# ---- evidence
class EvidenceResponse(ApiModel):
    """Mirrors ``service.evidence_to_dict``.

    ``stored_path`` is deliberately absent: the server's filesystem layout is not
    provenance, and the SHA-256 is what an auditor needs to prove integrity.
    """

    id: int
    project_id: int
    filename: str
    extension: str = ""
    content_type: str = ""
    size_bytes: int = 0
    sha256: str = ""
    evidence_type: EvidenceType = EvidenceType.OTHER
    description: Optional[str] = None
    uploaded_by: str = ""
    uploaded_at: Optional[str] = None
    parse_status: ParseStatus = ParseStatus.PENDING
    parse_error: Optional[str] = None
    page_count: int = 0
    row_count: int = 0
    chunk_count: int = 0
    char_count: int = 0
    extra_metadata: Dict[str, Any] = Field(
        default_factory=dict, description="Parser extras: sheet names, headers, warnings, indexed_chunks."
    )
    is_synthetic: bool = False
    provenance: EvidenceProvenance = Field(
        default=EvidenceProvenance.SYNTHETIC,
        description=(
            "Where the artefact came from: SYNTHETIC (generated test data), HISTORICAL_PUBLIC "
            "(a reconstruction of a publicly documented failure pattern) or ORGANISATIONAL "
            "(real evidence from an audited entity). Independent of evidence_type."
        ),
    )
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    sha256_short: str = ""
    size_kb: float = 0.0


class EvidenceDetailResponse(EvidenceResponse):
    """Mirrors ``service.evidence_metadata`` - the file record plus what makes it citable."""

    chunks_stored: int = 0
    chunks_embedded: int = Field(
        default=0, description="Chunks with a stored vector. Fewer than chunks_stored means vector search is partial."
    )
    chunks_by_source_type: Dict[str, int] = Field(default_factory=dict)
    distinct_pages: int = 0
    sheet_names: List[str] = Field(default_factory=list)


class SourceLocatorModel(ApiModel):
    """Mirrors ``app.rag.base.SourceLocator.to_dict`` - where a quote physically came from."""

    filename: str = ""
    source_type: str = ""
    page_number: Optional[int] = None
    sheet_name: Optional[str] = None
    row_start: Optional[int] = None
    row_end: Optional[int] = None
    row_numbers: List[int] = Field(default_factory=list)
    column_names: List[str] = Field(default_factory=list)
    section: Optional[str] = None
    paragraph_index: Optional[int] = None
    rendered: str = ""


class ChunkResponse(ApiModel):
    """One retrievable, citable unit of evidence, as rendered by ``app.evidence.service``."""

    chunk_id: int
    chunk_index: int = 0
    text: str = ""
    char_count: int = 0
    source_type: str = ""
    filename: str = ""
    locator_text: str = ""
    locator: SourceLocatorModel = Field(default_factory=SourceLocatorModel)
    extra_metadata: Dict[str, Any] = Field(default_factory=dict)


class ChunkContextResponse(ApiModel):
    """Mirrors ``app.evidence.service.get_chunk_context`` - a cited chunk plus neighbours.

    This is what makes a citation clickable: the reviewer sees the quoted chunk *and*
    what surrounded it, which is the only way to tell a fair quotation from a selective one.
    """

    found: bool
    chunk_id: int
    evidence_file_id: Optional[int] = None
    project_id: Optional[int] = None
    filename: str = ""
    evidence_type: str = ""
    chunk_index: Optional[int] = None
    locator_text: str = ""
    source_type: str = ""
    text: str = ""
    chunk: Optional[ChunkResponse] = None
    before: List[ChunkResponse] = Field(default_factory=list)
    after: List[ChunkResponse] = Field(default_factory=list)
    chunks: List[ChunkResponse] = Field(default_factory=list)
    context_text: str = ""


class EvidenceStatsResponse(ApiModel):
    """Mirrors ``service.project_evidence_stats``."""

    evidence_files: int = 0
    evidence_chunks: int = 0
    total_bytes: int = 0
    total_mb: float = 0.0


# ---- assessments
class CitationResponse(ApiModel):
    """Mirrors ``service.citation_to_dict``.

    ``chunk_resolved`` false means the model pointed at something that no longer resolves
    to a stored chunk - the signal a fabricated citation leaves behind.
    """

    id: int
    assessment_id: int
    chunk_id: Optional[int] = None
    evidence_file_id: Optional[int] = None
    order_index: int = 0
    filename: str = ""
    locator_text: str = ""
    quoted_text: str = ""
    relevance: str = ""
    supports: str = ""
    verdict: CitationVerdict = CitationVerdict.UNVERIFIED
    match_score: float = 0.0
    verification_note: str = ""
    created_at: Optional[str] = None
    chunk_text: str = ""
    chunk_resolved: bool = False


class ReviewResponse(ApiModel):
    """Mirrors ``service.review_to_dict`` - the human record, kept separate from the AI one."""

    id: int
    assessment_id: int
    reviewer_name: str = ""
    decision: HumanDecision = HumanDecision.PENDING
    final_status: str = Field(
        default="", description="Auditor's status. Empty string while the decision is still PENDING."
    )
    final_risk_level: RiskLevel = RiskLevel.NOT_RATED
    final_finding: str = ""
    final_recommendation: str = ""
    comments: str = ""
    requested_evidence: List[str] = Field(default_factory=list)
    agreed_with_ai_status: bool = False
    agreed_with_ai_risk: bool = False
    review_seconds: float = 0.0
    usefulness_rating: Optional[int] = None
    flagged_hallucination: bool = False
    hallucination_note: str = ""
    created_at: Optional[str] = None
    source: str = "Human auditor"


class AssessmentSummaryResponse(ApiModel):
    """Mirrors ``service.assessment_to_dict(include_citations=False)``.

    ``source`` and ``requires_human_review`` are constants on purpose: no payload from
    this API may present an AI assessment as a final audit decision.
    """

    id: int
    project_id: int
    control_id: int
    control_ref: str = ""
    experiment_mode: ExperimentMode = ExperimentMode.C_RAG_WORKFLOW
    status: AssessmentStatus = AssessmentStatus.INSUFFICIENT_EVIDENCE
    assessment: str = ""
    finding: str = ""
    risk: str = ""
    risk_level: RiskLevel = RiskLevel.NOT_RATED
    risk_score: float = 0.0
    risk_factors: Dict[str, Any] = Field(
        default_factory=dict, description="Component scores from the prototype risk model; see app.audit.risk."
    )
    evidence_sufficiency: EvidenceSufficiency = EvidenceSufficiency.NONE
    missing_evidence: List[str] = Field(default_factory=list)
    reasoning: str = ""
    recommendation: str = ""
    inferences: List[str] = Field(
        default_factory=list, description="What the model inferred beyond what the evidence literally shows."
    )
    human_verification_required: List[str] = Field(default_factory=list)
    confidence: ConfidenceLevel = ConfidenceLevel.LOW
    confidence_score: float = 0.0
    human_review_required: bool = True
    llm_provider: str = ""
    llm_model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    llm_calls: int = 0
    retrieval_strategy: str = ""
    retrieval_top_k: int = 0
    retrieved_chunk_ids: List[int] = Field(default_factory=list)
    retrieval_queries: List[str] = Field(default_factory=list)
    validation_report: Dict[str, Any] = Field(
        default_factory=dict, description="Citation-grounding result; see app.audit.validators.ValidationReport."
    )
    raw_response: Optional[str] = None
    prompt_snapshot: Optional[str] = None
    error: Optional[str] = None
    evaluation_run_id: Optional[int] = None
    created_at: Optional[str] = None
    control_name: str = ""
    control_category: str = ""
    citation_count: int = 0
    is_reviewed: bool = False
    source: str = Field(default="AI-generated", description="Always 'AI-generated'.")
    requires_human_review: bool = Field(default=True, description="Always true.")
    latest_review: Optional[ReviewResponse] = None


class AssessmentDetailResponse(AssessmentSummaryResponse):
    """The summary plus the citations, which is what makes a finding checkable."""

    citations: List[CitationResponse] = Field(default_factory=list)


class AssessmentRunRequest(ApiModel):
    project_id: int
    control_ref: str = Field(description="Control reference ('CONTROL-001') or numeric library id.")
    mode: ExperimentMode = Field(
        default=ExperimentMode.C_RAG_WORKFLOW,
        description=(
            "A_RAW_LLM: no retrieval, raw truncated evidence, no citation scaffolding (the weak baseline). "
            "B_RAG: retrieval + full control + structured output. "
            "C_RAG_WORKFLOW: sufficiency pre-check, assessment, self-critique, citation validation, safety rails."
        ),
    )
    persist: bool = Field(default=True, description="False runs the pipeline without writing an Assessment row.")


class RetrievalSummary(ApiModel):
    """The retrieval half of ``AssessmentRunResult.to_dict``."""

    strategy: str = ""
    chunk_ids: List[int] = Field(default_factory=list)
    queries: List[str] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)


class RunTelemetry(ApiModel):
    """Mirrors ``app.audit.engine.AssessmentRunResult.to_dict`` - what the run actually did."""

    assessment_id: Optional[int] = None
    control_ref: str = ""
    project_id: Optional[int] = None
    mode: ExperimentMode = ExperimentMode.C_RAG_WORKFLOW
    status: AssessmentStatus = AssessmentStatus.INSUFFICIENT_EVIDENCE
    risk_level: RiskLevel = RiskLevel.NOT_RATED
    risk_score: float = 0.0
    latency_ms: int = 0
    retrieval_ms: int = 0
    llm_ms: int = 0
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    rails_enforced: bool = False
    error: str = ""
    retrieval: RetrievalSummary = Field(default_factory=RetrievalSummary)
    validation: Dict[str, Any] = Field(
        default_factory=dict, description="See app.audit.validators.ValidationReport.to_dict."
    )
    output: Dict[str, Any] = Field(
        default_factory=dict, description="The structured model answer; see app.schemas.assessment.AssessmentOutput."
    )
    steps: List[Dict[str, Any]] = Field(
        default_factory=list, description="Per-step timing and token counts; see app.audit.engine.StepTelemetry."
    )


class AssessmentRunResponse(ApiModel):
    """What a synchronous assessment run returns: the stored record and the telemetry."""

    persisted: bool
    assessment: Optional[AssessmentDetailResponse] = Field(
        default=None, description="The stored assessment. Null when persist=false was requested."
    )
    run: RunTelemetry
    notice: str = Field(
        description="Standing disclaimer: this output is AI-generated and is not an audit conclusion."
    )


# ---- human review
class ReviewCreateRequest(ApiModel):
    """An auditor's decision on one AI assessment.

    Omitting ``final_status`` means "the reviewer left the conclusion alone" and carries
    the AI status over - except for MORE_EVIDENCE_REQUESTED, which defaults to
    INSUFFICIENT_EVIDENCE because asking for more evidence is not a conclusion.
    """

    assessment_id: int
    reviewer_name: str = ""
    decision: HumanDecision
    final_status: Optional[AssessmentStatus] = None
    final_risk_level: Optional[RiskLevel] = None
    final_finding: str = ""
    final_recommendation: str = ""
    comments: str = ""
    requested_evidence: List[str] = Field(default_factory=list)
    review_seconds: float = Field(default=0.0, ge=0.0, description="Wall-clock seconds spent reviewing.")
    usefulness_rating: Optional[int] = Field(default=None, ge=1, le=5)
    flagged_hallucination: bool = False
    hallucination_note: str = ""


# ---- dashboard
class DashboardStatsResponse(ApiModel):
    """Mirrors ``service.dashboard_stats``, including its ``definitions`` block.

    The definitions travel with the figures so a reader can check what each number
    counted rather than having to guess.
    """

    project_id: Optional[int] = None
    generated_at: str = ""
    total_controls: int = 0
    controls_in_scope: int = 0
    controls_assessed: int = 0
    effective: int = 0
    potential_deficiencies: int = 0
    not_effective: int = 0
    insufficient_evidence: int = 0
    not_applicable: int = 0
    high_risk_findings: int = 0
    pending_human_reviews: int = 0
    completed_reviews: int = 0
    human_ai_agreement_rate: float = 0.0
    human_ai_risk_agreement_rate: float = 0.0
    evidence_files: int = 0
    evidence_chunks: int = 0
    avg_latency_ms: float = 0.0
    citation_grounding_rate: float = 0.0
    citations_total: int = 0
    citations_verified: int = 0
    assessments_counted: int = 0
    flagged_hallucinations: int = 0
    definitions: Dict[str, str] = Field(default_factory=dict)


class BreakdownResponse(ApiModel):
    """A label -> count map plus the basis it was counted on."""

    project_id: Optional[int] = None
    counts: Dict[str, int] = Field(default_factory=dict)
    basis: str = ""


class ActivityResponse(ApiModel):
    """One row of the append-only activity trail."""

    id: int
    entity_type: str = ""
    entity_id: Optional[int] = None
    project_id: Optional[int] = None
    action: str = ""
    actor: str = ""
    actor_type: str = Field(default="HUMAN", description="'AI' or 'HUMAN' - makes provenance queryable.")
    details: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None


# ---- reports
class ReportGenerateRequest(ApiModel):
    project_id: int
    format: str = Field(default="markdown", description="'markdown' or 'html'.")
    title: str = ""
    generated_by: str = ""
    include_evaluation: bool = Field(
        default=False,
        description="Include assessments produced by the evaluation harness. Off by default: they are research runs, not fieldwork.",
    )


class ReportSummaryResponse(ApiModel):
    """A generated report without its body, for listings.

    ``stored_path`` is withheld for the same reason as on evidence: the server's
    filesystem layout is not part of the audit trail. Use the download endpoint.
    """

    id: int
    project_id: int
    title: str = ""
    format: str = "markdown"
    generated_by: str = ""
    summary_stats: Dict[str, Any] = Field(
        default_factory=dict, description="Counts recorded at generation time; see app.audit.report.ReportData."
    )
    generated_at: Optional[str] = None
    content_chars: int = 0
    has_file: bool = False
    download_url: str = ""


class ReportResponse(ReportSummaryResponse):
    content: str = Field(default="", description="The rendered report, in the declared format.")


# ---- evaluation
class DatasetFileResponse(ApiModel):
    filename: str
    extension: str = ""
    evidence_type: str = ""
    role: str = ""
    description: str = ""


class DatasetResponse(ApiModel):
    """Mirrors ``app.evaluation.datasets.SyntheticDataset.to_dict``. Entirely synthetic data."""

    dataset_id: str
    name: str
    control_ref: str
    expected_status: AssessmentStatus
    expected_risk: RiskLevel
    expected_finding: str = ""
    expected_missing_evidence: bool = False
    mandated: bool = True
    files: List[DatasetFileResponse] = Field(default_factory=list)
    file_names: List[str] = Field(default_factory=list)
    extensions: List[str] = Field(default_factory=list)
    key_evidence_markers: List[str] = Field(default_factory=list)
    population: Dict[str, Any] = Field(default_factory=dict)
    exception_rows: List[int] = Field(default_factory=list)
    exception_ratio: Optional[float] = None
    notes: str = ""
    rationale: str = ""
    seed: int = 0


class EvaluationRunRequest(ApiModel):
    mode: ExperimentMode = ExperimentMode.C_RAG_WORKFLOW
    dataset_ids: Optional[List[str]] = Field(
        default=None, description="Subset of dataset ids; null runs the whole suite."
    )
    run_name: str = ""


class EvaluationResultResponse(ApiModel):
    """One (dataset, control) prediction scored against a known ground truth."""

    id: int
    run_id: int
    assessment_id: Optional[int] = None
    dataset_id: str = ""
    dataset_name: str = ""
    control_ref: str = ""
    expected_status: str = ""
    predicted_status: str = ""
    status_correct: bool = False
    expected_risk: str = ""
    predicted_risk: str = ""
    expected_evidence_hits: int = 0
    expected_evidence_total: int = 0
    citation_count: int = 0
    verified_citation_count: int = 0
    fabricated_citation_count: int = 0
    unsupported_claim_count: int = 0
    hallucination_detected: bool = False
    declared_missing_evidence: bool = False
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: Optional[str] = None
    detail: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None


class EvaluationRunResponse(ApiModel):
    """An experiment run. ``metrics`` is produced by ``app.evaluation.metrics``."""

    id: int
    name: str = ""
    experiment_mode: str = ""
    llm_provider: str = ""
    llm_model: str = ""
    retrieval_strategy: str = ""
    dataset_ids: List[str] = Field(default_factory=list)
    config: Dict[str, Any] = Field(default_factory=dict)
    metrics: Dict[str, Any] = Field(default_factory=dict)
    notes: str = ""
    status: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_seconds: float = 0.0
    result_count: int = 0


class EvaluationRunDetailResponse(EvaluationRunResponse):
    results: List[EvaluationResultResponse] = Field(default_factory=list)


class EvaluationCompareResponse(ApiModel):
    """Side-by-side comparison of completed runs (typically modes A, B and C)."""

    run_ids: List[int] = Field(default_factory=list)
    rows: List[Dict[str, Any]] = Field(default_factory=list)
    source: str = Field(description="Which module produced the rows, so a reader knows what was computed.")
    warning: str = Field(
        default="",
        description="Set when the sample is too small for the statistics to carry weight.",
    )


__all__ = [
    "ActivityResponse",
    "ApiModel",
    "AssessmentDetailResponse",
    "AssessmentRunRequest",
    "AssessmentRunResponse",
    "AssessmentSummaryResponse",
    "BreakdownResponse",
    "ChunkContextResponse",
    "ChunkResponse",
    "CitationResponse",
    "ControlCreateRequest",
    "ControlResponse",
    "ControlUpdateRequest",
    "DashboardStatsResponse",
    "DatasetFileResponse",
    "DatasetResponse",
    "DeleteResponse",
    "ErrorBody",
    "ErrorResponse",
    "EvaluationCompareResponse",
    "EvaluationResultResponse",
    "EvaluationRunDetailResponse",
    "EvaluationRunRequest",
    "EvaluationRunResponse",
    "EvidenceDetailResponse",
    "EvidenceResponse",
    "EvidenceStatsResponse",
    "HealthResponse",
    "LimitsSummary",
    "ProjectCreateRequest",
    "ProjectResponse",
    "ProjectUpdateRequest",
    "ProviderHealth",
    "ProviderSummary",
    "ReportGenerateRequest",
    "ReportResponse",
    "ReportSummaryResponse",
    "RetrievalSummary",
    "ReviewCreateRequest",
    "ReviewResponse",
    "RunTelemetry",
    "ScopeControlsRequest",
    "ScopeResponse",
    "SettingsResponse",
    "SettingsUpdateRequest",
    "SettingsUpdateResponse",
    "SourceLocatorModel",
]
