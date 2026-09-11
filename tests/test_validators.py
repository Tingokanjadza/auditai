"""The anti-hallucination layer.

"The AI cannot invent evidence" is a promise until something checks it. These tests are
that check, checked. They cover the four citation verdicts, the numeric-claim scan and -
most importantly - the safety rails: an ungrounded EFFECTIVE conclusion must be
withdrawn, a fabricated citation must be kept and labelled rather than quietly deleted,
and human review must be forced on every assessment whatever the model asked for.
"""

from __future__ import annotations

import pytest

from app.audit.validators import (
    PARTIAL_CITATION_CREDIT,
    CitationCheck,
    ValidationReport,
    detect_unsupported_claims,
    enforce_safety_rails,
    grounding_score,
    longest_common_substring_ratio,
    normalise_text,
    token_overlap_ratio,
    validate_and_enforce,
    validate_citations,
)
from app.rag.base import RetrievalResult, RetrievedChunk, SourceLocator
from app.schemas.assessment import NO_EVIDENCE_SENTINEL, AssessmentOutput, EvidenceCitation
from app.schemas.enums import (
    AssessmentStatus,
    CitationVerdict,
    ConfidenceLevel,
    EvidenceSufficiency,
)

CHUNK_TEXT = (
    "TABLE SUMMARY - Privileged_Accounts_Export.csv\n"
    "Population: 100 data row(s) x 4 column(s). Header on spreadsheet row 1.\n"
    "MFA_Status: Enabled = 90 (90.0%) | Disabled = 10 (10.0%)\n"
    "  MFA_Status = Disabled occurs in 10 row(s), at spreadsheet rows: 14, 27, 38"
)


@pytest.fixture()
def retrieval():
    return RetrievalResult(
        chunks=[
            RetrievedChunk(
                chunk_id=7,
                text=CHUNK_TEXT,
                locator=SourceLocator(
                    filename="Privileged_Accounts_Export.csv",
                    source_type="TABLE_SUMMARY",
                    row_start=2,
                    row_end=101,
                ),
                filename="Privileged_Accounts_Export.csv",
                evidence_file_id=3,
            )
        ],
        queries=["mfa status"],
        strategy="HYBRID",
    )


# --------------------------------------------------------------------- similarity
def test_normalise_text_folds_case_and_punctuation():
    assert normalise_text("MFA_Status = Disabled!") == "mfa status disabled"
    assert normalise_text("") == ""


def test_normalise_text_preserves_non_ascii_letters():
    """Stripping ``\\W`` with an ASCII class would delete accented characters from both
    sides of a comparison and quietly make every such quote unverifiable."""
    assert normalise_text("P. Adeyemí") == "p adeyemí"
    # NFKC first, so a combining accent and a precomposed one normalise identically.
    assert normalise_text("cafe\u0301") == normalise_text("caf\u00e9")


def test_token_overlap_is_containment_not_symmetric_jaccard():
    """A chunk is far longer than a quote; a symmetric index would call everything fake."""
    assert token_overlap_ratio("mfa status", "mfa status disabled 10 of 100 accounts") == 1.0
    assert token_overlap_ratio("", "anything") == 0.0


def test_longest_common_substring_ratio_bounds():
    assert longest_common_substring_ratio("abc", "xxabcxx") == 1.0
    assert longest_common_substring_ratio("abc", "") == 0.0


def test_a_verbatim_quote_scores_one_and_an_invention_scores_low():
    assert grounding_score("MFA_Status: Enabled = 90", CHUNK_TEXT) == 1.0
    assert grounding_score("The vendor confirmed remediation by telephone.", CHUNK_TEXT) < 0.3


def test_grounding_score_is_symmetric_to_reflowed_whitespace():
    reflowed = "MFA_Status:   Enabled\n= 90"
    assert grounding_score(reflowed, CHUNK_TEXT) == 1.0


