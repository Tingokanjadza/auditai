"""The single data-access layer shared by the API and the Streamlit UI.

There are two front ends over the same database, and the fastest way to end up with two
subtly different applications would be to let each of them write its own queries. So the
rule is: **routers and pages contain no ORM code**. Everything they need - projects,
controls, scoping, evidence, assessments, human review, dashboard figures - is a
function here, taking a :class:`~sqlalchemy.orm.Session` plus plain Python values and
returning ORM objects or dictionaries. Nothing in this module imports FastAPI, Pydantic
request models or Streamlit, so it can also be driven from a script or a test.

Two conventions that matter when reading the rest of the file:

* **Mutating functions commit.** A caller may hold a request-scoped session from
  ``get_db()`` or a ``session_scope()`` block; committing here means both behave the
  same and neither front end has to remember. Reads never commit.
* **The AI record is immutable.** ``Assessment`` rows are never edited by a human
  action. A reviewer's conclusion is a separate :class:`HumanReview` row, which is what
  makes human/AI agreement measurable rather than overwritten - the point of the study.

``__all__`` at the foot of the file is exhaustive and is maintained by hand. It exists
because this module imports SQLAlchemy names - ``select``, ``func``, ``joinedload``,
``selectinload``, ``Session`` - that would otherwise be re-exported by
``from app.audit.service import *`` and read as part of the service surface. A router or
a page that reached for ``service.select`` would be writing ORM code in exactly the
place this module exists to keep it out of, so the public surface is stated rather than
inferred. Add a new public function here *and* to that list.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.config import get_settings
from app.database.models import (
    ActivityLog,
    Assessment,
    AssessmentCitation,
    AuditProject,
    AuditReport,
    Control,
    EvidenceChunk,
    EvidenceFile,
    HumanReview,
    ProjectControl,
)
from app.schemas.enums import (
    ActivityAction,
    AssessmentStatus,
    DEFICIENCY_STATUSES,
    EvidenceType,
    ExperimentMode,
    HIGH_RISK_LEVELS,
    HumanDecision,
    ParseStatus,
    ProjectStatus,
    RiskLevel,
)

#: A value that may be supplied as one item or as several.
Filterable = Union[str, Sequence[str], None]


class ServiceError(RuntimeError):
    """Base class so a router can map every service failure to an HTTP status."""


class NotFoundError(ServiceError):
    """A referenced project, control, assessment or file does not exist."""


class InvalidInputError(ServiceError):
    """Caller supplied a value this layer will not accept (bad enum, empty name, …)."""


# ---- projects
#: Fields a caller is allowed to change on an existing project. Anything else - ids,
#: timestamps, the demo flag - is owned by the application, not by the UI.
_PROJECT_UPDATABLE = frozenset(
    {
        "name",
        "audit_area",
        "description",
        "period_start",
        "period_end",
        "auditor_name",
        "status",
        "scope_note",
    }
)


def create_project(
    session: Session,
    name: str,
    audit_area: str,
    description: str = "",
    period_start: Optional[Any] = None,
    period_end: Optional[Any] = None,
    auditor_name: str = "",
    status: Optional[str] = None,
    scope_note: str = "",
    is_demo: bool = False,
    control_refs: Optional[Sequence[Any]] = None,
    actor: str = "",
) -> AuditProject:
    """Create an audit project, optionally scoping library controls in one step."""
    clean_name = (name or "").strip()
    if not clean_name:
        raise InvalidInputError("Project name is required.")
    clean_area = (audit_area or "").strip()
    if not clean_area:
        raise InvalidInputError("Audit area is required.")

    settings = get_settings()
    project = AuditProject(
        name=clean_name,
        audit_area=clean_area,
        description=description or "",
        period_start=_coerce_datetime(period_start),
        period_end=_coerce_datetime(period_end),
        auditor_name=(auditor_name or settings.default_auditor_name).strip(),
        status=_enum_value(ProjectStatus, status, ProjectStatus.PLANNING, "status"),
        scope_note=scope_note or "",
        is_demo=bool(is_demo),
    )
    session.add(project)
    session.commit()

    if control_refs:
        scope_controls(session, project.id, control_refs, actor=actor)

    log_activity(
        session,
        entity_type="project",
        entity_id=project.id,
        action=ActivityAction.PROJECT_CREATED,
        actor=actor or project.auditor_name,
        details={"name": project.name, "audit_area": project.audit_area},
        project_id=project.id,
    )
    return project


#: Marker written into ``AuditProject.scope_note`` by ``app.evaluation.runner`` for the
#: throwaway projects it creates. Duplicated here rather than imported so that the service
#: layer does not depend on the evaluation package; ``tests/test_service.py`` asserts the
#: two constants agree.
EVALUATION_SCOPE_TAG = "EVALUATION-HARNESS"


def list_projects(
    session: Session,
    status: Filterable = None,
    include_demo: bool = True,
    search: str = "",
    include_evaluation: bool = False,
) -> List[AuditProject]:
    """Projects newest first, optionally filtered by status or a name/area substring.

    Projects created by the evaluation harness are excluded by default. A research run
    produces one throwaway project per (dataset, mode) - eighteen for a single A/B/C sweep
    over six datasets - and they would otherwise swamp the auditor's project picker within
    minutes of running an experiment. This mirrors the treatment of harness-generated rows
    everywhere else in this module: they exist, they are queryable, and they are kept out
    of operational views unless explicitly asked for.

    Exclusion keys off :data:`EVALUATION_SCOPE_TAG` in ``scope_note``, never off the name,
    so a genuine engagement an auditor happened to name "[EVALUATION] Q3" still appears.
    """
    stmt = select(AuditProject)
    statuses = _value_list(ProjectStatus, status, "status")
    if statuses:
        stmt = stmt.where(AuditProject.status.in_(statuses))
    if not include_demo:
        stmt = stmt.where(AuditProject.is_demo.is_(False))
    if not include_evaluation:
        stmt = stmt.where(
            (AuditProject.scope_note.is_(None))
            | (~AuditProject.scope_note.startswith(EVALUATION_SCOPE_TAG))
        )
    if search:
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(AuditProject.name.ilike(pattern) | AuditProject.audit_area.ilike(pattern))
    stmt = stmt.order_by(AuditProject.created_at.desc(), AuditProject.id.desc())
    return list(session.execute(stmt).scalars().all())


def get_project(session: Session, project_id: int) -> Optional[AuditProject]:
    return session.get(AuditProject, int(project_id))


def require_project(session: Session, project_id: int) -> AuditProject:
    project = get_project(session, project_id)
    if project is None:
        raise NotFoundError(f"Audit project {project_id} not found.")
    return project


def update_project(session: Session, project_id: int, actor: str = "", **fields: Any) -> AuditProject:
    """Apply a whitelist of field updates. Unknown fields are rejected, not ignored."""
    project = require_project(session, project_id)
    unknown = sorted(set(fields) - _PROJECT_UPDATABLE)
    if unknown:
        raise InvalidInputError(f"Cannot update project field(s): {', '.join(unknown)}")

    changed: Dict[str, Any] = {}
    for key, value in fields.items():
        if value is None:
            continue
        if key in {"period_start", "period_end"}:
            value = _coerce_datetime(value)
        elif key == "status":
            value = _enum_value(ProjectStatus, value, ProjectStatus.PLANNING, "status")
        elif key in {"name", "audit_area"}:
            value = str(value).strip()
            if not value:
                raise InvalidInputError(f"{key} cannot be empty.")
        setattr(project, key, value)
        changed[key] = value.isoformat() if isinstance(value, datetime) else value

    if changed:
        session.commit()
        log_activity(
            session,
            entity_type="project",
            entity_id=project.id,
            action=ActivityAction.PROJECT_UPDATED,
            actor=actor or project.auditor_name,
            details={"changed": changed},
            project_id=project.id,
        )
    return project


def delete_project(session: Session, project_id: int) -> bool:
    """Delete a project and, by ORM cascade, its evidence, assessments and reports."""
    project = get_project(session, project_id)
    if project is None:
        return False
    session.delete(project)
    session.commit()
    return True


def project_to_dict(session: Session, project: AuditProject) -> Dict[str, Any]:
    """Serialisable project view with the counts both front ends display."""
    data = project.to_dict()
    data["period_label"] = project.period_label
    data["controls_in_scope"] = len(project.scoped_controls)
    data["evidence_files"] = len(project.evidence_files)
    data["assessments"] = len(project.assessments)
    data["control_refs"] = [
        link.control.control_id for link in project.scoped_controls if link.control is not None
    ]
    return data


# ---- control library
_CONTROL_UPDATABLE = frozenset(
    {
        "name",
        "objective",
        "description",
        "risk_addressed",
        "expected_evidence",
        "assessment_criteria",
        "framework_refs",
        "category",
        "control_type",
        "control_frequency",
        "inherent_risk",
        "privilege_level",
        "data_sensitivity",
        "retrieval_keywords",
        "is_active",
    }
)


def list_controls(
    session: Session,
    category: Filterable = None,
    active_only: bool = True,
    search: str = "",
    project_id: Optional[int] = None,
) -> List[Control]:
    """The control library, ordered by reference.

    ``project_id`` narrows the list to the controls scoped to that project, which is how
    the assessment pages offer "which control do you want to test?".
    """
    stmt = select(Control)
    if active_only:
        stmt = stmt.where(Control.is_active.is_(True))
    categories = _value_list(None, category, "category")
    if categories:
        stmt = stmt.where(Control.category.in_(categories))
    if search:
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(
            Control.control_id.ilike(pattern)
            | Control.name.ilike(pattern)
            | Control.objective.ilike(pattern)
        )
    if project_id is not None:
        stmt = stmt.join(ProjectControl, ProjectControl.control_id == Control.id).where(
            ProjectControl.project_id == int(project_id)
        )
    stmt = stmt.order_by(Control.control_id.asc())
    return list(session.execute(stmt).scalars().all())


def get_control(session: Session, control_id_or_ref: Any) -> Optional[Control]:
    """Look a control up by primary key (int, or a digit string) or by ``CONTROL-001``."""
    if isinstance(control_id_or_ref, Control):
        return control_id_or_ref
    if isinstance(control_id_or_ref, int):
        return session.get(Control, control_id_or_ref)
    ref = str(control_id_or_ref or "").strip()
    if not ref:
        return None
    if ref.isdigit():
        return session.get(Control, int(ref))
    stmt = select(Control).where(func.upper(Control.control_id) == ref.upper())
    return session.execute(stmt).scalars().first()


def require_control(session: Session, control_id_or_ref: Any) -> Control:
    control = get_control(session, control_id_or_ref)
    if control is None:
        raise NotFoundError(f"Control {control_id_or_ref!r} not found.")
    return control


def create_control(session: Session, data: Dict[str, Any], actor: str = "") -> Control:
    """Add a control to the library. ``control_id`` must be unique and is required."""
    ref = str(data.get("control_id", "")).strip()
    if not ref:
        raise InvalidInputError("control_id is required (e.g. 'CONTROL-015').")
    if get_control(session, ref) is not None:
        raise InvalidInputError(f"Control {ref} already exists.")
    name = str(data.get("name", "")).strip()
    if not name:
        raise InvalidInputError("Control name is required.")

    control = Control(
        control_id=ref,
        name=name,
        objective=str(data.get("objective", "") or ""),
        description=str(data.get("description", "") or ""),
        risk_addressed=str(data.get("risk_addressed", "") or ""),
        expected_evidence=_str_list(data.get("expected_evidence")),
        assessment_criteria=_str_list(data.get("assessment_criteria")),
        framework_refs=_str_list(data.get("framework_refs")),
        category=str(data.get("category", "General") or "General"),
        control_type=str(data.get("control_type", "Preventive") or "Preventive"),
        control_frequency=str(data.get("control_frequency", "Continuous") or "Continuous"),
        inherent_risk=_enum_value(RiskLevel, data.get("inherent_risk"), RiskLevel.MEDIUM, "inherent_risk"),
        privilege_level=_clamp_score(data.get("privilege_level")),
        data_sensitivity=_clamp_score(data.get("data_sensitivity")),
        retrieval_keywords=_str_list(data.get("retrieval_keywords")),
        is_active=bool(data.get("is_active", True)),
        source=str(data.get("source", "user-defined") or "user-defined"),
    )
    session.add(control)
    session.commit()
    log_activity(
        session,
        entity_type="control",
        entity_id=control.id,
        action="CONTROL_CREATED",
        actor=actor,
        details={"control_id": control.control_id, "name": control.name},
    )
    return control


def update_control(session: Session, control_id_or_ref: Any, actor: str = "", **fields: Any) -> Control:
    """Update library fields. ``control_id`` itself is immutable: it is the citation key
    printed in every assessment and report, so renaming it would orphan history."""
    control = require_control(session, control_id_or_ref)
    unknown = sorted(set(fields) - _CONTROL_UPDATABLE)
    if unknown:
        raise InvalidInputError(f"Cannot update control field(s): {', '.join(unknown)}")

    changed: List[str] = []
    for key, value in fields.items():
        if value is None:
            continue
        if key in {"expected_evidence", "assessment_criteria", "framework_refs", "retrieval_keywords"}:
            value = _str_list(value)
        elif key == "inherent_risk":
            value = _enum_value(RiskLevel, value, RiskLevel.MEDIUM, "inherent_risk")
        elif key in {"privilege_level", "data_sensitivity"}:
            value = _clamp_score(value)
        elif key == "is_active":
            value = bool(value)
        setattr(control, key, value)
        changed.append(key)

    if changed:
        session.commit()
        log_activity(
            session,
            entity_type="control",
            entity_id=control.id,
            action="CONTROL_UPDATED",
            actor=actor,
            details={"control_id": control.control_id, "changed": changed},
        )
    return control


def set_control_active(session: Session, control_id_or_ref: Any, active: bool, actor: str = "") -> Control:
    """Activate or retire a control.

    Retiring is a soft delete on purpose: assessments and reports reference the control
    row, so removing it would break the evidence trail. An inactive control disappears
    from pickers but every past finding still resolves.
    """
    control = require_control(session, control_id_or_ref)
    control.is_active = bool(active)
    session.commit()
    log_activity(
        session,
        entity_type="control",
        entity_id=control.id,
        action="CONTROL_ACTIVATED" if active else "CONTROL_DEACTIVATED",
        actor=actor,
        details={"control_id": control.control_id, "is_active": control.is_active},
    )
    return control


def deactivate_control(session: Session, control_id_or_ref: Any, actor: str = "") -> Control:
    return set_control_active(session, control_id_or_ref, False, actor=actor)


def control_categories(session: Session, active_only: bool = True) -> List[str]:
    stmt = select(Control.category).distinct()
    if active_only:
        stmt = stmt.where(Control.is_active.is_(True))
    return sorted(value for value in session.execute(stmt).scalars().all() if value)


def control_to_dict(control: Control) -> Dict[str, Any]:
    data = control.to_dict()
    data["scoped_project_count"] = len(control.scoped_projects)
    return data


# ---- scoping controls to a project
def scope_control(
    session: Session,
    project_id: int,
    control_id_or_ref: Any,
    scope_note: str = "",
    actor: str = "",
) -> ProjectControl:
    """Put one library control in scope for a project (idempotent)."""
    project = require_project(session, project_id)
    control = require_control(session, control_id_or_ref)

    existing = session.execute(
        select(ProjectControl).where(
            ProjectControl.project_id == project.id,
            ProjectControl.control_id == control.id,
        )
    ).scalars().first()
    if existing is not None:
        if scope_note:
            existing.scope_note = scope_note
            session.commit()
        return existing

    link = ProjectControl(project_id=project.id, control_id=control.id, scope_note=scope_note or None)
    session.add(link)
    session.commit()
    log_activity(
        session,
        entity_type="project_control",
        entity_id=link.id,
        action=ActivityAction.CONTROL_LINKED,
        actor=actor or project.auditor_name,
        details={"control_id": control.control_id, "control_name": control.name},
        project_id=project.id,
    )
    return link


def scope_controls(
    session: Session,
    project_id: int,
    control_refs: Sequence[Any],
    scope_note: str = "",
    actor: str = "",
) -> List[ProjectControl]:
    return [
        scope_control(session, project_id, ref, scope_note=scope_note, actor=actor)
        for ref in control_refs
    ]


def unscope_control(session: Session, project_id: int, control_id_or_ref: Any, actor: str = "") -> bool:
    """Remove a control from a project's scope. Past assessments are left untouched."""
    control = require_control(session, control_id_or_ref)
    link = session.execute(
        select(ProjectControl).where(
            ProjectControl.project_id == int(project_id),
            ProjectControl.control_id == control.id,
        )
    ).scalars().first()
    if link is None:
        return False
    session.delete(link)
    session.commit()
    log_activity(
        session,
        entity_type="project_control",
        entity_id=None,
        action=ActivityAction.CONTROL_UNLINKED,
        actor=actor,
        details={"control_id": control.control_id},
        project_id=int(project_id),
    )
    return True


