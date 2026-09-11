"""Turning evidence text into vectors, reproducibly and offline.

Two properties drive every decision here.

*Reproducibility.* A chunk embedded during ingestion must stay comparable to a query
embedded weeks later in a different process. A vectoriser that learns a vocabulary
(TF-IDF, word2vec) breaks that the moment the corpus changes: yesterday's stored
vectors silently stop meaning what today's query vector means, and nothing in the
system would report the drift. :class:`LocalHashingEmbedding` therefore uses scikit-
learn's ``HashingVectorizer``, which is *stateless* - the mapping from token to
dimension is a fixed hash function (MurmurHash3, seeded inside scikit-learn, not by
``PYTHONHASHSEED``), so there is no fitted state to persist and nothing to keep in
sync. The price is that dimensions are not interpretable and rare collisions occur;
for a prototype whose vector retriever is one half of a hybrid, that is a fair trade.

*Offline operation.* The default provider makes no network call at all, so the whole
application runs with ``LLM_PROVIDER=mock`` on a disconnected machine. ``OpenAIEmbedding``
exists so the same pipeline can be pointed at a real embedding endpoint, and
:func:`get_embedding_provider` falls back to the local provider - loudly - rather than
letting an unconfigured remote provider fail deep inside an ingestion run.

This module also owns the two text decisions the lexical and vector retrievers must
agree on: what counts as a *token* (:func:`tokenize`) and what counts as the
*document* for a chunk (:func:`chunk_document_text`). They live here, next to the
embedding analyser, so the two halves of hybrid retrieval cannot drift apart.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from app.config import Settings, get_settings
from app.rag.base import EmbeddingProvider

logger = logging.getLogger(__name__)

#: Stored vectors are float32: half the bytes of float64 in the ``LargeBinary``
#: column, and well beyond the precision a cosine ranking can make use of.
VECTOR_DTYPE = np.float32


class EmbeddingError(RuntimeError):
    """Raised when an embedding provider cannot produce usable vectors."""


# ---- tokenisation ---------------------------------------------------------------

#: A "word" for our purposes: alphanumerics and underscores, optionally joined by a
#: hyphen or dot so identifiers survive intact - ``svc-account-014``, ``CHG-1042``,
#: ``2024-01-15``, ``4.2`` are single tokens as well as being split below.
_RAW_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+(?:[-.][A-Za-z0-9_]+)*")

#: Splits a compound identifier into its parts: ``MFAStatus`` -> ``MFA``, ``Status``;
#: ``account014`` -> ``account``, ``014``.
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z][a-z]+|[a-z]+|[A-Z]+|\d+")

_SPLIT_RE = re.compile(r"[-._]+")


def sub_tokens(raw: str) -> List[str]:
    """Break an identifier-like string into its lower-cased constituent words."""
    parts: List[str] = []
    for segment in _SPLIT_RE.split(raw):
        if not segment:
            continue
        for piece in _CAMEL_RE.findall(segment):
            parts.append(piece.lower())
    return parts


def _keep_token(token: str) -> bool:
    # Single letters are noise; single digits are not - "minimum length of 8" is
    # exactly the kind of configuration value an auditor is looking for.
    return len(token) >= 2 or token.isdigit()


def tokenize(text: str) -> List[str]:
    """Lower-cased tokens, with compound identifiers indexed whole *and* in parts.

    Audit evidence is dense with machine-generated column headers and identifiers:
    ``MFA_Status``, ``LastPatchDate``, ``svc-account-014``. An auditor asking about
    "MFA" must match a CSV whose only mention of it is a column header, so every
    identifier contributes both its full form and its sub-tokens
    (``MFA_Status`` -> ``mfa_status``, ``mfa``, ``status``). Emitting both is a
    deliberate recall-over-precision choice; BM25's IDF then discounts whichever of
    the two forms turns out to be ubiquitous in the corpus.
    """
    if not text:
        return []
    tokens: List[str] = []
    for match in _RAW_TOKEN_RE.finditer(text):
        raw = match.group(0)
        forms: List[str] = [raw.lower()]
        for piece in sub_tokens(raw):
            if piece not in forms:
                forms.append(piece)
        tokens.extend(form for form in forms if _keep_token(form))
    return tokens


def chunk_document_text(
    text: str,
    filename: str = "",
    sheet_name: Optional[str] = None,
    section: Optional[str] = None,
    column_names: Optional[Sequence[str]] = None,
) -> str:
    """The text actually indexed for a chunk: its content plus its locator terms.

    Auditors search for artefacts as often as for contents ("the privileged user
    listing", "the password standard"), and for tabular evidence the column headers
    are frequently the only place a control keyword appears at all. Appending
    filename, sheet, section heading and column names makes those searchable without
    altering the chunk text that is quoted back in a citation. Both the BM25 index
    and the stored embedding are built from this same string, so the lexical and
    vector halves of hybrid retrieval see the same document.
    """
    parts: List[str] = [text or ""]
    if filename:
        parts.append(filename)
    if sheet_name:
        parts.append(sheet_name)
    if section:
        parts.append(section)
    if column_names:
        parts.append(" ".join(str(name) for name in column_names))
    return "\n".join(part for part in parts if part)


# ---- vector (de)serialisation ---------------------------------------------------


def encode_vector(vector: Sequence[float]) -> bytes:
    """Pack a vector into the bytes stored in ``EvidenceChunk.embedding``."""
    array = np.asarray(vector, dtype=VECTOR_DTYPE).ravel()
    return array.tobytes()


def decode_vector(blob: bytes, dim: int = 0) -> np.ndarray:
    """Unpack stored bytes back into a float32 vector.

    ``dim`` is checked rather than trusted: a stored vector whose length does not
    match what the current provider produces is a configuration change (a different
    ``embedding_dim`` or a different provider) that would otherwise surface as
    nonsense similarity scores. Callers that hold mixed-dimension corpora should
    filter on ``EvidenceChunk.embedding_dim`` before calling this.
    """
    if not blob:
        raise EmbeddingError("Cannot decode an empty embedding blob")
    array = np.frombuffer(blob, dtype=VECTOR_DTYPE)
    if dim and array.size != dim:
        raise EmbeddingError(
            "Stored embedding has {} dimensions but {} were expected; "
            "re-index the project after changing embedding settings".format(array.size, dim)
        )
    # frombuffer aliases read-only memory; hand back something callers may modify.
    return np.array(array, dtype=VECTOR_DTYPE, copy=True)


def l2_normalise(matrix: np.ndarray) -> np.ndarray:
    """Row-wise L2 normalisation that leaves all-zero rows alone (no NaNs)."""
    array = np.asarray(matrix, dtype=VECTOR_DTYPE)
    if array.ndim == 1:
        array = array.reshape(1, -1)
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return (array / norms).astype(VECTOR_DTYPE, copy=False)


# ---- providers ------------------------------------------------------------------


class LocalHashingEmbedding(EmbeddingProvider):
    """Stateless hashing vectoriser: deterministic, offline, nothing to persist.

    Because there is no fitted vocabulary, two processes started months apart produce
    identical vectors for identical text. That is the only property that makes storing
    embeddings in the database safe.
    """

    name = "local-hashing"
    #: Recorded in ``EvidenceChunk.embedding_model`` so a re-index can tell which
    #: provider produced a stored vector.
    model_id = "hashing-vectorizer"

    def __init__(self, dimension: Optional[int] = None, settings: Optional[Settings] = None) -> None:
        resolved = settings or get_settings()
        self.dimension = int(dimension or resolved.embedding_dim)
        if self.dimension <= 0:
            raise EmbeddingError("embedding_dim must be a positive integer")
        self._vectorizer = None  # built lazily: importing scikit-learn is not cheap

    def _build(self):
        # Imported inside the method so that `import app.rag.embeddings` stays fast
        # for callers that only need the tokeniser or the encode/decode helpers.
        from sklearn.feature_extraction.text import HashingVectorizer

        return HashingVectorizer(
            n_features=self.dimension,
            analyzer=tokenize,
            lowercase=False,  # `tokenize` already lower-cases; avoids double work
            # alternate_sign=False keeps every component non-negative, so cosine
            # similarity stays in [0, 1] and is directly readable in the UI.
            alternate_sign=False,
            norm="l2",
            dtype=VECTOR_DTYPE,
        )

    @property
    def vectorizer(self):
        if self._vectorizer is None:
            self._vectorizer = self._build()
        return self._vectorizer

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        items = [text if isinstance(text, str) else "" for text in texts]
        if not items:
            return []
        matrix = self.vectorizer.transform(items)
        dense = np.asarray(matrix.toarray(), dtype=VECTOR_DTYPE)
        return [row.tolist() for row in dense]

    def is_available(self) -> bool:
        try:  # pragma: no cover - scikit-learn is a hard dependency of the project
            import sklearn  # noqa: F401
        except ImportError:
            return False
        return True

    def health(self) -> Dict[str, Any]:
        return {
            "provider": self.name,
            "model": self.model_id,
            "dimension": self.dimension,
            "available": self.is_available(),
            "stateless": True,
            "network": False,
        }


class OpenAIEmbedding(EmbeddingProvider):
    """Embeddings from any OpenAI-compatible endpoint.

    Availability is decided from configuration alone - never by probing the network -
    so nothing in this class can block application start-up or a test run.
    """

    name = "openai"

    def __init__(
        self,
        model: str = "",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        dimension: Optional[int] = None,
        timeout: Optional[int] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        resolved = settings or get_settings()
        self.model_id = model or resolved.embedding_model
        # Fall back to the LLM credentials: a single OpenAI-compatible gateway
        # usually serves both endpoints, and duplicating the key in .env is friction
        # that leads to keys being pasted into code.
        self.api_key = api_key or resolved.embedding_api_key or resolved.llm_api_key
        self.base_url = base_url or resolved.embedding_base_url or resolved.llm_base_url
        self.dimension = int(dimension or resolved.embedding_dim)
        self.timeout = int(timeout or resolved.llm_timeout)
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover - openai is installed here
                raise EmbeddingError("The `openai` package is required for OpenAIEmbedding") from exc
            kwargs: Dict[str, Any] = {"timeout": self.timeout}
            if self.api_key:
                kwargs["api_key"] = self.api_key
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = OpenAI(**kwargs)
        return self._client

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        items = [text if isinstance(text, str) else "" for text in texts]
        if not items:
            return []
        if not self.is_available():
            raise EmbeddingError("OpenAI embeddings are selected but no API key or base URL is configured")
        kwargs: Dict[str, Any] = {"model": self.model_id, "input": items}
        # Only the v3 models accept a requested dimensionality; asking an older model
        # for one is an immediate 400.
        if self.model_id.startswith("text-embedding-3"):
            kwargs["dimensions"] = self.dimension
        try:
            response = self._get_client().embeddings.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - any SDK/transport failure
            raise EmbeddingError("OpenAI embedding request failed: {}".format(exc)) from exc
        ordered = sorted(response.data, key=lambda item: getattr(item, "index", 0))
        vectors = [list(item.embedding) for item in ordered]
        if vectors and len(vectors[0]) != self.dimension:
            raise EmbeddingError(
                "{} returned {}-dimensional vectors but embedding_dim is {}".format(
                    self.model_id, len(vectors[0]), self.dimension
                )
            )
        # Normalise defensively: cosine ranking downstream assumes unit vectors.
        return [row.tolist() for row in l2_normalise(np.asarray(vectors, dtype=VECTOR_DTYPE))]

    def is_available(self) -> bool:
        return bool(self.api_key or self.base_url)

    def health(self) -> Dict[str, Any]:
        return {
            "provider": self.name,
            "model": self.model_id,
            "dimension": self.dimension,
            "available": self.is_available(),
            "base_url": self.base_url or "(provider default)",
            "network": True,
        }


#: Providers are stateless and cheap to reuse; the cache exists so that repeated
#: retrievals do not rebuild the scikit-learn analyser on every call. Keyed by the
#: configuration that produced them, so `reload_settings()` yields a fresh provider.
_PROVIDER_CACHE: Dict[Tuple[str, str, int], EmbeddingProvider] = {}


def get_embedding_provider(
    name: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> EmbeddingProvider:
    """Resolve the configured embedding provider, degrading to local on any doubt.

    A remote provider that is selected but unconfigured is a misconfiguration, not a
    reason to fail an ingestion run: the local provider is substituted and the
    substitution is logged, because silently producing *different* vectors would be
    far worse than producing weaker ones.
    """
    resolved = settings or get_settings()
    provider_name = (name or resolved.embedding_provider or "local").strip().lower()
    key = (provider_name, resolved.embedding_model, int(resolved.embedding_dim))
    cached = _PROVIDER_CACHE.get(key)
    if cached is not None:
        return cached

    provider: EmbeddingProvider
    if provider_name in ("openai", "remote", "api"):
        candidate = OpenAIEmbedding(settings=resolved)
        if candidate.is_available():
            provider = candidate
        else:
            logger.warning(
                "Embedding provider '%s' is selected but has no API key or base URL; "
                "falling back to the local hashing embedding.",
                provider_name,
            )
            provider = LocalHashingEmbedding(settings=resolved)
    else:
        if provider_name not in ("local", "hashing", "local-hashing"):
            logger.warning(
                "Unknown embedding provider '%s'; using the local hashing embedding.", provider_name
            )
        provider = LocalHashingEmbedding(settings=resolved)

    _PROVIDER_CACHE[key] = provider
    return provider


def reset_provider_cache() -> None:
    """Drop cached providers (used after a settings reload or in tests)."""
    _PROVIDER_CACHE.clear()


__all__ = [
    "EmbeddingError",
    "LocalHashingEmbedding",
    "OpenAIEmbedding",
    "VECTOR_DTYPE",
    "chunk_document_text",
    "decode_vector",
    "encode_vector",
    "get_embedding_provider",
    "l2_normalise",
    "reset_provider_cache",
    "sub_tokens",
    "tokenize",
]
