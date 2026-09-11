"""Audit reports: generate, list, read and download.

The renderer in :mod:`app.audit.report` produces the ten mandated sections and keeps the
AI-generated assessment and the auditor's final assessment in separate columns
throughout, so a reader can always tell which is which.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, Path, Query, Response, status
from sqlalchemy.orm import Session

from app.audit import report as report_module
from app.audit import service
from app.database.base import get_db
from app.database.models import AuditReport
from app.schemas.api import ReportGenerateRequest, ReportResponse, ReportSummaryResponse

router = APIRouter(prefix="/reports", tags=["reports"])

_MEDIA_TYPES = {"markdown": "text/markdown; charset=utf-8", "html": "text/html; charset=utf-8"}
_EXTENSIONS = {"markdown": "md", "html": "html"}


@router.post(
    "/generate",
    response_model=ReportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Generate an audit report (synchronous)",
    description=(
        "Builds the report from what is already in the database - it never re-runs an "
        "assessment - and returns the rendered document in the response. A copy is also "
        "written under the configured report directory.\n\n"
        "The request blocks while the document is rendered; that is milliseconds to a "
        "couple of seconds depending on how many controls and citations the project has. "
        "Controls with no assessment, and assessments with no completed human review, are "
        "reported as such rather than omitted: a report that hides what was not covered is "
        "worse than one that says so."
    ),
)
def generate_report(
    payload: ReportGenerateRequest = Body(...),
    session: Session = Depends(get_db),
) -> Dict[str, Any]:
    service.require_project(session, payload.project_id)
    row = report_module.generate_report(
        session,
        project_id=payload.project_id,
        generated_by=payload.generated_by,
        fmt=payload.format,
        title=payload.title,
        include_evaluation=payload.include_evaluation,
    )
    return _report_payload(row, include_content=True)


@router.get(
    "",
    response_model=List[ReportSummaryResponse],
    summary="List generated reports",
    description="Newest first, without the document body. Fetch one report, or download it, to get the text.",
)
def list_reports(
    session: Session = Depends(get_db),
    project_id: Optional[int] = Query(default=None, ge=1),
) -> List[Dict[str, Any]]:
    rows = service.list_reports(session, project_id=project_id)
    return [_report_payload(row, include_content=False) for row in rows]


@router.get(
    "/{report_id}",
    response_model=ReportResponse,
    summary="Get one report including its rendered text",
)
def get_report(
    report_id: int = Path(..., ge=1),
    session: Session = Depends(get_db),
) -> Dict[str, Any]:
    return _report_payload(_require_report(session, report_id), include_content=True)


@router.get(
    "/{report_id}/download",
    response_class=Response,
    summary="Download one report as a file",
    description=(
        "Returns the stored text with a filename attachment header - Markdown or HTML "
        "according to the format it was generated in. The bytes come from the database "
        "row, not from the file on disk, so a report stays downloadable after its "
        "generated copy has been moved or archived."
    ),
    responses={
        200: {
            "content": {"text/markdown": {"schema": {"type": "string"}}, "text/html": {"schema": {"type": "string"}}},
            "description": "The rendered report as an attachment.",
        }
    },
)
def download_report(
    report_id: int = Path(..., ge=1),
    session: Session = Depends(get_db),
) -> Response:
    row = _require_report(session, report_id)
    fmt = report_module.normalise_format(row.format)
    return Response(
        content=row.content or "",
        media_type=_MEDIA_TYPES.get(fmt, "text/plain; charset=utf-8"),
        headers={"Content-Disposition": 'attachment; filename="{0}"'.format(_download_name(row, fmt))},
    )


def _require_report(session: Session, report_id: int) -> AuditReport:
    row = service.get_report(session, report_id)
    if row is None:
        raise service.NotFoundError("Report {0} not found.".format(report_id))
    return row


def _download_name(row: AuditReport, fmt: str) -> str:
    """Prefer the name the generator chose; fall back to a stable one built from the id."""
    if row.stored_path:
        name = os.path.basename(row.stored_path)
        if name:
            return name
    return "audit_report_{0}.{1}".format(row.id, _EXTENSIONS.get(fmt, "txt"))


def _report_payload(row: AuditReport, include_content: bool) -> Dict[str, Any]:
    """Serialise a report row.

    ``stored_path`` is replaced by ``has_file`` and a download URL for the same reason it
    is dropped from evidence payloads: the server's filesystem layout is not part of the
    audit trail, and a path a client cannot open is not useful to it.
    """
    payload: Dict[str, Any] = {
        "id": int(row.id),
        "project_id": int(row.project_id),
        "title": row.title or "",
        "format": row.format or "markdown",
        "generated_by": row.generated_by or "",
        "summary_stats": dict(row.summary_stats or {}),
        "generated_at": row.generated_at.isoformat() if row.generated_at else None,
        "content_chars": len(row.content or ""),
        "has_file": bool(row.stored_path),
        "download_url": "/api/v1/reports/{0}/download".format(row.id),
    }
    if include_content:
        payload["content"] = row.content or ""
    return payload
