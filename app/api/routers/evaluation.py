"""The research evaluation harness: synthetic datasets, experiment runs and comparisons.

Two things about this router are worth stating plainly.

First, ``app.audit.service`` covers projects, controls, evidence, assessments, reviews
and dashboards, but it has no accessors for ``EvaluationRun`` / ``EvaluationResult``. The
two listing endpoints here therefore read those tables directly - deliberately, and only
as reads with no logic in them. The clean fix is a pair of ``list_evaluation_runs`` /
``get_evaluation_run`` helpers in the service layer, which this task does not own.

Second, the runner itself (``app.evaluation.runner``) is imported lazily. If it is not
present in this build, the endpoints that need it answer 503 with an explanation instead
of the whole API failing to import - and the dataset and listing endpoints keep working.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.base import get_db
from app.database.models import EvaluationRun
from app.evaluation.datasets import dataset_manifest
from app.evaluation.metrics import MIN_SAMPLE_SIZE
from app.schemas.api import (
    DatasetResponse,
    EvaluationCompareResponse,
    EvaluationRunDetailResponse,
    EvaluationRunRequest,
    EvaluationRunResponse,
)

router = APIRouter(prefix="/evaluation", tags=["evaluation"])

_RUNNER_UNAVAILABLE = (
    "The evaluation runner (app.evaluation.runner) is not available in this build, so "
    "experiments cannot be started over HTTP. The synthetic datasets and any runs already "
    "recorded remain readable."
)


@router.get(
    "/datasets",
    response_model=List[DatasetResponse],
    summary="The synthetic evaluation datasets",
    description=(
        "Every dataset is fabricated for this research prototype: it describes no real "
        "organisation, system, person or audit. Each entry states the condition planted in "
        "the files, the status a correct assessment should reach, and the evidence markers "
        "retrieval has to surface for that conclusion to be traceable."
    ),
)
def list_datasets() -> List[Dict[str, Any]]:
    return dataset_manifest()


@router.post(
    "/run",
    response_model=EvaluationRunDetailResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Run an experiment over the synthetic datasets (synchronous)",
    description=(
        "Generates each dataset's files, ingests them into an isolated evaluation project, "
        "assesses the control under the chosen mode, scores the prediction against the "
        "known ground truth and persists the run.\n\n"
        "**This request blocks until every dataset in the suite has been assessed.** With "
        "the offline mock provider the whole suite takes a few seconds; with a hosted model "
        "it is one to several minutes, because it is one full assessment per dataset (three "
        "model calls each in mode C). There is no background job and no progress endpoint - "
        "returning 202 for work that has not run would misrepresent exactly the latency this "
        "study is trying to measure. Raise the client timeout rather than splitting the run."
    ),
)
def run_experiment(
    payload: EvaluationRunRequest = Body(...),
    session: Session = Depends(get_db),
) -> Dict[str, Any]:
    runner = _load_runner()
    run = runner.run_experiment(
        session,
        mode=payload.mode,
        dataset_ids=payload.dataset_ids,
        run_name=payload.run_name,
    )
    return _run_payload(run, include_results=True)


@router.get(
    "/runs",
    response_model=List[EvaluationRunResponse],
    summary="List experiment runs",
    description="Newest first, without per-dataset results.",
)
def list_runs(
    session: Session = Depends(get_db),
    mode: Optional[str] = Query(default=None, description="Experiment mode, e.g. C_RAG_WORKFLOW."),
    limit: int = Query(default=50, ge=1, le=500),
) -> List[Dict[str, Any]]:
    stmt = select(EvaluationRun)
    if mode:
        stmt = stmt.where(EvaluationRun.experiment_mode == mode)
    stmt = stmt.order_by(EvaluationRun.started_at.desc(), EvaluationRun.id.desc()).limit(limit)
    return [_run_payload(row, include_results=False) for row in session.execute(stmt).scalars().all()]


@router.get(
    "/compare",
    response_model=EvaluationCompareResponse,
    summary="Compare completed runs side by side",
    description=(
        "Intended for the A/B/C comparison the research design calls for. Declared before "
        "'/runs/{run_id}' has any chance to shadow it, and it reports which module produced "
        "the rows so a reader knows whether the figures were recomputed or read back."
    ),
)
def compare_runs(
    session: Session = Depends(get_db),
    run_ids: List[int] = Query(..., description="Repeatable, e.g. ?run_ids=1&run_ids=2&run_ids=3."),
) -> Dict[str, Any]:
    runs = [_require_run(session, run_id) for run_id in run_ids]
    smallest = min([len(run.results) for run in runs], default=0)
    warning = ""
    if smallest < MIN_SAMPLE_SIZE:
        warning = (
            "Smallest run has {0} scored cases; below {1} these rates are descriptive of "
            "this suite only and carry no statistical weight.".format(smallest, MIN_SAMPLE_SIZE)
        )

    runner = _load_runner(required=False)
    if runner is not None and hasattr(runner, "compare_runs"):
        frame = runner.compare_runs(session, [run.id for run in runs])
        return {
            "run_ids": [run.id for run in runs],
            "rows": frame.to_dict(orient="records"),
            "source": "app.evaluation.runner.compare_runs",
            "warning": warning,
        }

    # Fallback: hand back what each run already recorded. Nothing is recomputed here -
    # a comparison the API invented would not match the one in the write-up.
    return {
        "run_ids": [run.id for run in runs],
        "rows": [
            {
                "run_id": run.id,
                "name": run.name,
                "experiment_mode": run.experiment_mode,
                "status": run.status,
                "result_count": len(run.results),
                "duration_seconds": run.duration_seconds,
                "metrics": dict(run.metrics or {}),
            }
            for run in runs
        ],
        "source": "stored EvaluationRun.metrics (app.evaluation.runner.compare_runs unavailable)",
        "warning": warning,
    }


@router.get(
    "/runs/{run_id}",
    response_model=EvaluationRunDetailResponse,
    summary="Get one experiment run with its scored results",
)
def get_run(
    run_id: int = Path(..., ge=1),
    session: Session = Depends(get_db),
) -> Dict[str, Any]:
    return _run_payload(_require_run(session, run_id), include_results=True)


# ---- internals
def _load_runner(required: bool = True) -> Any:
    """Import the evaluation runner on demand.

    Kept out of module scope so the API still starts, and every other endpoint still
    works, in a build where the harness is absent.
    """
    try:
        from app.evaluation import runner  # noqa: WPS433 - deliberate lazy import
    except ImportError:
        if required:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_RUNNER_UNAVAILABLE)
        return None
    return runner


def _require_run(session: Session, run_id: int) -> EvaluationRun:
    run = session.get(EvaluationRun, int(run_id))
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Evaluation run {0} not found.".format(run_id)
        )
    return run


def _run_payload(run: EvaluationRun, include_results: bool) -> Dict[str, Any]:
    payload = run.to_dict()
    payload["result_count"] = len(run.results)
    if include_results:
        payload["results"] = [result.to_dict() for result in run.results]
    return payload
