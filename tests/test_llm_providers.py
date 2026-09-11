"""Providers: the offline mock, the unreachable remote, and the factory between them.

Two things are proved here rather than assumed. First, that this suite never constructs
a usable OpenAI client - no key, no base URL, and a client build that raises rather than
reaching the network. Second, that the mock's citations quote text that is genuinely in
the chunk they point at, which is the property every grounding measurement in the study
depends on.
"""

from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.llm.base import (
    LLMCallContext,
    LLMError,
    LLMNotConfiguredError,
    LLMResponse,
    estimate_tokens,
    extract_json,
)
from app.llm.factory import (
    MOCK,
    OPENAI,
    available_providers,
    get_llm_provider,
    normalise_provider_name,
    provider_health,
)
from app.llm.mock_provider import MOCK_DISCLAIMER, MOCK_RULES_VERSION, MockLLMProvider
from app.llm.openai_provider import OpenAICompatibleProvider
from app.rag.query_builder import build_queries
from app.rag.retriever import HybridRetriever
from app.schemas.assessment import AssessmentOutput, SelfCritique, SufficiencyCheck
from app.schemas.enums import AssessmentStatus


# ------------------------------------------------------- no network, no credentials
def test_the_factory_hands_back_the_offline_mock(settings):
    provider = get_llm_provider(settings=settings)
    assert isinstance(provider, MockLLMProvider)
    assert provider.name == MOCK
    assert provider.is_available() is True


def test_the_openai_provider_is_never_constructed_with_a_real_key(settings):
    """Constructing it is harmless; *using* it must be impossible in this environment."""
    provider = OpenAICompatibleProvider(settings=settings)
    assert provider.api_key in (None, "")
    assert provider.base_url in (None, "")
    assert provider.is_available() is False
    with pytest.raises(LLMNotConfiguredError):
        provider._get_client()


def test_the_guard_against_building_a_remote_client_is_armed(no_remote_llm_client):
    """Not a fact about configuration: an actual attempt to build a client must fail.

    Constructed with an explicit key and base URL so nothing else can stop it before the
    SDK client is reached.
    """
    provider = OpenAICompatibleProvider(
        model="gpt-4o-mini", api_key="sk-test-only", base_url="http://127.0.0.1:9/v1"
    )
    assert provider.is_available() is True, "the fallback is not what is being tested here"
    with pytest.raises(no_remote_llm_client):
        provider._get_client()


def test_selecting_openai_without_credentials_falls_back_to_the_mock(settings):
    unconfigured = settings.model_copy(update={"llm_provider": "openai"})
    provider = get_llm_provider(settings=unconfigured)
    assert isinstance(provider, MockLLMProvider), "an unconfigured remote must not be returned"


def test_provider_health_reports_the_fallback_and_masks_the_key():
    configured = Settings(llm_provider="openai", llm_api_key="sk-super-secret-value")
    health = provider_health(settings=configured)
    assert "sk-super-secret-value" not in json.dumps(health)
    assert health["configured_provider"] == OPENAI
    assert health["active_provider"] == OPENAI
    assert health["fell_back_to_mock"] is False

    unconfigured = Settings(llm_provider="openai", llm_api_key=None, llm_base_url=None)
    fallen = provider_health(settings=unconfigured)
    assert fallen["active_provider"] == MOCK
    assert fallen["fell_back_to_mock"] is True
    assert fallen["fallback_reason"]


def test_provider_health_makes_no_network_call(settings):
    """It is rendered on every page load, so it must be configuration-only."""
    health = provider_health(settings=settings)
    assert health["providers"][OPENAI]["available"] is False
    assert "no network call" in health["providers"][OPENAI]["note"].lower()


@pytest.mark.parametrize(
    "given,expected",
    [
        (None, MOCK),
        ("", MOCK),
        ("mock", MOCK),
        ("OFFLINE", MOCK),
        ("openai", OPENAI),
        ("Ollama", OPENAI),
        ("vllm", OPENAI),
        ("openrouter", OPENAI),
        ("a provider nobody has heard of", MOCK),
    ],
)
def test_provider_name_normalisation_never_raises(given, expected):
    assert normalise_provider_name(given) == expected


def test_available_providers():
    assert available_providers() == [MOCK, OPENAI]