def list_scoped_controls(session: Session, project_id: int) -> List[Control]:
    stmt = (
        select(Control)
        .join(ProjectControl, ProjectControl.control_id == Control.id)
        .where(ProjectControl.project_id == int(project_id))
        .order_by(Control.control_id.asc())
    )
    return list(session.execute(stmt).scalars().all())


def list_unscoped_controls(session: Session, project_id: int, active_only: bool = True) -> List[Control]:
    """Library controls not yet in scope - the candidate list for the "add" picker."""
    scoped_ids = [control.id for control in list_scoped_controls(session, project_id)]
    stmt = select(Control)
    if active_only:
        stmt = stmt.where(Control.is_active.is_(True))
    if scoped_ids:
        stmt = stmt.where(Control.id.notin_(scoped_ids))
    return list(session.execute(stmt.order_by(Control.control_id.asc())).scalars().all())


# ---- evidence (read side only)
# Ingestion, parsing and deletion live in app.evidence.service, which owns the files on
# disk. This module only reads what that pipeline persisted.
def list_evidence(
    session: Session,
    project_id: Optional[int] = None,
    evidence_type: Filterable = None,
    parse_status: Filterable = None,
    search: str = "",
) -> List[EvidenceFile]:
    stmt = select(EvidenceFile)
    if project_id is not None:
        stmt = stmt.where(EvidenceFile.project_id == int(project_id))
    types = _value_list(EvidenceType, evidence_type, "evidence_type")
    if types:
        stmt = stmt.where(EvidenceFile.evidence_type.in_(types))
    statuses = _value_list(ParseStatus, parse_status, "parse_status")
    if statuses:
        stmt = stmt.where(EvidenceFile.parse_status.in_(statuses))
    if search:
        stmt = stmt.where(EvidenceFile.filename.ilike(f"%{search.strip()}%"))
    stmt = stmt.order_by(EvidenceFile.uploaded_at.desc(), EvidenceFile.id.desc())
    return list(session.execute(stmt).scalars().all())


