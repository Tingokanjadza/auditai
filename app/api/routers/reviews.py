"""Human review: the auditor's decision on an AI assessment.

A review never edits the assessment it is about. The AI record stays exactly as the
model produced it and the human conclusion is a separate row, which is what makes
human/AI agreement measurable instead of overwritten - the point of the study.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, Query, status
from sqlalchemy.orm import Session

from app.audit import service
from app.database.base import get_db
from app.schemas.api import AssessmentSummaryResponse, ReviewCreateRequest, ReviewResponse

router = APIRouter(prefix="/reviews", tags=["reviews"])


@router.post(
    "",
    response_model=ReviewResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record a human review of an AI assessment",
    description=(
        "Agreement is derived on write and is defined on the **outcome**: "
        "``agreed_with_ai_status`` is true whenever the auditor's final status equals the "
        "AI's, even if the decision was MODIFIED or REJECTED. The raw ``decision`` is stored "
        "alongside it, so a stricter definition (ACCEPTED only) can be recomputed from the "
        "same rows without re-reviewing anything.\n\n"
        "Omitting ``final_status`` carries the AI status over for ACCEPTED, MODIFIED and "
        "REJECTED. MORE_EVIDENCE_REQUESTED always records INSUFFICIENT_EVIDENCE / NOT_RATED "
        "with both agreement flags false, whatever was supplied, because an auditor asking "
        "for more evidence has not concluded anything. A PENDING decision records that the "
        "review was opened and never counts as agreement. ``reviewer_name`` is required."
    ),
)
def create_review(
    payload: ReviewCreateRequest = Body(...),
    session: Session = Depends(get_db),
) -> Dict[str, Any]:
    review = service.record_human_review(
        session,
        assessment_id=payload.assessment_id,
        reviewer_name=payload.reviewer_name,
        decision=payload.decision,
        final_status=payload.final_status.value if payload.final_status else None,
        final_risk_level=payload.final_risk_level.value if payload.final_risk_level else None,
        final_finding=payload.final_finding,
        final_recommendation=payload.final_recommendation,
        comments=payload.comments,
        requested_evidence=payload.requested_evidence,
        review_seconds=payload.review_seconds,
        usefulness_rating=payload.usefulness_rating,
        flagged_hallucination=payload.flagged_hallucination,
        hallucination_note=payload.hallucination_note,
    )
    return service.review_to_dict(review)


@router.get(
    "",
    response_model=List[ReviewResponse],
    summary="List human reviews",
)
def list_reviews(
    session: Session = Depends(get_db),
    project_id: Optional[int] = Query(default=None, ge=1),
    assessment_id: Optional[int] = Query(default=None, ge=1),
    reviewer_name: str = Query(default="", description="Exact reviewer name."),
    include_pending: bool = Query(default=True, description="False hides rows whose decision is still PENDING."),
) -> List[Dict[str, Any]]:
    reviews = service.list_reviews(
        session,
        project_id=project_id,
        assessment_id=assessment_id,
        reviewer_name=reviewer_name,
        include_pending=include_pending,
    )
    return [service.review_to_dict(review) for review in reviews]


@router.get(
    "/pending",
    response_model=List[AssessmentSummaryResponse],
    summary="The review queue",
    description=(
        "The latest assessment per control with no completed human decision. Computed "
        "latest-first and then filtered, so re-running a control does not resurrect a "
        "superseded assessment as pending; this is the same pair of helpers behind "
        "``dashboard_stats().pending_human_reviews``, so the two cannot disagree."
    ),
)
def list_pending(
    session: Session = Depends(get_db),
    project_id: Optional[int] = Query(default=None, ge=1),
) -> List[Dict[str, Any]]:
    rows = service.pending_reviews(session, project_id=project_id)
    return [service.assessment_to_dict(row, include_citations=False) for row in rows]
