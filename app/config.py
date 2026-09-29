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

#: Provider ids as ``app.llm.factory`` spells them. Kept here (not imported) because the
#: factory imports this module; the two lists must stay in step with ``factory._ALIASES``.
_MOCK_PROVIDER_IDS = frozenset({"mock", "offline", "none", "fake", "stub", "deterministic", ""})
_CLAUDE_PROVIDER_IDS = frozenset({"claude", "anthropic"})

#: Every field that holds a credential. ``redacted_dict`` masks exactly these; add a field
#: here the moment it is added above, or it will be rendered on the Settings page.
SECRET_FIELDS = frozenset({"llm_api_key", "embedding_api_key", "anthropic_api_key"})


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
    #: Display name shown to auditors. The long ``app_name`` is the research title.
    app_short_name: str = "AuditAI"
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
    #: Canonical ids are "mock", "openai" and "claude" ("anthropic" is accepted as an
    #: alias of "claude"); see ``app.llm.factory`` for the full alias table.
    llm_provider: str = "mock"
    #: Model for the ``openai`` provider only. Claude reads ``anthropic_model``.
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

    # ----------------------------------------------------------------- Claude
    #: Anthropic credentials. Set ANTHROPIC_API_KEY and the provider becomes available;
    #: leave it unset and the application falls back to the offline mock with a visible
    #: notice. The key is never logged, never returned by the API and never rendered.
    anthropic_api_key: Optional[str] = None
    anthropic_model: str = "claude-opus-5"
    #: Only for Anthropic-compatible gateways/proxies; leave unset for the real API.
    anthropic_base_url: Optional[str] = None
    #: Reasoning depth for Claude: low | medium | high | xhigh | max. Audit reasoning is
    #: non-trivial, so "high" is the default; drop to "low" for cheap bulk runs.
    anthropic_effort: str = "high"
    #: Adaptive thinking. Current Claude models reject a fixed token budget, and they
    #: reject `temperature` outright - determinism is controlled by effort, not sampling.
    anthropic_thinking: bool = True

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

    # ------------------------------------------------------------- deployment
    #: Public URLs of the deployed services. Used for CORS and for any link the app
    #: renders. Left at the localhost defaults these change nothing; set them when the
    #: app is deployed so no hostname is baked into the code.
    public_app_url: Optional[str] = None
    public_api_url: Optional[str] = None
    #: Comma-separated extra origins permitted by CORS, e.g.
    #: "https://audit.example.edu,https://myapp.streamlit.app". Localhost is always
    #: allowed in development; in production ONLY these origins are allowed.
    cors_allow_origins: str = ""
    #: When true the API stops trusting localhost automatically and permits only the
    #: origins named above. Turn this on for any internet-facing deployment.
    cors_restrict_to_configured: bool = False
    #: Route the Streamlit UI through the HTTP API instead of the in-process service
    #: layer. Needed when the UI and API are deployed as separate services.
    use_api: bool = False

    # ------------------------------------------------------------- evaluation
    evaluation_output_dir: Path = BASE_DIR / "data" / "evaluation"
    #: Blank by design: the sidebar asks the auditor for a name, and a blank name is never
    #: silently attributed to a record. Set DEFAULT_AUDITOR_NAME to pre-fill the field.
    default_auditor_name: str = ""

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
    def is_claude_provider(self) -> bool:
        """True when ``llm_provider`` selects Claude (``claude`` or its alias ``anthropic``)."""
        return self.llm_provider in _CLAUDE_PROVIDER_IDS

    @property
    def active_llm_model(self) -> str:
        """The model the selected provider would actually call.

        Claude reads ``anthropic_model``; every OpenAI-compatible endpoint reads
        ``llm_model``; the mock has no model and reports itself as such.
        """
        if self.llm_provider in _MOCK_PROVIDER_IDS:
            return "mock (offline rule-based)"
        if self.is_claude_provider:
            return self.anthropic_model
        return self.llm_model

    @property
    def llm_configured(self) -> bool:
        """True when the selected provider has everything it needs to be called.

        Configuration only - no network call and no package import. The factory still
        decides at run time whether the provider is *available* (e.g. the ``anthropic``
        package is installed); this property answers "did the operator supply what the
        provider asked for", which is what the Settings page and ``/health`` report.
        """
        if self.llm_provider in _MOCK_PROVIDER_IDS:
            return True
        if self.is_claude_provider:
            return bool(self.anthropic_api_key) or bool(self.anthropic_base_url)
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
        secret_fields = SECRET_FIELDS
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
        """Provider status safe to render. Reports the *active* provider's model and key.

        ``llm_model`` and ``llm_api_key`` keep their historical keys (the API schema and
        the Settings page read them) but describe whichever provider is selected, so a
        Claude deployment shows ``anthropic_model`` rather than the unused OpenAI model.
        """
        if self.is_claude_provider:
            api_key = self.anthropic_api_key
            base_url = self.anthropic_base_url
        else:
            api_key = self.llm_api_key
            base_url = self.llm_base_url
        return {
            "llm_provider": self.llm_provider,
            "llm_model": self.active_llm_model,
            "llm_base_url": base_url or "(provider default)",
            "llm_api_key": _mask(api_key),
            "llm_configured": self.llm_configured,
            "anthropic_model": self.anthropic_model,
            "anthropic_api_key": _mask(self.anthropic_api_key),
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
