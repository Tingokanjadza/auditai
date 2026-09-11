"""The experiment harness: the procedure that produces the research results.

The properties that matter here are experimental rather than functional. Each dataset
must get its own throwaway project, or DATASET-001's MFA export could be retrieved while
assessing DATASET-005 and the case the study turns on would stop being interpretable.
Every assessment must be tagged with its run id, or the experiments would leak into the
auditor's dashboard. A dataset that crashes must be recorded as a wrong answer rather
than dropped from the denominator. And the run must record enough configuration to be
re-run at all.

The whole-suite runs are marked ``slow``; the rest use two datasets.
"""

from __future__ import annotations

import json
import math

import pytest

from app.audit import service
from app.database.models import Assessment
from app.evaluation.datasets import get_dataset
from app.evaluation.runner import (
    DEFAULT_MODES,
    EVALUATION_PROJECT_PREFIX,
    EVALUATION_SCOPE_TAG,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_COMPLETED_WITH_ERRORS,
    RUN_STATUS_FAILED,
    RUNNER_VERSION,
    build_run_config,
    cleanup_evaluation_projects,
    compare_runs,
    evaluation_projects,
    get_run,
    is_evaluation_project,
    list_runs,
    prediction_matrix,
    prepare_datasets,
    result_to_dict,
    results_frame,
    run_all_experiments,
    run_experiment,
    run_results,
    run_to_dict,
)
from app.schemas.enums import AssessmentStatus, ExperimentMode

PAIR = ["DATASET-001", "DATASET-005"]


@pytest.fixture()
def prepared(generated_datasets):
    """The generated file paths, keyed by dataset id, in the shape the runner wants."""
    return {dataset_id: list(paths) for dataset_id, paths in generated_datasets.items()}


@pytest.fixture()
def pair_run(seeded_session, prepared):
    return run_experiment(
        seeded_session,
        ExperimentMode.C_RAG_WORKFLOW,
        dataset_ids=PAIR,
        run_name="unit pair",
        generated_files=prepared,
    )


# ------------------------------------------------------------------- one run
def test_a_run_scores_every_dataset_against_its_ground_truth(seeded_session, pair_run):
    assert pair_run.id is not None
    assert pair_run.status == RUN_STATUS_COMPLETED
    assert pair_run.experiment_mode == ExperimentMode.C_RAG_WORKFLOW.value
    assert pair_run.llm_provider == "mock"
    assert pair_run.dataset_ids == PAIR

    results = run_results(seeded_session, pair_run.id)
    assert {r.dataset_id for r in results} == set(PAIR)
    for result in results:
        dataset = get_dataset(result.dataset_id)
        assert result.expected_status == dataset.expected_status.value
        assert result.predicted_status
        assert result.status_correct is (result.predicted_status == result.expected_status)
        assert result.expected_evidence_total == len(dataset.key_evidence_markers)
        assert result.error is None


def test_the_two_headline_cases_come_out_right(seeded_session, pair_run):
    """DATASET-001 is a deficiency; DATASET-005 must decline to conclude."""
    by_id = {r.dataset_id: r for r in run_results(seeded_session, pair_run.id)}
    assert by_id["DATASET-001"].predicted_status == AssessmentStatus.POTENTIAL_DEFICIENCY.value
    assert by_id["DATASET-005"].predicted_status == AssessmentStatus.INSUFFICIENT_EVIDENCE.value
    assert by_id["DATASET-005"].declared_missing_evidence is True


def test_each_dataset_gets_its_own_isolated_project(seeded_session, pair_run):
    """Isolation is the experimental control, not tidiness: one shared project would let
    DATASET-001's MFA export be retrieved while assessing DATASET-005."""
    projects = evaluation_projects(seeded_session, run_id=pair_run.id)
    assert len(projects) == 2
    assert all(is_evaluation_project(p) for p in projects)
    assert all(p.name.startswith(EVALUATION_PROJECT_PREFIX) for p in projects)
    assert all(EVALUATION_SCOPE_TAG in (p.scope_note or "") for p in projects)

    for project in projects:
        files = service.list_evidence(seeded_session, project_id=project.id)
        assert files
        assert all(f.is_synthetic for f in files)


def test_every_assessment_is_tagged_with_its_run(seeded_session, pair_run):
    """The tag is what keeps the experiments out of an auditor's figures."""
    rows = seeded_session.query(Assessment).all()
    assert rows
    assert all(row.evaluation_run_id == pair_run.id for row in rows)
    assert service.list_assessments(seeded_session, include_evaluation=False) == []
    assert service.dashboard_stats(seeded_session)["controls_assessed"] == 0


