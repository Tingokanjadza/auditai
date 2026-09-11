"""Tests for additive schema reconciliation and the ``findings`` view.

Why these tests build their own database
----------------------------------------
Every other module in the suite runs against the session-scoped engine in
``conftest.py``. These tests cannot: their subject is *DDL applied to a database that is
out of date*, so they have to drop a column from a live table and watch it come back. Do
that to the shared engine and every test that runs afterwards inherits the damage. Each
test here therefore gets its own SQLite file under ``tmp_path``, created and thrown away
inside the test.

How an "old database" is simulated
----------------------------------
``EvidenceFile.provenance`` is the column that motivated this module: it was added to a
model that people already have populated databases for. The tests reproduce that exactly
- create the current schema, write rows, then ``ALTER TABLE ... DROP COLUMN provenance``
with raw SQL so the file on disk is genuinely one column behind the models - and then run
the reconciliation the application runs at start-up. Nothing is mocked; the assertions are
about what is really in the SQLite file afterwards.

One case cannot be reached that way. Every NOT NULL column in ``models.py`` today either
has a ``server_default`` or is nullable, so no real column exercises the branch that
relaxes NOT NULL to nullable - and that branch is the one with the potential to lose data.
:func:`_schema_with_an_unbackfillable_column` builds it by **copying** the metadata and
appending a column to the copy, passed to :func:`reconcile_schema` through its
``metadata`` parameter. A copy rather than a mutation of ``EvidenceFile.__table__``,
because that object is global: mutating it would leak a phantom column into every test
that ran afterwards, and a failed assertion would leave it mutated.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy import JSON, Column, MetaData, create_engine, inspect

from app.database.base import Base
from app.database.migrations import reconcile_schema
from app.database.models import (
    Assessment,
    AssessmentCitation,
    AuditProject,
    Control,
    EvaluationRun,
    EvidenceFile,
    HumanReview,
)
from app.database.views import (
    FINDINGS_VIEW_NAME,
    create_findings_view,
    drop_findings_view,
    findings_select,
)
from app.schemas.enums import (
    AssessmentStatus,
    CitationVerdict,
    ConfidenceLevel,
    EvidenceProvenance,
    EvidenceSufficiency,
    ExperimentMode,
    HumanDecision,
    RiskLevel,
)

#: The column whose addition to an existing database this module exists to make work.
PROVENANCE = "provenance"

#: Name of the synthetic column used only for the relax-to-nullable case.
UNBACKFILLABLE = "legacy_annotation"


# --------------------------------------------------------------------------- fixtures
@pytest.fixture()
def temp_engine(tmp_path):
    """A private, fully created SQLite database, disposed at the end of the test."""
    engine = create_engine("sqlite:///" + str(tmp_path / "migrations-test.db"), future=True)
    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _schema_with_an_unbackfillable_column() -> MetaData:
    """A copy of the real schema, plus one NOT NULL column with no server default.

    ``Table.to_metadata`` is SQLAlchemy's supported way to clone a table definition into
    another ``MetaData``; iterating ``sorted_tables`` copies dependencies before the
    tables that reference them. The clone is what gets the extra column, so the real
    ``Base.metadata`` is left exactly as it was.

    ``nullable=False`` with only a Python-side ``default`` is the shape that cannot be
    added to a populated table as declared - there is no value for the rows already
    there. See :func:`test_a_not_null_column_without_a_server_default_is_relaxed`.
    """
    copy = MetaData(naming_convention=Base.metadata.naming_convention)
    for table in Base.metadata.sorted_tables:
        table.to_metadata(copy)
    copy.tables[EvidenceFile.__tablename__].append_column(
        Column(UNBACKFILLABLE, JSON, nullable=False, default=dict)
    )
    return copy


def _make_database_predate_provenance(engine: Any) -> None:
    """Remove ``EvidenceFile.provenance`` and its index, as an older database would be.

    The index has to go first: SQLite refuses to drop a column an index still names. That
    is not an artefact of the test - a database created before the column existed has
    neither, so dropping both is what reproduces its schema.
    """
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "DROP INDEX IF EXISTS ix_{0}_{1}".format(EvidenceFile.__tablename__, PROVENANCE)
        )
        connection.exec_driver_sql(
            "ALTER TABLE {0} DROP COLUMN {1}".format(EvidenceFile.__tablename__, PROVENANCE)
        )


def _column_names(engine: Any, table_name: str) -> List[str]:
    return [column["name"] for column in inspect(engine).get_columns(table_name)]


def _seed_finding(
    engine: Any,
    *,
    evaluation_run: bool = False,
    review: bool = True,
    decision: str = HumanDecision.ACCEPTED.value,
) -> Dict[str, Any]:
    """Write one project/control/assessment (+ citations, + review) and return the ids.

    Rows are inserted through the ORM rather than through ``app.audit.service`` so the
    values the view is asserted against are written here in plain sight, with no
    derivation in between.
    """
    from sqlalchemy.orm import Session

    ids: Dict[str, Any] = {}
    with Session(engine) as session:
        project = AuditProject(
            name="Privileged Access Review (view test)",
            audit_area="Identity and Access Management",
            auditor_name="Test Auditor",
        )
        control = Control(
            control_id="CONTROL-VIEW-1",
            name="Multi-Factor Authentication for Privileged Accounts",
            objective="Synthetic control used by the findings-view test.",
        )
        session.add_all([project, control])
        session.flush()

        run_id: Optional[int] = None
        if evaluation_run:
            run = EvaluationRun(name="harness", experiment_mode=ExperimentMode.C_RAG_WORKFLOW.value)
            session.add(run)
            session.flush()
            run_id = run.id

        assessment = Assessment(
            project_id=project.id,
            control_id=control.id,
            control_ref=control.control_id,
            experiment_mode=ExperimentMode.C_RAG_WORKFLOW.value,
            status=AssessmentStatus.POTENTIAL_DEFICIENCY.value,
            finding="Five of fifty privileged accounts are not enrolled in MFA.",
            risk_level=RiskLevel.HIGH.value,
            risk_score=71.5,
            confidence=ConfidenceLevel.MEDIUM.value,
            confidence_score=0.6,
            evidence_sufficiency=EvidenceSufficiency.PARTIAL.value,
            validation_report={"grounding_rate": 0.75, "total": 2, "verified": 1},
            evaluation_run_id=run_id,
        )
        session.add(assessment)
        session.flush()

        # One VERIFIED and one PARTIAL, so the two citation columns cannot both be right
        # by accident: the totals must come out 2 and 1, not 2 and 2.
        session.add_all(
            [
                AssessmentCitation(
                    assessment_id=assessment.id,
                    order_index=0,
                    quoted_text="svc-account-007 | Privileged | Disabled",
                    verdict=CitationVerdict.VERIFIED.value,
                ),
                AssessmentCitation(
                    assessment_id=assessment.id,
                    order_index=1,
                    quoted_text="svc-account-014 | Privileged | Disabled",
                    verdict=CitationVerdict.PARTIAL.value,
                ),
            ]
        )
        if review:
            session.add(
                HumanReview(
                    assessment_id=assessment.id,
                    reviewer_name="Lead Auditor",
                    decision=decision,
                    final_status=AssessmentStatus.POTENTIAL_DEFICIENCY.value,
                    final_risk_level=RiskLevel.HIGH.value,
                    final_finding="Confirmed; five accounts require enrolment.",
                    agreed_with_ai_status=True,
                    agreed_with_ai_risk=True,
                )
            )
        session.commit()
        ids = {
            "project_id": project.id,
            "control_id": control.id,
            "assessment_id": assessment.id,
            "control_ref": control.control_id,
            "project_name": project.name,
            "control_name": control.name,
        }
    return ids


def _findings_rows(engine: Any) -> List[Dict[str, Any]]:
    """Every row of the view as dictionaries, read with raw SQL.

    ``SELECT *`` rather than the SQLAlchemy construct on purpose: the point of the view
    is that someone can query it by name from a notebook or the ``sqlite3`` shell, so the
    test reads it the way they would.
    """
    with engine.connect() as connection:
        result = connection.exec_driver_sql("SELECT * FROM {0}".format(FINDINGS_VIEW_NAME))
        keys = list(result.keys())
        return [dict(zip(keys, row)) for row in result.fetchall()]


# ------------------------------------------------------------------- reconcile_schema
def test_a_fresh_database_needs_no_alters_and_gets_the_view(temp_engine):
    """The fresh-clone path: create_all produced a current schema, so nothing is altered."""
    assert reconcile_schema(temp_engine) == []

    assert create_findings_view(temp_engine) is True
    assert FINDINGS_VIEW_NAME in inspect(temp_engine).get_view_names()
    # An empty database still has a queryable relation - zero rows, not an error.
    assert _findings_rows(temp_engine) == []


def test_a_database_predating_evidence_file_provenance_is_upgraded(temp_engine):
    """The case this module exists for, end to end, against the real model column.

    An evidence file is written to a database that then has ``provenance`` removed, which
    is exactly the state of anyone who was using this application before that column was
    added. Reconciliation must put the column back without touching the row - the row is
    an audit evidence record, and its filename, integrity hash and counts are the audit
    trail.

    Because the model gives ``provenance`` a ``server_default``, the column can be added
    as declared - NOT NULL - and the pre-existing row is backfilled by the database with
    that default rather than left NULL.
    """
    from sqlalchemy.orm import Session

    ids = _seed_finding(temp_engine)
    with Session(temp_engine) as session:
        session.add(
            EvidenceFile(
                project_id=ids["project_id"],
                filename="Privileged_Accounts_Export.csv",
                stored_path="/synthetic/Privileged_Accounts_Export.csv",
                sha256="a" * 64,
                size_bytes=4096,
                row_count=50,
                is_synthetic=True,
            )
        )
        session.commit()

    _make_database_predate_provenance(temp_engine)
    assert PROVENANCE not in _column_names(temp_engine, EvidenceFile.__tablename__)

    changes = reconcile_schema(temp_engine)

    # Exactly that column, and nothing else.
    assert len(changes) == 1, changes
    assert "evidence_files.{0}".format(PROVENANCE) in changes[0]
    assert PROVENANCE in _column_names(temp_engine, EvidenceFile.__tablename__)

    with temp_engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT filename, sha256, size_bytes, row_count, {0} FROM evidence_files".format(
                PROVENANCE
            )
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][:4] == ("Privileged_Accounts_Export.csv", "a" * 64, 4096, 50)
    assert rows[0][4] == EvidenceProvenance.SYNTHETIC.value

    # And the ORM, which is what the application actually uses, can read the row back.
    with Session(temp_engine) as session:
        stored = session.query(EvidenceFile).one()
        assert stored.provenance == EvidenceProvenance.SYNTHETIC.value


def test_a_not_null_column_without_a_server_default_is_relaxed(temp_engine):
    """It is added nullable, and says so, rather than being skipped or failing.

    A NOT NULL column with no server default cannot be added to a populated table as
    declared: there is no value for the rows already there, and inventing one would
    fabricate audit data. Adding it nullable keeps both the data and a running
    application. The message has to make that visible, because a reader comparing the
    models against the live schema needs to know why they differ.
    """
    _seed_finding(temp_engine)
    changes = reconcile_schema(temp_engine, metadata=_schema_with_an_unbackfillable_column())

    assert len(changes) == 1, changes
    assert UNBACKFILLABLE in changes[0]
    assert "NULLABLE" in changes[0]
    live = [
        column
        for column in inspect(temp_engine).get_columns(EvidenceFile.__tablename__)
        if column["name"] == UNBACKFILLABLE
    ]
    assert live and live[0]["nullable"] is True


def test_reconciling_twice_is_a_no_op(temp_engine):
    """Idempotence: the second pass finds the column it added and does nothing."""
    _seed_finding(temp_engine)
    _make_database_predate_provenance(temp_engine)

    assert len(reconcile_schema(temp_engine)) == 1
    assert reconcile_schema(temp_engine) == []
    assert reconcile_schema(temp_engine) == []

    # The same holds for the relaxed branch, which takes a different code path.
    schema = _schema_with_an_unbackfillable_column()
    assert len(reconcile_schema(temp_engine, metadata=schema)) == 1
    assert reconcile_schema(temp_engine, metadata=schema) == []


def test_a_column_already_in_the_models_is_reconciled(temp_engine):
    """A real model column dropped from the live table is restored.

    The companion to the ``provenance`` test, with no copied metadata anywhere: the
    database is made genuinely old by dropping a column with raw SQL, and reconciliation
    is run exactly as ``init_db`` runs it.
    """
    ids = _seed_finding(temp_engine)
    with temp_engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE assessments DROP COLUMN recommendation")
    assert "recommendation" not in _column_names(temp_engine, Assessment.__tablename__)

    changes = reconcile_schema(temp_engine)

    assert len(changes) == 1, changes
    assert "assessments.recommendation" in changes[0]
    with temp_engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT id, control_ref, recommendation FROM assessments"
        ).fetchall()
    assert rows == [(ids["assessment_id"], ids["control_ref"], None)]


def test_reconciliation_never_drops_a_column_the_models_do_not_know_about(temp_engine):
    """Additive only: an unknown column in the database is left alone."""
    with temp_engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE assessments ADD COLUMN legacy_note VARCHAR(80)")

    assert reconcile_schema(temp_engine) == []
    assert "legacy_note" in _column_names(temp_engine, Assessment.__tablename__)


def test_a_reconciled_column_does_not_get_its_index_back(temp_engine):
    """A documented limitation, asserted so it stays documented rather than assumed.

    ``EvidenceFile.provenance`` is declared ``index=True``. Reconciliation restores the
    column and not the index, which costs a scan and never costs correctness - but a
    reader who assumed otherwise would be wrong, so the boundary is pinned here.
    """
    index_name = "ix_{0}_{1}".format(EvidenceFile.__tablename__, PROVENANCE)
    assert index_name in {
        index["name"] for index in inspect(temp_engine).get_indexes(EvidenceFile.__tablename__)
    }

    _make_database_predate_provenance(temp_engine)
    reconcile_schema(temp_engine)

    assert PROVENANCE in _column_names(temp_engine, EvidenceFile.__tablename__)
    assert index_name not in {
        index["name"] for index in inspect(temp_engine).get_indexes(EvidenceFile.__tablename__)
    }


def test_a_missing_table_is_left_to_create_all(temp_engine):
    """A table that does not exist is skipped rather than half-built by ALTER statements."""
    with temp_engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE audit_reports")

    assert reconcile_schema(temp_engine) == []
    assert "audit_reports" not in inspect(temp_engine).get_table_names()


# ------------------------------------------------------------------- the findings view
def test_the_findings_view_projects_a_seeded_assessment_and_its_review(temp_engine):
    """``SELECT * FROM findings`` returns the AI half and the human half of one finding."""
    ids = _seed_finding(temp_engine)
    assert create_findings_view(temp_engine) is True

    rows = _findings_rows(temp_engine)
    assert len(rows) == 1
    row = rows[0]

    assert row["assessment_id"] == ids["assessment_id"]
    assert row["project_id"] == ids["project_id"]
    assert row["project_name"] == ids["project_name"]
    assert row["control_ref"] == ids["control_ref"]
    assert row["control_name"] == ids["control_name"]
    assert row["experiment_mode"] == ExperimentMode.C_RAG_WORKFLOW.value

    # ---- the AI half
    assert row["ai_status"] == AssessmentStatus.POTENTIAL_DEFICIENCY.value
    assert row["ai_risk_level"] == RiskLevel.HIGH.value
    assert row["ai_risk_score"] == pytest.approx(71.5)
    assert row["ai_confidence"] == ConfidenceLevel.MEDIUM.value
    assert row["evidence_sufficiency"] == EvidenceSufficiency.PARTIAL.value
    assert "not enrolled in MFA" in row["finding"]
    assert row["citation_count"] == 2
    assert row["verified_citation_count"] == 1
    assert row["grounding_rate"] == pytest.approx(0.75)

    # ---- the human half
    assert row["human_decision"] == HumanDecision.ACCEPTED.value
    assert row["final_status"] == AssessmentStatus.POTENTIAL_DEFICIENCY.value
    assert row["final_risk_level"] == RiskLevel.HIGH.value
    assert row["reviewer"] == "Lead Auditor"
    assert row["reviewed_at"] is not None
    assert bool(row["agreed_with_ai"]) is True


def test_the_findings_view_excludes_evaluation_harness_rows(temp_engine):
    """Harness output belongs to the experiments, not to an audit - same rule as the service."""
    _seed_finding(temp_engine, evaluation_run=True)
    create_findings_view(temp_engine)

    assert _findings_rows(temp_engine) == []


def test_an_unreviewed_assessment_appears_with_null_human_columns(temp_engine):
    """A finding exists before anyone has reviewed it; the human half is simply absent."""
    _seed_finding(temp_engine, review=False)
    create_findings_view(temp_engine)

    rows = _findings_rows(temp_engine)
    assert len(rows) == 1
    assert rows[0]["human_decision"] is None
    assert rows[0]["reviewer"] is None
    assert rows[0]["reviewed_at"] is None
    # NULL, not False: nobody has disagreed with anything yet.
    assert rows[0]["agreed_with_ai"] is None


def test_a_pending_review_is_not_counted_as_agreement(temp_engine):
    """PENDING records that a review was opened, not that the auditor concurred."""
    _seed_finding(temp_engine, decision=HumanDecision.PENDING.value)
    create_findings_view(temp_engine)

    row = _findings_rows(temp_engine)[0]
    assert row["human_decision"] == HumanDecision.PENDING.value
    assert row["agreed_with_ai"] is None


def test_the_view_reports_the_latest_review_when_an_assessment_is_re_reviewed(temp_engine):
    """Re-reviewing must change the finding, which is the reason it is a view and not a table."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy.orm import Session

    ids = _seed_finding(temp_engine)
    with Session(temp_engine) as session:
        session.add(
            HumanReview(
                assessment_id=ids["assessment_id"],
                reviewer_name="Audit Manager",
                decision=HumanDecision.MODIFIED.value,
                final_status=AssessmentStatus.NOT_EFFECTIVE.value,
                final_risk_level=RiskLevel.CRITICAL.value,
                agreed_with_ai_status=False,
                created_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
        )
        session.commit()

    create_findings_view(temp_engine)
    rows = _findings_rows(temp_engine)

    assert len(rows) == 1, "one row per assessment, not one per review"
    assert rows[0]["reviewer"] == "Audit Manager"
    assert rows[0]["final_status"] == AssessmentStatus.NOT_EFFECTIVE.value
    assert rows[0]["final_risk_level"] == RiskLevel.CRITICAL.value
    assert bool(rows[0]["agreed_with_ai"]) is False


def test_creating_the_view_twice_is_safe_and_refreshes_a_stale_definition(temp_engine):
    """Idempotent, and not pinned to whatever definition the database was born with."""
    assert create_findings_view(temp_engine) is True
    assert create_findings_view(temp_engine) is True

    # A database carrying an old, narrower view must end up with the current one, or an
    # existing install would keep serving a projection the code no longer defines.
    drop_findings_view(temp_engine)
    with temp_engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE VIEW {0} AS SELECT id AS assessment_id FROM assessments".format(
                FINDINGS_VIEW_NAME
            )
        )
    _seed_finding(temp_engine)

    assert create_findings_view(temp_engine) is True
    assert "control_ref" in _findings_rows(temp_engine)[0]


