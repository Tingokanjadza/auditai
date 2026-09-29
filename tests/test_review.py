"""Human review, and the agreement flags the whole human/AI study is measured on.

``record_human_review`` derives ``agreed_with_ai_status`` on write, and the definition it
uses is a research decision, not an implementation detail: agreement is about the
**outcome**, so a MODIFIED or REJECTED decision that lands on the same status still
counts as agreement. The raw decision is stored beside it, so a stricter definition
("ACCEPTED only") can be recomputed later from the same rows.

The tests below pin that definition down case by case, including the two defaults that
shape the metric: an omitted final status carries the AI's over, except for
MORE_EVIDENCE_REQUESTED, where it does not.
"""

from __future__ import annotations

import pytest

from app.audit import service
from app.audit.service import InvalidInputError, NotFoundError
from app.schemas.enums import AssessmentStatus, ExperimentMode, HumanDecision, RiskLevel


@pytest.fixture()
def ai_conclusion(completed_assessment):
    """What the AI said, so each test can agree or disagree with it explicitly."""
    return {
        "status": completed_assessment.status,
        "risk_level": completed_assessment.risk_level,
    }


# ------------------------------------------------------------------ the three cases
def test_accepted_agrees_on_status_and_risk(seeded_session, completed_assessment, ai_conclusion):
    review = service.record_human_review(
        seeded_session, completed_assessment.id, "A. Auditor", HumanDecision.ACCEPTED
    )
    assert review.final_status == ai_conclusion["status"]
    assert review.final_risk_level == ai_conclusion["risk_level"]
    assert review.agreed_with_ai_status is True
    assert review.agreed_with_ai_risk is True


def test_rejected_with_a_different_conclusion_disagrees(seeded_session, completed_assessment):
    review = service.record_human_review(
        seeded_session,
        completed_assessment.id,
        "A. Auditor",
        HumanDecision.REJECTED,
        final_status=AssessmentStatus.EFFECTIVE.value,
        final_risk_level=RiskLevel.LOW.value,
        comments="The exceptions were approved in the exception register.",
    )
    assert review.final_status == AssessmentStatus.EFFECTIVE.value
    assert review.agreed_with_ai_status is False
    assert review.agreed_with_ai_risk is False


def test_modified_that_keeps_the_conclusion_still_counts_as_agreement(
    seeded_session, completed_assessment, ai_conclusion
):
    """The metric is about the control's condition, not about the editing."""
    review = service.record_human_review(
        seeded_session,
        completed_assessment.id,
        "A. Auditor",
        HumanDecision.MODIFIED,
        final_finding="Rewritten in the auditor's own words.",
        final_recommendation="Enrol the five accounts before period end.",
    )
    assert review.decision == HumanDecision.MODIFIED.value
    assert review.final_status == ai_conclusion["status"]
    assert review.agreed_with_ai_status is True
    assert review.final_finding


def test_modified_that_changes_the_conclusion_disagrees(seeded_session, completed_assessment):
    review = service.record_human_review(
        seeded_session,
        completed_assessment.id,
        "A. Auditor",
        HumanDecision.MODIFIED,
        final_status=AssessmentStatus.NOT_EFFECTIVE.value,
    )
    assert review.agreed_with_ai_status is False


def test_rejected_that_lands_on_the_same_status_is_recorded_as_agreement(
    seeded_session, completed_assessment, ai_conclusion
):
    """Deliberate: the auditor rejected the *reasoning*, not the conclusion. The raw
    decision is kept so a stricter definition can be recomputed."""
    review = service.record_human_review(
        seeded_session,
        completed_assessment.id,
        "A. Auditor",
        HumanDecision.REJECTED,
        final_status=ai_conclusion["status"],
        comments="Right answer, wrong reasoning.",
    )
    assert review.agreed_with_ai_status is True
    assert review.decision == HumanDecision.REJECTED.value


# ---------------------------------------------------------------------- defaults
def test_omitting_the_final_status_carries_the_ai_status_over(
    seeded_session, completed_assessment, ai_conclusion
):
    review = service.record_human_review(
        seeded_session, completed_assessment.id, "A. Auditor", HumanDecision.ACCEPTED
    )
    assert review.final_status == ai_conclusion["status"]


def test_asking_for_more_evidence_is_not_a_conclusion(seeded_session, completed_assessment):
    """Recording the AI's status as the auditor's would inflate agreement with a
    decision the auditor did not make."""
    review = service.record_human_review(
        seeded_session,
        completed_assessment.id,
        "A. Auditor",
        HumanDecision.MORE_EVIDENCE_REQUESTED,
        requested_evidence=["Exception register", "Conditional access export"],
    )
    assert review.final_status == AssessmentStatus.INSUFFICIENT_EVIDENCE.value
    assert review.agreed_with_ai_status is False
    assert review.requested_evidence == ["Exception register", "Conditional access export"]


