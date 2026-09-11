"""The structured contract between the LLM and the audit application.

``AssessmentOutput`` is the *only* shape the assessment engine will accept from a
model. It deliberately forces the model to separate four different kinds of statement,
which is the core research principle of this system:

======================  ==================================================
Field                   Question it answers
======================  ==================================================
``control_requirement`` What does the POLICY / CONTROL REQUIRE?
``evidence`` + quotes   What does the EVIDENCE PROVE (verbatim, cited)?
``inferences``          What did the AI INFER beyond the literal evidence?
``human_verification_   What still needs HUMAN VERIFICATION?
required`` / ``missing_
evidence``
======================  ==================================================

Anything the model cannot ground in a supplied chunk must appear under
``inferences`` or ``missing_evidence`` - never as an evidential claim.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.enums import (
    AssessmentStatus,
    ConfidenceLevel,
    EvidenceSufficiency,
    RiskLevel,
)

#: Sentinel the model must emit when it can find nothing to support a finding.
NO_EVIDENCE_SENTINEL = "No supporting evidence identified."


class EvidenceCitation(BaseModel):
    """A single, checkable pointer back into the supplied evidence.

    ``chunk_id`` must be one of the IDs listed in the prompt's evidence block. The
    validator in :mod:`app.audit.validators` re-checks ``quoted_text`` against the real
    chunk text; a citation that cannot be matched is recorded as FABRICATED rather than
    silently trusted.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    chunk_id: Optional[int] = Field(
        default=None, description="ID of the evidence chunk exactly as given in the EVIDENCE block."
    )
    filename: str = Field(default="", description="Source filename of the cited evidence.")
    locator: str = Field(
        default="",
        description="Human-readable location, e.g. 'rows 14, 27, 38' or 'page 3, section 4.2'.",
    )
    quoted_text: str = Field(
        default="",
        description="VERBATIM excerpt copied from the evidence chunk. Never paraphrase here.",
    )
    relevance: str = Field(default="", description="Why this excerpt matters for the control.")
    supports: str = Field(
        default="",
        description="What this citation establishes: 'requirement', 'observation', 'exception', or 'context'.",
    )

    @field_validator("filename", "locator", "quoted_text", "relevance", "supports", mode="before")
    @classmethod
    def _coerce_str(cls, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (list, tuple)):
            return ", ".join(str(v) for v in value)
        return str(value)

    @field_validator("chunk_id", mode="before")
    @classmethod
    def _coerce_chunk_id(cls, value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        try:
            return int(str(value).strip().lstrip("#").split()[0])
        except (ValueError, IndexError):
            return None


class AssessmentOutput(BaseModel):
    """Structured assessment returned by the LLM for one control."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    control_id: str = Field(default="", description="The control reference being assessed, e.g. CONTROL-001.")
    control_requirement: str = Field(
        default="",
        description="Restate, in one or two sentences, strictly what the control REQUIRES. Policy only - no evidence.",
    )
    assessment: str = Field(
        default="",
        description="What the evidence shows about the control's operation. Factual and grounded only.",
    )
    status: AssessmentStatus = Field(
        default=AssessmentStatus.INSUFFICIENT_EVIDENCE,
        description=(
            "One of EFFECTIVE, POTENTIAL_DEFICIENCY, INSUFFICIENT_EVIDENCE, NOT_EFFECTIVE, NOT_APPLICABLE. "
            "Use INSUFFICIENT_EVIDENCE whenever the evidence does not actually establish how the control operates."
        ),
    )
    finding: str = Field(default="", description="The potential audit finding, or a statement that none was identified.")
    risk: str = Field(default="", description="The risk that arises if this control does not operate as required.")
    risk_level: RiskLevel = Field(
        default=RiskLevel.NOT_RATED, description="Suggested risk band: LOW, MEDIUM, HIGH or CRITICAL."
    )
    evidence: List[EvidenceCitation] = Field(
        default_factory=list,
        description="Every excerpt relied on. Empty list means no supporting evidence was identified.",
    )
    evidence_sufficiency: EvidenceSufficiency = Field(
        default=EvidenceSufficiency.NONE,
        description="SUFFICIENT, PARTIAL, INSUFFICIENT or NONE - how completely the evidence covers the control.",
    )
    missing_evidence: List[str] = Field(
        default_factory=list,
        description="Specific artefacts that were expected but are absent from the supplied evidence.",
    )
    reasoning: str = Field(
        default="",
        description="Step-by-step justification linking the requirement to the cited evidence.",
    )
    inferences: List[str] = Field(
        default_factory=list,
        description="Conclusions NOT directly stated by the evidence. Anything not literally in a citation belongs here.",
    )
    human_verification_required: List[str] = Field(
        default_factory=list,
        description="Concrete checks a human auditor must perform before this assessment can be relied upon.",
    )
    recommendation: str = Field(default="", description="Suggested remediation or further audit procedure.")
    confidence: ConfidenceLevel = Field(
        default=ConfidenceLevel.LOW, description="LOW, MEDIUM or HIGH confidence in this assessment."
    )
    human_review_required: bool = Field(
        default=True, description="Always true. This system never issues a final compliance decision."
    )
    limitations: str = Field(
        default="",
        description="What this assessment cannot establish (population completeness, evidence authenticity, …).",
    )

    # ------------------------------------------------------------ normalisation
    @field_validator("status", mode="before")
    @classmethod
    def _coerce_status(cls, value: Any) -> Any:
        return AssessmentStatus.coerce(value, AssessmentStatus.INSUFFICIENT_EVIDENCE)

    @field_validator("risk_level", mode="before")
    @classmethod
    def _coerce_risk(cls, value: Any) -> Any:
        return RiskLevel.coerce(value, RiskLevel.NOT_RATED)

    @field_validator("evidence_sufficiency", mode="before")
    @classmethod
    def _coerce_sufficiency(cls, value: Any) -> Any:
        return EvidenceSufficiency.coerce(value, EvidenceSufficiency.NONE)

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, value: Any) -> Any:
        return ConfidenceLevel.coerce(value, ConfidenceLevel.LOW)

    @field_validator("missing_evidence", "inferences", "human_verification_required", mode="before")
    @classmethod
    def _coerce_str_list(cls, value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            text = value.strip()
            return [text] if text else []
        if isinstance(value, (list, tuple)):
            return [str(v).strip() for v in value if str(v).strip()]
        return [str(value)]

    @field_validator("evidence", mode="before")
    @classmethod
    def _coerce_evidence(cls, value: Any) -> Any:
        """Tolerate a model that returns bare strings instead of citation objects."""
        if value is None:
            return []
        if isinstance(value, dict):
            value = [value]
        if not isinstance(value, (list, tuple)):
            return []
        out: List[Any] = []
        for item in value:
            if isinstance(item, str):
                # A model that returns a bare string cited *something* - keep it as an
                # unlocated quote so the validator can judge it, rather than dropping it.
                out.append({"quoted_text": item, "locator": "", "filename": ""})
            elif isinstance(item, (dict, EvidenceCitation)):
                # Already-constructed citations must pass through untouched: silently
                # dropping them would erase real evidence links built in Python.
                out.append(item)
            elif hasattr(item, "model_dump"):
                out.append(item.model_dump())
        return out

    @field_validator("human_review_required", mode="before")
    @classmethod
    def _always_review(cls, value: Any) -> bool:
        # Hard safety rail: the model is not permitted to waive human review.
        return True

    # ---------------------------------------------------------------- helpers
    @property
    def has_evidence(self) -> bool:
        return any(c.quoted_text.strip() or c.chunk_id is not None for c in self.evidence)

    @property
    def evidence_statement(self) -> str:
        return NO_EVIDENCE_SENTINEL if not self.has_evidence else f"{len(self.evidence)} evidence reference(s) cited."

    def cited_chunk_ids(self) -> List[int]:
        return [c.chunk_id for c in self.evidence if c.chunk_id is not None]


def assessment_json_schema() -> Dict[str, Any]:
    """JSON Schema handed to providers that support structured / JSON-mode output."""
    schema = AssessmentOutput.model_json_schema()
    schema["title"] = "ITControlAssessment"
    schema["description"] = "Structured IT control assessment produced from cited audit evidence."
    return schema


#: A compact, prompt-friendly rendering of the required output shape. Kept in sync
#: with the model above by :func:`tests.test_schemas` .
ASSESSMENT_JSON_TEMPLATE = """{
  "control_id": "CONTROL-001",
  "control_requirement": "<what the control/policy requires - no evidence, no inference>",
  "assessment": "<what the cited evidence actually shows>",
  "status": "EFFECTIVE | POTENTIAL_DEFICIENCY | INSUFFICIENT_EVIDENCE | NOT_EFFECTIVE | NOT_APPLICABLE",
  "finding": "<the potential finding, or 'No finding identified'>",
  "risk": "<risk if the control does not operate as required>",
  "risk_level": "LOW | MEDIUM | HIGH | CRITICAL",
  "evidence": [
    {
      "chunk_id": 12,
      "filename": "Privileged_Users.csv",
      "locator": "rows 14, 27, 38",
      "quoted_text": "<VERBATIM excerpt copied from that chunk>",
      "relevance": "<why this matters for the control>",
      "supports": "requirement | observation | exception | context"
    }
  ],
  "evidence_sufficiency": "SUFFICIENT | PARTIAL | INSUFFICIENT | NONE",
  "missing_evidence": ["<expected artefact that was not provided>"],
  "reasoning": "<requirement -> cited evidence -> conclusion>",
  "inferences": ["<anything concluded that the evidence does not literally state>"],
  "human_verification_required": ["<check the auditor must perform>"],
  "recommendation": "<remediation or further audit procedure>",
  "confidence": "LOW | MEDIUM | HIGH",
  "human_review_required": true,
  "limitations": "<what this assessment cannot establish>"
}"""


class SufficiencyCheck(BaseModel):
    """Step 1 of the Experiment C workflow: can this control be tested at all?"""

    model_config = ConfigDict(extra="ignore")

    evidence_sufficiency: EvidenceSufficiency = Field(default=EvidenceSufficiency.NONE)
    present_evidence: List[str] = Field(default_factory=list)
    missing_evidence: List[str] = Field(default_factory=list)
    can_conclude: bool = Field(default=False, description="True only if the evidence can establish control operation.")
    rationale: str = Field(default="")

    @field_validator("evidence_sufficiency", mode="before")
    @classmethod
    def _coerce_sufficiency(cls, value: Any) -> Any:
        return EvidenceSufficiency.coerce(value, EvidenceSufficiency.NONE)

    @field_validator("present_evidence", "missing_evidence", mode="before")
    @classmethod
    def _coerce_list(cls, value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        return [str(v).strip() for v in value if str(v).strip()]

    @field_validator("can_conclude", mode="before")
    @classmethod
    def _coerce_bool(cls, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"true", "yes", "1"}


SUFFICIENCY_JSON_TEMPLATE = """{
  "evidence_sufficiency": "SUFFICIENT | PARTIAL | INSUFFICIENT | NONE",
  "present_evidence": ["<expected artefact that IS present in the supplied evidence>"],
  "missing_evidence": ["<expected artefact that is NOT present>"],
  "can_conclude": true,
  "rationale": "<one paragraph: what the evidence can and cannot establish>"
}"""


class SelfCritique(BaseModel):
    """Step 3 of Experiment C: the model checks its own draft for unsupported claims."""

    model_config = ConfigDict(extra="ignore")

    unsupported_claims: List[str] = Field(default_factory=list)
    fabricated_citations: List[str] = Field(default_factory=list)
    overstated_conclusion: bool = Field(default=False)
    recommended_status: Optional[AssessmentStatus] = Field(default=None)
    notes: str = Field(default="")

    @field_validator("unsupported_claims", "fabricated_citations", mode="before")
    @classmethod
    def _coerce_list(cls, value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        return [str(v).strip() for v in value if str(v).strip()]

    @field_validator("overstated_conclusion", mode="before")
    @classmethod
    def _coerce_bool(cls, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"true", "yes", "1"}

    @field_validator("recommended_status", mode="before")
    @classmethod
    def _coerce_status(cls, value: Any) -> Any:
        if value in (None, "", "null"):
            return None
        return AssessmentStatus.coerce(value, None)


SELF_CRITIQUE_JSON_TEMPLATE = """{
  "unsupported_claims": ["<claim in the draft that no cited evidence establishes>"],
  "fabricated_citations": ["<citation that does not correspond to a supplied chunk>"],
  "overstated_conclusion": false,
  "recommended_status": "<status the draft should be downgraded to, or null>",
  "notes": "<short explanation>"
}"""
