"""The prototype risk model.

Risk in this system is computed, not taken from the model, and the whole point of that
choice is that the arithmetic can be checked by hand. So the central tests here do check
it by hand: the factor values, the per-factor contributions and the final band are
written out in the test rather than compared against whatever the code returns.

Every rating this module produces must also carry the "prototype research model, not an
official framework" label, which is asserted directly - it is the honesty claim the
dissertation makes about this component.
"""

from __future__ import annotations

import pytest

from app.audit.risk import (
    BAND_THRESHOLDS,
    FACTOR_WEIGHTS,
    RISK_MODEL_LABEL,
    RiskAssessment,
    RiskFactors,
    band_for_score,
    derive_factors,
    score_risk,
)
from app.schemas.enums import AssessmentStatus, RiskLevel

#: A dict-shaped control matching CONTROL-001 in the library, so the hand computation
#: below does not depend on a database.
MFA_CONTROL = {
    "control_id": "CONTROL-001",
    "name": "Multi-Factor Authentication for Privileged Accounts",
    "inherent_risk": "HIGH",
    "privilege_level": 5,
    "data_sensitivity": 4,
}

LOW_EXPOSURE_CONTROL = {
    "control_id": "CONTROL-999",
    "name": "Reporting portal access",
    "inherent_risk": "LOW",
    "privilege_level": 1,
    "data_sensitivity": 1,
}


# ------------------------------------------------------------------ hand arithmetic
def test_factor_derivation_matches_the_published_formula():
    """impact = 0.60 x inherent(4.0) + 0.20 x privilege(5) + 0.20 x sensitivity(4) = 4.2;
    likelihood = base 3.5 for POTENTIAL_DEFICIENCY + min(1.5, 5.0 x 0.10) = 4.0."""
    factors = derive_factors(MFA_CONTROL, AssessmentStatus.POTENTIAL_DEFICIENCY, exception_ratio=0.10)
    assert factors.impact == pytest.approx(4.2)
    assert factors.likelihood == pytest.approx(4.0)
    assert factors.privilege_level == 5.0
    assert factors.data_sensitivity == 4.0
    assert factors.control_failure_severity == 4.0
    assert factors.exception_ratio == pytest.approx(0.10)


def test_contributions_are_weight_times_normalised_value_and_sum_to_the_raw_score():
    factors = derive_factors(MFA_CONTROL, AssessmentStatus.POTENTIAL_DEFICIENCY, exception_ratio=0.10)
    contributions = factors.contributions()
    assert contributions["control_failure_severity"] == pytest.approx(22.5)  # 0.30 x 3/4 x 100
    assert contributions["likelihood"] == pytest.approx(18.75)  # 0.25 x 3/4 x 100
    assert contributions["impact"] == pytest.approx(16.0)  # 0.20 x 3.2/4 x 100
    assert contributions["privilege_level"] == pytest.approx(15.0)  # 0.15 x 4/4 x 100
    assert contributions["data_sensitivity"] == pytest.approx(7.5)  # 0.10 x 3/4 x 100
    assert factors.raw_score() == pytest.approx(79.75)
    assert sum(contributions.values()) == pytest.approx(factors.raw_score())


def test_the_headline_case_scores_and_bands_as_hand_computed():
    rating = score_risk(MFA_CONTROL, AssessmentStatus.POTENTIAL_DEFICIENCY, exception_ratio=0.10)
    assert rating.score == pytest.approx(79.75)
    assert rating.level is RiskLevel.CRITICAL


def test_a_factor_at_its_floor_contributes_nothing():
    factors = RiskFactors(
        impact=1.0, likelihood=1.0, privilege_level=1.0, data_sensitivity=1.0, control_failure_severity=1.0
    )
    assert factors.raw_score() == pytest.approx(0.0)


def test_all_factors_at_their_ceiling_score_one_hundred():
    factors = RiskFactors(
        impact=5.0, likelihood=5.0, privilege_level=5.0, data_sensitivity=5.0, control_failure_severity=5.0
    )
    assert factors.raw_score() == pytest.approx(100.0)


def test_the_weights_sum_to_one_and_favour_what_the_evidence_showed():
    assert sum(FACTOR_WEIGHTS.values()) == pytest.approx(1.0)
    evidence_driven = FACTOR_WEIGHTS["control_failure_severity"] + FACTOR_WEIGHTS["likelihood"]
    exposure = (
        FACTOR_WEIGHTS["impact"] + FACTOR_WEIGHTS["privilege_level"] + FACTOR_WEIGHTS["data_sensitivity"]
    )
    assert evidence_driven > exposure


# ------------------------------------------------------------------------- bands
@pytest.mark.parametrize(
    "score,expected",
    [
        (0.0, RiskLevel.LOW),
        (24.99, RiskLevel.LOW),
        (25.0, RiskLevel.MEDIUM),
        (49.99, RiskLevel.MEDIUM),
        (50.0, RiskLevel.HIGH),
        (74.99, RiskLevel.HIGH),
        (75.0, RiskLevel.CRITICAL),
        (100.0, RiskLevel.CRITICAL),
    ],
)
def test_band_thresholds_are_inclusive_lower_bounds(score, expected):
    assert band_for_score(score) is expected


