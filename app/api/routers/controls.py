"""The control library: the reusable definitions every assessment is tested against."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, Path, Query, status
from sqlalchemy.orm import Session

from app.audit import service
from app.database.base import get_db
from app.schemas.api import ControlCreateRequest, ControlResponse, ControlUpdateRequest

router = APIRouter(prefix="/controls", tags=["controls"])


@router.get(
    "",
    response_model=List[ControlResponse],
    summary="List control-library entries",
    description="Ordered by control reference. Use ``project_id`` to list only the controls in one project's scope.",
)
def list_controls(
    session: Session = Depends(get_db),
    category: Optional[List[str]] = Query(default=None, description="Repeatable category filter."),
    active_only: bool = Query(default=True, description="False also returns retired controls."),
    search: str = Query(default="", description="Substring match on reference, name or objective."),
    project_id: Optional[int] = Query(default=None, ge=1, description="Narrow to controls scoped to this project."),
) -> List[Dict[str, Any]]:
    controls = service.list_controls(
        session,
        category=category,
        active_only=active_only,
        search=search,
        project_id=project_id,
    )
    return [service.control_to_dict(control) for control in controls]


@router.get(
    "/categories",
    response_model=List[str],
    summary="Distinct control categories",
    description="Declared before the '/{control_ref}' route so 'categories' is never read as a reference.",
)
def list_categories(
    session: Session = Depends(get_db),
    active_only: bool = Query(default=True),
) -> List[str]:
    return service.control_categories(session, active_only=active_only)


@router.get(
    "/{control_ref}",
    response_model=ControlResponse,
    summary="Get one control",
    description="Accepts the business reference ('CONTROL-001') or the numeric library id.",
)
def get_control(
    control_ref: str = Path(..., description="Control reference or numeric id."),
    session: Session = Depends(get_db),
) -> Dict[str, Any]:
    return service.control_to_dict(service.require_control(session, control_ref))


@router.post(
    "",
    response_model=ControlResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a control to the library",
    description=(
        "``control_id`` must be unique: it is the key printed in every citation, "
        "assessment and report. Framework references are informative only - this "
        "prototype asserts no official mapping to any published framework."
    ),
)
def create_control(
    payload: ControlCreateRequest = Body(...),
    session: Session = Depends(get_db),
) -> Dict[str, Any]:
    data = payload.model_dump(exclude={"actor"})
    data["source"] = "user-defined"
    control = service.create_control(session, data, actor=payload.actor)
    return service.control_to_dict(control)


@router.patch(
    "/{control_ref}",
    response_model=ControlResponse,
    summary="Update a control",
    description=(
        "Partial update. ``control_id`` cannot be changed: past assessments and reports "
        "cite it, so renaming it would orphan that history."
    ),
)
def update_control(
    control_ref: str = Path(..., description="Control reference or numeric id."),
    payload: ControlUpdateRequest = Body(...),
    session: Session = Depends(get_db),
) -> Dict[str, Any]:
    fields = payload.model_dump(exclude_unset=True, exclude={"actor"})
    control = service.update_control(session, control_ref, actor=payload.actor, **fields)
    return service.control_to_dict(control)


@router.post(
    "/{control_ref}/deactivate",
    response_model=ControlResponse,
    summary="Retire a control",
    description=(
        "A soft delete on purpose: assessments and reports reference the control row, so "
        "deleting it would break the evidence trail. A retired control disappears from "
        "pickers while every past finding still resolves."
    ),
)
def deactivate_control(
    control_ref: str = Path(..., description="Control reference or numeric id."),
    actor: str = Query(default="", description="Name recorded in the activity trail."),
    session: Session = Depends(get_db),
) -> Dict[str, Any]:
    return service.control_to_dict(service.deactivate_control(session, control_ref, actor=actor))


@router.post(
    "/{control_ref}/activate",
    response_model=ControlResponse,
    summary="Return a retired control to the library",
)
def activate_control(
    control_ref: str = Path(..., description="Control reference or numeric id."),
    actor: str = Query(default="", description="Name recorded in the activity trail."),
    session: Session = Depends(get_db),
) -> Dict[str, Any]:
    return service.control_to_dict(service.set_control_active(session, control_ref, True, actor=actor))