def test_a_pending_review_never_counts_as_agreement(seeded_session, completed_assessment):
    """Someone opened the review screen. That is not a human judgement."""
    review = service.record_human_review(
        seeded_session, completed_assessment.id, "A. Auditor", HumanDecision.PENDING
    )
    assert review.final_status == ""
    assert review.final_risk_level == RiskLevel.NOT_RATED.value
    assert review.agreed_with_ai_status is False
    assert review.agreed_with_ai_risk is False


def test_status_and_risk_agreement_are_recorded_separately(seeded_session, completed_assessment):
    """An auditor routinely keeps the conclusion and re-rates its severity."""
    review = service.record_human_review(
        seeded_session,
        completed_assessment.id,
        "A. Auditor",
        HumanDecision.MODIFIED,
        final_risk_level=RiskLevel.MEDIUM.value,
    )
    assert review.agreed_with_ai_status is True
    assert review.agreed_with_ai_risk is (completed_assessment.risk_level == RiskLevel.MEDIUM.value)


# ------------------------------------------------------------------- bookkeeping
def test_the_review_never_edits_the_ai_record(seeded_session, completed_assessment):
    """The pair is what makes agreement measurable; merging them would erase it."""
    before = (completed_assessment.status, completed_assessment.risk_level, completed_assessment.finding)
    service.record_human_review(
        seeded_session,
        completed_assessment.id,
        "A. Auditor",
        HumanDecision.REJECTED,
        final_status=AssessmentStatus.EFFECTIVE.value,
        final_finding="Different text entirely.",
    )
    refreshed = service.require_assessment(seeded_session, completed_assessment.id)
    assert (refreshed.status, refreshed.risk_level, refreshed.finding) == before


def test_reviews_accumulate_and_the_latest_one_wins(seeded_session, completed_assessment):
    service.record_human_review(seeded_session, completed_assessment.id, "First", HumanDecision.PENDING)
    second = service.record_human_review(
        seeded_session, completed_assessment.id, "Second", HumanDecision.ACCEPTED
    )
    # The fixture session already holds this assessment with an empty review collection;
    # a real caller gets a fresh session per request, so expire it before re-reading.
    seeded_session.expire(completed_assessment)
    refreshed = service.require_assessment(seeded_session, completed_assessment.id)
    assert len(refreshed.reviews) == 2
    assert refreshed.latest_review.id == second.id
    assert refreshed.is_reviewed is True


def test_a_pending_review_leaves_the_assessment_unreviewed(seeded_session, completed_assessment):
    service.record_human_review(seeded_session, completed_assessment.id, "A", HumanDecision.PENDING)
    seeded_session.expire(completed_assessment)
    assert service.require_assessment(seeded_session, completed_assessment.id).is_reviewed is False


def test_review_metadata_for_the_study_is_captured(seeded_session, completed_assessment):
    review = service.record_human_review(
        seeded_session,
        completed_assessment.id,
        "A. Auditor",
        HumanDecision.ACCEPTED,
        review_seconds=185.5,
        usefulness_rating=4,
        flagged_hallucination=True,
        hallucination_note="Quoted a row that reads oddly.",
    )
    assert review.review_seconds == pytest.approx(185.5)
    assert review.usefulness_rating == 4
    assert review.flagged_hallucination is True
    assert review.hallucination_note


@pytest.mark.parametrize(
    "rating,expected",
    [(0, 1), (6, 5), (1, 1), (5, 5), (None, None), ("", None), ("3", 3), ("high", None)],
)
def test_the_usefulness_rating_is_clamped_to_its_scale(
    seeded_session, completed_assessment, rating, expected
):
    review = service.record_human_review(
        seeded_session, completed_assessment.id, "A", HumanDecision.ACCEPTED, usefulness_rating=rating
    )
    assert review.usefulness_rating == expected


def test_negative_review_time_is_floored_at_zero(seeded_session, completed_assessment):
    review = service.record_human_review(
        seeded_session, completed_assessment.id, "A", HumanDecision.ACCEPTED, review_seconds=-10
    )
    assert review.review_seconds == 0.0


@pytest.mark.parametrize("name", ["", "   ", None])
def test_a_blank_reviewer_name_is_refused(seeded_session, completed_assessment, name):
    """A review is the human half of the record; an anonymous one cannot be attributed."""
    with pytest.raises(InvalidInputError) as excinfo:
        service.record_human_review(seeded_session, completed_assessment.id, name, HumanDecision.ACCEPTED)
    assert "Reviewer name is required" in str(excinfo.value)
    assert service.list_reviews(seeded_session, assessment_id=completed_assessment.id) == []


