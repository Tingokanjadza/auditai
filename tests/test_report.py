"""The audit report.

The report is where the system's central honesty claim becomes visible to a reader who
never opens the code: an AI-generated statement and an auditor's conclusion must be
separately labelled and never merged, an unreviewed control must be marked as pending
rather than counted, and the limitations must be printed rather than left implied.

Both renderers iterate the same ``SECTIONS`` tuple and the same ``ReportData`` counters,
so most tests below are parametrised over the two formats: a section that quietly
appears in one and not the other is exactly the drift the design is meant to prevent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.audit import service
from app.audit.report import (
    AI_LABEL,
    HUMAN_LABEL,
    PENDING_LABEL,
    SECTIONS,
    SUPPORTED_FORMATS,
    build_report_data,
    generate_report,
    normalise_format,
    render_report,
    report_filename,
)
from app.audit.risk import RISK_MODEL_LABEL
from app.audit.service import InvalidInputError, NotFoundError
from app.schemas.enums import AssessmentStatus, ExperimentMode, HumanDecision, RiskLevel

FORMATS = list(SUPPORTED_FORMATS)


@pytest.fixture()
def report_project(seeded_session, ingested_project, engine_factory):
    """A project with one assessed control, ready to report on."""
    engine_factory().assess_control(
        ingested_project.id, "CONTROL-001", mode=ExperimentMode.C_RAG_WORKFLOW
    )
    return ingested_project


# ---------------------------------------------------------------- the ten sections
@pytest.mark.parametrize("fmt", FORMATS)
def test_all_ten_mandated_sections_are_present(seeded_session, report_project, fmt):
    content = render_report(seeded_session, report_project.id, generated_by="Tester", fmt=fmt)
    for number, name in SECTIONS:
        assert name in content, "section {0} ({1}) missing from the {2} report".format(number, name, fmt)
    assert len(SECTIONS) == 10


@pytest.mark.parametrize("fmt", FORMATS)
def test_the_two_provenance_labels_appear_verbatim(seeded_session, report_project, fmt):
    """A reader learns to scan for these strings; a near-miss variant would defeat that."""
    content = render_report(seeded_session, report_project.id, fmt=fmt)
    assert AI_LABEL in content
    assert HUMAN_LABEL in content


@pytest.mark.parametrize("fmt", FORMATS)
def test_an_unreviewed_control_is_marked_pending_not_concluded(seeded_session, report_project, fmt):
    content = render_report(seeded_session, report_project.id, fmt=fmt)
    assert PENDING_LABEL in content


@pytest.mark.parametrize("fmt", FORMATS)
def test_the_risk_model_is_labelled_as_a_prototype_everywhere_it_is_used(
    seeded_session, report_project, fmt
):
    content = render_report(seeded_session, report_project.id, fmt=fmt)
    assert RISK_MODEL_LABEL in content


@pytest.mark.parametrize("fmt", FORMATS)
def test_the_limitations_section_states_what_the_report_cannot_establish(
    seeded_session, report_project, fmt
):
    content = render_report(seeded_session, report_project.id, fmt=fmt)
    lowered = content.lower()
    assert "limitations" in lowered
    for claim in ("authenticity", "completeness"):
        assert claim in lowered, "the limitations must name {0}".format(claim)


@pytest.mark.parametrize("fmt", FORMATS)
def test_the_evidence_appendix_carries_a_resolvable_locator(seeded_session, report_project, fmt):
    """Section 7 is what makes a finding checkable against the source artefact."""
    content = render_report(seeded_session, report_project.id, fmt=fmt)
    assessment = service.list_assessments(seeded_session, project_id=report_project.id)[0]
    citation = assessment.citations[0]
    assert citation.filename in content
    # The rendered locator names the file and (for a table) its rows.
    assert citation.locator_text.split(" - ")[0] in content


@pytest.mark.parametrize("fmt", FORMATS)
def test_the_evidence_section_lists_the_uploaded_files_with_their_hashes(
    seeded_session, report_project, fmt
):
    content = render_report(seeded_session, report_project.id, fmt=fmt)
    for evidence in service.list_evidence(seeded_session, project_id=report_project.id):
        assert evidence.filename in content
        assert evidence.sha256[:12] in content


@pytest.mark.parametrize("fmt", FORMATS)
def test_the_mock_provider_is_disclosed_in_the_report(seeded_session, report_project, fmt):
    """A result produced by a rule-based stand-in must never read as a model result."""
    content = render_report(seeded_session, report_project.id, fmt=fmt)
    assert "mock" in content.lower()


# ------------------------------------------------------------------- AI vs human
def test_the_ai_conclusion_and_the_auditors_are_both_printed(seeded_session, report_project):
    """The auditor overturns the AI; both must survive into the report."""
    assessment = service.list_assessments(seeded_session, project_id=report_project.id)[0]
    ai_status = assessment.status
    service.record_human_review(
        seeded_session,
        assessment.id,
        "A. Auditor",
        HumanDecision.REJECTED,
        final_status=AssessmentStatus.NOT_APPLICABLE.value,
        final_risk_level=RiskLevel.LOW.value,
        comments="This system was out of scope for the period.",
    )
    content = render_report(seeded_session, report_project.id, fmt="markdown")
    assert ai_status in content
    assert AssessmentStatus.NOT_APPLICABLE.value in content
    assert "A. Auditor" in content


def test_divergence_between_the_ai_and_the_auditor_is_counted(seeded_session, report_project):
    assessment = service.list_assessments(seeded_session, project_id=report_project.id)[0]
    service.record_human_review(
        seeded_session,
        assessment.id,
        "A. Auditor",
        HumanDecision.MODIFIED,
        final_status=AssessmentStatus.NOT_EFFECTIVE.value,
    )
    data = build_report_data(seeded_session, report_project.id)
    assert len(data.divergent_controls()) == 1
    assert data.agreement_rate() == 0.0


def test_agreement_over_nothing_is_none_not_zero(seeded_session, report_project):
    """A rate over an empty denominator is no measurement, not a low one."""
    data = build_report_data(seeded_session, report_project.id)
    assert data.concluded_controls() == []
    assert data.agreement_rate() is None


def test_only_auditor_decisions_count_towards_confirmed_findings(seeded_session, report_project):
    data = build_report_data(seeded_session, report_project.id)
    assert len(data.assessed_controls()) == 1
    assert data.confirmed_deficiencies() == [], "an unreviewed AI finding is not a finding"

    assessment = service.list_assessments(seeded_session, project_id=report_project.id)[0]
    service.record_human_review(seeded_session, assessment.id, "A. Auditor", HumanDecision.ACCEPTED)
    confirmed = build_report_data(seeded_session, report_project.id)
    assert len(confirmed.confirmed_deficiencies()) == 1


def test_a_scoped_but_unassessed_control_is_reported_as_not_assessed(
    seeded_session, report_project
):
    service.scope_controls(seeded_session, report_project.id, ["CONTROL-005"])
    data = build_report_data(seeded_session, report_project.id)
    assert {c.control_ref for c in data.unassessed_controls()} == {"CONTROL-005"}


# --------------------------------------------------------------------- persistence
@pytest.mark.parametrize("fmt,extension", [("markdown", ".md"), ("html", ".html")])
def test_generate_report_writes_a_file_under_the_configured_directory(
    seeded_session, report_project, settings, fmt, extension
):
    row = generate_report(seeded_session, report_project.id, generated_by="Tester", fmt=fmt)
    path = Path(row.stored_path)
    assert path.is_file()
    assert path.suffix == extension
    assert Path(settings.report_dir).resolve() in path.resolve().parents
    # The row carries the text too: a report whose only copy is a file path stops being
    # reproducible the moment the file is moved.
    assert path.read_text(encoding="utf-8") == row.content


def test_generate_report_persists_a_row_the_api_can_serve(seeded_session, report_project):
    row = generate_report(seeded_session, report_project.id, generated_by="Tester")
    assert row.id is not None
    assert row.format == "markdown"
    assert row.generated_by == "Tester"
    assert row.content
    assert service.get_report(seeded_session, row.id) is row
    assert service.list_reports(seeded_session, project_id=report_project.id) == [row]


def test_summary_stats_are_a_superset_of_what_the_document_prints(seeded_session, report_project):
    row = generate_report(seeded_session, report_project.id, generated_by="Tester")
    stats = row.summary_stats
    assert stats["controls_assessed"] == 1
    assert stats["controls_pending_review"] == 1
    assert stats["controls_concluded_by_auditor"] == 0
    assert stats["citations_total"] > 0
    assert stats["sections"] == [name for _, name in SECTIONS]
    assert stats["risk_model_label"] == RISK_MODEL_LABEL
    assert stats["counting_basis"]
    assert stats["used_mock_provider"] is True
    assert stats["human_ai_status_agreement_rate"] is None


def test_generation_is_recorded_in_the_activity_trail(seeded_session, report_project):
    generate_report(seeded_session, report_project.id, generated_by="Tester")
    entries = service.list_activity(seeded_session, project_id=report_project.id, entity_type="report")
    assert entries and entries[0].action == "REPORT_GENERATED"


def test_write_file_false_leaves_nothing_on_disk(seeded_session, report_project):
    row = generate_report(seeded_session, report_project.id, write_file=False)
    assert row.stored_path is None
    assert row.content


def test_report_filenames_are_stable_and_sortable(seeded_session, report_project):
    data = build_report_data(seeded_session, report_project.id)
    name = report_filename(data, "markdown")
    assert name.startswith("audit_report_p{0}_".format(report_project.id))
    assert name.endswith(".md")
    assert report_filename(data, "markdown") == name


# ------------------------------------------------------------------------ formats
@pytest.mark.parametrize(
    "given,expected",
    [("markdown", "markdown"), ("MD", "markdown"), ("html", "html"), ("HTM", "html"), (None, "markdown")],
)
def test_format_normalisation(given, expected):
    assert normalise_format(given) == expected


@pytest.mark.parametrize("given", ["pdf", "docx", "csv", "  "])
def test_an_unsupported_format_is_refused_loudly(given):
    with pytest.raises(InvalidInputError):
        normalise_format(given)


def test_html_output_is_a_complete_document(seeded_session, report_project):
    content = render_report(seeded_session, report_project.id, fmt="html")
    assert content.lstrip().lower().startswith("<!doctype html")
    assert "</html>" in content


def test_html_escapes_content_that_would_otherwise_inject_markup(seeded_session, report_project):
    service.update_project(seeded_session, report_project.id, description="<script>alert(1)</script>")
    content = render_report(seeded_session, report_project.id, fmt="html")
    assert "<script>alert(1)</script>" not in content
    assert "&lt;script&gt;" in content


# -------------------------------------------------------------------- edge cases
def test_a_project_with_no_evidence_and_no_assessments_still_reports(seeded_session, project):
    """An empty engagement must produce a readable report, not an exception."""
    content = render_report(seeded_session, project.id, fmt="markdown")
    for _, name in SECTIONS:
        assert name in content
    data = build_report_data(seeded_session, project.id)
    assert data.assessed_controls() == []
    assert data.evidence == []


def test_reporting_on_a_missing_project_raises(seeded_session):
    with pytest.raises(NotFoundError):
        build_report_data(seeded_session, 987654)


def test_evaluation_runs_are_excluded_by_default(seeded_session, ingested_project, engine_factory):
    """Research runs are not fieldwork and must not appear in an auditor's report."""
    from app.database.models import EvaluationRun

    run = EvaluationRun(name="run", experiment_mode=ExperimentMode.B_RAG.value)
    seeded_session.add(run)
    seeded_session.commit()
    engine_factory().assess_control(
        ingested_project.id, "CONTROL-001", mode=ExperimentMode.B_RAG, evaluation_run_id=run.id
    )

    excluded = build_report_data(seeded_session, ingested_project.id)
    assert excluded.assessed_controls() == []
    included = build_report_data(seeded_session, ingested_project.id, include_evaluation=True)
    assert len(included.assessed_controls()) == 1


def test_a_custom_title_is_honoured(seeded_session, report_project):
    row = generate_report(seeded_session, report_project.id, title="Q2 Privileged Access Review")
    assert row.title == "Q2 Privileged Access Review"
    assert "Q2 Privileged Access Review" in row.content
