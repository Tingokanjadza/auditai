"""Canonical enumerations for the LLM-Assisted IT Audit Risk and Control Assessment System.

All enums subclass ``str`` so that they serialise transparently to JSON and can be
persisted in portable ``VARCHAR`` columns (SQLite today, PostgreSQL later) without
relying on database-native ENUM types.

Target runtime: Python 3.9 (no PEP 604 unions, no ``match`` statements).
"""

from enum import Enum
from typing import Dict, List


class StrEnum(str, Enum):
    """Base class giving every enum a stable ``str`` value and helper accessors."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)

    @classmethod
    def values(cls) -> List[str]:
        return [member.value for member in cls]

    @classmethod
    def coerce(cls, raw, default=None):
        """Best-effort conversion of arbitrary model output into a member.

        LLMs frequently return lower-case, spaced or otherwise decorated labels.
        Anything that cannot be mapped returns ``default`` rather than raising, so a
        malformed model response degrades gracefully instead of crashing an audit run.
        """
        if isinstance(raw, cls):
            return raw
        if raw is None:
            return default
        token = str(raw).strip().upper().replace(" ", "_").replace("-", "_")
        for member in cls:
            if member.value.upper() == token or member.name == token:
                return member
        return default


class AssessmentStatus(StrEnum):
    """Outcome vocabulary for an AI or human control assessment.

    ``INSUFFICIENT_EVIDENCE`` is deliberately a first-class outcome: the research
    premise of this system is that "we cannot tell from this evidence" must be
    expressible and must never be silently collapsed into a compliance verdict.
    """

    EFFECTIVE = "EFFECTIVE"
    POTENTIAL_DEFICIENCY = "POTENTIAL_DEFICIENCY"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    NOT_EFFECTIVE = "NOT_EFFECTIVE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


#: Statuses that represent an adverse conclusion about control operation.
DEFICIENCY_STATUSES = (
    AssessmentStatus.POTENTIAL_DEFICIENCY,
    AssessmentStatus.NOT_EFFECTIVE,
)


class RiskLevel(StrEnum):
    """Prototype research risk bands. Not an official industry risk framework."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"
    NOT_RATED = "NOT_RATED"


#: Ordering used for dashboards, sorting and "high risk finding" counts.
RISK_ORDER = {
    RiskLevel.NOT_RATED: 0,
    RiskLevel.LOW: 1,
    RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 3,
    RiskLevel.CRITICAL: 4,
}

HIGH_RISK_LEVELS = (RiskLevel.HIGH, RiskLevel.CRITICAL)


class EvidenceSufficiency(StrEnum):
    """How completely the supplied evidence covers what the control requires."""

    SUFFICIENT = "SUFFICIENT"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"
    NONE = "NONE"


class ConfidenceLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class HumanDecision(StrEnum):
    """Actions available to the human auditor reviewing an AI assessment."""

    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    MODIFIED = "MODIFIED"
    MORE_EVIDENCE_REQUESTED = "MORE_EVIDENCE_REQUESTED"


class ProjectStatus(StrEnum):
    PLANNING = "PLANNING"
    FIELDWORK = "FIELDWORK"
    REVIEW = "REVIEW"
    COMPLETED = "COMPLETED"
    ARCHIVED = "ARCHIVED"


class EvidenceType(StrEnum):
    """Auditor-declared classification of an uploaded artefact."""

    POLICY = "POLICY"
    STANDARD = "STANDARD"
    CONFIGURATION_EXPORT = "CONFIGURATION_EXPORT"
    SYSTEM_REPORT = "SYSTEM_REPORT"
    USER_LISTING = "USER_LISTING"
    TICKET_EXPORT = "TICKET_EXPORT"
    LOG_EXTRACT = "LOG_EXTRACT"
    SCREENSHOT_NARRATIVE = "SCREENSHOT_NARRATIVE"
    INTERVIEW_NOTES = "INTERVIEW_NOTES"
    OTHER = "OTHER"


