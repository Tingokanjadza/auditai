"""The ``findings`` SQL view: a finding, defined once, as a query.

Why there is no findings *table*
--------------------------------
An examiner opening this schema will look for a ``findings`` table, not find one, and
should be able to read this paragraph as the answer.

A finding in this system is not an entity anyone creates. It is what you get when you
put an **AI-generated assessment** next to the **auditor's decision on it** - the pair
the whole research question is about. Both halves already exist as rows, deliberately
kept apart: :class:`~app.database.models.Assessment` holds what the model produced and
:class:`~app.database.models.HumanReview` holds what the human concluded, and they are
never merged in place, because the difference between them *is* the measurement
(human/AI agreement, and whether the reviewer caught a hallucination).

Writing those two rows into a third ``findings`` table would create a second source of
truth for facts that already have one. The failure mode is not hypothetical: an auditor
re-reviews an assessment and changes the final status; unless every write path also
updates the findings row, the schema now asserts two different final statuses for the
same control and nothing in the database says which is stale. The application would be
carrying the risk of quietly reporting a finding the auditor had already overturned - in
a tool whose entire claim is that AI output is traceable and human-authoritative.

A view has no such risk. It stores nothing, cannot drift, and is recomputed from the
authoritative rows on every read. What it buys is real: anyone inspecting the schema
sees a ``findings`` relation with the columns they expected, and a researcher exporting
data for analysis can ``SELECT * FROM findings`` from pandas, a notebook, or ``sqlite3``
on the command line without reimplementing the join - and therefore without the risk of
reimplementing it *differently* from the application.

The one cost is that a view cannot be written to, which is correct here: a finding is not
something you record, it is something you derive from an assessment and a review.

What the view contains
----------------------
One row per **operational** assessment - every assessment an auditor's project produced,
including re-runs of the same control and runs under different experiment modes. Rows
produced by the evaluation harness are excluded, matching
:func:`app.audit.service.list_assessments` with ``include_evaluation=False``: those
belong to the research experiments rather than to an audit, and mixing them would inflate
every count taken over this view. The exclusion is the same predicate the service layer
uses - ``evaluation_run_id IS NULL``.

Because re-runs are included, a consumer that wants "the current state of each control"
must reduce to the latest row per ``(project_id, control_ref)`` itself, exactly as
:func:`app.audit.service.dashboard_stats` does in Python. The view deliberately does not
make that choice on the consumer's behalf; a projection that silently dropped rows would
be a different kind of second source of truth.

Each row carries the latest human review, or NULLs where there is none. "Latest" is by
``created_at`` with the primary key as tie-breaker, so the ordering is total and the view
is deterministic even for two reviews written in the same clock tick.

Portability
-----------
The SELECT is built with the SQLAlchemy expression language and rendered by the target
dialect's own compiler, so the same definition is valid on SQLite and PostgreSQL - the
``grounding_rate`` JSON extraction included, which compiles to ``JSON_EXTRACT`` on SQLite
and to a ``->>`` operator with a cast on PostgreSQL.
"""

from __future__ import annotations

import logging
import re
from typing import Any, List, Optional

from sqlalchemy import Engine, Select, case, func, inspect, null, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import aliased

from app.database.base import engine as default_engine
from app.database.models import (
    Assessment,
    AssessmentCitation,
    AuditProject,
    Control,
    HumanReview,
)
from app.schemas.enums import CitationVerdict, HumanDecision

logger = logging.getLogger(__name__)

#: Name of the view in the database. Nothing maps to it; it is read by hand and by
#: analysis code, never by the ORM.
FINDINGS_VIEW_NAME = "findings"

#: Every review decision that represents a concluded human judgement. A PENDING row
#: records that someone opened the review and nothing more, so it must not be read as
#: agreement or disagreement - see ``agreed_with_ai`` below.
_CONCLUDED_DECISIONS: List[str] = [
    decision.value for decision in HumanDecision if decision is not HumanDecision.PENDING
]

__all__ = [
    "FINDINGS_VIEW_NAME",
    "create_findings_view",
    "drop_findings_view",
    "findings_select",
    "findings_view_sql",
]


