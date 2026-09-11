"""Provider-agnostic LLM interface.

Every concrete provider (OpenAI-compatible HTTP endpoint, or the offline deterministic
mock) implements :class:`LLMProvider`. The assessment engine only ever talks to this
interface, so swapping in a locally hosted model is a configuration change, not a code
change.
"""

from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class LLMError(RuntimeError):
    """Raised when a provider cannot produce a usable response."""


class LLMNotConfiguredError(LLMError):
    """Raised when a provider is selected but its credentials/endpoint are missing."""


@dataclass
class LLMResponse:
    """A single completion plus the telemetry the research harness measures."""

    text: str
    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    finish_reason: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class LLMCallContext:
    """Optional hints a provider may use. The mock provider uses these to reason
    deterministically over the *actual* retrieved evidence rather than inventing text."""

    purpose: str = "assessment"
    control: Optional[Dict[str, Any]] = None
    chunks: List[Dict[str, Any]] = field(default_factory=list)
    extras: Dict[str, Any] = field(default_factory=dict)


class LLMProvider(ABC):
    """Minimal surface the audit engine depends on."""

    name: str = "base"

    def __init__(self, model: str = "", **kwargs: Any) -> None:
        self.model = model
        self.options: Dict[str, Any] = dict(kwargs)

    @abstractmethod
    def complete(
        self,
        system: str,
        user: str,
        json_schema: Optional[Dict[str, Any]] = None,
        temperature: float = 0.0,
        max_tokens: int = 2000,
        context: Optional[LLMCallContext] = None,
    ) -> LLMResponse:
        """Return a completion. When ``json_schema`` is supplied the provider should
        request structured output if the backend supports it."""

    def embed(self, texts: List[str]) -> List[List[float]]:
        """Optional. Providers without an embedding endpoint may raise."""
        raise NotImplementedError(f"{self.name} does not implement embeddings")

    def is_available(self) -> bool:
        """True when the provider can actually be called right now."""
        return True

    def health(self) -> Dict[str, Any]:
        return {"provider": self.name, "model": self.model, "available": self.is_available()}

    # ------------------------------------------------------------------ helpers
    def _timed(self, start: float) -> int:
        return int((time.perf_counter() - start) * 1000)


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> Dict[str, Any]:
    """Recover a JSON object from model output.

    Real models wrap JSON in prose or fences, emit trailing commas, or use smart
    quotes. Rather than failing an audit run on a formatting quirk, this walks through
    progressively more forgiving strategies and raises :class:`LLMError` only when no
    object can be recovered at all.
    """
    if not text or not text.strip():
        raise LLMError("Model returned an empty response")

    candidates: List[str] = []
    stripped = text.strip()
    candidates.append(stripped)

    fenced = _JSON_FENCE.findall(text)
    candidates.extend(block.strip() for block in fenced)

    # Greedy outermost {...} span.
    first, last = stripped.find("{"), stripped.rfind("}")
    if first != -1 and last > first:
        candidates.append(stripped[first : last + 1])

    # Balanced-brace scan: handles trailing prose after a complete object.
    balanced = _first_balanced_object(stripped)
    if balanced:
        candidates.append(balanced)

    for candidate in candidates:
        if not candidate:
            continue
        for attempt in (candidate, _repair_json(candidate)):
            try:
                parsed = json.loads(attempt)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                return parsed[0]

    preview = stripped[:400].replace("\n", " ")
    raise LLMError(f"Could not parse JSON from model response. First 400 chars: {preview}")


def _first_balanced_object(text: str) -> Optional[str]:
    depth = 0
    start = -1
    in_string = False
    escape = False
    for index, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start != -1:
                return text[start : index + 1]
    return None


def _repair_json(text: str) -> str:
    """Common, safe repairs: smart quotes, trailing commas, python literals."""
    repaired = (
        text.replace("“", '"')
        .replace("”", '"')
        .replace("‘", "'")
        .replace("’", "'")
    )
    repaired = re.sub(r",(\s*[}\]])", r"\1", repaired)
    repaired = re.sub(r"\bNone\b", "null", repaired)
    repaired = re.sub(r"\bTrue\b", "true", repaired)
    repaired = re.sub(r"\bFalse\b", "false", repaired)
    return repaired


def estimate_tokens(text: str) -> int:
    """Cheap, provider-independent token estimate (~4 characters per token)."""
    if not text:
        return 0
    return max(1, len(text) // 4)
