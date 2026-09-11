"""Audit report generation - the document a human is expected to sign, or refuse to.

Why this module exists
----------------------
Everything upstream of here keeps the AI record and the human record in separate rows
(``Assessment`` vs ``HumanReview``) precisely so that the two can never be confused. A
report is the one place where that separation is most likely to collapse: the natural
thing to write is a single flowing narrative about each control, and the moment that is
written the reader can no longer tell which sentences a language model produced and
which sentences an auditor stands behind. This module refuses to do that. For every
control it prints the AI conclusion and the auditor's conclusion **side by side**, each
carrying its own provenance label, and it marks the pairs that disagree.

Three rules follow from that and are enforced structurally rather than by convention:

* A control with no completed human review is listed as ``PENDING AUDITOR REVIEW`` and
  is excluded from every headline figure in the executive summary. The summary states
  its own counting basis, so a reader can see that the pending controls were left out
  rather than quietly folded in.
* Section 4 prints the full SHA-256 of every evidence file next to its size, upload
  timestamp and parse status. That hash is the only integrity claim this system can
  make, and Section 10 says exactly what it does and does not establish.
* Section 7 resolves every citation back to a filename, a locator and the verbatim
  quoted text, and prints the mechanical verification verdict beside it - including,
  loudly, the citations that could not be verified. A report that hid those would be
  reporting the validator's silence as success.

Design tension this resolves
----------------------------
Rendering and persistence are deliberately split. :func:`build_report_data` is the only
function that touches the database; :func:`render_markdown` and :func:`render_html` are
pure functions of the resulting :class:`ReportData` and touch neither the database nor
the filesystem, so a test can assert on the text of a report without either. Only
:func:`generate_report` writes a file and inserts an ``AuditReport`` row.

The data class in between holds primitives, not ORM rows. That costs a copying step but
means a rendered report cannot change under a lazy load, and that what was rendered is
exactly what the summary statistics were computed from.

Honest scope
------------
This document is a research prototype's output. It is not an audit report in the
professional sense: no opinion is expressed, no assurance is given, and Section 10 is
part of the deliverable rather than a disclaimer bolted onto it.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import service
from app.audit.prompts import PROMPT_VERSION
from app.audit.risk import BAND_THRESHOLDS, RISK_MODEL_LABEL
from app.config import get_settings
from app.database.models import (
    Assessment,
    AssessmentCitation,
    AuditReport,
    Control,
    EvidenceFile,
    HumanReview,
    ProjectControl,
)
from app.schemas.enums import (
    ActivityAction,
    AssessmentStatus,
    CitationVerdict,
    DEFICIENCY_STATUSES,
    HIGH_RISK_LEVELS,
    HumanDecision,
    RISK_ORDER,
    RiskLevel,
)

#: The two provenance labels. They are constants because they must be byte-identical
#: everywhere they appear - a reader learns to scan for them, and a near-miss variant
#: ("AI assessment", "auditor's view") would defeat that.
AI_LABEL = "AI-generated assessment (not a final audit conclusion)"
HUMAN_LABEL = "Final auditor assessment"
PENDING_LABEL = "PENDING AUDITOR REVIEW"
NOT_ASSESSED_LABEL = "NOT ASSESSED"

#: The ten mandated sections, in order. Rendering iterates this, so a section cannot be
#: dropped from one format and kept in the other.
SECTIONS: Tuple[Tuple[int, str], ...] = (
    (1, "Executive Summary"),
    (2, "Audit Scope"),
    (3, "Controls Tested"),
    (4, "Evidence Reviewed"),
    (5, "AI Findings"),
    (6, "Risk Ratings"),
    (7, "Evidence References"),
    (8, "Human Auditor Decisions"),
    (9, "Recommendations"),
    (10, "Limitations"),
)

SUPPORTED_FORMATS: Tuple[str, ...] = ("markdown", "html")

#: Longest quoted excerpt reproduced in Section 7 before it is elided. Long enough to
#: carry a spreadsheet row or a policy sentence whole; the elision is always marked.
MAX_QUOTE_CHARS = 1200

#: Cells in the summary tables are one line each. Anything longer is elided there and
#: printed in full in the per-control sections, which are not tabular.
MAX_CELL_CHARS = 160


# ---- collected data (primitives only; safe to render without a session)
@dataclass
class CitationLine:
    """One evidence reference as it will be printed, with its mechanical verdict."""

    order_index: int = 0
    chunk_id: Optional[int] = None
    evidence_file_id: Optional[int] = None
    filename: str = ""
    locator_text: str = ""
    quoted_text: str = ""
    relevance: str = ""
    supports: str = ""
    verdict: str = CitationVerdict.UNVERIFIED.value
    match_score: float = 0.0
    note: str = ""
    #: False when the cited chunk no longer resolves to a stored evidence chunk.
    resolved: bool = False

    @property
    def is_verified(self) -> bool:
        return self.verdict == CitationVerdict.VERIFIED.value

    @property
    def verification_sentence(self) -> str:
        """One plain sentence a non-technical reader can act on."""
        if self.verdict == CitationVerdict.VERIFIED.value:
            return (
                "Verified: the quoted text was found in the cited evidence chunk "
                "(text match {0:.2f}).".format(self.match_score)
            )
        if self.verdict == CitationVerdict.PARTIAL.value:
            return (
                "NOT FULLY VERIFIED: the quoted text only partially matches the cited "
                "evidence chunk (text match {0:.2f}). Read the source before relying on "
                "this quotation.".format(self.match_score)
            )
        if self.verdict == CitationVerdict.FABRICATED.value:
            return (
                "NOT VERIFIED - POSSIBLE FABRICATION: this citation could not be matched "
                "to any evidence chunk that was supplied to the model. Treat the quotation "
                "as unsupported."
            )
        return (
            "NOT VERIFIED: no quotation was supplied, or it could not be checked "
            "mechanically. The reference has not been confirmed against the evidence."
        )


@dataclass
class AssessmentLine:
    """The AI side of one control. Every field here is model output or telemetry."""

    assessment_id: Optional[int] = None
    experiment_mode: str = ""
    status: str = ""
    assessment: str = ""
    finding: str = ""
    risk: str = ""
    risk_level: str = RiskLevel.NOT_RATED.value
    risk_score: float = 0.0
    risk_factors: Dict[str, Any] = field(default_factory=dict)
    evidence_sufficiency: str = ""
    missing_evidence: List[str] = field(default_factory=list)
    reasoning: str = ""
    recommendation: str = ""
    inferences: List[str] = field(default_factory=list)
    human_verification_required: List[str] = field(default_factory=list)
    confidence: str = ""
    llm_provider: str = ""
    llm_model: str = ""
    retrieval_strategy: str = ""
    retrieval_top_k: int = 0
    retrieved_chunk_ids: List[int] = field(default_factory=list)
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_calls: int = 0
    citations: List[CitationLine] = field(default_factory=list)
    validation: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    created_at: Optional[datetime] = None

    @property
    def unverified_citations(self) -> List[CitationLine]:
        return [c for c in self.citations if not c.is_verified]

    @property
    def unsupported_claims(self) -> List[str]:
        return [str(item) for item in self.validation.get("unsupported_claims", []) or []]

    @property
    def rails_applied(self) -> List[str]:
        return [str(item) for item in self.validation.get("rails_applied", []) or []]


@dataclass
class ReviewLine:
    """The human side of one control. Only an auditor writes any of this."""

    review_id: Optional[int] = None
    reviewer_name: str = ""
    decision: str = HumanDecision.PENDING.value
    final_status: str = ""
    final_risk_level: str = RiskLevel.NOT_RATED.value
    final_finding: str = ""
    final_recommendation: str = ""
    comments: str = ""
    requested_evidence: List[str] = field(default_factory=list)
    agreed_with_ai_status: bool = False
    agreed_with_ai_risk: bool = False
    review_seconds: float = 0.0
    usefulness_rating: Optional[int] = None
    flagged_hallucination: bool = False
    hallucination_note: str = ""
    created_at: Optional[datetime] = None

    @property
    def is_decision(self) -> bool:
        """A PENDING row means somebody opened the review, not that they concluded."""
        return self.decision != HumanDecision.PENDING.value


@dataclass
class ControlLine:
    """One control with its AI assessment and its auditor decision held apart."""

    control_ref: str = ""
    name: str = ""
    category: str = ""
    objective: str = ""
    description: str = ""
    risk_addressed: str = ""
    control_type: str = ""
    control_frequency: str = ""
    inherent_risk: str = ""
    expected_evidence: List[str] = field(default_factory=list)
    assessment_criteria: List[str] = field(default_factory=list)
    framework_refs: List[str] = field(default_factory=list)
    scope_note: str = ""
    in_scope: bool = True
    assessment: Optional[AssessmentLine] = None
    review: Optional[ReviewLine] = None

    @property
    def is_assessed(self) -> bool:
        return self.assessment is not None

    @property
    def has_final_decision(self) -> bool:
        return self.review is not None and self.review.is_decision

    @property
    def is_pending_review(self) -> bool:
        """Assessed by the system, not yet concluded by a human."""
        return self.is_assessed and not self.has_final_decision

    @property
    def ai_status(self) -> str:
        return self.assessment.status if self.assessment is not None else ""

    @property
    def ai_risk_level(self) -> str:
        return self.assessment.risk_level if self.assessment is not None else ""

    @property
    def final_status(self) -> str:
        """The auditor's status, or "" when no auditor has concluded."""
        if not self.has_final_decision or self.review is None:
            return ""
        return self.review.final_status

    @property
    def final_risk_level(self) -> str:
        if not self.has_final_decision or self.review is None:
            return ""
        return self.review.final_risk_level

    @property
    def status_diverges(self) -> bool:
        """True only when a human concluded *and* landed somewhere else than the AI."""
        final = self.final_status
        return bool(final and self.ai_status and final != self.ai_status)

    @property
    def risk_diverges(self) -> bool:
        final = self.final_risk_level
        if not self.has_final_decision or not final or not self.ai_risk_level:
            return False
        return final != self.ai_risk_level

    @property
    def display_final_status(self) -> str:
        if not self.is_assessed:
            return NOT_ASSESSED_LABEL
        if not self.has_final_decision:
            return PENDING_LABEL
        return self.final_status or "(no status recorded)"

    @property
    def effective_risk_level(self) -> str:
        """Risk used for ordering: the auditor's if there is one, else the AI's."""
        return self.final_risk_level or self.ai_risk_level or RiskLevel.NOT_RATED.value


@dataclass
class EvidenceLine:
    """One uploaded artefact and the integrity facts recorded about it."""

    evidence_file_id: Optional[int] = None
    filename: str = ""
    evidence_type: str = ""
    description: str = ""
    size_bytes: int = 0
    sha256: str = ""
    uploaded_at: Optional[datetime] = None
    uploaded_by: str = ""
    parse_status: str = ""
    parse_error: str = ""
    page_count: int = 0
    row_count: int = 0
    chunk_count: int = 0
    char_count: int = 0
    is_synthetic: bool = False
    warnings: List[str] = field(default_factory=list)


@dataclass
class ReportData:
    """Everything the renderers need, and nothing that requires a live session."""

    project_id: int = 0
    project_name: str = ""
    audit_area: str = ""
    project_description: str = ""
    project_status: str = ""
    period_label: str = ""
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None
    auditor_name: str = ""
    scope_note: str = ""
    is_demo: bool = False

    title: str = ""
    generated_at: Optional[datetime] = None
    generated_by: str = ""
    app_name: str = ""
    app_version: str = ""

    llm_providers: List[str] = field(default_factory=list)
    llm_models: List[str] = field(default_factory=list)
    retrieval_strategies: List[str] = field(default_factory=list)
    experiment_modes: List[str] = field(default_factory=list)
    prompt_version: str = PROMPT_VERSION
    #: True when the current configuration was printed because no assessment recorded one.
    provenance_from_config: bool = False
    used_mock_provider: bool = False

    controls: List[ControlLine] = field(default_factory=list)
    evidence: List[EvidenceLine] = field(default_factory=list)
    evidence_stats: Dict[str, Any] = field(default_factory=dict)

    # ---- derived views (all counting is done here so both renderers agree)
    def assessed_controls(self) -> List[ControlLine]:
        return [c for c in self.controls if c.is_assessed]

    def concluded_controls(self) -> List[ControlLine]:
        """Controls an auditor has actually signed off - the only conclusive population."""
        return [c for c in self.controls if c.has_final_decision]

    def pending_controls(self) -> List[ControlLine]:
        return [c for c in self.controls if c.is_pending_review]

    def unassessed_controls(self) -> List[ControlLine]:
        return [c for c in self.controls if not c.is_assessed]

    def divergent_controls(self) -> List[ControlLine]:
        return [c for c in self.controls if c.status_diverges]

    def final_status_counts(self) -> Dict[str, int]:
        counts = {status.value: 0 for status in AssessmentStatus}
        for line in self.concluded_controls():
            key = line.final_status or "(no status recorded)"
            counts[key] = counts.get(key, 0) + 1
        return counts

    def ai_status_counts(self) -> Dict[str, int]:
        counts = {status.value: 0 for status in AssessmentStatus}
        for line in self.assessed_controls():
            counts[line.ai_status] = counts.get(line.ai_status, 0) + 1
        return counts

    def confirmed_deficiencies(self) -> List[ControlLine]:
        adverse = {status.value for status in DEFICIENCY_STATUSES}
        return [c for c in self.concluded_controls() if c.final_status in adverse]

    def confirmed_high_risk(self) -> List[ControlLine]:
        high = {level.value for level in HIGH_RISK_LEVELS}
        return [c for c in self.confirmed_deficiencies() if c.final_risk_level in high]

    def all_citations(self) -> List[Tuple[ControlLine, CitationLine]]:
        pairs: List[Tuple[ControlLine, CitationLine]] = []
        for line in self.assessed_controls():
            assessment = line.assessment
            if assessment is None:
                continue
            for citation in assessment.citations:
                pairs.append((line, citation))
        return pairs

    def citation_verdict_counts(self) -> Dict[str, int]:
        counts = {verdict.value: 0 for verdict in CitationVerdict}
        for _, citation in self.all_citations():
            counts[citation.verdict] = counts.get(citation.verdict, 0) + 1
        return counts

    def agreement_rate(self) -> Optional[float]:
        """Concluded controls where the auditor kept the AI status, over all concluded.

        ``None`` rather than 0.0 when nothing has been reviewed: a rate over an empty
        denominator is not a low agreement rate, it is no measurement at all.
        """
        concluded = self.concluded_controls()
        if not concluded:
            return None
        agreed = sum(1 for line in concluded if not line.status_diverges)
        return round(agreed / float(len(concluded)), 4)

    def provider_label(self) -> str:
        if not self.llm_providers:
            return "not recorded"
        return ", ".join(self.llm_providers)

    def model_label(self) -> str:
        if not self.llm_models:
            return "not recorded"
        return ", ".join(self.llm_models)

    def to_summary_stats(self, fmt: str = "markdown") -> Dict[str, Any]:
        """The JSON blob persisted on the ``AuditReport`` row.

        Deliberately a superset of what the document prints, so a later analysis does
        not have to re-parse the prose to recover what the report said.
        """
        verdicts = self.citation_verdict_counts()
        return {
            "format": fmt,
            "generated_at": _iso(self.generated_at),
            "generated_by": self.generated_by,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "audit_area": self.audit_area,
            "app_version": self.app_version,
            "prompt_version": self.prompt_version,
            "llm_providers": list(self.llm_providers),
            "llm_models": list(self.llm_models),
            "retrieval_strategies": list(self.retrieval_strategies),
            "experiment_modes": list(self.experiment_modes),
            "used_mock_provider": self.used_mock_provider,
            "controls_in_scope": sum(1 for c in self.controls if c.in_scope),
            "controls_reported": len(self.controls),
            "controls_assessed": len(self.assessed_controls()),
            "controls_not_assessed": len(self.unassessed_controls()),
            "controls_pending_review": len(self.pending_controls()),
            "controls_concluded_by_auditor": len(self.concluded_controls()),
            "controls_status_divergent": len(self.divergent_controls()),
            "confirmed_deficiencies": len(self.confirmed_deficiencies()),
            "confirmed_high_risk_findings": len(self.confirmed_high_risk()),
            "final_status_counts": self.final_status_counts(),
            "ai_status_counts": self.ai_status_counts(),
            "human_ai_status_agreement_rate": self.agreement_rate(),
            "citations_total": len(self.all_citations()),
            "citation_verdicts": verdicts,
            "citations_unverified": sum(
                count for key, count in verdicts.items() if key != CitationVerdict.VERIFIED.value
            ),
            "unsupported_claim_count": sum(
                len(c.assessment.unsupported_claims) for c in self.assessed_controls() if c.assessment
            ),
            "evidence_files": len(self.evidence),
            "evidence_bytes": sum(item.size_bytes for item in self.evidence),
            "evidence_synthetic_files": sum(1 for item in self.evidence if item.is_synthetic),
            "sections": [name for _, name in SECTIONS],
            "counting_basis": _COUNTING_BASIS,
            "risk_model_label": RISK_MODEL_LABEL,
        }