def test_the_reviewer_name_is_stored_trimmed(seeded_session, completed_assessment):
    review = service.record_human_review(
        seeded_session, completed_assessment.id, "  A. Auditor  ", HumanDecision.ACCEPTED
    )
    assert review.reviewer_name == "A. Auditor"


def test_more_evidence_requested_overrides_whatever_the_form_sent(seeded_session, completed_assessment):
    """A stale status left in the form must not turn a request for evidence into a
    conclusion that happens to agree with the AI."""
    review = service.record_human_review(
        seeded_session,
        completed_assessment.id,
        "A. Auditor",
        HumanDecision.MORE_EVIDENCE_REQUESTED,
        final_status=completed_assessment.status,
        final_risk_level=completed_assessment.risk_level,
    )
    assert review.final_status == AssessmentStatus.INSUFFICIENT_EVIDENCE.value
    assert review.final_risk_level == RiskLevel.NOT_RATED.value
    assert review.agreed_with_ai_status is False
    assert review.agreed_with_ai_risk is False


def test_more_evidence_requested_is_completed_but_never_agreement(seeded_session, completed_assessment):
    """It leaves the queue (the auditor acted) and lands in the denominator only."""
    service.record_human_review(
        seeded_session, completed_assessment.id, "A. Auditor", HumanDecision.MORE_EVIDENCE_REQUESTED
    )
    stats = service.dashboard_stats(seeded_session, project_id=completed_assessment.project_id)
    assert stats["pending_human_reviews"] == 0
    assert stats["completed_reviews"] == 1
    assert stats["human_ai_agreement_rate"] == 0.0
    # The definition is shown to auditors, so it names the decision in plain words.
    assert "request more evidence" in stats["definitions"]["human_ai_agreement_rate"]
    assert "never agreement" in stats["definitions"]["human_ai_agreement_rate"]


def test_pending_overrides_whatever_the_form_sent(seeded_session, completed_assessment):
    review = service.record_human_review(
        seeded_session,
        completed_assessment.id,
        "A. Auditor",
        HumanDecision.PENDING,
        final_status=completed_assessment.status,
        final_risk_level=completed_assessment.risk_level,
    )
    assert review.final_status == ""
    assert review.final_risk_level == RiskLevel.NOT_RATED.value
    assert review.agreed_with_ai_status is False
    assert review.agreed_with_ai_risk is False


def test_a_bad_final_value_is_still_reported_when_the_decision_would_override_it(
    seeded_session, completed_assessment
):
    with pytest.raises(InvalidInputError):
        service.record_human_review(
            seeded_session,
            completed_assessment.id,
            "A. Auditor",
            HumanDecision.MORE_EVIDENCE_REQUESTED,
            final_status="NOT_A_STATUS",
        )


def test_the_review_is_written_to_the_activity_trail(seeded_session, completed_assessment):
    service.record_human_review(seeded_session, completed_assessment.id, "A. Auditor", HumanDecision.ACCEPTED)
    entries = service.list_activity(
        seeded_session, project_id=completed_assessment.project_id, entity_type="assessment"
    )
    recorded = [e for e in entries if e.action == "HUMAN_REVIEW_RECORDED"]
    assert recorded
    assert recorded[0].actor_type == "HUMAN"
    assert recorded[0].details["agreed_with_ai_status"] is True


def test_review_to_dict_labels_its_source(seeded_session, completed_review):
    assert service.review_to_dict(completed_review)["source"] == "Human auditor"


# ------------------------------------------------------------------ bad requests
def test_reviewing_a_missing_assessment_raises(seeded_session):
    with pytest.raises(NotFoundError):
        service.record_human_review(seeded_session, 987654, "A", HumanDecision.ACCEPTED)


@pytest.mark.parametrize("decision", ["APPROVED", "", None, 42])
def test_an_unknown_decision_is_refused(seeded_session, completed_assessment, decision):
    with pytest.raises(InvalidInputError):
        service.record_human_review(seeded_session, completed_assessment.id, "A", decision)


@pytest.mark.parametrize("field", ["final_status", "final_risk_level"])
def test_an_unknown_final_value_is_refused(seeded_session, completed_assessment, field):
    with pytest.raises(InvalidInputError):
        service.record_human_review(
            seeded_session,
            completed_assessment.id,
            "A",
            HumanDecision.MODIFIED,
            **{field: "SOMETHING_ELSE"}
        )


# --------------------------------------------------------------------- the queue
def test_the_review_queue_holds_unreviewed_assessments(seeded_session, completed_assessment):
    queue = service.pending_reviews(seeded_session, project_id=completed_assessment.project_id)
    assert [a.id for a in queue] == [completed_assessment.id]

    service.record_human_review(seeded_session, completed_assessment.id, "A", HumanDecision.ACCEPTED)
    assert service.pending_reviews(seeded_session, project_id=completed_assessment.project_id) == []