def test_the_metric_set_is_stored_on_the_run(seeded_session, pair_run):
    metrics = pair_run.metrics
    assert json.loads(json.dumps(metrics))
    assert metrics["n"] == 2
    assert metrics["classification"]["accuracy"] == pytest.approx(1.0)
    assert metrics["evidence"]["citations_total"] > 0
    assert metrics["caveats"], "a six-case suite must carry its caveats"


def test_the_run_records_enough_to_be_reproduced(seeded_session, pair_run, settings):
    config = pair_run.config
    assert config["runner_version"] == RUNNER_VERSION
    assert config["experiment_mode"] == ExperimentMode.C_RAG_WORKFLOW.value
    assert config["llm"]["provider"] == "mock"
    assert config["mock"]["seed"] == settings.mock_seed
    assert config["retrieval"]["strategy"] == settings.retrieval_strategy
    assert config["embedding"]["dimension"] == settings.embedding_dim
    assert config["datasets"]["ids"] == PAIR
    assert config["datasets"]["manifest_sha256"]
    assert config["prompt_version"]


def test_the_run_config_never_records_a_secret(seeded_session, prepared, settings):
    from app.llm.mock_provider import MockLLMProvider

    loud = settings.model_copy(update={"llm_api_key": "sk-should-never-appear"})
    config = build_run_config(
        ExperimentMode.B_RAG,
        [get_dataset("DATASET-001")],
        MockLLMProvider(settings=loud),
        loud,
    )
    assert "sk-should-never-appear" not in json.dumps(config)
    assert config["llm"]["base_url_configured"] is False


def test_the_run_config_records_the_provider_that_actually_ran(seeded_session, settings):
    """A results table naming the *requested* provider after a silent fallback would be
    a false statement about how the numbers were obtained."""
    from app.llm.factory import get_llm_provider

    requested = settings.model_copy(update={"llm_provider": "openai"})
    provider = get_llm_provider(settings=requested)
    config = build_run_config(ExperimentMode.B_RAG, [get_dataset("DATASET-001")], provider, requested)
    assert config["llm"]["provider"] == "mock"
    assert config["llm"]["provider_requested"] == "openai"
    assert config["llm"]["fell_back_to_mock"] is True


def test_the_mock_disclaimer_travels_with_the_numbers(seeded_session, pair_run):
    note = pair_run.config["mock"]["note"].lower()
    assert "rule-based" in note
    assert "not a language model" in note


# ------------------------------------------------------------------ mode effects
@pytest.mark.parametrize("mode", list(ExperimentMode), ids=lambda m: m.value)
def test_every_mode_can_be_run(seeded_session, prepared, mode):
    run = run_experiment(
        seeded_session, mode, dataset_ids=["DATASET-001"], generated_files=prepared
    )
    assert run.experiment_mode == mode.value
    assert len(run.results) == 1
    assert run.config["retrieval"]["performed"] is mode.uses_retrieval


def test_mode_a_records_that_it_retrieved_nothing(seeded_session, prepared):
    run = run_experiment(
        seeded_session, ExperimentMode.A_RAW_LLM, dataset_ids=["DATASET-001"], generated_files=prepared
    )
    result = run.results[0]
    assert result.detail["retrieval"]["strategy"] == "NONE"
    assert run.config["retrieval"]["top_k"] == 0


def test_the_effect_of_the_rails_is_recoverable_from_the_row(seeded_session, prepared):
    """``detail["status"]["model"]`` keeps the model's unedited answer beside the one
    the system stands behind."""
    run = run_experiment(
        seeded_session,
        ExperimentMode.C_RAG_WORKFLOW,
        dataset_ids=["DATASET-001"],
        generated_files=prepared,
    )
    status = run.results[0].detail["status"]
    assert status["predicted"] == run.results[0].predicted_status
    assert status["model"] in AssessmentStatus.values()
    assert status["rails_enforced"] is True
    assert isinstance(status["rails_applied"], list)


# ------------------------------------------------------------------ persistence
def test_persist_false_writes_nothing_to_the_research_record(seeded_session, prepared):
    before = len(list_runs(seeded_session))
    run = run_experiment(
        seeded_session,
        ExperimentMode.B_RAG,
        dataset_ids=["DATASET-001"],
        generated_files=prepared,
        persist=False,
    )
    assert run.id is None
    assert run.results, "the in-memory results are still populated"
    assert run.metrics["n"] == 1
    assert len(list_runs(seeded_session)) == before
    assert seeded_session.query(Assessment).count() == 0