#: Printed in the executive summary and stored in ``summary_stats`` so the basis of
#: every figure travels with the figures.
_COUNTING_BASIS = (
    "Conclusive figures count only controls where an auditor has recorded a decision "
    "other than PENDING. Controls assessed by the system but not yet reviewed are "
    "listed separately as " + PENDING_LABEL + " and are excluded. One assessment per "
    "control is counted: the most recent, excluding assessments produced by the "
    "evaluation harness."
)


# ---- collection
def build_report_data(
    session: Session,
    project_id: int,
    generated_by: str = "",
    title: str = "",
    include_evaluation: bool = False,
) -> ReportData:
    """Read one project out of the database into a render-ready, primitive structure.

    This is the only function in the module that queries. ``include_evaluation`` is off
    by default: assessments produced by the evaluation harness belong to the research
    experiments, and folding them into an auditor's report would put controls in the
    document that nobody scoped.
    """
    settings = get_settings()
    project = service.require_project(session, project_id)

    scoped = service.list_scoped_controls(session, project.id)
    scoped_ids = {control.id for control in scoped}
    scope_notes = _scope_notes(session, project.id)

    assessments = service.list_assessments(
        session,
        project_id=project.id,
        include_evaluation=include_evaluation,
        latest_per_control=True,
    )
    by_control: Dict[int, Assessment] = {a.control_id: a for a in assessments}
    latest_reviews = _latest_reviews(session, project.id)
    citations = _citations_by_assessment(session, [a.id for a in assessments])

    # A control can carry an assessment and no longer be in scope (it was unscoped after
    # being tested). Dropping it would silently delete a finding from the report, so the
    # reported population is the union, with the out-of-scope ones flagged.
    controls: Dict[int, Control] = {control.id: control for control in scoped}
    for assessment in assessments:
        if assessment.control_id not in controls and assessment.control is not None:
            controls[assessment.control_id] = assessment.control

    lines: List[ControlLine] = []
    for control in sorted(controls.values(), key=lambda c: (c.control_id or "", c.id or 0)):
        assessment = by_control.get(control.id)
        lines.append(
            ControlLine(
                control_ref=control.control_id or "",
                name=control.name or "",
                category=control.category or "",
                objective=control.objective or "",
                description=control.description or "",
                risk_addressed=control.risk_addressed or "",
                control_type=control.control_type or "",
                control_frequency=control.control_frequency or "",
                inherent_risk=control.inherent_risk or "",
                expected_evidence=list(control.expected_evidence or []),
                assessment_criteria=list(control.assessment_criteria or []),
                framework_refs=list(control.framework_refs or []),
                scope_note=scope_notes.get(control.id, ""),
                in_scope=control.id in scoped_ids,
                assessment=(
                    _assessment_line(assessment, citations.get(assessment.id, []))
                    if assessment is not None
                    else None
                ),
                review=(
                    _review_line(latest_reviews.get(assessment.id))
                    if assessment is not None
                    else None
                ),
            )
        )

    evidence = [
        _evidence_line(item)
        for item in sorted(
            service.list_evidence(session, project_id=project.id),
            key=lambda e: (e.uploaded_at or datetime.min.replace(tzinfo=timezone.utc), e.id or 0),
        )
    ]

    providers = _distinct(a.llm_provider for a in assessments)
    models = _distinct(a.llm_model for a in assessments)
    strategies = _distinct(a.retrieval_strategy for a in assessments)
    modes = _distinct(a.experiment_mode for a in assessments)

    provenance_from_config = not providers
    if provenance_from_config:
        # Nothing has been assessed yet (or every row predates provenance capture). The
        # current configuration is printed instead, and labelled as such rather than
        # being passed off as the configuration that produced the findings.
        providers = [settings.llm_provider]
        models = [settings.llm_model]
    if not strategies:
        strategies = [settings.retrieval_strategy]

    generated_at = datetime.now(timezone.utc)
    return ReportData(
        project_id=project.id,
        project_name=project.name or "",
        audit_area=project.audit_area or "",
        project_description=project.description or "",
        project_status=project.status or "",
        period_label=project.period_label,
        period_start=project.period_start,
        period_end=project.period_end,
        auditor_name=project.auditor_name or "",
        scope_note=project.scope_note or "",
        is_demo=bool(project.is_demo),
        title=title or "IT Audit Report - {0}".format(project.name or "Untitled project"),
        generated_at=generated_at,
        generated_by=(generated_by or settings.default_auditor_name or "").strip(),
        app_name=settings.app_name,
        app_version=settings.app_version,
        llm_providers=providers,
        llm_models=models,
        retrieval_strategies=strategies,
        experiment_modes=modes,
        prompt_version=_prompt_version(assessments),
        provenance_from_config=provenance_from_config,
        used_mock_provider=any(p.lower() == "mock" for p in providers),
        controls=lines,
        evidence=evidence,
        evidence_stats=service.project_evidence_stats(session, project.id),
    )


def _scope_notes(session: Session, project_id: int) -> Dict[int, str]:
    """Per-control scope notes, read directly rather than through the project object.

    ``AuditProject.scoped_controls`` would be the obvious source, but sessions in this
    app are configured with ``expire_on_commit=False``: a project object loaded earlier
    in a long-lived Streamlit session keeps whatever that collection held at load time.
    A report must show what is in the database now, so it queries.
    """
    rows = session.execute(
        select(ProjectControl.control_id, ProjectControl.scope_note).where(
            ProjectControl.project_id == int(project_id)
        )
    ).all()
    return {int(control_id): note for control_id, note in rows if note}


def _latest_reviews(session: Session, project_id: int) -> Dict[int, HumanReview]:
    """Most recent review row per assessment, for the whole project, in one query.

    Same reason as :func:`_scope_notes` for not using ``Assessment.latest_review``: the
    caller may hold assessment objects whose ``reviews`` collection was loaded before
    the review being reported was written, and a report that silently omits a recorded
    auditor decision is exactly the failure this document exists to prevent. Ordering
    matches :mod:`app.audit.service` so the report's pending count cannot disagree with
    the dashboard's.
    """
    latest: Dict[int, HumanReview] = {}
    for review in service.list_reviews(session, project_id=project_id):
        # list_reviews returns newest first, so the first row seen per assessment wins.
        latest.setdefault(review.assessment_id, review)
    return latest


def _citations_by_assessment(
    session: Session, assessment_ids: Sequence[int]
) -> Dict[int, List[AssessmentCitation]]:
    """Citations for every reported assessment, queried for the same staleness reason."""
    if not assessment_ids:
        return {}
    rows = session.execute(
        select(AssessmentCitation)
        .where(AssessmentCitation.assessment_id.in_(list(assessment_ids)))
        .order_by(AssessmentCitation.assessment_id.asc(), AssessmentCitation.order_index.asc())
    ).unique().scalars().all()
    grouped: Dict[int, List[AssessmentCitation]] = {}
    for row in rows:
        grouped.setdefault(row.assessment_id, []).append(row)
    return grouped


def _assessment_line(
    assessment: Assessment, citations: Sequence[AssessmentCitation]
) -> AssessmentLine:
    return AssessmentLine(
        assessment_id=assessment.id,
        experiment_mode=assessment.experiment_mode or "",
        status=assessment.status or "",
        assessment=assessment.assessment or "",
        finding=assessment.finding or "",
        risk=assessment.risk or "",
        risk_level=assessment.risk_level or RiskLevel.NOT_RATED.value,
        risk_score=float(assessment.risk_score or 0.0),
        risk_factors=dict(assessment.risk_factors or {}),
        evidence_sufficiency=assessment.evidence_sufficiency or "",
        missing_evidence=[str(item) for item in (assessment.missing_evidence or [])],
        reasoning=assessment.reasoning or "",
        recommendation=assessment.recommendation or "",
        inferences=[str(item) for item in (assessment.inferences or [])],
        human_verification_required=[str(item) for item in (assessment.human_verification_required or [])],
        confidence=assessment.confidence or "",
        llm_provider=assessment.llm_provider or "",
        llm_model=assessment.llm_model or "",
        retrieval_strategy=assessment.retrieval_strategy or "",
        retrieval_top_k=int(assessment.retrieval_top_k or 0),
        retrieved_chunk_ids=[int(cid) for cid in (assessment.retrieved_chunk_ids or [])],
        latency_ms=int(assessment.latency_ms or 0),
        prompt_tokens=int(assessment.prompt_tokens or 0),
        completion_tokens=int(assessment.completion_tokens or 0),
        llm_calls=int(assessment.llm_calls or 0),
        citations=[_citation_line(citation) for citation in citations],
        validation=dict(assessment.validation_report or {}),
        error=assessment.error or "",
        created_at=assessment.created_at,
    )


def _citation_line(citation: AssessmentCitation) -> CitationLine:
    return CitationLine(
        order_index=int(citation.order_index or 0),
        chunk_id=citation.chunk_id,
        evidence_file_id=citation.evidence_file_id,
        filename=citation.filename or "",
        locator_text=citation.locator_text or "",
        quoted_text=citation.quoted_text or "",
        relevance=citation.relevance or "",
        supports=citation.supports or "",
        verdict=citation.verdict or CitationVerdict.UNVERIFIED.value,
        match_score=float(citation.match_score or 0.0),
        note=citation.verification_note or "",
        resolved=citation.chunk is not None,
    )


def _review_line(review: Optional[HumanReview]) -> Optional[ReviewLine]:
    if review is None:
        return None
    return ReviewLine(
        review_id=review.id,
        reviewer_name=review.reviewer_name or "",
        decision=review.decision or HumanDecision.PENDING.value,
        final_status=review.final_status or "",
        final_risk_level=review.final_risk_level or RiskLevel.NOT_RATED.value,
        final_finding=review.final_finding or "",
        final_recommendation=review.final_recommendation or "",
        comments=review.comments or "",
        requested_evidence=[str(item) for item in (review.requested_evidence or [])],
        agreed_with_ai_status=bool(review.agreed_with_ai_status),
        agreed_with_ai_risk=bool(review.agreed_with_ai_risk),
        review_seconds=float(review.review_seconds or 0.0),
        usefulness_rating=review.usefulness_rating,
        flagged_hallucination=bool(review.flagged_hallucination),
        hallucination_note=review.hallucination_note or "",
        created_at=review.created_at,
    )


def _evidence_line(item: EvidenceFile) -> EvidenceLine:
    extra = item.extra_metadata or {}
    warnings = extra.get("warnings") or []
    if isinstance(warnings, str):
        warnings = [warnings]
    return EvidenceLine(
        evidence_file_id=item.id,
        filename=item.filename or "",
        evidence_type=item.evidence_type or "",
        description=item.description or "",
        size_bytes=int(item.size_bytes or 0),
        sha256=item.sha256 or "",
        uploaded_at=item.uploaded_at,
        uploaded_by=item.uploaded_by or "",
        parse_status=item.parse_status or "",
        parse_error=item.parse_error or "",
        page_count=int(item.page_count or 0),
        row_count=int(item.row_count or 0),
        chunk_count=int(item.chunk_count or 0),
        char_count=int(item.char_count or 0),
        is_synthetic=bool(item.is_synthetic),
        warnings=[str(w) for w in warnings],
    )


def _prompt_version(assessments: Sequence[Assessment]) -> str:
    """Prefer a prompt version recorded on a run; fall back to this build's constant.

    ``Assessment`` has no prompt-version column, so the engine folds prompt metadata
    into ``validation_report`` when it can. When it did not, printing the current
    build's version is still useful but is not a claim about the stored rows, and the
    header says so.
    """
    recorded = _distinct(
        str((a.validation_report or {}).get("prompt_version", "")) for a in assessments
    )
    if recorded:
        return ", ".join(recorded)
    return PROMPT_VERSION


# ---- small formatting helpers
def _distinct(values: Any) -> List[str]:
    out: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return sorted(out)


def _iso(value: Optional[datetime]) -> str:
    if value is None:
        return ""
    return _aware(value).isoformat()


def _aware(value: datetime) -> datetime:
    # SQLite hands back naive datetimes; everything written by this app is UTC.
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _fmt_dt(value: Optional[datetime]) -> str:
    if value is None:
        return "not recorded"
    return _aware(value).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _fmt_date(value: Optional[datetime]) -> str:
    if value is None:
        return "not set"
    return _aware(value).astimezone(timezone.utc).strftime("%Y-%m-%d")


def _fmt_bytes(size: int) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0 or unit == "GB":
            if unit == "B":
                return "{0:.0f} {1}".format(value, unit)
            return "{0:.1f} {1}".format(value, unit)
        value /= 1024.0
    return "{0:.1f} GB".format(value)


def _one_line(text: str, limit: int = MAX_CELL_CHARS) -> str:
    collapsed = re.sub(r"\s+", " ", str(text or "")).strip()
    if not collapsed:
        return ""
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _clip_quote(text: str) -> Tuple[str, bool]:
    """Return the quote as it will be printed and whether it was shortened."""
    raw = str(text or "")
    if len(raw) <= MAX_QUOTE_CHARS:
        return raw, False
    return raw[:MAX_QUOTE_CHARS].rstrip(), True


def _dash(text: str) -> str:
    return text if text else "-"


def _locator_display(filename: str, locator_text: str) -> str:
    """Drop the filename from a locator that already repeats it.

    Locators are rendered upstream as "File.csv - rows 2, 3, 4"; a citation header
    prints the filename separately, so leaving it in reads as a stutter.
    """
    locator = str(locator_text or "").strip()
    name = str(filename or "").strip()
    if name and locator.startswith(name):
        trimmed = locator[len(name) :].lstrip()
        for separator in ("-", "–", ":", "|", ","):
            if trimmed.startswith(separator):
                trimmed = trimmed[len(separator) :].lstrip()
                break
        return trimmed or locator
    return locator


def _percent(value: Optional[float]) -> str:
    if value is None:
        return "not measurable (no reviews completed)"
    return "{0:.0%}".format(value)


