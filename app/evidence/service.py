"""Ingestion: bytes in, citable and retrievable evidence out.

This is the only place that turns an upload into database rows, and it exists to keep
three things that must not drift apart in one transaction: the stored artefact and its
hash, the chunks quoted in findings, and the vectors retrieval searches. If chunks were
written without their locator columns, or indexed without being persisted, a citation
could point at something no one can open.

Two deliberate design choices are worth stating:

* **Every locator column is written explicitly, plus ``locator_text``.** Retrieval and
  the report renderer must be able to show an auditor where a quote came from without
  re-parsing the source file, and the denormalised one-line citation is what appears in
  the report appendix.
* **Indexing is best-effort and never fails an upload.** ``app.rag.indexer`` is imported
  lazily inside the call so a missing or broken retrieval layer degrades to "stored,
  parsed, not yet searchable" - recorded as a warning on the row - rather than losing the
  evidence entirely. Keyword retrieval still works without vectors; silently discarding an
  upload because an embedding failed would be far worse.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import SUPPORTED_UPLOAD_EXTENSIONS, get_settings
from app.database.models import ActivityLog, AuditProject, EvidenceChunk, EvidenceFile
from app.evidence.chunking import estimate_tokens
from app.evidence.parsers import ParseResult, parse_file
from app.evidence.storage import StoredFile, delete_stored, save_upload
from app.rag.base import SourceLocator
from app.schemas.enums import ActivityAction, EvidenceType, ParseStatus

__all__ = [
    "EvidenceIngestError",
    "EvidenceTooLargeError",
    "delete_evidence",
    "get_chunk_context",
    "ingest_file",
    "locator_from_chunk",
    "persist_chunks",
]

#: Column widths from ``app.database.models``. SQLite ignores them; PostgreSQL will not.
_MAX_FILENAME = 512
_MAX_SHEET = 255
_MAX_SECTION = 512
_MAX_LOCATOR = 512


class EvidenceIngestError(RuntimeError):
    """Raised when an upload cannot be accepted at all (nothing is stored)."""


class EvidenceTooLargeError(EvidenceIngestError):
    """Raised when an upload exceeds ``settings.max_upload_bytes``."""


def ingest_file(
    session: Session,
    project_id: int,
    data: bytes,
    filename: str,
    evidence_type: Union[str, EvidenceType] = EvidenceType.OTHER,
    description: str = "",
    uploaded_by: str = "",
    is_synthetic: bool = False,
) -> EvidenceFile:
    """Store, parse, chunk, persist and index one uploaded artefact.

    Returns the persisted :class:`~app.database.models.EvidenceFile` in every case that
    produced a row - including a file that could not be parsed, which is recorded with
    ``parse_status`` FAILED or UNSUPPORTED and its warnings rather than being dropped. An
    exception is raised only when nothing is stored at all (missing project, oversized
    upload), so a caller never has to guess whether something reached the database.
    """
    settings = get_settings()
    payload = bytes(data or b"")

    project = session.get(AuditProject, int(project_id))
    if project is None:
        raise EvidenceIngestError("Audit project {0} does not exist.".format(project_id))

    if len(payload) > settings.max_upload_bytes:
        raise EvidenceTooLargeError(
            "'{0}' is {1:.1f} MB; the configured limit is {2} MB.".format(
                filename, len(payload) / (1024.0 * 1024.0), settings.max_upload_mb
            )
        )

    stored: StoredFile = save_upload(payload, filename, int(project_id))
    actor = uploaded_by or settings.default_auditor_name

    record = EvidenceFile(
        project_id=int(project_id),
        filename=str(filename)[:_MAX_FILENAME],
        stored_path=stored.stored_path,
        extension=stored.extension,
        content_type=stored.content_type,
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
        evidence_type=str(EvidenceType.coerce(evidence_type, EvidenceType.OTHER).value),
        description=description or None,
        uploaded_by=actor,
        parse_status=ParseStatus.PENDING.value,
        is_synthetic=bool(is_synthetic),
        extra_metadata={},
    )
    session.add(record)
    session.flush()

    if stored.extension not in SUPPORTED_UPLOAD_EXTENSIONS:
        message = "Unsupported file type '{0}'. Supported: {1}.".format(
            stored.extension or "(none)", ", ".join(SUPPORTED_UPLOAD_EXTENSIONS)
        )
        record.parse_status = ParseStatus.UNSUPPORTED.value
        record.parse_error = message
        record.extra_metadata = {"warnings": [message]}
        _log(session, record, ActivityAction.EVIDENCE_UPLOADED, actor, {"parse_status": record.parse_status})
        session.commit()
        return record

    parsed: ParseResult = parse_file(stored.stored_path, str(filename))
    persist_chunks(session, record, parsed)

    warnings: List[str] = list(parsed.warnings)
    record.page_count = int(parsed.page_count)
    record.row_count = int(parsed.row_count)
    record.char_count = int(parsed.char_count)
    record.chunk_count = len(parsed.chunks)
    record.parse_status = (ParseStatus.PARSED if parsed.chunks else ParseStatus.FAILED).value
    record.parse_error = "; ".join(warnings) if (warnings and not parsed.chunks) else None
    metadata: Dict[str, Any] = dict(parsed.extra_metadata)
    metadata["warnings"] = warnings
    # Reassigned rather than mutated: a plain JSON column is not change-tracked in place.
    record.extra_metadata = metadata
    session.commit()

    indexed = _index_best_effort(session, record, warnings)
    metadata = dict(record.extra_metadata)
    metadata["warnings"] = warnings
    metadata["indexed_chunks"] = indexed
    record.extra_metadata = metadata

    _log(
        session,
        record,
        ActivityAction.EVIDENCE_UPLOADED,
        actor,
        {
            "filename": record.filename,
            "size_bytes": record.size_bytes,
            "sha256": record.sha256,
            "evidence_type": record.evidence_type,
            "is_synthetic": record.is_synthetic,
        },
    )
    _log(
        session,
        record,
        ActivityAction.EVIDENCE_PARSED,
        actor,
        {
            "parse_status": record.parse_status,
            "chunks": record.chunk_count,
            "pages": record.page_count,
            "rows": record.row_count,
            "indexed_chunks": indexed,
            "warnings": warnings,
        },
    )
    session.commit()
    return record


def persist_chunks(session: Session, record: EvidenceFile, parsed: ParseResult) -> List[EvidenceChunk]:
    """Write one ``EvidenceChunk`` per parsed chunk with every locator column populated.

    ``locator_text`` is rendered here rather than in the UI so that the citation string
    an auditor reads, the one stored on ``AssessmentCitation`` and the one shown to the
    model are all literally the same string.
    """
    rows: List[EvidenceChunk] = []
    for chunk in parsed.chunks:
        locator = chunk.locator or SourceLocator(filename=record.filename)
        if not locator.filename:
            locator.filename = record.filename
        row = EvidenceChunk(
            evidence_file_id=record.id,
            project_id=record.project_id,
            chunk_index=int(chunk.chunk_index),
            text=chunk.text,
            char_count=len(chunk.text),
            token_estimate=estimate_tokens(chunk.text),
            source_type=str(locator.source_type or ""),
            filename=str(locator.filename)[:_MAX_FILENAME],
            page_number=locator.page_number,
            sheet_name=str(locator.sheet_name)[:_MAX_SHEET] if locator.sheet_name else None,
            row_start=locator.row_start,
            row_end=locator.row_end,
            row_numbers=[int(n) for n in (locator.row_numbers or [])],
            column_names=[str(c) for c in (locator.column_names or [])],
            section=str(locator.section)[:_MAX_SECTION] if locator.section else None,
            paragraph_index=locator.paragraph_index,
            locator_text=locator.render()[:_MAX_LOCATOR],
            extra_metadata=dict(chunk.extra_metadata or {}),
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


def locator_from_chunk(chunk: EvidenceChunk) -> SourceLocator:
    """Rebuild a :class:`SourceLocator` from stored columns, without re-parsing anything."""
    return SourceLocator(
        filename=chunk.filename or "",
        source_type=chunk.source_type or "",
        page_number=chunk.page_number,
        sheet_name=chunk.sheet_name,
        row_start=chunk.row_start,
        row_end=chunk.row_end,
        row_numbers=list(chunk.row_numbers or []),
        column_names=list(chunk.column_names or []),
        section=chunk.section,
        paragraph_index=chunk.paragraph_index,
    )


def delete_evidence(session: Session, evidence_file_id: int) -> bool:
    """Remove an evidence file, its chunks and its stored bytes. Idempotent.

    Deleting evidence is itself an audit-relevant act, so the activity trail records it
    (including whether the file on disk was actually removed) before the row disappears.
    """
    record = session.get(EvidenceFile, int(evidence_file_id))
    if record is None:
        return False

    project_id = record.project_id
    details = {
        "filename": record.filename,
        "sha256": record.sha256,
        "chunks": record.chunk_count,
        "stored_path_removed": delete_stored(record.stored_path),
    }
    session.add(
        ActivityLog(
            entity_type="evidence_file",
            entity_id=int(evidence_file_id),
            project_id=project_id,
            action=ActivityAction.EVIDENCE_DELETED.value,
            actor=record.uploaded_by or get_settings().default_auditor_name,
            actor_type="HUMAN",
            details=details,
        )
    )
    session.delete(record)  # chunks cascade
    session.commit()
    return True


def get_chunk_context(session: Session, chunk_id: int, window: int = 1) -> Dict[str, Any]:
    """Return a chunk with its neighbours, so a citation can be read in context.

    A quoted fragment is only checkable if the reviewer can see what surrounded it. The
    return value is a plain dictionary rather than ORM rows so that the API, the
    Streamlit viewer and the report renderer can all consume it unchanged.
    """
    chunk = session.get(EvidenceChunk, int(chunk_id))
    if chunk is None:
        return {"found": False, "chunk_id": int(chunk_id), "before": [], "after": [], "chunks": []}

    span = max(0, int(window))
    statement = (
        select(EvidenceChunk)
        .where(
            EvidenceChunk.evidence_file_id == chunk.evidence_file_id,
            EvidenceChunk.chunk_index >= chunk.chunk_index - span,
            EvidenceChunk.chunk_index <= chunk.chunk_index + span,
        )
        .order_by(EvidenceChunk.chunk_index)
    )
    neighbours = list(session.execute(statement).scalars())

    before = [_chunk_dict(c) for c in neighbours if c.chunk_index < chunk.chunk_index]
    after = [_chunk_dict(c) for c in neighbours if c.chunk_index > chunk.chunk_index]
    evidence_file = session.get(EvidenceFile, chunk.evidence_file_id)

    return {
        "found": True,
        "chunk_id": int(chunk.id),
        "evidence_file_id": int(chunk.evidence_file_id),
        "project_id": int(chunk.project_id),
        "filename": chunk.filename,
        "evidence_type": evidence_file.evidence_type if evidence_file else "",
        "chunk_index": int(chunk.chunk_index),
        "locator_text": chunk.locator_text,
        "source_type": chunk.source_type,
        "text": chunk.text,
        "chunk": _chunk_dict(chunk),
        "before": before,
        "after": after,
        "chunks": [_chunk_dict(c) for c in neighbours],
        "context_text": "\n\n---\n\n".join(c.text for c in neighbours),
    }


# ---- internals
def _index_best_effort(session: Session, record: EvidenceFile, warnings: List[str]) -> int:
    """Embed the new chunks, downgrading any failure to a warning on the row.

    The import is deliberately inside the function: ingestion must keep working while the
    retrieval layer is absent, half-written or misconfigured.
    """
    if not record.chunk_count:
        return 0
    try:
        from app.rag.indexer import index_evidence_file

        return int(index_evidence_file(session, record.id) or 0)
    except Exception as exc:  # noqa: BLE001 - including ImportError while the RAG layer is absent
        session.rollback()
        warnings.append(
            "Evidence was stored and parsed but vector indexing failed ({0}: {1}). Keyword "
            "retrieval still works; re-index from the Settings page once resolved.".format(
                type(exc).__name__, exc
            )
        )
        return 0


def _chunk_dict(chunk: EvidenceChunk) -> Dict[str, Any]:
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


def _log(
    session: Session,
    record: EvidenceFile,
    action: ActivityAction,
    actor: str,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    session.add(
        ActivityLog(
            entity_type="evidence_file",
            entity_id=int(record.id),
            project_id=int(record.project_id),
            action=action.value,
            actor=actor,
            actor_type="HUMAN",
            details=dict(details or {}),
        )
    )