def test_runs_are_listable_newest_first_and_filterable(seeded_session, prepared):
    first = run_experiment(
        seeded_session, ExperimentMode.B_RAG, dataset_ids=["DATASET-001"], generated_files=prepared
    )
    second = run_experiment(
        seeded_session,
        ExperimentMode.C_RAG_WORKFLOW,
        dataset_ids=["DATASET-001"],
        generated_files=prepared,
    )
    ids = [run.id for run in list_runs(seeded_session)]
    assert ids[0] == second.id and first.id in ids
    assert [r.id for r in list_runs(seeded_session, mode=ExperimentMode.B_RAG.value)] == [first.id]
    assert len(list_runs(seeded_session, limit=1)) == 1
    assert get_run(seeded_session, 987654) is None


def test_run_and_result_serialisation(seeded_session, pair_run):
    payload = run_to_dict(pair_run, include_results=True)
    assert json.loads(json.dumps(payload))
    assert payload["experiment_mode"] == ExperimentMode.C_RAG_WORKFLOW.value
    assert len(payload["results"]) == 2

    result = result_to_dict(run_results(seeded_session, pair_run.id)[0])
    assert result["dataset_id"] in PAIR
    assert "status_correct" in result


def test_results_frame_is_a_flat_export(seeded_session, pair_run):
    frame = results_frame(seeded_session, [pair_run.id])
    assert len(frame) == 2
    assert {"dataset_id", "expected_status", "predicted_status"} <= set(frame.columns)


# --------------------------------------------------------------------- comparison
def test_compare_runs_puts_one_row_per_run(seeded_session, prepared):
    runs = [
        run_experiment(
            seeded_session, mode, dataset_ids=PAIR, generated_files=prepared
        )
        for mode in (ExperimentMode.A_RAW_LLM, ExperimentMode.B_RAG)
    ]
    frame = compare_runs(seeded_session, [run.id for run in runs])
    assert len(frame) == 2
    assert list(frame["run_id"]) == [run.id for run in runs]
    for column in ("accuracy", "macro_f1", "citation_rate", "grounding_rate", "latency_median_ms"):
        assert column in frame.columns


def test_an_undefined_cell_is_nan_not_zero(seeded_session, prepared):
    """A rate with no denominator rendered as 0 would read as a measured failure."""
    run = run_experiment(
        seeded_session,
        ExperimentMode.B_RAG,
        dataset_ids=["DATASET-005"],
        generated_files=prepared,
    )
    frame = compare_runs(seeded_session, [run.id])
    # Only an INSUFFICIENT_EVIDENCE case: no true or predicted deficiency exists, so
    # deficiency precision has no denominator.
    assert math.isnan(float(frame["deficiency_precision"].iloc[0]))


def test_compare_runs_rejects_an_unknown_run(seeded_session):
    with pytest.raises(ValueError):
        compare_runs(seeded_session, [987654])


def test_prediction_matrix_shows_which_case_was_missed(seeded_session, pair_run):
    """The table the aggregate metrics are an average of, and the one to read first."""
    frame = prediction_matrix(seeded_session, [pair_run.id])
    assert list(frame["dataset"]) == sorted(PAIR)
    assert list(frame.columns)[:3] == ["dataset", "control", "expected"]
    assert frame.shape[0] == 2
    column = [c for c in frame.columns if c.startswith(ExperimentMode.C_RAG_WORKFLOW.value)][0]
    row = frame[frame["dataset"] == "DATASET-005"].iloc[0]
    assert row["expected"] == AssessmentStatus.INSUFFICIENT_EVIDENCE.value
    assert row[column] == AssessmentStatus.INSUFFICIENT_EVIDENCE.value


# ----------------------------------------------------------------------- failure
def test_a_dataset_that_crashes_is_recorded_as_a_wrong_answer_not_dropped(
    seeded_session, prepared, broken_llm
):
    """Removing failed rows would score the harness on the subset that happened to work.

    A crash is deliberately *not* scored as an abstention: doing so would credit the
    system with correctly declining on DATASET-005 when what actually happened was a
    provider failure, and that is the one dataset where the difference matters most.
    """
    run = run_experiment(
        seeded_session,
        ExperimentMode.B_RAG,
        dataset_ids=PAIR,
        llm=broken_llm,
        generated_files=prepared,
    )
    assert run.status == RUN_STATUS_FAILED, "every dataset errored"
    results = run_results(seeded_session, run.id)
    assert len(results) == 2
    assert all(r.predicted_status == "" for r in results), "a crash is not an abstention"
    assert all(r.status_correct is False for r in results)
    assert all(r.error for r in results)
    assert run.metrics["n_errors"] == 2
    assert run.metrics["classification"]["accuracy"] == pytest.approx(0.0)