def _risk_sort_key(line: ControlLine) -> Tuple[int, str]:
    level = RiskLevel.coerce(line.effective_risk_level, RiskLevel.NOT_RATED)
    return (-RISK_ORDER.get(level, 0), line.control_ref)


def _top_factors(risk_factors: Dict[str, Any], limit: int = 3) -> str:
    """The largest contributors to a prototype risk score, most significant first."""
    contributions = (risk_factors or {}).get("contributions") or {}
    values = (risk_factors or {}).get("values") or {}
    if not contributions:
        return ""
    ordered = sorted(contributions.items(), key=lambda item: float(item[1] or 0.0), reverse=True)
    parts: List[str] = []
    for name, points in ordered[:limit]:
        label = name.replace("_", " ")
        value = values.get(name)
        if value is None:
            parts.append("{0} {1:.1f} pts".format(label, float(points or 0.0)))
        else:
            parts.append("{0} {1:.1f}/5 ({2:.1f} pts)".format(label, float(value), float(points or 0.0)))
    return "; ".join(parts)


def _risk_adjustments(risk_factors: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for adjustment in (risk_factors or {}).get("adjustments") or []:
        if not isinstance(adjustment, dict):
            continue
        name = str(adjustment.get("name", "")).replace("_", " ")
        detail = str(adjustment.get("detail", ""))
        out.append("{0}: {1}".format(name, detail) if name else detail)
    return [item for item in out if item]


# ---- markdown rendering
def _md_cell(text: Any) -> str:
    """Table cells are single-line; a stray pipe would silently break the table."""
    return _one_line(str(text if text is not None else "")).replace("|", "\\|") or "-"


def _md_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> List[str]:
    lines = ["| " + " | ".join(str(h) for h in headers) + " |"]
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        lines.append("| " + " | ".join(_md_cell(cell) for cell in row) + " |")
    return lines


def _md_list(items: Sequence[str], empty: str = "None recorded.") -> List[str]:
    cleaned = [str(item).strip() for item in items if str(item).strip()]
    if not cleaned:
        return [empty]
    return ["- " + re.sub(r"\s+", " ", item) for item in cleaned]


def _md_paragraph(label: str, text: str, empty: str = "Not recorded.") -> List[str]:
    body = re.sub(r"\n{3,}", "\n\n", str(text or "").strip()) or empty
    return ["**{0}:** {1}".format(label, body)]


def render_markdown(report: ReportData) -> str:
    """Render the ten mandated sections as Markdown. Pure: no I/O, no session."""
    out: List[str] = []
    out.extend(_md_header(report))
    out.extend(_md_section_1(report))
    out.extend(_md_section_2(report))
    out.extend(_md_section_3(report))
    out.extend(_md_section_4(report))
    out.extend(_md_section_5(report))
    out.extend(_md_section_6(report))
    out.extend(_md_section_7(report))
    out.extend(_md_section_8(report))
    out.extend(_md_section_9(report))
    out.extend(_md_section_10(report))
    out.append("")
    out.append("---")
    out.append("")
    out.append(
        "_End of report. Generated by {0} v{1} at {2}. This document is the output of a "
        "research prototype and is not an audit opinion._".format(
            report.app_name, report.app_version, _fmt_dt(report.generated_at)
        )
    )
    out.append("")
    return "\n".join(out)


def _md_header(report: ReportData) -> List[str]:
    out = ["# {0}".format(report.title), ""]
    if report.is_demo:
        out.append(
            "> **Demonstration project.** The evidence in this project is synthetic and "
            "describes no real organisation, system or person."
        )
        out.append("")
    out.append(
        "> **Provenance notice.** Every statement in this report is labelled either "
        '"{0}" or "{1}". They are never merged. An AI-generated statement is advisory '
        "and carries no assurance; only a statement labelled as the auditor's is a "
        "conclusion of this audit.".format(AI_LABEL, HUMAN_LABEL)
    )
    out.append("")

    rows = [
        ["Project", "{0} (id {1})".format(report.project_name, report.project_id)],
        ["Audit area", report.audit_area],
        ["Audit period", report.period_label],
        ["Project status", report.project_status],
        ["Auditor of record", _dash(report.auditor_name)],
        ["Report generated by", _dash(report.generated_by)],
        ["Report generated at", _fmt_dt(report.generated_at)],
        ["Application", "{0} v{1}".format(report.app_name, report.app_version)],
        ["LLM provider", report.provider_label()],
        ["LLM model", report.model_label()],
        ["Retrieval strategy", ", ".join(report.retrieval_strategies) or "not recorded"],
        ["Assessment mode(s)", ", ".join(report.experiment_modes) or "not recorded"],
        ["Prompt version", report.prompt_version],
    ]
    out.extend(_md_table(["Field", "Value"], rows))
    out.append("")
    if report.provenance_from_config:
        out.append(
            "_No assessment in this project recorded a provider. The provider, model and "
            "retrieval strategy above are the application's current configuration, not a "
            "record of what produced the findings._"
        )
        out.append("")
    out.append(
        "_The prompt version is this build's prompt library version. It is a property of "
        "the software, not a per-assessment record, unless an assessment stored one._"
    )
    out.append("")
    out.append("## Report contents")
    out.append("")
    for number, name in SECTIONS:
        out.append("{0}. {1}".format(number, name))
    out.append("")
    return out


def _md_heading(number: int, name: str) -> List[str]:
    return ["", "---", "", "## {0}. {1}".format(number, name), ""]


def _md_section_1(report: ReportData) -> List[str]:
    out = _md_heading(1, "Executive Summary")
    concluded = report.concluded_controls()
    pending = report.pending_controls()
    unassessed = report.unassessed_controls()
    divergent = report.divergent_controls()

    out.append(
        "This project covers {0} control(s): {1} assessed by the system, {2} with an "
        "auditor's decision recorded, {3} still {4}, and {5} not assessed at all.".format(
            len(report.controls),
            len(report.assessed_controls()),
            len(concluded),
            len(pending),
            PENDING_LABEL,
            len(unassessed),
        )
    )
    out.append("")
    out.append("**Counting basis.** " + _COUNTING_BASIS)
    out.append("")

    out.append("### Audit conclusions ({0})".format(HUMAN_LABEL))
    out.append("")
    if not concluded:
        out.append(
            "**No control in this project has an auditor decision recorded, so this "
            "report states no audit conclusion.** Everything below is AI-generated "
            "material awaiting review."
        )
        out.append("")
    else:
        counts = report.final_status_counts()
        rows = [[status, counts.get(status, 0)] for status in AssessmentStatus.values()]
        extra = [key for key in counts if key not in set(AssessmentStatus.values())]
        rows.extend([[key, counts[key]] for key in sorted(extra)])
        rows.append(["**Total concluded**", len(concluded)])
        out.extend(_md_table(["Final auditor status", "Controls"], rows))
        out.append("")
        deficiencies = report.confirmed_deficiencies()
        high_risk = report.confirmed_high_risk()
        out.append(
            "Auditor-confirmed deficiencies: **{0}** (**{1}** rated HIGH or "
            "CRITICAL).".format(len(deficiencies), len(high_risk))
        )
        out.append("")
        if deficiencies:
            out.extend(
                _md_list(
                    [
                        "{0} - {1} - {2} (risk {3})".format(
                            line.control_ref,
                            line.name,
                            line.final_status,
                            line.final_risk_level or RiskLevel.NOT_RATED.value,
                        )
                        for line in sorted(deficiencies, key=_risk_sort_key)
                    ]
                )
            )
            out.append("")

    out.append("### Controls excluded from the conclusions above")
    out.append("")
    if pending:
        out.append(
            "The following {0} control(s) have an AI-generated assessment that **no "
            "auditor has reviewed**. They are marked {1} and are excluded from every "
            "figure in this section. The AI status is shown only so the reader knows "
            "what is waiting; it is not a conclusion.".format(len(pending), PENDING_LABEL)
        )
        out.append("")
        out.extend(
            _md_table(
                ["Control", "Name", "AI-generated status (advisory)", "Review state"],
                [
                    [line.control_ref, line.name, line.ai_status, PENDING_LABEL]
                    for line in pending
                ],
            )
        )
        out.append("")
    else:
        out.append("Every assessed control in this project has an auditor decision recorded.")
        out.append("")
    if unassessed:
        out.append(
            "A further {0} control(s) are in scope but have not been assessed by the "
            "system: {1}.".format(
                len(unassessed), ", ".join(line.control_ref for line in unassessed)
            )
        )
        out.append("")

    out.append("### AI-generated status distribution (advisory - not a conclusion)")
    out.append("")
    if report.assessed_controls():
        ai_counts = report.ai_status_counts()
        out.extend(
            _md_table(
                ["AI-generated status", "Controls"],
                [[status, ai_counts.get(status, 0)] for status in AssessmentStatus.values()],
            )
        )
        out.append("")
        out.append(
            "These counts include controls that are {0}. They describe what the system "
            "proposed, not what the audit found.".format(PENDING_LABEL)
        )
        out.append("")
    else:
        out.append("No assessments have been produced for this project.")
        out.append("")

    out.append("### AI / auditor divergence")
    out.append("")
    if not concluded:
        out.append("Not measurable: no auditor decisions have been recorded.")
    elif divergent:
        out.append(
            "The auditor reached a different conclusion from the system on **{0} of {1}** "
            "reviewed control(s) (agreement rate {2}).".format(
                len(divergent), len(concluded), _percent(report.agreement_rate())
            )
        )
        out.append("")
        out.extend(
            _md_table(
                ["Control", AI_LABEL, HUMAN_LABEL, "Divergence"],
                [
                    [line.control_ref, line.ai_status, line.final_status, "DIVERGENT"]
                    for line in divergent
                ],
            )
        )
    else:
        out.append(
            "On all {0} reviewed control(s) the auditor's final status matched the "
            "AI-generated status (agreement rate {1}). Agreement is not evidence that "
            "either conclusion is correct.".format(len(concluded), _percent(report.agreement_rate()))
        )
    out.append("")
    return out


def _md_section_2(report: ReportData) -> List[str]:
    out = _md_heading(2, "Audit Scope")
    rows = [
        ["Audit area", report.audit_area],
        ["Period start", _fmt_date(report.period_start)],
        ["Period end", _fmt_date(report.period_end)],
        ["Controls in scope", sum(1 for c in report.controls if c.in_scope)],
        ["Controls reported on", len(report.controls)],
        ["Evidence files examined", len(report.evidence)],
        ["Evidence chunks indexed", report.evidence_stats.get("evidence_chunks", 0)],
    ]
    out.extend(_md_table(["Field", "Value"], rows))
    out.append("")
    out.extend(_md_paragraph("Project description", report.project_description, "Not provided."))
    out.append("")
    out.extend(_md_paragraph("Scope note", report.scope_note, "Not provided."))
    out.append("")
    out.append("### Controls in scope")
    out.append("")
    in_scope = [line for line in report.controls if line.in_scope]
    if in_scope:
        out.extend(
            _md_table(
                ["Control", "Name", "Category", "Type", "Frequency", "Inherent risk", "Scope note"],
                [
                    [
                        line.control_ref,
                        line.name,
                        line.category,
                        line.control_type,
                        line.control_frequency,
                        line.inherent_risk,
                        line.scope_note,
                    ]
                    for line in in_scope
                ],
            )
        )
    else:
        out.append("No controls are currently scoped to this project.")
    out.append("")
    out_of_scope = [line for line in report.controls if not line.in_scope]
    if out_of_scope:
        out.append(
            "The following control(s) are reported because they were assessed during "
            "this project but have since been removed from its scope: {0}.".format(
                ", ".join(line.control_ref for line in out_of_scope)
            )
        )
        out.append("")
    out.append("### What was not in scope")
    out.append("")
    out.extend(
        _md_list(
            [
                "Only the evidence files listed in Section 4 were examined. No system, "
                "directory or database was accessed directly.",
                "No control was re-performed, no transaction was re-executed and no "
                "configuration was observed live.",
                "No interviews, walkthroughs or management representations were obtained "
                "or corroborated.",
                "No statistical sample was drawn. Where individual records are quoted, "
                "they are records the retrieval layer surfaced, not a drawn sample.",
                "No assessment of design effectiveness over the full period was "
                "performed; the evidence is a point-in-time extract unless the file "
                "itself states otherwise.",
            ]
        )
    )
    out.append("")
    return out


def _md_section_3(report: ReportData) -> List[str]:
    out = _md_heading(3, "Controls Tested")
    out.append(
        "The AI conclusion and the auditor's conclusion are shown side by side for every "
        "control. They are separate records and are never merged. A control marked "
        "**{0}** has no auditor conclusion at all.".format(PENDING_LABEL)
    )
    out.append("")
    rows = []
    for line in report.controls:
        if line.status_diverges:
            agreement = "DIVERGENT"
        elif line.has_final_decision:
            agreement = "Agreed"
        elif line.is_assessed:
            agreement = "Not reviewed"
        else:
            agreement = "Not assessed"
        rows.append(
            [
                line.control_ref,
                line.name,
                line.category,
                line.ai_status or "-",
                line.display_final_status,
                agreement,
                line.ai_risk_level or "-",
                line.final_risk_level or "-",
            ]
        )
    out.extend(
        _md_table(
            [
                "Control",
                "Name",
                "Category",
                AI_LABEL,
                HUMAN_LABEL,
                "Agreement",
                "AI risk",
                "Final risk",
            ],
            rows,
        )
    )
    out.append("")
    diverging = report.divergent_controls()
    if diverging:
        out.append(
            "**Divergence.** {0} control(s) where the auditor did not accept the AI "
            "status: {1}. The auditor's status governs.".format(
                len(diverging), ", ".join(line.control_ref for line in diverging)
            )
        )
        out.append("")
    return out


def _md_section_4(report: ReportData) -> List[str]:
    out = _md_heading(4, "Evidence Reviewed")
    stats = report.evidence_stats or {}
    out.append(
        "{0} evidence file(s), {1} in total, producing {2} indexed evidence chunk(s). "
        "The SHA-256 recorded against each file was computed over the stored bytes at "
        "the moment of upload.".format(
            len(report.evidence),
            _fmt_bytes(int(stats.get("total_bytes", 0) or 0)),
            stats.get("evidence_chunks", 0),
        )
    )
    out.append("")
    if not report.evidence:
        out.append("**No evidence files have been uploaded to this project.**")
        out.append("")
        return out

    out.extend(
        _md_table(
            [
                "#",
                "Filename",
                "Type",
                "Size",
                "SHA-256 (first 16)",
                "Uploaded (UTC)",
                "Uploaded by",
                "Parse status",
            ],
            [
                [
                    index,
                    item.filename,
                    item.evidence_type,
                    _fmt_bytes(item.size_bytes),
                    # Truncated only so the table stays readable; the full 64-character
                    # digest is printed for every file in the register below.
                    "`{0}`".format((item.sha256 or "not recorded")[:16]),
                    _fmt_dt(item.uploaded_at),
                    _dash(item.uploaded_by),
                    item.parse_status,
                ]
                for index, item in enumerate(report.evidence, start=1)
            ],
        )
    )
    out.append("")
    out.append("### Evidence integrity register")
    out.append("")
    for index, item in enumerate(report.evidence, start=1):
        out.append("**{0}. {1}**".format(index, item.filename))
        out.append("")
        out.append("- SHA-256: `{0}`".format(item.sha256 or "not recorded"))
        out.append("- Size: {0} ({1} bytes)".format(_fmt_bytes(item.size_bytes), item.size_bytes))
        out.append("- Uploaded: {0} by {1}".format(_fmt_dt(item.uploaded_at), _dash(item.uploaded_by)))
        out.append("- Evidence type: {0}".format(_dash(item.evidence_type)))
        out.append("- Parse status: {0}".format(_dash(item.parse_status)))
        out.append(
            "- Extracted: {0} page(s), {1} data row(s), {2} chunk(s), {3} characters".format(
                item.page_count, item.row_count, item.chunk_count, item.char_count
            )
        )
        if item.description:
            out.append("- Description: {0}".format(_one_line(item.description, 400)))
        if item.is_synthetic:
            out.append(
                "- **Synthetic file.** Generated by this project's dataset generator; it "
                "describes no real organisation, system or person."
            )
        if item.parse_error:
            out.append("- **Parse error:** {0}".format(_one_line(item.parse_error, 400)))
        for warning in item.warnings:
            out.append("- Parser warning: {0}".format(_one_line(warning, 400)))
        out.append("")

    failed = [item for item in report.evidence if item.parse_status not in ("PARSED",)]
    if failed:
        out.append(
            "**{0} file(s) were not fully parsed** and their content was therefore not "
            "available to the assessment: {1}. Any conclusion that depended on them is "
            "unsupported.".format(len(failed), ", ".join(item.filename for item in failed))
        )
        out.append("")
    out.append(
        "The hash chain above establishes that each file is unchanged since it was "
        "uploaded to this system, and nothing more. It does not establish who produced "
        "the file, whether it was altered before upload, or whether it is a complete and "
        "faithful export of the underlying system. See Section 10."
    )
    out.append("")
    return out


def _md_section_5(report: ReportData) -> List[str]:
    out = _md_heading(5, "AI Findings")
    out.append(
        "Everything in this section is **{0}**. Nothing here is an audit conclusion. "
        "The auditor's decisions are in Section 8.".format(AI_LABEL)
    )
    out.append("")
    assessed = report.assessed_controls()
    if not assessed:
        out.append("No assessments have been produced for this project.")
        out.append("")
        return out

    for line in assessed:
        assessment = line.assessment
        if assessment is None:  # pragma: no cover - guarded by assessed_controls()
            continue
        out.append("### {0} - {1}".format(line.control_ref, line.name))
        out.append("")
        out.append("_{0}_".format(AI_LABEL))
        out.append("")
        if line.is_pending_review:
            out.append("> **{0}.** No auditor has reviewed this assessment.".format(PENDING_LABEL))
        else:
            marker = "differs from" if line.status_diverges else "matches"
            out.append(
                "> Reviewed by an auditor. The auditor's final status ({0}) **{1}** the "
                "AI status below. See Section 8.".format(line.final_status, marker)
            )
        out.append("")
        out.extend(
            _md_table(
                ["Field", "Value"],
                [
                    ["AI-generated status", assessment.status],
                    [
                        "AI-suggested risk",
                        "{0} (score {1:.1f}/100)".format(
                            assessment.risk_level, assessment.risk_score
                        ),
                    ],
                    ["Evidence sufficiency", assessment.evidence_sufficiency],
                    ["Model confidence", assessment.confidence],
                    ["Assessment mode", assessment.experiment_mode],
                    [
                        "Provider / model",
                        "{0} / {1}".format(
                            _dash(assessment.llm_provider), _dash(assessment.llm_model)
                        ),
                    ],
                    ["Citations recorded", len(assessment.citations)],
                    ["Assessment generated", _fmt_dt(assessment.created_at)],
                ],
            )
        )
        out.append("")
        out.extend(_md_paragraph("Control requirement", line.objective, "Not recorded."))
        out.append("")
        out.extend(_md_paragraph("What the evidence shows (AI-generated)", assessment.assessment))
        out.append("")
        out.extend(_md_paragraph("Potential finding (AI-generated)", assessment.finding, "None stated."))
        out.append("")
        out.extend(
            _md_paragraph(
                "Risk if the control does not operate (AI-generated)", assessment.risk, "None stated."
            )
        )
        out.append("")
        out.extend(_md_paragraph("Reasoning (AI-generated)", assessment.reasoning, "None recorded."))
        out.append("")
        out.append("**Inferences the AI drew beyond the literal evidence:**")
        out.append("")
        out.extend(_md_list(assessment.inferences, "None declared."))
        out.append("")
        out.append("**Checks a human auditor must still perform:**")
        out.append("")
        out.extend(
            _md_list(
                assessment.human_verification_required,
                "None declared by the model. This does not mean none are required.",
            )
        )
        out.append("")
        out.append("**Evidence the AI reported as missing:**")
        out.append("")
        out.extend(_md_list(assessment.missing_evidence, "None declared."))
        out.append("")
        if assessment.error:
            out.append("**Run error recorded:** {0}".format(_one_line(assessment.error, 600)))
            out.append("")
        out.extend(_md_validation_block(assessment))
        out.append("")
    return out


def _md_validation_block(assessment: AssessmentLine) -> List[str]:
    """The mechanical grounding check, printed next to the finding it constrains."""
    out = ["**Automated grounding check:**", ""]
    validation = assessment.validation or {}
    if not validation and not assessment.citations:
        out.append(
            "- No citation validation was recorded for this assessment. Its statements "
            "have not been checked against the evidence by this system."
        )
        return out
    total = int(validation.get("total", len(assessment.citations)) or 0)
    verified = int(validation.get("verified", 0) or 0)
    partial = int(validation.get("partial", 0) or 0)
    fabricated = int(validation.get("fabricated", 0) or 0)
    unverified = int(validation.get("unverified", 0) or 0)
    out.append(
        "- Citations: {0} total - {1} verified, {2} partial, {3} unverified, "
        "{4} not matched to any supplied evidence.".format(
            total, verified, partial, unverified, fabricated
        )
    )
    if "grounding_rate" in validation:
        out.append(
            "- Grounding rate: {0} ({1}).".format(
                validation.get("grounding_rate"),
                validation.get("grounding_rate_definition", "definition not recorded"),
            )
        )
    if fabricated:
        out.append(
            "- **{0} citation(s) could not be matched to evidence supplied to the model.** "
            "Treat the associated statements as unsupported.".format(fabricated)
        )
    claims = assessment.unsupported_claims
    if claims:
        out.append("- **Unsupported numeric claims detected in the AI text:**")
        for claim in claims:
            out.append("    - {0}".format(_one_line(claim, 300)))
    rails = assessment.rails_applied
    if rails:
        out.append("- Safety rails applied: {0}.".format(", ".join(rails)))
    if validation.get("status_downgraded"):
        out.append(
            "- The AI status was automatically downgraded from {0} because it was not "
            "supported by verified evidence.".format(validation.get("original_status", "its original value"))
        )
    return out


def _md_section_6(report: ReportData) -> List[str]:
    out = _md_heading(6, "Risk Ratings")
    out.append("**{0}**".format(RISK_MODEL_LABEL))
    out.append("")
    out.append(
        "Ratings are computed deterministically from five weighted factors, not taken "
        "from the language model. Bands on the 0-100 scale: "
        + ", ".join(
            "{0} from {1:.0f}".format(name, value)
            for name, value in sorted(BAND_THRESHOLDS.items(), key=lambda kv: kv[1])
        )
        + ". Scores are comparable within this system only."
    )
    out.append("")
    assessed = report.assessed_controls()
    if not assessed:
        out.append("No risk ratings have been produced for this project.")
        out.append("")
        return out

    rows = []
    for line in sorted(assessed, key=_risk_sort_key):
        assessment = line.assessment
        if assessment is None:  # pragma: no cover
            continue
        rows.append(
            [
                line.control_ref,
                assessment.status,
                "{0} ({1:.1f})".format(assessment.risk_level, assessment.risk_score),
                line.final_risk_level or PENDING_LABEL,
                "DIVERGENT" if line.risk_diverges else ("Agreed" if line.has_final_decision else "-"),
                _top_factors(assessment.risk_factors),
            ]
        )
    out.extend(
        _md_table(
            [
                "Control",
                "AI status",
                "Prototype risk rating (AI run)",
                "Final auditor risk",
                "Agreement",
                "Largest score contributors",
            ],
            rows,
        )
    )
    out.append("")
    adjusted = [
        (line, _risk_adjustments(line.assessment.risk_factors))
        for line in assessed
        if line.assessment is not None and _risk_adjustments(line.assessment.risk_factors)
    ]
    if adjusted:
        out.append("### Score adjustments applied by the risk model")
        out.append("")
        for line, adjustments in adjusted:
            out.append("- **{0}**".format(line.control_ref))
            for adjustment in adjustments:
                out.append("    - {0}".format(_one_line(adjustment, 400)))
        out.append("")
    pending = [line for line in assessed if not line.has_final_decision]
    if pending:
        out.append(
            "Risk ratings for {0} control(s) are shown as {1}: the rating in the "
            "AI column has not been confirmed by an auditor.".format(len(pending), PENDING_LABEL)
        )
        out.append("")
    return out


def _md_section_7(report: ReportData) -> List[str]:
    out = _md_heading(7, "Evidence References")
    out.append(
        "Every reference the system attached to a finding, resolved to its file, its "
        "location within that file, the verbatim text quoted, and the result of "
        "mechanically re-checking that quotation against the stored evidence."
    )
    out.append("")
    pairs = report.all_citations()
    if not pairs:
        out.append(
            "**No evidence references were recorded for this project.** No finding in "
            "this report is traceable to a specific passage of evidence."
        )
        out.append("")
        return out

    verdicts = report.citation_verdict_counts()
    out.extend(
        _md_table(
            ["Verification verdict", "Citations", "Meaning"],
            [
                [
                    CitationVerdict.VERIFIED.value,
                    verdicts.get(CitationVerdict.VERIFIED.value, 0),
                    "Quoted text was found in the cited evidence chunk.",
                ],
                [
                    CitationVerdict.PARTIAL.value,
                    verdicts.get(CitationVerdict.PARTIAL.value, 0),
                    "Quoted text only partially matches the cited chunk.",
                ],
                [
                    CitationVerdict.UNVERIFIED.value,
                    verdicts.get(CitationVerdict.UNVERIFIED.value, 0),
                    "No quotation supplied, or it could not be checked.",
                ],
                [
                    CitationVerdict.FABRICATED.value,
                    verdicts.get(CitationVerdict.FABRICATED.value, 0),
                    "Could not be matched to any evidence supplied to the model.",
                ],
            ],
        )
    )
    out.append("")
    out.append(
        "A VERIFIED verdict means the quoted characters are present in the chunk the "
        "model pointed at. It does not mean the quotation was read in context or that it "
        "supports the conclusion drawn from it. See Section 10."
    )
    out.append("")

    for line in report.assessed_controls():
        assessment = line.assessment
        if assessment is None:  # pragma: no cover
            continue
        out.append("### {0} - {1}".format(line.control_ref, line.name))
        out.append("")
        if not assessment.citations:
            out.append(
                "**No evidence references were recorded for this control.** The "
                "AI-generated statements in Section 5 are not traceable to a specific "
                "passage of evidence."
            )
            out.append("")
            continue
        for citation in assessment.citations:
            quote, clipped = _clip_quote(citation.quoted_text)
            out.append(
                "**[{0}] {1}** - {2}".format(
                    citation.order_index + 1,
                    _dash(citation.filename),
                    _dash(_locator_display(citation.filename, citation.locator_text)),
                )
            )
            out.append("")
            if quote.strip():
                for quote_line in quote.splitlines() or [""]:
                    out.append("> " + quote_line)
                if clipped:
                    out.append("> [quotation truncated for the report]")
            else:
                out.append("> (no verbatim text was quoted)")
            out.append("")
            out.append("- Verification: **{0}** - {1}".format(citation.verdict, citation.verification_sentence))
            out.append(
                "- Evidence chunk id: {0}{1}".format(
                    citation.chunk_id if citation.chunk_id is not None else "not supplied",
                    "" if citation.resolved else " (does not resolve to a stored evidence chunk)",
                )
            )
            if citation.supports:
                out.append("- Cited as: {0}".format(citation.supports))
            if citation.relevance:
                out.append("- Stated relevance (AI-generated): {0}".format(_one_line(citation.relevance, 400)))
            if citation.note:
                out.append("- Validator note: {0}".format(_one_line(citation.note, 400)))
            out.append("")
        unverified = assessment.unverified_citations
        if unverified:
            out.append(
                "**{0} of {1} reference(s) for this control could not be fully "
                "verified.** An auditor must open the source file before relying on "
                "them.".format(len(unverified), len(assessment.citations))
            )
            out.append("")
    return out


def _md_section_8(report: ReportData) -> List[str]:
    out = _md_heading(8, "Human Auditor Decisions")
    out.append(
        "Everything in this section is **{0}**. These are the only conclusive "
        "statements in this report.".format(HUMAN_LABEL)
    )
    out.append("")
    concluded = report.concluded_controls()
    pending = report.pending_controls()

    if not concluded:
        out.append("**No auditor decision has been recorded for any control in this project.**")
        out.append("")
    else:
        out.extend(
            _md_table(
                ["Control", "Reviewer", "Decision", HUMAN_LABEL, "Final risk", AI_LABEL, "Divergence", "Reviewed at"],
                [
                    [
                        line.control_ref,
                        line.review.reviewer_name if line.review else "",
                        line.review.decision if line.review else "",
                        line.final_status,
                        line.final_risk_level,
                        line.ai_status,
                        "DIVERGENT" if line.status_diverges else "Agreed",
                        _fmt_dt(line.review.created_at) if line.review else "",
                    ]
                    for line in concluded
                ],
            )
        )
        out.append("")
        for line in concluded:
            review = line.review
            if review is None:  # pragma: no cover - guarded by concluded_controls()
                continue
            out.append("### {0} - {1}".format(line.control_ref, line.name))
            out.append("")
            out.append("_{0}_".format(HUMAN_LABEL))
            out.append("")
            out.extend(
                _md_table(
                    ["Field", "Value"],
                    [
                        ["Reviewer", _dash(review.reviewer_name)],
                        ["Decision", review.decision],
                        ["Final status", _dash(review.final_status)],
                        ["Final risk level", _dash(review.final_risk_level)],
                        ["AI-generated status (for comparison)", _dash(line.ai_status)],
                        ["AI-suggested risk (for comparison)", _dash(line.ai_risk_level)],
                        ["Status agreement", "DIVERGENT" if line.status_diverges else "Agreed"],
                        ["Risk agreement", "DIVERGENT" if line.risk_diverges else "Agreed"],
                        ["Reviewed at", _fmt_dt(review.created_at)],
                        [
                            "Time spent",
                            "{0:.0f} s".format(review.review_seconds)
                            if review.review_seconds
                            else "not recorded",
                        ],
                        [
                            "Usefulness rating of the AI output",
                            "{0}/5".format(review.usefulness_rating) if review.usefulness_rating else "not recorded",
                        ],
                    ],
                )
            )
            out.append("")
            out.extend(_md_paragraph("Auditor's finding", review.final_finding, "Not restated by the auditor."))
            out.append("")
            out.extend(
                _md_paragraph(
                    "Auditor's recommendation", review.final_recommendation, "Not stated by the auditor."
                )
            )
            out.append("")
            out.extend(_md_paragraph("Auditor's comments", review.comments, "None."))
            out.append("")
            if review.requested_evidence:
                out.append("**Further evidence requested by the auditor:**")
                out.append("")
                out.extend(_md_list(review.requested_evidence))
                out.append("")
            if review.flagged_hallucination:
                out.append(
                    "> **The auditor flagged this AI output as containing a hallucination.** {0}".format(
                        _one_line(review.hallucination_note, 600) or "No note recorded."
                    )
                )
                out.append("")
            if line.status_diverges:
                out.append(
                    "> **Divergence.** The system proposed {0}; the auditor concluded {1}. "
                    "The auditor's status governs and the AI status is retained only as a "
                    "research record.".format(line.ai_status, line.final_status)
                )
                out.append("")

    out.append("### Controls awaiting auditor review")
    out.append("")
    if pending:
        out.append(
            "{0} control(s) are {1}. No conclusion has been reached on them and they are "
            "excluded from the executive summary.".format(len(pending), PENDING_LABEL)
        )
        out.append("")
        out.extend(
            _md_table(
                ["Control", "Name", "AI-generated status (advisory)", "Assessment generated", "Review state"],
                [
                    [
                        line.control_ref,
                        line.name,
                        line.ai_status,
                        _fmt_dt(line.assessment.created_at) if line.assessment else "",
                        _pending_state(line),
                    ]
                    for line in pending
                ],
            )
        )
        out.append("")
    else:
        out.append("None. Every assessed control has an auditor decision recorded.")
        out.append("")
    return out


def _pending_state(line: ControlLine) -> str:
    """Distinguish "nobody opened it" from "opened, decision still PENDING"."""
    if line.review is None:
        return "{0} - no review started".format(PENDING_LABEL)
    return "{0} - review opened {1}, decision still PENDING".format(
        PENDING_LABEL, _fmt_dt(line.review.created_at)
    )


def _md_section_9(report: ReportData) -> List[str]:
    out = _md_heading(9, "Recommendations")
    out.append(
        "Recommendations are listed highest risk first. Each one is labelled with its "
        "source: only a recommendation labelled **{0}** has been adopted by an "
        "auditor.".format(HUMAN_LABEL)
    )
    out.append("")
    printed = 0
    for line in sorted(report.controls, key=_risk_sort_key):
        review = line.review if line.has_final_decision else None
        assessment = line.assessment
        human_text = review.final_recommendation.strip() if review and review.final_recommendation else ""
        ai_text = assessment.recommendation.strip() if assessment and assessment.recommendation else ""
        if not human_text and not ai_text:
            continue
        printed += 1
        out.append(
            "### {0} - {1} (risk {2})".format(
                line.control_ref, line.name, line.effective_risk_level
            )
        )
        out.append("")
        if human_text:
            out.append("**{0}:** {1}".format(HUMAN_LABEL, human_text))
            out.append("")
        if ai_text:
            out.append("**{0}:** {1}".format(AI_LABEL, ai_text))
            out.append("")
            if not human_text:
                if line.is_pending_review:
                    out.append(
                        "> This control is {0}. The recommendation above is a machine "
                        "proposal that no auditor has adopted.".format(PENDING_LABEL)
                    )
                else:
                    out.append(
                        "> The auditor reviewed this control but did not record a "
                        "recommendation of their own. The proposal above remains "
                        "AI-generated and unadopted."
                    )
                out.append("")
    if not printed:
        out.append("No recommendations have been recorded for this project.")
        out.append("")
    return out


def _md_section_10(report: ReportData) -> List[str]:
    out = _md_heading(10, "Limitations")
    for heading, body in _limitations(report):
        out.append("**{0}.** {1}".format(heading, body))
        out.append("")
    return out


def _limitations(report: ReportData) -> List[Tuple[str, str]]:
    """The honest scope of this document.

    Written as data rather than prose so that both renderers print the identical text
    and so that a reader can see the list is not decorative: each item names something
    this system provably cannot do.
    """
    # The opening sentence has to agree with what actually produced the assessments.
    # Saying "a language model" and then, one item later, "not a language model at all"
    # would be the first contradiction a reader hits in the section that is supposed to
    # be the honest one.
    if report.used_mock_provider:
        origin_sentence = (
            "Every statement in Sections 5, 6 and 7 was produced automatically by the "
            "configured assessment provider - for this report the offline mock described "
            "in the next item - and not by a human auditor. "
        )
    else:
        origin_sentence = (
            "Every statement in Sections 5, 6 and 7 was produced by a language model "
            "operating on the supplied evidence. "
        )

    items: List[Tuple[str, str]] = [
        (
            "The assessments are machine-generated and advisory",
            origin_sentence
            + "It is not an audit opinion, it carries "
            "no assurance, and it is not a substitute for the professional judgement of "
            "a qualified auditor. Only the statements in Section 8, labelled \"{0}\", "
            "are conclusions of this audit. Controls marked {1} have no conclusion at "
            "all and are excluded from the executive summary.".format(HUMAN_LABEL, PENDING_LABEL),
        ),
        (
            "Evidence authenticity cannot be established",
            "The system records a SHA-256 hash of each file at the moment of upload. "
            "That hash shows the file has not changed inside this system since then. It "
            "says nothing about who produced the file, whether it is a genuine export "
            "from the system it claims to describe, whether it was edited before it was "
            "uploaded, or whether an authorised person approved it. No file was obtained "
            "independently, and no system was accessed directly to corroborate one.",
        ),
        (
            "Population completeness cannot be established",
            "The system can only examine what was uploaded. It cannot determine whether "
            "a supplied listing is the complete population, whether rows were filtered, "
            "sorted or removed before export, or whether a policy document is the version "
            "that was actually in force. Counts and percentages quoted anywhere in this "
            "report describe the contents of the supplied files, not the underlying "
            "systems.",
        ),
        (
            "The evidence examined may not represent the audit period",
            "Uploaded extracts are point-in-time unless the file itself states otherwise. "
            "Nothing in this system establishes that an extract covers the audit period "
            "stated in Section 2, that the control operated the same way throughout that "
            "period, or that the records shown are representative of it.",
        ),
        (
            "Quoted records are retrieved passages, not a statistical sample",
            "Where individual rows or paragraphs are quoted, they are the passages the "
            "retrieval layer ranked highest for the control, not a sample drawn by any "
            "sampling method. No confidence interval, projection or extrapolation to the "
            "wider population may be inferred from them. Retrieval also returns only part "
            "of each file, so material elsewhere in a document may never have been seen "
            "by the model.",
        ),
        (
            "Citation verification is textual, not semantic",
            "A VERIFIED verdict in Section 7 means the quoted characters were found in "
            "the evidence chunk the model pointed at. It does not mean the quotation was "
            "read in its context, that it means what the surrounding text claims, or that "
            "it supports the conclusion drawn from it. Text reassembled from a chunk's "
            "own vocabulary can score as verified while asserting something the chunk "
            "does not. The verified-citation figures are therefore a lower bound on "
            "traceability and not a measure of faithfulness.",
        ),
        (
            "No legal, regulatory or certification opinion is expressed",
            "Nothing in this report states or implies compliance with any law, "
            "regulation, contract, standard or certification scheme. Framework references "
            "attached to controls in the library are illustrative subject-area pointers "
            "and have not been verified against the published frameworks they name. This "
            "system is not authorised to declare regulatory compliance and does not do so.",
        ),
        (
            "The risk model is a prototype research construct",
            RISK_MODEL_LABEL
            + " The five factors, their weights and the band thresholds are the author's "
            "own defensible choices, calibrated so the ordering of outcomes is sensible. "
            "They are not derived from, endorsed by, or equivalent to COBIT, ISO 27005, "
            "NIST SP 800-30, FAIR or any other published methodology, and the scores are "
            "comparable only within this system.",
        ),
        (
            "Results are specific to the recorded configuration",
            "The conclusions depend on the provider, model, retrieval strategy and prompt "
            "version recorded in the header of this report. A different model, a different "
            "retrieval configuration or a revised prompt may produce different conclusions "
            "from identical evidence. Reproducibility extends only to the configuration "
            "recorded above, and the model provider may itself change a model behind a "
            "stable name.",
        ),
        (
            "This report is a snapshot",
            "It reflects the evidence, assessments and auditor decisions stored in this "
            "system at {0}. Evidence uploaded later, assessments re-run later, or reviews "
            "recorded later are not reflected here.".format(_fmt_dt(report.generated_at)),
        ),
    ]

    if report.used_mock_provider:
        items.insert(
            1,
            (
                "These assessments were NOT produced by a language model",
                "The recorded provider is \"{0}\" with model \"{1}\". That is this "
                "project's offline mock provider: a deterministic, rule-based stand-in "
                "that reads the retrieved evidence and applies hand-written rules. It "
                "makes no network call and involves no language model at all. Results "
                "obtained with it measure the behaviour of the surrounding pipeline - "
                "retrieval, citation handling, validation, risk scoring, the human-review "
                "gate - and say nothing whatsoever about the quality of any language "
                "model. No claim about LLM performance may be based on this "
                "report.".format(report.provider_label(), report.model_label()),
            ),
        )

    if any(item.is_synthetic for item in report.evidence) or report.is_demo:
        items.append(
            (
                "The evidence is synthetic",
                "One or more evidence files in this project were produced by the "
                "project's synthetic dataset generator. They describe no real "
                "organisation, system, account or person, and no finding in this report "
                "describes a real control environment.",
            )
        )

    failed = [item for item in report.evidence if item.parse_status not in ("PARSED",)]
    if failed:
        items.append(
            (
                "Some evidence could not be read",
                "{0} uploaded file(s) were not fully parsed ({1}), so their contents were "
                "not available to the assessment. Any conclusion that would have depended "
                "on them is unsupported and the file must be reviewed manually.".format(
                    len(failed), ", ".join(item.filename for item in failed)
                ),
            )
        )

    return items


# ---- HTML rendering
def _h(value: Any) -> str:
    """Escape for HTML text and attribute contexts. Every cell goes through this."""
    return html.escape(str(value if value is not None else ""), quote=True)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-") or "item"


#: Rendered in a table cell that has no value to colour-code.
_NO_VALUE_BADGE = '<span class="badge badge-none">-</span>'


def _badge(value: str, kind: str) -> str:
    """A colour-coded token. ``kind`` selects the palette (status / risk / verdict)."""
    text = str(value or "").strip()
    if not text:
        return _NO_VALUE_BADGE
    return '<span class="badge {0}-{1}">{2}</span>'.format(kind, _slug(text), _h(text))


def _html_table(headers: Sequence[str], rows: Sequence[Sequence[str]], css_class: str = "") -> str:
    """Build a table. Cells must already be HTML-safe fragments."""
    classes = " class=\"{0}\"".format(_h(css_class)) if css_class else ""
    parts = ["<div class=\"table-wrap\"><table{0}>".format(classes), "<thead><tr>"]
    parts.extend("<th>{0}</th>".format(_h(header)) for header in headers)
    parts.append("</tr></thead><tbody>")
    for row in rows:
        parts.append("<tr>")
        parts.extend("<td>{0}</td>".format(cell) for cell in row)
        parts.append("</tr>")
    parts.append("</tbody></table></div>")
    return "".join(parts)


def _html_list(items: Sequence[str], empty: str = "None recorded.") -> str:
    cleaned = [str(item).strip() for item in items if str(item).strip()]
    if not cleaned:
        return "<p class=\"muted\">{0}</p>".format(_h(empty))
    return "<ul>" + "".join("<li>{0}</li>".format(_h(item)) for item in cleaned) + "</ul>"


def _html_paragraphs(text: str, empty: str = "Not recorded.") -> str:
    body = str(text or "").strip()
    if not body:
        return "<p class=\"muted\">{0}</p>".format(_h(empty))
    blocks = [block.strip() for block in re.split(r"\n\s*\n", body) if block.strip()]
    return "".join("<p>{0}</p>".format(_h(block).replace("\n", "<br>")) for block in blocks)


def _html_field(label: str, text: str, empty: str = "Not recorded.") -> str:
    return "<div class=\"field\"><div class=\"field-label\">{0}</div>{1}</div>".format(
        _h(label), _html_paragraphs(text, empty)
    )


def _html_section(number: int, name: str, body: str) -> str:
    return (
        "<section id=\"section-{0}\" class=\"report-section\">"
        "<h2><span class=\"section-number\">{0}</span> {1}</h2>{2}</section>"
    ).format(number, _h(name), body)


def render_html(report: ReportData) -> str:
    """Render the same ten sections as a self-contained, printable HTML document.

    No external stylesheet, font or script is referenced: the file must open and print
    correctly from a filesystem with no network, which is also the only way an evidence
    artefact can be archived honestly.
    """
    body = [
        _html_header(report),
        _html_section(1, "Executive Summary", _html_section_1(report)),
        _html_section(2, "Audit Scope", _html_section_2(report)),
        _html_section(3, "Controls Tested", _html_section_3(report)),
        _html_section(4, "Evidence Reviewed", _html_section_4(report)),
        _html_section(5, "AI Findings", _html_section_5(report)),
        _html_section(6, "Risk Ratings", _html_section_6(report)),
        _html_section(7, "Evidence References", _html_section_7(report)),
        _html_section(8, "Human Auditor Decisions", _html_section_8(report)),
        _html_section(9, "Recommendations", _html_section_9(report)),
        _html_section(10, "Limitations", _html_section_10(report)),
        "<footer><p>End of report. Generated by {0} v{1} at {2}. This document is the "
        "output of a research prototype and is not an audit opinion.</p></footer>".format(
            _h(report.app_name), _h(report.app_version), _h(_fmt_dt(report.generated_at))
        ),
    ]
    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<title>{title}</title>\n<style>\n{css}\n</style>\n</head>\n"
        "<body>\n<main class=\"report\">\n{body}\n</main>\n</body>\n</html>\n"
    ).format(title=_h(report.title), css=_REPORT_CSS, body="\n".join(body))


