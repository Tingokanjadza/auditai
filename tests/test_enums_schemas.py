"""Enumerations and the LLM output contract.

The contract tests here are not cosmetic. ``AssessmentOutput`` is the only shape the
engine accepts from a model, so its coercion rules decide what happens when a model
answers in lower case, returns a bare string where a citation object was asked for, or -
the one that matters most - tries to waive human review.
"""

from __future__ import annotations

import json

import pytest

from app.schemas.assessment import (
    ASSESSMENT_JSON_TEMPLATE,
    NO_EVIDENCE_SENTINEL,
    SELF_CRITIQUE_JSON_TEMPLATE,
    SUFFICIENCY_JSON_TEMPLATE,
    AssessmentOutput,
    EvidenceCitation,
    SelfCritique,
    SufficiencyCheck,
    assessment_json_schema,
)
from app.schemas.enums import (
    DEFICIENCY_STATUSES,
    HIGH_RISK_LEVELS,
    RISK_ORDER,
    ActivityAction,
    AssessmentStatus,
    CitationVerdict,
    ConfidenceLevel,
    EvidenceSufficiency,
    EvidenceType,
    ExperimentMode,
    HumanDecision,
    ParseStatus,
    ProjectStatus,
    RetrievalStrategy,
    RiskLevel,
    SourceType,
)

ALL_ENUMS = [
    ActivityAction,
    AssessmentStatus,
    CitationVerdict,
    ConfidenceLevel,
    EvidenceSufficiency,
    EvidenceType,
    ExperimentMode,
    HumanDecision,
    ParseStatus,
    ProjectStatus,
    RetrievalStrategy,
    RiskLevel,
    SourceType,
]


# ---- enums
@pytest.mark.parametrize("enum_cls", ALL_ENUMS, ids=lambda c: c.__name__)
def test_every_enum_is_a_string_enum_with_values(enum_cls):
    values = enum_cls.values()
    assert values and len(values) == len(set(values))
    for member in enum_cls:
        assert isinstance(member.value, str)
        assert str(member) == member.value
        # str subclass: a member is directly comparable with the persisted column value.
        assert member == member.value


@pytest.mark.parametrize("enum_cls", ALL_ENUMS, ids=lambda c: c.__name__)
def test_coerce_round_trips_every_member(enum_cls):
    for member in enum_cls:
        assert enum_cls.coerce(member) is member
        assert enum_cls.coerce(member.value) is member
        assert enum_cls.coerce(member.value.lower()) is member
        assert enum_cls.coerce(member.value.replace("_", " ")) is member
        assert enum_cls.coerce(member.value.replace("_", "-")) is member


@pytest.mark.parametrize("raw", [None, "", "not a status", 17, [], {"a": 1}])
def test_coerce_returns_the_default_for_anything_unmappable(raw):
    sentinel = object()
    assert AssessmentStatus.coerce(raw, sentinel) is sentinel
    assert AssessmentStatus.coerce(raw) is None


def test_insufficient_evidence_is_a_first_class_status():
    """The research premise: "we cannot tell" must be expressible, never a verdict."""
    assert AssessmentStatus.INSUFFICIENT_EVIDENCE in AssessmentStatus
    assert AssessmentStatus.INSUFFICIENT_EVIDENCE not in DEFICIENCY_STATUSES


def test_deficiency_and_high_risk_groupings():
    assert set(DEFICIENCY_STATUSES) == {
        AssessmentStatus.POTENTIAL_DEFICIENCY,
        AssessmentStatus.NOT_EFFECTIVE,
    }
    assert set(HIGH_RISK_LEVELS) == {RiskLevel.HIGH, RiskLevel.CRITICAL}


def test_risk_order_is_a_total_order_over_every_level():
    assert set(RISK_ORDER) == set(RiskLevel)
    ordered = sorted(RISK_ORDER, key=lambda level: RISK_ORDER[level])
    assert ordered == [
        RiskLevel.NOT_RATED,
        RiskLevel.LOW,
        RiskLevel.MEDIUM,
        RiskLevel.HIGH,
        RiskLevel.CRITICAL,
    ]


@pytest.mark.parametrize(
    "mode,uses_retrieval,uses_workflow",
    [
        (ExperimentMode.A_RAW_LLM, False, False),
        (ExperimentMode.B_RAG, True, False),
        (ExperimentMode.C_RAG_WORKFLOW, True, True),
    ],
)
def test_experiment_mode_properties(mode, uses_retrieval, uses_workflow):
    assert mode.uses_retrieval is uses_retrieval
    assert mode.uses_workflow is uses_workflow
    assert mode.label.startswith("Experiment ")


# ---- the safety rail that lives in the schema
def test_a_model_cannot_waive_human_review_by_setting_the_flag():
    """The single most important line in the contract: the flag is not the model's."""
    assert AssessmentOutput(human_review_required=False).human_review_required is True
    assert AssessmentOutput.model_validate(
        {"status": "EFFECTIVE", "human_review_required": False}
    ).human_review_required is True


@pytest.mark.parametrize("raw", [False, "false", "no", 0, None, "absolutely not"])
def test_human_review_required_is_true_whatever_the_model_sends(raw):
    assert AssessmentOutput.model_validate({"human_review_required": raw}).human_review_required is True


