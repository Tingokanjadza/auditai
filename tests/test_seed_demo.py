"""The demo project seed and the "Try the demo audit" evidence load.

Two properties are pinned here. First, restarting the application must never undo an
auditor's decision about the demo project: the seed scopes the mandated controls only
when it *creates* the row, and it finds an existing demo by its flag rather than by a
name the auditor may have changed. Second, loading the demo evidence is idempotent - the
button is safe to press twice - and it reports exactly what it did, so the UI can show
the auditor which files arrived, which were already there and which failed.

Everything runs against the suite's temporary database and temporary upload directory
(see ``tests/conftest.py``); nothing here touches ``data/``. DATASET-003 is used because
it is the cheapest dataset to generate (plain text and CSV, no PDF or Office writers).
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from app.audit import service
from app.database import seed
from app.database.models import AuditProject, ProjectControl
from app.evaluation.datasets import get_dataset
from app.schemas.enums import ParseStatus

DATASET = "DATASET-003"


# ------------------------------------------------------------------- the demo project
def test_bootstrap_creates_the_demo_project_once_with_the_mandated_scope(db_session):
    first = seed.bootstrap(db_session)
    second = seed.bootstrap(db_session)
    assert first["demo_project_id"] == second["demo_project_id"]
    assert first["scoped_control_refs"] == seed.MANDATED_CONTROL_REFS
    demos = db_session.execute(select(AuditProject).where(AuditProject.is_demo.is_(True))).scalars().all()
    assert len(demos) == 1
    assert demos[0].auditor_name


def test_the_demo_is_found_by_its_flag_even_after_a_rename(db_session):
    seed.bootstrap(db_session)
    demo = seed.seed_demo_project(db_session)
    service.update_project(db_session, demo.id, name="My renamed demo")

    again = seed.seed_demo_project(db_session)
    assert again.id == demo.id
    assert len(db_session.execute(select(AuditProject)).scalars().all()) == 1


def test_an_unscoped_demo_control_does_not_come_back_on_restart(db_session):
    seed.bootstrap(db_session)
    demo = seed.seed_demo_project(db_session)
    assert service.unscope_control(db_session, demo.id, "CONTROL-003") is True

    seed.bootstrap(db_session)
    refs = {c.control_id for c in service.list_scoped_controls(db_session, demo.id)}
    assert "CONTROL-003" not in refs
    assert refs == {"CONTROL-001", "CONTROL-002", "CONTROL-004", "CONTROL-005"}


def test_the_seed_uses_a_named_actor_when_no_auditor_is_configured(db_session, settings, monkeypatch):
    monkeypatch.setattr(settings, "default_auditor_name", "")
    assert seed.demo_actor() == seed.DEMO_SEED_ACTOR
    demo = seed.seed_demo_project(db_session)
    assert demo.auditor_name == seed.DEMO_SEED_ACTOR


def test_the_seed_scopes_only_controls_that_exist(db_session):
    """A reference missing from the library is skipped, never invented."""
    seed.seed_controls(db_session)
    demo = seed.seed_demo_project(db_session, control_refs=["CONTROL-001", "CONTROL-NOPE"])
    links = db_session.execute(select(ProjectControl).where(ProjectControl.project_id == demo.id)).scalars().all()
    assert len(links) == 1


# ---------------------------------------------------------------- the demo evidence
def test_load_demo_evidence_ingests_the_dataset_and_is_idempotent(db_session, settings):
    dataset = get_dataset(DATASET)
    seen = []

    summary = seed.load_demo_evidence(
        db_session,
        dataset_ids=[DATASET],
        progress=lambda done, total, name: seen.append((done, total, name)),
    )

    assert summary["datasets"] == [DATASET]
    assert summary["failures"] == []
    assert summary["skipped"] == []
    assert sorted(item["filename"] for item in summary["ingested"]) == sorted(dataset.file_names)
    assert all(item["dataset_id"] == DATASET for item in summary["ingested"])
    assert all(item["parse_status"] == ParseStatus.PARSED.value for item in summary["ingested"])
    assert all(item["chunks"] > 0 for item in summary["ingested"])
    assert summary["project_name"] and summary["controls_total"] >= 5
    assert summary["scoped_control_refs"] == seed.MANDATED_CONTROL_REFS
    assert summary["note"] == seed.DEMO_EVIDENCE_NOTE
    for key in ("project_id", "project_name", "controls_total", "scoped_control_refs",
                "datasets", "ingested", "skipped", "failures", "note"):
        assert key in summary

    # The progress hook saw every file, with the total fixed up front.
    total = len(dataset.file_names)
    assert [done for done, _, _ in seen] == list(range(1, total + 1))
    assert {t for _, t, _ in seen} == {total}
    assert sorted(name for _, _, name in seen) == sorted(dataset.file_names)

    # What was reported is what is in the database, with the declared evidence types,
    # marked synthetic and stored under the temporary upload directory.
    files = service.list_evidence(db_session, project_id=summary["project_id"])
    assert sorted(f.filename for f in files) == sorted(dataset.file_names)
    for record in files:
        declared = dataset.file(record.filename)
        assert record.evidence_type == declared.evidence_type
        assert record.is_synthetic is True
        assert record.chunk_count > 0
        assert Path(settings.upload_dir).resolve() in Path(record.stored_path).resolve().parents
    assert service.project_evidence_stats(db_session, summary["project_id"])["evidence_chunks"] > 0

    # Pressing the button again adds nothing.
    again = seed.load_demo_evidence(db_session, dataset_ids=[DATASET])
    assert again["ingested"] == []
    assert sorted(again["skipped"]) == sorted(dataset.file_names)
    assert again["failures"] == []
    assert len(service.list_evidence(db_session, project_id=summary["project_id"])) == total


def test_load_demo_evidence_records_who_loaded_it(db_session):
    summary = seed.load_demo_evidence(db_session, dataset_ids=[DATASET], uploaded_by="  Ada  ")
    files = service.list_evidence(db_session, project_id=summary["project_id"])
    assert files and all(f.uploaded_by == "Ada" for f in files)


def test_load_demo_evidence_runs_no_assessment(db_session):
    """Ingestion only: every assessment still needs an explicit click."""
    summary = seed.load_demo_evidence(db_session, dataset_ids=[DATASET])
    assert service.list_assessments(db_session, project_id=summary["project_id"]) == []
    assert service.pending_reviews(db_session, project_id=summary["project_id"]) == []


def test_load_demo_evidence_survives_a_failing_progress_hook(db_session):
    def explode(done, total, name):
        raise RuntimeError("UI went away")

    summary = seed.load_demo_evidence(db_session, dataset_ids=[DATASET], progress=explode)
    assert summary["failures"] == []
    assert len(summary["ingested"]) == len(get_dataset(DATASET).file_names)


def test_the_default_dataset_list_excludes_the_contradicting_mfa_cases():
    assert seed.DEMO_DATASET_IDS == ["DATASET-001", "DATASET-002", "DATASET-003", "DATASET-004"]
    assert "DATASET-005" not in seed.DEMO_DATASET_IDS
    assert "DATASET-006" not in seed.DEMO_DATASET_IDS