# ------------------------------------------------------------------- the verdicts
def test_a_chunk_id_that_was_never_retrieved_is_fabricated(retrieval):
    """Plausibility is the failure mode, so the quote is not even scored."""
    output = AssessmentOutput(
        evidence=[
            EvidenceCitation(
                chunk_id=424242,
                filename="invented.csv",
                quoted_text="MFA_Status: Enabled = 90 (90.0%) | Disabled = 10 (10.0%)",
            )
        ]
    )
    report = validate_citations(output, retrieval)
    check = report.citations[0]
    assert check.verdict is CitationVerdict.FABRICATED
    assert check.match_score == 0.0
    assert "424242" in check.note
    assert report.fabricated == 1
    assert report.has_hallucination is True


def test_a_verbatim_quote_from_a_retrieved_chunk_is_verified(retrieval):
    output = AssessmentOutput(
        evidence=[EvidenceCitation(chunk_id=7, quoted_text="MFA_Status: Enabled = 90 (90.0%)")]
    )
    report = validate_citations(output, retrieval)
    check = report.citations[0]
    assert check.verdict is CitationVerdict.VERIFIED
    assert check.match_score == pytest.approx(1.0)
    # Stored provenance wins over whatever the model typed.
    assert check.filename == "Privileged_Accounts_Export.csv"
    assert check.evidence_file_id == 3
    assert report.verified == 1


def test_a_real_chunk_with_an_invented_quote_is_fabricated(retrieval):
    output = AssessmentOutput(
        evidence=[
            EvidenceCitation(
                chunk_id=7,
                quoted_text="Management confirmed that remediation was completed in July.",
            )
        ]
    )
    check = validate_citations(output, retrieval).citations[0]
    assert check.verdict is CitationVerdict.FABRICATED
    assert "chunk is real but this excerpt is not in it" in check.note


def test_a_pointer_with_no_quotation_is_unverified_not_fabricated(retrieval):
    """Pointing at real evidence without excerpting it is honest, just uncredited."""
    output = AssessmentOutput(evidence=[EvidenceCitation(chunk_id=7, quoted_text="")])
    report = validate_citations(output, retrieval)
    assert report.citations[0].verdict is CitationVerdict.UNVERIFIED
    assert report.unverified == 1
    assert report.has_hallucination is False


def test_a_genuine_quote_with_no_chunk_id_is_partial(retrieval):
    """Untraceable, so it can never be VERIFIED - but it is not an invention either."""
    output = AssessmentOutput(
        evidence=[EvidenceCitation(quoted_text="MFA_Status: Enabled = 90 (90.0%)")]
    )
    check = validate_citations(output, retrieval).citations[0]
    assert check.verdict is CitationVerdict.PARTIAL
    assert "No chunk_id was supplied" in check.note


def test_an_untraceable_invention_is_fabricated(retrieval):
    output = AssessmentOutput(evidence=[EvidenceCitation(quoted_text="Zebras roam the datacentre.")])
    assert validate_citations(output, retrieval).citations[0].verdict is CitationVerdict.FABRICATED


def test_an_empty_citation_is_unverified(retrieval):
    output = AssessmentOutput(evidence=[EvidenceCitation(relevance="it matters")])
    assert validate_citations(output, retrieval).citations[0].verdict is CitationVerdict.UNVERIFIED


def test_every_citation_lands_in_exactly_one_verdict(retrieval):
    output = AssessmentOutput(
        evidence=[
            EvidenceCitation(chunk_id=7, quoted_text="MFA_Status: Enabled = 90"),  # VERIFIED
            EvidenceCitation(chunk_id=7, quoted_text=""),  # UNVERIFIED
            EvidenceCitation(chunk_id=999, quoted_text="anything"),  # FABRICATED
            EvidenceCitation(quoted_text="MFA_Status: Enabled = 90"),  # PARTIAL
        ]
    )
    report = validate_citations(output, retrieval)
    assert report.total == 4
    assert report.verified + report.partial + report.unverified + report.fabricated == report.total


def test_citing_nothing_is_a_total_of_zero_not_a_perfect_score(retrieval):
    report = validate_citations(AssessmentOutput(), retrieval)
    assert report.total == 0
    assert report.grounding_rate == 0.0
    assert report.grounding_rate_strict == 0.0
    assert report.has_hallucination is False


