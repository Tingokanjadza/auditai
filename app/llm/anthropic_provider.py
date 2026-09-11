"""Claude (Anthropic) provider.

Everything Anthropic-specific in this application lives in this file. The assessment
engine, the evaluation harness, the API and the UI all talk to :class:`LLMProvider`
(``app.llm.base``) and know nothing about Anthropic's request shape, so Claude is one
implementation among several rather than a dependency of the system.

Three details of the current Claude API drive the shape of the code below, and each one
is a 400 error rather than a soft failure if you get it wrong:

* **``temperature`` is rejected.** Current Claude models removed sampling parameters
  entirely. ``LLMProvider.complete`` accepts a ``temperature`` argument because other
  providers need one; here it is deliberately ignored, and reproducibility is controlled
  through ``output_config.effort`` instead. See :meth:`_unsupported_sampling`.
* **Thinking is configured adaptively, not with a token budget.** ``budget_tokens`` is
  rejected by current models; ``{"type": "adaptive"}`` lets the model decide how much
  reasoning an assessment needs. Audit reasoning is exactly the kind of work that
  benefits, so it is on by default.
* **Structured output is a first-class request parameter**, not a prompt convention.
  When the caller supplies a JSON schema this provider asks the API to constrain the
  response to it, which removes an entire class of parse failure. Older or proxied
  endpoints may reject the parameter, so there is an explicit, logged degradation path
  back to schema-in-the-prompt rather than a hard failure mid-audit.

The API key is read from configuration, never written to a log, never attached to an
``LLMResponse``, and never included in an error message returned to a caller.
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

#: Effort levels the current API accepts. An unrecognised value is clamped to "high"
#: rather than passed through, because an invalid effort is a 400 mid-assessment.
VALID_EFFORT = ("low", "medium", "high", "xhigh", "max")

#: Substrings that identify a 400 caused by the *request shape* rather than the content.
#: Hitting one of these means the endpoint does not understand a parameter we sent, so
#: retrying without that parameter is worth doing; any other 400 is a real error.
_SHAPE_REJECTIONS = (
    "output_config",
    "output_format",
    "json_schema",
    "unexpected keyword",
    "unrecognized request argument",
    "extra inputs are not permitted",
    "thinking",
    "effort",
)


class AnthropicProvider(LLMProvider):
    """Calls Claude through the official Anthropic SDK."""

    name = "claude"

    def __init__(
        self,
        model: str = "",
        settings: Optional[Settings] = None,
        **kwargs: Any,
    ) -> None:
        self.settings = settings or get_settings()
        super().__init__(model=model or self.settings.anthropic_model, **kwargs)
        self._client: Optional[Any] = None
        #: Set once the endpoint has told us it cannot honour a structured-output
        #: request, so we stop paying for the round trip on every later call.
        self._structured_output_supported = True

    # ------------------------------------------------------------------ client
    def _get_client(self) -> Any:
        """Construct the SDK client lazily so importing this module needs no key."""
        if self._client is not None:
            return self._client
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise LLMNotConfiguredError(
                "The 'anthropic' package is not installed. Run: pip install anthropic"
            ) from exc

        if not self.settings.anthropic_api_key:
            raise LLMNotConfiguredError(
                "ANTHROPIC_API_KEY is not set. Set it in .env to use Claude, or leave "
                "LLM_PROVIDER=mock to run the offline demonstration provider."
            )

        client_kwargs: Dict[str, Any] = {
            "api_key": self.settings.anthropic_api_key,
            "timeout": float(self.settings.llm_timeout),
            "max_retries": int(self.settings.llm_max_retries),
        }
        if self.settings.anthropic_base_url:
            client_kwargs["base_url"] = self.settings.anthropic_base_url
        self._client = anthropic.Anthropic(**client_kwargs)
        return self._client

    def is_available(self) -> bool:
        """True when Claude could actually be called. Makes no network request."""
        if not self.settings.anthropic_api_key:
            return False
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False
        return True

    def health(self) -> Dict[str, Any]:
        try:
            import anthropic

            sdk_version = getattr(anthropic, "__version__", "unknown")
            sdk_installed = True
        except ImportError:
            sdk_version = "not installed"
            sdk_installed = False
        return {
            "provider": self.name,
            "model": self.model,
            "available": self.is_available(),
            "sdk_installed": sdk_installed,
            "sdk_version": sdk_version,
            # Presence only. The key itself is never returned by this method.
            "api_key_configured": bool(self.settings.anthropic_api_key),
            "base_url": self.settings.anthropic_base_url or "(Anthropic default)",
            "effort": self._effort(),
            "adaptive_thinking": bool(self.settings.anthropic_thinking),
        }

    # ------------------------------------------------------------------ helpers
    def _effort(self) -> str:
        value = (self.settings.anthropic_effort or "high").strip().lower()
        if value not in VALID_EFFORT:
            logger.warning(
                "ANTHROPIC_EFFORT=%r is not one of %s; using 'high'.", value, ", ".join(VALID_EFFORT)
            )
            return "high"
        return value

    @staticmethod
    def _unsupported_sampling(temperature: float) -> None:
        """Explain, once per surprising call, why ``temperature`` is discarded."""
        if temperature not in (0.0, None):
            logger.debug(
                "temperature=%s ignored: current Claude models reject sampling parameters. "
                "Use ANTHROPIC_EFFORT to control reasoning depth instead.",
                temperature,
            )

    @staticmethod
    def _strictify(schema: Dict[str, Any]) -> Dict[str, Any]:
        """Make a Pydantic-generated schema acceptable to constrained decoding.

        Structured output requires every object to forbid unknown keys. Pydantic does not
        emit ``additionalProperties: false``, so it is added throughout, including inside
        ``$defs``. The schema is copied - mutating the caller's would corrupt the shared
        schema object that other providers also use.
        """
        import copy

        def walk(node: Any) -> Any:
            if isinstance(node, dict):
                out = {key: walk(value) for key, value in node.items()}
                if out.get("type") == "object" and "additionalProperties" not in out:
                    out["additionalProperties"] = False
                return out
            if isinstance(node, list):
                return [walk(item) for item in node]
            return node

        return walk(copy.deepcopy(schema))

    def _build_request(
        self,
        system: str,
        user: str,
        json_schema: Optional[Dict[str, Any]],
        max_tokens: int,
        structured: bool,
    ) -> Dict[str, Any]:
        output_config: Dict[str, Any] = {"effort": self._effort()}
        if structured and json_schema:
            output_config["format"] = {
                "type": "json_schema",
                "schema": self._strictify(json_schema),
            }

        request: Dict[str, Any] = {
            "model": self.model,
            # Audit assessments are long: citations, reasoning and the verification list
            # add up, and a truncated assessment is worse than a slow one.
            "max_tokens": max(1024, int(max_tokens)),
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": output_config,
        }
        if self.settings.anthropic_thinking:
            # Adaptive: the model decides how much reasoning this assessment needs.
            # "summarized" so a researcher can inspect the reasoning that led to a finding.
            request["thinking"] = {"type": "adaptive", "display": "summarized"}
        return request

    @staticmethod
    def _extract_text(message: Any) -> str:
        """Join the text blocks of a response, ignoring thinking blocks."""
        parts: List[str] = []
        for block in getattr(message, "content", []) or []:
            if getattr(block, "type", None) == "text":
                parts.append(getattr(block, "text", "") or "")
        return "\n".join(part for part in parts if part).strip()

    @staticmethod
    def _thinking_summary(message: Any) -> str:
        parts: List[str] = []
        for block in getattr(message, "content", []) or []:
            if getattr(block, "type", None) == "thinking":
                text = getattr(block, "thinking", "") or ""
                if text:
                    parts.append(text)
        return "\n".join(parts)

    # ------------------------------------------------------------------ main call
    def complete(
        self,
        system: str,
        user: str,
        json_schema: Optional[Dict[str, Any]] = None,
        temperature: float = 0.0,
        max_tokens: int = 2000,
        context: Optional[LLMCallContext] = None,
    ) -> LLMResponse:
        self._unsupported_sampling(temperature)
        client = self._get_client()
        want_structured = bool(json_schema) and self.settings.llm_use_json_mode and self._structured_output_supported

        start = time.perf_counter()
        message = self._call(client, system, user, json_schema, max_tokens, want_structured)
        latency_ms = self._timed(start)
        # _call may have degraded to an unconstrained request. Report what actually
        # happened, not what was asked for - a researcher reading this telemetry needs to
        # know whether the JSON was schema-guaranteed or merely parsed out of prose.
        used_structured = want_structured and self._structured_output_supported

        text = self._extract_text(message)
        stop_reason = getattr(message, "stop_reason", "") or ""

        if stop_reason == "refusal":
            # A safety decline is an HTTP 200 with no usable content. Surfacing it as a
            # normal empty response would show the auditor a blank assessment.
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) or "unspecified"
            raise LLMError(
                "Claude declined to complete this assessment (category: {0}). The evidence or "
                "control text may have triggered a safety classifier. Review the inputs, or run "
                "this control with the offline provider.".format(category)
            )

        if not text:
            raise LLMError(
                "Claude returned no text content (stop_reason={0!r}). If this is 'max_tokens', "
                "raise LLM_MAX_TOKENS.".format(stop_reason)
            )

        usage = getattr(message, "usage", None)
        return LLMResponse(
            text=text,
            provider=self.name,
            model=getattr(message, "model", self.model) or self.model,
            prompt_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            latency_ms=latency_ms,
            finish_reason=stop_reason,
            raw={
                "id": getattr(message, "id", ""),
                "stop_reason": stop_reason,
                "structured_output": used_structured,
                "structured_output_requested": want_structured,
                "effort": self._effort(),
                "thinking_summary": self._thinking_summary(message),
                "cache_read_input_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
                "estimated_prompt_tokens": estimate_tokens(system) + estimate_tokens(user),
            },
        )

    def _call(
        self,
        client: Any,
        system: str,
        user: str,
        json_schema: Optional[Dict[str, Any]],
        max_tokens: int,
        structured: bool,
    ) -> Any:
        """One API call, translating SDK exceptions into actionable LLMErrors.

        The SDK already retries connection errors, 429s and 5xxs, so there is no retry
        loop here - only the single degradation that the SDK cannot do for us: dropping
        structured output when the endpoint does not support it.
        """
        import anthropic

        request = self._build_request(system, user, json_schema, max_tokens, structured)
        try:
            return client.messages.create(**request)

        except anthropic.BadRequestError as exc:
            detail = str(getattr(exc, "message", "") or exc).lower()
            if structured and any(token in detail for token in _SHAPE_REJECTIONS):
                # The endpoint (often a proxy or an older gateway) does not understand a
                # parameter we sent. Retry unconstrained and let the caller's JSON
                # recovery handle the response, rather than failing the assessment.
                logger.warning(
                    "Claude endpoint rejected the structured-output request; retrying without it. "
                    "Responses will be parsed from prose, so malformed JSON becomes possible. "
                    "Detail: %s",
                    detail[:300],
                )
                self._structured_output_supported = False
                return client.messages.create(
                    **self._build_request(system, user, json_schema, max_tokens, structured=False)
                )
            raise LLMError("Claude rejected the request: {0}".format(detail[:400])) from exc

        except anthropic.AuthenticationError as exc:
            # Deliberately says nothing about the key's value.
            raise LLMNotConfiguredError(
                "Claude rejected the configured ANTHROPIC_API_KEY. Check the key in .env; "
                "the application falls back to the offline provider if you unset it."
            ) from exc

        except anthropic.PermissionDeniedError as exc:
            raise LLMNotConfiguredError(
                "The configured Anthropic key does not have access to model "
                "{0!r}. Set ANTHROPIC_MODEL to a model your account can use.".format(self.model)
            ) from exc

        except anthropic.NotFoundError as exc:
            raise LLMError(
                "Anthropic model {0!r} was not found. Check ANTHROPIC_MODEL.".format(self.model)
            ) from exc

        except anthropic.RateLimitError as exc:
            raise LLMError(
                "Claude rate limit reached after the SDK's automatic retries. Wait and re-run "
                "this assessment, or lower the batch size on the Evaluation page."
            ) from exc

        except anthropic.APIConnectionError as exc:
            raise LLMError(
                "Could not reach the Anthropic API. Check network connectivity, or set "
                "LLM_PROVIDER=mock to continue offline."
            ) from exc

        except anthropic.APIStatusError as exc:
            status = getattr(exc, "status_code", "unknown")
            raise LLMError("Anthropic API error (HTTP {0}).".format(status)) from exc