def get_evidence_file(session: Session, evidence_file_id: int) -> Optional[EvidenceFile]:
    return session.get(EvidenceFile, int(evidence_file_id))


def require_evidence_file(session: Session, evidence_file_id: int) -> EvidenceFile:
    evidence = get_evidence_file(session, evidence_file_id)
    if evidence is None:
        raise NotFoundError(f"Evidence file {evidence_file_id} not found.")
    return evidence


def evidence_to_dict(evidence: EvidenceFile) -> Dict[str, Any]:
    data = evidence.to_dict()
    # The absolute path is a server detail; the UI shows provenance, not filesystem layout.
    data.pop("stored_path", None)
    data["sha256_short"] = (evidence.sha256 or "")[:12]
    data["size_kb"] = round((evidence.size_bytes or 0) / 1024.0, 1)
    return data


def evidence_metadata(session: Session, evidence_file_id: int) -> Dict[str, Any]:
    """File record plus the chunk-level facts that make it citable.

    The embedded-chunk count is the honest answer to "is this file actually searchable
    by the vector retriever yet?", and the source-type breakdown tells an auditor
    whether the parser produced page, row or summary chunks for this artefact.
    """
    evidence = require_evidence_file(session, evidence_file_id)
    rows = session.execute(
        select(EvidenceChunk.source_type, func.count(EvidenceChunk.id))
        .where(EvidenceChunk.evidence_file_id == evidence.id)
        .group_by(EvidenceChunk.source_type)
    ).all()
    by_source_type = {str(source): int(count) for source, count in rows}

    embedded = session.execute(
        select(func.count(EvidenceChunk.id)).where(
            EvidenceChunk.evidence_file_id == evidence.id,
            EvidenceChunk.embedding.is_not(None),
        )
    ).scalar_one()

    pages = session.execute(
        select(func.count(func.distinct(EvidenceChunk.page_number))).where(
            EvidenceChunk.evidence_file_id == evidence.id,
            EvidenceChunk.page_number.is_not(None),
        )
    ).scalar_one()

    sheets = [
        value
        for value in session.execute(
            select(EvidenceChunk.sheet_name)
            .where(
                EvidenceChunk.evidence_file_id == evidence.id,
                EvidenceChunk.sheet_name.is_not(None),
            )
            .distinct()
        ).scalars().all()
        if value
    ]

    data = evidence_to_dict(evidence)
    data.update(
        {
            "chunks_stored": sum(by_source_type.values()),
            "chunks_embedded": int(embedded),
            "chunks_by_source_type": by_source_type,
            "distinct_pages": int(pages),
            "sheet_names": sorted(sheets),
        }
    )
    return data


