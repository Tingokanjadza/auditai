"""The assessment engine, and the behaviours the research actually turns on.

Four of these tests are the study in miniature, and they are written against the
synthetic datasets whose correct answer is known:

* a privileged listing with **no MFA column at all** must produce INSUFFICIENT_EVIDENCE
  and *name* what is missing, not a verdict (DATASET-005);
* the same control against a listing that does report MFA, with 10 of 100 disabled, must
  produce POTENTIAL_DEFICIENCY with verified citations - and must not claim the listing
  it was given is missing (DATASET-001);
* a policy requiring a minimum password length of 14 against a configuration showing 8
  must produce NOT_EFFECTIVE (DATASET-003);
* a model that builds an EFFECTIVE conclusion on a fabricated citation must be caught by
  the mode-C rails and left uncorrected in mode B, because the difference between those
  two outcomes *is* the experiment.

The remaining tests guard the properties that make the comparison between modes
meaningful at all: A performs no retrieval, A and B are never railed, and a failed run is
recorded rather than lost.
"""

from __future__ import annotations

import json

import pytest

from app.audit import service
from app.audit.engine import (
    ENGINE_VERSION,
    NO_RETRIEVAL,
    AssessmentEngine,
    AssessmentRunResult,
    assess,
)
from app.audit.service import NotFoundError
from app.schemas.enums import (
    AssessmentStatus,
    CitationVerdict,
    EvidenceSufficiency,
    ExperimentMode,
    RiskLevel,
)

RETRIEVING_MODES = [ExperimentMode.B_RAG, ExperimentMode.C_RAG_WORKFLOW]
ALL_MODES = list(ExperimentMode)


# ==========================================================================
# The research behaviours
# ==========================================================================
@pytest.mark.parametrize("mode", RETRIEVING_MODES, ids=lambda m: m.value)
def test_a_listing_with_no_mfa_column_yields_insufficient_evidence(
    seeded_session, dataset_project, engine_factory, mode
):
    """DATASET-005. The attribute under test is simply absent from the evidence.

    "We cannot tell from this" must be expressible and must not collapse into a
    compliance verdict - this is the premise of the whole system.
    """
    dataset, project = dataset_project("DATASET-005")
    result = engine_factory().assess_control(project.id, dataset.control_ref, mode=mode)

    assert result.output.status is AssessmentStatus.INSUFFICIENT_EVIDENCE
    assert result.output.status is not AssessmentStatus.EFFECTIVE
    assert result.error == ""


@pytest.mark.parametrize("mode", RETRIEVING_MODES, ids=lambda m: m.value)
def test_the_missing_mfa_artefact_is_named_not_merely_implied(
    seeded_session, dataset_project, engine_factory, mode
):
    """A bare "insufficient evidence" tells an auditor nothing about what to go and get."""
    dataset, project = dataset_project("DATASET-005")
    result = engine_factory().assess_control(project.id, dataset.control_ref, mode=mode)

    missing = result.output.missing_evidence
    assert missing, "the assessment must say what it needs"
    blob = " ".join(missing).lower()
    assert "mfa" in blob or "multi-factor" in blob
    assert any(
        term in blob for term in ("status", "enrolment", "enrollment", "export", "listing")
    ), "the named artefact must be specific enough to request: {0!r}".format(missing)


@pytest.mark.parametrize("mode", RETRIEVING_MODES, ids=lambda m: m.value)
def test_ten_of_a_hundred_disabled_yields_a_potential_deficiency_with_verified_citations(
    seeded_session, dataset_project, engine_factory, mode
):
    """DATASET-001: the same control, the same policy, but the attribute *is* reported."""
    dataset, project = dataset_project("DATASET-001")
    result = engine_factory().assess_control(project.id, dataset.control_ref, mode=mode)

    assert result.output.status is AssessmentStatus.POTENTIAL_DEFICIENCY
    assert result.validation.total > 0
    assert result.validation.verified > 0
    assert result.validation.fabricated == 0
    assert result.validation.grounding_rate_strict > 0.0
    assert set(result.output.cited_chunk_ids()) <= set(result.retrieval.chunk_ids)


