"""Finding the evidence a control assessment must be built on.

Three strategies, one interface. They differ only in how a chunk is scored:

* :class:`KeywordRetriever` - BM25 over the project's chunks, implemented here rather
  than pulled in as a dependency so the ranking function is inspectable by a reader of
  this thesis and so identifier sub-tokens can be indexed (see ``embeddings.tokenize``).
* :class:`VectorRetriever` - cosine similarity over the vectors stored on each chunk.
* :class:`HybridRetriever` - reciprocal-rank fusion of the two.

What matters more than the scoring, for audit work, is what happens *after* scoring.
Audit evidence is pathologically unbalanced: a 100-row privileged-user listing produces
dozens of chunks that all mention "privileged account", while the policy that states
the actual requirement is a single paragraph. Pure relevance ranking hands the model a
hundred rows and no requirement, and the model then has nothing to test the rows
against. Two corrections are therefore applied to every strategy:

1. **A per-file cap** of ``max(2, top_k // 2)``, so no single file can occupy the whole
   evidence budget - relaxed by backfilling if the cap would return fewer chunks than
   asked for, since starving the model is not an improvement.
2. **Mandatory table summaries.** If any row-level chunk of a file is retrieved, that
   file's ``TABLE_SUMMARY`` chunk is retrieved too. Ten exception rows mean something
   very different in a population of 20 than in a population of 10,000, and the model
   cannot know which without the summary. These are added on top of ``top_k`` rather
   than displacing an exception row.

A control is expanded into several queries (see :mod:`app.rag.query_builder`), and the
two halves combine them differently on purpose: BM25 scores are summed across the
queries, because a term contributes zero when absent and summing lets an angle that
only one query asks about - typically the vocabulary of failure - still count; cosine
similarities are not additive, so the vector retriever reports the best-matching query.

Scores are reported honestly rather than made to look uniform: cosine similarity is an
absolute value in [0, 1], the summed BM25 score has no natural upper bound and is
reported normalised against the best-scoring chunk *of that retrieval* (the raw sum is
kept in ``extra_metadata['bm25_score']``), and the fused hybrid score is the raw
reciprocal-rank sum, whose maximum is ``2 / (k + 1)``.

``min_score`` therefore means different things per strategy and is compared against the
*component* score in each case: an absolute cosine for VECTOR, a relative BM25 score for
KEYWORD, and for HYBRID it is applied to both components before fusion rather than to
the fused value, which has no interpretable scale. The default of 0.0 filters nothing
beyond chunks that matched nothing at all.
"""

from __future__ import annotations

import logging
import math
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database.models import EvidenceChunk, EvidenceFile
from app.rag.base import (
    BaseRetriever,
    EmbeddingProvider,
    RetrievalResult,
    RetrievedChunk,
    SourceLocator,
)
from app.rag.embeddings import (
    EmbeddingError,
    chunk_document_text,
    decode_vector,
    get_embedding_provider,
    l2_normalise,
    tokenize,
)
from app.schemas.enums import RetrievalStrategy, SourceType

logger = logging.getLogger(__name__)

#: BM25 parameters. k1 controls how fast term frequency saturates, b how strongly a
#: long chunk is penalised. These are the standard Robertson/Lucene defaults; they are
#: named constants rather than tuned values because this prototype has no labelled
#: relevance data to tune them against.
BM25_K1 = 1.5
BM25_B = 0.75

#: Reciprocal-rank-fusion constant. 60 is the value from Cormack et al. (2009); it
#: damps the influence of the very top ranks so one confident retriever cannot
#: unilaterally decide the fused ordering.
RRF_K = 60


# ---- internal representations ---------------------------------------------------


@dataclass
class _CorpusRow:
    """A stored chunk plus the file-level attribute the chunk does not denormalise."""

    chunk: EvidenceChunk
    evidence_type: str = ""


@dataclass
class _Scored:
    row: _CorpusRow
    score: float
    raw_score: float = 0.0
    matched_terms: List[str] = field(default_factory=list)