def list_chunks(
    session: Session,
    evidence_file_id: int,
    limit: Optional[int] = None,
    offset: int = 0,
) -> List[EvidenceChunk]:
    stmt = (
        select(EvidenceChunk)
        .where(EvidenceChunk.evidence_file_id == int(evidence_file_id))
        .order_by(EvidenceChunk.chunk_index.asc(), EvidenceChunk.id.asc())
        .offset(max(0, int(offset)))
    )
    if limit is not None:
        stmt = stmt.limit(int(limit))
    return list(session.execute(stmt).scalars().all())


def get_chunk(session: Session, chunk_id: int) -> Optional[EvidenceChunk]:
    return session.get(EvidenceChunk, int(chunk_id))


def project_evidence_stats(session: Session, project_id: int) -> Dict[str, Any]:
    """Counts used by the evidence page header and by the report's evidence appendix."""
    files = session.execute(
        select(func.count(EvidenceFile.id)).where(EvidenceFile.project_id == int(project_id))
    ).scalar_one()
    chunks = session.execute(
        select(func.count(EvidenceChunk.id)).where(EvidenceChunk.project_id == int(project_id))
    ).scalar_one()
    bytes_total = session.execute(
        select(func.coalesce(func.sum(EvidenceFile.size_bytes), 0)).where(
            EvidenceFile.project_id == int(project_id)
        )
    ).scalar_one()
    return {
        "evidence_files": int(files),
        "evidence_chunks": int(chunks),
        "total_bytes": int(bytes_total),
        "total_mb": round(int(bytes_total) / (1024.0 * 1024.0), 2),
    }


# ---- assessments
def list_assessments(
    session: Session,
    project_id: Optional[int] = None,
    control_id: Optional[Any] = None,
    status: Filterable = None,
    risk_level: Filterable = None,
    mode: Filterable = None,
    reviewed: Optional[bool] = None,
    evaluation_run_id: Optional[int] = None,
    include_evaluation: bool = True,
    latest_per_control: bool = False,
    limit: Optional[int] = None,
    offset: int = 0,
) -> List[Assessment]:
    """AI assessments, newest first, with the filters both front ends expose.

    ``reviewed=True`` means "a human has recorded a decision other than PENDING";
    ``reviewed=False`` is the human-review work queue. ``include_evaluation=False``
    hides assessments produced by the evaluation harness, which belong to the research
    experiments rather than to an auditor's project.
    """
    stmt = select(Assessment)
    if project_id is not None:
        stmt = stmt.where(Assessment.project_id == int(project_id))
    if control_id is not None:
        control = require_control(session, control_id)
        stmt = stmt.where(Assessment.control_id == control.id)
    statuses = _value_list(AssessmentStatus, status, "status")
    if statuses:
        stmt = stmt.where(Assessment.status.in_(statuses))
    risks = _value_list(RiskLevel, risk_level, "risk_level")
    if risks:
        stmt = stmt.where(Assessment.risk_level.in_(risks))
    modes = _value_list(ExperimentMode, mode, "mode")
    if modes:
        stmt = stmt.where(Assessment.experiment_mode.in_(modes))
    if evaluation_run_id is not None:
        stmt = stmt.where(Assessment.evaluation_run_id == int(evaluation_run_id))
    elif not include_evaluation:
        stmt = stmt.where(Assessment.evaluation_run_id.is_(None))

    if reviewed is not None:
        reviewed_ids = select(HumanReview.assessment_id).where(
            HumanReview.decision != HumanDecision.PENDING.value
        )
        stmt = stmt.where(
            Assessment.id.in_(reviewed_ids) if reviewed else Assessment.id.notin_(reviewed_ids)
        )

    stmt = stmt.order_by(Assessment.created_at.desc(), Assessment.id.desc())
    rows = list(session.execute(stmt).scalars().all())

    if latest_per_control:
        rows = _latest_per_control(rows)
    if offset:
        rows = rows[max(0, int(offset)) :]
    if limit is not None:
        rows = rows[: int(limit)]
    return rows