def test_re_running_a_control_does_not_resurrect_the_superseded_assessment(
    seeded_session, ingested_project, engine_factory
):
    """The queue is computed latest-first and then filtered, so a re-run leaves one item."""
    engine = engine_factory()
    first = engine.assess_control(ingested_project.id, "CONTROL-001", mode=ExperimentMode.B_RAG)
    service.record_human_review(seeded_session, first.assessment_id, "A", HumanDecision.ACCEPTED)
    second = engine.assess_control(ingested_project.id, "CONTROL-001", mode=ExperimentMode.C_RAG_WORKFLOW)

    queue = service.pending_reviews(seeded_session, project_id=ingested_project.id)
    assert [a.id for a in queue] == [second.assessment_id]


def test_the_queue_and_the_dashboard_cannot_disagree(seeded_session, completed_assessment):
    queue = service.pending_reviews(seeded_session, project_id=completed_assessment.project_id)
    stats = service.dashboard_stats(seeded_session, project_id=completed_assessment.project_id)
    assert stats["pending_human_reviews"] == len(queue)


def test_list_assessments_unreviewed_agrees_with_the_dashboard(
    seeded_session, ingested_project, engine_factory
):
    """``reviewed=False`` means "no review, or the latest review is PENDING" - the same
    definition ``pending_reviews`` and ``dashboard_stats`` use. A re-opened review (a
    PENDING row after an ACCEPTED one) must therefore be pending in all three places."""
    service.scope_controls(seeded_session, ingested_project.id, ["CONTROL-005"])
    engine = engine_factory()
    first = engine.assess_control(ingested_project.id, "CONTROL-001", mode=ExperimentMode.B_RAG)
    second = engine.assess_control(ingested_project.id, "CONTROL-005", mode=ExperimentMode.B_RAG)
    # Accepted, then re-opened: the latest row is PENDING, so it is unreviewed again.
    service.record_human_review(seeded_session, first.assessment_id, "A", HumanDecision.ACCEPTED)
    service.record_human_review(seeded_session, first.assessment_id, "A", HumanDecision.PENDING)
    service.record_human_review(seeded_session, second.assessment_id, "A", HumanDecision.ACCEPTED)

    p = ingested_project.id
    unreviewed = service.list_assessments(seeded_session, project_id=p, reviewed=False, latest_per_control=True)
    reviewed = service.list_assessments(seeded_session, project_id=p, reviewed=True, latest_per_control=True)
    assert [a.id for a in unreviewed] == [first.assessment_id]
    assert [a.id for a in reviewed] == [second.assessment_id]
    assert len(unreviewed) == service.dashboard_stats(seeded_session, project_id=p)["pending_human_reviews"]
    assert [a.id for a in service.pending_reviews(seeded_session, project_id=p)] == [a.id for a in unreviewed]


def test_list_reviews_filters(seeded_session, completed_assessment):
    service.record_human_review(seeded_session, completed_assessment.id, "Ada", HumanDecision.PENDING)
    service.record_human_review(seeded_session, completed_assessment.id, "Grace", HumanDecision.ACCEPTED)

    assert len(service.list_reviews(seeded_session)) == 2
    assert len(service.list_reviews(seeded_session, include_pending=False)) == 1
    assert len(service.list_reviews(seeded_session, reviewer_name="Ada")) == 1
    assert len(service.list_reviews(seeded_session, assessment_id=completed_assessment.id)) == 2
    assert len(service.list_reviews(seeded_session, project_id=completed_assessment.project_id)) == 2


def test_agreement_rate_is_computed_over_completed_reviews_only(
    seeded_session, ingested_project, engine_factory
):
    service.scope_controls(seeded_session, ingested_project.id, ["CONTROL-005"])
    engine = engine_factory()
    first = engine.assess_control(ingested_project.id, "CONTROL-001", mode=ExperimentMode.B_RAG)
    second = engine.assess_control(ingested_project.id, "CONTROL-005", mode=ExperimentMode.B_RAG)

    service.record_human_review(seeded_session, first.assessment_id, "A", HumanDecision.ACCEPTED)
    service.record_human_review(
        seeded_session,
        second.assessment_id,
        "A",
        HumanDecision.REJECTED,
        final_status=AssessmentStatus.NOT_APPLICABLE.value,
    )

    stats = service.dashboard_stats(seeded_session, project_id=ingested_project.id)
    assert stats["completed_reviews"] == 2
    assert stats["human_ai_agreement_rate"] == pytest.approx(0.5)