def _html_header(report: ReportData) -> str:
    parts = ["<header class=\"report-header\">", "<h1>{0}</h1>".format(_h(report.title))]
    if report.is_demo:
        parts.append(
            "<div class=\"callout callout-synthetic\"><strong>Demonstration project.</strong> "
            "The evidence in this project is synthetic and describes no real organisation, "
            "system or person.</div>"
        )
    parts.append(
        "<div class=\"callout callout-provenance\"><strong>Provenance notice.</strong> Every "
        "statement in this report is labelled either &ldquo;{0}&rdquo; or &ldquo;{1}&rdquo;. "
        "They are never merged. An AI-generated statement is advisory and carries no "
        "assurance; only a statement labelled as the auditor&rsquo;s is a conclusion of this "
        "audit.</div>".format(_h(AI_LABEL), _h(HUMAN_LABEL))
    )
    rows = [
        ["Project", "{0} (id {1})".format(report.project_name, report.project_id)],
        ["Audit area", report.audit_area],
        ["Audit period", report.period_label],
        ["Project status", report.project_status],
        ["Auditor of record", _dash(report.auditor_name)],
        ["Report generated by", _dash(report.generated_by)],
        ["Report generated at", _fmt_dt(report.generated_at)],
        ["Application", "{0} v{1}".format(report.app_name, report.app_version)],
        ["LLM provider", report.provider_label()],
        ["LLM model", report.model_label()],
        ["Retrieval strategy", ", ".join(report.retrieval_strategies) or "not recorded"],
        ["Assessment mode(s)", ", ".join(report.experiment_modes) or "not recorded"],
        ["Prompt version", report.prompt_version],
    ]
    parts.append(
        _html_table(
            ["Field", "Value"],
            [["<strong>{0}</strong>".format(_h(key)), _h(value)] for key, value in rows],
            css_class="meta",
        )
    )
    if report.provenance_from_config:
        parts.append(
            "<p class=\"muted\">No assessment in this project recorded a provider. The "
            "provider, model and retrieval strategy above are the application&rsquo;s "
            "current configuration, not a record of what produced the findings.</p>"
        )
    parts.append(
        "<p class=\"muted\">The prompt version is this build&rsquo;s prompt library "
        "version. It is a property of the software, not a per-assessment record, unless "
        "an assessment stored one.</p>"
    )
    parts.append("<nav class=\"toc\"><h2>Report contents</h2><ol>")
    for number, name in SECTIONS:
        parts.append("<li><a href=\"#section-{0}\">{1}</a></li>".format(number, _h(name)))
    parts.append("</ol></nav></header>")
    return "".join(parts)