@pytest.mark.parametrize("mode", RETRIEVING_MODES, ids=lambda m: m.value)
def test_the_supplied_listing_is_not_reported_as_missing_evidence(
    seeded_session, dataset_project, engine_factory, mode
):
    """Asking the auditor for the file they just uploaded destroys trust in the tool."""
    dataset, project = dataset_project("DATASET-001")
    control = service.require_control(seeded_session, dataset.control_ref)
    result = engine_factory().assess_control(project.id, dataset.control_ref, mode=mode)

    # expected_evidence[0] is the privileged account listing with MFA_Status - the file
    # that was actually supplied for this dataset.
    listing_artefact = control.expected_evidence[0]
    assert "MFA_Status" in listing_artefact, "the control library entry has changed shape"
    assert listing_artefact not in result.output.missing_evidence
    assert not any("MFA_Status" in item for item in result.output.missing_evidence)
    supplied = {f.filename for f in service.list_evidence(seeded_session, project_id=project.id)}
    for name in supplied:
        assert not any(name in item for item in result.output.missing_evidence)


@pytest.mark.parametrize("mode", RETRIEVING_MODES, ids=lambda m: m.value)
def test_a_configured_value_below_its_policy_threshold_is_not_effective(
    seeded_session, dataset_project, engine_factory, mode
):
    """DATASET-003: policy requires a minimum length of 14; the domain shows 8."""
    dataset, project = dataset_project("DATASET-003")
    result = engine_factory().assess_control(project.id, dataset.control_ref, mode=mode)

    assert result.output.status is AssessmentStatus.NOT_EFFECTIVE
    narrative = " ".join([result.output.finding, result.output.assessment, result.output.reasoning])
    assert "14" in narrative and "8" in narrative
    assert result.validation.verified > 0, "a definite conclusion must be grounded"


# --------------------------------------------------------- fabricated citations
def _fabricating_payloads(control_ref="CONTROL-001"):
    """A model that reaches a clean verdict on a citation pointing nowhere."""
    return {
        "sufficiency": {
            "evidence_sufficiency": "SUFFICIENT",
            "present_evidence": ["Privileged account listing"],
            "missing_evidence": [],
            "can_conclude": True,
            "rationale": "The listing reports enrolment status.",
        },
        "assessment": {
            "control_id": control_ref,
            "control_requirement": "Every privileged account must use a second factor.",
            "assessment": "Every privileged account is enrolled in MFA.",
            "status": "EFFECTIVE",
            "finding": "No finding identified.",
            "risk": "None arising.",
            "risk_level": "LOW",
            "evidence": [
                {
                    "chunk_id": 424242,
                    "filename": "Never_Retrieved.csv",
                    "locator": "row 9",
                    "quoted_text": "All privileged accounts show MFA_Status = Enabled.",
                    "relevance": "Proves universal enrolment.",
                    "supports": "observation",
                }
            ],
            "evidence_sufficiency": "SUFFICIENT",
            "missing_evidence": [],
            "reasoning": "The listing shows universal enrolment.",
            "inferences": [],
            "human_verification_required": [],
            "recommendation": "None.",
            "confidence": "HIGH",
            "human_review_required": False,
            "limitations": "",
        },
        "critique": {
            "unsupported_claims": [],
            "fabricated_citations": [],
            "overstated_conclusion": False,
            "recommended_status": None,
            "notes": "Looks fine to me.",
        },
    }


def test_a_fabricated_citation_is_recorded_as_fabricated(
    seeded_session, dataset_project, engine_factory, scripted_llm
):
    dataset, project = dataset_project("DATASET-001")
    engine = engine_factory(llm=scripted_llm(_fabricating_payloads()))
    result = engine.assess_control(project.id, dataset.control_ref, mode=ExperimentMode.C_RAG_WORKFLOW)

    assert result.validation.fabricated == 1
    assert result.validation.citations[0].verdict is CitationVerdict.FABRICATED
    assert result.validation.has_hallucination is True

    # Persisted with a NULL chunk_id: that NULL *is* the fabrication signal downstream.
    row = service.require_assessment(seeded_session, result.assessment_id)
    assert [c.verdict for c in row.citations] == [CitationVerdict.FABRICATED.value]
    assert row.citations[0].chunk_id is None
    assert row.citations[0].quoted_text, "the fabricated quote is kept, not deleted"