def test_the_view_select_is_executable_through_the_orm(temp_engine):
    """The construct behind the view is usable directly, with the same result.

    Proves the two are one definition rather than two that happen to agree today: a
    researcher working in a session gets what ``SELECT * FROM findings`` gives them.
    """
    from sqlalchemy.orm import Session

    ids = _seed_finding(temp_engine)
    create_findings_view(temp_engine)

    with Session(temp_engine) as session:
        direct = session.execute(findings_select()).mappings().all()

    assert [dict(row) for row in direct][0]["assessment_id"] == ids["assessment_id"]
    assert len(direct) == len(_findings_rows(temp_engine)) == 1


def test_drop_findings_view_is_safe_when_there_is_no_view(temp_engine):
    """``drop_all`` calls it unconditionally, so it must tolerate a database without one."""
    drop_findings_view(temp_engine)
    drop_findings_view(temp_engine)

    assert FINDINGS_VIEW_NAME not in inspect(temp_engine).get_view_names()


# --------------------------------------------------------------------------- init_db
def test_init_db_leaves_the_application_database_reconciled_and_viewable():
    """The wiring itself: the engine the suite runs on has the view after ``init_db``.

    ``conftest`` already called ``init_db`` once for the session. Calling it again proves
    it is repeatable on a database that is already current, which is what both entry
    points do on every start.
    """
    from app.database.base import engine as app_engine, init_db

    init_db()
    init_db()

    assert FINDINGS_VIEW_NAME in inspect(app_engine).get_view_names()
    assert reconcile_schema(app_engine) == []
    with app_engine.connect() as connection:
        connection.exec_driver_sql("SELECT * FROM {0}".format(FINDINGS_VIEW_NAME)).fetchall()