def _html_section_1(report: ReportData) -> str:
    concluded = report.concluded_controls()
    pending = report.pending_controls()
    unassessed = report.unassessed_controls()
    divergent = report.divergent_controls()
    parts = [
        "<p>This project covers {0} control(s): {1} assessed by the system, {2} with an "
        "auditor&rsquo;s decision recorded, {3} still {4}, and {5} not assessed at "
        "all.</p>".format(
            len(report.controls),
            len(report.assessed_controls()),
            len(concluded),
            len(pending),
            _h(PENDING_LABEL),
            len(unassessed),
        ),
        "<p class=\"muted\"><strong>Counting basis.</strong> {0}</p>".format(_h(_COUNTING_BASIS)),
        "<h3>Audit conclusions <span class=\"tag tag-human\">{0}</span></h3>".format(_h(HUMAN_LABEL)),
    ]
    if not concluded:
        parts.append(
            "<div class=\"callout callout-pending\"><strong>No control in this project has "
            "an auditor decision recorded, so this report states no audit conclusion.</strong> "
            "Everything below is AI-generated material awaiting review.</div>"
        )
    else:
        counts = report.final_status_counts()
        rows = [
            [_badge(status, "status"), str(counts.get(status, 0))]
            for status in AssessmentStatus.values()
        ]
        for key in sorted(k for k in counts if k not in set(AssessmentStatus.values())):
            rows.append([_h(key), str(counts[key])])
        rows.append(["<strong>Total concluded</strong>", "<strong>{0}</strong>".format(len(concluded))])
        parts.append(_html_table(["Final auditor status", "Controls"], rows))
        deficiencies = report.confirmed_deficiencies()
        parts.append(
            "<p>Auditor-confirmed deficiencies: <strong>{0}</strong> "
            "(<strong>{1}</strong> rated HIGH or CRITICAL).</p>".format(
                len(deficiencies), len(report.confirmed_high_risk())
            )
        )
        if deficiencies:
            parts.append(
                _html_list(
                    [
                        "{0} - {1} - {2} (risk {3})".format(
                            line.control_ref,
                            line.name,
                            line.final_status,
                            line.final_risk_level or RiskLevel.NOT_RATED.value,
                        )
                        for line in sorted(deficiencies, key=_risk_sort_key)
                    ]
                )
            )

    parts.append("<h3>Controls excluded from the conclusions above</h3>")
    if pending:
        parts.append(
            "<div class=\"callout callout-pending\">The following {0} control(s) have an "
            "AI-generated assessment that <strong>no auditor has reviewed</strong>. They are "
            "marked {1} and are excluded from every figure in this section. The AI status is "
            "shown only so the reader knows what is waiting; it is not a conclusion.</div>".format(
                len(pending), _h(PENDING_LABEL)
            )
        )
        parts.append(
            _html_table(
                ["Control", "Name", "AI-generated status (advisory)", "Review state"],
                [
                    [
                        _h(line.control_ref),
                        _h(line.name),
                        _badge(line.ai_status, "status"),
                        "<span class=\"badge badge-pending\">{0}</span>".format(_h(PENDING_LABEL)),
                    ]
                    for line in pending
                ],
            )
        )
    else:
        parts.append("<p>Every assessed control in this project has an auditor decision recorded.</p>")
    if unassessed:
        parts.append(
            "<p>A further {0} control(s) are in scope but have not been assessed by the "
            "system: {1}.</p>".format(
                len(unassessed), _h(", ".join(line.control_ref for line in unassessed))
            )
        )

    parts.append(
        "<h3>AI-generated status distribution <span class=\"tag tag-ai\">advisory - not a "
        "conclusion</span></h3>"
    )
    if report.assessed_controls():
        ai_counts = report.ai_status_counts()
        parts.append(
            _html_table(
                ["AI-generated status", "Controls"],
                [
                    [_badge(status, "status"), str(ai_counts.get(status, 0))]
                    for status in AssessmentStatus.values()
                ],
            )
        )
        parts.append(
            "<p class=\"muted\">These counts include controls that are {0}. They describe "
            "what the system proposed, not what the audit found.</p>".format(_h(PENDING_LABEL))
        )
    else:
        parts.append("<p class=\"muted\">No assessments have been produced for this project.</p>")

    parts.append("<h3>AI / auditor divergence</h3>")
    if not concluded:
        parts.append("<p class=\"muted\">Not measurable: no auditor decisions have been recorded.</p>")
    elif divergent:
        parts.append(
            "<p>The auditor reached a different conclusion from the system on "
            "<strong>{0} of {1}</strong> reviewed control(s) (agreement rate {2}).</p>".format(
                len(divergent), len(concluded), _h(_percent(report.agreement_rate()))
            )
        )
        parts.append(
            _html_table(
                ["Control", AI_LABEL, HUMAN_LABEL, "Divergence"],
                [
                    [
                        _h(line.control_ref),
                        _badge(line.ai_status, "status"),
                        _badge(line.final_status, "status"),
                        "<span class=\"badge badge-divergent\">DIVERGENT</span>",
                    ]
                    for line in divergent
                ],
            )
        )
    else:
        parts.append(
            "<p>On all {0} reviewed control(s) the auditor&rsquo;s final status matched the "
            "AI-generated status (agreement rate {1}). Agreement is not evidence that either "
            "conclusion is correct.</p>".format(len(concluded), _h(_percent(report.agreement_rate())))
        )
    return "".join(parts)