# ------------------------------------------------------------------ the mock itself
def test_the_mock_is_deterministic(mock_llm):
    first = mock_llm.complete("system", "assess CONTROL-001", context=LLMCallContext(purpose="assessment"))
    second = mock_llm.complete("system", "assess CONTROL-001", context=LLMCallContext(purpose="assessment"))
    assert first.text == second.text


def test_the_mock_labels_itself_as_a_rule_based_stand_in(mock_llm):
    response = mock_llm.complete("s", "u", context=LLMCallContext(purpose="assessment"))
    assert response.provider == "mock"
    assert response.raw["rules_version"] == MOCK_RULES_VERSION
    assert response.raw["disclaimer"] == MOCK_DISCLAIMER
    assert "not a language model" in MOCK_DISCLAIMER.lower() or "rule-based" in MOCK_DISCLAIMER.lower()


def test_the_mock_reports_telemetry_the_engine_measures(mock_llm):
    response = mock_llm.complete("system prompt", "user prompt", context=LLMCallContext())
    assert isinstance(response, LLMResponse)
    assert response.prompt_tokens > 0
    assert response.completion_tokens > 0
    assert response.total_tokens == response.prompt_tokens + response.completion_tokens
    assert response.finish_reason == "stop"


@pytest.mark.parametrize(
    "purpose,model_cls",
    [
        ("assessment", AssessmentOutput),
        ("sufficiency", SufficiencyCheck),
        ("critique", SelfCritique),
    ],
)
def test_the_mock_answers_in_the_shape_each_workflow_step_expects(mock_llm, purpose, model_cls):
    response = mock_llm.complete("s", "u", context=LLMCallContext(purpose=purpose))
    payload = json.loads(response.text)
    model_cls.model_validate(payload)


def _retrieve(session, project, control, top_k=12):
    return HybridRetriever(session).retrieve(build_queries(control), project.id, top_k=top_k)


def test_mock_citations_quote_text_that_is_literally_in_the_chunk(
    seeded_session, ingested_project, control, mock_llm
):
    """The property every grounding figure in this study rests on.

    Not "similar to", not "paraphrased from": the quoted characters are a substring of
    the chunk the citation points at. If this ever stops holding, the citation
    validator's VERIFIED verdicts stop meaning what the write-up says they mean.
    """
    retrieval = _retrieve(seeded_session, ingested_project, control)
    response = mock_llm.complete(
        "system",
        "user",
        context=LLMCallContext(
            purpose="assessment",
            control={"control_id": control.control_id, "name": control.name,
                     "objective": control.objective,
                     "expected_evidence": list(control.expected_evidence),
                     "assessment_criteria": list(control.assessment_criteria),
                     "retrieval_keywords": list(control.retrieval_keywords)},
            chunks=list(retrieval.chunks),
            extras={"retrieval_performed": True},
        ),
    )
    output = AssessmentOutput.model_validate(json.loads(response.text))
    assert output.evidence, "the mock must cite something when evidence was supplied"
    for citation in output.evidence:
        assert citation.chunk_id in retrieval.chunk_ids
        chunk = retrieval.by_id(citation.chunk_id)
        assert citation.quoted_text
        assert citation.quoted_text in chunk.text, (
            "citation {0} quotes text that is not in chunk {1}".format(
                citation.quoted_text[:60], citation.chunk_id
            )
        )


def test_the_mock_never_cites_a_chunk_it_was_not_given(
    seeded_session, ingested_project, control, mock_llm
):
    retrieval = _retrieve(seeded_session, ingested_project, control)
    response = mock_llm.complete(
        "s",
        "u",
        context=LLMCallContext(
            purpose="assessment",
            control={"control_id": control.control_id, "name": control.name},
            chunks=list(retrieval.chunks),
            extras={"retrieval_performed": True},
        ),
    )
    output = AssessmentOutput.model_validate(json.loads(response.text))
    assert set(output.cited_chunk_ids()) <= set(retrieval.chunk_ids)


def test_the_mock_declines_to_conclude_when_the_attribute_is_absent(
    seeded_session, dataset_project, mock_llm
):
    """DATASET-005: a privileged listing with no MFA column at all."""
    from app.audit import service

    dataset, project = dataset_project("DATASET-005")
    control = service.require_control(seeded_session, dataset.control_ref)
    retrieval = _retrieve(seeded_session, project, control)
    response = mock_llm.complete(
        "s",
        "u",
        context=LLMCallContext(
            purpose="assessment",
            control={
                "control_id": control.control_id,
                "name": control.name,
                "objective": control.objective,
                "expected_evidence": list(control.expected_evidence),
                "assessment_criteria": list(control.assessment_criteria),
                "retrieval_keywords": list(control.retrieval_keywords),
            },
            chunks=list(retrieval.chunks),
            extras={"retrieval_performed": True},
        ),
    )
    output = AssessmentOutput.model_validate(json.loads(response.text))
    assert output.status is AssessmentStatus.INSUFFICIENT_EVIDENCE
    assert output.missing_evidence