def findings_select() -> Select:
    """The SELECT behind the view, as a SQLAlchemy construct.

    Exposed separately from the DDL so the same projection can be executed directly
    against a session - useful in a notebook, and what the tests assert against - without
    depending on the view having been created.
    """
    review = aliased(HumanReview, name="review")

    # The id of the newest review for this assessment, correlated to the outer row. A
    # scalar subquery rather than a window function or a GROUP BY: it reads as the
    # sentence "the latest review", it is valid on both backends, and it keeps the outer
    # query a plain left join that returns exactly one row per assessment.
    latest_review_id = (
        select(HumanReview.id)
        .where(HumanReview.assessment_id == Assessment.id)
        .order_by(HumanReview.created_at.desc(), HumanReview.id.desc())
        .limit(1)
        .correlate(Assessment)
        .scalar_subquery()
    )

    citation_count = (
        select(func.count(AssessmentCitation.id))
        .where(AssessmentCitation.assessment_id == Assessment.id)
        .correlate(Assessment)
        .scalar_subquery()
    )
    # Only VERIFIED counts as grounded here, matching the ``citation_grounding_rate``
    # definition in app.audit.service.dashboard_stats: PARTIAL matches are excluded so
    # the figure is a floor rather than a flattering estimate.
    verified_citation_count = (
        select(func.count(AssessmentCitation.id))
        .where(
            AssessmentCitation.assessment_id == Assessment.id,
            AssessmentCitation.verdict == CitationVerdict.VERIFIED.value,
        )
        .correlate(Assessment)
        .scalar_subquery()
    )

    # NULL, not False, when the review is absent or still PENDING. False would read as
    # "the auditor disagreed with the AI", which is a claim about a judgement nobody has
    # made yet; a three-valued column forces a consumer to handle "not concluded".
    agreed_with_ai = case(
        (review.decision.in_(_CONCLUDED_DECISIONS), review.agreed_with_ai_status),
        else_=null(),
    )

    return (
        select(
            Assessment.id.label("assessment_id"),
            Assessment.created_at.label("assessed_at"),
            Assessment.project_id.label("project_id"),
            AuditProject.name.label("project_name"),
            Assessment.control_ref.label("control_ref"),
            Control.name.label("control_name"),
            Assessment.experiment_mode.label("experiment_mode"),
            Assessment.status.label("ai_status"),
            Assessment.risk_level.label("ai_risk_level"),
            Assessment.risk_score.label("ai_risk_score"),
            Assessment.confidence.label("ai_confidence"),
            Assessment.confidence_score.label("ai_confidence_score"),
            Assessment.evidence_sufficiency.label("evidence_sufficiency"),
            Assessment.finding.label("finding"),
            citation_count.label("citation_count"),
            verified_citation_count.label("verified_citation_count"),
            # Persisted by the citation validator; NULL for a mode-A run, which records
            # no validation report at all. "Rate if available" is the honest shape.
            Assessment.validation_report["grounding_rate"].as_float().label("grounding_rate"),
            review.decision.label("human_decision"),
            review.final_status.label("final_status"),
            review.final_risk_level.label("final_risk_level"),
            review.final_finding.label("final_finding"),
            review.reviewer_name.label("reviewer"),
            review.created_at.label("reviewed_at"),
            agreed_with_ai.label("agreed_with_ai"),
        )
        .select_from(Assessment)
        .join(AuditProject, AuditProject.id == Assessment.project_id)
        .join(Control, Control.id == Assessment.control_id)
        .outerjoin(review, review.id == latest_review_id)
        .where(Assessment.evaluation_run_id.is_(None))
    )


def findings_view_sql(dialect: Any) -> str:
    """The SELECT rendered as literal SQL for the given dialect.

    ``literal_binds`` is required: a view definition is stored as text and cannot carry
    bind parameters, so the status literals and the JSON path have to be inlined. Every
    value inlined here comes from this module's own constants and from the enum
    definitions, never from user input.
    """
    return findings_select().compile(
        dialect=dialect, compile_kwargs={"literal_binds": True}
    ).string