def _html_section_2(report: ReportData) -> str:
    rows = [
        ["Audit area", report.audit_area],
        ["Period start", _fmt_date(report.period_start)],
        ["Period end", _fmt_date(report.period_end)],
        ["Controls in scope", sum(1 for c in report.controls if c.in_scope)],
        ["Controls reported on", len(report.controls)],
        ["Evidence files examined", len(report.evidence)],
        ["Evidence chunks indexed", report.evidence_stats.get("evidence_chunks", 0)],
    ]
    parts = [
        _html_table(
            ["Field", "Value"],
            [["<strong>{0}</strong>".format(_h(key)), _h(value)] for key, value in rows],
            css_class="meta",
        ),
        _html_field("Project description", report.project_description, "Not provided."),
        _html_field("Scope note", report.scope_note, "Not provided."),
        "<h3>Controls in scope</h3>",
    ]
    in_scope = [line for line in report.controls if line.in_scope]
    if in_scope:
        parts.append(
            _html_table(
                ["Control", "Name", "Category", "Type", "Frequency", "Inherent risk", "Scope note"],
                [
                    [
                        _h(line.control_ref),
                        _h(line.name),
                        _h(line.category),
                        _h(line.control_type),
                        _h(line.control_frequency),
                        _badge(line.inherent_risk, "risk"),
                        _h(_one_line(line.scope_note)),
                    ]
                    for line in in_scope
                ],
            )
        )
    else:
        parts.append("<p class=\"muted\">No controls are currently scoped to this project.</p>")
    out_of_scope = [line for line in report.controls if not line.in_scope]
    if out_of_scope:
        parts.append(
            "<p>The following control(s) are reported because they were assessed during this "
            "project but have since been removed from its scope: {0}.</p>".format(
                _h(", ".join(line.control_ref for line in out_of_scope))
            )
        )
    parts.append("<h3>What was not in scope</h3>")
    parts.append(
        _html_list(
            [
                "Only the evidence files listed in Section 4 were examined. No system, "
                "directory or database was accessed directly.",
                "No control was re-performed, no transaction was re-executed and no "
                "configuration was observed live.",
                "No interviews, walkthroughs or management representations were obtained or "
                "corroborated.",
                "No statistical sample was drawn. Where individual records are quoted, they "
                "are records the retrieval layer surfaced, not a drawn sample.",
                "No assessment of design effectiveness over the full period was performed; "
                "the evidence is a point-in-time extract unless the file itself states "
                "otherwise.",
            ]
        )
    )
    return "".join(parts)


def _html_section_3(report: ReportData) -> str:
    rows = []
    for line in report.controls:
        if line.status_diverges:
            agreement = "<span class=\"badge badge-divergent\">DIVERGENT</span>"
        elif line.has_final_decision:
            agreement = "<span class=\"badge badge-agreed\">Agreed</span>"
        elif line.is_assessed:
            agreement = "<span class=\"badge badge-pending\">Not reviewed</span>"
        else:
            agreement = "<span class=\"badge badge-none\">Not assessed</span>"
        final = (
            _badge(line.final_status, "status")
            if line.has_final_decision and line.final_status
            else "<span class=\"badge badge-pending\">{0}</span>".format(_h(line.display_final_status))
        )
        rows.append(
            [
                _h(line.control_ref),
                _h(line.name),
                _h(line.category),
                _badge(line.ai_status, "status") if line.ai_status else _NO_VALUE_BADGE,
                final,
                agreement,
                _badge(line.ai_risk_level, "risk") if line.ai_risk_level else _NO_VALUE_BADGE,
                _badge(line.final_risk_level, "risk") if line.final_risk_level else _NO_VALUE_BADGE,
            ]
        )
    parts = [
        "<p>The AI conclusion and the auditor&rsquo;s conclusion are shown side by side for "
        "every control. They are separate records and are never merged. A control marked "
        "<strong>{0}</strong> has no auditor conclusion at all.</p>".format(_h(PENDING_LABEL)),
        _html_table(
            [
                "Control",
                "Name",
                "Category",
                AI_LABEL,
                HUMAN_LABEL,
                "Agreement",
                "AI risk",
                "Final risk",
            ],
            rows,
            css_class="controls",
        ),
    ]
    diverging = report.divergent_controls()
    if diverging:
        parts.append(
            "<div class=\"callout callout-divergent\"><strong>Divergence.</strong> {0} "
            "control(s) where the auditor did not accept the AI status: {1}. The "
            "auditor&rsquo;s status governs.</div>".format(
                len(diverging), _h(", ".join(line.control_ref for line in diverging))
            )
        )
    return "".join(parts)


def _html_section_4(report: ReportData) -> str:
    stats = report.evidence_stats or {}
    parts = [
        "<p>{0} evidence file(s), {1} in total, producing {2} indexed evidence chunk(s). The "
        "SHA-256 recorded against each file was computed over the stored bytes at the moment "
        "of upload.</p>".format(
            len(report.evidence),
            _h(_fmt_bytes(int(stats.get("total_bytes", 0) or 0))),
            _h(stats.get("evidence_chunks", 0)),
        )
    ]
    if not report.evidence:
        parts.append(
            "<div class=\"callout callout-pending\"><strong>No evidence files have been "
            "uploaded to this project.</strong></div>"
        )
        return "".join(parts)

    parts.append(
        _html_table(
            ["#", "Filename", "Type", "Size", "SHA-256", "Uploaded (UTC)", "Uploaded by", "Parse status"],
            [
                [
                    str(index),
                    _h(item.filename),
                    _h(item.evidence_type),
                    _h(_fmt_bytes(item.size_bytes)),
                    "<code class=\"hash\">{0}</code>".format(_h(item.sha256 or "not recorded")),
                    _h(_fmt_dt(item.uploaded_at)),
                    _h(_dash(item.uploaded_by)),
                    _badge(item.parse_status, "parse"),
                ]
                for index, item in enumerate(report.evidence, start=1)
            ],
            css_class="evidence",
        )
    )
    parts.append("<h3>Evidence integrity register</h3>")
    for index, item in enumerate(report.evidence, start=1):
        rows = [
            ["SHA-256", "<code class=\"hash\">{0}</code>".format(_h(item.sha256 or "not recorded"))],
            ["Size", _h("{0} ({1} bytes)".format(_fmt_bytes(item.size_bytes), item.size_bytes))],
            ["Uploaded", _h("{0} by {1}".format(_fmt_dt(item.uploaded_at), _dash(item.uploaded_by)))],
            ["Evidence type", _h(_dash(item.evidence_type))],
            ["Parse status", _badge(item.parse_status, "parse")],
            [
                "Extracted",
                _h(
                    "{0} page(s), {1} data row(s), {2} chunk(s), {3} characters".format(
                        item.page_count, item.row_count, item.chunk_count, item.char_count
                    )
                ),
            ],
        ]
        if item.description:
            rows.append(["Description", _h(item.description)])
        block = ["<div class=\"card\"><h4>{0}. {1}</h4>".format(index, _h(item.filename))]
        block.append(
            _html_table(
                ["Field", "Value"],
                [["<strong>{0}</strong>".format(_h(key)), value] for key, value in rows],
                css_class="meta",
            )
        )
        if item.is_synthetic:
            block.append(
                "<div class=\"callout callout-synthetic\"><strong>Synthetic file.</strong> "
                "Generated by this project&rsquo;s dataset generator; it describes no real "
                "organisation, system or person.</div>"
            )
        if item.parse_error:
            block.append(
                "<div class=\"callout callout-error\"><strong>Parse error:</strong> {0}</div>".format(
                    _h(item.parse_error)
                )
            )
        if item.warnings:
            block.append("<p class=\"field-label\">Parser warnings</p>")
            block.append(_html_list(item.warnings))
        block.append("</div>")
        parts.append("".join(block))

    failed = [item for item in report.evidence if item.parse_status not in ("PARSED",)]
    if failed:
        parts.append(
            "<div class=\"callout callout-error\"><strong>{0} file(s) were not fully "
            "parsed</strong> and their content was therefore not available to the "
            "assessment: {1}. Any conclusion that depended on them is unsupported.</div>".format(
                len(failed), _h(", ".join(item.filename for item in failed))
            )
        )
    parts.append(
        "<p class=\"muted\">The hash chain above establishes that each file is unchanged "
        "since it was uploaded to this system, and nothing more. It does not establish who "
        "produced the file, whether it was altered before upload, or whether it is a complete "
        "and faithful export of the underlying system. See Section 10.</p>"
    )
    return "".join(parts)