def test_mode_c_withdraws_an_effective_conclusion_built_on_a_fabricated_citation(
    seeded_session, dataset_project, engine_factory, scripted_llm
):
    dataset, project = dataset_project("DATASET-001")
    engine = engine_factory(llm=scripted_llm(_fabricating_payloads()))
    result = engine.assess_control(project.id, dataset.control_ref, mode=ExperimentMode.C_RAG_WORKFLOW)

    assert result.model_output.status is AssessmentStatus.EFFECTIVE, "the model's answer is preserved"
    assert result.output.status is AssessmentStatus.INSUFFICIENT_EVIDENCE
    assert result.output.evidence_sufficiency is EvidenceSufficiency.INSUFFICIENT
    assert result.rails_enforced is True
    assert result.validation.status_downgraded is True
    assert result.validation.original_status == AssessmentStatus.EFFECTIVE.value

    row = service.require_assessment(seeded_session, result.assessment_id)
    assert row.status == AssessmentStatus.INSUFFICIENT_EVIDENCE.value


def test_mode_b_is_deliberately_left_unrailed(
    seeded_session, dataset_project, engine_factory, scripted_llm
):
    """Railing the baseline would erase the difference the experiment exists to measure.

    The validator still runs, so the ungroundedness is *recorded* - it is simply not
    corrected.
    """
    dataset, project = dataset_project("DATASET-001")
    engine = engine_factory(llm=scripted_llm(_fabricating_payloads()))
    result = engine.assess_control(project.id, dataset.control_ref, mode=ExperimentMode.B_RAG)

    assert result.output.status is AssessmentStatus.EFFECTIVE
    assert result.rails_enforced is False
    assert result.validation.fabricated == 1, "the failure is measured even though it stands"
    assert result.telemetry["rails_note"]


def test_human_review_is_forced_even_when_the_model_says_otherwise(
    seeded_session, dataset_project, engine_factory, scripted_llm
):
    """The scripted model answers ``human_review_required: false`` in every mode."""
    dataset, project = dataset_project("DATASET-001")
    for mode in ALL_MODES:
        engine = engine_factory(llm=scripted_llm(_fabricating_payloads()))
        result = engine.assess_control(project.id, dataset.control_ref, mode=mode)
        assert result.output.human_review_required is True
        row = service.require_assessment(seeded_session, result.assessment_id)
        assert row.human_review_required is True


# ------------------------------------------------------------------ self-critique
def test_a_critique_downgrade_is_applied_only_when_the_validator_agrees(
    seeded_session, dataset_project, engine_factory, scripted_llm
):
    """A model must not be able to talk itself out of a correct, well-cited finding."""
    dataset, project = dataset_project("DATASET-001")

    # First establish what a well-grounded mode-C answer looks like, then re-use its
    # citations in a scripted answer whose critique asks for a downgrade.
    grounded = engine_factory().assess_control(
        project.id, dataset.control_ref, mode=ExperimentMode.C_RAG_WORKFLOW, persist=False
    )
    assert grounded.validation.verified > 0

    payloads = _fabricating_payloads(dataset.control_ref)
    payloads["assessment"]["status"] = "POTENTIAL_DEFICIENCY"
    payloads["assessment"]["evidence"] = [
        json.loads(c.model_dump_json()) for c in grounded.output.evidence
    ]
    payloads["critique"] = {
        "unsupported_claims": [],
        "fabricated_citations": [],
        "overstated_conclusion": True,
        "recommended_status": "INSUFFICIENT_EVIDENCE",
        "notes": "On reflection I am not sure.",
    }

    engine = engine_factory(llm=scripted_llm(payloads))
    result = engine.assess_control(project.id, dataset.control_ref, mode=ExperimentMode.C_RAG_WORKFLOW)

    assert result.output.status is AssessmentStatus.POTENTIAL_DEFICIENCY, "the finding stands"
    assert any("critique_downgrade_declined" in r for r in result.validation.rails_applied)


def test_a_conclusion_drawn_from_no_retrieved_evidence_is_withdrawn_in_mode_c(
    seeded_session, project, engine_factory, scripted_llm
):
    """A project with no evidence at all: any verdict is unsupported by construction."""
    payloads = _fabricating_payloads()
    payloads["assessment"]["status"] = "POTENTIAL_DEFICIENCY"
    payloads["assessment"]["evidence"] = []
    engine = engine_factory(llm=scripted_llm(payloads))
    result = engine.assess_control(project.id, "CONTROL-001", mode=ExperimentMode.C_RAG_WORKFLOW)

    assert result.retrieval.chunks == []
    assert result.output.status is AssessmentStatus.INSUFFICIENT_EVIDENCE
    assert any("no_evidence_retrieved" in r for r in result.validation.rails_applied)
    assert result.telemetry["no_evidence_note"]


