"""Audit projects: CRUD and the control scope that defines what will be tested."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, Path, Query, status
from sqlalchemy.orm import Session

from app.audit import service
from app.database.base import get_db
from app.schemas.api import (
    DeleteResponse,
    ProjectCreateRequest,
    ProjectResponse,
    ProjectUpdateRequest,
    ScopeControlsRequest,
    ScopeResponse,
)
from app.schemas.enums import ProjectStatus

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post(
    "",
    response_model=ProjectResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an audit project",
    description=(
        "Creates a project and, when ``control_refs`` is supplied, scopes those library "
        "controls in the same call. The project period is optional: it is descriptive "
        "metadata for the report header, not a filter on evidence."
    ),
)
def create_project(
    payload: ProjectCreateRequest = Body(...),
    session: Session = Depends(get_db),
) -> Dict[str, Any]:
    project = service.create_project(
        session,
        name=payload.name,
        audit_area=payload.audit_area,
        description=payload.description,
        period_start=payload.period_start,
        period_end=payload.period_end,
        auditor_name=payload.auditor_name,
        status=payload.status.value if payload.status else None,
        scope_note=payload.scope_note,
        is_demo=payload.is_demo,
        control_refs=payload.control_refs or None,
        actor=payload.actor,
    )
    return service.project_to_dict(session, project)


@router.get(
    "",
    response_model=List[ProjectResponse],
    summary="List audit projects",
    description="Newest first. Filters combine with AND.",
)
def list_projects(
    session: Session = Depends(get_db),
    status_filter: Optional[List[ProjectStatus]] = Query(
        default=None, alias="status", description="Repeatable; e.g. ?status=FIELDWORK&status=REVIEW."
    ),
    include_demo: bool = Query(default=True, description="False hides the seeded demo project."),
    search: str = Query(default="", description="Substring match on name or audit area."),
) -> List[Dict[str, Any]]:
    projects = service.list_projects(
        session,
        status=[item.value for item in status_filter] if status_filter else None,
        include_demo=include_demo,
        search=search,
    )
    return [service.project_to_dict(session, project) for project in projects]


@router.get(
    "/{project_id}",
    response_model=ProjectResponse,
    summary="Get one audit project",
)
def get_project(
    project_id: int = Path(..., ge=1),
    session: Session = Depends(get_db),
) -> Dict[str, Any]:
    project = service.require_project(session, project_id)
    return service.project_to_dict(session, project)


@router.patch(
    "/{project_id}",
    response_model=ProjectResponse,
    summary="Update an audit project",
    description=(
        "Partial update: only the fields present in the body are changed. Identifiers, "
        "timestamps and the demo flag are owned by the application and cannot be set here."
    ),
)
def update_project(
    project_id: int = Path(..., ge=1),
    payload: ProjectUpdateRequest = Body(...),
    session: Session = Depends(get_db),
) -> Dict[str, Any]:
    fields = payload.model_dump(exclude_unset=True, exclude={"actor"})
    if "status" in fields and fields["status"] is not None:
        fields["status"] = ProjectStatus(fields["status"]).value
    project = service.update_project(session, project_id, actor=payload.actor, **fields)
    return service.project_to_dict(session, project)


@router.delete(
    "/{project_id}",
    response_model=DeleteResponse,
    summary="Delete an audit project",
    description=(
        "Cascades to the project's evidence rows, assessments, reviews and reports, and "
        "removes the project's stored evidence files and rendered report files from disk. "
        "The deletion is written to the activity trail as PROJECT_DELETED."
    ),
)
def delete_project(
    project_id: int = Path(..., ge=1),
    session: Session = Depends(get_db),
) -> Dict[str, Any]:
    service.require_project(session, project_id)  # 404 rather than a silent no-op
    deleted = service.delete_project(session, project_id)
    return {
        "deleted": bool(deleted),
        "id": int(project_id),
        "message": "Project and its dependent records were deleted.",
    }


# ---- scope
@router.get(
    "/{project_id}/controls",
    response_model=ScopeResponse,
    summary="List the controls in scope for a project",
)
def list_scope(
    project_id: int = Path(..., ge=1),
    session: Session = Depends(get_db),
) -> Dict[str, Any]:
    service.require_project(session, project_id)
    controls = service.list_scoped_controls(session, project_id)
    refs = [control.control_id for control in controls]
    return {
        "project_id": int(project_id),
        "changed": False,
        "scoped_control_refs": refs,
        "controls_in_scope": len(refs),
        "message": "",
    }


@router.post(
    "/{project_id}/controls",
    response_model=ScopeResponse,
    summary="Put library controls in scope",
    description="Idempotent: scoping a control that is already in scope updates its note and nothing else.",
)
def scope_controls(
    project_id: int = Path(..., ge=1),
    payload: ScopeControlsRequest = Body(...),
    session: Session = Depends(get_db),
) -> Dict[str, Any]:
    service.scope_controls(
        session,
        project_id,
        payload.control_refs,
        scope_note=payload.scope_note,
        actor=payload.actor,
    )
    refs = [control.control_id for control in service.list_scoped_controls(session, project_id)]
    return {
        "project_id": int(project_id),
        "changed": True,
        "scoped_control_refs": refs,
        "controls_in_scope": len(refs),
        "message": "Scoped {0} control(s).".format(len(payload.control_refs)),
    }


@router.delete(
    "/{project_id}/controls/{control_ref}",
    response_model=ScopeResponse,
    summary="Remove a control from a project's scope",
    description=(
        "Past assessments of that control are left untouched: they are part of the "
        "evidence trail, and removing them because the scope changed would rewrite history."
    ),
)
def unscope_control(
    project_id: int = Path(..., ge=1),
    control_ref: str = Path(..., description="Control reference such as CONTROL-001, or a numeric library id."),
    actor: str = Query(default="", description="Name recorded in the activity trail."),
    session: Session = Depends(get_db),
) -> Dict[str, Any]:
    service.require_project(session, project_id)
    removed = service.unscope_control(session, project_id, control_ref, actor=actor)
    refs = [control.control_id for control in service.list_scoped_controls(session, project_id)]
    return {
        "project_id": int(project_id),
        "changed": bool(removed),
        "scoped_control_refs": refs,
        "controls_in_scope": len(refs),
        "message": (
            "Control removed from scope." if removed else "That control was not in scope; nothing changed."
        ),
    }