def get_assessment(session: Session, assessment_id: int) -> Optional[Assessment]:
    """One assessment with citations (and their chunks), reviews and control preloaded.

    The detail view renders all of these together, so they are loaded in one go rather
    than lazily behind a template loop.
    """
    stmt = (
        select(Assessment)
        .where(Assessment.id == int(assessment_id))
        .options(
            selectinload(Assessment.citations).joinedload(AssessmentCitation.chunk),
            selectinload(Assessment.reviews),
            joinedload(Assessment.control),
        )
    )
    return session.execute(stmt).unique().scalars().first()


def require_assessment(session: Session, assessment_id: int) -> Assessment:
    assessment = get_assessment(session, assessment_id)
    if assessment is None:
        raise NotFoundError(f"Assessment {assessment_id} not found.")
    return assessment


def latest_assessment_for_control(
    session: Session,
    project_id: int,
    control_id_or_ref: Any,
    mode: Optional[str] = None,
) -> Optional[Assessment]:
    rows = list_assessments(
        session,
        project_id=project_id,
        control_id=control_id_or_ref,
        mode=mode,
        limit=1,
    )
    return rows[0] if rows else None


def list_findings(
    session: Session,
    project_id: Optional[int] = None,
    high_risk_only: bool = False,
    include_evaluation: bool = False,
) -> List[Assessment]:
    """Latest assessments that concluded on a deficiency - the Findings page population."""
    rows = list_assessments(
        session,
        project_id=project_id,
        status=[status.value for status in DEFICIENCY_STATUSES],
        include_evaluation=include_evaluation,
        latest_per_control=True,
    )
    if high_risk_only:
        high = {level.value for level in HIGH_RISK_LEVELS}
        rows = [row for row in rows if row.risk_level in high]
    return rows


def assessment_to_dict(assessment: Assessment, include_citations: bool = True) -> Dict[str, Any]:
    """Serialisable assessment, always carrying the AI-generated provenance flags."""
    data = assessment.to_dict()
    control = assessment.control
    data["control_name"] = control.name if control is not None else ""
    data["control_category"] = control.category if control is not None else ""
    data["citation_count"] = len(assessment.citations)
    data["is_reviewed"] = assessment.is_reviewed
    data["source"] = "AI-generated"
    data["requires_human_review"] = True
    if include_citations:
        data["citations"] = [citation_to_dict(citation) for citation in assessment.citations]
    review = assessment.latest_review
    data["latest_review"] = review_to_dict(review) if review is not None else None
    return data


def citation_to_dict(citation: AssessmentCitation) -> Dict[str, Any]:
    data = citation.to_dict()
    chunk = citation.chunk
    # Carrying the real chunk text lets the UI show the cited evidence in place, and
    # makes it visible when a citation resolves to nothing at all.
    data["chunk_text"] = chunk.text if chunk is not None else ""
    data["chunk_resolved"] = chunk is not None
    return data


def review_to_dict(review: HumanReview) -> Dict[str, Any]:
    data = review.to_dict()
    data["source"] = "Human auditor"
    return data


