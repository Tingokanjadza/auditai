"""Retrieval layer contracts.

The retrieval layer is intentionally modular: the assessment engine depends only on
:class:`BaseRetriever` and :class:`RetrievedChunk`, so keyword, vector and hybrid
strategies (and, later, an external vector database) are interchangeable.

Crucially, a :class:`RetrievedChunk` always carries its full source locator. Retrieval
never returns naked text - if a fragment cannot say where it came from, it cannot be
cited, and an uncitable fragment is useless to an auditor.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence


@dataclass
class SourceLocator:
    """Precise, format-aware pointer to where a piece of evidence lives."""

    filename: str = ""
    source_type: str = "TEXT_BLOCK"
    page_number: Optional[int] = None
    sheet_name: Optional[str] = None
    row_start: Optional[int] = None
    row_end: Optional[int] = None
    row_numbers: List[int] = field(default_factory=list)
    column_names: List[str] = field(default_factory=list)
    section: Optional[str] = None
    paragraph_index: Optional[int] = None

    def render(self) -> str:
        """One-line citation string shown to the auditor and to the model."""
        parts: List[str] = [self.filename or "(unknown file)"]
        if self.sheet_name:
            parts.append(f"sheet '{self.sheet_name}'")
        if self.page_number is not None:
            parts.append(f"page {self.page_number}")
        if self.section:
            section = self.section if len(self.section) <= 80 else self.section[:77] + "..."
            parts.append(f"section '{section}'")
        if self.row_numbers:
            preview = ", ".join(str(r) for r in self.row_numbers[:8])
            suffix = ", ..." if len(self.row_numbers) > 8 else ""
            parts.append(f"rows {preview}{suffix}")
        elif self.row_start is not None:
            if self.row_end is not None and self.row_end != self.row_start:
                parts.append(f"rows {self.row_start}-{self.row_end}")
            else:
                parts.append(f"row {self.row_start}")
        if self.paragraph_index is not None and self.page_number is None and not self.row_numbers:
            parts.append(f"paragraph {self.paragraph_index}")
        if self.column_names:
            parts.append("columns: " + ", ".join(self.column_names[:8]))
        return " - ".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "filename": self.filename,
            "source_type": self.source_type,
            "page_number": self.page_number,
            "sheet_name": self.sheet_name,
            "row_start": self.row_start,
            "row_end": self.row_end,
            "row_numbers": list(self.row_numbers),
            "column_names": list(self.column_names),
            "section": self.section,
            "paragraph_index": self.paragraph_index,
            "rendered": self.render(),
        }


@dataclass
class ParsedChunk:
    """A chunk produced by a document parser, before it is persisted."""

    text: str
    chunk_index: int = 0
    locator: SourceLocator = field(default_factory=SourceLocator)
    extra_metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def char_count(self) -> int:
        return len(self.text)


@dataclass
class RetrievedChunk:
    """A chunk selected by retrieval, with its score and full provenance."""

    chunk_id: int
    text: str
    locator: SourceLocator
    score: float = 0.0
    evidence_file_id: Optional[int] = None
    filename: str = ""
    evidence_type: str = ""
    source_type: str = "TEXT_BLOCK"
    keyword_score: float = 0.0
    vector_score: float = 0.0
    rank: int = 0
    extra_metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def citation(self) -> str:
        return self.locator.render()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "text": self.text,
            "score": round(self.score, 4),
            "keyword_score": round(self.keyword_score, 4),
            "vector_score": round(self.vector_score, 4),
            "rank": self.rank,
            "evidence_file_id": self.evidence_file_id,
            "filename": self.filename,
            "evidence_type": self.evidence_type,
            "source_type": self.source_type,
            "locator": self.locator.to_dict(),
            "citation": self.citation,
        }


@dataclass
class RetrievalResult:
    """Everything the engine needs to explain *why* these chunks were used."""

    chunks: List[RetrievedChunk] = field(default_factory=list)
    queries: List[str] = field(default_factory=list)
    strategy: str = ""
    total_candidates: int = 0
    elapsed_ms: int = 0
    notes: List[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.chunks)

    def __iter__(self):
        return iter(self.chunks)

    @property
    def chunk_ids(self) -> List[int]:
        return [c.chunk_id for c in self.chunks]

    def by_id(self, chunk_id: int) -> Optional[RetrievedChunk]:
        for chunk in self.chunks:
            if chunk.chunk_id == chunk_id:
                return chunk
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "queries": list(self.queries),
            "total_candidates": self.total_candidates,
            "elapsed_ms": self.elapsed_ms,
            "notes": list(self.notes),
            "chunks": [c.to_dict() for c in self.chunks],
        }


class EmbeddingProvider(ABC):
    """Turns text into vectors. Implementations must be swappable and dimension-stable."""

    name: str = "base"
    dimension: int = 0

    @abstractmethod
    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        ...

    def embed_one(self, text: str) -> List[float]:
        return self.embed([text])[0]

    def is_available(self) -> bool:
        return True

    def health(self) -> Dict[str, Any]:
        return {"provider": self.name, "dimension": self.dimension, "available": self.is_available()}


class BaseRetriever(ABC):
    """Selects the evidence chunks relevant to a control."""

    name: str = "base"

    @abstractmethod
    def retrieve(
        self,
        queries: Sequence[str],
        project_id: int,
        top_k: int = 12,
        evidence_file_ids: Optional[Sequence[int]] = None,
        min_score: float = 0.0,
    ) -> RetrievalResult:
        ...
