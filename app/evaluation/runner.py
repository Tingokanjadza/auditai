"""The experiment harness: the one place the research results are produced.

Everything else in :mod:`app.evaluation` is a component - the datasets declare the
ground truth, the generator writes the evidence, the metrics module defines the
arithmetic. This module is the *procedure*: for one experimental condition it walks
every dataset through the full production pipeline (ingest -> parse -> chunk -> embed ->
retrieve -> assess -> validate) and records what came out next to what should have come
out. A number in the write-up is a number this module wrote.

What one run is
---------------
A run is (one mode) x (a set of datasets). Each dataset gets its **own throwaway audit
project**, because the alternative - one project holding six datasets' evidence - would
let DATASET-001's MFA export be retrieved while assessing DATASET-005, and DATASET-005
is the case the whole study turns on. Isolation is not tidiness here; it is the
experimental control that makes the result interpretable.

Keeping the experiments out of the auditor's numbers
----------------------------------------------------
Two mechanisms, and the first is the one that matters:

1. Every assessment carries ``evaluation_run_id``. ``app.audit.service.dashboard_stats``
   and ``list_findings`` already filter on it (``include_evaluation=False``), so the
   moment an assessment is tagged it stops counting towards any operational figure.
   This module simply passes the id through to
   :meth:`app.audit.engine.AssessmentEngine.assess_control`; it invents no new
   convention.
2. The projects themselves are named with :data:`EVALUATION_PROJECT_PREFIX`, carry
   :data:`EVALUATION_SCOPE_TAG` in ``scope_note`` (the machine-readable marker
   :func:`is_evaluation_project` tests) and are created with ``is_demo=True``, which is
   the only flag the shared service layer offers for "not a real engagement" and is what
   ``list_projects(include_demo=False)`` hides. Overloading ``is_demo`` is a compromise,
   recorded here rather than hidden: adding an ``is_evaluation`` column would mean
   editing a foundation file this task does not own.

One known gap, stated because it is easier to fix than to discover later: with
``project_id=None`` the dashboard's ``evidence_files`` and ``evidence_chunks`` counters
and ``controls_in_scope`` are computed over *all* rows, so evaluation projects do inflate
those three fields while they exist. Statuses, findings, risk and latency are unaffected.
:func:`cleanup_evaluation_projects` removes the projects entirely and is the remedy until
those three queries learn the same exclusion.

Why cleanup is not automatic
----------------------------
Deleting an evaluation project cascades to its ``Assessment`` rows, and those rows hold
the prompt snapshot, the verbatim model response and the citation verdicts - the only
record of *how* a prediction was reached. Discarding them would leave a metrics blob no
one can audit, which is the opposite of the point. So a run keeps its evidence by
default; ``cleanup=True`` (or a later :func:`cleanup_evaluation_projects` call) is an
explicit choice to trade the trail for disk space, and it is documented as such.

Reproducibility is part of the result
-------------------------------------
:func:`build_run_config` records everything needed to re-run: provider and model as
*actually resolved* (not as requested - the factory may have fallen back to the mock),
prompt version and fingerprint, retrieval strategy and top-k, embedding provider and
dimension, chunk settings, the mock seed and hallucination rate, the dataset ids, the
generator seed, a fingerprint of the ground truth itself, and the app/engine/runner
versions. A result set that cannot be reproduced is not a result.

What these runs cannot establish
--------------------------------
With ``LLM_PROVIDER=mock`` the provider is a deterministic rule-based stand-in, so a run
measures the *pipeline* - retrieval, prompt scaffolding, validation, rails - and says
nothing about language-model quality. The datasets are authored, so accuracy is accuracy
against a designed answer key. n = 6. Every caveat the metrics layer attaches to a figure
travels with it into ``EvaluationRun.metrics``; none of them should be dropped when the
numbers are quoted.
"""

from __future__ import annotations

import hashlib
import json
import logging
import platform
import time
import traceback
from collections.abc import Mapping as _MappingABC
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import service as audit_service
from app.audit.engine import ENGINE_VERSION, NO_RETRIEVAL, AssessmentEngine, AssessmentRunResult
from app.audit.prompts import PROMPT_VERSION, prompt_metadata
from app.config import Settings, get_settings
from app.database.base import utcnow
from app.database.models import Assessment, AuditProject, EvaluationResult, EvaluationRun
from app.evaluation.datasets import (
    DATASET_DISCLAIMER,
    GENERATOR_SEED,
    SYNTHETIC_DATASETS,
    SYNTHETIC_NOTICE,
    SyntheticDataset,
    get_dataset,
)
from app.evaluation.metrics import compare_modes, compute_metrics, results_to_frame, timing_metrics
from app.evidence.service import delete_evidence, ingest_file
from app.llm.base import LLMProvider
from app.schemas.enums import ActivityAction, ExperimentMode, ProjectStatus

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MODES",
    "EVALUATION_ACTOR",
    "EVALUATION_PROJECT_PREFIX",
    "EVALUATION_SCOPE_TAG",
    "RUNNER_VERSION",
    "RUN_STATUS_COMPLETED",
    "RUN_STATUS_COMPLETED_WITH_ERRORS",
    "RUN_STATUS_FAILED",
    "RUN_STATUS_RUNNING",
    "build_run_config",
    "cleanup_evaluation_projects",
    "compare_runs",
    "evaluation_projects",
    "get_run",
    "is_evaluation_project",
    "list_runs",
    "prediction_matrix",
    "prepare_datasets",
    "result_to_dict",
    "results_frame",
    "run_all_experiments",
    "run_experiment",
    "run_results",
    "run_to_dict",
]

#: Bumped when the *procedure* changes (what is created, ingested, scored or recorded),
#: so a stored run can be traced to the harness that produced it. Distinct from
#: ``ENGINE_VERSION``: the same engine scored by a different harness is a different
#: experiment.
RUNNER_VERSION = "1.0.0"