# ------------------------------------------------------------------- the three rates
def test_the_three_grounding_rates_are_arithmetically_consistent():
    report = ValidationReport(
        citations=[CitationCheck() for _ in range(4)],
        total=4,
        verified=2,
        partial=1,
        unverified=1,
        fabricated=0,
        grounding_rate=(2 + PARTIAL_CITATION_CREDIT * 1) / 4.0,
    )
    assert report.grounding_rate == pytest.approx(0.625)
    assert report.grounding_rate_strict == pytest.approx(0.5)
    assert report.partial_credit_rate == pytest.approx(0.125)
    assert report.grounding_rate == pytest.approx(
        report.grounding_rate_strict + report.partial_credit_rate
    )


def test_partial_credit_is_a_named_constant_not_a_derived_quantity():
    assert PARTIAL_CITATION_CREDIT == 0.5


def test_report_to_dict_carries_the_components_a_reader_needs():
    report = ValidationReport(total=2, verified=0, partial=2, grounding_rate=0.5, threshold=0.6)
    payload = report.to_dict()
    # "0 verified, grounding 0.50" is uninterpretable without these.
    for key in (
        "total",
        "verified",
        "partial",
        "fabricated",
        "unverified",
        "grounding_rate",
        "grounding_rate_strict",
        "partial_credit_rate",
        "partial_citation_credit",
        "grounding_rate_definition",
        "grounding_rate_strict_definition",
    ):
        assert key in payload
    assert payload["grounding_rate_strict"] == 0.0


# ------------------------------------------------------------------- safety rails
def test_an_ungrounded_effective_conclusion_is_withdrawn(retrieval):
    """The rail the whole safety argument rests on."""
    output = AssessmentOutput(
        status=AssessmentStatus.EFFECTIVE,
        confidence=ConfidenceLevel.HIGH,
        evidence_sufficiency=EvidenceSufficiency.SUFFICIENT,
        assessment="Every privileged account is enrolled.",
        evidence=[EvidenceCitation(chunk_id=424242, quoted_text="All accounts are enrolled.")],
    )
    report = validate_citations(output, retrieval)
    guarded = enforce_safety_rails(output, report)

    assert guarded.status is AssessmentStatus.INSUFFICIENT_EVIDENCE
    assert guarded.confidence is ConfidenceLevel.LOW
    assert guarded.evidence_sufficiency is EvidenceSufficiency.INSUFFICIENT
    assert report.status_downgraded is True
    assert report.original_status == AssessmentStatus.EFFECTIVE.value
    assert any(r.startswith("status_downgraded") for r in report.rails_applied)


def test_an_ungrounded_accusation_is_withdrawn_too(retrieval):
    """Both directions: an unsupported clean opinion and an unsupported accusation."""
    output = AssessmentOutput(
        status=AssessmentStatus.NOT_EFFECTIVE,
        evidence=[EvidenceCitation(chunk_id=999, quoted_text="nothing real")],
    )
    report = validate_citations(output, retrieval)
    assert enforce_safety_rails(output, report).status is AssessmentStatus.INSUFFICIENT_EVIDENCE


def test_a_hedged_flag_is_left_alone(retrieval):
    """POTENTIAL_DEFICIENCY already asserts only a possibility; suppressing it would
    remove exactly the signal an auditor most needs to see."""
    output = AssessmentOutput(status=AssessmentStatus.POTENTIAL_DEFICIENCY)
    report = validate_citations(output, retrieval)
    guarded = enforce_safety_rails(output, report)
    assert guarded.status is AssessmentStatus.POTENTIAL_DEFICIENCY
    assert report.status_downgraded is False


def test_a_grounded_effective_conclusion_stands(retrieval):
    output = AssessmentOutput(
        status=AssessmentStatus.EFFECTIVE,
        evidence=[EvidenceCitation(chunk_id=7, quoted_text="MFA_Status: Enabled = 90 (90.0%)")],
    )
    report = validate_citations(output, retrieval)
    assert enforce_safety_rails(output, report).status is AssessmentStatus.EFFECTIVE