def test_published_thresholds_match_the_bands():
    assert BAND_THRESHOLDS[RiskLevel.MEDIUM.value] == 25.0
    assert BAND_THRESHOLDS[RiskLevel.HIGH.value] == 50.0
    assert BAND_THRESHOLDS[RiskLevel.CRITICAL.value] == 75.0


# ---------------------------------------------------------------- status adjustments
def test_an_effective_conclusion_is_capped_at_the_low_band():
    """Raw score would be 38.5 on exposure alone; the ceiling reports residual risk."""
    rating = score_risk(MFA_CONTROL, AssessmentStatus.EFFECTIVE, exception_ratio=0.0)
    assert rating.factors["raw_score"] == pytest.approx(38.5)
    assert rating.score == pytest.approx(24.0)
    assert rating.level is RiskLevel.LOW
    assert any(a["name"] == "effective_ceiling" for a in rating.factors["adjustments"])


def test_exposure_is_still_reported_after_the_ceiling_is_applied():
    """The auditor must be able to see what is at stake if the conclusion is overturned."""
    rating = score_risk(MFA_CONTROL, AssessmentStatus.EFFECTIVE)
    assert rating.factors["values"]["privilege_level"] == 5.0
    assert rating.factors["values"]["impact"] == pytest.approx(4.2)


@pytest.mark.parametrize("control", [MFA_CONTROL, LOW_EXPOSURE_CONTROL], ids=["high", "low"])
def test_an_unverifiable_control_is_never_reported_as_low_risk(control):
    """"We cannot tell whether this operates" is an open risk, not a low one - otherwise
    the system would be rewarded for producing an uninformative answer."""
    rating = score_risk(control, AssessmentStatus.INSUFFICIENT_EVIDENCE)
    assert rating.level is not RiskLevel.LOW
    assert rating.score >= BAND_THRESHOLDS[RiskLevel.MEDIUM.value]


def test_not_applicable_carries_no_rating_at_all():
    rating = score_risk(MFA_CONTROL, AssessmentStatus.NOT_APPLICABLE)
    assert rating.level is RiskLevel.NOT_RATED
    assert rating.score == 0.0
    assert "NOT_APPLICABLE" in rating.rationale


def test_severity_and_likelihood_order_the_statuses_sensibly():
    scores = {
        status: score_risk(MFA_CONTROL, status).score
        for status in (
            AssessmentStatus.EFFECTIVE,
            AssessmentStatus.INSUFFICIENT_EVIDENCE,
            AssessmentStatus.POTENTIAL_DEFICIENCY,
            AssessmentStatus.NOT_EFFECTIVE,
        )
    }
    assert (
        scores[AssessmentStatus.EFFECTIVE]
        < scores[AssessmentStatus.INSUFFICIENT_EVIDENCE]
        < scores[AssessmentStatus.POTENTIAL_DEFICIENCY]
        < scores[AssessmentStatus.NOT_EFFECTIVE]
    )


# ------------------------------------------------------------------ exception ratio
@pytest.mark.parametrize(
    "ratio,expected_likelihood",
    [(0.0, 3.5), (0.02, 3.6), (0.10, 4.0), (0.30, 5.0), (0.90, 5.0)],
)
def test_an_observed_exception_rate_raises_likelihood_and_the_uplift_is_capped(ratio, expected_likelihood):
    factors = derive_factors(MFA_CONTROL, AssessmentStatus.POTENTIAL_DEFICIENCY, exception_ratio=ratio)
    assert factors.likelihood == pytest.approx(expected_likelihood)


@pytest.mark.parametrize("ratio", [-1.0, 2.0, "not a number", None])
def test_a_nonsensical_exception_ratio_is_clamped_or_ignored(ratio):
    rating = score_risk(MFA_CONTROL, AssessmentStatus.POTENTIAL_DEFICIENCY, exception_ratio=ratio)
    assert 0.0 <= rating.score <= 100.0


def test_a_zero_exception_rate_is_reported_as_an_observation():
    rating = score_risk(MFA_CONTROL, AssessmentStatus.EFFECTIVE, exception_ratio=0.0)
    assert "No exceptions were observed" in rating.rationale


# ------------------------------------------------------- the model's advisory opinion
def test_the_model_may_escalate_by_exactly_one_band():
    """Under-rating is the more damaging error, so a nudge upward is allowed - but the
    model may not set the rating, or the arithmetic would be back in its hands."""
    computed = score_risk(LOW_EXPOSURE_CONTROL, AssessmentStatus.POTENTIAL_DEFICIENCY)
    escalated = score_risk(
        LOW_EXPOSURE_CONTROL, AssessmentStatus.POTENTIAL_DEFICIENCY, model_risk_level=RiskLevel.CRITICAL
    )
    assert computed.level is RiskLevel.MEDIUM
    assert escalated.level is RiskLevel.HIGH  # one band, not three
    assert any(a["name"] == "model_escalation" for a in escalated.factors["adjustments"])


