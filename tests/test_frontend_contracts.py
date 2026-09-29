"""Cross-module contracts the frontend facade, widgets, seed and launcher agree on.

These tests pin the seams between modules that were written separately: the demo
loader's summary shape, the single list of demo datasets, the provider display names
the badge and the mismatch alert share, the plain-language label table covering every
enum an auditor can read, and the workflow checklist ``project_stage`` derives. None of
them start Streamlit; the state helpers are exercised in their "outside a script run"
mode, where every setter is a no-op that still returns the parsed value.
"""

from __future__ import annotations

import inspect

import pytest

from app.database import seed
from app.frontend import components, data_access, state
from app.schemas.enums import (
    AssessmentStatus,
    CitationVerdict,
    ConfidenceLevel,
    EvidenceSufficiency,
    EvidenceType,
    ExperimentMode,
    HumanDecision,
    ParseStatus,
    ProjectStatus,
    RiskLevel,
)

# ---- shared constants


def test_demo_dataset_ids_have_one_source():
    assert data_access.DEMO_DATASET_IDS is seed.DEMO_DATASET_IDS
    assert data_access.DEMO_DATASET_IDS == ["DATASET-001", "DATASET-002", "DATASET-003", "DATASET-004"]


def test_provider_display_names_agree_between_facade_and_widgets():
    assert components.PROVIDER_DISPLAY_NAMES == data_access.PROVIDER_DISPLAY_NAMES
    # The mock must never read like a model.
    assert "model" not in components.PROVIDER_DISPLAY_NAMES["mock"].lower()
    assert components.provider_display_name("mock") == data_access.provider_display_name("mock")
    assert data_access.provider_display_name("anthropic") == "Claude"
    assert data_access.provider_display_name("") == "No provider"


def test_load_demo_evidence_signature_matches_its_callers():
    """run.py cmd_seed_demo and data_access.load_demo_project both call this shape."""
    params = inspect.signature(seed.load_demo_evidence).parameters
    assert list(params) == ["session", "dataset_ids", "progress", "uploaded_by"]
    ui_params = inspect.signature(data_access.load_demo_project).parameters
    assert list(ui_params) == ["dataset_ids", "progress"]


# ---- labels


@pytest.mark.parametrize(
    ("kind", "enum"),
    [
        ("status", AssessmentStatus),
        ("risk", RiskLevel),
        ("decision", HumanDecision),
        ("sufficiency", EvidenceSufficiency),
        ("verdict", CitationVerdict),
        ("mode", ExperimentMode),
        ("project_status", ProjectStatus),
        ("evidence_type", EvidenceType),
        ("parse", ParseStatus),
        ("confidence", ConfidenceLevel),
    ],
)
def test_every_enum_member_has_a_plain_language_label(kind, enum):
    for member in enum:
        text = components.label(kind, member)
        assert text, (kind, member)
        assert text != member.value, (kind, member)
        assert "_" not in text, (kind, member, text)
        assert components.label(kind, member.value.lower()) == text


def test_insufficient_evidence_is_not_worded_as_a_deficiency():
    text = components.label("status", AssessmentStatus.INSUFFICIENT_EVIDENCE).lower()
    assert "deficien" not in text


def test_mode_badge_never_assumes_a_mode():
    assert "Mode unknown" in components.mode_badge(None)
    assert "Mode unknown" in components.mode_badge("")


# ---- state helpers outside a script run


def test_state_is_inert_outside_streamlit():
    assert state.available() is False
    state.init_state(auditor_name="Someone")
    assert state.current_project_id() is None
    assert state.set_current_project({"id": "7", "name": "x"}) == 7
    assert state.set_current_project(None) is None
    state.request_project_switch(3)
    assert state.take_pending_project_switch() is None
    assert state.take_last_ingest() == []
    assert state.current_project_name() == "No audit project selected"
    assert state.pop_flashes() == []
    # open_assessment must not raise when there is no page to switch to.
    state.open_assessment(1, project_id=2)


def test_project_scoped_prefixes_cover_the_review_and_assessment_forms():
    prefixes = state.PROJECT_SCOPED_KEY_PREFIXES
    assert "assess_" in prefixes and "review_" in prefixes
    assert state.K_PROJECT_WIDGET == "sidebar_project_select"
    assert state.ASSESSMENTS_PAGE == "views/assessments.py"


# ---- the demo path against the test database


def test_demo_path_bootstraps_loads_and_reports_stage(clean_database):  # noqa: ARG001
    data_access.invalidate_cache()
    first = data_access.first_run_state()
    assert first["demo_project_id"] is None
    assert first["has_evidence"] is False

    assert data_access.bootstrap_database()["controls_total"] > 0
    demo_id = data_access.demo_project_id()
    assert demo_id is not None
    stage = data_access.project_stage(demo_id)
    assert stage["stage"] == "no_evidence"
    assert [step["key"] for step in stage["steps"]] == ["scope", "evidence", "assess", "review", "report"]
    assert stage["steps"][0]["done"] is True and stage["steps"][1]["current"] is True

    seen = []
    summary = data_access.load_demo_project(
        dataset_ids=["DATASET-003"], progress=lambda done, total, name: seen.append((done, total, name))
    )
    assert summary["project_id"] == demo_id
    assert summary["failures"] == []
    assert summary["skipped"] == []
    assert len(summary["ingested"]) == len(seen) == seen[-1][1] > 0
    assert seen == [(index + 1, len(seen), row["filename"]) for index, row in enumerate(summary["ingested"])]
    assert "synthetic" in summary["note"].lower()
    for row in summary["ingested"]:
        assert set(row) == {"filename", "dataset_id", "evidence_type", "parse_status", "chunks"}
        assert row["dataset_id"] == "DATASET-003"

    # Loading evidence never assesses anything; the auditor still has to click.
    stage = data_access.project_stage(demo_id)
    assert stage["stage"] == "not_assessed"
    assert stage["controls_assessed"] == 0 and stage["pending_reviews"] == 0
    assert stage["steps"][2]["current"] is True
    assert stage["steps"][2]["label"] == "Run the AI assessment"
    assert stage["steps"][2]["count_text"] == "0 of {0} assessed".format(stage["controls_in_scope"])

    # Pressing the button twice adds nothing.
    again = data_access.load_demo_project(dataset_ids=["DATASET-003"])
    assert again["ingested"] == []
    assert sorted(again["skipped"]) == sorted(row["filename"] for row in summary["ingested"])

    overview = data_access.portfolio_overview()
    assert overview == {"projects": 1, "active_audits": 1, "awaiting_review": 0, "demo_project_id": demo_id}
    assert data_access.first_run_state()["has_evidence"] is True
    assert data_access.list_scoped_controls(demo_id, active_only=True)


def test_provider_badge_names_the_mock_as_demo_mode(clean_database):  # noqa: ARG001
    data_access.invalidate_cache()
    badge = data_access.provider_badge()
    assert badge["is_mock"] is True
    assert badge["demo_mode"] is True
    assert badge["fell_back_to_mock"] is False
    assert badge["display_name"] == "Demo mode (offline rules)"
    assert badge["label"] == "MOCK PROVIDER"