# ==========================================================================
# Failure is recorded, never swallowed
# ==========================================================================
@pytest.mark.parametrize("mode", ALL_MODES, ids=lambda m: m.value)
def test_every_mode_persists_an_assessment_when_the_provider_fails(
    seeded_session, dataset_project, engine_factory, broken_llm, mode
):
    """An audit run that quietly loses a control is worse than one that records a failure."""
    dataset, project = dataset_project("DATASET-001")
    result = engine_factory(llm=broken_llm).assess_control(project.id, dataset.control_ref, mode=mode)

    assert result.assessment_id is not None
    assert broken_llm.calls >= 1, "the provider must actually have been called"
    assert result.error
    assert "simulated provider outage" in result.error

    row = service.require_assessment(seeded_session, result.assessment_id)
    assert row.status == AssessmentStatus.INSUFFICIENT_EVIDENCE.value
    assert row.human_review_required is True
    assert row.error and "simulated provider outage" in row.error
    assert row.experiment_mode == mode.value
    assert row.risk_level == RiskLevel.NOT_RATED.value, "no band is put next to an unassessed control"


def test_a_failed_run_says_so_in_words_an_auditor_reads(
    seeded_session, dataset_project, engine_factory, broken_llm
):
    dataset, project = dataset_project("DATASET-001")
    result = engine_factory(llm=broken_llm).assess_control(
        project.id, dataset.control_ref, mode=ExperimentMode.B_RAG
    )
    assert "No assessment was produced" in result.output.assessment
    assert any("NOT assessed" in item for item in result.output.human_verification_required)
    assert result.telemetry["completed"] is False


def test_the_technical_error_is_kept_out_of_the_narrative_fields(
    seeded_session, dataset_project, engine_factory, scripted_llm
):
    """Error text carries incidental numbers; the claim scanner would flag them."""
    dataset, project = dataset_project("DATASET-001")
    engine = engine_factory(llm=scripted_llm({"assessment": "this is not JSON at all"}))
    result = engine.assess_control(project.id, dataset.control_ref, mode=ExperimentMode.B_RAG)
    assert result.error
    assert result.validation.unsupported_claims == []


def test_assess_project_records_a_failure_per_control_and_keeps_going(
    seeded_session, ingested_project, engine_factory, broken_llm
):
    service.scope_controls(seeded_session, ingested_project.id, ["CONTROL-005"])
    results = engine_factory(llm=broken_llm).assess_project(
        ingested_project.id, mode=ExperimentMode.B_RAG
    )
    assert len(results) == 2
    assert all(r.assessment_id is not None for r in results)
    assert all(r.error for r in results)


# ==========================================================================
# What distinguishes the modes
# ==========================================================================
def test_mode_a_performs_no_retrieval_and_records_that_fact(
    seeded_session, dataset_project, engine_factory
):
    dataset, project = dataset_project("DATASET-001")
    result = engine_factory().assess_control(
        project.id, dataset.control_ref, mode=ExperimentMode.A_RAW_LLM
    )

    assert result.retrieval.strategy == NO_RETRIEVAL
    assert result.retrieval.queries == []
    assert any("performs no retrieval" in note for note in result.retrieval.notes)

    row = service.require_assessment(seeded_session, result.assessment_id)
    assert row.retrieval_strategy == NO_RETRIEVAL
    assert row.retrieval_top_k == 0
    assert row.retrieval_queries == []


def test_mode_a_is_still_measured_against_the_text_it_was_shown(
    seeded_session, dataset_project, engine_factory
):
    """Scoring A against an empty set would report a 100% hallucination rate that
    measures the prompt format rather than the model."""
    dataset, project = dataset_project("DATASET-001")
    result = engine_factory().assess_control(
        project.id, dataset.control_ref, mode=ExperimentMode.A_RAW_LLM
    )
    assert result.retrieval.chunks, "the raw blob's chunks are recorded so citations stay checkable"
    assert result.validation.total > 0
    assert result.telemetry["raw_evidence"]["chunks_shown"] >= 1
    assert result.telemetry["raw_evidence_note"]


@pytest.mark.parametrize(
    "mode,expected_calls",
    [(ExperimentMode.A_RAW_LLM, 1), (ExperimentMode.B_RAG, 1), (ExperimentMode.C_RAG_WORKFLOW, 3)],
    ids=lambda v: str(v),
)
def test_the_workflow_costs_exactly_the_calls_it_claims(
    seeded_session, dataset_project, engine_factory, mode, expected_calls
):
    """C is sufficiency + assessment + critique; A and B are a single call each."""
    dataset, project = dataset_project("DATASET-001")
    result = engine_factory().assess_control(project.id, dataset.control_ref, mode=mode)
    assert result.llm_calls == expected_calls
    assert len([s for s in result.steps if s.kind == "llm"]) == expected_calls