# ---- human review
def record_human_review(
    session: Session,
    assessment_id: int,
    reviewer_name: str,
    decision: Any,
    final_status: Optional[str] = None,
    final_risk_level: Optional[str] = None,
    final_finding: str = "",
    final_recommendation: str = "",
    comments: str = "",
    requested_evidence: Optional[Sequence[str]] = None,
    review_seconds: float = 0.0,
    usefulness_rating: Optional[int] = None,
    flagged_hallucination: bool = False,
    hallucination_note: str = "",
) -> HumanReview:
    """Record the auditor's decision on an AI assessment and derive the agreement flags.

    Agreement is defined on the **outcome**, not on the process:
    ``agreed_with_ai_status`` is true whenever the auditor's final status equals the
    status the AI proposed, even when the decision was MODIFIED or REJECTED. An auditor
    who rewrites the finding text, adds a caveat, or rejects the reasoning while still
    concluding POTENTIAL_DEFICIENCY has agreed about the control's condition, and the
    human/AI concordance metric is about the condition. The ``decision`` field is stored
    alongside, so a stricter definition ("ACCEPTED only") can be recomputed from the same
    rows without re-reviewing anything - which is why both are kept rather than one.

    Two defaults are worth stating explicitly because they shape the metric:

    * If ``final_status`` is omitted, the AI status is carried over - the reviewer left
      the conclusion alone - so the review counts as agreement.
    * Except for MORE_EVIDENCE_REQUESTED, where the omitted status defaults to
      INSUFFICIENT_EVIDENCE. A reviewer asking for more evidence has not concluded, and
      recording the AI's status as theirs would inflate agreement with a decision the
      auditor did not make.

    A PENDING decision records the row (someone opened the review) but never counts as
    agreement and never counts as a completed review.
    """
    assessment = require_assessment(session, assessment_id)

    decision_member = HumanDecision.coerce(decision, None)
    if decision_member is None:
        raise InvalidInputError(
            f"Unknown review decision {decision!r}. Expected one of {HumanDecision.values()}."
        )

    ai_status = AssessmentStatus.coerce(assessment.status, AssessmentStatus.INSUFFICIENT_EVIDENCE)
    ai_risk = RiskLevel.coerce(assessment.risk_level, RiskLevel.NOT_RATED)
    is_pending = decision_member is HumanDecision.PENDING

    if final_status:
        status_member = AssessmentStatus.coerce(final_status, None)
        if status_member is None:
            raise InvalidInputError(
                f"Unknown final_status {final_status!r}. Expected one of {AssessmentStatus.values()}."
            )
    elif is_pending:
        status_member = None
    elif decision_member is HumanDecision.MORE_EVIDENCE_REQUESTED:
        status_member = AssessmentStatus.INSUFFICIENT_EVIDENCE
    else:
        status_member = ai_status

    if final_risk_level:
        risk_member = RiskLevel.coerce(final_risk_level, None)
        if risk_member is None:
            raise InvalidInputError(
                f"Unknown final_risk_level {final_risk_level!r}. Expected one of {RiskLevel.values()}."
            )
    elif is_pending:
        risk_member = RiskLevel.NOT_RATED
    else:
        risk_member = ai_risk

    review = HumanReview(
        assessment_id=assessment.id,
        reviewer_name=(reviewer_name or get_settings().default_auditor_name).strip(),
        decision=decision_member.value,
        final_status=status_member.value if status_member is not None else "",
        final_risk_level=risk_member.value,
        final_finding=final_finding or "",
        final_recommendation=final_recommendation or "",
        comments=comments or "",
        requested_evidence=_str_list(requested_evidence),
        agreed_with_ai_status=bool(status_member is not None and status_member == ai_status),
        agreed_with_ai_risk=bool(not is_pending and risk_member == ai_risk),
        review_seconds=max(0.0, float(review_seconds or 0.0)),
        usefulness_rating=_clamp_rating(usefulness_rating),
        flagged_hallucination=bool(flagged_hallucination),
        hallucination_note=hallucination_note or "",
    )
    session.add(review)
    session.commit()

    log_activity(
        session,
        entity_type="assessment",
        entity_id=assessment.id,
        action=ActivityAction.HUMAN_REVIEW_RECORDED,
        actor=review.reviewer_name,
        actor_type="HUMAN",
        details={
            "decision": review.decision,
            "ai_status": assessment.status,
            "final_status": review.final_status,
            "agreed_with_ai_status": review.agreed_with_ai_status,
            "agreed_with_ai_risk": review.agreed_with_ai_risk,
            "flagged_hallucination": review.flagged_hallucination,
        },
        project_id=assessment.project_id,
    )
    return review


def list_reviews(
    session: Session,
    project_id: Optional[int] = None,
    assessment_id: Optional[int] = None,
    reviewer_name: str = "",
    include_pending: bool = True,
) -> List[HumanReview]:
    stmt = select(HumanReview)
    if assessment_id is not None:
        stmt = stmt.where(HumanReview.assessment_id == int(assessment_id))
    if project_id is not None:
        stmt = stmt.join(Assessment, Assessment.id == HumanReview.assessment_id).where(
            Assessment.project_id == int(project_id)
        )
    if reviewer_name:
        stmt = stmt.where(HumanReview.reviewer_name == reviewer_name)
    if not include_pending:
        stmt = stmt.where(HumanReview.decision != HumanDecision.PENDING.value)
    stmt = stmt.order_by(HumanReview.created_at.desc(), HumanReview.id.desc())
    return list(session.execute(stmt).scalars().all())


def pending_reviews(session: Session, project_id: Optional[int] = None) -> List[Assessment]:
    """The review queue: the latest assessment per control with no completed decision.

    Deliberately computed as *latest first, then unreviewed* - the reverse order would
    resurrect a superseded assessment as "pending" whenever a control was re-run, and
    would disagree with ``dashboard_stats()["pending_human_reviews"]``. Both numbers come
    from this same pair of helpers so they cannot drift apart.
    """
    latest = _latest_per_control(
        list_assessments(session, project_id=project_id, include_evaluation=False)
    )
    reviews = _latest_review_per_assessment(session, [assessment.id for assessment in latest])
    return [assessment for assessment in latest if assessment.id not in _completed_ids(reviews)]


