"""Retrieval: query construction, the three strategies, embeddings and indexing.

The property that matters to the research is not ranking quality - with two files and a
dozen chunks there is nothing to rank - but that retrieval never returns text that
cannot say where it came from, and that it does not let one large export crowd out the
policy document that states the requirement.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.database.models import EvidenceChunk
from app.rag.base import BaseRetriever, RetrievalResult
from app.rag.embeddings import (
    LocalHashingEmbedding,
    decode_vector,
    encode_vector,
    get_embedding_provider,
    l2_normalise,
    tokenize,
)
from app.rag.indexer import index_evidence_file, reindex_project
from app.rag.query_builder import MAX_QUERIES, build_queries, build_query_text
from app.rag.retriever import HybridRetriever, KeywordRetriever, VectorRetriever, get_retriever
from app.schemas.enums import SourceType

RETRIEVER_CLASSES = [KeywordRetriever, VectorRetriever, HybridRetriever]


# ------------------------------------------------------------------ query building
def test_build_queries_from_an_orm_control(control):
    queries = build_queries(control)
    assert 1 <= len(queries) <= MAX_QUERIES
    assert len(queries) == len(set(queries))
    blob = " ".join(queries).lower()
    assert "multi-factor" in blob
    assert "mfa_status" in blob


def test_build_queries_accepts_a_plain_dict():
    queries = build_queries(
        {
            "control_id": "CONTROL-999",
            "name": "Backup restoration testing",
            "objective": "Prove that backups can actually be restored.",
            "assessment_criteria": ["A restore test was performed in the period."],
            "retrieval_keywords": ["restore", "backup"],
        }
    )
    assert queries
    assert any("restore" in q.lower() for q in queries)


@pytest.mark.parametrize("control", [None, {}, {"control_id": "X"}])
def test_build_queries_degrades_rather_than_raising(control):
    assert isinstance(build_queries(control), list)


def test_build_query_text_is_a_single_string(control):
    text = build_query_text(control)
    assert isinstance(text, str) and text.strip()


# --------------------------------------------------------------------- retrievers
@pytest.mark.parametrize("retriever_cls", RETRIEVER_CLASSES, ids=lambda c: c.__name__)
def test_every_retrieved_chunk_carries_a_full_locator(seeded_session, ingested_project, control, retriever_cls):
    """Retrieval never returns naked text: an uncitable fragment is useless."""
    retriever = retriever_cls(seeded_session)
    assert isinstance(retriever, BaseRetriever)
    result = retriever.retrieve(build_queries(control), ingested_project.id, top_k=8)

    assert isinstance(result, RetrievalResult)
    assert result.chunks
    assert result.strategy
    for chunk in result.chunks:
        assert chunk.chunk_id
        assert chunk.text.strip()
        assert chunk.locator is not None
        assert chunk.locator.filename
        assert chunk.locator.render().startswith(chunk.locator.filename)
        assert chunk.evidence_file_id is not None
        assert chunk.rank >= 1


@pytest.mark.parametrize("retriever_cls", RETRIEVER_CLASSES, ids=lambda c: c.__name__)
def test_results_are_deduplicated_and_ranked_contiguously(seeded_session, ingested_project, control, retriever_cls):
    result = retriever_cls(seeded_session).retrieve(build_queries(control), ingested_project.id, top_k=8)
    ids = result.chunk_ids
    assert len(ids) == len(set(ids))
    assert [c.rank for c in result.chunks] == list(range(1, len(result.chunks) + 1))
    assert result.by_id(ids[0]) is not None
    assert result.by_id(-1) is None


@pytest.mark.parametrize("retriever_cls", RETRIEVER_CLASSES, ids=lambda c: c.__name__)
def test_top_k_is_respected(seeded_session, ingested_project, control, retriever_cls):
    result = retriever_cls(seeded_session).retrieve(build_queries(control), ingested_project.id, top_k=3)
    assert len(result.chunks) <= 3


def test_keyword_and_vector_scores_are_recorded_separately(seeded_session, ingested_project, control):
    result = HybridRetriever(seeded_session).retrieve(build_queries(control), ingested_project.id, top_k=6)
    assert any(c.keyword_score > 0 for c in result.chunks)
    assert any(c.vector_score > 0 for c in result.chunks)


def test_diversity_cap_stops_one_file_crowding_out_the_policy(seeded_session, ingested_project, control):
    """Cap is ``max(2, top_k // 2)`` per evidence file, so a large export cannot fill
    the whole budget and hide the document that states the requirement."""
    top_k = 6
    result = HybridRetriever(seeded_session).retrieve(
        build_queries(control), ingested_project.id, top_k=top_k
    )
    per_file = {}
    for chunk in result.chunks:
        per_file[chunk.filename] = per_file.get(chunk.filename, 0) + 1
    assert max(per_file.values()) <= max(2, top_k // 2)
    assert len(per_file) > 1


def test_retrieval_surfaces_the_population_summary_for_a_listing(seeded_session, ingested_project, control):
    """The TABLE_SUMMARY chunk is what lets the model reason over the whole population."""
    result = HybridRetriever(seeded_session).retrieve(
        build_queries(control), ingested_project.id, top_k=12
    )
    summaries = [c for c in result.chunks if c.source_type == SourceType.TABLE_SUMMARY.value]
    assert summaries
    assert any("MFA_Status" in c.text for c in summaries)


@pytest.mark.parametrize("retriever_cls", RETRIEVER_CLASSES, ids=lambda c: c.__name__)
def test_unknown_project_returns_nothing_rather_than_raising(seeded_session, retriever_cls):
    result = retriever_cls(seeded_session).retrieve(["mfa"], 987654, top_k=5)
    assert result.chunks == []


@pytest.mark.parametrize("retriever_cls", RETRIEVER_CLASSES, ids=lambda c: c.__name__)
def test_no_queries_returns_nothing_and_says_why(seeded_session, ingested_project, retriever_cls):
    result = retriever_cls(seeded_session).retrieve([], ingested_project.id, top_k=5)
    assert result.chunks == []
    assert result.notes


def test_evidence_file_filter_restricts_the_search(seeded_session, ingested_project, control):
    from app.audit import service

    files = service.list_evidence(seeded_session, project_id=ingested_project.id)
    target = [f for f in files if f.filename.endswith(".csv")][0]
    result = HybridRetriever(seeded_session).retrieve(
        build_queries(control), ingested_project.id, top_k=8, evidence_file_ids=[target.id]
    )
    assert result.chunks
    assert {c.evidence_file_id for c in result.chunks} == {target.id}


@pytest.mark.parametrize(
    "strategy,expected",
    [
        (None, HybridRetriever),
        ("KEYWORD", KeywordRetriever),
        ("keyword", KeywordRetriever),
        ("VECTOR", VectorRetriever),
        ("HYBRID", HybridRetriever),
        ("nonsense-strategy", HybridRetriever),
    ],
)
def test_get_retriever_resolves_configuration_and_falls_back_loudly(seeded_session, strategy, expected):
    assert isinstance(get_retriever(seeded_session, strategy), expected)


def test_retrieval_result_serialises_for_the_research_record(seeded_session, ingested_project, control):
    result = HybridRetriever(seeded_session).retrieve(build_queries(control), ingested_project.id, top_k=4)
    payload = result.to_dict()
    assert payload["strategy"]
    assert len(payload["chunks"]) == len(result.chunks)
    assert payload["chunks"][0]["locator"]["rendered"]
    assert len(result) == len(result.chunks)


# --------------------------------------------------------------------- embeddings
def test_local_embedding_is_deterministic_and_normalised():
    provider = LocalHashingEmbedding()
    first, second = provider.embed(["MFA_Status = Disabled", "MFA_Status = Disabled"])
    assert first == second
    assert len(first) == provider.dimension
    assert np.isclose(np.linalg.norm(np.asarray(first)), 1.0)


def test_local_embedding_has_no_negative_components():
    """``alternate_sign=False`` keeps cosine similarity readable in [0, 1]."""
    vector = LocalHashingEmbedding().embed_one("privileged accounts multi-factor authentication")
    assert min(vector) >= 0.0


def test_two_processes_would_agree_because_there_is_no_fitted_vocabulary():
    a = LocalHashingEmbedding().embed_one("password policy")
    b = LocalHashingEmbedding().embed_one("password policy")
    assert a == b


def test_embedding_of_nothing_is_nothing():
    assert LocalHashingEmbedding().embed([]) == []


def test_vector_round_trips_through_the_database_representation():
    vector = LocalHashingEmbedding().embed_one("minimum password length 14")
    blob = encode_vector(vector)
    restored = decode_vector(blob, len(vector))
    assert isinstance(blob, bytes)
    assert restored.dtype == np.float32
    assert np.allclose(np.asarray(vector, dtype=np.float32), restored)


def test_l2_normalise_leaves_a_zero_row_alone():
    matrix = np.array([[3.0, 4.0], [0.0, 0.0]], dtype=np.float32)
    normalised = l2_normalise(matrix)
    assert np.allclose(normalised[0], [0.6, 0.8])
    assert np.allclose(normalised[1], [0.0, 0.0])


def test_tokenizer_splits_audit_style_field_names():
    tokens = tokenize("MFA_Status = Disabled")
    assert "mfa_status" in tokens
    assert "mfa" in tokens and "status" in tokens
    assert "disabled" in tokens


def test_embedding_provider_falls_back_to_local_when_the_remote_is_unconfigured():
    """No key and no base URL: the offline provider is substituted, not an error."""
    assert isinstance(get_embedding_provider(), LocalHashingEmbedding)
    assert isinstance(get_embedding_provider("openai"), LocalHashingEmbedding)
    assert isinstance(get_embedding_provider("something-unknown"), LocalHashingEmbedding)


# ------------------------------------------------------------------------ indexing
def test_ingestion_leaves_every_chunk_embedded(seeded_session, ingested_project, settings):
    chunks = (
        seeded_session.query(EvidenceChunk)
        .filter(EvidenceChunk.project_id == ingested_project.id)
        .all()
    )
    assert chunks
    for chunk in chunks:
        assert chunk.embedding is not None
        assert chunk.embedding_dim == settings.embedding_dim
        assert chunk.embedding_model


def test_indexing_an_already_current_file_costs_nothing(seeded_session, ingested_project):
    from app.audit import service

    files = service.list_evidence(seeded_session, project_id=ingested_project.id)
    assert index_evidence_file(seeded_session, files[0].id) == 0


def test_reindex_project_is_unconditional_by_design(seeded_session, ingested_project):
    """It is the "I changed the embedding provider" operation, so it must not honour an
    'already indexed' check: a mixture of incomparable vectors would be worse."""
    total = (
        seeded_session.query(EvidenceChunk)
        .filter(EvidenceChunk.project_id == ingested_project.id)
        .count()
    )
    assert reindex_project(seeded_session, ingested_project.id) == total


def test_reindex_restores_a_cleared_vector(seeded_session, ingested_project):
    chunk = (
        seeded_session.query(EvidenceChunk)
        .filter(EvidenceChunk.project_id == ingested_project.id)
        .first()
    )
    chunk.embedding = None
    chunk.embedding_dim = 0
    seeded_session.commit()
    assert reindex_project(seeded_session, ingested_project.id) >= 1
    seeded_session.refresh(chunk)
    assert chunk.embedding is not None
