"""The Claude provider, exercised without a key, without a network and without mercy.

This module is the reason anyone can trust a number produced with ``LLM_PROVIDER=claude``
without having run it themselves. It proves four separate things, and each of them is a
way the integration could fail quietly rather than loudly:

**It cannot reach the network here.** Every test runs under :func:`no_network`, which
replaces ``httpx``'s transport *and* the SDK's client constructor with something that
raises. A test that accidentally starts working against the real API fails instead of
billing someone, and "no HTTP call" becomes an assertion rather than a claim in a
docstring.

**The request shape matches what current Claude models actually accept.** Sampling
parameters are rejected outright by these models - a stray ``temperature`` is a 400 in
the middle of an audit run, not a subtly different answer - so the tests scan the whole
request recursively for ``temperature``/``top_p``/``top_k`` rather than checking the top
level and hoping. The same scan confirms the two parameters that must be present:
``output_config.effort`` and adaptive thinking.

**The key never leaves configuration.** ``health()`` is rendered on the Settings page and
returned by ``/health``; the authentication error is shown to whoever is at the console.
Both are asserted not to contain the key, using a key value distinctive enough that a
substring search is conclusive.

**Degradation is visible.** A gateway that does not understand ``output_config`` must not
fail an assessment - but the response must then say, in ``raw``, that the JSON was parsed
out of prose rather than guaranteed by the schema. A researcher reading telemetry has to
be able to tell those apart, so ``structured_output`` and ``structured_output_requested``
are checked as a pair. The neighbouring case - a 400 about the *content*, such as a prompt
over the context limit - must not be swallowed by the same path.

Everything here uses hand-built fakes rather than recorded HTTP traffic. The provider
talks to the SDK through ``client.messages.create(**request)`` and reads the response with
``getattr``, so a fake that records its kwargs and returns duck-typed blocks exercises the
real code path, and the tests stay readable as a statement of the contract.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Optional

import anthropic
import httpx
import pytest

from app.config import Settings
from app.llm.anthropic_provider import VALID_EFFORT, AnthropicProvider
from app.llm.base import LLMError, LLMNotConfiguredError, LLMResponse
from app.llm.factory import CLAUDE, MOCK, get_llm_provider, provider_health
from app.llm.mock_provider import MockLLMProvider
from app.schemas.assessment import assessment_json_schema

#: Fabricated, and shaped like a real key so that a substring search for it is a genuine
#: test of "this value is nowhere in the output". It authenticates against nothing.
FAKE_KEY = "sk-ant-api03-FAKE-KEY-FOR-TESTS-0123456789-do-not-use"

#: Request keys that current Claude models reject. Sending any of them is an HTTP 400
#: mid-assessment, which is why they are hunted for at every depth rather than checked
#: for at the top level.
FORBIDDEN_REQUEST_KEYS = ("temperature", "top_p", "top_k")


# --------------------------------------------------------------------------- settings
def _settings(**overrides: Any) -> Settings:
    """Settings that select Claude, with every Anthropic field stated explicitly.

    Stated explicitly because ``Settings`` reads the process environment: a developer
    with ``ANTHROPIC_API_KEY`` or ``ANTHROPIC_BASE_URL`` exported in their shell - which
    is common, and is exactly the inheritance the Settings page warns about - would
    otherwise get different test results from CI, and the difference would be silent.
    """
    values: Dict[str, Any] = {
        "llm_provider": "claude",
        "anthropic_api_key": FAKE_KEY,
        "anthropic_model": "claude-opus-5",
        "anthropic_base_url": None,
        "anthropic_effort": "high",
        "anthropic_thinking": True,
        "llm_use_json_mode": True,
    }
    values.update(overrides)
    return Settings(**values)


# ------------------------------------------------------------------------ the network
@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any real HTTP request, at any layer, an immediate test failure.

    Autouse rather than opt-in: the value of this fixture is that no test in this module
    *can* be written that reaches the API, not that the careful ones do not. Both the
    transport (``httpx``) and the layer above it (the SDK client constructor) are
    blocked, so neither a change in this provider nor a change in the SDK can open a
    path to the wire without failing here first.
    """

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "A test attempted a real network call. This suite must run entirely offline."
        )

    monkeypatch.setattr(httpx.Client, "send", refuse)
    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    monkeypatch.setattr(anthropic, "Anthropic", refuse)


