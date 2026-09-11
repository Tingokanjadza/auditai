"""The shared data-access layer used by both front ends.

Two things are worth stating about what is tested here. First, the filter semantics:
both the API and the Streamlit pages depend on them, and a silently-ignored filter is a
page that shows the wrong rows. Second, ``dashboard_stats`` - its counting basis (latest
assessment per control, evaluation runs excluded) is a definitional choice that the
dashboard reports as a headline number, so the definitions block is asserted alongside
the figures.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.audit import service
from app.audit.service import InvalidInputError, NotFoundError
from app.database.seed import seed_controls
from app.schemas.enums import (
    ActivityAction,
    AssessmentStatus,
    EvidenceType,
    ExperimentMode,
    ParseStatus,
    ProjectStatus,
    RiskLevel,
)


# ------------------------------------------------------------------------ projects
def test_create_project_records_who_did_it(seeded_session):
    project = service.create_project(
        seeded_session,
        name="  Access review  ",
        audit_area="IAM",
        auditor_name="A. Auditor",
        control_refs=["CONTROL-001", "CONTROL-005"],
        actor="A. Auditor",
    )
    assert project.name == "Access review"
    assert project.status == ProjectStatus.PLANNING.value
    assert len(service.list_scoped_controls(seeded_session, project.id)) == 2

    entries = service.list_activity(seeded_session, project_id=project.id, entity_type="project")
    assert entries and entries[0].action == ActivityAction.PROJECT_CREATED.value


def test_the_auditor_name_defaults_to_configuration(seeded_session, settings):
    project = service.create_project(seeded_session, name="Nameless", audit_area="IAM")
    assert project.auditor_name == settings.default_auditor_name


@pytest.mark.parametrize("name,area", [("", "IAM"), ("   ", "IAM"), ("Project", ""), ("Project", "  ")])
def test_a_project_needs_a_name_and_an_area(seeded_session, name, area):
    with pytest.raises(InvalidInputError):
        service.create_project(seeded_session, name=name, audit_area=area)


def test_list_projects_filters(seeded_session):
    service.create_project(seeded_session, name="Alpha review", audit_area="IAM")
    service.create_project(seeded_session, name="Beta review", audit_area="Change", status="FIELDWORK")
    service.create_project(seeded_session, name="Demo data", audit_area="IAM", is_demo=True)

    assert len(service.list_projects(seeded_session)) == 3
    assert len(service.list_projects(seeded_session, include_demo=False)) == 2
    assert [p.name for p in service.list_projects(seeded_session, status="FIELDWORK")] == ["Beta review"]
    assert [p.name for p in service.list_projects(seeded_session, search="alpha")] == ["Alpha review"]
    assert [p.name for p in service.list_projects(seeded_session, search="Change")] == ["Beta review"]


def test_projects_are_newest_first(seeded_session):
    first = service.create_project(seeded_session, name="First", audit_area="IAM")
    second = service.create_project(seeded_session, name="Second", audit_area="IAM")
    assert [p.id for p in service.list_projects(seeded_session)][0] == second.id


def test_require_project_raises_a_typed_error(seeded_session):
    assert service.get_project(seeded_session, 999) is None
    with pytest.raises(NotFoundError):
        service.require_project(seeded_session, 999)


def test_update_project_applies_a_whitelist(seeded_session, project):
    service.update_project(
        seeded_session,
        project.id,
        actor="A. Auditor",
        status="FIELDWORK",
        period_start="2024-01-01",
        description="Updated.",
    )
    refreshed = service.require_project(seeded_session, project.id)
    assert refreshed.status == ProjectStatus.FIELDWORK.value
    assert refreshed.period_start.year == 2024
    assert refreshed.description == "Updated."


def test_unknown_update_fields_are_rejected_not_ignored(seeded_session, project):
    """Silently dropping a field the caller asked for is the worse failure mode."""
    with pytest.raises(InvalidInputError):
        service.update_project(seeded_session, project.id, is_demo=True)


def test_a_project_name_cannot_be_blanked(seeded_session, project):
    with pytest.raises(InvalidInputError):
        service.update_project(seeded_session, project.id, name="   ")


def test_project_to_dict_carries_the_counts_both_front_ends_show(seeded_session, ingested_project):
    payload = service.project_to_dict(seeded_session, ingested_project)
    assert payload["controls_in_scope"] == 1
    assert payload["evidence_files"] == 4
    assert payload["control_refs"] == ["CONTROL-001"]
    assert payload["period_label"]


def test_deleting_a_project_cascades_to_its_evidence(seeded_session, ingested_project):
    project_id = ingested_project.id
    assert service.delete_project(seeded_session, project_id) is True
    assert service.list_evidence(seeded_session, project_id=project_id) == []
    assert service.delete_project(seeded_session, project_id) is False


# ------------------------------------------------------------------------ controls
def test_the_library_seeds_idempotently(seeded_session):
    before = len(service.list_controls(seeded_session))
    assert before >= 5
    assert seed_controls(seeded_session) == 0
    assert len(service.list_controls(seeded_session)) == before


def test_seeding_with_overwrite_restores_an_edited_definition(seeded_session):
    service.update_control(seeded_session, "CONTROL-001", name="Edited locally")
    assert service.require_control(seeded_session, "CONTROL-001").name == "Edited locally"
    seed_controls(seeded_session, overwrite=True)
    assert service.require_control(seeded_session, "CONTROL-001").name != "Edited locally"


@pytest.mark.parametrize("reference", ["CONTROL-001", 1])
def test_a_control_resolves_by_reference_or_primary_key(seeded_session, reference):
    control = service.require_control(seeded_session, reference)
    assert control.control_id == "CONTROL-001"


def test_get_control_passes_an_orm_row_straight_through(seeded_session, control):
    assert service.get_control(seeded_session, control) is control


def test_unknown_control_raises(seeded_session):
    assert service.get_control(seeded_session, "CONTROL-NOPE") is None
    with pytest.raises(NotFoundError):
        service.require_control(seeded_session, "CONTROL-NOPE")


def test_list_controls_filters(seeded_session):
    categories = service.control_categories(seeded_session)
    assert "Logical Access" in categories

    logical = service.list_controls(seeded_session, category="Logical Access")
    assert logical and all(c.category == "Logical Access" for c in logical)
    assert [c.control_id for c in service.list_controls(seeded_session, search="CONTROL-001")] == ["CONTROL-001"]
    assert service.list_controls(seeded_session)[0].control_id == "CONTROL-001"  # ordered by reference


def test_retiring_a_control_is_a_soft_delete(seeded_session, project):
    """Assessments reference the row, so removing it would break the evidence trail."""
    service.deactivate_control(seeded_session, "CONTROL-002")
    active = {c.control_id for c in service.list_controls(seeded_session, active_only=True)}
    everything = {c.control_id for c in service.list_controls(seeded_session, active_only=False)}
    assert "CONTROL-002" not in active
    assert "CONTROL-002" in everything
    assert service.require_control(seeded_session, "CONTROL-002") is not None

    service.set_control_active(seeded_session, "CONTROL-002", True)
    assert service.require_control(seeded_session, "CONTROL-002").is_active is True


def test_create_control_validates_and_clamps(seeded_session):
    control = service.create_control(
        seeded_session,
        {
            "control_id": "CONTROL-900",
            "name": "Synthetic test control",
            "privilege_level": 99,
            "data_sensitivity": -5,
            "expected_evidence": "a single artefact",
        },
    )
    assert control.privilege_level == 5
    assert control.data_sensitivity == 1
    assert control.inherent_risk == RiskLevel.MEDIUM.value
    assert control.expected_evidence == ["a single artefact"]


def test_an_unknown_enum_value_is_reported_rather_than_defaulted(seeded_session):
    """Quietly recording MEDIUM for a risk band the caller mistyped would hide the typo."""
    with pytest.raises(InvalidInputError) as excinfo:
        service.create_control(
            seeded_session,
            {"control_id": "CONTROL-902", "name": "Bad band", "inherent_risk": "nonsense"},
        )
    assert "inherent_risk" in str(excinfo.value)


@pytest.mark.parametrize(
    "payload",
    [{"name": "No reference"}, {"control_id": "CONTROL-901"}, {"control_id": "CONTROL-001", "name": "Clash"}],
)
def test_create_control_rejects_bad_input(seeded_session, payload):
    with pytest.raises(InvalidInputError):
        service.create_control(seeded_session, payload)


def test_a_control_reference_is_immutable(seeded_session):
    with pytest.raises(InvalidInputError):
        service.update_control(seeded_session, "CONTROL-001", control_id="CONTROL-002")


# -------------------------------------------------------------------------- scoping
def test_scoping_is_idempotent(seeded_session, project):
    first = service.scope_control(seeded_session, project.id, "CONTROL-005")
    second = service.scope_control(seeded_session, project.id, "CONTROL-005")
    assert first.id == second.id
    assert len(service.list_scoped_controls(seeded_session, project.id)) == 2


def test_unscoping_leaves_past_assessments_alone(seeded_session, completed_assessment):
    project_id = completed_assessment.project_id
    assert service.unscope_control(seeded_session, project_id, "CONTROL-001") is True
    assert service.list_scoped_controls(seeded_session, project_id) == []
    assert service.get_assessment(seeded_session, completed_assessment.id) is not None
    assert service.unscope_control(seeded_session, project_id, "CONTROL-001") is False


def test_unscoped_controls_are_the_candidate_picker(seeded_session, project):
    scoped = {c.control_id for c in service.list_scoped_controls(seeded_session, project.id)}
    unscoped = {c.control_id for c in service.list_unscoped_controls(seeded_session, project.id)}
    assert scoped == {"CONTROL-001"}
    assert "CONTROL-001" not in unscoped
    assert scoped | unscoped == {c.control_id for c in service.list_controls(seeded_session)}


# ------------------------------------------------------------------------ evidence
def test_evidence_listing_and_filters(seeded_session, ingested_project):
    files = service.list_evidence(seeded_session, project_id=ingested_project.id)
    assert len(files) == 4
    assert all(f.parse_status == ParseStatus.PARSED.value for f in files)

    listings = service.list_evidence(
        seeded_session, project_id=ingested_project.id, evidence_type=EvidenceType.USER_LISTING.value
    )
    assert [f.filename for f in listings] == ["Privileged_Accounts_Export.csv"]
    assert service.list_evidence(seeded_session, project_id=ingested_project.id, search="policy")


def test_evidence_to_dict_hides_the_server_filesystem(seeded_session, ingested_project):
    payload = service.evidence_to_dict(service.list_evidence(seeded_session, project_id=ingested_project.id)[0])
    assert "stored_path" not in payload
    assert len(payload["sha256_short"]) == 12
    assert payload["size_kb"] >= 0


def test_evidence_metadata_answers_is_this_actually_searchable(seeded_session, ingested_project):
    csv_file = [
        f for f in service.list_evidence(seeded_session, project_id=ingested_project.id)
        if f.filename.endswith(".csv")
    ][0]
    meta = service.evidence_metadata(seeded_session, csv_file.id)
    assert meta["chunks_stored"] > 0
    assert meta["chunks_embedded"] == meta["chunks_stored"]
    assert "TABLE_SUMMARY" in meta["chunks_by_source_type"]


def test_chunk_listing_is_ordered_and_pageable(seeded_session, ingested_project):
    files = service.list_evidence(seeded_session, project_id=ingested_project.id)
    chunks = service.list_chunks(seeded_session, files[0].id)
    assert [c.chunk_index for c in chunks] == sorted(c.chunk_index for c in chunks)
    assert service.list_chunks(seeded_session, files[0].id, limit=1) == chunks[:1]
    assert service.list_chunks(seeded_session, files[0].id, offset=1) == chunks[1:]
    assert service.get_chunk(seeded_session, chunks[0].id) is chunks[0]


def test_project_evidence_stats(seeded_session, ingested_project):
    stats = service.project_evidence_stats(seeded_session, ingested_project.id)
    assert stats["evidence_files"] == 4
    assert stats["evidence_chunks"] > 0
    assert stats["total_bytes"] > 0


def test_deleting_evidence_removes_its_chunks_and_records_the_act(seeded_session, ingested_project):
    from app.evidence.service import delete_evidence

    target = service.list_evidence(seeded_session, project_id=ingested_project.id)[0]
    assert delete_evidence(seeded_session, target.id) is True
    assert service.get_evidence_file(seeded_session, target.id) is None
    assert service.list_chunks(seeded_session, target.id) == []

    entries = service.list_activity(seeded_session, project_id=ingested_project.id, entity_type="evidence_file")
    assert any(e.action == ActivityAction.EVIDENCE_DELETED.value for e in entries)
    assert delete_evidence(seeded_session, target.id) is False


def test_an_unsupported_upload_is_stored_rather_than_dropped(seeded_session, project):
    """Nothing the auditor supplied may disappear silently."""
    from app.evidence.service import ingest_file

    record = ingest_file(seeded_session, project.id, b"MZ binary", "tool.exe")
    assert record.id is not None
    assert record.parse_status == ParseStatus.UNSUPPORTED.value
    assert record.parse_error
    assert record.sha256


def test_ingesting_into_a_missing_project_raises(seeded_session):
    from app.evidence.service import EvidenceIngestError, ingest_file

    with pytest.raises(EvidenceIngestError):
        ingest_file(seeded_session, 987654, b"A,B\n1,2\n", "x.csv")


def test_an_oversized_upload_is_refused(seeded_session, project, settings):
    from app.evidence.service import EvidenceTooLargeError, ingest_file

    payload = b"x" * (settings.max_upload_bytes + 1)
    with pytest.raises(EvidenceTooLargeError):
        ingest_file(seeded_session, project.id, payload, "huge.csv")


def test_chunk_context_shows_a_citation_in_its_surroundings(seeded_session, ingested_project):
    from app.evidence.service import get_chunk_context

    files = service.list_evidence(seeded_session, project_id=ingested_project.id)
    chunks = service.list_chunks(seeded_session, files[0].id)
    context = get_chunk_context(seeded_session, chunks[0].id, window=1)
    assert context["found"] is True
    assert context["locator_text"]
    assert context["text"] == chunks[0].text
    assert context["context_text"]
    assert get_chunk_context(seeded_session, 999999)["found"] is False


# --------------------------------------------------------------------- assessments
def test_assessment_filters(seeded_session, ingested_project, engine_factory):
    engine = engine_factory()
    engine.assess_control(ingested_project.id, "CONTROL-001", mode=ExperimentMode.B_RAG)
    engine.assess_control(ingested_project.id, "CONTROL-001", mode=ExperimentMode.C_RAG_WORKFLOW)

    all_rows = service.list_assessments(seeded_session, project_id=ingested_project.id)
    assert len(all_rows) == 2
    assert len(service.list_assessments(seeded_session, mode=ExperimentMode.B_RAG.value)) == 1
    assert len(service.list_assessments(seeded_session, control_id="CONTROL-001")) == 2
    assert service.list_assessments(seeded_session, control_id="CONTROL-005") == []
    assert len(service.list_assessments(seeded_session, limit=1)) == 1
    assert len(service.list_assessments(seeded_session, offset=1)) == 1


def test_latest_per_control_collapses_re_runs(seeded_session, ingested_project, engine_factory):
    engine = engine_factory()
    engine.assess_control(ingested_project.id, "CONTROL-001", mode=ExperimentMode.B_RAG)
    newest = engine.assess_control(ingested_project.id, "CONTROL-001", mode=ExperimentMode.C_RAG_WORKFLOW)

    latest = service.list_assessments(seeded_session, latest_per_control=True)
    assert len(latest) == 1
    assert latest[0].id == newest.assessment_id
    assert service.latest_assessment_for_control(seeded_session, ingested_project.id, "CONTROL-001").id == (
        newest.assessment_id
    )


def test_an_unknown_filter_value_is_rejected(seeded_session):
    with pytest.raises(InvalidInputError):
        service.list_assessments(seeded_session, status="NOT_A_STATUS")


def test_assessment_to_dict_always_declares_its_provenance(seeded_session, completed_assessment):
    payload = service.assessment_to_dict(completed_assessment)
    assert payload["source"] == "AI-generated"
    assert payload["requires_human_review"] is True
    assert payload["control_name"]
    assert payload["citation_count"] == len(payload["citations"])
    assert payload["citations"][0]["chunk_resolved"] is True
    assert payload["citations"][0]["chunk_text"]


def test_findings_are_the_latest_deficiency_per_control(seeded_session, dataset_project, engine_factory):
    dataset, project = dataset_project("DATASET-001")
    engine_factory().assess_control(project.id, dataset.control_ref, mode=ExperimentMode.C_RAG_WORKFLOW)
    findings = service.list_findings(seeded_session, project_id=project.id)
    assert len(findings) == 1
    assert findings[0].status == AssessmentStatus.POTENTIAL_DEFICIENCY.value
    assert service.list_findings(seeded_session, project_id=project.id, high_risk_only=True)


def test_an_effective_assessment_is_not_a_finding(seeded_session, dataset_project, engine_factory):
    dataset, project = dataset_project("DATASET-006")
    result = engine_factory().assess_control(
        project.id, dataset.control_ref, mode=ExperimentMode.C_RAG_WORKFLOW
    )
    assert result.output.status is AssessmentStatus.EFFECTIVE
    assert service.list_findings(seeded_session, project_id=project.id) == []


# ---------------------------------------------------------------------- dashboard
def test_dashboard_of_an_empty_database_reports_zeros_not_percentages(seeded_session):
    stats = service.dashboard_stats(seeded_session)
    assert stats["controls_assessed"] == 0
    assert stats["human_ai_agreement_rate"] == 0.0, "never '100% of nothing'"
    assert stats["citation_grounding_rate"] == 0.0
    assert stats["total_controls"] >= 5
    assert stats["definitions"]["counting_basis"]


def test_dashboard_counts_each_control_once_however_often_it_was_run(
    seeded_session, ingested_project, engine_factory
):
    engine = engine_factory()
    for mode in ExperimentMode:
        engine.assess_control(ingested_project.id, "CONTROL-001", mode=mode)

    stats = service.dashboard_stats(seeded_session, project_id=ingested_project.id)
    assert stats["assessments_counted"] == 1
    assert stats["controls_assessed"] == 1
    assert stats["controls_in_scope"] == 1


def test_dashboard_reflects_a_completed_review(seeded_session, completed_review, completed_assessment):
    stats = service.dashboard_stats(seeded_session, project_id=completed_assessment.project_id)
    assert stats["completed_reviews"] == 1
    assert stats["pending_human_reviews"] == 0
    assert stats["human_ai_agreement_rate"] == 1.0
    assert stats["citations_total"] >= stats["citations_verified"]


def test_status_and_risk_breakdowns_cover_every_member(seeded_session, completed_assessment):
    statuses = service.status_breakdown(seeded_session)
    risks = service.risk_breakdown(seeded_session)
    assert set(statuses) == set(AssessmentStatus.values())
    assert set(risks) == set(RiskLevel.values())
    assert sum(statuses.values()) == 1


def test_evaluation_assessments_are_excluded_from_operational_figures(
    seeded_session, ingested_project, engine_factory
):
    """Research runs belong to the experiments, not to an auditor's project."""
    from app.database.models import EvaluationRun

    run = EvaluationRun(name="test run", experiment_mode=ExperimentMode.B_RAG.value)
    seeded_session.add(run)
    seeded_session.commit()

    engine_factory().assess_control(
        ingested_project.id, "CONTROL-001", mode=ExperimentMode.B_RAG, evaluation_run_id=run.id
    )
    stats = service.dashboard_stats(seeded_session, project_id=ingested_project.id)
    assert stats["controls_assessed"] == 0
    assert service.list_findings(seeded_session, project_id=ingested_project.id) == []
    assert service.list_assessments(seeded_session, include_evaluation=False) == []
    assert len(service.list_assessments(seeded_session, include_evaluation=True)) == 1


