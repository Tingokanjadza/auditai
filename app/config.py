"""Environment-driven configuration.

Every externally-supplied secret (API keys, base URLs, model names) is read from the
environment or a local ``.env`` file. Nothing sensitive is ever hard-coded here, and
``Settings.redacted_dict`` is the only representation that should be rendered in the UI
or written into a report.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Repository root (…/llm-it-auditor). Resolved from this file so the app works
#: regardless of the current working directory.
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Application settings. Override any field via environment variable or .env."""

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        protected_namespaces=("settings_",),
    )

    # ------------------------------------------------------------------ general
    app_name: str = "LLM-Assisted IT Audit Risk and Control Assessment System"
    app_short_name: str = "LLM IT Auditor"
    app_version: str = "0.1.0"
    environment: str = "development"
    debug: bool = False

    # ----------------------------------------------------------------- storage
    data_dir: Path = BASE_DIR / "data"
    upload_dir: Path = BASE_DIR / "data" / "uploads"
    report_dir: Path = BASE_DIR / "data" / "reports"
    synthetic_dir: Path = BASE_DIR / "data" / "synthetic"
    controls_dir: Path = BASE_DIR / "data" / "controls"
    sample_evidence_dir: Path = BASE_DIR / "data" / "sample_evidence"

    #: SQLAlchemy URL. Swap for e.g.
    #: postgresql+psycopg://user:pass@host:5432/itaudit to move to PostgreSQL.
    database_url: str = Field(default="sqlite:///" + str(BASE_DIR / "data" / "audit.db"))
    db_echo: bool = False

    max_upload_mb: int = 50

    # --------------------------------------------------------------------- API
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_base_url: str = "http://127.0.0.1:8000"
    api_request_timeout: int = 300

    # ---------------------------------------------------------------- frontend
    streamlit_port: int = 8501

    # --------------------------------------------------------------------- LLM
    #: "mock" (offline, deterministic) or "openai" (any OpenAI-compatible endpoint:
    #: OpenAI, Azure-compatible gateways, Ollama, vLLM, LM Studio, OpenRouter, …).
    llm_provider: str = "mock"
    llm_model: str = "gpt-4o-mini"
    llm_api_key: Optional[str] = None
    #: Point this at any OpenAI-compatible server, e.g. http://localhost:11434/v1
    llm_base_url: Optional[str] = None
    llm_temperature: float = 0.0
    llm_max_tokens: int = 2500
    llm_timeout: int = 120
    llm_max_retries: int = 2
    #: Ask the provider for a JSON-schema-constrained response when it supports it.
    llm_use_json_mode: bool = True

    #: Research lever: fraction of mock assessments that deliberately fabricate a
    #: citation or an unsupported claim, so the validation layer can be exercised
    #: and measured offline. 0.0 disables. Never enable outside experiments.
    mock_hallucination_rate: float = 0.0
    mock_seed: int = 1337

    # -------------------------------------------------------------- embeddings
    #: "local" (stateless hashing vectoriser, no network, deterministic) or "openai".
    embedding_provider: str = "local"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1024
    embedding_api_key: Optional[str] = None
    embedding_base_url: Optional[str] = None

    # --------------------------------------------------------------------- RAG
    retrieval_strategy: str = "HYBRID"
    retrieval_top_k: int = 12
    retrieval_candidate_k: int = 60
    retrieval_min_score: float = 0.0
    chunk_size: int = 1200
    chunk_overlap: int = 180
    table_rows_per_chunk: int = 25
    #: Hard ceiling on characters of evidence handed to the model in one call.
    max_evidence_chars: int = 24000
    #: Experiment A deliberately gets raw, untargeted evidence up to this budget.
    raw_evidence_char_budget: int = 24000

    # ------------------------------------------------------------- assessment
    #: Safety rail: the UI must never present an AI assessment as a final decision.
    force_human_review: bool = True
    citation_match_threshold: float = 0.6

    # ------------------------------------------------------------- evaluation
    evaluation_output_dir: Path = BASE_DIR / "data" / "evaluation"
    default_auditor_name: str = "Research Auditor"

    # ------------------------------------------------------------- validators
    @field_validator(
        "data_dir",
        "upload_dir",
        "report_dir",
        "synthetic_dir",
        "controls_dir",
        "sample_evidence_dir",
        "evaluation_output_dir",
        mode="before",
    )
    @classmethod
    def _expand_path(cls, value: Any) -> Any:
        if isinstance(value, str):
            return Path(os.path.expanduser(value))
        return value

    @field_validator("llm_provider", "embedding_provider", mode="before")
    @classmethod
    def _normalise_provider(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("retrieval_strategy", mode="before")
    @classmethod
    def _normalise_strategy(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("mock_hallucination_rate")
    @classmethod
    def _clamp_rate(cls, value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    # ---------------------------------------------------------------- helpers
    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def llm_configured(self) -> bool:
        """True when a real provider has everything it needs to be called."""
        if self.llm_provider == "mock":
            return True
        return bool(self.llm_api_key) or bool(self.llm_base_url)

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    def ensure_directories(self) -> None:
        for path in (
            self.data_dir,
            self.upload_dir,
            self.report_dir,
            self.synthetic_dir,
            self.controls_dir,
            self.sample_evidence_dir,
            self.evaluation_output_dir,
        ):
            Path(path).mkdir(parents=True, exist_ok=True)

    def redacted_dict(self) -> Dict[str, Any]:
        """Settings safe to display: every secret is masked, never echoed."""
        secret_fields = {"llm_api_key", "embedding_api_key"}
        out: Dict[str, Any] = {}
        for key, value in self.model_dump().items():
            if key in secret_fields:
                out[key] = _mask(value)
            elif isinstance(value, Path):
                out[key] = str(value)
            else:
                out[key] = value
        return out

    def provider_summary(self) -> Dict[str, Any]:
        return {
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "llm_base_url": self.llm_base_url or "(provider default)",
            "llm_api_key": _mask(self.llm_api_key),
            "llm_configured": self.llm_configured,
            "embedding_provider": self.embedding_provider,
            "embedding_model": self.embedding_model if self.embedding_provider != "local" else "hashing-vectorizer",
            "retrieval_strategy": self.retrieval_strategy,
            "retrieval_top_k": self.retrieval_top_k,
            "mock_hallucination_rate": self.mock_hallucination_rate,
        }


def _mask(value: Optional[str]) -> str:
    """Render a secret as ``sk-…abcd`` so an auditor can confirm *which* key is set."""
    if not value:
        return "(not set)"
    text = str(value)
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:3]}...{text[-4:]} (len {len(text)})"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings


def reload_settings() -> Settings:
    """Clear the cache and re-read the environment (used by the Settings page)."""
    get_settings.cache_clear()
    return get_settings()


#: Convenience singleton for modules that do not need to re-read configuration.
settings = get_settings()

SUPPORTED_UPLOAD_EXTENSIONS: List[str] = [".pdf", ".docx", ".csv", ".xlsx", ".xls", ".txt", ".md", ".json"]