# ---------------------------------------------------------------------------- fakes
class FakeTextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class FakeThinkingBlock:
    """A reasoning block. It must never end up in the assessment text."""

    type = "thinking"

    def __init__(self, thinking: str) -> None:
        self.thinking = thinking


class FakeUsage:
    def __init__(self, input_tokens: int, output_tokens: int, cache_read: int = 0) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read


class FakeStopDetails:
    def __init__(self, category: str) -> None:
        self.category = category


class FakeMessage:
    """Duck-typed like an SDK ``Message``; the provider reads it with ``getattr``."""

    def __init__(
        self,
        content: Optional[List[Any]] = None,
        stop_reason: str = "end_turn",
        usage: Optional[FakeUsage] = None,
        model: str = "claude-opus-5",
        stop_details: Optional[FakeStopDetails] = None,
        id: str = "msg_test_0001",  # noqa: A002 - mirrors the SDK's attribute name
    ) -> None:
        self.content = content if content is not None else [FakeTextBlock("{}")]
        self.stop_reason = stop_reason
        self.usage = usage or FakeUsage(0, 0)
        self.model = model
        self.stop_details = stop_details
        self.id = id


class FakeMessages:
    def __init__(self, outcomes: List[Any]) -> None:
        #: What to do on each successive call, in order. An exception is raised, anything
        #: else is returned.
        self._outcomes = list(outcomes)
        #: Every request the provider built, kept so a test can assert on its shape.
        self.calls: List[Dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(copy.deepcopy(kwargs))
        if not self._outcomes:
            raise AssertionError(
                "The provider called the API {0} time(s); the test allowed for "
                "{1}.".format(len(self.calls), len(self.calls) - 1)
            )
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, *outcomes: Any) -> None:
        self.messages = FakeMessages(list(outcomes))


def _provider(*outcomes: Any, **setting_overrides: Any) -> AnthropicProvider:
    """A provider wired to a fake client, so ``_get_client`` never builds a real one."""
    provider = AnthropicProvider(settings=_settings(**setting_overrides))
    provider._client = FakeClient(*outcomes)
    return provider