# ---------------------------------------------------------------- activity trail
def test_the_activity_trail_is_append_only_and_filterable(seeded_session, ingested_project):
    entries = service.list_activity(seeded_session, project_id=ingested_project.id)
    assert entries
    assert entries == sorted(entries, key=lambda e: (e.created_at, e.id), reverse=True)

    uploads = service.list_activity(
        seeded_session, project_id=ingested_project.id, action=ActivityAction.EVIDENCE_UPLOADED.value
    )
    assert len(uploads) == 4
    assert all(e.actor_type == "HUMAN" for e in uploads)


def test_log_activity_accepts_an_enum_or_a_plain_string(seeded_session):
    a = service.log_activity(seeded_session, entity_type="control", action=ActivityAction.PROJECT_CREATED)
    b = service.log_activity(seeded_session, entity_type="control", action="CONTROL_CREATED")
    assert a.action == ActivityAction.PROJECT_CREATED.value
    assert b.action == "CONTROL_CREATED"


def test_activity_limit_is_honoured(seeded_session, ingested_project):
    assert len(service.list_activity(seeded_session, limit=2)) == 2


# ------------------------------------------------------------------- date coercion
@pytest.mark.parametrize(
    "value",
    ["2024-06-30", "2024-06-30T12:00:00", datetime(2024, 6, 30, tzinfo=timezone.utc), None],
)
def test_project_periods_accept_the_shapes_the_front_ends_send(seeded_session, value):
    project = service.create_project(
        seeded_session, name="Dated", audit_area="IAM", period_start=value, period_end=value
    )
    if value is None:
        assert project.period_start is None
    else:
        assert project.period_start.year == 2024