def _locator_for(chunk: EvidenceChunk) -> SourceLocator:
    """Rebuild the citation locator from the chunk's persisted columns."""
    return SourceLocator(
        filename=chunk.filename or "",
        source_type=chunk.source_type or SourceType.TEXT_BLOCK.value,
        page_number=chunk.page_number,
        sheet_name=chunk.sheet_name,
        row_start=chunk.row_start,
        row_end=chunk.row_end,
        row_numbers=list(chunk.row_numbers or []),
        column_names=list(chunk.column_names or []),
        section=chunk.section,
        paragraph_index=chunk.paragraph_index,
    )


def _to_retrieved(
    row: _CorpusRow,
    score: float,
    keyword_score: float = 0.0,
    vector_score: float = 0.0,
    extra: Optional[Dict[str, Any]] = None,
) -> RetrievedChunk:
    chunk = row.chunk
    metadata: Dict[str, Any] = dict(chunk.extra_metadata or {})
    if extra:
        metadata.update(extra)
    return RetrievedChunk(
        chunk_id=int(chunk.id),
        text=chunk.text or "",
        locator=_locator_for(chunk),
        score=float(score),
        evidence_file_id=chunk.evidence_file_id,
        filename=chunk.filename or "",
        evidence_type=row.evidence_type or "",
        source_type=chunk.source_type or SourceType.TEXT_BLOCK.value,
        keyword_score=float(keyword_score),
        vector_score=float(vector_score),
        extra_metadata=metadata,
    )


def _document_for(chunk: EvidenceChunk) -> str:
    return chunk_document_text(
        text=chunk.text or "",
        filename=chunk.filename or "",
        sheet_name=chunk.sheet_name,
        section=chunk.section,
        column_names=list(chunk.column_names or []),
    )


def _load_corpus(
    session: Session,
    project_id: int,
    evidence_file_ids: Optional[Sequence[int]] = None,
) -> List[_CorpusRow]:
    """Load every candidate chunk for a project, with its file's evidence type.

    A prototype corpus is a few hundred chunks per project, so scoring in Python is
    both fast enough and far more transparent than pushing ranking into SQL. The join
    is an outer join so a chunk is never silently dropped because of a missing file row.
    """
    stmt = (
        select(EvidenceChunk, EvidenceFile.evidence_type)
        .join(EvidenceFile, EvidenceFile.id == EvidenceChunk.evidence_file_id, isouter=True)
        .where(EvidenceChunk.project_id == project_id)
    )
    if evidence_file_ids:
        stmt = stmt.where(EvidenceChunk.evidence_file_id.in_(list(evidence_file_ids)))
    stmt = stmt.order_by(EvidenceChunk.evidence_file_id, EvidenceChunk.chunk_index, EvidenceChunk.id)
    return [_CorpusRow(chunk=chunk, evidence_type=evidence_type or "") for chunk, evidence_type in session.execute(stmt).all()]


def _normalise_queries(queries: Any) -> List[str]:
    if queries is None:
        return []
    if isinstance(queries, str):
        queries = [queries]
    seen = set()
    cleaned: List[str] = []
    for query in queries:
        text = str(query or "").strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        cleaned.append(text)
    return cleaned


# ---- BM25 -----------------------------------------------------------------------


