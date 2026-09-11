"""ORM models.

Design notes
------------
* Enum-valued columns are ``String`` so the schema is portable and so an unexpected
  model output can be stored and inspected rather than rejected at the DB layer.
* Every AI-produced artefact (``Assessment``) is separated from every human-produced
  artefact (``HumanReview``). They are never merged in place; the pair is what makes
  human/AI agreement measurable, which is the point of the research.
* Citations are first-class rows (``AssessmentCitation``) with a foreign key to the
  exact ``EvidenceChunk`` used, which is what makes a finding traceable and what makes
  a *fabricated* citation detectable (chunk_id is NULL / does not match).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, utcnow
from app.schemas.enums import (
    AssessmentStatus,
    ConfidenceLevel,
    EvidenceSufficiency,
    EvidenceType,
    ExperimentMode,
    HumanDecision,
    ParseStatus,
    ProjectStatus,
    RiskLevel,
    SourceType,
)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


# --------------------------------------------------------------------------- projects
class AuditProject(TimestampMixin, Base):
    __tablename__ = "audit_projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    audit_area: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    period_start: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    auditor_name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=ProjectStatus.PLANNING.value)
    scope_note: Mapped[Optional[str]] = mapped_column(Text)
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    scoped_controls: Mapped[List["ProjectControl"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", lazy="selectin"
    )
    evidence_files: Mapped[List["EvidenceFile"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", lazy="selectin"
    )
    assessments: Mapped[List["Assessment"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", lazy="selectin"
    )
    reports: Mapped[List["AuditReport"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def period_label(self) -> str:
        fmt = "%Y-%m-%d"
        start = self.period_start.strftime(fmt) if self.period_start else "?"
        end = self.period_end.strftime(fmt) if self.period_end else "?"
        return f"{start} to {end}"


# --------------------------------------------------------------------------- controls
class Control(TimestampMixin, Base):
    """A reusable IT control definition from the control library."""

    __tablename__ = "controls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    control_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    risk_addressed: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: List[str] - the artefacts an auditor would expect to see to test this control.
    expected_evidence: Mapped[List[str]] = mapped_column(JSON, nullable=False, default=list)
    #: List[str] - the explicit, testable criteria the evidence is judged against.
    assessment_criteria: Mapped[List[str]] = mapped_column(JSON, nullable=False, default=list)
    #: List[str] - informative cross-references (synthetic; not an official mapping).
    framework_refs: Mapped[List[str]] = mapped_column(JSON, nullable=False, default=list)
    category: Mapped[str] = mapped_column(String(120), nullable=False, default="General")
    control_type: Mapped[str] = mapped_column(String(64), nullable=False, default="Preventive")
    control_frequency: Mapped[str] = mapped_column(String(64), nullable=False, default="Continuous")
    #: Inherent risk of the area this control protects; feeds the prototype risk model.
    inherent_risk: Mapped[str] = mapped_column(String(32), nullable=False, default=RiskLevel.MEDIUM.value)
    privilege_level: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    data_sensitivity: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    #: Free-text keywords used to seed retrieval queries for this control.
    retrieval_keywords: Mapped[List[str]] = mapped_column(JSON, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="synthetic-library")

    scoped_projects: Mapped[List["ProjectControl"]] = relationship(
        back_populates="control", cascade="all, delete-orphan", lazy="selectin"
    )
    assessments: Mapped[List["Assessment"]] = relationship(back_populates="control", lazy="selectin")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Control {self.control_id} {self.name!r}>"


class ProjectControl(TimestampMixin, Base):
    """Association putting a library control in scope for a specific audit project."""

    __tablename__ = "project_controls"
    __table_args__ = (UniqueConstraint("project_id", "control_id", name="project_control"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("audit_projects.id", ondelete="CASCADE"), nullable=False, index=True)
    control_id: Mapped[int] = mapped_column(ForeignKey("controls.id", ondelete="CASCADE"), nullable=False, index=True)
    scope_note: Mapped[Optional[str]] = mapped_column(Text)

    project: Mapped[AuditProject] = relationship(back_populates="scoped_controls", lazy="joined")
    control: Mapped[Control] = relationship(back_populates="scoped_projects", lazy="joined")


# --------------------------------------------------------------------------- evidence
class EvidenceFile(TimestampMixin, Base):
    __tablename__ = "evidence_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("audit_projects.id", ondelete="CASCADE"), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    stored_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    extension: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    content_type: Mapped[str] = mapped_column(String(160), nullable=False, default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: SHA-256 of the stored bytes - the integrity anchor for the evidence trail.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    evidence_type: Mapped[str] = mapped_column(String(48), nullable=False, default=EvidenceType.OTHER.value)
    description: Mapped[Optional[str]] = mapped_column(Text)
    uploaded_by: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    parse_status: Mapped[str] = mapped_column(String(32), nullable=False, default=ParseStatus.PENDING.value)
    parse_error: Mapped[Optional[str]] = mapped_column(Text)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Parser-specific extras: sheet names, headers, column dtypes, pdf metadata…
    extra_metadata: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    #: True when the file was produced by the synthetic dataset generator.
    is_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    project: Mapped[AuditProject] = relationship(back_populates="evidence_files")
    chunks: Mapped[List["EvidenceChunk"]] = relationship(
        back_populates="evidence_file", cascade="all, delete-orphan", lazy="selectin"
    )


class EvidenceChunk(Base):
    """One retrievable, citable unit of evidence with a precise source locator.

    The locator fields are what Feature 6 (evidence traceability) stands on: every
    chunk knows the file it came from and, depending on format, its page, sheet, row
    range, columns and section heading.
    """

    __tablename__ = "evidence_chunks"
    __table_args__ = (
        Index("ix_evidence_chunks_file_idx", "evidence_file_id", "chunk_index"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    evidence_file_id: Mapped[int] = mapped_column(
        ForeignKey("evidence_files.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[int] = mapped_column(ForeignKey("audit_projects.id", ondelete="CASCADE"), nullable=False, index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default=SourceType.TEXT_BLOCK.value)
    #: Denormalised for fast citation rendering without a join.
    filename: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    page_number: Mapped[Optional[int]] = mapped_column(Integer)
    sheet_name: Mapped[Optional[str]] = mapped_column(String(255))
    row_start: Mapped[Optional[int]] = mapped_column(Integer)
    row_end: Mapped[Optional[int]] = mapped_column(Integer)
    #: Explicit source row numbers (1-based, as seen in the spreadsheet) in this chunk.
    row_numbers: Mapped[List[int]] = mapped_column(JSON, nullable=False, default=list)
    column_names: Mapped[List[str]] = mapped_column(JSON, nullable=False, default=list)
    section: Mapped[Optional[str]] = mapped_column(String(512))
    paragraph_index: Mapped[Optional[int]] = mapped_column(Integer)
    #: Human-readable one-line citation, e.g. "Privileged_Users.csv - rows 14-38".
    locator_text: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    extra_metadata: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    #: float32 vector bytes; NULL until the chunk is embedded.
    embedding: Mapped[Optional[bytes]] = mapped_column(LargeBinary)
    embedding_dim: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding_model: Mapped[Optional[str]] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    evidence_file: Mapped[EvidenceFile] = relationship(back_populates="chunks")


# ------------------------------------------------------------------------ assessments
class Assessment(Base):
    """An AI-generated control assessment. Never a final audit decision."""

    __tablename__ = "assessments"
    __table_args__ = (Index("ix_assessments_project_control", "project_id", "control_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("audit_projects.id", ondelete="CASCADE"), nullable=False, index=True)
    control_id: Mapped[int] = mapped_column(ForeignKey("controls.id", ondelete="CASCADE"), nullable=False, index=True)
    #: Denormalised business key ("CONTROL-001") for reporting and export.
    control_ref: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    experiment_mode: Mapped[str] = mapped_column(String(32), nullable=False, default=ExperimentMode.C_RAG_WORKFLOW.value)

    status: Mapped[str] = mapped_column(String(40), nullable=False, default=AssessmentStatus.INSUFFICIENT_EVIDENCE.value)
    assessment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    finding: Mapped[str] = mapped_column(Text, nullable=False, default="")
    risk: Mapped[str] = mapped_column(Text, nullable=False, default="")
    risk_level: Mapped[str] = mapped_column(String(32), nullable=False, default=RiskLevel.NOT_RATED.value)
    risk_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: Component scores from the prototype risk model, for transparency in the UI.
    risk_factors: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    evidence_sufficiency: Mapped[str] = mapped_column(
        String(32), nullable=False, default=EvidenceSufficiency.NONE.value
    )
    missing_evidence: Mapped[List[str]] = mapped_column(JSON, nullable=False, default=list)
    reasoning: Mapped[str] = mapped_column(Text, nullable=False, default="")
    recommendation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Explicitly separates what the model INFERRED from what the evidence PROVES.
    inferences: Mapped[List[str]] = mapped_column(JSON, nullable=False, default=list)
    human_verification_required: Mapped[List[str]] = mapped_column(JSON, nullable=False, default=list)
    confidence: Mapped[str] = mapped_column(String(32), nullable=False, default=ConfidenceLevel.LOW.value)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    human_review_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # ---- provenance / telemetry (research measurements) ----
    llm_provider: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    llm_model: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retrieval_strategy: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    retrieval_top_k: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retrieved_chunk_ids: Mapped[List[int]] = mapped_column(JSON, nullable=False, default=list)
    retrieval_queries: Mapped[List[str]] = mapped_column(JSON, nullable=False, default=list)
    #: Output of the citation-grounding validator (Feature 6 / hallucination metric).
    validation_report: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    #: Verbatim model output, retained so a researcher can audit the audit.
    raw_response: Mapped[Optional[str]] = mapped_column(Text)
    prompt_snapshot: Mapped[Optional[str]] = mapped_column(Text)
    error: Mapped[Optional[str]] = mapped_column(Text)
    #: Set when this assessment was produced by the evaluation harness, not an auditor.
    evaluation_run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("evaluation_runs.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    project: Mapped[AuditProject] = relationship(back_populates="assessments")
    control: Mapped[Control] = relationship(back_populates="assessments")
    citations: Mapped[List["AssessmentCitation"]] = relationship(
        back_populates="assessment",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="AssessmentCitation.order_index",
    )
    reviews: Mapped[List["HumanReview"]] = relationship(
        back_populates="assessment",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="HumanReview.created_at",
    )

    @property
    def latest_review(self) -> Optional["HumanReview"]:
        return self.reviews[-1] if self.reviews else None

    @property
    def is_reviewed(self) -> bool:
        review = self.latest_review
        return bool(review and review.decision != HumanDecision.PENDING.value)


class AssessmentCitation(Base):
    """One evidence reference attached to an AI finding, with verification verdict."""

    __tablename__ = "assessment_citations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    assessment_id: Mapped[int] = mapped_column(
        ForeignKey("assessments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: NULL means the model referred to something that could not be matched to a real
    #: chunk - i.e. a candidate fabricated citation.
    chunk_id: Mapped[Optional[int]] = mapped_column(ForeignKey("evidence_chunks.id", ondelete="SET NULL"), index=True)
    evidence_file_id: Mapped[Optional[int]] = mapped_column(ForeignKey("evidence_files.id", ondelete="SET NULL"))
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    filename: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    locator_text: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    quoted_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    relevance: Mapped[str] = mapped_column(Text, nullable=False, default="")
    supports: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    verdict: Mapped[str] = mapped_column(String(32), nullable=False, default="UNVERIFIED")
    match_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    verification_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    assessment: Mapped[Assessment] = relationship(back_populates="citations")
    chunk: Mapped[Optional[EvidenceChunk]] = relationship(lazy="joined")


# ----------------------------------------------------------------------- human review
class HumanReview(Base):
    """The auditor's decision on an AI assessment. This is the authoritative record."""

    __tablename__ = "human_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    assessment_id: Mapped[int] = mapped_column(
        ForeignKey("assessments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reviewer_name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    decision: Mapped[str] = mapped_column(String(40), nullable=False, default=HumanDecision.PENDING.value)
    final_status: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    final_risk_level: Mapped[str] = mapped_column(String(32), nullable=False, default=RiskLevel.NOT_RATED.value)
    final_finding: Mapped[str] = mapped_column(Text, nullable=False, default="")
    final_recommendation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    comments: Mapped[str] = mapped_column(Text, nullable=False, default="")
    requested_evidence: Mapped[List[str]] = mapped_column(JSON, nullable=False, default=list)
    #: Derived on write: did the human keep the AI status and risk level?
    agreed_with_ai_status: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    agreed_with_ai_risk: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Wall-clock seconds the auditor spent on this review (time-saving metric).
    review_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: Auditor's subjective 1-5 rating of the AI output, captured for the study.
    usefulness_rating: Mapped[Optional[int]] = mapped_column(Integer)
    flagged_hallucination: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    hallucination_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    assessment: Mapped[Assessment] = relationship(back_populates="reviews")


# ------------------------------------------------------------------------- evaluation
class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    experiment_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    llm_provider: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    llm_model: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    retrieval_strategy: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    dataset_ids: Mapped[List[str]] = mapped_column(JSON, nullable=False, default=list)
    config: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    metrics: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="RUNNING")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    results: Mapped[List["EvaluationResult"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="selectin"
    )


class EvaluationResult(Base):
    """One (dataset, control) prediction scored against a known ground truth."""

    __tablename__ = "evaluation_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("evaluation_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    assessment_id: Mapped[Optional[int]] = mapped_column(ForeignKey("assessments.id", ondelete="SET NULL"))
    dataset_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    dataset_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    control_ref: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    expected_status: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    predicted_status: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    status_correct: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expected_risk: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    predicted_risk: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    #: Retrieval quality against the dataset's known key evidence.
    expected_evidence_hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expected_evidence_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    citation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    verified_citation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fabricated_citation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unsupported_claim_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hallucination_detected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    declared_missing_evidence: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text)
    detail: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    run: Mapped[EvaluationRun] = relationship(back_populates="results")


# ---------------------------------------------------------------------------- reports
class AuditReport(Base):
    __tablename__ = "audit_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("audit_projects.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    format: Mapped[str] = mapped_column(String(16), nullable=False, default="markdown")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stored_path: Mapped[Optional[str]] = mapped_column(String(1024))
    generated_by: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    summary_stats: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    project: Mapped[AuditProject] = relationship(back_populates="reports")


# ----------------------------------------------------------------------- audit trail
class ActivityLog(Base):
    """Append-only trail of who did what - supports the evidence-integrity narrative."""

    __tablename__ = "activity_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    entity_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    project_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    actor: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    #: "AI" or "HUMAN" - makes AI-vs-human provenance queryable.
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False, default="HUMAN")
    details: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


__all__ = [
    "ActivityLog",
    "Assessment",
    "AssessmentCitation",
    "AuditProject",
    "AuditReport",
    "Control",
    "EvaluationResult",
    "EvaluationRun",
    "EvidenceChunk",
    "EvidenceFile",
    "HumanReview",
    "ProjectControl",
]