def test_partial_matches_alone_do_not_ground_a_conclusion(retrieval):
    """The rail takes the strict rate, even though PARTIAL earns half credit elsewhere."""
    output = AssessmentOutput(
        status=AssessmentStatus.EFFECTIVE,
        evidence=[EvidenceCitation(quoted_text="MFA_Status: Enabled = 90 (90.0%)")],
    )
    report = validate_citations(output, retrieval)
    assert report.partial == 1 and report.verified == 0
    assert enforce_safety_rails(output, report).status is AssessmentStatus.INSUFFICIENT_EVIDENCE


def test_human_review_can_never_be_waived(retrieval):
    """Not by the model, not by a clean report, not by anything."""
    output = AssessmentOutput(status=AssessmentStatus.EFFECTIVE)
    object.__setattr__(output, "human_review_required", False)  # bypass the field validator
    assert output.human_review_required is False

    report = validate_citations(output, retrieval)
    guarded = enforce_safety_rails(output, report)
    assert guarded.human_review_required is True
    assert any(r.startswith("human_review_enforced") for r in report.rails_applied)


def test_the_human_review_rail_is_recorded_even_on_a_clean_assessment(retrieval):
    output = AssessmentOutput(
        status=AssessmentStatus.POTENTIAL_DEFICIENCY,
        evidence=[EvidenceCitation(chunk_id=7, quoted_text="MFA_Status: Enabled = 90")],
    )
    report = validate_citations(output, retrieval)
    enforce_safety_rails(output, report)
    assert any("human_review_enforced" in r for r in report.rails_applied)


def test_fabricated_citations_are_flagged_and_kept_not_deleted(retrieval):
    """One that quietly disappeared would be worse than one left visible and labelled."""
    output = AssessmentOutput(
        status=AssessmentStatus.POTENTIAL_DEFICIENCY,
        evidence=[
            EvidenceCitation(chunk_id=7, quoted_text="MFA_Status: Enabled = 90"),
            EvidenceCitation(chunk_id=999, quoted_text="invented"),
        ],
    )
    report = validate_citations(output, retrieval)
    guarded = enforce_safety_rails(output, report)
    assert len(guarded.evidence) == 2
    assert any(r.startswith("fabricated_citations") for r in report.rails_applied)
    assert any("Disregard citation" in item for item in guarded.human_verification_required)


def test_no_citations_at_all_writes_the_sentinel(retrieval):
    output = AssessmentOutput(status=AssessmentStatus.POTENTIAL_DEFICIENCY, assessment="Something is wrong.")
    report = validate_citations(output, retrieval)
    guarded = enforce_safety_rails(output, report)
    assert NO_EVIDENCE_SENTINEL in guarded.assessment
    assert NO_EVIDENCE_SENTINEL in guarded.finding
    assert guarded.evidence_sufficiency is EvidenceSufficiency.NONE


def test_the_rails_never_mutate_the_model_answer(retrieval):
    """The unedited answer is the audit trail; the hallucination rate is only checkable
    if it survives."""
    output = AssessmentOutput(
        status=AssessmentStatus.EFFECTIVE,
        confidence=ConfidenceLevel.HIGH,
        assessment="Original text.",
        evidence=[EvidenceCitation(chunk_id=999, quoted_text="invented")],
    )
    report = validate_citations(output, retrieval)
    guarded = enforce_safety_rails(output, report)

    assert output.status is AssessmentStatus.EFFECTIVE
    assert output.confidence is ConfidenceLevel.HIGH
    assert output.assessment == "Original text."
    assert guarded is not output


def test_validate_and_enforce_runs_the_whole_pass(retrieval, control):
    output = AssessmentOutput(
        status=AssessmentStatus.EFFECTIVE,
        assessment="A total of 4321 accounts were reviewed.",
        evidence=[EvidenceCitation(chunk_id=999, quoted_text="invented")],
    )
    guarded, report = validate_and_enforce(output, retrieval, control)
    assert guarded.status is AssessmentStatus.INSUFFICIENT_EVIDENCE
    assert report.fabricated == 1
    assert report.unsupported_claims
    assert report.has_hallucination is True


# --------------------------------------------------------- numeric claim detection
def test_a_figure_that_appears_nowhere_in_the_evidence_is_flagged(retrieval):
    output = AssessmentOutput(assessment="A total of 4321 accounts were reviewed.")
    claims = detect_unsupported_claims(output, retrieval)
    assert len(claims) == 1
    assert "4321" in claims[0]
    assert "assessment:" in claims[0]