def _html_section_5(report: ReportData) -> str:
    parts = [
        "<div class=\"callout callout-ai\">Everything in this section is <strong>{0}</strong>. "
        "Nothing here is an audit conclusion. The auditor&rsquo;s decisions are in "
        "Section 8.</div>".format(_h(AI_LABEL))
    ]
    assessed = report.assessed_controls()
    if not assessed:
        parts.append("<p class=\"muted\">No assessments have been produced for this project.</p>")
        return "".join(parts)

    for line in assessed:
        assessment = line.assessment
        if assessment is None:  # pragma: no cover
            continue
        block = [
            "<div class=\"card ai-card\">",
            "<h3>{0} &ndash; {1}</h3>".format(_h(line.control_ref), _h(line.name)),
            "<p class=\"tag tag-ai\">{0}</p>".format(_h(AI_LABEL)),
        ]
        if line.is_pending_review:
            block.append(
                "<div class=\"callout callout-pending\"><strong>{0}.</strong> No auditor has "
                "reviewed this assessment.</div>".format(_h(PENDING_LABEL))
            )
        else:
            marker = "differs from" if line.status_diverges else "matches"
            block.append(
                "<div class=\"callout callout-{0}\">Reviewed by an auditor. The "
                "auditor&rsquo;s final status ({1}) <strong>{2}</strong> the AI status below. "
                "See Section 8.</div>".format(
                    "divergent" if line.status_diverges else "agreed",
                    _h(line.final_status),
                    _h(marker),
                )
            )
        meta_rows = [
            ["AI-generated status", _badge(assessment.status, "status")],
            [
                "AI-suggested risk",
                "{0} <span class=\"muted\">score {1:.1f}/100</span>".format(
                    _badge(assessment.risk_level, "risk"), assessment.risk_score
                ),
            ],
            ["Evidence sufficiency", _h(assessment.evidence_sufficiency)],
            ["Model confidence", _h(assessment.confidence)],
            ["Assessment mode", _h(assessment.experiment_mode)],
            [
                "Provider / model",
                _h("{0} / {1}".format(_dash(assessment.llm_provider), _dash(assessment.llm_model))),
            ],
            ["Citations recorded", _h(len(assessment.citations))],
            ["Assessment generated", _h(_fmt_dt(assessment.created_at))],
        ]
        block.append(
            _html_table(
                ["Field", "Value"],
                [["<strong>{0}</strong>".format(_h(key)), value] for key, value in meta_rows],
                css_class="meta",
            )
        )
        block.append(_html_field("Control requirement", line.objective, "Not recorded."))
        block.append(_html_field("What the evidence shows (AI-generated)", assessment.assessment))
        block.append(_html_field("Potential finding (AI-generated)", assessment.finding, "None stated."))
        block.append(
            _html_field("Risk if the control does not operate (AI-generated)", assessment.risk, "None stated.")
        )
        block.append(_html_field("Reasoning (AI-generated)", assessment.reasoning, "None recorded."))
        block.append(
            "<div class=\"field\"><div class=\"field-label\">Inferences the AI drew "
            "beyond the literal evidence</div>"
        )
        block.append(_html_list(assessment.inferences, "None declared."))
        block.append("</div>")
        block.append(
            "<div class=\"field\"><div class=\"field-label\">Checks a human auditor "
            "must still perform</div>"
        )
        block.append(
            _html_list(
                assessment.human_verification_required,
                "None declared by the model. This does not mean none are required.",
            )
        )
        block.append("</div>")
        block.append(
            "<div class=\"field\"><div class=\"field-label\">Evidence the AI reported "
            "as missing</div>"
        )
        block.append(_html_list(assessment.missing_evidence, "None declared."))
        block.append("</div>")
        if assessment.error:
            block.append(
                "<div class=\"callout callout-error\"><strong>Run error recorded:</strong> "
                "{0}</div>".format(_h(assessment.error))
            )
        block.append(_html_validation_block(assessment))
        block.append("</div>")
        parts.append("".join(block))
    return "".join(parts)


def _html_validation_block(assessment: AssessmentLine) -> str:
    validation = assessment.validation or {}
    items: List[str] = []
    if not validation and not assessment.citations:
        items.append(
            "No citation validation was recorded for this assessment. Its statements have "
            "not been checked against the evidence by this system."
        )
    else:
        total = int(validation.get("total", len(assessment.citations)) or 0)
        items.append(
            "Citations: {0} total - {1} verified, {2} partial, {3} unverified, {4} not "
            "matched to any supplied evidence.".format(
                total,
                int(validation.get("verified", 0) or 0),
                int(validation.get("partial", 0) or 0),
                int(validation.get("unverified", 0) or 0),
                int(validation.get("fabricated", 0) or 0),
            )
        )
        if "grounding_rate" in validation:
            items.append(
                "Grounding rate: {0} ({1}).".format(
                    validation.get("grounding_rate"),
                    validation.get("grounding_rate_definition", "definition not recorded"),
                )
            )
        fabricated = int(validation.get("fabricated", 0) or 0)
        if fabricated:
            items.append(
                "{0} citation(s) could not be matched to evidence supplied to the model. "
                "Treat the associated statements as unsupported.".format(fabricated)
            )
        for claim in assessment.unsupported_claims:
            items.append("Unsupported numeric claim detected in the AI text: {0}".format(claim))
        if assessment.rails_applied:
            items.append("Safety rails applied: {0}.".format(", ".join(assessment.rails_applied)))
        if validation.get("status_downgraded"):
            items.append(
                "The AI status was automatically downgraded from {0} because it was not "
                "supported by verified evidence.".format(
                    validation.get("original_status", "its original value")
                )
            )
    return (
        "<div class=\"field validation\"><div class=\"field-label\">Automated grounding "
        "check</div>{0}</div>"
    ).format(_html_list(items, "Not recorded."))


def _html_section_6(report: ReportData) -> str:
    parts = [
        "<div class=\"callout callout-model\"><strong>{0}</strong></div>".format(_h(RISK_MODEL_LABEL)),
        "<p>Ratings are computed deterministically from five weighted factors, not taken from "
        "the language model. Bands on the 0-100 scale: {0}. Scores are comparable within this "
        "system only.</p>".format(
            _h(
                ", ".join(
                    "{0} from {1:.0f}".format(name, value)
                    for name, value in sorted(BAND_THRESHOLDS.items(), key=lambda kv: kv[1])
                )
            )
        ),
    ]
    assessed = report.assessed_controls()
    if not assessed:
        parts.append("<p class=\"muted\">No risk ratings have been produced for this project.</p>")
        return "".join(parts)

    rows = []
    for line in sorted(assessed, key=_risk_sort_key):
        assessment = line.assessment
        if assessment is None:  # pragma: no cover
            continue
        if line.risk_diverges:
            agreement = "<span class=\"badge badge-divergent\">DIVERGENT</span>"
        elif line.has_final_decision:
            agreement = "<span class=\"badge badge-agreed\">Agreed</span>"
        else:
            agreement = "<span class=\"badge badge-none\">-</span>"
        rows.append(
            [
                _h(line.control_ref),
                _badge(assessment.status, "status"),
                "{0} <span class=\"muted\">{1:.1f}</span>".format(
                    _badge(assessment.risk_level, "risk"), assessment.risk_score
                ),
                _badge(line.final_risk_level, "risk")
                if line.final_risk_level
                else "<span class=\"badge badge-pending\">{0}</span>".format(_h(PENDING_LABEL)),
                agreement,
                _h(_top_factors(assessment.risk_factors)),
            ]
        )
    parts.append(
        _html_table(
            [
                "Control",
                "AI status",
                "Prototype risk rating (AI run)",
                "Final auditor risk",
                "Agreement",
                "Largest score contributors",
            ],
            rows,
        )
    )
    adjusted = [
        (line, _risk_adjustments(line.assessment.risk_factors))
        for line in assessed
        if line.assessment is not None and _risk_adjustments(line.assessment.risk_factors)
    ]
    if adjusted:
        parts.append("<h3>Score adjustments applied by the risk model</h3>")
        for line, adjustments in adjusted:
            parts.append("<p class=\"field-label\">{0}</p>".format(_h(line.control_ref)))
            parts.append(_html_list(adjustments))
    pending = [line for line in assessed if not line.has_final_decision]
    if pending:
        parts.append(
            "<p class=\"muted\">Risk ratings for {0} control(s) are shown as {1}: the rating "
            "in the AI column has not been confirmed by an auditor.</p>".format(
                len(pending), _h(PENDING_LABEL)
            )
        )
    return "".join(parts)


def _html_section_7(report: ReportData) -> str:
    parts = [
        "<p>Every reference the system attached to a finding, resolved to its file, its "
        "location within that file, the verbatim text quoted, and the result of mechanically "
        "re-checking that quotation against the stored evidence.</p>"
    ]
    pairs = report.all_citations()
    if not pairs:
        parts.append(
            "<div class=\"callout callout-error\"><strong>No evidence references were "
            "recorded for this project.</strong> No finding in this report is traceable to a "
            "specific passage of evidence.</div>"
        )
        return "".join(parts)

    verdicts = report.citation_verdict_counts()
    parts.append(
        _html_table(
            ["Verification verdict", "Citations", "Meaning"],
            [
                [
                    _badge(CitationVerdict.VERIFIED.value, "verdict"),
                    str(verdicts.get(CitationVerdict.VERIFIED.value, 0)),
                    "Quoted text was found in the cited evidence chunk.",
                ],
                [
                    _badge(CitationVerdict.PARTIAL.value, "verdict"),
                    str(verdicts.get(CitationVerdict.PARTIAL.value, 0)),
                    "Quoted text only partially matches the cited chunk.",
                ],
                [
                    _badge(CitationVerdict.UNVERIFIED.value, "verdict"),
                    str(verdicts.get(CitationVerdict.UNVERIFIED.value, 0)),
                    "No quotation supplied, or it could not be checked.",
                ],
                [
                    _badge(CitationVerdict.FABRICATED.value, "verdict"),
                    str(verdicts.get(CitationVerdict.FABRICATED.value, 0)),
                    "Could not be matched to any evidence supplied to the model.",
                ],
            ],
        )
    )
    parts.append(
        "<p class=\"muted\">A VERIFIED verdict means the quoted characters are present in the "
        "chunk the model pointed at. It does not mean the quotation was read in context or "
        "that it supports the conclusion drawn from it. See Section 10.</p>"
    )

    for line in report.assessed_controls():
        assessment = line.assessment
        if assessment is None:  # pragma: no cover
            continue
        parts.append("<h3>{0} &ndash; {1}</h3>".format(_h(line.control_ref), _h(line.name)))
        if not assessment.citations:
            parts.append(
                "<div class=\"callout callout-error\"><strong>No evidence references were "
                "recorded for this control.</strong> The AI-generated statements in Section 5 "
                "are not traceable to a specific passage of evidence.</div>"
            )
            continue
        for citation in assessment.citations:
            quote, clipped = _clip_quote(citation.quoted_text)
            block = [
                "<div class=\"card citation-card verdict-{0}\">".format(_slug(citation.verdict)),
                "<div class=\"citation-head\"><strong>[{0}] {1}</strong> "
                "<span class=\"locator\">{2}</span> {3}</div>".format(
                    citation.order_index + 1,
                    _h(_dash(citation.filename)),
                    _h(_dash(_locator_display(citation.filename, citation.locator_text))),
                    _badge(citation.verdict, "verdict"),
                ),
            ]
            if quote.strip():
                block.append(
                    "<blockquote class=\"quote\">{0}{1}</blockquote>".format(
                        _h(quote).replace("\n", "<br>"),
                        "<br><em>[quotation truncated for the report]</em>" if clipped else "",
                    )
                )
            else:
                block.append("<blockquote class=\"quote muted\">(no verbatim text was quoted)</blockquote>")
            details = [citation.verification_sentence]
            details.append(
                "Evidence chunk id: {0}{1}".format(
                    citation.chunk_id if citation.chunk_id is not None else "not supplied",
                    "" if citation.resolved else " (does not resolve to a stored evidence chunk)",
                )
            )
            if citation.supports:
                details.append("Cited as: {0}".format(citation.supports))
            if citation.relevance:
                details.append("Stated relevance (AI-generated): {0}".format(citation.relevance))
            if citation.note:
                details.append("Validator note: {0}".format(citation.note))
            block.append(_html_list(details))
            block.append("</div>")
            parts.append("".join(block))
        unverified = assessment.unverified_citations
        if unverified:
            parts.append(
                "<div class=\"callout callout-error\"><strong>{0} of {1} reference(s) for this "
                "control could not be fully verified.</strong> An auditor must open the source "
                "file before relying on them.</div>".format(len(unverified), len(assessment.citations))
            )
    return "".join(parts)


def _html_section_8(report: ReportData) -> str:
    parts = [
        "<div class=\"callout callout-human\">Everything in this section is <strong>{0}</strong>. "
        "These are the only conclusive statements in this report.</div>".format(_h(HUMAN_LABEL))
    ]
    concluded = report.concluded_controls()
    pending = report.pending_controls()

    if not concluded:
        parts.append(
            "<div class=\"callout callout-pending\"><strong>No auditor decision has been "
            "recorded for any control in this project.</strong></div>"
        )
    else:
        parts.append(
            _html_table(
                ["Control", "Reviewer", "Decision", HUMAN_LABEL, "Final risk", AI_LABEL, "Divergence", "Reviewed at"],
                [
                    [
                        _h(line.control_ref),
                        _h(line.review.reviewer_name if line.review else ""),
                        _h(line.review.decision if line.review else ""),
                        _badge(line.final_status, "status"),
                        _badge(line.final_risk_level, "risk"),
                        _badge(line.ai_status, "status"),
                        "<span class=\"badge badge-divergent\">DIVERGENT</span>"
                        if line.status_diverges
                        else "<span class=\"badge badge-agreed\">Agreed</span>",
                        _h(_fmt_dt(line.review.created_at) if line.review else ""),
                    ]
                    for line in concluded
                ],
            )
        )
        for line in concluded:
            review = line.review
            if review is None:  # pragma: no cover
                continue
            rows = [
                ["Reviewer", _h(_dash(review.reviewer_name))],
                ["Decision", _h(review.decision)],
                ["Final status", _badge(review.final_status, "status")],
                ["Final risk level", _badge(review.final_risk_level, "risk")],
                ["AI-generated status (for comparison)", _badge(line.ai_status, "status")],
                ["AI-suggested risk (for comparison)", _badge(line.ai_risk_level, "risk")],
                [
                    "Status agreement",
                    "<span class=\"badge badge-divergent\">DIVERGENT</span>"
                    if line.status_diverges
                    else "<span class=\"badge badge-agreed\">Agreed</span>",
                ],
                [
                    "Risk agreement",
                    "<span class=\"badge badge-divergent\">DIVERGENT</span>"
                    if line.risk_diverges
                    else "<span class=\"badge badge-agreed\">Agreed</span>",
                ],
                ["Reviewed at", _h(_fmt_dt(review.created_at))],
                [
                    "Time spent",
                    _h("{0:.0f} s".format(review.review_seconds) if review.review_seconds else "not recorded"),
                ],
                [
                    "Usefulness rating of the AI output",
                    _h("{0}/5".format(review.usefulness_rating) if review.usefulness_rating else "not recorded"),
                ],
            ]
            block = [
                "<div class=\"card human-card\">",
                "<h3>{0} &ndash; {1}</h3>".format(_h(line.control_ref), _h(line.name)),
                "<p class=\"tag tag-human\">{0}</p>".format(_h(HUMAN_LABEL)),
                _html_table(
                    ["Field", "Value"],
                    [["<strong>{0}</strong>".format(_h(key)), value] for key, value in rows],
                    css_class="meta",
                ),
                _html_field("Auditor's finding", review.final_finding, "Not restated by the auditor."),
                _html_field("Auditor's recommendation", review.final_recommendation, "Not stated by the auditor."),
                _html_field("Auditor's comments", review.comments, "None."),
            ]
            if review.requested_evidence:
                block.append(
                    "<div class=\"field\"><div class=\"field-label\">Further evidence requested "
                    "by the auditor</div>{0}</div>".format(_html_list(review.requested_evidence))
                )
            if review.flagged_hallucination:
                block.append(
                    "<div class=\"callout callout-error\"><strong>The auditor flagged this AI "
                    "output as containing a hallucination.</strong> {0}</div>".format(
                        _h(review.hallucination_note or "No note recorded.")
                    )
                )
            if line.status_diverges:
                block.append(
                    "<div class=\"callout callout-divergent\"><strong>Divergence.</strong> The "
                    "system proposed {0}; the auditor concluded {1}. The auditor&rsquo;s status "
                    "governs and the AI status is retained only as a research record.</div>".format(
                        _h(line.ai_status), _h(line.final_status)
                    )
                )
            block.append("</div>")
            parts.append("".join(block))

    parts.append("<h3>Controls awaiting auditor review</h3>")
    if pending:
        parts.append(
            "<div class=\"callout callout-pending\">{0} control(s) are {1}. No conclusion has "
            "been reached on them and they are excluded from the executive summary.</div>".format(
                len(pending), _h(PENDING_LABEL)
            )
        )
        parts.append(
            _html_table(
                ["Control", "Name", "AI-generated status (advisory)", "Assessment generated", "Review state"],
                [
                    [
                        _h(line.control_ref),
                        _h(line.name),
                        _badge(line.ai_status, "status"),
                        _h(_fmt_dt(line.assessment.created_at) if line.assessment else ""),
                        _h(_pending_state(line)),
                    ]
                    for line in pending
                ],
            )
        )
    else:
        parts.append("<p>None. Every assessed control has an auditor decision recorded.</p>")
    return "".join(parts)