class EvidenceProvenance(StrEnum):
    """Where an artefact came from, which is a different question from what it is.

    ``EvidenceType`` answers *what the artefact is* - a policy, a user listing, a log
    extract. This enum answers *where it came from*, and therefore how far a conclusion
    drawn from it may be trusted. The two are independent: a privileged account listing
    fabricated by this project's generators and a privileged account listing exported
    from a client's identity provider are the same type and are emphatically not the
    same evidence.

    The distinction exists because this repository contains, and will keep containing,
    material that *looks* like organisational evidence and is not. A reconstruction of a
    publicly documented failure pattern is useful precisely because it reads like the
    real thing; that is also exactly what makes it dangerous if it is ever quoted as
    though it described a real organisation. Recording provenance on the row - rather
    than relying on a filename convention or a reader's memory - is what makes that
    mistake detectable instead of invisible.
    """

    #: Fabricated by this project's own generators for testing and demonstration.
    #: Represents no real organisation, system, person, incident or audit.
    SYNTHETIC = "SYNTHETIC"

    #: A synthetic reconstruction derived from publicly documented facts about a real,
    #: historically documented incident or failure pattern. The *pattern* is real and
    #: publicly reported; the files, names, accounts, dates and figures are invented to
    #: illustrate it. This is **not** the original organisation's evidence, was never
    #: obtained from that organisation, and must never be cited as though it were.
    HISTORICAL_PUBLIC = "HISTORICAL_PUBLIC"

    #: Real evidence obtained from a real audited entity. Never present in this
    #: repository, and never produced by any generator in it. The member exists so that
    #: a deployment which one day does hold real evidence can say so explicitly, and so
    #: that "is this real?" is always an answered question rather than an assumption.
    ORGANISATIONAL = "ORGANISATIONAL"


#: The disclosure sentence every surface - UI badge tooltip, evidence detail panel,
#: report appendix - must show for an artefact of this provenance. Defined once, here,
#: because a disclaimer that is re-worded per screen is a disclaimer that will eventually
#: be dropped from one of them.
PROVENANCE_LABELS: Dict[str, str] = {
    EvidenceProvenance.SYNTHETIC.value: (
        "Synthetic test data generated by this research prototype. It represents no real "
        "organisation, system, person or audit."
    ),
    EvidenceProvenance.HISTORICAL_PUBLIC.value: (
        "Synthetic research dataset derived from publicly documented historical facts. It "
        "reconstructs a publicly reported failure pattern using invented organisation and "
        "account names; it is not the original organisation's evidence."
    ),
    EvidenceProvenance.ORGANISATIONAL.value: (
        "Real evidence supplied by an audited entity. Handle under the engagement's "
        "confidentiality terms."
    ),
}

#: Short pill text, for places where only a few characters fit. A badge drawn from this
#: map is never the whole disclosure: the sentence in :data:`PROVENANCE_LABELS` must be
#: reachable from wherever the badge appears.
PROVENANCE_BADGES: Dict[str, str] = {
    EvidenceProvenance.SYNTHETIC.value: "SYNTHETIC",
    EvidenceProvenance.HISTORICAL_PUBLIC.value: "HISTORICAL RECONSTRUCTION",
    EvidenceProvenance.ORGANISATIONAL.value: "ORGANISATIONAL EVIDENCE",
}

#: Provenances that did not come from an audited entity. Nothing carrying one of these
#: may be reported as evidence about a real organisation, whatever its evidence type.
NON_ORGANISATIONAL_PROVENANCE = (
    EvidenceProvenance.SYNTHETIC,
    EvidenceProvenance.HISTORICAL_PUBLIC,
)

#: What an artefact is assumed to be when nobody said. It is the safest assumption: a
#: file wrongly labelled synthetic is merely under-trusted, whereas a file wrongly
#: labelled organisational is a fabricated audit record.
DEFAULT_EVIDENCE_PROVENANCE = EvidenceProvenance.SYNTHETIC


