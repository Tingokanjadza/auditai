"""Evidence: upload, inventory, deletion, and the chunk lookup behind every citation."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Path, Query, UploadFile, status
from sqlalchemy.orm import Session

from app.audit import service
from app.config import SUPPORTED_UPLOAD_EXTENSIONS, get_settings
from app.database.base import get_db
from app.database.models import EvidenceChunk
from app.evidence.service import EvidenceTooLargeError, delete_evidence, get_chunk_context, ingest_file
from app.evidence.service import locator_from_chunk
from app.schemas.api import (
    ChunkContextResponse,
    ChunkResponse,
    DeleteResponse,
    EvidenceDetailResponse,
    EvidenceResponse,
    EvidenceStatsResponse,
)
from app.schemas.enums import EvidenceType, ParseStatus

router = APIRouter(prefix="/evidence", tags=["evidence"])

#: Upload read granularity. The body is read in blocks so an oversized file is rejected
#: after one megabyte rather than after the whole thing has been pulled into memory.
_READ_BLOCK = 1024 * 1024


@router.post(
    "/upload",
    response_model=EvidenceDetailResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload one evidence file",
    description=(
        "Stores the bytes, hashes them, parses the file into locator-bearing chunks and "
        "embeds those chunks for retrieval - synchronously - then returns the parsed "
        "metadata. Expect roughly a second for a policy document and longer for a large "
        "spreadsheet.\n\n"
        "Supported extensions: {0}. A file with any other extension is still stored and "
        "hashed, and comes back with ``parse_status`` UNSUPPORTED rather than being "
        "discarded, so nothing an auditor supplied disappears silently. A file that "
        "parses to no chunks comes back FAILED with the reason in ``parse_error``.\n\n"
        "Uploads larger than the configured limit are rejected with 413."
    ).format(", ".join(SUPPORTED_UPLOAD_EXTENSIONS)),
)
def upload_evidence(
    project_id: int = Form(..., ge=1, description="Project this evidence belongs to."),
    file: UploadFile = File(..., description="The evidence file."),
    evidence_type: EvidenceType = Form(
        default=EvidenceType.OTHER, description="Auditor's classification of the artefact."
    ),
    description: str = Form(default="", description="Free-text note about provenance."),
    uploaded_by: str = Form(default="", description="Who supplied it; recorded in the activity trail."),
    is_synthetic: bool = Form(
        default=False, description="Mark generated research data as synthetic so it is never mistaken for fieldwork."
    ),
    session: Session = Depends(get_db),
) -> Dict[str, Any]:
    settings = get_settings()
    filename = (file.filename or "").strip()
    if not filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The uploaded part has no filename.")

    # Read in blocks and stop at the limit: the point of a size limit is not to have to
    # hold the oversized file in memory in order to discover that it is oversized.
    limit = settings.max_upload_bytes
    payload = bytearray()
    while True:
        block = file.file.read(_READ_BLOCK)
        if not block:
            break
        payload.extend(block)
        if len(payload) > limit:
            raise EvidenceTooLargeError(
                "'{0}' exceeds the configured upload limit of {1} MB.".format(filename, settings.max_upload_mb)
            )

    record = ingest_file(
        session,
        project_id=project_id,
        data=bytes(payload),
        filename=filename,
        evidence_type=evidence_type,
        description=description,
        uploaded_by=uploaded_by,
        is_synthetic=is_synthetic,
    )
    return service.evidence_metadata(session, record.id)


@router.get(
    "",
    response_model=List[EvidenceResponse],
    summary="List evidence files",
    description="Newest first. ``stored_path`` is never returned; the SHA-256 is the integrity anchor.",
)
def list_evidence(
    session: Session = Depends(get_db),
    project_id: Optional[int] = Query(default=None, ge=1),
    evidence_type: Optional[List[EvidenceType]] = Query(default=None, description="Repeatable."),
    parse_status: Optional[List[ParseStatus]] = Query(default=None, description="Repeatable."),
    search: str = Query(default="", description="Substring match on filename."),
) -> List[Dict[str, Any]]:
    files = service.list_evidence(
        session,
        project_id=project_id,
        evidence_type=[item.value for item in evidence_type] if evidence_type else None,
        parse_status=[item.value for item in parse_status] if parse_status else None,
        search=search,
    )
    return [service.evidence_to_dict(item) for item in files]


@router.get(
    "/stats",
    response_model=EvidenceStatsResponse,
    summary="Evidence totals for one project",
)
def evidence_stats(
    project_id: int = Query(..., ge=1),
    session: Session = Depends(get_db),
) -> Dict[str, Any]:
    service.require_project(session, project_id)
    return service.project_evidence_stats(session, project_id)


@router.get(
    "/chunks/{chunk_id}",
    response_model=ChunkContextResponse,
    summary="Read a cited chunk with its neighbours",
    description=(
        "The endpoint behind a clickable citation. It returns the chunk a finding quoted, "
        "its locator, and the chunks either side of it, because a quoted fragment can only "
        "be checked by someone who can see what surrounded it."
    ),
)
def get_chunk(
    chunk_id: int = Path(..., ge=1),
    window: int = Query(default=1, ge=0, le=10, description="How many chunks of context to include each side."),
    session: Session = Depends(get_db),
) -> Dict[str, Any]:
    context = get_chunk_context(session, chunk_id, window=window)
    if not context.get("found"):
        raise service.NotFoundError("Evidence chunk {0} not found.".format(chunk_id))
    return context


@router.get(
    "/{evidence_file_id}",
    response_model=EvidenceDetailResponse,
    summary="Get one evidence file with its chunk-level facts",
)
def get_evidence(
    evidence_file_id: int = Path(..., ge=1),
    session: Session = Depends(get_db),
) -> Dict[str, Any]:
    return service.evidence_metadata(session, evidence_file_id)


@router.get(
    "/{evidence_file_id}/chunks",
    response_model=List[ChunkResponse],
    summary="List the chunks parsed out of one file",
    description="In parse order, so a reviewer can read the file the way the retriever sees it.",
)
def list_file_chunks(
    evidence_file_id: int = Path(..., ge=1),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db),
) -> List[Dict[str, Any]]:
    service.require_evidence_file(session, evidence_file_id)
    chunks = service.list_chunks(session, evidence_file_id, limit=limit, offset=offset)
    return [_chunk_payload(chunk) for chunk in chunks]


@router.delete(
    "/{evidence_file_id}",
    response_model=DeleteResponse,
    summary="Delete an evidence file",
    description=(
        "Removes the row, its chunks and the stored bytes, and records the deletion in the "
        "activity trail - deleting evidence is itself an audit-relevant act. Citations that "
        "pointed at those chunks survive as rows with an unresolved chunk, which is "
        "deliberate: a report must not quietly lose the fact that it cited something."
    ),
)
def delete_evidence_file(
    evidence_file_id: int = Path(..., ge=1),
    session: Session = Depends(get_db),
) -> Dict[str, Any]:
    service.require_evidence_file(session, evidence_file_id)
    removed = delete_evidence(session, evidence_file_id)
    return {
        "deleted": bool(removed),
        "id": int(evidence_file_id),
        "message": "Evidence file, chunks and stored bytes were removed.",
    }


def _chunk_payload(chunk: EvidenceChunk) -> Dict[str, Any]:
    """Serialise one chunk row.

    Deliberately identical in shape to the chunk dictionaries inside
    ``app.evidence.service.get_chunk_context``, so a client can use one model for both.
    It is written out here rather than imported because that module's per-chunk helper is
    private; a public ``chunk_to_dict`` in the service layer would remove this duplication.
    ``EvidenceChunk.to_dict()`` is not usable: it would emit the raw embedding bytes.
    """
    return {
        "chunk_id": int(chunk.id),
        "chunk_index": int(chunk.chunk_index),
        "text": chunk.text,
        "char_count": int(chunk.char_count),
        "source_type": chunk.source_type,
        "filename": chunk.filename,
        "locator_text": chunk.locator_text,
        "locator": locator_from_chunk(chunk).to_dict(),
        "extra_metadata": dict(chunk.extra_metadata or {}),
    }
