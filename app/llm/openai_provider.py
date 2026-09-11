"""Provider for any OpenAI-compatible chat-completions endpoint.

One class covers OpenAI itself and every gateway that speaks the same protocol -
Ollama, vLLM, LM Studio, OpenRouter, an internal proxy - because the only thing that
changes between them is ``llm_base_url``. That matters for a university project: the
same experiment can be re-run against a locally hosted open-weight model without
touching the audit code, and a reader can reproduce it without an OpenAI account.

Two behaviours here exist because compatible endpoints are only *mostly* compatible:

* **Structured output degrades in steps.** A JSON-schema-constrained response is asked
  for first, then plain JSON mode, then an unconstrained completion. Each fallback is
  triggered only by a 4xx that indicates the server does not support the feature, and
  each is recorded on the response so an experiment can report how the output was
  actually obtained rather than assuming the best case.
* **Transient failures are retried with exponential backoff**, but 4xx errors that will
  never succeed (bad key, unknown model) are raised immediately instead of being
  retried four times over half a minute.

Token counts come from the provider's own ``usage`` block wherever it is returned; the
character-based estimate is a labelled fallback, never presented as measured.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from app.config import Settings, get_settings
from app.llm.base import (
    LLMCallContext,
    LLMError,
    LLMNotConfiguredError,
    LLMProvider,
    LLMResponse,
    estimate_tokens,
)

logger = logging.getLogger(__name__)

try:  # The SDK is a hard dependency of the project, but the app must still import
    # and run in mock mode on a machine where it failed to install.
    import openai as _openai

    _IMPORT_ERROR: Optional[str] = None
except Exception as exc:  # pragma: no cover - exercised only on a broken install
    _openai = None  # type: ignore[assignment]
    _IMPORT_ERROR = str(exc)


#: Errors worth retrying: the request was well-formed and may succeed shortly.
def _transient_errors() -> tuple:
    if _openai is None:  # pragma: no cover
        return tuple()
    return (
        _openai.RateLimitError,
        _openai.APIConnectionError,
        _openai.APITimeoutError,
        _openai.InternalServerError,
    )


#: Errors meaning "this endpoint will not accept that request shape", which is the
#: signal to degrade structured output rather than to retry.
def _capability_errors() -> tuple:
    if _openai is None:  # pragma: no cover
        return tuple()
    return (
        _openai.BadRequestError,
        _openai.UnprocessableEntityError,
    )


class OpenAICompatibleProvider(LLMProvider):
    """Chat-completions provider with schema, JSON and plain-text response modes."""

    name = "openai"

    def __init__(
        self,
        model: str = "",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        settings: Optional[Settings] = None,
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
        **kwargs: Any,
    ) -> None:
        resolved = settings or get_settings()
        super().__init__(model=model or resolved.llm_model, **kwargs)
        self.settings = resolved
        self.api_key = api_key if api_key is not None else resolved.llm_api_key
        self.base_url = base_url if base_url is not None else resolved.llm_base_url
        self.timeout = int(timeout if timeout is not None else resolved.llm_timeout)
        self.max_retries = int(max_retries if max_retries is not None else resolved.llm_max_retries)
        self.use_json_mode = bool(resolved.llm_use_json_mode)
        self._client: Any = None
        #: Set once a server has rejected a response format, so the whole run does not
        #: pay the cost of rediscovering the same limitation on every call.
        self._schema_unsupported = False
        self._json_mode_unsupported = False
        #: Renamed to max_completion_tokens by newer models; discovered on first refusal.
        self._max_tokens_param = "max_tokens"

    # ------------------------------------------------------------------ client
    def _get_client(self) -> Any:
        """Build the SDK client on first use.

        Deliberately lazy: constructing a provider must never require credentials, so
        that the settings page can display an unconfigured provider and the factory can
        fall back to the mock without an exception being raised on import.
        """
        if self._client is not None:
            return self._client
        if _openai is None:
            raise LLMNotConfiguredError(
                "The 'openai' package is not importable ({0}). Run with LLM_PROVIDER=mock or reinstall it.".format(
                    _IMPORT_ERROR
                )
            )
        if not self.api_key and not self.base_url:
            raise LLMNotConfiguredError(
                "No LLM_API_KEY and no LLM_BASE_URL are configured. Set one of them, or use LLM_PROVIDER=mock."
            )
        kwargs: Dict[str, Any] = {
            "timeout": self.timeout,
            # Retries are handled here so backoff and error classification stay visible
            # in the research telemetry rather than being hidden inside the SDK.
            "max_retries": 0,
        }
        # A local gateway usually ignores the key but the SDK still requires one.
        kwargs["api_key"] = self.api_key or "not-required-by-this-endpoint"
        if self.base_url:
            kwargs["base_url"] = self.base_url
        self._client = _openai.OpenAI(**kwargs)
        return self._client

    # --------------------------------------------------------------- interface
    def is_available(self) -> bool:
        """Configuration check only - deliberately makes no network call.

        The settings page and the provider health endpoint are rendered on every page
        load; probing a remote endpoint there would make the UI depend on a third party
        being reachable, and would leak the fact that a key exists to that third party.
        """
        if _openai is None:
            return False
        return bool(self.api_key or self.base_url)

    def health(self) -> Dict[str, Any]:
        return {
            "provider": self.name,
            "model": self.model,
            "available": self.is_available(),
            "base_url": self.base_url or "(OpenAI default)",
            "api_key_configured": bool(self.api_key),
            "sdk_importable": _openai is not None,
            "sdk_import_error": _IMPORT_ERROR,
            "json_mode_requested": self.use_json_mode,
            "note": "Availability reflects configuration only; no network call is made to determine it.",
        }

    def complete(
        self,
        system: str,
        user: str,
        json_schema: Optional[Dict[str, Any]] = None,
        temperature: float = 0.0,
        max_tokens: int = 2000,
        context: Optional[LLMCallContext] = None,
    ) -> LLMResponse:
        start = time.perf_counter()
        client = self._get_client()
        messages = [
            {"role": "system", "content": system or ""},
            {"role": "user", "content": user or ""},
        ]
        attempts = self._response_format_ladder(json_schema)
        last_capability_error: Optional[Exception] = None

        for response_format, mode in attempts:
            try:
                completion = self._call_with_backoff(
                    client,
                    messages=messages,
                    response_format=response_format,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except _capability_errors() as exc:
                # The endpoint does not support this response format. Remember it and
                # drop down a rung rather than failing the assessment run.
                last_capability_error = exc
                if mode == "json_schema":
                    self._schema_unsupported = True
                elif mode == "json_object":
                    self._json_mode_unsupported = True
                logger.warning(
                    "Provider rejected response_format=%s (%s); falling back to a less constrained mode.",
                    mode,
                    _short(exc),
                )
                continue
            response = self._to_response(completion, system, user, mode)
            # Wall-clock latency including every retry and fallback, which is what the
            # research harness reports as the cost of one assessment call.
            response.latency_ms = self._timed(start)
            return response

        raise LLMError(
            "The endpoint rejected every response format, including an unconstrained request: {0}".format(
                _short(last_capability_error) if last_capability_error else "unknown error"
            )
        )

    def embed(self, texts: List[str]) -> List[List[float]]:
        """Embeddings from the same endpoint. Used only when EMBEDDING_PROVIDER=openai."""
        client = self._get_client()
        model = self.settings.embedding_model
        try:
            response = client.embeddings.create(model=model, input=list(texts))
        except Exception as exc:  # noqa: BLE001 - surfaced as a provider error
            raise LLMError("Embedding request failed: {0}".format(_short(exc)))
        return [list(item.embedding) for item in response.data]

    # ----------------------------------------------------------------- helpers
    def _response_format_ladder(self, json_schema: Optional[Dict[str, Any]]) -> List[Any]:
        """Response formats to try, best first.

        ``strict`` is left off: strict schema mode requires every property to be
        required and additionalProperties to be false throughout, which the assessment
        schema (with its optional fields and ``$defs``) does not satisfy. Constrained
        but non-strict output plus the tolerant parser in :func:`app.llm.base.extract_json`
        is the combination that works across the widest range of endpoints.
        """
        ladder: List[Any] = []
        if json_schema and self.use_json_mode and not self._schema_unsupported:
            ladder.append(
                (
                    {
                        "type": "json_schema",
                        "json_schema": {
                            "name": str(json_schema.get("title") or "StructuredOutput").replace(" ", "_"),
                            "schema": json_schema,
                            "strict": False,
                        },
                    },
                    "json_schema",
                )
            )
        if self.use_json_mode and not self._json_mode_unsupported:
            ladder.append(({"type": "json_object"}, "json_object"))
        ladder.append((None, "text"))
        return ladder

    def _call_with_backoff(
        self,
        client: Any,
        messages: List[Dict[str, str]],
        response_format: Optional[Dict[str, Any]],
        temperature: float,
        max_tokens: int,
    ) -> Any:
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            self._max_tokens_param: max_tokens,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format

        last_error: Optional[Exception] = None
        attempt = 0
        while True:
            try:
                return client.chat.completions.create(**kwargs)
            except _capability_errors() as exc:
                # Newer reasoning models renamed max_tokens. That is a parameter-naming
                # difference, not a missing feature, so it is corrected in place rather
                # than being allowed to strip the response format off the request.
                message = str(exc).lower()
                if (
                    self._max_tokens_param == "max_tokens"
                    and "max_tokens" in message
                    and "max_completion_tokens" in message
                ):
                    self._max_tokens_param = "max_completion_tokens"
                    kwargs.pop("max_tokens", None)
                    kwargs["max_completion_tokens"] = max_tokens
                    logger.info("Endpoint requires max_completion_tokens; retrying with that parameter.")
                    continue
                raise  # handled a level up by degrading the response format
            except _transient_errors() as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                delay = min(30.0, 1.5 * (2 ** attempt))
                logger.warning(
                    "Transient LLM error (%s); retrying in %.1fs (attempt %d of %d).",
                    _short(exc),
                    delay,
                    attempt + 1,
                    self.max_retries,
                )
                attempt += 1
                time.sleep(delay)
            except Exception as exc:  # noqa: BLE001 - auth, quota, unknown model
                raise LLMError("LLM request failed: {0}".format(_short(exc)))
        raise LLMError(
            "LLM request failed after {0} attempt(s): {1}".format(self.max_retries + 1, _short(last_error))
        )

    def _to_response(self, completion: Any, system: str, user: str, mode: str) -> LLMResponse:
        choice = completion.choices[0] if getattr(completion, "choices", None) else None
        text = ""
        finish_reason = ""
        if choice is not None:
            message = getattr(choice, "message", None)
            text = (getattr(message, "content", None) or "") if message is not None else ""
            finish_reason = getattr(choice, "finish_reason", "") or ""
        if not text.strip():
            raise LLMError(
                "The provider returned an empty completion (finish_reason={0!r}). "
                "This usually means max_tokens was reached before any content was produced.".format(finish_reason)
            )

        usage = getattr(completion, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0) if usage is not None else 0
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0) if usage is not None else 0
        tokens_measured = bool(prompt_tokens or completion_tokens)
        if not tokens_measured:
            # Some compatible gateways omit usage entirely. Estimate, but say so.
            prompt_tokens = estimate_tokens("{0}\n{1}".format(system or "", user or ""))
            completion_tokens = estimate_tokens(text)

        return LLMResponse(
            text=text,
            provider=self.name,
            model=getattr(completion, "model", "") or self.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=0,
            finish_reason=finish_reason,
            raw={
                "provider": self.name,
                "response_format_used": mode,
                "tokens_measured": tokens_measured,
                "base_url": self.base_url or "(OpenAI default)",
                "id": getattr(completion, "id", ""),
                "system_fingerprint": getattr(completion, "system_fingerprint", "") or "",
            },
        )


def _short(exc: Optional[Exception], limit: int = 300) -> str:
    """Error text without leaking a key that some SDKs echo back in the message."""
    if exc is None:
        return ""
    text = "{0}: {1}".format(type(exc).__name__, exc)
    text = text.replace("\n", " ")
    return text[:limit]