def provenance_label(raw) -> str:
    """Full disclosure sentence for a provenance value, however it was spelled.

    Unrecognised input falls back to the sentence for the default rather than returning
    an empty string, so a surface that renders this can never end up silently showing
    nothing where a provenance disclosure belongs.
    """
    member = EvidenceProvenance.coerce(raw, DEFAULT_EVIDENCE_PROVENANCE)
    return PROVENANCE_LABELS[member.value]


def provenance_badge_text(raw) -> str:
    """Short pill text for a provenance value. See :data:`PROVENANCE_BADGES`."""
    member = EvidenceProvenance.coerce(raw, DEFAULT_EVIDENCE_PROVENANCE)
    return PROVENANCE_BADGES[member.value]


class ParseStatus(StrEnum):
    PENDING = "PENDING"
    PARSED = "PARSED"
    FAILED = "FAILED"
    UNSUPPORTED = "UNSUPPORTED"


class SourceType(StrEnum):
    """Shape of the origin of an evidence chunk; drives how a locator is rendered."""

    PDF_PAGE = "PDF_PAGE"
    DOCX_PARAGRAPH = "DOCX_PARAGRAPH"
    DOCX_TABLE = "DOCX_TABLE"
    TABLE_ROWS = "TABLE_ROWS"
    TABLE_SUMMARY = "TABLE_SUMMARY"
    TEXT_BLOCK = "TEXT_BLOCK"


class ExperimentMode(StrEnum):
    """The three configurations compared by the research evaluation harness.

    A: LLM + raw evidence (no retrieval, no structured control context).
    B: LLM + RAG + full control requirement, single structured call.
    C: LLM + RAG + multi-step audit workflow + citation validation + human-review gate.
    """

    A_RAW_LLM = "A_RAW_LLM"
    B_RAG = "B_RAG"
    C_RAG_WORKFLOW = "C_RAG_WORKFLOW"

    @property
    def label(self) -> str:
        return {
            "A_RAW_LLM": "Experiment A - LLM + raw evidence",
            "B_RAG": "Experiment B - LLM + RAG + control requirements",
            "C_RAG_WORKFLOW": "Experiment C - LLM + RAG + structured workflow + human review",
        }[self.value]

    @property
    def uses_retrieval(self) -> bool:
        return self is not ExperimentMode.A_RAW_LLM

    @property
    def uses_workflow(self) -> bool:
        return self is ExperimentMode.C_RAG_WORKFLOW


class RetrievalStrategy(StrEnum):
    KEYWORD = "KEYWORD"
    VECTOR = "VECTOR"
    HYBRID = "HYBRID"


class CitationVerdict(StrEnum):
    """Result of mechanically checking one model citation against the evidence store."""

    VERIFIED = "VERIFIED"
    PARTIAL = "PARTIAL"
    UNVERIFIED = "UNVERIFIED"
    FABRICATED = "FABRICATED"


class ActivityAction(StrEnum):
    PROJECT_CREATED = "PROJECT_CREATED"
    PROJECT_UPDATED = "PROJECT_UPDATED"
    CONTROL_LINKED = "CONTROL_LINKED"
    CONTROL_UNLINKED = "CONTROL_UNLINKED"
    EVIDENCE_UPLOADED = "EVIDENCE_UPLOADED"
    EVIDENCE_PARSED = "EVIDENCE_PARSED"
    EVIDENCE_DELETED = "EVIDENCE_DELETED"
    ASSESSMENT_RUN = "ASSESSMENT_RUN"
    HUMAN_REVIEW_RECORDED = "HUMAN_REVIEW_RECORDED"
    REPORT_GENERATED = "REPORT_GENERATED"
    EVALUATION_RUN = "EVALUATION_RUN"
    #: A historical case study was imported as an engagement. Recorded as its own action
    #: so the activity trail says plainly that an engagement's evidence is a
    #: reconstruction rather than something an auditor uploaded.
    CASE_STUDY_IMPORTED = "CASE_STUDY_IMPORTED"