def _html_section_9(report: ReportData) -> str:
    parts = [
        "<p>Recommendations are listed highest risk first. Each one is labelled with its "
        "source: only a recommendation labelled <strong>{0}</strong> has been adopted by an "
        "auditor.</p>".format(_h(HUMAN_LABEL))
    ]
    printed = 0
    for line in sorted(report.controls, key=_risk_sort_key):
        review = line.review if line.has_final_decision else None
        assessment = line.assessment
        human_text = review.final_recommendation.strip() if review and review.final_recommendation else ""
        ai_text = assessment.recommendation.strip() if assessment and assessment.recommendation else ""
        if not human_text and not ai_text:
            continue
        printed += 1
        block = [
            "<div class=\"card\">",
            "<h3>{0} &ndash; {1} {2}</h3>".format(
                _h(line.control_ref), _h(line.name), _badge(line.effective_risk_level, "risk")
            ),
        ]
        if human_text:
            block.append(
                "<div class=\"field human-card\"><div class=\"field-label tag tag-human\">{0}</div>"
                "{1}</div>".format(_h(HUMAN_LABEL), _html_paragraphs(human_text))
            )
        if ai_text:
            block.append(
                "<div class=\"field ai-card\"><div class=\"field-label tag tag-ai\">{0}</div>"
                "{1}</div>".format(_h(AI_LABEL), _html_paragraphs(ai_text))
            )
            if not human_text:
                if line.is_pending_review:
                    block.append(
                        "<div class=\"callout callout-pending\">This control is {0}. The "
                        "recommendation above is a machine proposal that no auditor has "
                        "adopted.</div>".format(_h(PENDING_LABEL))
                    )
                else:
                    block.append(
                        "<div class=\"callout callout-pending\">The auditor reviewed this "
                        "control but did not record a recommendation of their own. The proposal "
                        "above remains AI-generated and unadopted.</div>"
                    )
        block.append("</div>")
        parts.append("".join(block))
    if not printed:
        parts.append("<p class=\"muted\">No recommendations have been recorded for this project.</p>")
    return "".join(parts)


def _html_section_10(report: ReportData) -> str:
    parts = []
    for heading, body in _limitations(report):
        parts.append(
            "<div class=\"limitation\"><h3>{0}</h3><p>{1}</p></div>".format(_h(heading), _h(body))
        )
    return "".join(parts)


#: Light, print-first stylesheet. Kept inline because a report that needs the network to
#: render correctly cannot be archived as evidence of anything.
_REPORT_CSS = """
:root {
  --ink: #1b2430;
  --ink-soft: #55637a;
  --line: #d9dee8;
  --paper: #ffffff;
  --wash: #f5f7fa;
  --ai: #4c6ef5;
  --human: #0b7a55;
  --pending: #b26a00;
  --divergent: #b3261e;
  --ok: #0b7a55;
  --warn: #b26a00;
  --bad: #b3261e;
  --crit: #7a1113;
  --muted: #6b7688;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--wash);
  color: var(--ink);
  font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, Roboto, Helvetica, Arial, sans-serif;
  font-size: 14px;
  line-height: 1.55;
}
.report {
  max-width: 60rem;
  margin: 0 auto;
  padding: 2.5rem 2rem 4rem;
  background: var(--paper);
}
h1 { font-size: 1.9rem; margin: 0 0 0.4rem; line-height: 1.25; }
h2 { font-size: 1.35rem; margin: 0 0 0.9rem; padding-bottom: 0.35rem; border-bottom: 2px solid var(--ink); }
h3 { font-size: 1.05rem; margin: 1.6rem 0 0.5rem; }
h4 { font-size: 0.98rem; margin: 0 0 0.6rem; }
p { margin: 0.5rem 0; }
ul, ol { margin: 0.4rem 0 0.8rem; padding-left: 1.3rem; }
li { margin: 0.2rem 0; }
a { color: var(--ai); }
code.hash { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.82em; word-break: break-all; }
.muted { color: var(--muted); }
.section-number {
  display: inline-block; min-width: 1.6rem; color: var(--muted); font-variant-numeric: tabular-nums;
}
.report-section { margin-top: 2.4rem; page-break-inside: auto; }
.report-header { border-bottom: 1px solid var(--line); padding-bottom: 1.2rem; }
.toc ol { columns: 2; column-gap: 2rem; }
.toc h2 { font-size: 1rem; border: 0; margin-bottom: 0.3rem; }
.table-wrap { overflow-x: auto; margin: 0.6rem 0 1rem; }
table { border-collapse: collapse; width: 100%; font-size: 0.9rem; }
th, td { border: 1px solid var(--line); padding: 0.4rem 0.55rem; text-align: left; vertical-align: top; }
th { background: var(--wash); font-weight: 600; }
table.meta td:first-child { width: 30%; background: #fafbfd; }
tbody tr:nth-child(even) td { background: #fcfdfe; }
.card {
  border: 1px solid var(--line); border-radius: 6px; padding: 0.9rem 1.1rem;
  margin: 1rem 0; background: var(--paper); page-break-inside: avoid;
}
.ai-card { border-left: 4px solid var(--ai); }
.human-card { border-left: 4px solid var(--human); }
.citation-card { border-left: 4px solid var(--line); }
.citation-card.verdict-verified { border-left-color: var(--ok); }
.citation-card.verdict-partial { border-left-color: var(--warn); }
.citation-card.verdict-fabricated { border-left-color: var(--bad); }
.citation-head { margin-bottom: 0.4rem; }
.locator { color: var(--muted); }
.field { margin: 0.8rem 0; }
.field-label { font-weight: 600; font-size: 0.86rem; text-transform: uppercase; letter-spacing: 0.03em; color: var(--ink-soft); }
.validation ul { font-size: 0.9rem; }
blockquote.quote {
  margin: 0.5rem 0; padding: 0.6rem 0.9rem; border-left: 3px solid var(--line);
  background: var(--wash); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.85rem; white-space: pre-wrap; word-break: break-word;
}
.callout { border-radius: 5px; padding: 0.6rem 0.85rem; margin: 0.7rem 0; border-left: 4px solid var(--line); background: var(--wash); }
.callout-provenance { border-left-color: var(--ink); background: #eef1f6; }
.callout-ai { border-left-color: var(--ai); background: #eef2ff; }
.callout-human { border-left-color: var(--human); background: #e9f6f0; }
.callout-agreed { border-left-color: var(--human); background: #e9f6f0; }
.callout-pending { border-left-color: var(--pending); background: #fdf3e3; }
.callout-divergent { border-left-color: var(--divergent); background: #fdecea; }
.callout-error { border-left-color: var(--bad); background: #fdecea; }
.callout-synthetic { border-left-color: var(--muted); background: #f1f3f7; }
.callout-model { border-left-color: var(--ink-soft); background: #f1f3f7; }
.tag { display: inline-block; font-size: 0.76rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; padding: 0.1rem 0.45rem; border-radius: 3px; }
.tag-ai { color: var(--ai); background: #eef2ff; }
.tag-human { color: var(--human); background: #e9f6f0; }
.badge {
  display: inline-block; padding: 0.08rem 0.45rem; border-radius: 3px; font-size: 0.78rem;
  font-weight: 600; white-space: nowrap; border: 1px solid transparent;
}
/* Every palette rule is scoped to .badge. The verdict/status/risk class names are also
   put on containers (a citation card carries verdict-fabricated so its left edge can be
   red); without the .badge prefix those rules would repaint the whole card. */
.badge-none { color: var(--muted); background: #f1f3f7; }
.badge-pending { color: var(--pending); background: #fdf3e3; border-color: #f0d5a8; }
.badge-agreed { color: var(--ok); background: #e9f6f0; border-color: #b7e0cd; }
.badge-divergent { color: var(--divergent); background: #fdecea; border-color: #f3c2bd; }
.badge.status-effective { color: var(--ok); background: #e9f6f0; border-color: #b7e0cd; }
.badge.status-potential-deficiency { color: var(--warn); background: #fdf3e3; border-color: #f0d5a8; }
.badge.status-not-effective { color: var(--bad); background: #fdecea; border-color: #f3c2bd; }
.badge.status-insufficient-evidence { color: #1d4ed8; background: #e8eefc; border-color: #bdd0f5; }
.badge.status-not-applicable { color: var(--muted); background: #f1f3f7; border-color: #dde1e9; }
.badge.risk-low { color: var(--ok); background: #e9f6f0; border-color: #b7e0cd; }
.badge.risk-medium { color: var(--warn); background: #fdf3e3; border-color: #f0d5a8; }
.badge.risk-high { color: var(--bad); background: #fdecea; border-color: #f3c2bd; }
.badge.risk-critical { color: #ffffff; background: var(--crit); border-color: var(--crit); }
.badge.risk-not-rated { color: var(--muted); background: #f1f3f7; border-color: #dde1e9; }
.badge.verdict-verified { color: var(--ok); background: #e9f6f0; border-color: #b7e0cd; }
.badge.verdict-partial { color: var(--warn); background: #fdf3e3; border-color: #f0d5a8; }
.badge.verdict-unverified { color: var(--muted); background: #f1f3f7; border-color: #dde1e9; }
.badge.verdict-fabricated { color: #ffffff; background: var(--bad); border-color: var(--bad); }
.badge.parse-parsed { color: var(--ok); background: #e9f6f0; border-color: #b7e0cd; }
.badge.parse-pending { color: var(--warn); background: #fdf3e3; border-color: #f0d5a8; }
.badge.parse-failed, .badge.parse-unsupported { color: var(--bad); background: #fdecea; border-color: #f3c2bd; }
.limitation { margin: 1rem 0; page-break-inside: avoid; }
.limitation h3 { margin-bottom: 0.2rem; }
footer { margin-top: 3rem; padding-top: 1rem; border-top: 1px solid var(--line); color: var(--muted); font-size: 0.85rem; }
@media print {
  body { background: var(--paper); font-size: 10.5pt; }
  .report { max-width: none; padding: 0; }
  .toc { page-break-after: avoid; }
  h2 { page-break-after: avoid; }
  .card, .limitation, blockquote.quote { page-break-inside: avoid; }
  a { color: inherit; text-decoration: none; }
  /* On screen a wide table scrolls sideways; on paper it cannot, and a clipped
     column would silently drop an auditor's status from the report. Fixed layout
     plus aggressive wrapping makes every column fit the page instead. */
  .table-wrap { overflow: visible; }
  table { table-layout: fixed; width: 100%; font-size: 8.5pt; }
  th, td { overflow-wrap: anywhere; word-break: break-word; }
  /* A nowrap badge is wider than its share of a fixed-layout column, which is the
     one thing that still forces a table off the page. Measured at a content width
     narrower than A4: with this rule no table overflows. */
  .badge { white-space: normal; }
  code.hash { font-size: 7.5pt; }
  * { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
}
"""


# ---- persistence
def normalise_format(fmt: Any) -> str:
    """Accept ``md``/``markdown``/``html``/``htm``; reject anything else loudly."""
    text = str(fmt or "markdown").strip().lower()
    if text in ("md", "markdown", "text/markdown"):
        return "markdown"
    if text in ("html", "htm", "text/html"):
        return "html"
    raise service.InvalidInputError(
        "Unsupported report format {0!r}. Expected one of {1}.".format(fmt, list(SUPPORTED_FORMATS))
    )


def render_report(
    session: Session,
    project_id: int,
    generated_by: str = "",
    fmt: str = "markdown",
    title: str = "",
    include_evaluation: bool = False,
) -> str:
    """Convenience: collect and render in one call, without persisting anything."""
    resolved = normalise_format(fmt)
    data = build_report_data(
        session,
        project_id,
        generated_by=generated_by,
        title=title,
        include_evaluation=include_evaluation,
    )
    return render_markdown(data) if resolved == "markdown" else render_html(data)


def report_filename(report: ReportData, fmt: str) -> str:
    """Stable, sortable, collision-resistant filename for the on-disk copy."""
    stamp = _aware(report.generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    slug = _slug(report.project_name)[:60] or "project"
    extension = "md" if fmt == "markdown" else "html"
    return "audit_report_p{0}_{1}_{2}.{3}".format(
        report.project_id, slug, stamp.strftime("%Y%m%dT%H%M%SZ"), extension
    )


def generate_report(
    session: Session,
    project_id: int,
    generated_by: str = "",
    fmt: str = "markdown",
    title: str = "",
    include_evaluation: bool = False,
    write_file: bool = True,
) -> AuditReport:
    """Build, render, write to ``settings.report_dir`` and persist an ``AuditReport`` row.

    The rendered text is stored on the row as well as on disk. That is deliberate
    duplication: the row is what the UI and the API serve, and a report whose only copy
    is a file path stops being reproducible the moment the file is moved.
    """
    resolved = normalise_format(fmt)
    settings = get_settings()
    data = build_report_data(
        session,
        project_id,
        generated_by=generated_by,
        title=title,
        include_evaluation=include_evaluation,
    )
    content = render_markdown(data) if resolved == "markdown" else render_html(data)

    stored_path: Optional[str] = None
    if write_file:
        directory = Path(settings.report_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / report_filename(data, resolved)
        path.write_text(content, encoding="utf-8")
        stored_path = str(path)

    row = AuditReport(
        project_id=data.project_id,
        title=data.title,
        format=resolved,
        content=content,
        stored_path=stored_path,
        generated_by=data.generated_by,
        summary_stats=data.to_summary_stats(fmt=resolved),
        generated_at=data.generated_at,
    )
    session.add(row)
    session.commit()

    service.log_activity(
        session,
        entity_type="report",
        entity_id=row.id,
        action=ActivityAction.REPORT_GENERATED,
        actor=data.generated_by,
        actor_type="HUMAN",
        details={
            "format": resolved,
            "stored_path": stored_path,
            "controls_reported": len(data.controls),
            "controls_pending_review": len(data.pending_controls()),
            "controls_concluded_by_auditor": len(data.concluded_controls()),
        },
        project_id=data.project_id,
    )
    return row


__all__ = [
    "AI_LABEL",
    "HUMAN_LABEL",
    "NOT_ASSESSED_LABEL",
    "PENDING_LABEL",
    "SECTIONS",
    "SUPPORTED_FORMATS",
    "AssessmentLine",
    "CitationLine",
    "ControlLine",
    "EvidenceLine",
    "ReportData",
    "ReviewLine",
    "build_report_data",
    "generate_report",
    "normalise_format",
    "render_html",
    "render_markdown",
    "render_report",
    "report_filename",
]