def test_a_figure_present_in_the_evidence_is_not_flagged(retrieval):
    output = AssessmentOutput(assessment="10 of 100 privileged accounts show MFA_Status = Disabled.")
    assert detect_unsupported_claims(output, retrieval) == []


def test_a_percentage_derivable_from_evidence_counts_is_accepted(retrieval):
    """"10 of 100 failed" and "90% complied" are one observation stated two ways."""
    output = AssessmentOutput(finding="Only 90% of privileged accounts are enrolled.")
    assert detect_unsupported_claims(output, retrieval) == []


def test_an_invented_percentage_is_flagged(retrieval):
    # 55 is neither in the evidence nor derivable from any pair of its counts
    # (the counts are 1, 4, 10, 14, 27, 38, 90 and 100).
    output = AssessmentOutput(finding="Some 55% of privileged accounts are enrolled.")
    claims = detect_unsupported_claims(output, retrieval)
    assert claims and "55%" in claims[0]


@pytest.mark.parametrize(
    "text",
    [
        "CONTROL-001 was tested.",
        "See section 4.2 of the policy.",
        "Refer to chunk_id 7.",
        "Exceptions were found at rows 14, 27 and 38.",
        "Policy version v3.1 applies.",
        "This is the 1st exception noted.",
    ],
)
def test_identifiers_and_cross_references_are_not_read_as_statistics(retrieval, text):
    assert detect_unsupported_claims(AssessmentOutput(assessment=text), retrieval) == []


def test_the_control_definition_licenses_the_threshold_it_states(retrieval, control):
    """Restating a policy threshold is a requirement, not an invented observation."""
    output = AssessmentOutput(assessment="The criterion requires 100% enrolment.")
    assert detect_unsupported_claims(output, retrieval, control) == []


def test_the_models_own_restatement_does_not_license_its_own_statistics(retrieval):
    """``control_requirement`` is excluded from the supporting vocabulary on purpose."""
    output = AssessmentOutput(
        control_requirement="The policy requires 8888 accounts to be enrolled.",
        assessment="8888 accounts were enrolled.",
    )
    assert detect_unsupported_claims(output, retrieval)


def test_thousands_separators_are_normalised():
    chunk = RetrievedChunk(chunk_id=1, text="Total endpoints: 1250", locator=SourceLocator(filename="e.csv"))
    retrieval = RetrievalResult(chunks=[chunk])
    output = AssessmentOutput(assessment="There are 1,250 endpoints in scope.")
    assert detect_unsupported_claims(output, retrieval) == []


def test_prose_with_no_numbers_produces_no_claims(retrieval):
    output = AssessmentOutput(assessment="The control appears to operate as described.")
    assert detect_unsupported_claims(output, retrieval) == []


def test_claims_are_scanned_in_the_three_narrative_fields_only(retrieval):
    # Deliberately plain prose: a lead-in such as "figure" or "section" is masked out
    # as a cross-reference before the numeric scan ever sees the digits.
    output = AssessmentOutput(
        assessment="We counted 8888 accounts.",
        finding="We counted 7777 accounts.",
        reasoning="We counted 6666 accounts.",
        recommendation="We counted 5555 accounts.",
        limitations="We counted 4444 accounts.",
    )
    claims = detect_unsupported_claims(output, retrieval)
    blob = " ".join(claims)
    assert "8888" in blob and "7777" in blob and "6666" in blob
    assert "5555" not in blob and "4444" not in blob


# ---------------------------------------------------------------- empty retrieval
def test_validation_against_an_empty_evidence_set(retrieval):
    empty = RetrievalResult()
    output = AssessmentOutput(
        status=AssessmentStatus.EFFECTIVE,
        evidence=[EvidenceCitation(chunk_id=1, quoted_text="anything")],
    )
    report = validate_citations(output, empty)
    assert report.fabricated == 1
    assert report.retrieved_chunk_ids == []
    assert enforce_safety_rails(output, report).status is AssessmentStatus.INSUFFICIENT_EVIDENCE