class _Bm25Index:
    """Okapi BM25 over an in-memory corpus of tokenised documents.

    Uses the non-negative IDF variant ``ln(1 + (N - df + 0.5) / (df + 0.5))``: with the
    classical formulation a term appearing in more than half the documents scores
    *negatively*, which in a small audit corpus (where "account" may well appear in
    every chunk) would actively push relevant evidence down the ranking.
    """

    def __init__(self, documents: Sequence[Sequence[str]], k1: float = BM25_K1, b: float = BM25_B) -> None:
        self.k1 = float(k1)
        self.b = float(b)
        self.doc_count = len(documents)
        self.doc_lengths: List[int] = [len(doc) for doc in documents]
        total_length = sum(self.doc_lengths)
        self.avg_doc_length = (total_length / self.doc_count) if self.doc_count else 0.0

        postings: Dict[str, List[Tuple[int, int]]] = defaultdict(list)
        for index, tokens in enumerate(documents):
            for term, freq in Counter(tokens).items():
                postings[term].append((index, freq))
        self.postings: Dict[str, List[Tuple[int, int]]] = dict(postings)
        self.idf: Dict[str, float] = {}
        for term, plist in self.postings.items():
            df = len(plist)
            self.idf[term] = math.log(1.0 + (self.doc_count - df + 0.5) / (df + 0.5))

    def score(self, query_terms: Sequence[str]) -> Tuple[Dict[int, float], Dict[int, List[Tuple[str, float]]]]:
        """Score one query, returning per-document scores and their term breakdown.

        Repeated query terms are counted once (query term frequency is ignored, as in
        Lucene): an auditor's query is a description, not a weighted vector.
        """
        scores: Dict[int, float] = defaultdict(float)
        contributions: Dict[int, List[Tuple[str, float]]] = defaultdict(list)
        if not self.doc_count:
            return {}, {}
        avg_length = self.avg_doc_length or 1.0
        for term in dict.fromkeys(query_terms):
            plist = self.postings.get(term)
            if not plist:
                continue
            idf = self.idf.get(term, 0.0)
            if idf <= 0.0:
                continue
            for index, freq in plist:
                length = self.doc_lengths[index] or 1
                denominator = freq + self.k1 * (1.0 - self.b + self.b * (length / avg_length))
                if denominator <= 0:
                    continue
                contribution = idf * freq * (self.k1 + 1.0) / denominator
                scores[index] += contribution
                contributions[index].append((term, contribution))
        return dict(scores), dict(contributions)


# ---- shared post-processing -----------------------------------------------------