# --------------------------------------------------- evaluation project isolation
def test_evaluation_projects_are_hidden_from_the_operational_project_list(seeded_session):
    """A research sweep must not swamp the auditor's project picker.

    One A/B/C sweep over six datasets creates eighteen throwaway projects. They stay
    queryable, but an auditor opening the app should see engagements, not experiments.
    """
    from app.audit import service
    from app.evaluation.runner import EVALUATION_SCOPE_TAG as RUNNER_TAG

    # The service layer duplicates the marker to avoid depending on the evaluation
    # package; if the runner ever changes it, this test is the tripwire.
    assert service.EVALUATION_SCOPE_TAG == RUNNER_TAG

    before = len(service.list_projects(seeded_session))

    harness_project = service.create_project(
        seeded_session,
        name="[EVALUATION] DATASET-001 A_RAW_LLM run-1",
        audit_area="Research",
    )
    harness_project.scope_note = "{0} dataset=DATASET-001 mode=A_RAW_LLM run=1".format(RUNNER_TAG)
    seeded_session.flush()

    assert len(service.list_projects(seeded_session)) == before
    assert len(service.list_projects(seeded_session, include_evaluation=True)) == before + 1
    assert harness_project.id in [
        p.id for p in service.list_projects(seeded_session, include_evaluation=True)
    ]


def test_a_real_project_named_like_an_evaluation_is_not_hidden(seeded_session):
    """Exclusion keys off the scope_note marker, never off the project name."""
    from app.audit import service

    decoy = service.create_project(
        seeded_session,
        name="[EVALUATION] Q3 readiness review",
        audit_area="Identity and Access Management",
    )
    seeded_session.flush()

    assert decoy.id in [p.id for p in service.list_projects(seeded_session)]