# ---- dashboard
def dashboard_stats(session: Session, project_id: Optional[int] = None) -> Dict[str, Any]:
    """Headline figures for the dashboard, for one project or across all of them.

    Three definitional choices are baked in here and reported back under
    ``definitions`` so the UI can show them and a reader can check them:

    1. Status counts are taken over the **latest assessment per control**, not over
       every assessment row. Re-running a control (or running modes A, B and C over the
       same control, as the experiments do) would otherwise count one control several
       times and make the deficiency count meaningless.
    2. Assessments produced by the evaluation harness are **excluded** - they belong to
       the research experiments, not to an auditor's project, and mixing them would
       inflate every operational figure.
    3. ``citation_grounding_rate`` counts only VERIFIED citations as grounded. PARTIAL
       matches are deliberately not counted, so the figure is a floor rather than a
       flattering estimate.
    """
    latest = _latest_per_control(
        list_assessments(session, project_id=project_id, include_evaluation=False)
    )
    latest_ids = [assessment.id for assessment in latest]

    status_counts: Dict[str, int] = {status.value: 0 for status in AssessmentStatus}
    for assessment in latest:
        status_counts[assessment.status] = status_counts.get(assessment.status, 0) + 1

    high_risk_values = {level.value for level in HIGH_RISK_LEVELS}
    deficiency_values = {status.value for status in DEFICIENCY_STATUSES}
    high_risk_findings = sum(
        1
        for assessment in latest
        if assessment.risk_level in high_risk_values and assessment.status in deficiency_values
    )

    # ---- controls
    total_controls = session.execute(
        select(func.count(Control.id)).where(Control.is_active.is_(True))
    ).scalar_one()
    scope_stmt = select(func.count(func.distinct(ProjectControl.control_id)))
    if project_id is not None:
        scope_stmt = scope_stmt.where(ProjectControl.project_id == int(project_id))
    controls_in_scope = session.execute(scope_stmt).scalar_one()
    controls_assessed = len({assessment.control_id for assessment in latest})

    # ---- human review
    latest_reviews = _latest_review_per_assessment(session, latest_ids)
    completed_ids = _completed_ids(latest_reviews)
    completed = [latest_reviews[assessment_id] for assessment_id in completed_ids]
    pending_human_reviews = sum(1 for assessment in latest if assessment.id not in completed_ids)
    completed_reviews = len(completed)
    agreement_rate = _ratio(sum(1 for r in completed if r.agreed_with_ai_status), completed_reviews)
    risk_agreement_rate = _ratio(sum(1 for r in completed if r.agreed_with_ai_risk), completed_reviews)

    # ---- evidence
    file_stmt = select(func.count(EvidenceFile.id))
    chunk_stmt = select(func.count(EvidenceChunk.id))
    if project_id is not None:
        file_stmt = file_stmt.where(EvidenceFile.project_id == int(project_id))
        chunk_stmt = chunk_stmt.where(EvidenceChunk.project_id == int(project_id))
    evidence_files = session.execute(file_stmt).scalar_one()
    evidence_chunks = session.execute(chunk_stmt).scalar_one()

    # ---- telemetry
    latencies = [assessment.latency_ms for assessment in latest if assessment.latency_ms > 0]
    avg_latency_ms = round(sum(latencies) / float(len(latencies)), 1) if latencies else 0.0
    citation_total, citation_verified = _citation_counts(session, latest_ids)

    return {
        "project_id": project_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_controls": int(total_controls),
        "controls_in_scope": int(controls_in_scope),
        "controls_assessed": int(controls_assessed),
        "effective": status_counts.get(AssessmentStatus.EFFECTIVE.value, 0),
        "potential_deficiencies": status_counts.get(AssessmentStatus.POTENTIAL_DEFICIENCY.value, 0),
        "not_effective": status_counts.get(AssessmentStatus.NOT_EFFECTIVE.value, 0),
        "insufficient_evidence": status_counts.get(AssessmentStatus.INSUFFICIENT_EVIDENCE.value, 0),
        "not_applicable": status_counts.get(AssessmentStatus.NOT_APPLICABLE.value, 0),
        "high_risk_findings": high_risk_findings,
        "pending_human_reviews": pending_human_reviews,
        "completed_reviews": completed_reviews,
        "human_ai_agreement_rate": agreement_rate,
        "human_ai_risk_agreement_rate": risk_agreement_rate,
        "evidence_files": int(evidence_files),
        "evidence_chunks": int(evidence_chunks),
        "avg_latency_ms": avg_latency_ms,
        "citation_grounding_rate": _ratio(citation_verified, citation_total),
        "citations_total": citation_total,
        "citations_verified": citation_verified,
        "assessments_counted": len(latest),
        "flagged_hallucinations": sum(1 for review in completed if review.flagged_hallucination),
        "definitions": _DASHBOARD_DEFINITIONS,
    }


#: Shipped with the figures so the dashboard can state what each number means. Every
#: rate is in [0, 1] and is 0.0 when its denominator is zero - never "100% of nothing".
_DASHBOARD_DEFINITIONS: Dict[str, str] = {
    "counting_basis": (
        "Status counts, risk counts and latency are computed over the latest assessment "
        "per control, excluding assessments produced by the evaluation harness."
    ),
    "high_risk_findings": (
        "Latest assessments whose status is POTENTIAL_DEFICIENCY or NOT_EFFECTIVE and "
        "whose risk level is HIGH or CRITICAL."
    ),
    "pending_human_reviews": (
        "Latest assessments with no human review decision other than PENDING."
    ),
    "human_ai_agreement_rate": (
        "Completed reviews whose final status equals the AI status, divided by completed "
        "reviews. A MODIFIED or REJECTED decision that lands on the same status counts as "
        "agreement: the metric is about the conclusion, not the editing."
    ),
    "citation_grounding_rate": (
        "Citations with verdict VERIFIED divided by all citations on the counted "
        "assessments. PARTIAL matches do not count as grounded."
    ),
    "avg_latency_ms": "Mean end-to-end latency of the counted assessments that recorded a latency.",
}


def status_breakdown(session: Session, project_id: Optional[int] = None) -> Dict[str, int]:
    """Latest-assessment status counts on their own, for charts."""
    latest = _latest_per_control(
        list_assessments(session, project_id=project_id, include_evaluation=False)
    )
    counts = {status.value: 0 for status in AssessmentStatus}
    for assessment in latest:
        counts[assessment.status] = counts.get(assessment.status, 0) + 1
    return counts


def risk_breakdown(session: Session, project_id: Optional[int] = None) -> Dict[str, int]:
    latest = _latest_per_control(
        list_assessments(session, project_id=project_id, include_evaluation=False)
    )
    counts = {level.value: 0 for level in RiskLevel}
    for assessment in latest:
        counts[assessment.risk_level] = counts.get(assessment.risk_level, 0) + 1
    return counts


# ---- activity trail
def log_activity(
    session: Session,
    entity_type: str,
    entity_id: Optional[int] = None,
    action: Any = "",
    actor: str = "",
    actor_type: str = "HUMAN",
    details: Optional[Dict[str, Any]] = None,
    project_id: Optional[int] = None,
) -> ActivityLog:
    """Append one row to the activity trail.

    ``action`` accepts an :class:`ActivityAction` member or a plain string, because a few
    actions (control library edits) have no enum member yet and inventing one would mean
    editing a foundation file this module does not own.
    """
    entry = ActivityLog(
        entity_type=entity_type or "",
        entity_id=int(entity_id) if entity_id is not None else None,
        project_id=int(project_id) if project_id is not None else None,
        action=action.value if isinstance(action, ActivityAction) else str(action or ""),
        actor=(actor or get_settings().default_auditor_name).strip(),
        actor_type=(actor_type or "HUMAN").upper(),
        details=dict(details or {}),
    )
    session.add(entry)
    session.commit()
    return entry


def list_activity(
    session: Session,
    project_id: Optional[int] = None,
    entity_type: Optional[str] = None,
    action: Filterable = None,
    limit: int = 50,
) -> List[ActivityLog]:
    stmt = select(ActivityLog)
    if project_id is not None:
        stmt = stmt.where(ActivityLog.project_id == int(project_id))
    if entity_type:
        stmt = stmt.where(ActivityLog.entity_type == entity_type)
    actions = _value_list(None, action, "action")
    if actions:
        stmt = stmt.where(ActivityLog.action.in_(actions))
    stmt = stmt.order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc()).limit(int(limit))
    return list(session.execute(stmt).scalars().all())


