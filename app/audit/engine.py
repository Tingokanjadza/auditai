"""The assessment engine: the orchestrator the whole research design hangs on.

Everything else in this package is a component. This module is what turns those
components into the three *experimental conditions* the study compares, and it is the
only place where the difference between them is decided:

=========  ==========================================================================
Mode       What the model is given, and what is done with its answer
=========  ==========================================================================
``A``      No retrieval. Raw file text in upload order, crudely truncated, a naive
           persona, no chunk IDs, no citation scaffolding, no output schema. The
           answer is validated but **never corrected**. This is the baseline.
``B``      Retrieval + the full control requirement + a structured output schema. One
           call. Citations are validated and recorded, but no rail is applied.
``C``      Sufficiency pre-check -> assessment -> self-critique -> citation validation
           -> safety rails -> prototype risk scoring -> human-review gate.
=========  ==========================================================================

Why mode A is deliberately left unrailed
----------------------------------------
The safety rails in :mod:`app.audit.validators` downgrade an ungrounded conclusion to
INSUFFICIENT_EVIDENCE. Applying them to A would erase the very thing the experiment
exists to measure: how often an unscaffolded pipeline states a confident conclusion
that its evidence does not support. A railed baseline would make A look almost as safe
as C and the comparison would be circular. So A's conclusion is persisted exactly as
the model produced it, the validator is still run so the ungroundedness is *measured*,
and ``validation_report["engine"]["rails_enforced"]`` records that no rail was applied.
The same reasoning applies to B, which is the "RAG but no workflow" condition.

Two decisions worth challenging (both are deliberate)
-----------------------------------------------------
1. **Mode A is validated against the text it was actually shown**, not against an empty
   set. A's prompt carries no chunk IDs, so every citation it makes is untraceable by
   construction; scoring it against nothing would mark all of them FABRICATED and
   report a 100% hallucination rate that measures the prompt format rather than the
   model. Instead the engine records the raw chunks that fitted inside the budget as
   the "evidence shown", so a quotation that genuinely occurs in the supplied files
   scores as PARTIAL (real text, untraceable pointer) and only an invented one scores
   FABRICATED. ``Assessment.retrieval_strategy`` is still ``"NONE"`` and the result
   carries a note saying no retrieval was performed.
2. **Mode A's provider context carries a reduced control record** - reference, name and
   objective only. :func:`app.audit.prompts.build_raw_prompt` withholds the expected
   evidence and the assessment criteria on purpose; handing a provider the full
   :class:`~app.database.models.Control` row through
   :class:`~app.llm.base.LLMCallContext` would give the baseline through the side door
   exactly what the prompt withholds, and the A-vs-C comparison would be worthless.

Telemetry is a research measurement, not a progress bar
-------------------------------------------------------
Latency is wall clock, taken with :func:`time.perf_counter` around each call and around
the whole assessment. Retrieval time and LLM time are recorded separately and never
merged: a mode that spends 200 ms retrieving is not slower *at inference* than one that
does not retrieve, and conflating them would misattribute the cost of the workflow.
Token counts are whatever the provider reported, summed across steps. Nothing here is
estimated when a real figure was available, and nothing is rounded in a flattering
direction.

Failure is recorded, never swallowed
------------------------------------
A provider error, unparseable JSON or a schema violation produces a persisted
:class:`~app.database.models.Assessment` row with status INSUFFICIENT_EVIDENCE, the
error text in ``error`` and ``human_review_required`` true. An audit run that quietly
loses a control is worse than one that records a failure an auditor can see and re-run.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import service as audit_service
from app.audit.prompts import (
    PROMPT_VERSION,
    RAW_BASELINE_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_assessment_prompt,
    build_critique_prompt,
    build_raw_prompt,
    build_sufficiency_prompt,
    prompt_metadata,
)
from app.audit.risk import RISK_MODEL_LABEL, RiskAssessment, score_risk
from app.audit.validators import (
    ValidationReport,
    detect_unsupported_claims,
    enforce_safety_rails,
    validate_citations,
)
from app.config import Settings, get_settings
from app.database.models import (
    Assessment,
    AssessmentCitation,
    Control,
    EvidenceChunk,
    EvidenceFile,
)
from app.evidence.service import locator_from_chunk
from app.llm.base import LLMCallContext, LLMProvider, LLMResponse, extract_json
from app.llm.factory import get_llm_provider
from app.rag.base import BaseRetriever, RetrievalResult, RetrievedChunk
from app.rag.query_builder import build_queries
from app.rag.retriever import get_retriever
from app.schemas.assessment import (
    AssessmentOutput,
    SelfCritique,
    SufficiencyCheck,
    assessment_json_schema,
)
from app.schemas.enums import (
    ActivityAction,
    AssessmentStatus,
    ConfidenceLevel,
    EvidenceSufficiency,
    ExperimentMode,
    RiskLevel,
)

logger = logging.getLogger(__name__)

#: Bumped when the orchestration changes shape (a step added, an order changed), so a
#: stored assessment can be traced to the workflow that produced it.
ENGINE_VERSION = "1.0.0"

#: The strategy string recorded for Experiment A. It is not a retrieval strategy; it is
#: the explicit absence of one, and it must be distinguishable from KEYWORD/VECTOR/HYBRID
#: in every downstream table.
NO_RETRIEVAL = "NONE"

#: Display-only numeric rendering of the model's own confidence label. It is NOT a
#: calibrated probability and must never be presented as one - it exists because
#: ``Assessment.confidence_score`` is a float column and sorting on a string is worse.
_CONFIDENCE_SCORES: Dict[str, float] = {
    ConfidenceLevel.LOW.value: 0.33,
    ConfidenceLevel.MEDIUM.value: 0.66,
    ConfidenceLevel.HIGH.value: 1.0,
}

#: Statuses that assert nothing about how the control operates, and so can never be
#: "unsupported by the evidence" - there is nothing to support.
_NON_ASSERTING = (AssessmentStatus.INSUFFICIENT_EVIDENCE, AssessmentStatus.NOT_APPLICABLE)

#: How strong an assertion each status makes. A "downgrade" means moving toward
#: INSUFFICIENT_EVIDENCE - i.e. claiming less - which is the only direction the
#: self-critique step is permitted to move a conclusion.
_ASSERTION_RANK: Dict[str, int] = {
    AssessmentStatus.EFFECTIVE.value: 3,
    AssessmentStatus.NOT_EFFECTIVE.value: 3,
    AssessmentStatus.POTENTIAL_DEFICIENCY.value: 2,
    AssessmentStatus.NOT_APPLICABLE.value: 1,
    AssessmentStatus.INSUFFICIENT_EVIDENCE.value: 0,
}

#: Separator used between evidence files, and between chunks of one file, when the raw
#: Experiment A blob is assembled. Kept here because chunk offsets are computed against
#: it: change one and the "what the model could see" accounting changes with it.
_RAW_BLOCK_SEPARATOR = "\n\n"


class AssessmentEngineError(RuntimeError):
    """The model could not be called, or its answer could not be used."""


# ---- telemetry


@dataclass
class StepTelemetry:
    """One measured step of a run. Persisted so a latency figure can be explained."""

    name: str
    kind: str = "llm"
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    ok: bool = True
    error: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "latency_ms": self.latency_ms,
            "ok": self.ok,
        }
        if self.kind == "llm":
            out["prompt_tokens"] = self.prompt_tokens
            out["completion_tokens"] = self.completion_tokens
        if self.error:
            out["error"] = self.error
        if self.detail:
            out["detail"] = dict(self.detail)
        return out


@dataclass
class AssessmentRunResult:
    """Everything one assessment produced, whether or not it was persisted.

    ``output`` is what the system stands behind (post-rails in mode C);
    ``model_output`` is the unedited model answer. In modes A and B they are the same
    object's content, because no rail is applied - and that identity is itself part of
    the experimental record.
    """

    assessment_id: Optional[int] = None
    output: AssessmentOutput = field(default_factory=AssessmentOutput)
    retrieval: RetrievalResult = field(default_factory=RetrievalResult)
    validation: ValidationReport = field(default_factory=ValidationReport)
    risk: RiskAssessment = field(default_factory=RiskAssessment)
    latency_ms: int = 0
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw_response: str = ""
    prompt_snapshot: str = ""
    error: str = ""
    sufficiency: Optional[SufficiencyCheck] = None
    critique: Optional[SelfCritique] = None
    mode: ExperimentMode = ExperimentMode.C_RAG_WORKFLOW

    # ---- beyond the spec, because the harness and the UI need them
    model_output: Optional[AssessmentOutput] = None
    control_ref: str = ""
    project_id: Optional[int] = None
    retrieval_ms: int = 0
    llm_ms: int = 0
    steps: List[StepTelemetry] = field(default_factory=list)
    rails_enforced: bool = False
    telemetry: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def status(self) -> AssessmentStatus:
        return self.output.status

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> Dict[str, Any]:
        return {
            "assessment_id": self.assessment_id,
            "control_ref": self.control_ref,
            "project_id": self.project_id,
            "mode": self.mode.value,
            "status": self.output.status.value,
            "risk_level": self.risk.level.value,
            "risk_score": round(self.risk.score, 2),
            "latency_ms": self.latency_ms,
            "retrieval_ms": self.retrieval_ms,
            "llm_ms": self.llm_ms,
            "llm_calls": self.llm_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "rails_enforced": self.rails_enforced,
            "error": self.error,
            "retrieval": {
                "strategy": self.retrieval.strategy,
                "chunk_ids": list(self.retrieval.chunk_ids),
                "queries": list(self.retrieval.queries),
                "notes": list(self.retrieval.notes),
            },
            "validation": self.validation.to_dict(),
            "output": self.output.model_dump(mode="json"),
            "steps": [step.to_dict() for step in self.steps],
        }


@dataclass
class _RawEvidence:
    """The Experiment A evidence blob, plus an honest account of what fitted in it."""

    body: str = ""
    shown: List[RetrievedChunk] = field(default_factory=list)
    dropped_chunk_ids: List[int] = field(default_factory=list)
    total_chars: int = 0
    shown_chars: int = 0
    truncated: bool = False
    file_count: int = 0
    chunk_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "files": self.file_count,
            "chunks_available": self.chunk_count,
            "chunks_shown": len(self.shown),
            "chunks_dropped": len(self.dropped_chunk_ids),
            "dropped_chunk_ids": list(self.dropped_chunk_ids),
            "total_chars": self.total_chars,
            "shown_chars": self.shown_chars,
            "truncated": self.truncated,
        }


@dataclass
class _ModelPass:
    """Result of the LLM half of a run, before validation, rails and risk."""

    output: Optional[AssessmentOutput] = None
    raw_response: str = ""
    prompt_snapshot: str = ""
    error: str = ""
    sufficiency: Optional[SufficiencyCheck] = None
    critique: Optional[SelfCritique] = None
    exception_ratio: Optional[float] = None
    detail: Dict[str, Any] = field(default_factory=dict)


# ---- the engine


class AssessmentEngine:
    """Runs one control assessment end to end under a chosen experimental condition."""

    def __init__(
        self,
        session: Session,
        llm: Optional[LLMProvider] = None,
        retriever: Optional[BaseRetriever] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        # One provider instance for the life of the engine. The OpenAI-compatible
        # provider learns which structured-output modes an endpoint rejects; a fresh
        # instance per call would rediscover that on every step of every control.
        self.llm = llm or get_llm_provider(settings=self.settings)
        self._retriever = retriever

    # ------------------------------------------------------------------ public
    @property
    def retriever(self) -> BaseRetriever:
        """Built lazily so Experiment A never constructs an index it will not use."""
        if self._retriever is None:
            self._retriever = get_retriever(self.session, settings=self.settings)
        return self._retriever

    def assess_control(
        self,
        project_id: int,
        control_id_or_ref: Any,
        mode: Union[ExperimentMode, str] = ExperimentMode.C_RAG_WORKFLOW,
        persist: bool = True,
        evaluation_run_id: Optional[int] = None,
    ) -> AssessmentRunResult:
        """Assess one control and (by default) persist the result.

        Raises only when the *request itself* is invalid - an unknown project or
        control - because there is then nothing to attach a result to. Every failure
        after that point is captured in the returned result and, when ``persist`` is
        true, in a persisted row.
        """
        run_started = time.perf_counter()
        resolved_mode = ExperimentMode.coerce(mode, ExperimentMode.C_RAG_WORKFLOW)
        project = audit_service.require_project(self.session, int(project_id))
        control = audit_service.require_control(self.session, control_id_or_ref)

        steps: List[StepTelemetry] = []

        retrieval, raw_evidence = self._gather_evidence(project.id, control, resolved_mode, steps)

        if resolved_mode is ExperimentMode.A_RAW_LLM:
            model_pass = self._run_baseline(control, project, raw_evidence, steps)
        elif resolved_mode is ExperimentMode.B_RAG:
            model_pass = self._run_single_call(control, project, retrieval, steps)
        else:
            model_pass = self._run_workflow(control, project, retrieval, steps)

        failed = model_pass.output is None
        model_output = self._failure_output(control, model_pass.error) if failed else model_pass.output

        # The validator runs in every mode, including the ones whose output is never
        # corrected. The point of leaving A and B unrailed is to observe how ungrounded
        # they are, and that observation only exists if the measurement is still taken.
        report = validate_citations(model_output, retrieval)
        report.unsupported_claims = detect_unsupported_claims(model_output, retrieval, control)

        rails_enforced = resolved_mode is ExperimentMode.C_RAG_WORKFLOW and not failed
        if rails_enforced:
            output = self._apply_workflow_corrections(model_output, model_pass.critique, report)
        else:
            output = model_output
            report.original_status = model_output.status.value

        risk = self._score(control, output, model_pass.exception_ratio, failed)

        llm_steps = [s for s in steps if s.kind == "llm"]
        llm_ms = sum(s.latency_ms for s in llm_steps)
        retrieval_ms = sum(s.latency_ms for s in steps if s.kind == "retrieval")
        total_ms = int((time.perf_counter() - run_started) * 1000)

        result = AssessmentRunResult(
            output=output,
            model_output=model_output,
            retrieval=retrieval,
            validation=report,
            risk=risk,
            latency_ms=total_ms,
            llm_calls=len(llm_steps),
            prompt_tokens=sum(s.prompt_tokens for s in llm_steps),
            completion_tokens=sum(s.completion_tokens for s in llm_steps),
            raw_response=model_pass.raw_response,
            prompt_snapshot=model_pass.prompt_snapshot,
            error=model_pass.error,
            sufficiency=model_pass.sufficiency,
            critique=model_pass.critique,
            mode=resolved_mode,
            control_ref=control.control_id,
            project_id=project.id,
            retrieval_ms=retrieval_ms,
            llm_ms=llm_ms,
            steps=steps,
            rails_enforced=rails_enforced,
        )
        result.telemetry = self._telemetry(result, raw_evidence, model_pass, failed)

        if persist:
            self._persist(project.id, control, result, evaluation_run_id)
        return result

    def assess_project(
        self,
        project_id: int,
        mode: Union[ExperimentMode, str] = ExperimentMode.C_RAG_WORKFLOW,
        control_refs: Optional[Sequence[Any]] = None,
        persist: bool = True,
        evaluation_run_id: Optional[int] = None,
    ) -> List[AssessmentRunResult]:
        """Assess every control in scope, or the subset named by ``control_refs``.

        ``control_refs=None`` means "everything scoped to this project". One control
        failing never stops the rest: a failure is recorded as a result carrying the
        error, because a half-finished audit run whose gaps are invisible is worse than
        one that says which controls did not complete.
        """
        project = audit_service.require_project(self.session, int(project_id))
        if control_refs is None:
            controls: List[Any] = list(audit_service.list_scoped_controls(self.session, project.id))
        else:
            controls = list(control_refs)

        results: List[AssessmentRunResult] = []
        for item in controls:
            try:
                results.append(
                    self.assess_control(
                        project.id,
                        item,
                        mode=mode,
                        persist=persist,
                        evaluation_run_id=evaluation_run_id,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one bad control must not end the run
                logger.exception("Assessment of %r failed outright", item)
                ref = item.control_id if isinstance(item, Control) else str(item)
                results.append(
                    AssessmentRunResult(
                        output=self._failure_output(None, str(exc), control_ref=ref),
                        mode=ExperimentMode.coerce(mode, ExperimentMode.C_RAG_WORKFLOW),
                        control_ref=ref,
                        project_id=project.id,
                        error=str(exc),
                    )
                )
        return results

    # ------------------------------------------------------------- evidence
    def _gather_evidence(
        self,
        project_id: int,
        control: Control,
        mode: ExperimentMode,
        steps: List[StepTelemetry],
    ) -> Tuple[RetrievalResult, Optional[_RawEvidence]]:
        """Retrieve for B/C; assemble the raw blob for A. Never raises."""
        if mode is ExperimentMode.A_RAW_LLM:
            started = time.perf_counter()
            raw = self._collect_raw_evidence(project_id)
            elapsed = int((time.perf_counter() - started) * 1000)
            steps.append(
                StepTelemetry(
                    name="raw_evidence",
                    kind="retrieval",
                    latency_ms=elapsed,
                    detail=raw.to_dict(),
                )
            )
            retrieval = RetrievalResult(
                chunks=list(raw.shown),
                queries=[],
                strategy=NO_RETRIEVAL,
                total_candidates=raw.chunk_count,
                elapsed_ms=elapsed,
                notes=[
                    "Experiment A performs no retrieval. These chunks are the raw file text, in "
                    "upload order, that fitted inside the raw evidence budget and was pasted into "
                    "the prompt. They are recorded so the model's citations remain checkable; the "
                    "model was shown no chunk IDs and no source metadata.",
                ],
            )
            if raw.truncated:
                retrieval.notes.append(
                    "The blob was truncated at {0} of {1} characters; {2} chunk(s) were never "
                    "shown to the model.".format(raw.shown_chars, raw.total_chars, len(raw.dropped_chunk_ids))
                )
            return retrieval, raw

        started = time.perf_counter()
        queries = build_queries(control)
        try:
            retrieval = self.retriever.retrieve(
                queries,
                project_id,
                top_k=self.settings.retrieval_top_k,
                min_score=self.settings.retrieval_min_score,
            )
            error = ""
        except Exception as exc:  # noqa: BLE001 - a retrieval failure is a finding, not a crash
            logger.exception("Retrieval failed for project %s", project_id)
            error = str(exc)
            retrieval = RetrievalResult(
                queries=list(queries),
                strategy=str(self.settings.retrieval_strategy),
                notes=["Retrieval failed: {0}. The model was given no evidence.".format(error)],
            )
        elapsed = int((time.perf_counter() - started) * 1000)
        steps.append(
            StepTelemetry(
                name="retrieval",
                kind="retrieval",
                latency_ms=elapsed,
                ok=not error,
                error=error,
                detail={
                    "strategy": retrieval.strategy,
                    "queries": len(retrieval.queries),
                    "chunks": len(retrieval.chunks),
                    "candidates": retrieval.total_candidates,
                },
            )
        )
        return retrieval, None

    def _collect_raw_evidence(self, project_id: int) -> _RawEvidence:
        """Concatenate every stored chunk of the project, in upload order.

        This is the naive thing a developer without a retrieval layer would do, and it
        is what Experiment A is a measurement of. The offsets are tracked so the engine
        can say afterwards which chunks actually survived
        ``settings.raw_evidence_char_budget`` - a truncated exception row is a
        legitimate explanation for a missed finding, and it must be visible in the
        record rather than inferred.
        """
        stmt = (
            select(EvidenceChunk, EvidenceFile.filename)
            .join(EvidenceFile, EvidenceChunk.evidence_file_id == EvidenceFile.id)
            .where(EvidenceChunk.project_id == int(project_id))
            .order_by(EvidenceChunk.evidence_file_id.asc(), EvidenceChunk.chunk_index.asc())
        )
        rows = list(self.session.execute(stmt).all())
        bundle = _RawEvidence()
        if not rows:
            return bundle

        pieces: List[str] = []
        cursor = 0
        spans: List[Tuple[EvidenceChunk, int, int]] = []
        current_file: Optional[int] = None
        files: List[int] = []

        for chunk, filename in rows:
            if chunk.evidence_file_id != current_file:
                header = "--- {0} ---\n".format(filename or chunk.filename or "(unnamed file)")
                if pieces:
                    header = _RAW_BLOCK_SEPARATOR + header
                pieces.append(header)
                cursor += len(header)
                current_file = chunk.evidence_file_id
                files.append(chunk.evidence_file_id)
            elif pieces:
                pieces.append(_RAW_BLOCK_SEPARATOR)
                cursor += len(_RAW_BLOCK_SEPARATOR)
            text = chunk.text or ""
            pieces.append(text)
            spans.append((chunk, cursor, cursor + len(text)))
            cursor += len(text)

        body = "".join(pieces)
        budget = int(self.settings.raw_evidence_char_budget)
        cut = min(len(body), budget)

        shown: List[RetrievedChunk] = []
        dropped: List[int] = []
        for chunk, start, end in spans:
            if start >= cut:
                dropped.append(chunk.id)
                continue
            visible = (chunk.text or "")[: max(0, cut - start)]
            if not visible.strip():
                dropped.append(chunk.id)
                continue
            shown.append(
                RetrievedChunk(
                    chunk_id=chunk.id,
                    # The text recorded is the text the model could actually read, so a
                    # quotation from a truncated tail is correctly scored as unsupported.
                    text=visible,
                    locator=locator_from_chunk(chunk),
                    score=0.0,
                    evidence_file_id=chunk.evidence_file_id,
                    filename=chunk.filename,
                    source_type=chunk.source_type,
                    rank=len(shown) + 1,
                    extra_metadata={"shown_in_raw_prompt": True, "truncated": end > cut},
                )
            )

        bundle.body = body
        bundle.shown = shown
        bundle.dropped_chunk_ids = dropped
        bundle.total_chars = len(body)
        bundle.shown_chars = cut
        bundle.truncated = len(body) > budget
        bundle.file_count = len(files)
        bundle.chunk_count = len(spans)
        return bundle

    # ------------------------------------------------------------ mode A
    def _run_baseline(
        self,
        control: Control,
        project: Any,
        raw_evidence: Optional[_RawEvidence],
        steps: List[StepTelemetry],
    ) -> _ModelPass:
        """Experiment A: one call, naive persona, no schema, no citation scaffolding."""
        bundle = raw_evidence or _RawEvidence()
        prompt = build_raw_prompt(control, [bundle.body] if bundle.body else [], project)

        response, telemetry = self._complete(
            step="assessment",
            purpose="assessment",
            system=RAW_BASELINE_SYSTEM_PROMPT,
            user=prompt,
            control=_baseline_control_payload(control),
            # No chunks: the baseline provider must see one undifferentiated blob, with
            # no source types and no evidence-type labels, exactly as the prompt does.
            chunks=[],
            extras={
                "mode": ExperimentMode.A_RAW_LLM.value,
                "raw_mode": True,
                "retrieval_performed": False,
            },
            # No JSON schema either. Schema-constrained decoding is part of the
            # scaffolding under test, so granting it to the baseline would move the
            # comparison in A's favour.
            json_schema=None,
        )
        steps.append(telemetry)
        if response is None:
            return _ModelPass(error=telemetry.error, prompt_snapshot=prompt)

        try:
            output = _parse(response.text, AssessmentOutput)
        except AssessmentEngineError as exc:
            telemetry.ok = False
            telemetry.error = str(exc)
            return _ModelPass(error=str(exc), raw_response=response.text, prompt_snapshot=prompt)

        return _ModelPass(
            output=output,
            raw_response=response.text,
            prompt_snapshot=prompt,
            exception_ratio=_exception_ratio(response),
            detail={"raw_evidence": bundle.to_dict()},
        )

    # ------------------------------------------------------------ mode B
    def _run_single_call(
        self,
        control: Control,
        project: Any,
        retrieval: RetrievalResult,
        steps: List[StepTelemetry],
    ) -> _ModelPass:
        """Experiment B: retrieval + full control + schema, one call, no workflow."""
        prompt = build_assessment_prompt(control, retrieval, ExperimentMode.B_RAG, project)
        response, telemetry = self._complete(
            step="assessment",
            purpose="assessment",
            system=SYSTEM_PROMPT,
            user=prompt,
            control=control,
            chunks=list(retrieval.chunks),
            extras={"mode": ExperimentMode.B_RAG.value, "retrieval_performed": True},
            json_schema=assessment_json_schema(),
        )
        steps.append(telemetry)
        if response is None:
            return _ModelPass(error=telemetry.error, prompt_snapshot=prompt)

        try:
            output = _parse(response.text, AssessmentOutput)
        except AssessmentEngineError as exc:
            telemetry.ok = False
            telemetry.error = str(exc)
            return _ModelPass(error=str(exc), raw_response=response.text, prompt_snapshot=prompt)

        return _ModelPass(
            output=output,
            raw_response=response.text,
            prompt_snapshot=prompt,
            exception_ratio=_exception_ratio(response),
        )

    # ------------------------------------------------------------ mode C
    def _run_workflow(
        self,
        control: Control,
        project: Any,
        retrieval: RetrievalResult,
        steps: List[StepTelemetry],
    ) -> _ModelPass:
        """Experiment C: sufficiency pre-check, assessment, self-critique.

        The pre-check never *replaces* the assessment. If it says the evidence cannot
        support a conclusion, the assessment still runs - but the pre-check's finding
        is put in front of the model first, so the assessment is written knowing that a
        separate step already judged the attribute under test to be absent. Skipping
        the assessment instead would produce a conclusion nobody could inspect and
        would make C incomparable with A and B, which always answer.
        """
        detail: Dict[str, Any] = {}
        sufficiency = self._sufficiency_step(control, project, retrieval, steps, detail)

        prompt = build_assessment_prompt(control, retrieval, ExperimentMode.C_RAG_WORKFLOW, project)
        extras: Dict[str, Any] = {
            "mode": ExperimentMode.C_RAG_WORKFLOW.value,
            "retrieval_performed": True,
        }
        if sufficiency is not None:
            extras["sufficiency"] = sufficiency.model_dump(mode="json")
            if not sufficiency.can_conclude:
                prompt = _sufficiency_addendum(sufficiency) + "\n\n" + prompt
                detail["assessment_prompt_augmented"] = True

        response, telemetry = self._complete(
            step="assessment",
            purpose="assessment",
            system=SYSTEM_PROMPT,
            user=prompt,
            control=control,
            chunks=list(retrieval.chunks),
            extras=extras,
            json_schema=assessment_json_schema(),
        )
        steps.append(telemetry)
        if response is None:
            return _ModelPass(error=telemetry.error, prompt_snapshot=prompt, sufficiency=sufficiency, detail=detail)

        try:
            output = _parse(response.text, AssessmentOutput)
        except AssessmentEngineError as exc:
            telemetry.ok = False
            telemetry.error = str(exc)
            return _ModelPass(
                error=str(exc),
                raw_response=response.text,
                prompt_snapshot=prompt,
                sufficiency=sufficiency,
                detail=detail,
            )

        critique = self._critique_step(control, project, retrieval, output, steps, detail)
        return _ModelPass(
            output=output,
            raw_response=response.text,
            prompt_snapshot=prompt,
            sufficiency=sufficiency,
            critique=critique,
            exception_ratio=_exception_ratio(response),
            detail=detail,
        )

    def _sufficiency_step(
        self,
        control: Control,
        project: Any,
        retrieval: RetrievalResult,
        steps: List[StepTelemetry],
        detail: Dict[str, Any],
    ) -> Optional[SufficiencyCheck]:
        """Step 1. A failure here degrades the workflow; it does not end the run."""
        prompt = build_sufficiency_prompt(control, retrieval, project)
        response, telemetry = self._complete(
            step="sufficiency",
            purpose="sufficiency",
            system=SYSTEM_PROMPT,
            user=prompt,
            control=control,
            chunks=list(retrieval.chunks),
            extras={"mode": ExperimentMode.C_RAG_WORKFLOW.value, "retrieval_performed": True},
            json_schema=_schema_for(SufficiencyCheck, "EvidenceSufficiencyCheck"),
        )
        steps.append(telemetry)
        if response is None:
            detail["sufficiency_error"] = telemetry.error
            return None
        try:
            check = _parse(response.text, SufficiencyCheck)
        except AssessmentEngineError as exc:
            telemetry.ok = False
            telemetry.error = str(exc)
            detail["sufficiency_error"] = str(exc)
            return None
        telemetry.detail["can_conclude"] = check.can_conclude
        telemetry.detail["evidence_sufficiency"] = check.evidence_sufficiency.value
        detail["sufficiency"] = check.model_dump(mode="json")
        return check

    def _critique_step(
        self,
        control: Control,
        project: Any,
        retrieval: RetrievalResult,
        draft: AssessmentOutput,
        steps: List[StepTelemetry],
        detail: Dict[str, Any],
    ) -> Optional[SelfCritique]:
        """Step 3. Advisory: the mechanical validator, not this, is what is measured."""
        prompt = build_critique_prompt(control, retrieval, draft, project)
        response, telemetry = self._complete(
            step="critique",
            purpose="critique",
            system=SYSTEM_PROMPT,
            user=prompt,
            control=control,
            chunks=list(retrieval.chunks),
            extras={
                "mode": ExperimentMode.C_RAG_WORKFLOW.value,
                "retrieval_performed": True,
                "draft": draft.model_dump(mode="json"),
            },
            json_schema=_schema_for(SelfCritique, "AssessmentSelfCritique"),
        )
        steps.append(telemetry)
        if response is None:
            detail["critique_error"] = telemetry.error
            return None
        try:
            critique = _parse(response.text, SelfCritique)
        except AssessmentEngineError as exc:
            telemetry.ok = False
            telemetry.error = str(exc)
            detail["critique_error"] = str(exc)
            return None
        telemetry.detail["overstated_conclusion"] = critique.overstated_conclusion
        telemetry.detail["unsupported_claims"] = len(critique.unsupported_claims)
        telemetry.detail["fabricated_citations"] = len(critique.fabricated_citations)
        detail["critique"] = critique.model_dump(mode="json")
        return critique

    # -------------------------------------------------------- corrections
    def _apply_workflow_corrections(
        self,
        model_output: AssessmentOutput,
        critique: Optional[SelfCritique],
        report: ValidationReport,
    ) -> AssessmentOutput:
        """Mode C only: the workflow's own corrections, then the mechanical rails.

        Two corrections happen here, in this order, before
        :func:`app.audit.validators.enforce_safety_rails` runs.

        **Nothing retrieved.** If retrieval returned no chunks at all, the model was
        shown no evidence, and any status other than INSUFFICIENT_EVIDENCE is
        unsupported by construction rather than by degree. This is caught here rather
        than in the validator because the validator is handed the citations, not the
        knowledge that the evidence set was empty, and because the standing rails
        deliberately leave POTENTIAL_DEFICIENCY alone - a sensible policy for a hedged
        flag drawn from real evidence, and the wrong one for a hedged flag drawn from
        none.

        **The self-critique.** The model's critique is never trusted on its own. A
        downgrade is applied only when the critique asks for one *and* the mechanical
        validator independently found the conclusion ungrounded - nothing verified,
        something fabricated, or a figure that appears nowhere in the evidence.
        Requiring both stops a model talking itself out of a correct, well-cited
        finding, which would be a false negative introduced by the workflow itself.
        """
        working = model_output
        if not report.retrieved_chunk_ids and model_output.status not in _NON_ASSERTING:
            working = model_output.model_copy(deep=True)
            working.status = AssessmentStatus.INSUFFICIENT_EVIDENCE
            working.confidence = ConfidenceLevel.LOW
            working.evidence_sufficiency = EvidenceSufficiency.NONE
            _require_verification(
                working,
                "No evidence was retrieved for this control. Supply the expected artefacts and "
                "re-run, or test this control manually.",
            )
            report.rails_applied.append(
                "no_evidence_retrieved: retrieval returned no evidence chunks for this control, so "
                "'{0}' rested on nothing that was actually supplied. It was withdrawn to "
                "INSUFFICIENT_EVIDENCE.".format(model_output.status.value)
            )

        requested = _requested_downgrade(working, critique)
        if requested is not None:
            agrees, why = _validator_agrees_unsupported(report)
            current = working.status.value
            if agrees:
                working = working.model_copy(deep=True)
                working.status = requested
                working.confidence = ConfidenceLevel.LOW
                report.rails_applied.append(
                    "critique_downgrade: the self-critique recommended {0} instead of {1} and the "
                    "citation validator independently agreed the conclusion was unsupported ({2}). "
                    "The downgrade was applied.".format(requested.value, current, why)
                )
            else:
                report.rails_applied.append(
                    "critique_downgrade_declined: the self-critique recommended {0} instead of {1}, "
                    "but the citation validator found the conclusion grounded ({2} of {3} citation(s) "
                    "verified, none fabricated, no unsupported figures). The model's conclusion "
                    "stands and the disagreement is recorded for the reviewer.".format(
                        requested.value, current, report.verified, report.total
                    )
                )

        guarded = enforce_safety_rails(working, report)
        # enforce_safety_rails records the status it was handed. When the critique moved
        # the status first, that is not what the model said - so the model's own answer
        # is restored here, because the research record must show what the model
        # produced, not what the workflow had already corrected.
        report.original_status = model_output.status.value
        report.status_downgraded = guarded.status.value != model_output.status.value
        return guarded

    def _score(
        self,
        control: Control,
        output: AssessmentOutput,
        exception_ratio: Optional[float],
        failed: bool,
    ) -> RiskAssessment:
        """Prototype risk scoring. Applied in every mode so the modes are comparable.

        A run that never completed is left NOT_RATED: recording MEDIUM risk because a
        provider timed out would put a risk band next to a control nobody assessed.
        """
        if failed:
            return RiskAssessment(
                level=RiskLevel.NOT_RATED,
                score=0.0,
                factors={},
                rationale=(
                    "No risk rating was computed: the assessment did not complete, so there is no "
                    "assessed outcome to rate. Re-run the assessment or assess this control manually."
                ),
                model_label=RISK_MODEL_LABEL,
            )
        return score_risk(
            control,
            output.status,
            exception_ratio=exception_ratio,
            model_risk_level=output.risk_level,
        )

    # ------------------------------------------------------------ provider
    def _complete(
        self,
        step: str,
        purpose: str,
        system: str,
        user: str,
        control: Any,
        chunks: Sequence[Any],
        extras: Dict[str, Any],
        json_schema: Optional[Dict[str, Any]],
    ) -> Tuple[Optional[LLMResponse], StepTelemetry]:
        """One provider call, timed, with every failure turned into telemetry.

        The latency recorded is the engine's own wall clock around the call, not the
        provider's self-report: retries and client-side backoff are part of what a user
        waits for and must not be excluded from a latency measurement.
        """
        call_extras = dict(extras)
        # Stated explicitly so a provider can tell "this mode retrieves nothing" from
        # "retrieval ran and found nothing" - two situations that look identical from an
        # empty chunk list and warrant opposite behaviour.
        call_extras["evidence_chunks_supplied"] = len(chunks)
        context = LLMCallContext(
            purpose=purpose,
            control=control if isinstance(control, dict) else _control_payload(control),
            chunks=list(chunks),
            extras=call_extras,
        )
        telemetry = StepTelemetry(name=step, kind="llm")
        started = time.perf_counter()
        try:
            response = self.llm.complete(
                system=system,
                user=user,
                json_schema=json_schema,
                temperature=self.settings.llm_temperature,
                max_tokens=self.settings.llm_max_tokens,
                context=context,
            )
        except Exception as exc:  # noqa: BLE001 - provider errors are data, not crashes
            telemetry.latency_ms = int((time.perf_counter() - started) * 1000)
            telemetry.ok = False
            telemetry.error = "{0}: {1}".format(type(exc).__name__, exc)
            logger.warning("LLM step %r failed: %s", step, telemetry.error)
            return None, telemetry

        telemetry.latency_ms = int((time.perf_counter() - started) * 1000)
        telemetry.prompt_tokens = int(response.prompt_tokens or 0)
        telemetry.completion_tokens = int(response.completion_tokens or 0)
        telemetry.detail["provider"] = response.provider or self.llm.name
        telemetry.detail["model"] = response.model or self.llm.model
        telemetry.detail["prompt_chars"] = len(user or "")
        if response.finish_reason:
            telemetry.detail["finish_reason"] = response.finish_reason
        return response, telemetry

    # ---------------------------------------------------------- persistence
    def _persist(
        self,
        project_id: int,
        control: Control,
        result: AssessmentRunResult,
        evaluation_run_id: Optional[int],
    ) -> Assessment:
        output = result.output
        row = Assessment(
            project_id=int(project_id),
            control_id=control.id,
            control_ref=control.control_id,
            experiment_mode=result.mode.value,
            status=output.status.value,
            assessment=output.assessment,
            finding=output.finding,
            risk=output.risk,
            risk_level=result.risk.level.value,
            risk_score=float(result.risk.score),
            risk_factors=dict(result.risk.to_dict()),
            evidence_sufficiency=output.evidence_sufficiency.value,
            missing_evidence=list(output.missing_evidence),
            reasoning=output.reasoning,
            recommendation=output.recommendation,
            inferences=list(output.inferences),
            human_verification_required=list(output.human_verification_required),
            confidence=output.confidence.value,
            confidence_score=_CONFIDENCE_SCORES.get(output.confidence.value, 0.0),
            # Never negotiable, and never taken from the model's answer.
            human_review_required=True,
            llm_provider=self.llm.name,
            llm_model=str(self.llm.model or ""),
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            latency_ms=result.latency_ms,
            llm_calls=result.llm_calls,
            retrieval_strategy=result.retrieval.strategy or NO_RETRIEVAL,
            retrieval_top_k=0 if result.mode is ExperimentMode.A_RAW_LLM else int(self.settings.retrieval_top_k),
            retrieved_chunk_ids=list(result.retrieval.chunk_ids),
            retrieval_queries=list(result.retrieval.queries),
            validation_report=_validation_payload(result),
            raw_response=result.raw_response or None,
            prompt_snapshot=result.prompt_snapshot or None,
            error=result.error or None,
            evaluation_run_id=evaluation_run_id,
        )
        self.session.add(row)
        self.session.flush()

        for check in result.validation.citations:
            # A chunk_id that does not resolve to a real chunk is stored as NULL. That
            # NULL *is* the fabrication signal downstream, and it is also the only way
            # to persist the row at all: the column is a foreign key.
            resolved = result.retrieval.by_id(check.chunk_id) if check.chunk_id is not None else None
            self.session.add(
                AssessmentCitation(
                    assessment_id=row.id,
                    chunk_id=resolved.chunk_id if resolved is not None else None,
                    evidence_file_id=check.evidence_file_id if resolved is not None else None,
                    order_index=check.order_index,
                    filename=check.filename[:512],
                    locator_text=check.locator_text[:512],
                    quoted_text=check.quoted_text,
                    relevance=check.relevance,
                    supports=check.supports[:64],
                    verdict=check.verdict.value,
                    match_score=float(check.match_score),
                    verification_note=check.note,
                )
            )
        self.session.commit()
        result.assessment_id = row.id

        audit_service.log_activity(
            self.session,
            entity_type="assessment",
            entity_id=row.id,
            action=ActivityAction.ASSESSMENT_RUN,
            actor="{0} ({1})".format(self.llm.name, self.llm.model or "default"),
            actor_type="AI",
            project_id=int(project_id),
            details={
                "control_ref": control.control_id,
                "mode": result.mode.value,
                "status": output.status.value,
                "risk_level": result.risk.level.value,
                "citations": result.validation.total,
                "verified_citations": result.validation.verified,
                "fabricated_citations": result.validation.fabricated,
                "rails_enforced": result.rails_enforced,
                "latency_ms": result.latency_ms,
                "llm_calls": result.llm_calls,
                "error": result.error,
            },
        )
        return row

    # ------------------------------------------------------------- helpers
    def _telemetry(
        self,
        result: AssessmentRunResult,
        raw_evidence: Optional[_RawEvidence],
        model_pass: _ModelPass,
        failed: bool,
    ) -> Dict[str, Any]:
        """The research record that has no dedicated column of its own."""
        meta = prompt_metadata()
        payload: Dict[str, Any] = {
            "engine_version": ENGINE_VERSION,
            "mode": result.mode.value,
            "mode_label": result.mode.label,
            "prompt_version": PROMPT_VERSION,
            "prompt_fingerprint": meta.get("fingerprint", ""),
            "llm_provider": self.llm.name,
            "llm_model": str(self.llm.model or ""),
            "retrieval_strategy": result.retrieval.strategy,
            "latency": {
                "total_ms": result.latency_ms,
                "llm_ms": result.llm_ms,
                "retrieval_ms": result.retrieval_ms,
                "orchestration_ms": max(0, result.latency_ms - result.llm_ms - result.retrieval_ms),
                "definition": (
                    "total_ms is wall clock around the whole assessment; llm_ms is the sum of the "
                    "wall clock around each provider call; retrieval_ms is the evidence-gathering "
                    "phase only - which in mode A is reading and concatenating stored chunks, not "
                    "retrieval, because mode A performs none. LLM time and evidence time are kept "
                    "apart so a workflow's inference cost is never inflated by its retrieval cost."
                ),
            },
            "tokens": {
                "prompt": result.prompt_tokens,
                "completion": result.completion_tokens,
                "source": "provider-reported, summed across steps",
            },
            "steps": [step.to_dict() for step in result.steps],
            "rails_enforced": result.rails_enforced,
            "completed": not failed,
            "evidence_chunks_supplied": len(result.retrieval.chunks),
        }
        if not result.retrieval.chunks:
            payload["no_evidence_note"] = (
                "The model was given no evidence chunks at all. Any status other than "
                "INSUFFICIENT_EVIDENCE is unsupported by construction here, whether or not the mode "
                "in use corrects it."
            )
        if not result.rails_enforced:
            payload["rails_note"] = (
                "No safety rail was applied because the run did not complete; there is no model "
                "conclusion to correct."
                if failed
                else "No safety rail was applied. Modes A and B are unrailed by design: correcting "
                "the baseline would erase the difference this experiment exists to measure. The "
                "citation validator still ran, and its verdicts are recorded above."
            )
        if raw_evidence is not None:
            payload["raw_evidence"] = raw_evidence.to_dict()
            payload["raw_evidence_note"] = (
                "Experiment A performed no retrieval. The chunk ids recorded against this assessment "
                "are the raw file text pasted into the prompt, kept so citations stay checkable."
            )
        for key in ("sufficiency", "critique", "sufficiency_error", "critique_error", "assessment_prompt_augmented"):
            if key in model_pass.detail:
                payload[key] = model_pass.detail[key]
        if model_pass.exception_ratio is not None:
            payload["exception_ratio"] = round(model_pass.exception_ratio, 4)
            payload["exception_ratio_source"] = (
                "Population telemetry reported by the provider. Only the offline mock reports it "
                "today, so risk scores that used it are not directly comparable with runs against a "
                "provider that does not."
            )
        if result.error:
            payload["error"] = result.error
        return payload

    def _failure_output(
        self,
        control: Optional[Control],
        error: str,
        control_ref: str = "",
    ) -> AssessmentOutput:
        """The row a failed run leaves behind: visible, honest and non-committal."""
        ref = control_ref or (control.control_id if control is not None else "")
        message = error or "The assessment step did not return a usable answer."
        return AssessmentOutput(
            control_id=ref,
            control_requirement=(control.objective if control is not None else "") or "",
            # The technical error is deliberately kept out of the narrative fields. Error
            # text carries incidental numbers ("first 400 chars", an HTTP status), and
            # detect_unsupported_claims scans exactly these fields - a failure message
            # would otherwise be reported as an unsupported numeric claim by the system
            # that wrote it. The message lives in `error` and in the verification list.
            assessment=(
                "No assessment was produced. The automated step failed before a conclusion could be "
                "formed; the technical error is recorded against this assessment."
            ),
            status=AssessmentStatus.INSUFFICIENT_EVIDENCE,
            finding="No finding was produced; this control has not been assessed.",
            risk=(control.risk_addressed if control is not None else "") or "",
            risk_level=RiskLevel.NOT_RATED,
            evidence=[],
            evidence_sufficiency=EvidenceSufficiency.NONE,
            missing_evidence=[],
            reasoning="",
            inferences=[],
            human_verification_required=[
                "This control was NOT assessed - the automated run failed. Assess it manually or "
                "re-run once the underlying error is resolved.",
                "Technical error recorded: {0}".format(message),
            ],
            recommendation="Re-run the assessment, or perform this test manually.",
            confidence=ConfidenceLevel.LOW,
            human_review_required=True,
            limitations=(
                "This record exists only to make a failed run visible. It states nothing about the "
                "control and must not be counted as evidence of any outcome."
            ),
        )


# ---- module-level helpers


def _control_payload(control: Any) -> Optional[Dict[str, Any]]:
    """The control record handed to the provider through :class:`LLMCallContext`.

    Mirrors what the prompt already contains. Providers accept a dict or an object, but
    a dict makes the information set explicit and reviewable, which matters when the
    point of the experiment is *which* information each mode was given.
    """
    if control is None:
        return None
    if isinstance(control, dict):
        return dict(control)
    return {
        "control_id": getattr(control, "control_id", ""),
        "name": getattr(control, "name", ""),
        "objective": getattr(control, "objective", ""),
        "description": getattr(control, "description", ""),
        "risk_addressed": getattr(control, "risk_addressed", ""),
        "category": getattr(control, "category", ""),
        "control_type": getattr(control, "control_type", ""),
        "control_frequency": getattr(control, "control_frequency", ""),
        "expected_evidence": list(getattr(control, "expected_evidence", None) or []),
        "assessment_criteria": list(getattr(control, "assessment_criteria", None) or []),
        "retrieval_keywords": list(getattr(control, "retrieval_keywords", None) or []),
        "framework_refs": list(getattr(control, "framework_refs", None) or []),
        "inherent_risk": getattr(control, "inherent_risk", ""),
    }


def _baseline_control_payload(control: Any) -> Dict[str, Any]:
    """Experiment A's reduced control record: exactly what ``build_raw_prompt`` shows.

    The baseline prompt names the control and gives a one-line objective, and nothing
    else. Passing the full library record through the call context would hand the
    baseline the expected evidence and the assessment criteria that its prompt
    deliberately omits - the comparison would then be between two differently informed
    conditions rather than between two levels of scaffolding.
    """
    objective = getattr(control, "objective", "") or getattr(control, "description", "")
    return {
        "control_id": getattr(control, "control_id", ""),
        "name": getattr(control, "name", ""),
        "objective": objective,
    }


def _require_verification(output: AssessmentOutput, item: str) -> None:
    """Add a human-verification instruction without duplicating an existing one."""
    if item not in output.human_verification_required:
        output.human_verification_required.append(item)


def _schema_for(model_cls: Any, title: str) -> Dict[str, Any]:
    schema = model_cls.model_json_schema()
    schema["title"] = title
    return schema


def _parse(text: str, model_cls: Any) -> Any:
    """Recover and validate one structured payload, or say precisely what went wrong."""
    try:
        payload = extract_json(text or "")
    except Exception as exc:  # noqa: BLE001 - LLMError and anything a provider wraps it in
        raise AssessmentEngineError("Model response was not parseable JSON: {0}".format(exc)) from exc
    try:
        return model_cls.model_validate(payload)
    except ValidationError as exc:
        raise AssessmentEngineError(
            "Model response did not match the {0} schema: {1}".format(model_cls.__name__, exc)
        ) from exc


def _exception_ratio(response: LLMResponse) -> Optional[float]:
    """Read population telemetry, if the provider chose to report any.

    ``exception_count`` / ``population_total`` in :attr:`LLMResponse.raw` feed the risk
    model's exception-rate input. Only the offline mock reports them today; a real
    provider returns nothing here and the risk model falls back to its status-derived
    defaults. That difference is recorded in the persisted telemetry, because risk
    scores computed with and without a real exception rate are not the same measurement.

    Both figures are required, and both must be integers, because a *rate* is only
    meaningful when its numerator and denominator count the same population. A provider
    that can state a population size but not how many of it failed reports the count as
    ``None``, and this returns ``None`` rather than dividing by an unrelated total -
    which would hand the risk model a confident "0% exceptions" for a population nobody
    measured. ``bool`` is excluded explicitly: it is an ``int`` subclass in Python, and
    a flag arriving in either field would otherwise be read as a count.
    """
    raw = response.raw if isinstance(response.raw, dict) else {}
    total = raw.get("population_total")
    exceptions = raw.get("exception_count")
    if isinstance(total, bool) or isinstance(exceptions, bool):
        return None
    if not isinstance(total, int) or not isinstance(exceptions, int):
        return None
    if total <= 0 or exceptions < 0:
        return None
    return max(0.0, min(1.0, float(exceptions) / float(total)))


def _requested_downgrade(
    output: AssessmentOutput,
    critique: Optional[SelfCritique],
) -> Optional[AssessmentStatus]:
    """The status the self-critique asked for, if it asked for a weaker claim."""
    if critique is None:
        return None
    current = _ASSERTION_RANK.get(output.status.value, 0)
    recommended = critique.recommended_status
    if recommended is not None and _ASSERTION_RANK.get(recommended.value, 0) < current:
        return recommended
    if critique.overstated_conclusion and current > 0:
        return AssessmentStatus.INSUFFICIENT_EVIDENCE
    return None


def _validator_agrees_unsupported(report: ValidationReport) -> Tuple[bool, str]:
    """Does the mechanical check independently support withdrawing the conclusion?"""
    reasons: List[str] = []
    if report.total == 0:
        reasons.append("nothing was cited")
    elif report.verified == 0:
        reasons.append("no citation could be verified against the retrieved evidence")
    if report.fabricated:
        reasons.append("{0} citation(s) were fabricated".format(report.fabricated))
    if report.unsupported_claims:
        reasons.append("{0} figure(s) appear nowhere in the evidence".format(len(report.unsupported_claims)))
    return bool(reasons), "; ".join(reasons)


def _sufficiency_addendum(check: SufficiencyCheck) -> str:
    """Carry a negative pre-check into the assessment prompt, verbatim and labelled."""
    lines = [
        "----- RESULT OF THE EVIDENCE SUFFICIENCY PRE-CHECK (step 1 of this workflow) -----",
        "Before this assessment was requested, the same evidence was examined for one question "
        "only: is the attribute this control is tested on actually present? The conclusion was "
        "that it is NOT sufficient to support a conclusion about how the control operates.",
        "Sufficiency rating: {0}".format(check.evidence_sufficiency.value),
    ]
    if check.present_evidence:
        lines.append("Present: {0}".format("; ".join(check.present_evidence[:8])))
    if check.missing_evidence:
        lines.append("Missing: {0}".format("; ".join(check.missing_evidence[:8])))
    if check.rationale:
        lines.append("Rationale: {0}".format(check.rationale.strip()))
    lines.extend(
        [
            "Treat this as a finding of the workflow, not as a hint. Do not reason around a missing "
            "attribute, and do not substitute a related field for it. If what is needed to test the "
            "control is absent, the status is INSUFFICIENT_EVIDENCE and missing_evidence must name "
            "the specific artefact an auditor should request.",
            "----- END OF PRE-CHECK RESULT -----",
        ]
    )
    return "\n".join(lines)


def _validation_payload(result: AssessmentRunResult) -> Dict[str, Any]:
    """Validator output plus the engine record, in one JSON column.

    ``Assessment`` has no column for the prompt version, the per-step latencies or the
    workflow's intermediate outputs, and this module may not add one. They are nested
    under ``"engine"`` so the validator's own keys stay exactly where every other
    consumer expects to find them.
    """
    payload = result.validation.to_dict()
    payload["engine"] = dict(result.telemetry)
    return payload


def assess(
    session: Session,
    project_id: int,
    control_ref: Any,
    mode: Union[ExperimentMode, str] = ExperimentMode.C_RAG_WORKFLOW,
    llm: Optional[LLMProvider] = None,
    retriever: Optional[BaseRetriever] = None,
    settings: Optional[Settings] = None,
    persist: bool = True,
    evaluation_run_id: Optional[int] = None,
) -> AssessmentRunResult:
    """Assess one control with a throwaway engine - the one-liner for scripts and the UI."""
    engine = AssessmentEngine(session, llm=llm, retriever=retriever, settings=settings)
    return engine.assess_control(
        project_id,
        control_ref,
        mode=mode,
        persist=persist,
        evaluation_run_id=evaluation_run_id,
    )


__all__ = [
    "ENGINE_VERSION",
    "NO_RETRIEVAL",
    "AssessmentEngine",
    "AssessmentEngineError",
    "AssessmentRunResult",
    "StepTelemetry",
    "assess",
]