def create_findings_view(engine: Optional[Engine] = None) -> bool:
    """Create or refresh the ``findings`` view. Never raises.

    Like :func:`app.database.migrations.reconcile_schema`, this runs during start-up, so
    a failure is logged and reported rather than propagated: an application that starts
    without a convenience view is usable, and one that refuses to start is not.

    :returns: ``True`` when the view is in place afterwards.
    """
    target = engine if engine is not None else default_engine
    try:
        sql = findings_view_sql(target.dialect)
    except SQLAlchemyError as exc:  # pragma: no cover - a dialect that cannot render it
        logger.warning("Could not render the %s view for this dialect: %s", FINDINGS_VIEW_NAME, exc)
        return False

    try:
        if target.dialect.name == "sqlite":
            return _create_sqlite(target, sql)
        return _create_or_replace(target, sql)
    except Exception as exc:
        # Deliberately broader than SQLAlchemyError: this is called from ``init_db`` on
        # every start, and the promise in the docstring has to hold for a driver-level
        # failure too, not only for the errors SQLAlchemy wraps.
        logger.warning("Could not create the %s view: %s", FINDINGS_VIEW_NAME, exc)
        return False


def drop_findings_view(engine: Optional[Engine] = None) -> None:
    """Drop the view if it exists. Never raises.

    Called before ``drop_all`` because on PostgreSQL a ``DROP TABLE`` fails outright
    while a view depends on the table. On SQLite the drop is merely tidy - SQLite allows
    a view to outlive its tables and go stale - but doing it on both backends keeps
    ``drop_all`` meaning the same thing everywhere.
    """
    target = engine if engine is not None else default_engine
    try:
        with target.connect() as connection:
            connection.exec_driver_sql("DROP VIEW IF EXISTS {0}".format(FINDINGS_VIEW_NAME))
            connection.commit()
    except Exception as exc:
        logger.warning("Could not drop the %s view: %s", FINDINGS_VIEW_NAME, exc)


# ---- per-dialect DDL
def _create_sqlite(target: Engine, sql: str) -> bool:
    """``CREATE VIEW IF NOT EXISTS``, after discarding a definition that has gone stale.

    ``IF NOT EXISTS`` on its own would pin a database to whatever definition it was
    first created with: add a column to the view in a later version and every existing
    database would keep serving the old projection forever, silently. SQLite has no
    ``CREATE OR REPLACE VIEW``, so the stale definition is detected by comparing the
    stored DDL against what would be created now, and dropped when it differs.
    """
    if _sqlite_definition_differs(target, sql):
        drop_findings_view(target)
    with target.connect() as connection:
        connection.exec_driver_sql(
            "CREATE VIEW IF NOT EXISTS {0} AS {1}".format(FINDINGS_VIEW_NAME, sql)
        )
        connection.commit()
    return True


def _sqlite_definition_differs(target: Engine, sql: str) -> bool:
    """True when a ``findings`` view exists whose body is not the current SELECT."""
    try:
        inspector = inspect(target)
        if FINDINGS_VIEW_NAME not in inspector.get_view_names():
            return False
        # SQLite stores the CREATE statement verbatim, so the current SELECT appears as
        # a literal substring of it when - and only when - the definition is current.
        definition = inspector.get_view_definition(FINDINGS_VIEW_NAME) or ""
    except SQLAlchemyError as exc:  # pragma: no cover - inspection failure
        logger.warning("Could not read the existing %s view definition: %s", FINDINGS_VIEW_NAME, exc)
        return False
    return _normalise(sql) not in _normalise(definition)


def _create_or_replace(target: Engine, sql: str) -> bool:
    """``CREATE OR REPLACE VIEW`` for PostgreSQL and friends, with a drop-and-create fallback.

    PostgreSQL only *replaces* a view whose column list is unchanged; adding a column to
    the projection makes ``CREATE OR REPLACE`` fail with "cannot change name of view
    column". Falling back to an explicit drop is what lets the definition here evolve.
    """
    statement = "CREATE OR REPLACE VIEW {0} AS {1}".format(FINDINGS_VIEW_NAME, sql)
    try:
        with target.connect() as connection:
            connection.exec_driver_sql(statement)
            connection.commit()
        return True
    except SQLAlchemyError as exc:
        logger.info(
            "CREATE OR REPLACE VIEW %s failed (%s); recreating it from scratch.",
            FINDINGS_VIEW_NAME,
            exc.__class__.__name__,
        )

    drop_findings_view(target)
    with target.connect() as connection:
        connection.exec_driver_sql("CREATE VIEW {0} AS {1}".format(FINDINGS_VIEW_NAME, sql))
        connection.commit()
    return True


def _normalise(sql: str) -> str:
    """Collapse whitespace so two renderings of the same SELECT compare equal."""
    return re.sub(r"\s+", " ", sql).strip()