# ---- reports (read side; generation lives in app.audit.report)
def list_reports(session: Session, project_id: Optional[int] = None) -> List[AuditReport]:
    stmt = select(AuditReport)
    if project_id is not None:
        stmt = stmt.where(AuditReport.project_id == int(project_id))
    stmt = stmt.order_by(AuditReport.generated_at.desc(), AuditReport.id.desc())
    return list(session.execute(stmt).scalars().all())


def get_report(session: Session, report_id: int) -> Optional[AuditReport]:
    return session.get(AuditReport, int(report_id))


# ---- internal helpers
def _latest_per_control(assessments: Sequence[Assessment]) -> List[Assessment]:
    """Keep one assessment per (project, control): the most recent by created_at then id."""
    best: Dict[Tuple[int, int], Assessment] = {}
    for assessment in assessments:
        key = (assessment.project_id, assessment.control_id)
        current = best.get(key)
        if current is None or _assessment_sort_key(assessment) > _assessment_sort_key(current):
            best[key] = assessment
    return sorted(best.values(), key=_assessment_sort_key, reverse=True)


def _assessment_sort_key(assessment: Assessment) -> Tuple[datetime, int]:
    created = assessment.created_at or datetime.min.replace(tzinfo=timezone.utc)
    if created.tzinfo is None:  # SQLite can hand back naive datetimes
        created = created.replace(tzinfo=timezone.utc)
    return (created, assessment.id or 0)


def _latest_review_per_assessment(
    session: Session, assessment_ids: Sequence[int]
) -> Dict[int, HumanReview]:
    """Most recent review row per assessment; earlier drafts do not double-count."""
    if not assessment_ids:
        return {}
    rows = session.execute(
        select(HumanReview)
        .where(HumanReview.assessment_id.in_(list(assessment_ids)))
        .order_by(HumanReview.created_at.asc(), HumanReview.id.asc())
    ).scalars().all()
    latest: Dict[int, HumanReview] = {}
    for review in rows:
        latest[review.assessment_id] = review
    return latest


def _completed_ids(latest_reviews: Dict[int, HumanReview]) -> Set[int]:
    """Assessment ids whose most recent review is an actual decision, not PENDING."""
    return {
        assessment_id
        for assessment_id, review in latest_reviews.items()
        if review.decision != HumanDecision.PENDING.value
    }


def _citation_counts(session: Session, assessment_ids: Sequence[int]) -> Tuple[int, int]:
    if not assessment_ids:
        return (0, 0)
    total = session.execute(
        select(func.count(AssessmentCitation.id)).where(
            AssessmentCitation.assessment_id.in_(list(assessment_ids))
        )
    ).scalar_one()
    verified = session.execute(
        select(func.count(AssessmentCitation.id)).where(
            AssessmentCitation.assessment_id.in_(list(assessment_ids)),
            AssessmentCitation.verdict == "VERIFIED",
        )
    ).scalar_one()
    return (int(total), int(verified))


def _ratio(numerator: int, denominator: int) -> float:
    """Rates are 0.0 on an empty denominator - never 1.0, which would read as perfect."""
    if not denominator:
        return 0.0
    return round(float(numerator) / float(denominator), 4)


def _value_list(enum_cls: Any, value: Filterable, field_name: str) -> List[str]:
    """Normalise a filter argument into a list of string values.

    When ``enum_cls`` is given each item must resolve to a member, so a typo in a filter
    fails loudly here rather than silently returning an empty result set.
    """
    if value is None:
        return []
    raw: Sequence[Any]
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw = list(value)
    else:
        raw = [value]

    out: List[str] = []
    for item in raw:
        if item is None or item == "":
            continue
        if enum_cls is None:
            out.append(str(item))
            continue
        member = enum_cls.coerce(item, None)
        if member is None:
            raise InvalidInputError(
                f"Unknown {field_name} {item!r}. Expected one of {enum_cls.values()}."
            )
        out.append(member.value)
    return out


def _enum_value(enum_cls: Any, value: Any, default: Any, field_name: str) -> str:
    if value is None or value == "":
        return default.value
    member = enum_cls.coerce(value, None)
    if member is None:
        raise InvalidInputError(
            f"Unknown {field_name} {value!r}. Expected one of {enum_cls.values()}."
        )
    return member.value


def _str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _clamp_score(value: Any, default: int = 3) -> int:
    try:
        score = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(5, score))


def _clamp_rating(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        rating = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, min(5, rating))


def _coerce_datetime(value: Any) -> Optional[datetime]:
    """Accept a datetime, a date, or an ISO-8601 string from an API payload."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise InvalidInputError(f"Could not parse date/time {value!r}; expected ISO-8601.")


__all__ = [
    # Types and errors that appear in this module's own signatures.
    "Filterable",
    "InvalidInputError",
    "NotFoundError",
    "ServiceError",
    # Service functions. Nothing imported from SQLAlchemy belongs on this list.
    "assessment_to_dict",
    "citation_to_dict",
    "control_categories",
    "control_to_dict",
    "create_control",
    "create_project",
    "dashboard_stats",
    "deactivate_control",
    "delete_project",
    "evidence_metadata",
    "evidence_to_dict",
    "get_assessment",
    "get_chunk",
    "get_control",
    "get_evidence_file",
    "get_project",
    "get_report",
    "latest_assessment_for_control",
    "list_activity",
    "list_assessments",
    "list_chunks",
    "list_controls",
    "list_evidence",
    "list_findings",
    "EVALUATION_SCOPE_TAG",
    "list_projects",
    "list_reports",
    "list_reviews",
    "list_scoped_controls",
    "list_unscoped_controls",
    "log_activity",
    "pending_reviews",
    "project_evidence_stats",
    "project_to_dict",
    "record_human_review",
    "require_assessment",
    "require_control",
    "require_evidence_file",
    "require_project",
    "review_to_dict",
    "risk_breakdown",
    "scope_control",
    "scope_controls",
    "set_control_active",
    "status_breakdown",
    "unscope_control",
    "update_control",
    "update_project",
]