def test_the_model_can_never_lower_a_rating():
    rating = score_risk(
        MFA_CONTROL,
        AssessmentStatus.POTENTIAL_DEFICIENCY,
        exception_ratio=0.10,
        model_risk_level=RiskLevel.LOW,
    )
    assert rating.level is RiskLevel.CRITICAL
    assert any(a["name"] == "model_disagreement_recorded" for a in rating.factors["adjustments"])


def test_a_model_that_contradicts_its_own_effective_conclusion_is_recorded_not_obeyed():
    rating = score_risk(
        LOW_EXPOSURE_CONTROL, AssessmentStatus.EFFECTIVE, model_risk_level=RiskLevel.CRITICAL
    )
    assert rating.level is RiskLevel.LOW
    assert any(a["name"] == "model_disagreement_recorded" for a in rating.factors["adjustments"])


def test_the_model_suggestion_is_always_recorded_for_the_reviewer():
    rating = score_risk(MFA_CONTROL, AssessmentStatus.POTENTIAL_DEFICIENCY, model_risk_level=RiskLevel.HIGH)
    assert rating.factors["model_suggested_risk_level"] == "HIGH"
    assert rating.factors["model_agrees_with_computed"] in (True, False)


def test_no_model_suggestion_is_reported_as_absent_not_as_disagreement():
    rating = score_risk(MFA_CONTROL, AssessmentStatus.POTENTIAL_DEFICIENCY)
    assert rating.factors["model_suggested_risk_level"] is None
    assert rating.factors["model_agrees_with_computed"] is None


# ------------------------------------------------------------------ explainability
def test_every_rating_is_labelled_as_a_prototype():
    rating = score_risk(MFA_CONTROL, AssessmentStatus.POTENTIAL_DEFICIENCY)
    assert rating.model_label == RISK_MODEL_LABEL
    assert RISK_MODEL_LABEL in rating.rationale
    assert rating.to_dict()["model_label"] == RISK_MODEL_LABEL
    assert "not an official industry framework" in RISK_MODEL_LABEL


def test_the_rationale_decomposes_the_score_factor_by_factor():
    rating = score_risk(MFA_CONTROL, AssessmentStatus.POTENTIAL_DEFICIENCY, exception_ratio=0.10)
    for label in ("control failure severity", "likelihood", "impact", "privilege level", "data sensitivity"):
        assert label in rating.rationale
    assert "Weighted total: 79.8 / 100" in rating.rationale
    assert "requires auditor review" in rating.rationale


def test_to_dict_is_json_shaped_for_the_assessment_row():
    payload = score_risk(MFA_CONTROL, AssessmentStatus.POTENTIAL_DEFICIENCY).to_dict()
    assert set(payload) >= {"level", "score", "factors", "rationale", "band_thresholds", "model_label"}
    assert isinstance(payload["level"], str)
    assert payload["factors"]["scale"]


# ---------------------------------------------------------------------- robustness
def test_an_orm_control_and_an_equivalent_dict_score_identically(control):
    """The engine passes ORM rows, the harness passes dicts; they must not diverge."""
    as_dict = {
        "control_id": control.control_id,
        "name": control.name,
        "inherent_risk": control.inherent_risk,
        "privilege_level": control.privilege_level,
        "data_sensitivity": control.data_sensitivity,
    }
    from_orm = score_risk(control, AssessmentStatus.POTENTIAL_DEFICIENCY, exception_ratio=0.10)
    from_dict = score_risk(as_dict, AssessmentStatus.POTENTIAL_DEFICIENCY, exception_ratio=0.10)
    assert from_orm.score == from_dict.score
    assert from_orm.level is from_dict.level


@pytest.mark.parametrize(
    "control",
    [None, {}, {"privilege_level": "not a number", "inherent_risk": "unknown band"}],
)
def test_a_missing_or_malformed_control_falls_back_to_the_midpoint(control):
    rating = score_risk(control, AssessmentStatus.POTENTIAL_DEFICIENCY)
    assert isinstance(rating, RiskAssessment)
    assert 0.0 <= rating.score <= 100.0
    assert rating.level in set(RiskLevel)


@pytest.mark.parametrize("status", [None, "", "a status nobody defined"])
def test_an_unrecognised_status_is_treated_as_insufficient_evidence(status):
    rating = score_risk(MFA_CONTROL, status)
    assert rating.level is not RiskLevel.NOT_RATED
    assert "INSUFFICIENT_EVIDENCE" in rating.rationale


@pytest.mark.parametrize("value,expected", [(0, 1.0), (9, 5.0), (3, 3.0)])
def test_factor_inputs_are_clamped_to_the_one_to_five_ladder(value, expected):
    control = dict(MFA_CONTROL, privilege_level=value)
    assert derive_factors(control, AssessmentStatus.EFFECTIVE).privilege_level == expected
