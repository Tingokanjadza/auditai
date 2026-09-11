"""Selects the LLM provider from configuration.

Everything above this module - the assessment engine, the evaluation runner, the API,
the Streamlit app - asks for a provider by calling :func:`get_llm_provider` and never
imports a concrete class. Switching the whole system between the offline mock and a
hosted or self-hosted model is therefore a change to ``LLM_PROVIDER`` in ``.env``,
not a change to any code.

The one piece of judgement in here: when a real provider is selected but has no
credentials and no base URL, the system falls back to the mock and logs a warning
rather than raising. A half-configured environment should still start, and the demo
should still run. The fallback is loud - it is recorded in the log, surfaced through
:func:`provider_health`, and visible on the response itself as ``provider="mock"`` -
because an experiment that silently ran on the mock while its author believed it ran
on a real model would be worthless.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.config import Settings, get_settings
from app.llm.base import LLMProvider
from app.llm.anthropic_provider import AnthropicProvider
from app.llm.mock_provider import MockLLMProvider
from app.llm.openai_provider import OpenAICompatibleProvider

logger = logging.getLogger(__name__)

#: Canonical provider names.
MOCK = "mock"
OPENAI = "openai"
CLAUDE = "claude"

#: Names people actually type. Every OpenAI-protocol gateway resolves to the same class;
#: what distinguishes them is ``LLM_BASE_URL``, not the code path.
_ALIASES: Dict[str, str] = {
    "": MOCK,
    "mock": MOCK,
    "offline": MOCK,
    "none": MOCK,
    "fake": MOCK,
    "stub": MOCK,
    "deterministic": MOCK,
    "openai": OPENAI,
    "openai-compatible": OPENAI,
    "openai_compatible": OPENAI,
    "compatible": OPENAI,
    "azure": OPENAI,
    "ollama": OPENAI,
    "vllm": OPENAI,
    "lmstudio": OPENAI,
    "lm-studio": OPENAI,
    "openrouter": OPENAI,
    "together": OPENAI,
    "groq": OPENAI,
    "local": OPENAI,
    # Claude goes through the Anthropic SDK, not the OpenAI protocol, so it is a
    # separate class rather than another base_url.
    "claude": CLAUDE,
    "anthropic": CLAUDE,
}


def available_providers() -> List[str]:
    """Canonical provider names this build can construct."""
    return [MOCK, CLAUDE, OPENAI]


def normalise_provider_name(name: Optional[str]) -> str:
    """Map a user-supplied provider name onto a canonical one.

    An unrecognised name resolves to the mock rather than raising: an unknown value in
    a ``.env`` file should not stop the application from starting.
    """
    key = (name or "").strip().lower()
    if key in _ALIASES:
        return _ALIASES[key]
    logger.warning("Unknown LLM provider %r; using the offline mock provider instead.", name)
    return MOCK


def get_llm_provider(
    name: Optional[str] = None,
    model: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> LLMProvider:
    """Return the configured provider, falling back to the mock when unconfigured.

    A new instance is returned per call so that a change on the Settings page takes
    effect on the next assessment without a restart. The OpenAI client inside the real
    provider is created lazily, so this is cheap.
    """
    resolved = settings or get_settings()
    canonical = normalise_provider_name(name if name is not None else resolved.llm_provider)
    chosen_model = model or resolved.llm_model

    if canonical == MOCK:
        return MockLLMProvider(model=model or "", settings=resolved)

    if canonical == CLAUDE:
        provider: LLMProvider = AnthropicProvider(
            model=model or resolved.anthropic_model, settings=resolved
        )
        missing = "ANTHROPIC_API_KEY is not set (or the 'anthropic' package is missing)"
    else:
        provider = OpenAICompatibleProvider(model=chosen_model, settings=resolved)
        missing = "neither LLM_API_KEY nor LLM_BASE_URL is set"

    if not provider.is_available():
        logger.warning(
            "LLM provider %r was selected but %s. Falling back to the offline mock provider - "
            "any results produced now measure the pipeline, not a language model.",
            canonical,
            missing,
        )
        return MockLLMProvider(model=model or "", settings=resolved)
    return provider


def provider_health(settings: Optional[Settings] = None) -> Dict[str, Any]:
    """Configuration-only status for the Settings page and the ``/health`` endpoint.

    Makes no network calls, so it is safe to render on every page load and cannot leak
    the existence of a key to a remote endpoint.
    """
    resolved = settings or get_settings()
    configured = normalise_provider_name(resolved.llm_provider)
    active = get_llm_provider(settings=resolved)
    fell_back = configured != MOCK and active.name == MOCK
    reasons = {
        CLAUDE: "ANTHROPIC_API_KEY is not configured, or the 'anthropic' package is not installed.",
        OPENAI: "Neither LLM_API_KEY nor LLM_BASE_URL is configured for the selected provider.",
    }
    return {
        "configured_provider": configured,
        "active_provider": active.name,
        "active_model": active.model,
        "fell_back_to_mock": fell_back,
        "fallback_reason": reasons.get(configured, "") if fell_back else "",
        "available_providers": available_providers(),
        "providers": {
            MOCK: MockLLMProvider(settings=resolved).health(),
            CLAUDE: AnthropicProvider(settings=resolved).health(),
            OPENAI: OpenAICompatibleProvider(settings=resolved).health(),
        },
        "settings": resolved.provider_summary(),
    }