def test_mode_c_runs_the_sufficiency_pre_check_and_the_self_critique(
    seeded_session, dataset_project, engine_factory
):
    dataset, project = dataset_project("DATASET-005")
    result = engine_factory().assess_control(
        project.id, dataset.control_ref, mode=ExperimentMode.C_RAG_WORKFLOW
    )
    assert result.sufficiency is not None
    assert result.critique is not None
    assert result.sufficiency.can_conclude is False
    assert result.sufficiency.missing_evidence


def test_the_workflow_steps_run_in_the_documented_order(
    seeded_session, dataset_project, engine_factory, scripted_llm
):
    """Sufficiency pre-check, then the assessment, then the self-critique of that draft."""
    dataset, project = dataset_project("DATASET-001")
    provider = scripted_llm(_fabricating_payloads(dataset.control_ref))
    engine_factory(llm=provider).assess_control(
        project.id, dataset.control_ref, mode=ExperimentMode.C_RAG_WORKFLOW
    )
    assert provider.purposes == ["sufficiency", "assessment", "critique"]


@pytest.mark.parametrize(
    "mode,expected_purposes",
    [
        (ExperimentMode.A_RAW_LLM, ["assessment"]),
        (ExperimentMode.B_RAG, ["assessment"]),
    ],
    ids=lambda v: str(v),
)
def test_the_single_call_modes_ask_for_nothing_else(
    seeded_session, dataset_project, engine_factory, scripted_llm, mode, expected_purposes
):
    dataset, project = dataset_project("DATASET-001")
    provider = scripted_llm(_fabricating_payloads(dataset.control_ref))
    engine_factory(llm=provider).assess_control(project.id, dataset.control_ref, mode=mode)
    assert provider.purposes == expected_purposes


@pytest.mark.parametrize("mode", [ExperimentMode.A_RAW_LLM, ExperimentMode.B_RAG], ids=lambda m: m.value)
def test_the_unrailed_modes_report_the_model_answer_verbatim(
    seeded_session, dataset_project, engine_factory, mode
):
    dataset, project = dataset_project("DATASET-001")
    result = engine_factory().assess_control(project.id, dataset.control_ref, mode=mode)
    assert result.rails_enforced is False
    assert result.output.status is result.model_output.status
    assert result.validation.status_downgraded is False


def test_latency_separates_retrieval_from_inference(seeded_session, dataset_project, engine_factory):
    """A mode that spends time retrieving is not slower *at inference* than one that
    does not, and conflating the two would misattribute the cost of the workflow."""
    dataset, project = dataset_project("DATASET-001")
    result = engine_factory().assess_control(
        project.id, dataset.control_ref, mode=ExperimentMode.C_RAG_WORKFLOW
    )
    latency = result.telemetry["latency"]
    assert latency["total_ms"] >= 0
    assert latency["llm_ms"] >= 0
    assert latency["retrieval_ms"] >= 0
    assert latency["llm_ms"] + latency["retrieval_ms"] <= latency["total_ms"] + 1
    assert "definition" in latency


# ==========================================================================
# Persistence and the run record
# ==========================================================================
def test_a_persisted_assessment_carries_the_whole_research_record(
    seeded_session, dataset_project, engine_factory, settings
):
    dataset, project = dataset_project("DATASET-001")
    result = engine_factory().assess_control(
        project.id, dataset.control_ref, mode=ExperimentMode.C_RAG_WORKFLOW
    )
    row = service.require_assessment(seeded_session, result.assessment_id)

    assert row.control_ref == dataset.control_ref
    assert row.experiment_mode == ExperimentMode.C_RAG_WORKFLOW.value
    assert row.llm_provider == "mock"
    assert row.retrieval_strategy == settings.retrieval_strategy
    assert row.retrieval_top_k == settings.retrieval_top_k
    assert row.retrieved_chunk_ids == list(result.retrieval.chunk_ids)
    assert row.retrieval_queries
    assert row.prompt_snapshot and row.raw_response
    assert row.risk_factors["model_label"]
    assert row.validation_report["engine"]["engine_version"] == ENGINE_VERSION
    assert row.validation_report["engine"]["prompt_version"]