#: Attributed as the actor on every project, upload and activity row the harness writes,
#: so the trail never suggests a human auditor uploaded this evidence.
EVALUATION_ACTOR = "evaluation-harness"

#: Cosmetic, but load-bearing in a UI project picker: an evaluation project must be
#: identifiable at a glance.
EVALUATION_PROJECT_PREFIX = "[EVALUATION]"

#: The machine-readable marker, written into ``AuditProject.scope_note``. Cleanup keys off
#: this and never off the name alone - a real project an auditor happened to name
#: "[EVALUATION] Q3" must survive.
EVALUATION_SCOPE_TAG = "EVALUATION-HARNESS"

_SCOPE_NOTE = (
    "{tag} dataset={dataset} mode={mode} run={run}\n"
    "Throwaway project created by app.evaluation.runner to score one synthetic dataset "
    "against its known correct answer. Not an audit engagement; the evidence is fabricated "
    "and the assessments are excluded from every operational figure."
)

RUN_STATUS_RUNNING = "RUNNING"
RUN_STATUS_COMPLETED = "COMPLETED"
#: At least one dataset errored. The run is still reportable - the failed rows are scored
#: as wrong answers - but the status says so rather than letting a partial run read as a
#: clean one.
RUN_STATUS_COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
RUN_STATUS_FAILED = "FAILED"

#: A, B then C. Order matters only for readability of the comparison table.
DEFAULT_MODES: Tuple[ExperimentMode, ...] = (
    ExperimentMode.A_RAW_LLM,
    ExperimentMode.B_RAG,
    ExperimentMode.C_RAG_WORKFLOW,
)

#: Truncation limits from ``app.database.models``. SQLite ignores them, PostgreSQL will not.
_MAX_NAME = 255
_MAX_AUDIT_AREA = 255


# ---- dataset preparation


def prepare_datasets(
    dataset_ids: Optional[Sequence[Any]] = None,
    output_dir: Optional[Any] = None,
    verify: bool = True,
) -> Dict[str, List[Path]]:
    """Write the datasets to disk and return ``{dataset_id: [paths]}``.

    ``verify=True`` re-parses what was written and checks it still satisfies its own
    declaration - the counts, the exception rows, every key evidence marker - and raises
    if it does not. A dataset that has silently drifted would not fail an evaluation run;
    it would quietly change what the run measured, and the result would look fine.

    Called once per :func:`run_all_experiments` so that modes A, B and C are scored
    against the *same bytes*, not merely against equal-looking regenerated files.
    """
    from app.evaluation.generator import dataset_dir, generate_dataset, verify_dataset

    datasets = _resolve_datasets(dataset_ids)
    prepared: Dict[str, List[Path]] = {}
    for dataset in datasets:
        if verify:
            report = verify_dataset(dataset, output_dir=output_dir, regenerate=True)
            if not report.get("ok"):
                raise RuntimeError(
                    "{0} does not match its own declaration; refusing to evaluate against it. "
                    "Failed checks: {1}".format(dataset.dataset_id, _verification_failures(report))
                )
        else:
            generate_dataset(dataset, output_dir=output_dir)
        directory = dataset_dir(dataset, output_dir)
        prepared[dataset.dataset_id] = [directory / name for name in dataset.file_names]
    return prepared


def _verification_failures(report: Mapping[str, Any]) -> str:
    """One line naming what failed in a :func:`generator.verify_dataset` report."""
    parts: List[str] = []
    if report.get("missing_files"):
        parts.append("missing files: {0}".format(", ".join(report["missing_files"])))
    bad_files = [f["filename"] for f in report.get("files", []) if not f.get("ok")]
    if bad_files:
        parts.append("unparsed: {0}".format(", ".join(bad_files)))
    bad_markers = [m["marker"][:60] for m in report.get("markers", []) if not m.get("ok")]
    if bad_markers:
        parts.append("markers not found: {0}".format(" | ".join(bad_markers)))
    bad_checks = [
        str(c.get("check", c)) for c in report.get("population_checks", []) if not c.get("ok")
    ]
    if bad_checks:
        parts.append("population checks: {0}".format(", ".join(bad_checks)))
    return "; ".join(parts) or "(no detail reported)"


# ---- run configuration