def _apply_diversity_cap(
    candidates: Sequence[RetrievedChunk],
    top_k: int,
    notes: List[str],
) -> List[RetrievedChunk]:
    """Take the best ``top_k`` chunks, allowing at most ``max(2, top_k // 2)`` per file."""
    cap = max(2, top_k // 2)
    selected: List[RetrievedChunk] = []
    overflow: List[RetrievedChunk] = []
    per_file: Dict[Optional[int], int] = Counter()

    for candidate in candidates:
        if len(selected) >= top_k:
            break
        file_id = candidate.evidence_file_id
        if per_file[file_id] >= cap:
            overflow.append(candidate)
            continue
        per_file[file_id] += 1
        selected.append(candidate)

    if overflow and len(selected) < top_k:
        # The cap is a diversity heuristic, not a hard limit: returning six chunks
        # when twelve were asked for would starve the assessment of evidence.
        backfill = overflow[: top_k - len(selected)]
        selected.extend(backfill)
        notes.append(
            "Per-file cap of {} relaxed for {} chunk(s) to reach top_k={}.".format(cap, len(backfill), top_k)
        )
    elif overflow:
        notes.append("Per-file diversity cap of {} chunk(s) per evidence file applied.".format(cap))
    return selected


def _attach_table_summaries(
    session: Session,
    selected: List[RetrievedChunk],
    project_id: int,
    scored_by_id: Dict[int, RetrievedChunk],
    notes: List[str],
) -> List[RetrievedChunk]:
    """Ensure the population summary travels with any retrieved rows of the same file.

    Without this, a model shown ten rows with ``MFA_Status=Disabled`` has no way to
    know whether those ten are out of twelve accounts or out of a thousand, and any
    exception rate it states would be invented. The summary is added on top of
    ``top_k`` rather than in place of a row, and is placed immediately after the last
    chunk from its file so the two read together in the prompt. A consequence worth
    knowing when reading a result: ranks are then no longer strictly ordered by score.
    """
    row_file_ids = {
        chunk.evidence_file_id
        for chunk in selected
        if chunk.source_type == SourceType.TABLE_ROWS.value and chunk.evidence_file_id is not None
    }
    if not row_file_ids:
        return selected

    present = {chunk.chunk_id for chunk in selected}
    stmt = (
        select(EvidenceChunk, EvidenceFile.evidence_type)
        .join(EvidenceFile, EvidenceFile.id == EvidenceChunk.evidence_file_id, isouter=True)
        .where(
            EvidenceChunk.project_id == project_id,
            EvidenceChunk.evidence_file_id.in_(list(row_file_ids)),
            EvidenceChunk.source_type == SourceType.TABLE_SUMMARY.value,
        )
        .order_by(EvidenceChunk.evidence_file_id, EvidenceChunk.chunk_index, EvidenceChunk.id)
    )

    pending: Dict[Optional[int], List[RetrievedChunk]] = defaultdict(list)
    added = 0
    for chunk, evidence_type in session.execute(stmt).all():
        if int(chunk.id) in present:
            continue
        # If the summary was scored but fell below the cut-off, keep its real scores
        # so the UI does not show a misleading 0.0 next to it.
        existing = scored_by_id.get(int(chunk.id))
        retrieved = _to_retrieved(
            _CorpusRow(chunk=chunk, evidence_type=evidence_type or ""),
            score=existing.score if existing else 0.0,
            keyword_score=existing.keyword_score if existing else 0.0,
            vector_score=existing.vector_score if existing else 0.0,
            extra={"auto_included": "table_summary"},
        )
        pending[chunk.evidence_file_id].append(retrieved)
        added += 1

    if not added:
        return selected

    last_index: Dict[Optional[int], int] = {}
    for index, chunk in enumerate(selected):
        last_index[chunk.evidence_file_id] = index

    ordered: List[RetrievedChunk] = []
    for index, chunk in enumerate(selected):
        ordered.append(chunk)
        if last_index.get(chunk.evidence_file_id) == index:
            ordered.extend(pending.pop(chunk.evidence_file_id, []))
    for remaining in pending.values():  # defensive: files whose rows vanished
        ordered.extend(remaining)

    notes.append(
        "Added {} TABLE_SUMMARY chunk(s) beyond top_k so population-level context accompanies the "
        "retrieved rows; each sits next to the rows it describes, so rank is not strictly score order.".format(added)
    )
    return ordered


def _finalise(
    session: Session,
    candidates: Sequence[RetrievedChunk],
    project_id: int,
    top_k: int,
    notes: List[str],
) -> List[RetrievedChunk]:
    """Diversity cap, then table summaries, then rank assignment."""
    scored_by_id = {chunk.chunk_id: chunk for chunk in candidates}
    selected = _apply_diversity_cap(candidates, top_k, notes)
    selected = _attach_table_summaries(session, selected, project_id, scored_by_id, notes)
    for position, chunk in enumerate(selected, start=1):
        chunk.rank = position
    return selected


# ---- retrievers -----------------------------------------------------------------


class _SessionRetriever(BaseRetriever):
    """Shared plumbing: settings resolution, corpus loading, result assembly."""

    def __init__(self, session: Session, settings: Optional[Settings] = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    def _resolved_top_k(self, top_k: int) -> int:
        return int(top_k) if top_k and int(top_k) > 0 else int(self.settings.retrieval_top_k)

    def _empty(self, queries: Sequence[str], started: float, note: str, total: int = 0) -> RetrievalResult:
        return RetrievalResult(
            chunks=[],
            queries=list(queries),
            strategy=self.name,
            total_candidates=total,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            notes=[note],
        )


class KeywordRetriever(_SessionRetriever):
    """BM25 over the project's chunks, summed across the control's queries.

    The several queries built for a control are angles on one question, so a chunk's
    keyword score is the *sum* of its BM25 scores across them. Taking the best angle
    instead was tried first and behaves badly on audit data: every query derived from a
    control describes the compliant state, so the exception rows - the whole point of
    the test - lose to compliant rows on nine angles out of ten, and their single
    strong angle is then discarded. Summing lets the exception vocabulary that appears
    in only one query still count. It is well defined because a BM25 term contributes
    zero when absent, so an angle a chunk does not answer costs it nothing.
    """

    name = RetrievalStrategy.KEYWORD.value

    def _score(
        self,
        queries: Sequence[str],
        corpus: Sequence[_CorpusRow],
        min_score: float = 0.0,
    ) -> List[_Scored]:
        if not corpus or not queries:
            return []
        documents = [tokenize(_document_for(row.chunk)) for row in corpus]
        index = _Bm25Index(documents, k1=BM25_K1, b=BM25_B)

        totals: Dict[int, float] = defaultdict(float)
        term_totals: Dict[int, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for query in queries:
            terms = tokenize(query)
            if not terms:
                continue
            scores, contributions = index.score(terms)
            for doc_index, score in scores.items():
                if score <= 0.0:
                    continue
                totals[doc_index] += score
                for term, value in contributions.get(doc_index, []):
                    term_totals[doc_index][term] += value
        if not totals:
            return []

        # Raw BM25 sums have no upper bound and scale with the number of queries, so
        # the reported score is relative to the best chunk *in this retrieval*. The
        # unscaled value is kept in extra_metadata for anyone who needs it.
        top_raw = max(totals.values()) or 1.0
        scored: List[_Scored] = []
        for doc_index, raw in totals.items():
            ranked_terms = sorted(term_totals[doc_index].items(), key=lambda item: item[1], reverse=True)
            normalised = raw / top_raw
            if normalised < min_score:
                continue
            scored.append(
                _Scored(
                    row=corpus[doc_index],
                    score=normalised,
                    raw_score=raw,
                    matched_terms=[term for term, _ in ranked_terms[:8]],
                )
            )
        scored.sort(key=lambda item: (-item.score, item.row.chunk.id))
        return scored

    def retrieve(
        self,
        queries: Sequence[str],
        project_id: int,
        top_k: int = 12,
        evidence_file_ids: Optional[Sequence[int]] = None,
        min_score: float = 0.0,
    ) -> RetrievalResult:
        started = time.perf_counter()
        cleaned = _normalise_queries(queries)
        top_k = self._resolved_top_k(top_k)
        if not cleaned:
            return self._empty(cleaned, started, "No queries supplied; nothing retrieved.")

        corpus = _load_corpus(self.session, project_id, evidence_file_ids)
        if not corpus:
            return self._empty(cleaned, started, "No evidence chunks indexed for this project.")

        scored = self._score(cleaned, corpus, min_score=min_score)
        notes: List[str] = [
            "BM25 (k1={}, b={}) over {} chunks; {} matched at least one query term.".format(
                BM25_K1, BM25_B, len(corpus), len(scored)
            )
        ]
        candidates = [
            _to_retrieved(
                item.row,
                score=item.score,
                keyword_score=item.score,
                extra={"bm25_score": round(item.raw_score, 4), "matched_terms": item.matched_terms},
            )
            for item in scored
        ]
        chunks = _finalise(self.session, candidates, project_id, top_k, notes)
        return RetrievalResult(
            chunks=chunks,
            queries=cleaned,
            strategy=self.name,
            total_candidates=len(corpus),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            notes=notes,
        )


class VectorRetriever(_SessionRetriever):
    """Cosine similarity between the query vectors and the stored chunk vectors.

    A chunk is scored by its *best* matching query, unlike the keyword retriever which
    sums. Cosine similarities are not additive evidence - a sum of ten of them is not a
    similarity to anything - so the honest summary of a chunk's standing across several
    angles is the closest angle. The consequence is that the vector half is worse at
    isolating exception rows than the lexical half; hybrid fusion is what recovers it.
    """

    name = RetrievalStrategy.VECTOR.value

    def __init__(
        self,
        session: Session,
        embedding_provider: Optional[EmbeddingProvider] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        super().__init__(session, settings=settings)
        self.provider = embedding_provider or get_embedding_provider(settings=self.settings)

    def _score(
        self,
        queries: Sequence[str],
        corpus: Sequence[_CorpusRow],
        min_score: float = 0.0,
        notes: Optional[List[str]] = None,
    ) -> List[_Scored]:
        if not corpus or not queries:
            return []
        dimension = int(self.provider.dimension)
        usable: List[_CorpusRow] = []
        vectors: List[np.ndarray] = []
        skipped_missing = 0
        skipped_dim = 0
        for row in corpus:
            chunk = row.chunk
            if not chunk.embedding:
                skipped_missing += 1
                continue
            if int(chunk.embedding_dim or 0) != dimension:
                skipped_dim += 1
                continue
            try:
                vectors.append(decode_vector(chunk.embedding, dimension))
            except EmbeddingError:  # pragma: no cover - guarded by the dim check above
                skipped_dim += 1
                continue
            usable.append(row)

        if notes is not None:
            if skipped_missing:
                notes.append(
                    "{} chunk(s) skipped: no stored embedding (run the indexer).".format(skipped_missing)
                )
            if skipped_dim:
                notes.append(
                    "{} chunk(s) skipped: embedding dimension differs from the active provider "
                    "({}); re-index the project.".format(skipped_dim, dimension)
                )
        if not usable:
            return []

        matrix = l2_normalise(np.vstack(vectors))
        query_matrix = l2_normalise(np.asarray(self.provider.embed(list(queries)), dtype=np.float32))
        # (chunks x dim) @ (dim x queries) -> every chunk against every query at once.
        similarities = matrix @ query_matrix.T
        best = similarities.max(axis=1) if similarities.size else np.zeros(len(usable), dtype=np.float32)

        scored: List[_Scored] = []
        for position, row in enumerate(usable):
            score = float(max(0.0, float(best[position])))
            if score <= 0.0 or score < min_score:
                continue
            scored.append(_Scored(row=row, score=score, raw_score=score))
        scored.sort(key=lambda item: (-item.score, item.row.chunk.id))
        return scored

    def retrieve(
        self,
        queries: Sequence[str],
        project_id: int,
        top_k: int = 12,
        evidence_file_ids: Optional[Sequence[int]] = None,
        min_score: float = 0.0,
    ) -> RetrievalResult:
        started = time.perf_counter()
        cleaned = _normalise_queries(queries)
        top_k = self._resolved_top_k(top_k)
        if not cleaned:
            return self._empty(cleaned, started, "No queries supplied; nothing retrieved.")

        corpus = _load_corpus(self.session, project_id, evidence_file_ids)
        if not corpus:
            return self._empty(cleaned, started, "No evidence chunks indexed for this project.")

        notes: List[str] = []
        scored = self._score(cleaned, corpus, min_score=min_score, notes=notes)
        notes.insert(
            0,
            "Cosine similarity over {} chunks using {} ({} dimensions).".format(
                len(corpus), getattr(self.provider, "model_id", self.provider.name), self.provider.dimension
            ),
        )
        candidates = [
            _to_retrieved(item.row, score=item.score, vector_score=item.score) for item in scored
        ]
        chunks = _finalise(self.session, candidates, project_id, top_k, notes)
        return RetrievalResult(
            chunks=chunks,
            queries=cleaned,
            strategy=self.name,
            total_candidates=len(corpus),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            notes=notes,
        )


class HybridRetriever(_SessionRetriever):
    """Reciprocal-rank fusion of the keyword and vector rankings.

    RRF is used rather than a weighted score sum because BM25 and cosine live on
    incomparable scales; fusing *positions* needs no calibration constant that would
    have to be justified without tuning data. A chunk found by both retrievers rises
    above one found emphatically by only one, which is the behaviour an auditor wants:
    agreement between an exact-term match and a semantic match is the strongest signal
    available offline.
    """

    name = RetrievalStrategy.HYBRID.value

    def __init__(
        self,
        session: Session,
        settings: Optional[Settings] = None,
        embedding_provider: Optional[EmbeddingProvider] = None,
        rrf_k: int = RRF_K,
    ) -> None:
        super().__init__(session, settings=settings)
        self.rrf_k = int(rrf_k)
        self.keyword = KeywordRetriever(session, settings=self.settings)
        self.vector = VectorRetriever(session, embedding_provider=embedding_provider, settings=self.settings)

    def retrieve(
        self,
        queries: Sequence[str],
        project_id: int,
        top_k: int = 12,
        evidence_file_ids: Optional[Sequence[int]] = None,
        min_score: float = 0.0,
    ) -> RetrievalResult:
        started = time.perf_counter()
        cleaned = _normalise_queries(queries)
        top_k = self._resolved_top_k(top_k)
        if not cleaned:
            return self._empty(cleaned, started, "No queries supplied; nothing retrieved.")

        # One corpus load shared by both halves - the sub-retrievers score in memory.
        corpus = _load_corpus(self.session, project_id, evidence_file_ids)
        if not corpus:
            return self._empty(cleaned, started, "No evidence chunks indexed for this project.")

        candidate_k = max(int(self.settings.retrieval_candidate_k or 0), top_k)
        notes: List[str] = []
        # min_score is applied to the component scores, before fusion: a threshold on
        # the fused reciprocal-rank value (max 2/(k+1)) would mean nothing to a caller.
        keyword_scored = self.keyword._score(cleaned, corpus, min_score=min_score)[:candidate_k]
        vector_scored = self.vector._score(cleaned, corpus, min_score=min_score, notes=notes)[:candidate_k]

        fused: Dict[int, float] = defaultdict(float)
        rows: Dict[int, _CorpusRow] = {}
        keyword_scores: Dict[int, float] = {}
        vector_scores: Dict[int, float] = {}
        extras: Dict[int, Dict[str, Any]] = defaultdict(dict)

        for rank, item in enumerate(keyword_scored, start=1):
            chunk_id = int(item.row.chunk.id)
            rows[chunk_id] = item.row
            fused[chunk_id] += 1.0 / (self.rrf_k + rank)
            keyword_scores[chunk_id] = item.score
            extras[chunk_id].update(
                {
                    "bm25_score": round(item.raw_score, 4),
                    "matched_terms": item.matched_terms,
                    "keyword_rank": rank,
                }
            )
        for rank, item in enumerate(vector_scored, start=1):
            chunk_id = int(item.row.chunk.id)
            rows[chunk_id] = item.row
            fused[chunk_id] += 1.0 / (self.rrf_k + rank)
            vector_scores[chunk_id] = item.score
            extras[chunk_id]["vector_rank"] = rank

        candidates = [
            _to_retrieved(
                rows[chunk_id],
                score=score,
                keyword_score=keyword_scores.get(chunk_id, 0.0),
                vector_score=vector_scores.get(chunk_id, 0.0),
                extra=extras.get(chunk_id),
            )
            for chunk_id, score in fused.items()
        ]
        # Ties are broken by the component scores, then by chunk id, so the ordering is
        # reproducible run to run.
        candidates.sort(
            key=lambda chunk: (
                -chunk.score,
                -(chunk.keyword_score + chunk.vector_score),
                chunk.chunk_id,
            )
        )

        notes.insert(
            0,
            "Reciprocal-rank fusion (k={}) of {} keyword and {} vector candidates over {} chunks.".format(
                self.rrf_k, len(keyword_scored), len(vector_scored), len(corpus)
            ),
        )
        both = len(set(keyword_scores) & set(vector_scores))
        notes.append("{} chunk(s) were ranked by both retrievers.".format(both))

        chunks = _finalise(self.session, candidates, project_id, top_k, notes)
        return RetrievalResult(
            chunks=chunks,
            queries=cleaned,
            strategy=self.name,
            total_candidates=len(corpus),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            notes=notes,
        )


def get_retriever(
    session: Session,
    strategy: Optional[Any] = None,
    settings: Optional[Settings] = None,
    embedding_provider: Optional[EmbeddingProvider] = None,
) -> BaseRetriever:
    """Resolve the configured retrieval strategy into a retriever.

    ``strategy`` accepts a :class:`RetrievalStrategy`, a string in any case, or None
    (meaning "whatever ``settings.retrieval_strategy`` says"). An unrecognised value
    falls back to hybrid with a warning rather than raising: a typo in a ``.env`` file
    should degrade the ranking, not take the application down.
    """
    resolved_settings = settings or get_settings()
    raw = strategy if strategy is not None else resolved_settings.retrieval_strategy
    chosen = RetrievalStrategy.coerce(raw, default=None)
    if chosen is None:
        logger.warning("Unknown retrieval strategy %r; falling back to HYBRID.", raw)
        chosen = RetrievalStrategy.HYBRID

    if chosen is RetrievalStrategy.KEYWORD:
        return KeywordRetriever(session, settings=resolved_settings)
    if chosen is RetrievalStrategy.VECTOR:
        return VectorRetriever(session, embedding_provider=embedding_provider, settings=resolved_settings)
    return HybridRetriever(session, settings=resolved_settings, embedding_provider=embedding_provider)


__all__ = [
    "BM25_B",
    "BM25_K1",
    "HybridRetriever",
    "KeywordRetriever",
    "RRF_K",
    "VectorRetriever",
    "get_retriever",
]