def test_citations_are_persisted_as_rows_pointing_at_real_chunks(
    seeded_session, dataset_project, engine_factory
):
    dataset, project = dataset_project("DATASET-001")
    result = engine_factory().assess_control(
        project.id, dataset.control_ref, mode=ExperimentMode.C_RAG_WORKFLOW
    )
    row = service.require_assessment(seeded_session, result.assessment_id)
    assert row.citations
    for citation in row.citations:
        assert citation.chunk_id is not None
        assert citation.chunk is not None
        assert citation.quoted_text in citation.chunk.text
        assert citation.locator_text
        assert citation.verdict in CitationVerdict.values()


def test_the_run_is_written_to_the_activity_trail(seeded_session, dataset_project, engine_factory):
    dataset, project = dataset_project("DATASET-001")
    result = engine_factory().assess_control(project.id, dataset.control_ref, mode=ExperimentMode.B_RAG)
    entries = service.list_activity(seeded_session, project_id=project.id, entity_type="assessment")
    assert entries
    entry = entries[0]
    assert entry.actor_type == "AI"
    assert entry.details["control_ref"] == dataset.control_ref
    assert entry.details["mode"] == ExperimentMode.B_RAG.value


def test_persist_false_writes_nothing_at_all(seeded_session, dataset_project, engine_factory):
    dataset, project = dataset_project("DATASET-001")
    result = engine_factory().assess_control(
        project.id, dataset.control_ref, mode=ExperimentMode.B_RAG, persist=False
    )
    assert result.assessment_id is None
    assert result.output.status is AssessmentStatus.POTENTIAL_DEFICIENCY
    assert service.list_assessments(seeded_session, project_id=project.id) == []


def test_result_to_dict_is_json_serialisable(seeded_session, dataset_project, engine_factory):
    dataset, project = dataset_project("DATASET-001")
    result = engine_factory().assess_control(
        project.id, dataset.control_ref, mode=ExperimentMode.B_RAG, persist=False
    )
    payload = result.to_dict()
    assert json.loads(json.dumps(payload))
    assert payload["mode"] == ExperimentMode.B_RAG.value
    assert payload["validation"]["total"] == result.validation.total
    assert payload["retrieval"]["chunk_ids"] == list(result.retrieval.chunk_ids)


def test_assess_project_covers_every_scoped_control(seeded_session, ingested_project, engine_factory):
    service.scope_controls(seeded_session, ingested_project.id, ["CONTROL-005"])
    results = engine_factory().assess_project(ingested_project.id, mode=ExperimentMode.B_RAG)
    assert {r.control_ref for r in results} == {"CONTROL-001", "CONTROL-005"}
    assert all(isinstance(r, AssessmentRunResult) for r in results)


def test_the_one_liner_helper_matches_the_engine(seeded_session, dataset_project, settings):
    dataset, project = dataset_project("DATASET-001")
    result = assess(
        seeded_session,
        project.id,
        dataset.control_ref,
        mode=ExperimentMode.B_RAG,
        persist=False,
        settings=settings,
    )
    assert result.output.status is AssessmentStatus.POTENTIAL_DEFICIENCY


# ------------------------------------------------------------------- bad requests
def test_an_unknown_project_raises_because_there_is_nothing_to_attach_a_result_to(
    seeded_session, engine_factory
):
    with pytest.raises(NotFoundError):
        engine_factory().assess_control(987654, "CONTROL-001")


def test_an_unknown_control_raises(seeded_session, project, engine_factory):
    with pytest.raises(NotFoundError):
        engine_factory().assess_control(project.id, "CONTROL-DOES-NOT-EXIST")


def test_an_unrecognised_mode_falls_back_to_the_full_workflow(
    seeded_session, dataset_project, engine_factory
):
    dataset, project = dataset_project("DATASET-001")
    result = engine_factory().assess_control(
        project.id, dataset.control_ref, mode="not-a-mode", persist=False
    )
    assert result.mode is ExperimentMode.C_RAG_WORKFLOW


def test_the_engine_builds_its_retriever_lazily(seeded_session, dataset_project, settings):
    """Experiment A must never construct an index it will not use."""
    dataset, project = dataset_project("DATASET-001")
    engine = AssessmentEngine(seeded_session, settings=settings)
    assert engine._retriever is None
    engine.assess_control(project.id, dataset.control_ref, mode=ExperimentMode.A_RAW_LLM, persist=False)
    assert engine._retriever is None
    engine.assess_control(project.id, dataset.control_ref, mode=ExperimentMode.B_RAG, persist=False)
    assert engine._retriever is not None
