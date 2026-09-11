"""The evaluation arithmetic, checked against values computed by hand.

Every number in the write-up comes out of this module, so nothing here is asserted
against "whatever the function returned". The four-row fixture below is small enough to
work through on paper, and the expected precision, recall, F1, kappa and confusion
matrix are written out in the tests.

The degenerate cases get as much attention as the happy path, because they are where a
metric silently starts lying: a rate over an empty denominator must be ``None`` and not
0.0, and Cohen's kappa must be ``None`` and not 1.0 when both raters used a single
category.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from app.evaluation.metrics import (
    MIN_SAMPLE_SIZE,
    NO_PREDICTION_LABEL,
    POSITIVE_STATUSES,
    agreement_metrics,
    classification_metrics,
    cohens_kappa,
    compare_modes,
    compute_metrics,
    confusion_matrix,
    confusion_matrix_frame,
    deficiency_detection_metrics,
    evidence_metrics,
    group_by_mode,
    normalise_result,
    normalise_results,
    per_class_frame,
    results_to_frame,
    timing_metrics,
)
from app.schemas.enums import AssessmentStatus, HumanDecision, RiskLevel

EFFECTIVE = AssessmentStatus.EFFECTIVE.value
POTENTIAL = AssessmentStatus.POTENTIAL_DEFICIENCY.value
INSUFFICIENT = AssessmentStatus.INSUFFICIENT_EVIDENCE.value
NOT_EFFECTIVE = AssessmentStatus.NOT_EFFECTIVE.value

#: Four predictions, worked through by hand in the tests below.
#:
#:   D1  truth POTENTIAL_DEFICIENCY   predicted POTENTIAL_DEFICIENCY   correct
#:   D2  truth POTENTIAL_DEFICIENCY   predicted INSUFFICIENT_EVIDENCE  abstained
#:   D3  truth EFFECTIVE              predicted EFFECTIVE              correct
#:   D4  truth NOT_EFFECTIVE          predicted POTENTIAL_DEFICIENCY   wrong class
HAND_ROWS = [
    {"dataset_id": "D1", "expected_status": POTENTIAL, "predicted_status": POTENTIAL},
    {"dataset_id": "D2", "expected_status": POTENTIAL, "predicted_status": INSUFFICIENT},
    {"dataset_id": "D3", "expected_status": EFFECTIVE, "predicted_status": EFFECTIVE},
    {"dataset_id": "D4", "expected_status": NOT_EFFECTIVE, "predicted_status": POTENTIAL},
]


# ---------------------------------------------------------------- normalisation
def test_a_dict_an_orm_row_and_a_normalised_result_all_flatten_the_same_way():
    from app.database.models import EvaluationResult

    row = EvaluationResult(
        dataset_id="D1",
        expected_status=POTENTIAL,
        predicted_status=POTENTIAL,
        citation_count=3,
        verified_citation_count=2,
        latency_ms=120,
    )
    from_orm = normalise_result(row)
    from_dict = normalise_result(
        {
            "dataset_id": "D1",
            "expected_status": POTENTIAL,
            "predicted_status": POTENTIAL,
            "citation_count": 3,
            "verified_citation_count": 2,
            "latency_ms": 120,
        }
    )
    assert from_orm == from_dict
    # An already-normalised item passes through untouched.
    assert normalise_results([from_orm])[0] is from_orm


def test_status_correct_is_recomputed_and_the_stored_flag_is_only_reported():
    """A stored flag that disagrees with the labels must not silently drive a metric."""
    result = normalise_result(
        {"expected_status": POTENTIAL, "predicted_status": EFFECTIVE, "status_correct": True}
    )
    assert result.status_correct is False
    assert result.stored_status_correct is True


def test_a_missing_prediction_is_labelled_rather_than_dropped_from_the_denominator():
    """A crashed run must count against accuracy, not disappear from the sample."""
    rows = [
        {"expected_status": POTENTIAL, "predicted_status": ""},
        {"expected_status": POTENTIAL, "predicted_status": POTENTIAL},
    ]
    assert normalise_result(rows[0]).scored is True
    assert normalise_result(rows[0]).status_correct is False

    metrics = classification_metrics(rows)
    assert metrics["n_scored"] == 2
    assert metrics["accuracy"] == pytest.approx(0.5)
    assert NO_PREDICTION_LABEL in metrics["labels"]
    assert confusion_matrix(rows)[POTENTIAL][NO_PREDICTION_LABEL] == 1


def test_the_experiment_mode_is_found_wherever_the_runner_put_it():
    assert normalise_result({"mode": "B_RAG"}).mode == "B_RAG"
    assert normalise_result({"detail": {"experiment_mode": "C_RAG_WORKFLOW"}}).mode == "C_RAG_WORKFLOW"
    assert normalise_result({}).mode == ""


def test_results_to_frame_is_one_row_per_result():
    frame = results_to_frame(HAND_ROWS)
    assert len(frame) == 4
    assert set(frame.columns) >= {"dataset_id", "expected_status", "predicted_status", "status_correct"}


# ------------------------------------------------------- multi-class, by hand
def test_accuracy_and_the_label_universe():
    metrics = classification_metrics(HAND_ROWS)
    assert metrics["n_scored"] == 4
    assert metrics["correct"] == 2
    assert metrics["accuracy"] == pytest.approx(0.5)
    assert set(metrics["labels"]) == {EFFECTIVE, POTENTIAL, INSUFFICIENT, NOT_EFFECTIVE}


@pytest.mark.parametrize(
    "label,precision,recall,f1,support",
    [
        # EFFECTIVE: predicted once (D3), truly once, correct -> 1/1, 1/1
        (EFFECTIVE, 1.0, 1.0, 1.0, 1),
        # POTENTIAL: predicted twice (D1, D4), truly twice (D1, D2), one hit
        (POTENTIAL, 0.5, 0.5, 0.5, 2),
        # INSUFFICIENT: predicted once (D2), never the truth -> precision 0, no support
        (INSUFFICIENT, 0.0, 0.0, 0.0, 0),
        # NOT_EFFECTIVE: truly once (D4), never predicted -> recall 0
        (NOT_EFFECTIVE, 0.0, 0.0, 0.0, 1),
    ],
)
def test_per_class_metrics_match_the_hand_computation(label, precision, recall, f1, support):
    cell = classification_metrics(HAND_ROWS)["per_class"][label]
    assert cell["precision"] == pytest.approx(precision)
    assert cell["recall"] == pytest.approx(recall)
    assert cell["f1"] == pytest.approx(f1)
    assert cell["support"] == support


def test_macro_and_weighted_averages_match_the_hand_computation():
    metrics = classification_metrics(HAND_ROWS)
    # macro F1 = (1.0 + 0.5 + 0.0 + 0.0) / 4
    assert metrics["macro_f1"] == pytest.approx(0.375)
    assert metrics["macro_precision"] == pytest.approx(0.375)
    assert metrics["macro_recall"] == pytest.approx(0.375)
    # weighted F1 = (1.0*1 + 0.5*2 + 0.0*0 + 0.0*1) / 4
    assert metrics["weighted_f1"] == pytest.approx(0.5)


def test_the_zero_division_convention_is_stated_in_the_output():
    metrics = classification_metrics(HAND_ROWS)
    assert "0.0" in metrics["zero_division_convention"]
    assert "accuracy" in metrics["micro_note"]


def test_the_confusion_matrix_is_expected_by_predicted():
    matrix = confusion_matrix(HAND_ROWS)
    assert matrix[POTENTIAL][POTENTIAL] == 1
    assert matrix[POTENTIAL][INSUFFICIENT] == 1
    assert matrix[EFFECTIVE][EFFECTIVE] == 1
    assert matrix[NOT_EFFECTIVE][POTENTIAL] == 1
    assert sum(sum(row.values()) for row in matrix.values()) == 4

    frame = confusion_matrix_frame(HAND_ROWS)
    assert frame.loc[POTENTIAL, INSUFFICIENT] == 1


def test_per_class_frame_is_a_display_table():
    frame = per_class_frame(HAND_ROWS)
    assert list(frame.columns) == ["precision", "recall", "f1", "support"]
    assert frame.loc[POTENTIAL, "support"] == 2


# ----------------------------------------------------- binary deficiency framing
def test_the_default_framing_counts_abstention_as_a_miss():
    """D2 is a real deficiency the system honestly declined to conclude on. Under this
    framing that is a false negative - recall is depressed by exactly the behaviour the
    system is designed to exhibit, and the docstring says so."""
    metrics = deficiency_detection_metrics(HAND_ROWS)
    assert metrics["framing"] == "negative"
    assert (metrics["true_positives"], metrics["false_positives"]) == (2, 0)
    assert (metrics["true_negatives"], metrics["false_negatives"]) == (1, 1)
    assert metrics["precision"] == pytest.approx(1.0)
    assert metrics["recall"] == pytest.approx(2.0 / 3.0)
    assert metrics["specificity"] == pytest.approx(1.0)
    assert metrics["fpr"] == pytest.approx(0.0)
    assert metrics["fnr"] == pytest.approx(1.0 / 3.0)
    assert metrics["f1"] == pytest.approx(0.8)
    assert metrics["accuracy"] == pytest.approx(0.75)
    assert metrics["balanced_accuracy"] == pytest.approx((2.0 / 3.0 + 1.0) / 2.0)


def test_the_abstention_framing_removes_those_rows_and_reports_coverage():
    metrics = deficiency_detection_metrics(HAND_ROWS, "excluded")
    assert metrics["n"] == 3
    assert metrics["n_considered"] == 4
    assert metrics["coverage"] == pytest.approx(0.75)
    assert metrics["false_negatives"] == 0
    assert metrics["recall"] == pytest.approx(1.0)


def test_the_positive_class_is_the_two_deficiency_statuses():
    assert set(POSITIVE_STATUSES) == {POTENTIAL, NOT_EFFECTIVE}


def test_a_system_that_never_raises_a_deficiency_has_no_precision_not_zero():
    """Reporting F1 as 0.0 would read as a measured failure rather than an absent
    measurement."""
    rows = [{"expected_status": EFFECTIVE, "predicted_status": EFFECTIVE}]
    metrics = deficiency_detection_metrics(rows)
    assert metrics["precision"] is None
    assert metrics["recall"] is None
    assert metrics["f1"] is None
    assert metrics["specificity"] == pytest.approx(1.0)


def test_an_unknown_framing_is_refused():
    with pytest.raises(ValueError):
        deficiency_detection_metrics(HAND_ROWS, "whatever")


# ------------------------------------------------------------------- evidence
def test_evidence_rates_name_their_denominators():
    rows = [
        {"citation_count": 4, "verified_citation_count": 3, "fabricated_citation_count": 1},
        {"citation_count": 2, "verified_citation_count": 2},
        {"citation_count": 0, "unsupported_claim_count": 1, "hallucination_detected": True},
    ]
    metrics = evidence_metrics(rows)
    assert metrics["n"] == 3
    assert metrics["citations_total"] == 6
    # assessment-level: 2 of 3 cited anything
    assert metrics["citation_rate"] == pytest.approx(2.0 / 3.0)
    # citation-level: 5 verified of 6 citations. PARTIAL counts as ungrounded here.
    assert metrics["grounding_rate"] == pytest.approx(5.0 / 6.0)
    assert metrics["fabrication_rate"] == pytest.approx(1.0 / 6.0)
    assert metrics["fabricated_assessment_rate"] == pytest.approx(1.0 / 3.0)
    assert metrics["unsupported_claim_rate"] == pytest.approx(1.0 / 3.0)
    assert metrics["hallucination_rate"] == pytest.approx(1.0 / 3.0)
    assert metrics["mean_citations_per_assessment"] == pytest.approx(2.0)
    assert "PARTIAL counts as 0" in metrics["definitions"]["grounding_rate"]


def test_the_clean_rate_requires_a_citation_as_well_as_no_fabrication():
    rows = [{"citation_count": 0}, {"citation_count": 1, "verified_citation_count": 1}]
    assert evidence_metrics(rows)["clean_rate"] == pytest.approx(0.5)


def test_retrieval_recall_is_pooled_and_also_reported_per_dataset():
    rows = [
        {"expected_evidence_hits": 1, "expected_evidence_total": 10},
        {"expected_evidence_hits": 1, "expected_evidence_total": 1},
    ]
    metrics = evidence_metrics(rows)
    assert metrics["retrieval_recall"] == pytest.approx(2.0 / 11.0)  # pooled (micro)
    assert metrics["retrieval_recall_macro"] == pytest.approx((0.1 + 1.0) / 2.0)
    assert metrics["retrieval_datasets_scored"] == 2


def test_missing_evidence_detection_requires_naming_the_gap():
    """A bare "insufficient evidence" tells an auditor nothing about what to obtain."""
    rows = [
        {"expected_status": INSUFFICIENT, "predicted_status": INSUFFICIENT, "declared_missing_evidence": True},
        {"expected_status": INSUFFICIENT, "predicted_status": INSUFFICIENT, "declared_missing_evidence": False},
        {"expected_status": INSUFFICIENT, "predicted_status": POTENTIAL, "declared_missing_evidence": True},
    ]
    metrics = evidence_metrics(rows)
    assert metrics["missing_evidence_cases"] == 3
    assert metrics["missing_evidence_concluded"] == 2
    assert metrics["missing_evidence_detected"] == 1
    assert metrics["missing_evidence_detection_rate"] == pytest.approx(1.0 / 3.0)


def test_a_suite_with_no_insufficient_evidence_case_reports_none_not_zero():
    metrics = evidence_metrics([{"expected_status": EFFECTIVE, "predicted_status": EFFECTIVE}])
    assert metrics["missing_evidence_cases"] == 0
    assert metrics["missing_evidence_detection_rate"] is None


# ------------------------------------------------------------ Cohen's kappa
def test_kappa_matches_the_hand_computation():
    """a = [A,A,A,B], b = [A,A,B,B]: po = 0.75, pe = (3/4)(2/4) + (1/4)(2/4) = 0.5,
    so kappa = (0.75 - 0.5) / 0.5 = 0.5."""
    assert cohens_kappa(["A", "A", "A", "B"], ["A", "A", "B", "B"]) == pytest.approx(0.5)


def test_kappa_is_zero_when_agreement_is_exactly_what_chance_predicts():
    assert cohens_kappa(["A", "B", "A", "B"], ["A", "B", "B", "A"]) == pytest.approx(0.0)


def test_kappa_is_one_for_perfect_agreement_over_more_than_one_category():
    assert cohens_kappa(["A", "B", "A"], ["A", "B", "A"]) == pytest.approx(1.0)


def test_kappa_can_be_negative():
    assert cohens_kappa(["A", "B"], ["B", "A"]) < 0.0


def test_kappa_is_undefined_when_both_raters_used_one_category():
    """Agreement cannot be corrected for chance when chance already predicts everything.
    1.0 would claim perfect chance-corrected agreement from data containing no
    information about disagreement; 0.0 would claim the raters did no better than
    chance."""
    assert cohens_kappa(["A", "A", "A"], ["A", "A", "A"]) is None


def test_kappa_of_nothing_is_none():
    assert cohens_kappa([], []) is None


def test_kappa_refuses_mismatched_sequences():
    with pytest.raises(ValueError):
        cohens_kappa(["A"], ["A", "B"])


# ------------------------------------------------------------ human agreement
def test_agreement_metrics_over_a_hand_built_set():
    reviews = [
        {"ai_status": POTENTIAL, "final_status": POTENTIAL, "decision": HumanDecision.ACCEPTED.value},
        {"ai_status": POTENTIAL, "final_status": NOT_EFFECTIVE, "decision": HumanDecision.MODIFIED.value},
        {"ai_status": EFFECTIVE, "final_status": EFFECTIVE, "decision": HumanDecision.ACCEPTED.value},
        {"ai_status": EFFECTIVE, "final_status": "", "decision": HumanDecision.PENDING.value},
    ]
    metrics = agreement_metrics(reviews)
    assert metrics["n_reviews"] == 4
    assert metrics["n_pending_excluded"] == 1
    assert metrics["n_completed"] == 3
    assert metrics["status_agreement"] == pytest.approx(2.0 / 3.0)
    assert metrics["status_change_rate"] == pytest.approx(1.0 / 3.0)
    assert metrics["modification_rate"] == pytest.approx(1.0 / 3.0)
    assert metrics["acceptance_rate"] == pytest.approx(2.0 / 3.0)


def test_pending_reviews_are_excluded_by_default_but_can_be_included():
    reviews = [{"ai_status": POTENTIAL, "final_status": "", "decision": HumanDecision.PENDING.value}]
    assert agreement_metrics(reviews)["n_completed"] == 0
    assert agreement_metrics(reviews, include_pending=True)["n_completed"] == 1


def test_status_and_risk_agreement_are_separate_judgements():
    reviews = [
        {
            "ai_status": POTENTIAL,
            "final_status": POTENTIAL,
            "ai_risk": RiskLevel.CRITICAL.value,
            "final_risk_level": RiskLevel.MEDIUM.value,
            "decision": HumanDecision.MODIFIED.value,
        }
    ]
    metrics = agreement_metrics(reviews)
    assert metrics["status_agreement"] == pytest.approx(1.0)
    assert metrics["risk_agreement"] == pytest.approx(0.0)


def test_agreement_reads_a_real_review_row(seeded_session, completed_review, completed_assessment):
    metrics = agreement_metrics([completed_review])
    assert metrics["n_completed"] == 1
    assert metrics["status_agreement"] == pytest.approx(1.0)


def test_a_bare_pair_is_accepted():
    metrics = agreement_metrics([(POTENTIAL, POTENTIAL), (POTENTIAL, EFFECTIVE)])
    assert metrics["status_agreement"] == pytest.approx(0.5)


# ------------------------------------------------------------------- timing
def test_timing_distributions():
    rows = [{"latency_ms": 100}, {"latency_ms": 200}, {"latency_ms": 300}]
    metrics = timing_metrics(rows)
    assert metrics["latency_ms"]["mean"] == pytest.approx(200.0)
    assert metrics["latency_ms"]["median"] == pytest.approx(200.0)
    assert metrics["latency_ms"]["min"] == pytest.approx(100.0)
    assert metrics["latency_ms"]["max"] == pytest.approx(300.0)
    assert metrics["latency_ms"]["p95"] == pytest.approx(float(np.percentile([100, 200, 300], 95)))


def test_timing_of_nothing_is_none_everywhere():
    metrics = timing_metrics([])
    assert metrics["latency_ms"]["mean"] is None
    assert metrics["latency_ms"]["p95"] is None


def test_grouping_by_mode():
    rows = [
        {"mode": "A_RAW_LLM", "expected_status": EFFECTIVE, "predicted_status": EFFECTIVE},
        {"mode": "B_RAG", "expected_status": EFFECTIVE, "predicted_status": POTENTIAL},
    ]
    grouped = group_by_mode(rows)
    assert set(grouped) == {"A_RAW_LLM", "B_RAG"}
    frame = compare_modes(grouped)
    assert len(frame) == 2


# -------------------------------------------------------------- empty and degenerate
def test_every_metric_over_an_empty_input_is_none_or_zero_never_a_rate():
    classification = classification_metrics([])
    assert classification["n_scored"] == 0
    assert classification["accuracy"] is None
    assert classification["macro_f1"] is None

    evidence = evidence_metrics([])
    assert evidence["n"] == 0
    assert evidence["citation_rate"] is None
    assert evidence["grounding_rate"] is None

    deficiency = deficiency_detection_metrics([])
    assert deficiency["n"] == 0
    assert deficiency["precision"] is None

    agreement = agreement_metrics([])
    assert agreement["n_completed"] == 0
    assert agreement["status_agreement"] is None
    assert agreement["status_kappa"] is None


def test_rows_with_no_ground_truth_are_not_scored():
    rows = [{"predicted_status": POTENTIAL}, {"expected_status": POTENTIAL, "predicted_status": POTENTIAL}]
    assert classification_metrics(rows)["n_scored"] == 1
    assert deficiency_detection_metrics(rows)["n"] == 1


def test_a_perfect_run_is_reported_as_perfect_but_kappa_stays_undefined():
    rows = [
        {"expected_status": POTENTIAL, "predicted_status": POTENTIAL},
        {"expected_status": POTENTIAL, "predicted_status": POTENTIAL},
    ]
    assert classification_metrics(rows)["accuracy"] == pytest.approx(1.0)
    assert cohens_kappa([POTENTIAL, POTENTIAL], [POTENTIAL, POTENTIAL]) is None


# ------------------------------------------------------------------ the whole set
def test_compute_metrics_is_json_serialisable_and_carries_its_sample_sizes():
    payload = compute_metrics(HAND_ROWS)
    assert json.loads(json.dumps(payload))  # no numpy scalars, no NaN
    assert payload["n"] == 4
    assert payload["n_scored"] == 4
    assert payload["n_errors"] == 0
    assert payload["datasets"] == ["D1", "D2", "D3", "D4"]
    for block in ("classification", "confusion_matrix", "deficiency_detection", "evidence", "timing"):
        assert block in payload
    assert payload["deficiency_detection_excluding_insufficient"]["framing"] == "excluded"


def test_compute_metrics_warns_when_the_sample_is_too_small_to_mean_much():
    """n = 6 is descriptive, not statistical, and the payload has to say so."""
    payload = compute_metrics(HAND_ROWS)
    assert payload["caveats"]
    blob = " ".join(payload["caveats"]).lower()
    assert str(MIN_SAMPLE_SIZE) in blob or "small" in blob


def test_human_agreement_is_omitted_rather_than_reported_as_zeros():
    assert "human_agreement" not in compute_metrics(HAND_ROWS)
    with_reviews = compute_metrics(HAND_ROWS, reviews=[(POTENTIAL, POTENTIAL)])
    assert with_reviews["human_agreement"]["n_completed"] == 1


def test_by_mode_appears_only_when_there_is_more_than_one_mode():
    single = compute_metrics([dict(row, mode="B_RAG") for row in HAND_ROWS])
    assert "by_mode" not in single

    mixed = compute_metrics(
        [dict(HAND_ROWS[0], mode="A_RAW_LLM"), dict(HAND_ROWS[1], mode="B_RAG")]
    )
    assert set(mixed["by_mode"]) == {"A_RAW_LLM", "B_RAG"}


def test_errors_are_counted_and_still_scored_as_wrong_answers():
    rows = HAND_ROWS + [
        {"dataset_id": "D5", "expected_status": POTENTIAL, "predicted_status": "", "error": "provider timeout"}
    ]
    payload = compute_metrics(rows)
    assert payload["n_errors"] == 1
    assert payload["n_scored"] == 5
    assert payload["classification"]["accuracy"] == pytest.approx(2.0 / 5.0)


# ---------------------------------------------------------------- grounded accuracy
def _row(expected, predicted, verified, error=""):
    return {
        "dataset_id": "D", "expected_status": expected, "predicted_status": predicted,
        "verified_citation_count": verified, "citation_count": max(verified, 1), "error": error,
    }


def test_grounded_accuracy_refuses_credit_for_correct_but_uncited_answers():
    """A right answer with no verified citation is not a defensible audit conclusion."""
    from app.evaluation.metrics import grounded_accuracy_metrics

    rows = [
        _row("EFFECTIVE", "EFFECTIVE", 2),              # correct + grounded
        _row("NOT_EFFECTIVE", "NOT_EFFECTIVE", 0),      # correct but ungrounded  <- the point
        _row("EFFECTIVE", "POTENTIAL_DEFICIENCY", 3),   # wrong, grounded
        _row("EFFECTIVE", "POTENTIAL_DEFICIENCY", 0),   # wrong, ungrounded
    ]
    m = grounded_accuracy_metrics(rows)

    assert m["n_scored"] == 4
    assert m["plain_accuracy"] == pytest.approx(0.5)      # two of four match ground truth
    assert m["grounded_accuracy"] == pytest.approx(0.25)  # only one of those is evidenced
    assert m["ungrounded_credit"] == pytest.approx(0.25)  # the gap between the two
    assert m["breakdown"] == {
        "correct_and_grounded": 1,
        "correct_but_ungrounded": 1,
        "incorrect_but_grounded": 1,
        "incorrect_and_ungrounded": 1,
    }


def test_grounded_accuracy_lenient_variant_exempts_correct_abstentions():
    """The documented judgement call: a correct INSUFFICIENT_EVIDENCE may be exempted."""
    from app.evaluation.metrics import grounded_accuracy_metrics

    rows = [_row("INSUFFICIENT_EVIDENCE", "INSUFFICIENT_EVIDENCE", 0)]

    assert grounded_accuracy_metrics(rows, exempt_insufficient=False)["grounded_accuracy"] == 0.0
    assert grounded_accuracy_metrics(rows, exempt_insufficient=True)["grounded_accuracy"] == 1.0


def test_grounded_accuracy_excludes_errored_rows_from_the_denominator():
    from app.evaluation.metrics import grounded_accuracy_metrics

    rows = [_row("EFFECTIVE", "EFFECTIVE", 1), _row("EFFECTIVE", "", 0, error="provider timeout")]
    m = grounded_accuracy_metrics(rows)

    assert m["n_scored"] == 1
    assert m["grounded_accuracy"] == pytest.approx(1.0)


def test_grounded_accuracy_on_empty_input_reports_none_not_zero():
    """An undefined ratio must not masquerade as a measured zero."""
    from app.evaluation.metrics import grounded_accuracy_metrics

    m = grounded_accuracy_metrics([])
    assert m["n_scored"] == 0
    assert m["grounded_accuracy"] is None


def test_compute_metrics_exposes_both_grounded_accuracy_variants():
    from app.evaluation.metrics import compute_metrics

    payload = compute_metrics([_row("EFFECTIVE", "EFFECTIVE", 1)])
    assert "grounded_accuracy" in payload
    assert "grounded_accuracy_lenient" in payload
    assert payload["grounded_accuracy"]["exempt_insufficient"] is False
    assert payload["grounded_accuracy_lenient"]["exempt_insufficient"] is True