def test_one_failing_dataset_does_not_end_the_run(seeded_session, prepared, settings):
    """Losing five datasets because the sixth failed would be far worse than a run that
    reports one failure."""
    from app.llm.base import LLMError
    from app.llm.mock_provider import MockLLMProvider

    class FailsOnDataset005(MockLLMProvider):
        def complete(self, system, user, json_schema=None, temperature=0.0, max_tokens=2000, context=None):
            if "Privileged_User_Listing" in (user or ""):
                raise LLMError("simulated outage on this dataset only")
            return super().complete(
                system, user, json_schema=json_schema, temperature=temperature,
                max_tokens=max_tokens, context=context,
            )

    run = run_experiment(
        seeded_session,
        ExperimentMode.B_RAG,
        dataset_ids=PAIR,
        llm=FailsOnDataset005(settings=settings),
        generated_files=prepared,
    )
    assert run.status == RUN_STATUS_COMPLETED_WITH_ERRORS
    by_id = {r.dataset_id: r for r in run_results(seeded_session, run.id)}
    assert by_id["DATASET-001"].error is None
    assert by_id["DATASET-001"].status_correct is True
    assert by_id["DATASET-005"].error
    assert by_id["DATASET-005"].predicted_status == ""
    assert run.metrics["n_errors"] == 1
    assert run.metrics["n_scored"] == 2, "the failed row stays in the denominator"


def test_a_run_needs_at_least_one_dataset(seeded_session):
    with pytest.raises(ValueError):
        run_experiment(seeded_session, ExperimentMode.B_RAG, dataset_ids=[])


# ----------------------------------------------------------------------- cleanup
def test_cleanup_removes_only_harness_projects(seeded_session, project, pair_run):
    """A real engagement an auditor named "[EVALUATION] ..." must survive."""
    decoy = service.create_project(
        seeded_session,
        name="{0} not really".format(EVALUATION_PROJECT_PREFIX),
        audit_area="IAM",
        scope_note="An auditor's own note.",
    )
    removed = cleanup_evaluation_projects(seeded_session, run_id=pair_run.id)
    assert removed == 2
    assert evaluation_projects(seeded_session, run_id=pair_run.id) == []
    assert service.get_project(seeded_session, decoy.id) is not None
    assert service.get_project(seeded_session, project.id) is not None


def test_cleanup_is_not_automatic(seeded_session, pair_run):
    """A run keeps its evidence by default: deleting it would leave a metrics blob
    nobody can audit."""
    assert evaluation_projects(seeded_session, run_id=pair_run.id)
    assert seeded_session.query(Assessment).count() == 2


def test_the_metrics_survive_cleanup(seeded_session, pair_run):
    metrics = dict(pair_run.metrics)
    cleanup_evaluation_projects(seeded_session, run_id=pair_run.id)
    refreshed = get_run(seeded_session, pair_run.id)
    assert refreshed is not None
    assert refreshed.metrics == metrics


# -------------------------------------------------------------------- whole suite
@pytest.mark.slow
def test_prepare_datasets_verifies_what_it_wrote(tmp_path):
    prepared = prepare_datasets(PAIR, output_dir=tmp_path, verify=True)
    assert set(prepared) == set(PAIR)
    for dataset_id, paths in prepared.items():
        assert [p.name for p in paths] == get_dataset(dataset_id).file_names
        assert all(p.is_file() for p in paths)


@pytest.mark.slow
def test_the_whole_suite_runs_in_every_mode_over_identical_bytes(seeded_session, prepared):
    """A/B/C must see the same files, or a difference in the results table could not be
    attributed to the mode under test."""
    runs = run_all_experiments(seeded_session, dataset_ids=PAIR)
    assert [run.experiment_mode for run in runs] == [mode.value for mode in DEFAULT_MODES]

    suites = {run.config["suite"]["suite_id"] for run in runs}
    assert len(suites) == 1, "the runs must be tied together by one suite id"
    assert all(run.config["suite"]["shared_evidence"] is True for run in runs)

    frame = compare_runs(seeded_session, [run.id for run in runs])
    assert len(frame) == 3
    matrix = prediction_matrix(seeded_session, [run.id for run in runs])
    assert matrix.shape[0] == 2


@pytest.mark.slow
def test_the_full_six_dataset_suite_scores_end_to_end(seeded_session, prepared):
    run = run_experiment(
        seeded_session, ExperimentMode.C_RAG_WORKFLOW, generated_files=prepared, run_name="full suite"
    )
    results = run_results(seeded_session, run.id)
    assert len(results) == 6
    assert run.metrics["n_scored"] == 6
    # The suite must contain more than one correct answer class, or accuracy would be
    # satisfiable by answering the same thing every time.
    assert len({r.expected_status for r in results}) >= 3
    assert run.metrics["classification"]["accuracy"] is not None
