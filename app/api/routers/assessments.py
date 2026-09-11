"""AI control assessments: running one, listing them, and reading one with its citations.

Nothing this router returns is an audit conclusion. Every assessment payload carries
``source="AI-generated"`` and ``requires_human_review=true``, and the run endpoint
repeats the same statement in its ``notice`` field.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, Path, Query
from sqlalchemy.orm import Session

from app.audit import service
from app.audit.engine import assess
from app.database.base import get_db
from app.schemas.api import (
    AssessmentDetailResponse,
    AssessmentRunRequest,
    AssessmentRunResponse,
    AssessmentSummaryResponse,
)
from app.schemas.enums import AssessmentStatus, ExperimentMode, RiskLevel

router = APIRouter(prefix="/assessments", tags=["assessments"])

#: Repeated on every run response. The API is the one place a caller can automate around
#: the UI's banner, so the disclaimer has to be in the payload, not only on the screen.
AI_NOTICE = (
    "AI-generated assessment. It is a research prototype's suggestion, not an audit "
    "conclusion, and requires review by a qualified auditor before any reliance."
)


@router.post(
    "/run",
    response_model=AssessmentRunResponse,
    summary="Assess one control (synchronous)",
    description=(
        "Runs retrieval, the model call(s), citation validation and the prototype risk "
        "score, then persists the assessment.\n\n"
        "**This request blocks until the run finishes.** There is no job queue and no "
        "polling endpoint: reporting a fake 202 for work that has not happened would make "
        "the latency figures this study measures meaningless. Expect roughly 0.1-2 seconds "
        "with the offline mock provider, and anywhere from 5 to 60 seconds with a hosted "
        "model - mode C makes three model calls (sufficiency, assessment, self-critique), "
        "so it is about three times mode B. Set the client timeout accordingly; "
        "``api_request_timeout`` in settings is the value this project assumes.\n\n"
        "A model or provider failure does not raise: it is recorded on the assessment as an "
        "``error`` with status INSUFFICIENT_EVIDENCE, because a failed run that leaves no "
        "trace is indistinguishable from a run that was never attempted.\n\n"
        "Returns 200 rather than 201 because this is an operation, not a creation: with "
        "``persist=false`` nothing is stored at all. ``persisted`` reports whether a row "
        "was written, and ``run.assessment_id`` identifies it."
    ),
)
def run_assessment(
    payload: AssessmentRunRequest = Body(...),
    session: Session = Depends(get_db),
) -> Dict[str, Any]:
    result = assess(
        session,
        project_id=payload.project_id,
        control_ref=payload.control_ref,
        mode=payload.mode,
        persist=payload.persist,
    )
    assessment: Optional[Dict[str, Any]] = None
    if result.assessment_id:
        assessment = service.assessment_to_dict(service.require_assessment(session, result.assessment_id))
    return {
        "persisted": bool(result.assessment_id),
        "assessment": assessment,
        "run": result.to_dict(),
        "notice": AI_NOTICE,
    }


@router.get(
    "",
    response_model=List[AssessmentSummaryResponse],
    summary="List assessments",
    description=(
        "Newest first, without citations - fetch one assessment to see those. "
        "``latest_per_control=true`` collapses re-runs so one control is counted once, "
        "which is the basis the dashboard figures use."
    ),
)
def list_assessments(
    session: Session = Depends(get_db),
    project_id: Optional[int] = Query(default=None, ge=1),
    control_ref: Optional[str] = Query(default=None, description="Control reference or numeric id."),
    status_filter: Optional[List[AssessmentStatus]] = Query(default=None, alias="status", description="Repeatable."),
    risk_level: Optional[List[RiskLevel]] = Query(default=None, description="Repeatable."),
    mode: Optional[List[ExperimentMode]] = Query(default=None, description="Repeatable experiment-mode filter."),
    reviewed: Optional[bool] = Query(
        default=None, description="True: a human decision exists. False: the human-review queue."
    ),
    include_evaluation: bool = Query(
        default=False,
        description="Include assessments produced by the evaluation harness. Off by default - they are research runs, not fieldwork.",
    ),
    latest_per_control: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> List[Dict[str, Any]]:
    rows = service.list_assessments(
        session,
        project_id=project_id,
        control_id=control_ref,
        status=[item.value for item in status_filter] if status_filter else None,
        risk_level=[item.value for item in risk_level] if risk_level else None,
        mode=[item.value for item in mode] if mode else None,
        reviewed=reviewed,
        include_evaluation=include_evaluation,
        latest_per_control=latest_per_control,
        limit=limit,
        offset=offset,
    )
    return [service.assessment_to_dict(row, include_citations=False) for row in rows]


@router.get(
    "/findings",
    response_model=List[AssessmentSummaryResponse],
    summary="Latest assessments that concluded on a deficiency",
    description=(
        "The findings population: the most recent assessment per control whose status is "
        "POTENTIAL_DEFICIENCY or NOT_EFFECTIVE. Declared before '/{assessment_id}' so "
        "'findings' is never read as an id."
    ),
)
def list_findings(
    session: Session = Depends(get_db),
    project_id: Optional[int] = Query(default=None, ge=1),
    high_risk_only: bool = Query(default=False, description="Keep only HIGH and CRITICAL risk levels."),
    include_evaluation: bool = Query(default=False),
) -> List[Dict[str, Any]]:
    rows = service.list_findings(
        session,
        project_id=project_id,
        high_risk_only=high_risk_only,
        include_evaluation=include_evaluation,
    )
    return [service.assessment_to_dict(row, include_citations=False) for row in rows]


@router.get(
    "/{assessment_id}",
    response_model=AssessmentDetailResponse,
    summary="Get one assessment with its citations",
    description=(
        "Each citation carries the verifier's verdict and the real chunk text, so a "
        "reviewer can see both what was quoted and whether that quote was actually found "
        "in the evidence. Follow ``citations[].chunk_id`` to "
        "``GET /api/v1/evidence/chunks/{chunk_id}`` to read it in context."
    ),
)
def get_assessment(
    assessment_id: int = Path(..., ge=1),
    session: Session = Depends(get_db),
) -> Dict[str, Any]:
    return service.assessment_to_dict(service.require_assessment(session, assessment_id))
