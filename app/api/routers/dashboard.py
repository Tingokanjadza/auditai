"""Dashboard figures and the activity trail.

Every count here comes from ``app.audit.service`` so that the API and the Streamlit
dashboard cannot show different numbers for the same database.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.audit import service
from app.database.base import get_db
from app.schemas.api import ActivityResponse, BreakdownResponse, DashboardStatsResponse

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

#: Stated on every breakdown so a chart cannot be read as "all assessments ever run".
_BASIS = (
    "Latest assessment per control, excluding assessments produced by the evaluation harness."
)


@router.get(
    "/stats",
    response_model=DashboardStatsResponse,
    summary="Headline figures",
    description=(
        "For one project, or across all of them when ``project_id`` is omitted. The "
        "``definitions`` block travels with the numbers and states exactly what each one "
        "counted - including that rates are 0.0 on an empty denominator rather than 1.0, "
        "and that only VERIFIED citations count as grounded."
    ),
)
def stats(
    session: Session = Depends(get_db),
    project_id: Optional[int] = Query(default=None, ge=1),
) -> Dict[str, Any]:
    if project_id is not None:
        service.require_project(session, project_id)
    return service.dashboard_stats(session, project_id=project_id)


@router.get(
    "/status-breakdown",
    response_model=BreakdownResponse,
    summary="Assessment status counts",
)
def status_breakdown(
    session: Session = Depends(get_db),
    project_id: Optional[int] = Query(default=None, ge=1),
) -> Dict[str, Any]:
    if project_id is not None:
        service.require_project(session, project_id)
    return {
        "project_id": project_id,
        "counts": service.status_breakdown(session, project_id=project_id),
        "basis": _BASIS,
    }


@router.get(
    "/risk-breakdown",
    response_model=BreakdownResponse,
    summary="Risk-level counts",
    description="Risk bands come from the prototype research risk model, not from an official industry framework.",
)
def risk_breakdown(
    session: Session = Depends(get_db),
    project_id: Optional[int] = Query(default=None, ge=1),
) -> Dict[str, Any]:
    if project_id is not None:
        service.require_project(session, project_id)
    return {
        "project_id": project_id,
        "counts": service.risk_breakdown(session, project_id=project_id),
        "basis": _BASIS,
    }


@router.get(
    "/activity",
    response_model=List[ActivityResponse],
    summary="Recent activity trail",
    description="Append-only record of who did what, newest first. ``actor_type`` separates AI actions from human ones.",
)
def activity(
    session: Session = Depends(get_db),
    project_id: Optional[int] = Query(default=None, ge=1),
    entity_type: Optional[str] = Query(default=None, description="e.g. 'project', 'evidence_file', 'assessment'."),
    action: Optional[List[str]] = Query(default=None, description="Repeatable action filter."),
    limit: int = Query(default=50, ge=1, le=500),
) -> List[Dict[str, Any]]:
    rows = service.list_activity(
        session,
        project_id=project_id,
        entity_type=entity_type,
        action=action,
        limit=limit,
    )
    return [row.to_dict() for row in rows]
