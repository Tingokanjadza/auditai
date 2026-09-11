"""Writing embeddings onto stored evidence chunks.

Indexing is deliberately a separate step from parsing. Parsing decides *what* a
citable unit of evidence is; indexing decides how it is found again. Keeping them
apart means the embedding provider or dimensionality can change without re-uploading
or re-parsing a single file - :func:`reindex_project` simply rewrites the vectors.

The transaction belongs to the caller. Both functions ``flush`` so that the new
vectors are visible to the rest of the session, but neither commits: ingestion writes
the file, its chunks and their embeddings as one unit of work, and a partial commit in
the middle of that would leave half-indexed evidence behind.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database.models import EvidenceChunk
from app.rag.base import EmbeddingProvider
from app.rag.embeddings import chunk_document_text, encode_vector, get_embedding_provider

logger = logging.getLogger(__name__)

#: Chunks embedded per provider call. Bounded so a remote provider is not handed a
#: 500-chunk request, and so memory stays flat for a large spreadsheet.
DEFAULT_BATCH_SIZE = 64


def _document_for(chunk: EvidenceChunk) -> str:
    """The exact string embedded for a chunk - see ``chunk_document_text``."""
    return chunk_document_text(
        text=chunk.text or "",
        filename=chunk.filename or "",
        sheet_name=chunk.sheet_name,
        section=chunk.section,
        column_names=list(chunk.column_names or []),
    )


def _is_current(chunk: EvidenceChunk, provider: EmbeddingProvider, model_id: str) -> bool:
    """True when the stored vector was produced by this provider at this dimension."""
    return bool(
        chunk.embedding
        and chunk.embedding_dim == provider.dimension
        and (chunk.embedding_model or "") == model_id
    )


def index_chunks(
    session: Session,
    chunks: Sequence[EvidenceChunk],
    provider: Optional[EmbeddingProvider] = None,
    force: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Embed the given chunks in batches. Returns how many vectors were written."""
    if not chunks:
        return 0
    provider = provider or get_embedding_provider()
    model_id = str(getattr(provider, "model_id", provider.name))

    pending: List[EvidenceChunk] = [
        chunk for chunk in chunks if force or not _is_current(chunk, provider, model_id)
    ]
    if not pending:
        return 0

    written = 0
    for start in range(0, len(pending), max(1, batch_size)):
        batch = pending[start : start + max(1, batch_size)]
        vectors = provider.embed([_document_for(chunk) for chunk in batch])
        if len(vectors) != len(batch):
            raise ValueError(
                "Embedding provider returned {} vectors for {} chunks".format(len(vectors), len(batch))
            )
        for chunk, vector in zip(batch, vectors):
            chunk.embedding = encode_vector(vector)
            chunk.embedding_dim = len(vector)
            chunk.embedding_model = model_id
            written += 1

    session.flush()
    return written


def index_evidence_file(
    session: Session,
    evidence_file_id: int,
    provider: Optional[EmbeddingProvider] = None,
    force: bool = False,
    settings: Optional[Settings] = None,
) -> int:
    """Embed every chunk of one evidence file. Returns the number of vectors written.

    Called by the ingestion service immediately after chunks are persisted. Chunks
    that already carry a current vector are skipped unless ``force`` is set, so
    re-running ingestion on an unchanged file costs nothing.
    """
    settings = settings or get_settings()
    provider = provider or get_embedding_provider(settings=settings)
    chunks = (
        session.execute(
            select(EvidenceChunk)
            .where(EvidenceChunk.evidence_file_id == evidence_file_id)
            .order_by(EvidenceChunk.chunk_index, EvidenceChunk.id)
        )
        .scalars()
        .all()
    )
    if not chunks:
        logger.debug("No chunks to index for evidence file %s", evidence_file_id)
        return 0
    written = index_chunks(session, chunks, provider=provider, force=force)
    logger.info(
        "Indexed %s/%s chunks for evidence file %s with %s",
        written,
        len(chunks),
        evidence_file_id,
        getattr(provider, "model_id", provider.name),
    )
    return written


def reindex_project(
    session: Session,
    project_id: int,
    provider: Optional[EmbeddingProvider] = None,
    settings: Optional[Settings] = None,
) -> int:
    """Re-embed every chunk in a project. Returns the number of vectors written.

    Unconditional by design: this is the operation an auditor runs *after* changing
    the embedding provider or dimension, so honouring an "already indexed" check
    would leave the project with a mixture of incomparable vectors.
    """
    settings = settings or get_settings()
    provider = provider or get_embedding_provider(settings=settings)
    chunks = (
        session.execute(
            select(EvidenceChunk)
            .where(EvidenceChunk.project_id == project_id)
            .order_by(EvidenceChunk.evidence_file_id, EvidenceChunk.chunk_index, EvidenceChunk.id)
        )
        .scalars()
        .all()
    )
    written = index_chunks(session, chunks, provider=provider, force=True)
    logger.info("Re-indexed %s chunks for project %s", written, project_id)
    return written


__all__ = ["DEFAULT_BATCH_SIZE", "index_chunks", "index_evidence_file", "reindex_project"]