def test_the_mock_reports_an_exception_count_only_beside_its_own_population(
    seeded_session, ingested_project, control, mock_llm
):
    """A rate is meaningless unless numerator and denominator count the same thing."""
    retrieval = _retrieve(seeded_session, ingested_project, control)
    response = mock_llm.complete(
        "s",
        "u",
        context=LLMCallContext(
            purpose="assessment",
            control={
                "control_id": control.control_id,
                "name": control.name,
                "objective": control.objective,
                "expected_evidence": list(control.expected_evidence),
                "assessment_criteria": list(control.assessment_criteria),
                "retrieval_keywords": list(control.retrieval_keywords),
            },
            chunks=list(retrieval.chunks),
            extras={"retrieval_performed": True},
        ),
    )
    raw = response.raw
    assert (raw["exception_count"] is None) == (raw["population_total"] is None)
    assert raw["exception_count_basis"]


def test_the_hallucination_lever_actually_injects_something(
    seeded_session, ingested_project, control, settings
):
    """The research lever must be observable, or it cannot exercise the validator."""
    from app.audit.validators import detect_unsupported_claims, validate_citations

    loud = settings.model_copy(update={"mock_hallucination_rate": 1.0})
    provider = MockLLMProvider(settings=loud)
    retrieval = _retrieve(seeded_session, ingested_project, control)
    response = provider.complete(
        "s",
        "u",
        context=LLMCallContext(
            purpose="assessment",
            control={
                "control_id": control.control_id,
                "name": control.name,
                "objective": control.objective,
                "expected_evidence": list(control.expected_evidence),
                "assessment_criteria": list(control.assessment_criteria),
                "retrieval_keywords": list(control.retrieval_keywords),
            },
            chunks=list(retrieval.chunks),
            extras={"retrieval_performed": True},
        ),
    )
    assert response.raw["mock_injection"] is not None
    output = AssessmentOutput.model_validate(json.loads(response.text))
    report = validate_citations(output, retrieval)
    report.unsupported_claims = detect_unsupported_claims(output, retrieval, control)
    assert report.has_hallucination, "an injected hallucination must be mechanically detectable"


def test_the_default_configuration_injects_nothing(settings):
    assert settings.mock_hallucination_rate == 0.0
    response = MockLLMProvider(settings=settings).complete(
        "s", "u", context=LLMCallContext(purpose="assessment")
    )
    assert response.raw["mock_injection"] is None


def test_mock_health_states_it_is_offline(mock_llm):
    health = mock_llm.health()
    assert health["available"] is True
    assert health["offline"] is True
    assert health["rules_version"] == MOCK_RULES_VERSION


# -------------------------------------------------------------------- JSON recovery
@pytest.mark.parametrize(
    "text",
    [
        '{"status": "EFFECTIVE"}',
        '```json\n{"status": "EFFECTIVE"}\n```',
        'Here is my answer:\n{"status": "EFFECTIVE"}\nHope that helps.',
        '{"status": "EFFECTIVE",}',
        '{“status”: “EFFECTIVE”}',
        '{"status": "EFFECTIVE"} and then some trailing prose {incomplete',
        '[{"status": "EFFECTIVE"}]',
    ],
)
def test_extract_json_recovers_a_real_model_answer(text):
    assert extract_json(text)["status"] == "EFFECTIVE"


def test_extract_json_handles_python_literals():
    payload = extract_json('{"human_review_required": True, "recommended_status": None}')
    assert payload["human_review_required"] is True
    assert payload["recommended_status"] is None


@pytest.mark.parametrize("text", ["", "   ", "no json here at all", "{{{"])
def test_extract_json_raises_rather_than_guessing(text):
    with pytest.raises(LLMError):
        extract_json(text)


@pytest.mark.parametrize("text,expected", [("", 0), ("abcd", 1), ("a" * 400, 100)])
def test_estimate_tokens(text, expected):
    assert estimate_tokens(text) == expected