def _bad_request(message: str) -> anthropic.BadRequestError:
    """A real SDK exception - the provider catches the SDK's class, not a stand-in."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(
        400,
        request=request,
        json={"type": "error", "error": {"type": "invalid_request_error", "message": message}},
    )
    return anthropic.BadRequestError(message, response=response, body=None)


def _auth_error(message: str = "invalid x-api-key") -> anthropic.AuthenticationError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(401, request=request, json={"type": "error"})
    return anthropic.AuthenticationError(message, response=response, body=None)


def _keys_at_every_depth(node: Any) -> List[str]:
    """Every mapping key anywhere in a nested structure."""
    found: List[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            found.append(str(key))
            found.extend(_keys_at_every_depth(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_keys_at_every_depth(item))
    return found


def _objects_missing_the_closed_flag(node: Any, path: str = "$") -> List[str]:
    """Paths of every JSON-Schema object node that still permits unknown keys."""
    offenders: List[str] = []
    if isinstance(node, dict):
        if node.get("type") == "object" and node.get("additionalProperties") is not False:
            offenders.append(path)
        for key, value in node.items():
            offenders.extend(_objects_missing_the_closed_flag(value, "{0}.{1}".format(path, key)))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            offenders.extend(_objects_missing_the_closed_flag(item, "{0}[{1}]".format(path, index)))
    return offenders


# ------------------------------------------------------------------- availability
def test_availability_follows_the_key_and_costs_nothing_to_ask() -> None:
    """Rendered on every page load, so it must be configuration-only."""
    assert AnthropicProvider(settings=_settings(anthropic_api_key=None)).is_available() is False
    assert AnthropicProvider(settings=_settings(anthropic_api_key="")).is_available() is False
    assert AnthropicProvider(settings=_settings()).is_available() is True
    # The autouse no_network fixture is what makes the second half of the sentence true:
    # had is_available() opened a connection or built an SDK client, it would have raised.


def test_health_reports_the_key_as_present_without_disclosing_it() -> None:
    """``health()`` reaches the Settings page and the ``/health`` endpoint verbatim."""
    health = AnthropicProvider(settings=_settings()).health()
    serialised = json.dumps(health)
    assert FAKE_KEY not in serialised
    # Not even a fragment: a prefix is enough to identify an account.
    assert "sk-ant" not in serialised
    assert health["api_key_configured"] is True
    assert health["available"] is True
    assert health["provider"] == "claude"
    assert health["effort"] == "high"
    assert health["adaptive_thinking"] is True

    without = AnthropicProvider(settings=_settings(anthropic_api_key=None)).health()
    assert without["api_key_configured"] is False
    assert without["available"] is False


# ----------------------------------------------------------------- the request shape
def test_the_request_carries_no_sampling_parameter_at_any_depth() -> None:
    """Current Claude models reject sampling parameters with a 400, so none is sent.

    ``complete`` accepts ``temperature`` because the :class:`LLMProvider` interface has
    one and other providers need it; this proves the argument is discarded rather than
    forwarded, including when a caller passes a non-default value.
    """
    provider = _provider(FakeMessage(content=[FakeTextBlock("{}")]))
    provider.complete("system", "user", json_schema=assessment_json_schema(), temperature=0.7)

    request = provider._client.messages.calls[0]
    keys = _keys_at_every_depth(request)
    for forbidden in FORBIDDEN_REQUEST_KEYS:
        assert forbidden not in keys, "{0} would be rejected with HTTP 400".format(forbidden)


def test_the_request_asks_for_effort_and_adaptive_thinking() -> None:
    """The two parameters that replace sampling control on current Claude models."""
    provider = _provider(FakeMessage())
    provider.complete("system", "user", json_schema=assessment_json_schema())

    request = provider._client.messages.calls[0]
    assert request["output_config"]["effort"] == "high"
    # Adaptive, not a token budget: a fixed ``budget_tokens`` is rejected by these models.
    assert request["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert request["model"] == "claude-opus-5"
    assert request["messages"] == [{"role": "user", "content": "user"}]
    assert request["system"] == "system"


def test_thinking_is_omitted_entirely_when_it_is_switched_off() -> None:
    provider = _provider(FakeMessage(), anthropic_thinking=False)
    provider.complete("system", "user")
    assert "thinking" not in provider._client.messages.calls[0]


def test_an_invalid_effort_level_is_clamped_rather_than_forwarded() -> None:
    """An unrecognised ANTHROPIC_EFFORT is a 400. Clamping keeps the audit running."""
    provider = _provider(FakeMessage(), anthropic_effort="turbo")
    provider.complete("system", "user")
    assert provider._client.messages.calls[0]["output_config"]["effort"] == "high"
    assert "turbo" not in VALID_EFFORT


# ------------------------------------------------------------------------ the schema
def test_every_object_in_the_schema_forbids_unknown_keys_including_the_defs() -> None:
    """Constrained decoding requires closed objects; Pydantic does not emit them.

    The assessment schema nests the citation model under ``$defs``, so a walk that only
    touched the top-level ``properties`` would pass while the API rejected the request.
    The first assertion keeps this test honest by failing if the schema ever stops having
    nested definitions.
    """
    schema = assessment_json_schema()
    assert schema.get("$defs"), "the schema no longer nests definitions - re-check this test"

    strict = AnthropicProvider(settings=_settings())._strictify(schema)

    assert not _objects_missing_the_closed_flag(strict)
    assert strict["additionalProperties"] is False

    # ``$defs`` holds the enum vocabularies as well as the nested citation model. Only
    # the object ones can carry unknown keys, and at least one of them must exist or the
    # walk above proved nothing about nested definitions.
    nested_objects = {
        name: definition
        for name, definition in strict["$defs"].items()
        if definition.get("type") == "object"
    }
    assert nested_objects, "no object under $defs - this test would be vacuous"
    for name, definition in nested_objects.items():
        assert definition.get("additionalProperties") is False, "$defs.{0} is open".format(name)


def test_making_the_schema_strict_does_not_touch_the_callers_copy() -> None:
    """The schema object is shared with the OpenAI provider and the prompt builder.

    Mutating it in place would silently change what a *different* provider sends, which
    is the kind of cross-contamination that makes a comparison between two conditions
    meaningless.
    """
    schema = assessment_json_schema()
    before = copy.deepcopy(schema)

    strict = AnthropicProvider(settings=_settings())._strictify(schema)

    assert schema == before, "the caller's schema was mutated"
    assert strict is not schema
    assert strict != schema, "nothing was added - the strictifier did not run"


def test_the_schema_reaches_the_request_as_output_config_format() -> None:
    provider = _provider(FakeMessage())
    provider.complete("system", "user", json_schema=assessment_json_schema())

    output_config = provider._client.messages.calls[0]["output_config"]
    assert output_config["format"]["type"] == "json_schema"
    assert output_config["format"]["schema"]["additionalProperties"] is False


# ------------------------------------------------------------------- happy response
def test_a_normal_response_becomes_an_llm_response_and_drops_the_thinking() -> None:
    """Reasoning is telemetry, not assessment text.

    A thinking block that leaked into ``text`` would be handed to ``extract_json`` and,
    worse, could end up quoted in a report as though it were the model's conclusion.
    """
    message = FakeMessage(
        content=[
            FakeThinkingBlock("The user listed 100 accounts; let me count the exceptions."),
            FakeTextBlock('{"status": "POTENTIAL_DEFICIENCY"}'),
            FakeTextBlock("Ten accounts are not enrolled."),
        ],
        usage=FakeUsage(input_tokens=1234, output_tokens=567, cache_read=89),
        stop_reason="end_turn",
    )
    response = _provider(message).complete("system", "user", json_schema=assessment_json_schema())

    assert isinstance(response, LLMResponse)
    assert response.text == '{"status": "POTENTIAL_DEFICIENCY"}\nTen accounts are not enrolled.'
    assert "let me count" not in response.text
    assert response.provider == "claude"
    assert response.model == "claude-opus-5"
    assert response.prompt_tokens == 1234
    assert response.completion_tokens == 567
    assert response.total_tokens == 1801
    assert response.finish_reason == "end_turn"
    assert response.raw["cache_read_input_tokens"] == 89
    assert response.raw["structured_output"] is True
    assert response.raw["structured_output_requested"] is True
    assert response.raw["effort"] == "high"
    # The reasoning is kept, but only where a researcher goes looking for it.
    assert "let me count" in response.raw["thinking_summary"]


# ------------------------------------------------------------------------- refusals
def test_a_refusal_is_raised_with_its_category_rather_than_shown_as_a_blank_assessment() -> None:
    """A safety decline arrives as HTTP 200 with no text. Returning it would put an
    empty assessment in front of an auditor as though the model had found nothing."""
    message = FakeMessage(content=[], stop_reason="refusal", stop_details=FakeStopDetails("csam"))
    with pytest.raises(LLMError) as excinfo:
        _provider(message).complete("system", "user")
    assert "csam" in str(excinfo.value)
    assert "declined" in str(excinfo.value).lower()


def test_a_refusal_without_a_category_still_raises() -> None:
    message = FakeMessage(content=[], stop_reason="refusal")
    with pytest.raises(LLMError) as excinfo:
        _provider(message).complete("system", "user")
    assert "unspecified" in str(excinfo.value)


def test_an_empty_response_is_an_error_and_says_which_stop_reason_caused_it() -> None:
    message = FakeMessage(content=[], stop_reason="max_tokens")
    with pytest.raises(LLMError) as excinfo:
        _provider(message).complete("system", "user")
    assert "max_tokens" in str(excinfo.value)


# ------------------------------------------------------------------- degradation
def test_an_endpoint_that_rejects_output_config_retries_without_it_and_says_so() -> None:
    """A proxy that does not understand structured output must not fail the audit.

    But the response has to record that the JSON was parsed out of prose rather than
    guaranteed by the schema, because that is the difference between a parse failure
    being impossible and being merely unlikely.
    """
    provider = _provider(
        _bad_request("output_config: Extra inputs are not permitted"),
        FakeMessage(content=[FakeTextBlock('{"status": "EFFECTIVE"}')]),
    )
    response = provider.complete("system", "user", json_schema=assessment_json_schema())

    calls = provider._client.messages.calls
    assert len(calls) == 2, "the degraded retry did not happen"
    assert "format" in calls[0]["output_config"], "the first attempt was not the constrained one"
    assert "format" not in calls[1]["output_config"], "the retry still asked for a schema"
    # Effort survives the degradation; only the schema is dropped.
    assert calls[1]["output_config"]["effort"] == "high"

    assert response.text == '{"status": "EFFECTIVE"}'
    assert response.raw["structured_output"] is False, "telemetry claims a guarantee it lacks"
    assert response.raw["structured_output_requested"] is True

    # The endpoint is remembered, so the next call does not pay for the round trip again.
    assert provider._structured_output_supported is False


def test_a_bad_request_about_the_content_is_not_swallowed_by_the_degradation_path() -> None:
    """A prompt over the context window is a real failure, not an unsupported parameter.

    Retrying it without the schema would send the same oversized prompt a second time and
    fail again, and hiding it behind a degradation warning would leave a researcher
    hunting for why an assessment came back unstructured.
    """
    provider = _provider(_bad_request("prompt is too long: 250000 tokens > 200000 maximum"))
    with pytest.raises(LLMError) as excinfo:
        provider.complete("system", "user", json_schema=assessment_json_schema())

    assert "too long" in str(excinfo.value)
    assert len(provider._client.messages.calls) == 1, "it retried a content error"
    assert provider._structured_output_supported is True, "structured output was wrongly disabled"


def test_a_content_error_is_not_a_configuration_error() -> None:
    """It must stay an ``LLMError``: the engine treats a configuration error as fatal."""
    provider = _provider(_bad_request("messages: at least one message is required"))
    with pytest.raises(LLMError) as excinfo:
        provider.complete("system", "user")
    assert not isinstance(excinfo.value, LLMNotConfiguredError)


# --------------------------------------------------------------------- credentials
def test_a_rejected_key_becomes_a_configuration_error_that_does_not_echo_the_key() -> None:
    """This message is shown at the console and written to the log."""
    provider = _provider(_auth_error("invalid x-api-key: {0}".format(FAKE_KEY)))
    with pytest.raises(LLMNotConfiguredError) as excinfo:
        provider.complete("system", "user")

    message = str(excinfo.value)
    assert FAKE_KEY not in message
    assert "sk-ant" not in message
    assert "ANTHROPIC_API_KEY" in message


def test_calling_claude_without_a_key_is_a_configuration_error_not_a_crash() -> None:
    provider = AnthropicProvider(settings=_settings(anthropic_api_key=None))
    with pytest.raises(LLMNotConfiguredError) as excinfo:
        provider.complete("system", "user")
    assert "ANTHROPIC_API_KEY" in str(excinfo.value)
    assert "mock" in str(excinfo.value).lower(), "it should name the offline way out"


# ------------------------------------------------------------------------- factory
def test_selecting_claude_without_a_key_falls_back_to_the_mock() -> None:
    """The application must start half-configured; it must not pretend to be configured."""
    provider = get_llm_provider(settings=_settings(anthropic_api_key=None))
    assert isinstance(provider, MockLLMProvider)
    assert provider.name == MOCK


def test_selecting_claude_with_a_key_returns_the_claude_provider() -> None:
    provider = get_llm_provider(settings=_settings())
    assert isinstance(provider, AnthropicProvider)
    assert provider.name == CLAUDE
    assert provider.model == "claude-opus-5"


def test_provider_health_reports_the_fallback_loudly_and_with_a_reason() -> None:
    """What the sidebar badge and the Settings panel are rendered from."""
    health = provider_health(settings=_settings(anthropic_api_key=None))

    assert health["configured_provider"] == CLAUDE
    assert health["active_provider"] == MOCK
    assert health["fell_back_to_mock"] is True
    assert health["fallback_reason"], "a silent fallback is the failure this guards against"
    assert "ANTHROPIC_API_KEY" in health["fallback_reason"]
    assert health["providers"][CLAUDE]["api_key_configured"] is False


def test_provider_health_never_serialises_the_key_even_when_claude_is_live() -> None:
    """It is returned by ``/health`` over HTTP, so this is the widest exposure surface."""
    health = provider_health(settings=_settings())
    serialised = json.dumps(health)

    assert FAKE_KEY not in serialised
    assert "sk-ant" not in serialised
    assert health["active_provider"] == CLAUDE
    assert health["fell_back_to_mock"] is False
    assert health["providers"][CLAUDE]["api_key_configured"] is True