def build_run_config(
    mode: ExperimentMode,
    datasets: Sequence[SyntheticDataset],
    llm: LLMProvider,
    settings: Settings,
    persist: bool = True,
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Everything needed to reproduce this run, as a JSON-serialisable dict.

    ``llm`` is the provider instance the engine actually holds, not the configured name:
    :func:`app.llm.factory.get_llm_provider` falls back to the mock when a real provider
    is selected but unconfigured, and a results table that recorded the *requested*
    provider in that case would be a false statement about how the numbers were obtained.
    ``llm_provider_requested`` keeps the configured value alongside so the substitution is
    visible rather than merely absent.

    No secret is recorded. The base URL is reduced to a boolean, since a self-hosted
    endpoint URL can carry credentials in its userinfo.
    """
    config: Dict[str, Any] = {
        "runner_version": RUNNER_VERSION,
        "engine_version": ENGINE_VERSION,
        "app_version": settings.app_version,
        "environment": settings.environment,
        "python_version": platform.python_version(),
        "generated_at": _now().isoformat(),
        "experiment_mode": mode.value,
        "experiment_mode_label": mode.label,
        "persisted": bool(persist),
        "llm": {
            "provider": llm.name,
            "model": str(getattr(llm, "model", "") or ""),
            "provider_requested": settings.llm_provider,
            "model_requested": settings.llm_model,
            "fell_back_to_mock": llm.name == "mock" and settings.llm_provider != "mock",
            "base_url_configured": bool(settings.llm_base_url),
            "temperature": settings.llm_temperature,
            "max_tokens": settings.llm_max_tokens,
            "use_json_mode": settings.llm_use_json_mode,
            "rules_version": getattr(llm, "rules_version", None),
        },
        "mock": {
            "seed": settings.mock_seed,
            "hallucination_rate": settings.mock_hallucination_rate,
            "note": (
                "The mock provider is a deterministic rule-based stand-in, not a language "
                "model. Results obtained with it measure the pipeline, not model quality."
            ),
        },
        "prompts": prompt_metadata(),
        "prompt_version": PROMPT_VERSION,
        "retrieval": {
            "performed": mode.uses_retrieval,
            "strategy": str(settings.retrieval_strategy) if mode.uses_retrieval else NO_RETRIEVAL,
            "top_k": settings.retrieval_top_k if mode.uses_retrieval else 0,
            "candidate_k": settings.retrieval_candidate_k if mode.uses_retrieval else 0,
            "min_score": settings.retrieval_min_score,
        },
        "embedding": {
            "provider": settings.embedding_provider,
            "model": (
                "hashing-vectorizer"
                if settings.embedding_provider == "local"
                else settings.embedding_model
            ),
            "dimension": settings.embedding_dim,
        },
        "chunking": {
            "chunk_size": settings.chunk_size,
            "chunk_overlap": settings.chunk_overlap,
            "table_rows_per_chunk": settings.table_rows_per_chunk,
        },
        "budgets": {
            "max_evidence_chars": settings.max_evidence_chars,
            "raw_evidence_char_budget": settings.raw_evidence_char_budget,
        },
        "validation": {
            "citation_match_threshold": settings.citation_match_threshold,
            "force_human_review": settings.force_human_review,
            "rails_applied": mode is ExperimentMode.C_RAG_WORKFLOW,
        },
        "datasets": {
            "ids": [dataset.dataset_id for dataset in datasets],
            "control_refs": sorted({dataset.control_ref for dataset in datasets}),
            "mandated_only": all(dataset.mandated for dataset in datasets),
            "generator_seed": GENERATOR_SEED,
            "manifest_sha256": _dataset_manifest_fingerprint(datasets),
            "disclaimer": DATASET_DISCLAIMER,
        },
        "database_backend": "sqlite" if settings.is_sqlite else "other",
    }
    if extra:
        config.update(dict(extra))
    return config


def _dataset_manifest_fingerprint(datasets: Sequence[SyntheticDataset]) -> str:
    """SHA-256 over the ground truth itself.

    Pins the answer key as tightly as the prompt fingerprint pins the instrument: if a
    dataset's expected status, population or markers were edited between two runs, the
    two runs measured different things and this digest says so.
    """
    payload = json.dumps(
        [dataset.to_dict() for dataset in datasets], sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---- the experiment


def run_experiment(
    session: Session,
    mode: Union[ExperimentMode, str],
    dataset_ids: Optional[Sequence[Any]] = None,
    llm: Optional[LLMProvider] = None,
    run_name: str = "",
    persist: bool = True,
    notes: str = "",
    generated_files: Optional[Mapping[str, Sequence[Any]]] = None,
    cleanup: bool = False,
    settings: Optional[Settings] = None,
) -> EvaluationRun:
    """Score one experimental condition over the synthetic suite.

    For each dataset: generate its files (unless ``generated_files`` supplies them),
    create an isolated evaluation project with the dataset's control in scope, ingest
    every file through the production ingestion path, assess the control in ``mode``,
    and score the answer against the declared ground truth into an
    :class:`~app.database.models.EvaluationResult`. Then compute the metric set over all
    the rows and store it on the returned :class:`~app.database.models.EvaluationRun`.

    ``persist=False`` returns a fully populated run object that was never added to the
    session: no ``EvaluationRun``, ``EvaluationResult`` or ``Assessment`` row is written,
    which is what a caller exploring a configuration wants before committing a result to
    the research record. The projects and their evidence are still created either way -
    ingestion *is* part of the pipeline under test - and are removed by ``cleanup=True``
    or a later :func:`cleanup_evaluation_projects` call.

    A dataset that raises is recorded as an errored ``EvaluationResult`` and the run
    continues. Losing five datasets because the sixth failed would be a far worse
    outcome than a run that reports one failure, and the metrics layer scores a missing
    prediction as a wrong answer rather than dropping it from the denominator.
    """
    resolved_mode = _coerce_mode(mode)
    resolved_settings = settings or get_settings()
    datasets = _resolve_datasets(dataset_ids)
    if not datasets:
        raise ValueError("An evaluation run needs at least one dataset.")

    files = _resolve_files(datasets, generated_files)
    engine = AssessmentEngine(session, llm=llm, settings=resolved_settings)

    started = time.perf_counter()
    run = EvaluationRun(
        name=(run_name or _default_run_name(resolved_mode, datasets))[:_MAX_NAME],
        experiment_mode=resolved_mode.value,
        llm_provider=engine.llm.name,
        llm_model=str(getattr(engine.llm, "model", "") or ""),
        retrieval_strategy=(
            str(resolved_settings.retrieval_strategy) if resolved_mode.uses_retrieval else NO_RETRIEVAL
        ),
        dataset_ids=[dataset.dataset_id for dataset in datasets],
        config=build_run_config(resolved_mode, datasets, engine.llm, resolved_settings, persist),
        metrics={},
        notes=notes or "",
        status=RUN_STATUS_RUNNING,
        started_at=utcnow(),
    )
    if persist:
        session.add(run)
        session.commit()

    # Persisted runs label their projects with the run id; a transient run has no id, so
    # it falls back to a timestamp token that is still unique enough to clean up by.
    run_label = str(run.id) if persist else _transient_label()

    results: List[EvaluationResult] = []
    project_ids: List[int] = []
    for dataset in datasets:
        row, project_id = _run_one_dataset(
            session=session,
            engine=engine,
            dataset=dataset,
            paths=files[dataset.dataset_id],
            mode=resolved_mode,
            run=run,
            run_label=run_label,
            persist=persist,
            settings=resolved_settings,
        )
        row.run = run  # back-populates run.results; cascades the insert when run is persistent
        results.append(row)
        if project_id is not None:
            project_ids.append(project_id)
        if persist:
            session.add(row)
            session.commit()

    run.metrics = compute_metrics(results)
    run.status = _run_status(results)
    run.finished_at = utcnow()
    run.duration_seconds = round(time.perf_counter() - started, 3)
    if persist:
        session.commit()
        audit_service.log_activity(
            session,
            entity_type="evaluation_run",
            entity_id=run.id,
            action=ActivityAction.EVALUATION_RUN,
            actor=EVALUATION_ACTOR,
            actor_type="AI",
            details={
                "mode": run.experiment_mode,
                "datasets": list(run.dataset_ids),
                "status": run.status,
                "accuracy": _dig(run.metrics, "classification", "accuracy"),
                "duration_seconds": run.duration_seconds,
                "llm_provider": run.llm_provider,
            },
        )

    if cleanup:
        removed = _delete_projects(session, project_ids)
        logger.info("Removed %s evaluation project(s) for run %s.", removed, run_label)

    return run


def run_all_experiments(
    session: Session,
    modes: Optional[Sequence[Union[ExperimentMode, str]]] = None,
    dataset_ids: Optional[Sequence[Any]] = None,
    llm: Optional[LLMProvider] = None,
    run_name: str = "",
    persist: bool = True,
    cleanup: bool = False,
    settings: Optional[Settings] = None,
) -> List[EvaluationRun]:
    """Run A, B and C over the same datasets so the comparison is like-for-like.

    The suite is generated (and verified) **once** and the same file paths are handed to
    every mode, so the three conditions see identical bytes, identical parsing, identical
    chunking and identical embeddings. The only difference between the runs is the
    orchestration under test. Anything else and a difference in the results table could
    not be attributed to the mode.

    The runs are tied together by a shared ``suite_id`` in each run's ``config``, which is
    what lets :func:`compare_runs` be reconstructed later from the database alone.
    """
    resolved_modes = [_coerce_mode(mode) for mode in (modes if modes is not None else DEFAULT_MODES)]
    if not resolved_modes:
        raise ValueError("run_all_experiments needs at least one mode.")
    datasets = _resolve_datasets(dataset_ids)
    resolved_settings = settings or get_settings()

    files = prepare_datasets([dataset.dataset_id for dataset in datasets])
    suite_id = _transient_label()
    label = run_name or "Suite {0}".format(suite_id)

    runs: List[EvaluationRun] = []
    for position, mode in enumerate(resolved_modes):
        run = run_experiment(
            session,
            mode,
            dataset_ids=[dataset.dataset_id for dataset in datasets],
            llm=llm,
            run_name="{0} - {1}".format(label, mode.value),
            persist=persist,
            notes=(
                "Part {0} of {1} in suite {2}: modes {3} scored over identical generated "
                "evidence.".format(
                    position + 1,
                    len(resolved_modes),
                    suite_id,
                    ", ".join(m.value for m in resolved_modes),
                )
            ),
            generated_files=files,
            cleanup=cleanup,
            settings=resolved_settings,
        )
        # Recorded after construction so the suite membership is stored even for a
        # transient run, whose config was built before the sibling runs existed.
        config = dict(run.config or {})
        config["suite"] = {
            "suite_id": suite_id,
            "position": position + 1,
            "modes": [m.value for m in resolved_modes],
            "shared_evidence": True,
        }
        run.config = config
        if persist:
            session.commit()
        runs.append(run)
    return runs


# ---- one dataset


def _run_one_dataset(
    session: Session,
    engine: AssessmentEngine,
    dataset: SyntheticDataset,
    paths: Sequence[Path],
    mode: ExperimentMode,
    run: EvaluationRun,
    run_label: str,
    persist: bool,
    settings: Settings,
) -> Tuple[EvaluationResult, Optional[int]]:
    """Ingest, assess and score one dataset. Returns the row and the project it used."""
    project: Optional[AuditProject] = None
    # Captured as a plain int the moment the project exists: after a failure the ORM
    # object may be expired or gone, and the id is all the error row needs.
    project_id: Optional[int] = None
    ingest_started = time.perf_counter()
    try:
        project = _create_evaluation_project(session, dataset, mode, run_label, settings)
        project_id = int(project.id)
        evidence = _ingest_dataset(session, project.id, dataset, paths)
        ingest_ms = int((time.perf_counter() - ingest_started) * 1000)

        outcome = engine.assess_control(
            project.id,
            dataset.control_ref,
            mode=mode,
            persist=persist,
            evaluation_run_id=run.id if persist else None,
        )
        row = _score_outcome(dataset, outcome, mode, project, evidence, ingest_ms)
        return row, project.id
    except Exception as exc:  # noqa: BLE001 - a broken dataset must not abort the run
        logger.exception("Dataset %s failed under mode %s", dataset.dataset_id, mode.value)
        # A database error leaves the session in a state where every later commit fails,
        # which would turn one failed dataset into a failed run. Rolling back the partial
        # transaction is what keeps the remaining datasets scorable.
        try:
            session.rollback()
        except Exception:  # noqa: BLE001 - nothing useful is left to do about this
            logger.exception("Rollback after the %s failure also failed.", dataset.dataset_id)
        row = _errored_result(dataset, mode, project_id, exc)
        return row, project_id


def _create_evaluation_project(
    session: Session,
    dataset: SyntheticDataset,
    mode: ExperimentMode,
    run_label: str,
    settings: Settings,
) -> AuditProject:
    """One isolated project per (dataset, mode), with only that dataset's control scoped."""
    name = "{0} {1} {2} run-{3}".format(
        EVALUATION_PROJECT_PREFIX, dataset.dataset_id, mode.value, run_label
    )
    return audit_service.create_project(
        session,
        name=name[:_MAX_NAME],
        audit_area="Research evaluation - {0}".format(dataset.control_ref)[:_MAX_AUDIT_AREA],
        description=(
            "{0}\n\nSynthetic evaluation case: {1}. Expected conclusion: {2}. {3}".format(
                SYNTHETIC_NOTICE, dataset.name, dataset.expected_status.value, DATASET_DISCLAIMER
            )
        ),
        auditor_name=EVALUATION_ACTOR,
        status=ProjectStatus.FIELDWORK,
        scope_note=_SCOPE_NOTE.format(
            tag=EVALUATION_SCOPE_TAG,
            dataset=dataset.dataset_id,
            mode=mode.value,
            run=run_label,
        ),
        is_demo=True,
        control_refs=[dataset.control_ref],
        actor=EVALUATION_ACTOR,
    )


def _ingest_dataset(
    session: Session,
    project_id: int,
    dataset: SyntheticDataset,
    paths: Sequence[Path],
) -> List[Dict[str, Any]]:
    """Push every generated file through the real ingestion path, in declaration order.

    The declared ``evidence_type`` is passed through rather than left to default: the
    retriever copies it onto every chunk and both the prompt and the offline provider use
    it to distinguish a document that *states a requirement* from an export that *records
    what happened*. Ingesting these files untyped would quietly change what the run
    measures.
    """
    ingested: List[Dict[str, Any]] = []
    by_name = {path.name: path for path in paths}
    for declared in dataset.files:
        path = by_name.get(declared.filename)
        if path is None or not path.exists():
            raise FileNotFoundError(
                "{0}: generated file '{1}' is missing from {2}.".format(
                    dataset.dataset_id, declared.filename, [str(p) for p in paths]
                )
            )
        record = ingest_file(
            session,
            project_id,
            path.read_bytes(),
            declared.filename,
            evidence_type=declared.evidence_type,
            description=declared.description,
            uploaded_by=EVALUATION_ACTOR,
            is_synthetic=True,
        )
        ingested.append(
            {
                "filename": record.filename,
                "evidence_type": record.evidence_type,
                "role": declared.role,
                "sha256": record.sha256,
                "size_bytes": record.size_bytes,
                "parse_status": record.parse_status,
                "chunks": record.chunk_count,
                "rows": record.row_count,
                "pages": record.page_count,
                "parse_error": record.parse_error or "",
            }
        )
    return ingested


# ---- scoring


def _score_outcome(
    dataset: SyntheticDataset,
    outcome: AssessmentRunResult,
    mode: ExperimentMode,
    project: AuditProject,
    evidence: Sequence[Mapping[str, Any]],
    ingest_ms: int,
) -> EvaluationResult:
    """Turn one assessment into a scored row. Every column is populated from the run.

    ``predicted_status`` is the status the *system stands behind* - post-rails in mode C,
    identical to the model's own answer in A and B, where no rail is applied. The model's
    unedited status is kept under ``detail["status"]["model"]`` so the effect of the rails
    is recoverable from the row rather than inferred from the mode.

    When the engine recorded an error, ``predicted_status`` is left empty even though the
    engine still produced a placeholder INSUFFICIENT_EVIDENCE row, and ``predicted_risk``
    with it. Scoring that placeholder as a prediction would credit the system with correctly
    abstaining on DATASET-005 when what actually happened was a crash, and that is the one
    dataset where the difference matters most. The band the risk model did assign is kept
    under ``detail["risk"]["engine_band"]``.
    """
    output = outcome.output
    model_output = outcome.model_output or output
    validation = outcome.validation

    hits, marker_detail = _marker_hits(dataset, outcome)
    verdicts = _verdict_counts(validation)
    declared_missing = [item for item in output.missing_evidence if str(item).strip()]

    # An engine failure produces a placeholder assessment; neither its status nor its
    # risk band is scored as a prediction (see the docstring).
    predicted = output.status.value if not outcome.error else ""
    predicted_risk = outcome.risk.level.value if not outcome.error else ""
    row = EvaluationResult(
        assessment_id=outcome.assessment_id,
        dataset_id=dataset.dataset_id,
        dataset_name=dataset.name[:255],
        control_ref=dataset.control_ref,
        expected_status=dataset.expected_status.value,
        predicted_status=predicted,
        status_correct=bool(predicted) and predicted == dataset.expected_status.value,
        expected_risk=dataset.expected_risk.value,
        predicted_risk=predicted_risk,
        expected_evidence_hits=hits,
        expected_evidence_total=len(dataset.key_evidence_markers),
        citation_count=validation.total,
        verified_citation_count=validation.verified,
        fabricated_citation_count=validation.fabricated,
        unsupported_claim_count=len(validation.unsupported_claims),
        hallucination_detected=bool(validation.has_hallucination),
        declared_missing_evidence=bool(declared_missing),
        latency_ms=int(outcome.latency_ms),
        prompt_tokens=int(outcome.prompt_tokens),
        completion_tokens=int(outcome.completion_tokens),
        error=outcome.error or None,
        created_at=utcnow(),
        detail={
            # Read by app.evaluation.metrics.normalise_result to group rows by mode.
            "experiment_mode": mode.value,
            "project_id": project.id,
            "project_name": project.name,
            "assessment_id": outcome.assessment_id,
            "mandated_dataset": dataset.mandated,
            "expected_finding": dataset.expected_finding,
            "expected_missing_evidence": dataset.expected_missing_evidence,
            "expected_exception_ratio": dataset.exception_ratio,
            "status": {
                "expected": dataset.expected_status.value,
                "predicted": predicted,
                "model": model_output.status.value,
                "rails_enforced": outcome.rails_enforced,
                "rails_applied": list(validation.rails_applied),
                "status_downgraded": validation.status_downgraded,
            },
            "risk": {
                "expected": dataset.expected_risk.value,
                "predicted": predicted_risk,
                "engine_band": outcome.risk.level.value,
                "correct": bool(predicted_risk) and predicted_risk == dataset.expected_risk.value,
                "score": round(float(outcome.risk.score), 2),
            },
            "citations": verdicts,
            "unsupported_claims": list(validation.unsupported_claims),
            "grounding_rate_engine": round(float(validation.grounding_rate), 4),
            "missing_evidence_declared": declared_missing,
            "confidence": output.confidence.value,
            "evidence_sufficiency": output.evidence_sufficiency.value,
            "retrieval": {
                "strategy": outcome.retrieval.strategy,
                "chunks": len(outcome.retrieval.chunks),
                "chunk_ids": list(outcome.retrieval.chunk_ids),
                "queries": list(outcome.retrieval.queries),
                "total_candidates": outcome.retrieval.total_candidates,
                "notes": list(outcome.retrieval.notes),
            },
            "markers": marker_detail,
            "evidence_files": [dict(item) for item in evidence],
            "timing": {
                "ingest_ms": ingest_ms,
                "assessment_ms": int(outcome.latency_ms),
                "retrieval_ms": int(outcome.retrieval_ms),
                "llm_ms": int(outcome.llm_ms),
                "llm_calls": int(outcome.llm_calls),
            },
            "sufficiency_precheck": (
                outcome.sufficiency.model_dump(mode="json") if outcome.sufficiency else None
            ),
            "critique": outcome.critique.model_dump(mode="json") if outcome.critique else None,
        },
    )
    return row


def _errored_result(
    dataset: SyntheticDataset,
    mode: ExperimentMode,
    project_id: Optional[int],
    exc: BaseException,
) -> EvaluationResult:
    """A dataset that crashed, recorded as a scorable row with no prediction.

    Deliberately *not* dropped: ``app.evaluation.metrics`` scores an empty prediction as
    ``(no prediction)`` and counts it against accuracy. Removing failed rows would score
    the system only on the runs it managed to complete, which flatters it exactly where
    it failed.
    """
    return EvaluationResult(
        assessment_id=None,
        dataset_id=dataset.dataset_id,
        dataset_name=dataset.name[:255],
        control_ref=dataset.control_ref,
        expected_status=dataset.expected_status.value,
        predicted_status="",
        status_correct=False,
        expected_risk=dataset.expected_risk.value,
        predicted_risk="",
        expected_evidence_hits=0,
        expected_evidence_total=len(dataset.key_evidence_markers),
        error="{0}: {1}".format(type(exc).__name__, exc),
        created_at=utcnow(),
        detail={
            "experiment_mode": mode.value,
            "project_id": project_id,
            "failed": True,
            "traceback": "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )[-4000:],
        },
    )


def _marker_hits(
    dataset: SyntheticDataset, outcome: AssessmentRunResult
) -> Tuple[int, List[Dict[str, Any]]]:
    """How many of the dataset's key evidence markers reached the model.

    This is the retrieval-quality metric: each marker is a sentence or column name that
    a correct answer has to have seen, declared in :mod:`app.evaluation.datasets` and
    written into the documents *by* the generator, so it is guaranteed to exist in the
    evidence. A marker is a hit when it appears in the text of a chunk the model was
    shown.

    Two properties worth being explicit about:

    * Matching is a literal substring test after collapsing runs of whitespace to single
      spaces. The normalisation is there because PDF and DOCX extraction re-wraps lines,
      so an un-normalised test would report a retrieval failure that is really a line
      break. Case is *not* folded and nothing is stemmed: this measures whether the exact
      declared text arrived, not whether something similar did.
    * In mode A the "retrieved" chunks are the raw file text that fitted inside
      ``raw_evidence_char_budget``, because mode A performs no retrieval (see
      :mod:`app.audit.engine`). The figure is therefore comparable across modes as
      *evidence coverage* - what share of the necessary evidence reached the model - but
      only in B and C is it a measurement of a retriever.
    """
    shown = [(chunk.chunk_id, chunk.filename, chunk.text or "") for chunk in outcome.retrieval.chunks]
    normalised = [(chunk_id, filename, _collapse(text)) for chunk_id, filename, text in shown]

    hits = 0
    detail: List[Dict[str, Any]] = []
    for marker in dataset.key_evidence_markers:
        needle = _collapse(marker)
        found = [
            {"chunk_id": chunk_id, "filename": filename}
            for chunk_id, filename, text in normalised
            if needle and needle in text
        ]
        if found:
            hits += 1
        detail.append(
            {
                "marker": marker if len(marker) <= 160 else marker[:157] + "...",
                "found": bool(found),
                "chunk_ids": [item["chunk_id"] for item in found],
                "files": sorted({item["filename"] for item in found}),
            }
        )
    return hits, detail


def _collapse(text: str) -> str:
    """Collapse whitespace runs to single spaces; nothing else is altered."""
    return " ".join(str(text or "").split())


def _verdict_counts(validation: Any) -> Dict[str, int]:
    """Citation counts by validator verdict, plus the totals the columns store."""
    return {
        "total": int(validation.total),
        "verified": int(validation.verified),
        "partial": int(validation.partial),
        "unverified": int(validation.unverified),
        "fabricated": int(validation.fabricated),
    }


def _run_status(results: Sequence[EvaluationResult]) -> str:
    errored = sum(1 for row in results if row.error)
    if not results:
        return RUN_STATUS_FAILED
    if errored == len(results):
        return RUN_STATUS_FAILED
    if errored:
        return RUN_STATUS_COMPLETED_WITH_ERRORS
    return RUN_STATUS_COMPLETED


# ---- reading runs back


def list_runs(
    session: Session,
    mode: Optional[Union[ExperimentMode, str]] = None,
    limit: Optional[int] = None,
) -> List[EvaluationRun]:
    """Evaluation runs, newest first."""
    stmt = select(EvaluationRun)
    if mode is not None:
        stmt = stmt.where(EvaluationRun.experiment_mode == _coerce_mode(mode).value)
    stmt = stmt.order_by(EvaluationRun.started_at.desc(), EvaluationRun.id.desc())
    rows = list(session.execute(stmt).scalars().all())
    return rows[: int(limit)] if limit is not None else rows


def get_run(session: Session, run_id: int) -> Optional[EvaluationRun]:
    return session.get(EvaluationRun, int(run_id))


def run_results(session: Session, run_id: int) -> List[EvaluationResult]:
    """One run's scored rows, in dataset order."""
    stmt = (
        select(EvaluationResult)
        .where(EvaluationResult.run_id == int(run_id))
        .order_by(EvaluationResult.dataset_id.asc(), EvaluationResult.id.asc())
    )
    return list(session.execute(stmt).scalars().all())


def run_to_dict(run: EvaluationRun, include_results: bool = False) -> Dict[str, Any]:
    """Serialisable run header for the API and the UI."""
    data: Dict[str, Any] = {
        "id": run.id,
        "name": run.name,
        "experiment_mode": run.experiment_mode,
        "experiment_mode_label": _mode_label(run.experiment_mode),
        "llm_provider": run.llm_provider,
        "llm_model": run.llm_model,
        "retrieval_strategy": run.retrieval_strategy,
        "dataset_ids": list(run.dataset_ids or []),
        "status": run.status,
        "notes": run.notes,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "duration_seconds": run.duration_seconds,
        "config": dict(run.config or {}),
        "metrics": dict(run.metrics or {}),
        "result_count": len(run.results or []),
    }
    if include_results:
        data["results"] = [result_to_dict(row) for row in (run.results or [])]
    return data


def result_to_dict(result: EvaluationResult) -> Dict[str, Any]:
    """Serialisable scored row, with the detail blob attached."""
    return {
        "id": result.id,
        "run_id": result.run_id,
        "assessment_id": result.assessment_id,
        "dataset_id": result.dataset_id,
        "dataset_name": result.dataset_name,
        "control_ref": result.control_ref,
        "expected_status": result.expected_status,
        "predicted_status": result.predicted_status,
        "status_correct": result.status_correct,
        "expected_risk": result.expected_risk,
        "predicted_risk": result.predicted_risk,
        "expected_evidence_hits": result.expected_evidence_hits,
        "expected_evidence_total": result.expected_evidence_total,
        "citation_count": result.citation_count,
        "verified_citation_count": result.verified_citation_count,
        "fabricated_citation_count": result.fabricated_citation_count,
        "unsupported_claim_count": result.unsupported_claim_count,
        "hallucination_detected": result.hallucination_detected,
        "declared_missing_evidence": result.declared_missing_evidence,
        "latency_ms": result.latency_ms,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "error": result.error,
        "detail": dict(result.detail or {}),
    }


def results_frame(session: Session, run_ids: Sequence[int]) -> pd.DataFrame:
    """Every scored row of the named runs as one flat frame, for export."""
    frames: List[pd.DataFrame] = []
    for run_id in run_ids:
        rows = run_results(session, int(run_id))
        frame = results_to_frame(rows)
        frame.insert(0, "run_id", int(run_id))
        frames.append(frame)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ---- comparison


def compare_runs(session: Session, run_ids: Sequence[int]) -> pd.DataFrame:
    """The A/B/C results table: one row per run, one column per headline metric.

    Columns are those of :func:`app.evaluation.metrics.compare_modes` - accuracy, macro
    and weighted F1, deficiency-detection precision/recall/FPR/FNR, citation, grounding,
    fabrication, unsupported-claim and hallucination rates, retrieval recall,
    missing-evidence detection rate, median and p95 latency and mean tokens - plus
    ``latency_mean_ms`` and the identifying ``run_id`` / ``run_name`` / ``status``
    columns. Undefined cells are ``NaN``, never 0: see the metrics module on why a rate
    with no denominator must not be rendered as zero.

    Every cell is computed by the same functions that produced the stored
    ``EvaluationRun.metrics``, so a figure here and the same figure in the run's metrics
    blob cannot disagree. Rows are keyed by mode; if two runs share a mode the key is
    disambiguated with the run id so neither row is silently overwritten.

    This returns figures, not a verdict. With the mandated datasets each row rests on six
    observations, so a gap between two rows describes these runs and is not evidence of a
    difference between the approaches.
    """
    runs = [get_run(session, int(run_id)) for run_id in run_ids]
    missing = [rid for rid, run in zip(run_ids, runs) if run is None]
    if missing:
        raise ValueError("Unknown evaluation run id(s): {0}".format(missing))

    modes = [str(run.experiment_mode) for run in runs]
    duplicated = {mode for mode in modes if modes.count(mode) > 1}

    keyed: Dict[str, Any] = {}
    keys: List[str] = []
    per_key_run: Dict[str, EvaluationRun] = {}
    for run in runs:
        key = (
            "{0}#{1}".format(run.experiment_mode, run.id)
            if run.experiment_mode in duplicated
            else str(run.experiment_mode)
        )
        keys.append(key)
        keyed[key] = run_results(session, run.id) or list(run.results or [])
        per_key_run[key] = run

    frame = compare_modes(keyed)
    # compare_modes orders A, B, C; reindex onto the caller's order so the identifying
    # columns below line up with the right rows.
    frame = frame.reindex([_mode_label_or_key(key) for key in keys])

    frame.insert(0, "run_id", [per_key_run[key].id for key in keys])
    frame.insert(1, "run_name", [per_key_run[key].name for key in keys])
    frame.insert(2, "status", [per_key_run[key].status for key in keys])
    frame["latency_mean_ms"] = [
        _mean_latency(keyed[key]) for key in keys
    ]
    return frame


def _mean_latency(results: Sequence[Any]) -> float:
    value = timing_metrics(results, include_by_mode=False)["latency_ms"]["mean"]
    return float("nan") if value is None else float(value)


def prediction_matrix(session: Session, run_ids: Sequence[int]) -> pd.DataFrame:
    """Per-dataset expected vs predicted status, one column per run.

    The table the aggregate metrics are an average of, and the one to read first: an
    accuracy figure cannot show *which* case a mode got wrong, and for this study the
    identity of the failure (DATASET-005 above all) is the finding.
    """
    rows: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    columns: List[str] = []
    for run_id in run_ids:
        run = get_run(session, int(run_id))
        if run is None:
            raise ValueError("Unknown evaluation run id: {0}".format(run_id))
        column = "{0} (run {1})".format(run.experiment_mode, run.id)
        columns.append(column)
        for result in run_results(session, run.id):
            key = result.dataset_id
            if key not in rows:
                rows[key] = {
                    "dataset": key,
                    "control": result.control_ref,
                    "expected": result.expected_status,
                }
                order.append(key)
            rows[key][column] = result.predicted_status or "(no prediction)"

    if not order:
        return pd.DataFrame(columns=["dataset", "control", "expected"] + columns)
    frame = pd.DataFrame([rows[key] for key in sorted(order)])
    return frame.reindex(columns=["dataset", "control", "expected"] + columns)


# ---- cleanup


def is_evaluation_project(project: AuditProject) -> bool:
    """True only for a project this harness created.

    Keyed on the tag in ``scope_note``, never on the name: a real engagement an auditor
    named "[EVALUATION] ..." must not be deletable by :func:`cleanup_evaluation_projects`.
    """
    return EVALUATION_SCOPE_TAG in str(project.scope_note or "")


def evaluation_projects(session: Session, run_id: Optional[int] = None) -> List[AuditProject]:
    """Evaluation projects, optionally only those a given run assessed.

    Run scoping goes through ``Assessment.evaluation_run_id`` - the same link the service
    layer uses to keep these rows out of the dashboard - rather than through the run label
    in the project name, so it stays exact even if a name is edited.
    """
    stmt = select(AuditProject).where(AuditProject.scope_note.like("%{0}%".format(EVALUATION_SCOPE_TAG)))
    projects = [p for p in session.execute(stmt).scalars().all() if is_evaluation_project(p)]
    if run_id is None:
        return projects
    project_ids = set(
        session.execute(
            select(Assessment.project_id).where(Assessment.evaluation_run_id == int(run_id))
        )
        .scalars()
        .all()
    )
    return [p for p in projects if p.id in project_ids]


def cleanup_evaluation_projects(session: Session, run_id: Optional[int] = None) -> int:
    """Delete evaluation projects and their evidence; return how many were removed.

    With ``run_id`` only that run's projects go; without it, every evaluation project in
    the database. Evidence files are deleted through
    :func:`app.evidence.service.delete_evidence` first so the stored bytes leave the disk
    as well as the database - a project delete alone would cascade the rows and orphan the
    files under ``upload_dir``.

    This is destructive in a way worth stating plainly: the projects' ``Assessment`` rows
    cascade with them, taking the prompt snapshots, raw responses and citation verdicts.
    The ``EvaluationRun`` and its ``EvaluationResult`` rows survive - including every
    metric and the ``detail`` blob - but ``EvaluationResult.assessment_id`` becomes NULL
    and the underlying reasoning is no longer inspectable. Run it when disk space matters
    more than the trail, not as routine housekeeping.
    """
    projects = evaluation_projects(session, run_id)
    return _delete_projects(session, [project.id for project in projects])


def _delete_projects(session: Session, project_ids: Sequence[int]) -> int:
    removed = 0
    for project_id in dict.fromkeys(int(pid) for pid in project_ids):
        project = audit_service.get_project(session, project_id)
        if project is None or not is_evaluation_project(project):
            continue
        for evidence in list(project.evidence_files):
            delete_evidence(session, evidence.id)
        # The project still holds the deleted files in its loaded collection, and the
        # cascade would then issue DELETEs for rows that are already gone. Expiring it
        # makes the follow-up delete work from the current state of the database.
        session.expire(project)
        if audit_service.delete_project(session, project_id):
            removed += 1
    return removed


# ---- small helpers


def _coerce_mode(mode: Union[ExperimentMode, str]) -> ExperimentMode:
    """Resolve a mode, or refuse.

    Deliberately not tolerant: silently defaulting an unrecognised mode to C would label
    a run with a condition it did not run under, and every figure derived from it would
    be attributed to the wrong experiment.
    """
    resolved = ExperimentMode.coerce(mode, None)
    if resolved is None:
        raise ValueError(
            "Unknown experiment mode {0!r}. Expected one of: {1}.".format(
                mode, ", ".join(ExperimentMode.values())
            )
        )
    return resolved


def _resolve_datasets(dataset_ids: Optional[Sequence[Any]]) -> List[SyntheticDataset]:
    if dataset_ids is None:
        return list(SYNTHETIC_DATASETS)
    return [get_dataset(item) for item in dataset_ids]


def _resolve_files(
    datasets: Sequence[SyntheticDataset],
    generated_files: Optional[Mapping[str, Sequence[Any]]],
) -> Dict[str, List[Path]]:
    """Use the caller's generated files where supplied, generating anything missing."""
    if not generated_files:
        return prepare_datasets([dataset.dataset_id for dataset in datasets])
    resolved: Dict[str, List[Path]] = {}
    missing: List[str] = []
    for dataset in datasets:
        paths = generated_files.get(dataset.dataset_id)
        if not paths:
            missing.append(dataset.dataset_id)
            continue
        resolved[dataset.dataset_id] = [Path(path) for path in paths]
    if missing:
        resolved.update(prepare_datasets(missing))
    return resolved


def _default_run_name(mode: ExperimentMode, datasets: Sequence[SyntheticDataset]) -> str:
    return "{0} over {1} dataset(s) - {2}".format(
        mode.value, len(datasets), _now().strftime("%Y-%m-%d %H:%M:%S UTC")
    )


def _transient_label() -> str:
    """A sortable token used to label projects when there is no run id to use."""
    return _now().strftime("%Y%m%dT%H%M%S")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _mode_label(raw: Any) -> str:
    member = ExperimentMode.coerce(raw, None)
    return member.label if member is not None else str(raw or "")


def _mode_label_or_key(key: str) -> str:
    """The index label :func:`compare_modes` will have produced for one of our keys."""
    member = ExperimentMode.coerce(key, None)
    return member.value if member is not None else str(key).strip()


def _dig(payload: Optional[Mapping[str, Any]], *path: str) -> Any:
    node: Any = payload or {}
    for part in path:
        if not isinstance(node, _MappingABC):
            return None
        node = node.get(part)
    return node