# ---- output coercion
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("effective", AssessmentStatus.EFFECTIVE),
        ("Potential Deficiency", AssessmentStatus.POTENTIAL_DEFICIENCY),
        ("not-effective", AssessmentStatus.NOT_EFFECTIVE),
        ("wildly unexpected", AssessmentStatus.INSUFFICIENT_EVIDENCE),
        (None, AssessmentStatus.INSUFFICIENT_EVIDENCE),
    ],
)
def test_status_degrades_to_insufficient_evidence_rather_than_raising(raw, expected):
    assert AssessmentOutput.model_validate({"status": raw}).status is expected


def test_unmappable_enum_fields_fall_back_to_the_conservative_member():
    output = AssessmentOutput.model_validate(
        {"risk_level": "catastrophic", "confidence": "absolute", "evidence_sufficiency": "plenty"}
    )
    assert output.risk_level is RiskLevel.NOT_RATED
    assert output.confidence is ConfidenceLevel.LOW
    assert output.evidence_sufficiency is EvidenceSufficiency.NONE


def test_extra_keys_from_a_chatty_model_are_ignored():
    output = AssessmentOutput.model_validate({"status": "EFFECTIVE", "sentiment": "confident"})
    assert output.status is AssessmentStatus.EFFECTIVE
    assert not hasattr(output, "sentiment")


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, []),
        ("", []),
        ("  ", []),
        ("one item", ["one item"]),
        (["a", "", "  b  "], ["a", "b"]),
    ],
)
def test_string_list_fields_tolerate_a_bare_string_or_none(raw, expected):
    assert AssessmentOutput.model_validate({"missing_evidence": raw}).missing_evidence == expected


def test_bare_string_evidence_is_kept_as_an_unlocated_quote():
    """Dropping it would erase a real quotation; keeping it lets the validator judge it."""
    output = AssessmentOutput.model_validate({"evidence": ["MFA_Status = Disabled"]})
    assert len(output.evidence) == 1
    assert output.evidence[0].quoted_text == "MFA_Status = Disabled"
    assert output.evidence[0].chunk_id is None


def test_already_constructed_citations_pass_through_untouched():
    citation = EvidenceCitation(chunk_id=4, quoted_text="verbatim")
    output = AssessmentOutput(evidence=[citation])
    assert output.evidence[0].chunk_id == 4
    assert output.cited_chunk_ids() == [4]


@pytest.mark.parametrize(
    "raw,expected",
    [(12, 12), ("12", 12), ("#12", 12), ("12 (rows 3-4)", 12), (None, None), ("", None), ("none", None)],
)
def test_chunk_id_coercion(raw, expected):
    assert EvidenceCitation.model_validate({"chunk_id": raw}).chunk_id == expected


def test_citation_string_fields_accept_a_list_from_a_confused_model():
    citation = EvidenceCitation.model_validate({"locator": ["row 14", "row 27"], "filename": None})
    assert citation.locator == "row 14, row 27"
    assert citation.filename == ""


def test_has_evidence_and_the_no_evidence_sentinel():
    empty = AssessmentOutput()
    assert empty.has_evidence is False
    assert empty.evidence_statement == NO_EVIDENCE_SENTINEL

    cited = AssessmentOutput(evidence=[EvidenceCitation(chunk_id=1, quoted_text="x")])
    assert cited.has_evidence is True
    assert "1 evidence reference" in cited.evidence_statement


def test_a_citation_with_neither_id_nor_quote_does_not_count_as_evidence():
    output = AssessmentOutput(evidence=[EvidenceCitation(relevance="it matters")])
    assert output.has_evidence is False


# ---- prompt templates stay in step with the models
@pytest.mark.parametrize(
    "template,model_cls",
    [
        (ASSESSMENT_JSON_TEMPLATE, AssessmentOutput),
        (SUFFICIENCY_JSON_TEMPLATE, SufficiencyCheck),
        (SELF_CRITIQUE_JSON_TEMPLATE, SelfCritique),
    ],
    ids=["assessment", "sufficiency", "critique"],
)
def test_prompt_templates_are_valid_json_that_the_model_accepts(template, model_cls):
    """A template that has drifted from its model teaches the LLM the wrong shape."""
    payload = json.loads(template)
    assert set(payload) == set(model_cls.model_fields), "template keys differ from the model's"
    model_cls.model_validate(payload)


def test_assessment_json_schema_is_titled_for_structured_output():
    schema = assessment_json_schema()
    assert schema["title"] == "ITControlAssessment"
    assert "status" in schema["properties"]
    assert "evidence" in schema["properties"]


# ---- workflow sub-models
@pytest.mark.parametrize("raw,expected", [(True, True), ("true", True), ("yes", True), ("1", True), ("no", False), (None, False)])
def test_sufficiency_can_conclude_coercion(raw, expected):
    assert SufficiencyCheck.model_validate({"can_conclude": raw}).can_conclude is expected


def test_self_critique_recommended_status_is_optional_and_tolerant():
    assert SelfCritique.model_validate({"recommended_status": "null"}).recommended_status is None
    assert SelfCritique.model_validate({"recommended_status": ""}).recommended_status is None
    assert (
        SelfCritique.model_validate({"recommended_status": "insufficient evidence"}).recommended_status
        is AssessmentStatus.INSUFFICIENT_EVIDENCE
    )
    assert SelfCritique.model_validate({"recommended_status": "gibberish"}).recommended_status is None
