"""The measurement instrument for the research evaluation.

Why this module exists
----------------------
Everything else in this system produces audit output. This module is the only place
that produces *numbers about* that output, and those numbers are what a dissertation
chapter will actually assert. So the priority here is definitional precision, not
convenience: every function states its exact formula, every rate states its
denominator, and every aggregate is reported next to the ``n`` it was computed from.
A metric quoted without its ``n`` is not a research result, which is why
:func:`compute_metrics` refuses to emit one - ``n`` and ``caveats`` are always present.

Three deliberate design choices are worth stating up front.

**The arithmetic is implemented here, not delegated.** scikit-learn is installed and
would compute the same classification figures in one line. It is not used, because an
examiner reading this file should be able to see the definition of precision that was
applied, rather than trust a library default they cannot see. The trade is that the
implementation must be proved right instead of assumed right: the verification script
for this module cross-checks per-class precision/recall/F1, the confusion matrix,
accuracy and Cohen's kappa against ``sklearn.metrics`` and asserts agreement to 1e-9.
sklearn is the oracle; it is not the implementation.

**Nothing here touches the database.** The functions accept ORM ``EvaluationResult``
rows *or* plain dictionaries with the same keys, and normalise both into
:class:`NormalisedResult` before any arithmetic happens. A measurement instrument that
can only be exercised through a database session is a measurement instrument that will
not be exercised, so this module imports no models and opens no session.

**An undefined rate is reported as ``None``, not as zero.** ``app.audit.service``'s
dashboard deliberately does the opposite (a rate with a zero denominator shows as 0.0)
because an operational dashboard should not show blanks. A results table must show a
blank: "0% of assessments cited evidence" and "no assessment could be scored for
citations" are different claims, and printing the second as the first would be a false
statement in a thesis. The one exception is per-class precision/recall/F1, which use
the ``zero_division=0`` convention so that macro averaging stays defined - that is
flagged in the output under ``zero_division_convention``.

What these metrics cannot establish
-----------------------------------
With the mandated synthetic datasets ``n`` is six. Every proportion below therefore has
a confidence interval wider than the differences it is being used to compare, and no
significance testing is offered here because none would be honest at that size. The
caveat machinery emits this automatically rather than leaving it to the write-up. The
grounding and fabrication figures inherit the limits of ``app.audit.validators``: they
measure whether cited text exists in the evidence, never whether it means what the
model said it means. And the ground truth is authored, not observed - it is what the
dataset generator built in, so accuracy here is accuracy against a designed answer key,
not against an audited reality.
"""

from __future__ import annotations

from collections.abc import Mapping as _MappingABC
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from app.schemas.enums import (
    DEFICIENCY_STATUSES,
    AssessmentStatus,
    ExperimentMode,
    HumanDecision,
    RiskLevel,
)

# ---- vocabulary

#: Canonical display order for status labels. Anything outside it is appended.
STATUS_ORDER: Tuple[str, ...] = tuple(status.value for status in AssessmentStatus)

#: Statuses that count as POSITIVE in the binary "deficiency detected" framing.
POSITIVE_STATUSES: Tuple[str, ...] = tuple(status.value for status in DEFICIENCY_STATUSES)

#: Label used when a row has a ground-truth status but the system produced none (an
#: engine error, a refused parse, an empty prediction). Such rows are *kept* and scored
#: as a wrong answer. Dropping them would silently score the system only on the runs it
#: managed to complete, which flatters it exactly where it failed.
NO_PREDICTION_LABEL = "(no prediction)"

#: Statuses that assert nothing about whether the control operates.
INSUFFICIENT = AssessmentStatus.INSUFFICIENT_EVIDENCE.value

# ---- thresholds that trigger an automatic caveat
#: Below this, a proportion is reported but should not be compared between modes.
MIN_SAMPLE_SIZE = 30
#: Per-class precision/recall below this support is dominated by one or two rows.
MIN_CLASS_SUPPORT = 5
#: Cohen's kappa on fewer pairs than this is not interpretable as chance-corrected
#: agreement; the chance term is estimated from the same handful of observations.
MIN_KAPPA_PAIRS = 10
#: Below this many observations the 95th percentile is effectively the maximum.
MIN_PERCENTILE_SAMPLE = 20


# --------------------------------------------------------------------- normalisation
def _get(row: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a mapping or an object, so ORM rows and dicts both work."""
    if isinstance(row, _MappingABC):
        return row.get(name, default)
    return getattr(row, name, default)


def _first(row: Any, names: Sequence[str], default: Any = None) -> Any:
    """Read the first of several accepted key spellings that is present and non-empty."""
    for name in names:
        value = _get(row, name, None)
        if value not in (None, ""):
            return value
    return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y")
    return bool(value)


def _status_label(raw: Any) -> str:
    """Map arbitrary status text onto a canonical label, preserving the unmappable.

    An unrecognised status is upper-cased and returned as-is rather than dropped or
    silently folded into INSUFFICIENT_EVIDENCE. It then appears as its own row/column
    in the confusion matrix, where a reader can see that the system emitted something
    outside the vocabulary - which is itself a finding about the pipeline.
    """
    if raw in (None, ""):
        return ""
    member = AssessmentStatus.coerce(raw, None)
    if member is not None:
        return member.value
    return str(raw).strip().upper().replace(" ", "_").replace("-", "_")


def _risk_label(raw: Any) -> str:
    if raw in (None, ""):
        return ""
    member = RiskLevel.coerce(raw, None)
    if member is not None:
        return member.value
    return str(raw).strip().upper().replace(" ", "_").replace("-", "_")


def _mode_label(raw: Any) -> str:
    if raw in (None, ""):
        return ""
    member = ExperimentMode.coerce(raw, None)
    if member is not None:
        return member.value
    return str(raw).strip()


@dataclass
class NormalisedResult:
    """One scored prediction, flattened out of whatever shape it arrived in.

    Field names mirror ``app.database.models.EvaluationResult`` columns so that an ORM
    row normalises into this without translation, and a hand-written dict in a test can
    use the same keys.
    """

    dataset_id: str = ""
    dataset_name: str = ""
    control_ref: str = ""
    mode: str = ""
    expected_status: str = ""
    predicted_status: str = ""
    expected_risk: str = ""
    predicted_risk: str = ""
    expected_evidence_hits: int = 0
    expected_evidence_total: int = 0
    citation_count: int = 0
    verified_citation_count: int = 0
    fabricated_citation_count: int = 0
    unsupported_claim_count: int = 0
    hallucination_detected: bool = False
    declared_missing_evidence: bool = False
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str = ""
    #: ``status_correct`` as the runner stored it. Never used in any metric - it is kept
    #: only so :func:`compute_metrics` can report when the stored flag disagrees with a
    #: fresh comparison of the two status strings.
    stored_status_correct: Optional[bool] = None

    @property
    def scored(self) -> bool:
        """True when a ground-truth status exists, i.e. the row can be scored at all."""
        return bool(self.expected_status)

    @property
    def status_correct(self) -> bool:
        """Recomputed from the two labels; the stored flag is deliberately ignored."""
        return bool(self.expected_status) and self.expected_status == self.predicted_status

    @property
    def has_error(self) -> bool:
        return bool(self.error)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "dataset_name": self.dataset_name,
            "control_ref": self.control_ref,
            "mode": self.mode,
            "expected_status": self.expected_status,
            "predicted_status": self.predicted_status,
            "status_correct": self.status_correct,
            "expected_risk": self.expected_risk,
            "predicted_risk": self.predicted_risk,
            "expected_evidence_hits": self.expected_evidence_hits,
            "expected_evidence_total": self.expected_evidence_total,
            "citation_count": self.citation_count,
            "verified_citation_count": self.verified_citation_count,
            "fabricated_citation_count": self.fabricated_citation_count,
            "unsupported_claim_count": self.unsupported_claim_count,
            "hallucination_detected": self.hallucination_detected,
            "declared_missing_evidence": self.declared_missing_evidence,
            "latency_ms": self.latency_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "error": self.error,
        }


def normalise_result(row: Any) -> NormalisedResult:
    """Flatten one ORM ``EvaluationResult`` row, or one dict with the same keys.

    The experiment mode is not a column on ``EvaluationResult`` (it lives on the parent
    ``EvaluationRun``), so it is looked for in four places, in order: an
    ``experiment_mode`` / ``mode`` attribute or key on the row, then the same two keys
    inside ``detail``, then ``run.experiment_mode``. A row with none of these gets an
    empty mode and is grouped under "" by :func:`compare_modes`.
    """
    detail = _get(row, "detail", None) or {}
    if not isinstance(detail, _MappingABC):
        detail = {}

    mode = _first(row, ("experiment_mode", "mode"), "")
    if not mode:
        mode = detail.get("experiment_mode") or detail.get("mode") or ""
    if not mode:
        run = _get(row, "run", None)
        if run is not None:
            mode = _get(run, "experiment_mode", "") or ""

    predicted = _first(row, ("predicted_status", "status"), "")
    expected = _first(row, ("expected_status", "ground_truth_status"), "")

    stored_correct = _get(row, "status_correct", None)

    return NormalisedResult(
        dataset_id=str(_get(row, "dataset_id", "") or ""),
        dataset_name=str(_get(row, "dataset_name", "") or ""),
        control_ref=str(_get(row, "control_ref", "") or ""),
        mode=_mode_label(mode),
        expected_status=_status_label(expected),
        predicted_status=_status_label(predicted),
        expected_risk=_risk_label(_get(row, "expected_risk", "")),
        predicted_risk=_risk_label(_first(row, ("predicted_risk", "risk_level"), "")),
        expected_evidence_hits=_as_int(_get(row, "expected_evidence_hits", 0)),
        expected_evidence_total=_as_int(_get(row, "expected_evidence_total", 0)),
        citation_count=_as_int(_get(row, "citation_count", 0)),
        verified_citation_count=_as_int(_get(row, "verified_citation_count", 0)),
        fabricated_citation_count=_as_int(_get(row, "fabricated_citation_count", 0)),
        unsupported_claim_count=_as_int(_get(row, "unsupported_claim_count", 0)),
        hallucination_detected=_as_bool(_get(row, "hallucination_detected", False)),
        declared_missing_evidence=_as_bool(_get(row, "declared_missing_evidence", False)),
        latency_ms=_as_int(_get(row, "latency_ms", 0)),
        prompt_tokens=_as_int(_get(row, "prompt_tokens", 0)),
        completion_tokens=_as_int(_get(row, "completion_tokens", 0)),
        error=str(_get(row, "error", "") or ""),
        stored_status_correct=None if stored_correct is None else bool(stored_correct),
    )


def normalise_results(results: Sequence[Any]) -> List[NormalisedResult]:
    """Normalise a sequence of rows; already-normalised items pass through unchanged."""
    out: List[NormalisedResult] = []
    for row in results or []:
        out.append(row if isinstance(row, NormalisedResult) else normalise_result(row))
    return out


def results_to_frame(results: Sequence[Any]) -> pd.DataFrame:
    """One row per result, for export and for eyeballing a run before trusting a mean."""
    rows = [item.to_dict() for item in normalise_results(results)]
    if not rows:
        return pd.DataFrame(
            columns=list(NormalisedResult().to_dict().keys())  # stable empty schema
        )
    return pd.DataFrame(rows)


# ------------------------------------------------------------------------- primitives
def _ratio(numerator: float, denominator: float) -> Optional[float]:
    """``numerator / denominator``, or ``None`` when the denominator is zero.

    ``None`` rather than 0.0 throughout the aggregate metrics: a rate with no
    denominator is undefined, and rendering it as zero would assert something the data
    does not support.
    """
    if not denominator:
        return None
    return float(numerator) / float(denominator)


def _label_universe(
    expected: Sequence[str], predicted: Sequence[str], labels: Optional[Sequence[str]] = None
) -> List[str]:
    """Label set for the confusion matrix and per-class table.

    Defaults to the union of labels actually observed in either vector - the same
    convention as ``sklearn.metrics`` - so that a status no run ever produced does not
    contribute an all-zero class that would drag the macro average down. Pass ``labels``
    explicitly (e.g. ``STATUS_ORDER``) to force a fixed vocabulary when several runs
    must be tabulated against identical rows and columns.
    """
    if labels is not None:
        return list(labels)
    observed = set(expected) | set(predicted)
    observed.discard("")
    known = [label for label in STATUS_ORDER if label in observed]
    sentinel = [NO_PREDICTION_LABEL] if NO_PREDICTION_LABEL in observed else []
    extra = sorted(
        label for label in observed if label not in STATUS_ORDER and label != NO_PREDICTION_LABEL
    )
    return known + extra + sentinel


def _vectors(results: Sequence[NormalisedResult]) -> Tuple[List[str], List[str]]:
    """Ground-truth and prediction label vectors over the scorable rows.

    A scorable row is one with a ground-truth status. A missing *prediction* does not
    make a row unscorable - it becomes :data:`NO_PREDICTION_LABEL`, so a crashed run
    counts against accuracy instead of disappearing from the denominator.
    """
    expected: List[str] = []
    predicted: List[str] = []
    for item in results:
        if not item.scored:
            continue
        expected.append(item.expected_status)
        predicted.append(item.predicted_status or NO_PREDICTION_LABEL)
    return expected, predicted


# --------------------------------------------------------------------- classification
def confusion_matrix(
    results: Sequence[Any], labels: Optional[Sequence[str]] = None
) -> Dict[str, Dict[str, int]]:
    """Counts of every (expected, predicted) pair as a nested dict.

    ``matrix[expected_label][predicted_label] = count``. Rows are ground truth, columns
    are what the system said; the diagonal is correct. Every label in the universe
    appears as both a row and a column, including zero rows, so two runs scored with the
    same ``labels`` produce matrices of identical shape.
    """
    items = normalise_results(results)
    expected, predicted = _vectors(items)
    universe = _label_universe(expected, predicted, labels)
    index = {label: position for position, label in enumerate(universe)}

    counts = np.zeros((len(universe), len(universe)), dtype=int)
    for true_label, pred_label in zip(expected, predicted):
        if true_label in index and pred_label in index:
            counts[index[true_label], index[pred_label]] += 1

    return {
        true_label: {
            pred_label: int(counts[row_i, col_i]) for col_i, pred_label in enumerate(universe)
        }
        for row_i, true_label in enumerate(universe)
    }


def confusion_matrix_frame(
    results: Sequence[Any], labels: Optional[Sequence[str]] = None
) -> pd.DataFrame:
    """The same matrix as a ``DataFrame`` (index = expected, columns = predicted)."""
    matrix = confusion_matrix(results, labels)
    universe = list(matrix.keys())
    frame = pd.DataFrame(
        [[matrix[row][col] for col in universe] for row in universe],
        index=pd.Index(universe, name="expected"),
        columns=pd.Index(universe, name="predicted"),
        dtype=int,
    )
    return frame


def classification_metrics(
    results: Sequence[Any], labels: Optional[Sequence[str]] = None
) -> Dict[str, Any]:
    """Per-class and averaged multi-class metrics over :class:`AssessmentStatus`.

    For each class ``c``, with TP = predicted ``c`` and truly ``c``, FP = predicted
    ``c`` but truly something else, FN = truly ``c`` but predicted something else:

    * ``precision[c] = TP / (TP + FP)``  - of the times the system said ``c``, how often
      it was right. Denominator is the number of *predictions* of ``c``.
    * ``recall[c]    = TP / (TP + FN)``  - of the cases that really were ``c``, how many
      the system found. Denominator is ``support[c]``.
    * ``f1[c]        = 2 * precision * recall / (precision + recall)`` - harmonic mean.
    * ``support[c]   = TP + FN``         - ground-truth count of ``c``.

    A zero denominator yields 0.0 (the ``zero_division=0`` convention), so that macro
    averaging remains defined when a class was never predicted. This is the one place in
    the module where an undefined ratio is reported as zero, and the output says so.

    Averages:

    * ``macro_*``    - the unweighted mean over classes: every status counts equally,
      so the four-instance INSUFFICIENT_EVIDENCE class matters as much as a common one.
      This is the headline average for this research, because the rare classes are
      precisely the ones the system is being tested on.
    * ``weighted_*`` - the support-weighted mean, i.e. the mean an aggregate reader
      expects; it is dominated by whichever class the dataset happens to contain most of.
    * ``accuracy``   - ``correct / n_scored`` over all classes.
    * ``micro_*``    - pooled over classes. In single-label multi-class classification
      micro precision, micro recall, micro F1 and accuracy are all the same number; it
      is reported once, under ``accuracy``, and ``micro_note`` records the identity.
    """
    items = normalise_results(results)
    expected, predicted = _vectors(items)
    universe = _label_universe(expected, predicted, labels)
    n = len(expected)

    expected_arr = np.array(expected, dtype=object)
    predicted_arr = np.array(predicted, dtype=object)

    per_class: Dict[str, Dict[str, float]] = {}
    precisions = np.zeros(len(universe), dtype=float)
    recalls = np.zeros(len(universe), dtype=float)
    f1s = np.zeros(len(universe), dtype=float)
    supports = np.zeros(len(universe), dtype=float)

    for position, label in enumerate(universe):
        if n:
            true_mask = expected_arr == label
            pred_mask = predicted_arr == label
            tp = float(np.sum(true_mask & pred_mask))
            fp = float(np.sum(~true_mask & pred_mask))
            fn = float(np.sum(true_mask & ~pred_mask))
        else:
            tp = fp = fn = 0.0
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (2.0 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
        support = tp + fn

        precisions[position] = precision
        recalls[position] = recall
        f1s[position] = f1
        supports[position] = support
        per_class[label] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": int(support),
            "true_positives": int(tp),
            "false_positives": int(fp),
            "false_negatives": int(fn),
        }

    correct = int(np.sum(expected_arr == predicted_arr)) if n else 0
    total_support = float(np.sum(supports))

    def _weighted(values: np.ndarray) -> Optional[float]:
        if not total_support:
            return None
        return float(np.sum(values * supports) / total_support)

    return {
        "n_scored": n,
        "labels": universe,
        "correct": correct,
        "accuracy": _ratio(correct, n),
        "per_class": per_class,
        "macro_precision": float(np.mean(precisions)) if len(universe) else None,
        "macro_recall": float(np.mean(recalls)) if len(universe) else None,
        "macro_f1": float(np.mean(f1s)) if len(universe) else None,
        "weighted_precision": _weighted(precisions),
        "weighted_recall": _weighted(recalls),
        "weighted_f1": _weighted(f1s),
        "zero_division_convention": (
            "Per-class precision/recall/F1 with a zero denominator are reported as 0.0 so "
            "that macro and weighted averages stay defined."
        ),
        "micro_note": (
            "For single-label multi-class classification micro-precision = micro-recall = "
            "micro-F1 = accuracy; the value is reported once as 'accuracy'."
        ),
        "macro_note": (
            "Macro averages are unweighted means over the labels in 'labels', which by "
            "default is the set of labels observed in the ground truth or the predictions."
        ),
    }


def per_class_frame(
    results: Sequence[Any], labels: Optional[Sequence[str]] = None
) -> pd.DataFrame:
    """Per-class precision/recall/F1/support as a display table, one row per class."""
    metrics = classification_metrics(results, labels)
    rows = []
    for label in metrics["labels"]:
        cell = metrics["per_class"][label]
        rows.append(
            {
                "status": label,
                "precision": cell["precision"],
                "recall": cell["recall"],
                "f1": cell["f1"],
                "support": cell["support"],
            }
        )
    frame = pd.DataFrame(
        rows, columns=["status", "precision", "recall", "f1", "support"]
    )
    return frame.set_index("status") if not frame.empty else frame


# ----------------------------------------------------------- binary deficiency framing
def deficiency_detection_metrics(
    results: Sequence[Any], insufficient_evidence: str = "negative"
) -> Dict[str, Any]:
    """The binary "did the system flag a deficiency?" view of the same predictions.

    **Framing, stated once and applied everywhere.** POSITIVE means *the system
    concluded a deficiency exists*, i.e. the status is POTENTIAL_DEFICIENCY or
    NOT_EFFECTIVE (:data:`POSITIVE_STATUSES`). NEGATIVE is every other status. The same
    rule is applied to the ground-truth status to obtain the true class. Then, over the
    rows that have a ground truth:

    ====  =========================================================
    TP    truly deficient, system said deficient
    FP    truly not deficient, system said deficient
    TN    truly not deficient, system said not deficient
    FN    truly deficient, system said not deficient
    ====  =========================================================

    * ``precision   = TP / (TP + FP)``  - of the deficiencies raised, how many were real
    * ``recall      = TP / (TP + FN)``  - of the real deficiencies, how many were raised
      (sensitivity / true positive rate)
    * ``specificity = TN / (TN + FP)``  - of the healthy controls, how many were left alone
    * ``fpr         = FP / (FP + TN)``  - false positive rate = 1 - specificity; the rate
      at which the system would send an auditor after a control that was fine
    * ``fnr         = FN / (FN + TP)``  - false negative rate = 1 - recall; the rate at
      which a real deficiency is missed. For an audit tool this is the dangerous error.
    * ``f1``, ``accuracy``, and ``balanced_accuracy = (recall + specificity) / 2``.
      ``f1`` is ``None`` whenever precision or recall is itself undefined - a system that
      never raised a single deficiency has no precision, and reporting its F1 as 0.0
      would read as a measured failure rather than an absent measurement.

    **INSUFFICIENT_EVIDENCE counts as NEGATIVE** under the default framing
    (``insufficient_evidence="negative"``). This is a deliberate and conservative
    choice, and it is not neutral: when the ground truth is a real deficiency and the
    system honestly answers "the evidence does not establish this", that row is scored
    as a false negative, so **recall is depressed by exactly the behaviour the system is
    designed to exhibit**. The choice is made anyway, because from the point of view of
    an audit programme an unraised deficiency is unraised regardless of how politely the
    tool declined, and a framing that let abstention off the hook could report high
    recall for a system that never concludes anything.

    Pass ``insufficient_evidence="excluded"`` for the second framing, in which
    INSUFFICIENT_EVIDENCE is treated as an *abstention* rather than a verdict: any row
    whose ground truth or prediction is INSUFFICIENT_EVIDENCE is removed from the table,
    the remaining rows are scored as above, and ``coverage`` reports the share of rows
    that survived. Report both. Neither alone is the honest number: the first understates
    the system's discrimination, the second overstates its usefulness by scoring it only
    where it chose to answer.
    """
    mode = str(insufficient_evidence or "negative").strip().lower()
    if mode not in ("negative", "excluded"):
        raise ValueError(
            "insufficient_evidence must be 'negative' or 'excluded', got {0!r}".format(
                insufficient_evidence
            )
        )

    items = [item for item in normalise_results(results) if item.scored]
    considered = len(items)

    if mode == "excluded":
        kept = [
            item
            for item in items
            if item.expected_status != INSUFFICIENT and item.predicted_status != INSUFFICIENT
        ]
    else:
        kept = items

    positives = set(POSITIVE_STATUSES)
    tp = fp = tn = fn = 0
    for item in kept:
        truth = item.expected_status in positives
        # A missing prediction is a NEGATIVE: no deficiency was raised.
        guess = item.predicted_status in positives
        if truth and guess:
            tp += 1
        elif truth and not guess:
            fn += 1
        elif not truth and guess:
            fp += 1
        else:
            tn += 1

    n = len(kept)
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    specificity = _ratio(tn, tn + fp)
    fpr = _ratio(fp, fp + tn)
    fnr = _ratio(fn, fn + tp)
    if precision is None or recall is None or (precision + recall) == 0:
        f1: Optional[float] = None if (precision is None or recall is None) else 0.0
    else:
        f1 = 2.0 * precision * recall / (precision + recall)
    balanced = (
        (recall + specificity) / 2.0 if (recall is not None and specificity is not None) else None
    )

    return {
        "framing": mode,
        "positive_statuses": list(POSITIVE_STATUSES),
        "n": n,
        "n_considered": considered,
        "coverage": _ratio(n, considered),
        "true_positives": tp,
        "false_positives": fp,
        "true_negatives": tn,
        "false_negatives": fn,
        "precision": precision,
        "recall": recall,
        "specificity": specificity,
        "fpr": fpr,
        "fnr": fnr,
        "f1": f1,
        "accuracy": _ratio(tp + tn, n),
        "balanced_accuracy": balanced,
        "definition": (
            "POSITIVE = the system concluded a deficiency exists (POTENTIAL_DEFICIENCY or "
            "NOT_EFFECTIVE). NEGATIVE = every other status, including INSUFFICIENT_EVIDENCE "
            "and a missing prediction."
            if mode == "negative"
            else (
                "POSITIVE = POTENTIAL_DEFICIENCY or NOT_EFFECTIVE. Rows whose ground truth or "
                "prediction is INSUFFICIENT_EVIDENCE are excluded as abstentions; 'coverage' is "
                "the share of scorable rows that survived that exclusion."
            )
        ),
    }


# ------------------------------------------------------------ evidence / traceability
def evidence_metrics(results: Sequence[Any]) -> Dict[str, Any]:
    """Traceability and hallucination rates, each with its denominator named.

    Assessment-level rates (denominator = number of results supplied, ``n``):

    * ``citation_rate``            - share with at least one citation
    * ``fabricated_assessment_rate`` - share with at least one FABRICATED citation
    * ``unsupported_claim_rate``   - share with at least one unsupported numeric claim
    * ``hallucination_rate``       - share flagged by the runner as containing a
      hallucination of either kind (``hallucination_detected``)
    * ``clean_rate``               - share with at least one citation, no fabricated
      citation and no unsupported claim

    Citation-level rates (denominator = total citations across all results):

    * ``grounding_rate``           - ``sum(verified) / sum(citations)``. **Strict**: only
      VERIFIED counts, PARTIAL counts as ungrounded. This is deliberately *not* the same
      figure as ``ValidationReport.grounding_rate``, which credits a PARTIAL match at
      0.5; the per-assessment figure is a working number for the UI, this one is the
      conservative floor a thesis should quote. Both are computable from the same rows.
    * ``fabrication_rate``         - ``sum(fabricated) / sum(citations)``
    * ``mean_citations_per_assessment`` - ``sum(citations) / n``

    Retrieval:

    * ``retrieval_recall`` - ``sum(expected_evidence_hits) / sum(expected_evidence_total)``
      pooled over datasets: the share of the datasets' ``key_evidence_markers`` that
      retrieval actually surfaced. Pooled (micro) rather than averaged per dataset, so a
      dataset with ten markers weighs ten times a dataset with one; the per-dataset mean
      is also given as ``retrieval_recall_macro`` because the two answer different
      questions ("what share of the evidence was found" vs "how well did retrieval do on
      a typical dataset").

    Missing-evidence detection:

    * ``missing_evidence_detection_rate`` - restricted to rows whose *ground truth* is
      INSUFFICIENT_EVIDENCE: the share where the system both concluded
      INSUFFICIENT_EVIDENCE **and** named at least one missing artefact
      (``declared_missing_evidence``). Naming the gap is part of the requirement: a bare
      "insufficient evidence" tells an auditor nothing about what to go and obtain, so a
      row that concludes correctly without naming anything does not count. ``None`` when
      the dataset contains no such ground-truth case, and ``n`` is reported alongside.
    """
    items = normalise_results(results)
    n = len(items)

    citations_total = sum(item.citation_count for item in items)
    verified_total = sum(item.verified_citation_count for item in items)
    fabricated_total = sum(item.fabricated_citation_count for item in items)
    unsupported_total = sum(item.unsupported_claim_count for item in items)

    with_citation = sum(1 for item in items if item.citation_count > 0)
    with_fabricated = sum(1 for item in items if item.fabricated_citation_count > 0)
    with_unsupported = sum(1 for item in items if item.unsupported_claim_count > 0)
    with_hallucination = sum(1 for item in items if item.hallucination_detected)
    clean = sum(
        1
        for item in items
        if item.citation_count > 0
        and item.fabricated_citation_count == 0
        and item.unsupported_claim_count == 0
    )

    markers_hit = sum(item.expected_evidence_hits for item in items)
    markers_total = sum(item.expected_evidence_total for item in items)
    per_dataset = [
        _ratio(item.expected_evidence_hits, item.expected_evidence_total)
        for item in items
        if item.expected_evidence_total > 0
    ]
    macro_recall = float(np.mean(per_dataset)) if per_dataset else None

    missing_cases = [item for item in items if item.expected_status == INSUFFICIENT]
    detected = sum(
        1
        for item in missing_cases
        if item.predicted_status == INSUFFICIENT and item.declared_missing_evidence
    )
    concluded_only = sum(1 for item in missing_cases if item.predicted_status == INSUFFICIENT)

    return {
        "n": n,
        "citations_total": citations_total,
        "citations_verified": verified_total,
        "citations_fabricated": fabricated_total,
        "unsupported_claims_total": unsupported_total,
        "assessments_with_citation": with_citation,
        "citation_rate": _ratio(with_citation, n),
        "grounding_rate": _ratio(verified_total, citations_total),
        "fabrication_rate": _ratio(fabricated_total, citations_total),
        "fabricated_assessment_rate": _ratio(with_fabricated, n),
        "unsupported_claim_rate": _ratio(with_unsupported, n),
        "mean_unsupported_claims": _ratio(unsupported_total, n),
        "hallucination_rate": _ratio(with_hallucination, n),
        "clean_rate": _ratio(clean, n),
        "mean_citations_per_assessment": _ratio(citations_total, n),
        "retrieval_recall": _ratio(markers_hit, markers_total),
        "retrieval_recall_macro": macro_recall,
        "retrieval_markers_hit": markers_hit,
        "retrieval_markers_total": markers_total,
        "retrieval_datasets_scored": len(per_dataset),
        "missing_evidence_cases": len(missing_cases),
        "missing_evidence_concluded": concluded_only,
        "missing_evidence_detected": detected,
        "missing_evidence_detection_rate": _ratio(detected, len(missing_cases)),
        "definitions": {
            "grounding_rate": "sum(verified citations) / sum(all citations); PARTIAL counts as 0",
            "fabrication_rate": "sum(fabricated citations) / sum(all citations)",
            "citation_rate": "assessments with >= 1 citation / all assessments",
            "unsupported_claim_rate": "assessments with >= 1 unsupported numeric claim / all assessments",
            "retrieval_recall": "sum(key evidence markers surfaced) / sum(key evidence markers expected)",
            "missing_evidence_detection_rate": (
                "among ground-truth INSUFFICIENT_EVIDENCE cases: concluded INSUFFICIENT_EVIDENCE "
                "AND named a missing artefact / all such cases"
            ),
        },
    }


# --------------------------------------------------------------- human/AI concordance
@dataclass
class NormalisedReview:
    """One AI-conclusion / human-conclusion pair, flattened for agreement statistics."""

    ai_status: str = ""
    human_status: str = ""
    ai_risk: str = ""
    human_risk: str = ""
    decision: str = ""

    @property
    def is_pending(self) -> bool:
        return self.decision == HumanDecision.PENDING.value

    @property
    def status_comparable(self) -> bool:
        return bool(self.ai_status and self.human_status)

    @property
    def risk_comparable(self) -> bool:
        return bool(self.ai_risk and self.human_risk)


def normalise_review(row: Any) -> NormalisedReview:
    """Flatten a ``HumanReview`` ORM row, a dict, or a bare ``(ai, human)`` pair.

    For an ORM row the AI side is read from the related ``assessment`` (``status`` and
    ``risk_level``) and the human side from ``final_status`` / ``final_risk_level``.
    Dicts may use either spelling: ``ai_status`` / ``assessment_status`` and
    ``human_status`` / ``final_status``.
    """
    if isinstance(row, NormalisedReview):
        return row
    if isinstance(row, (tuple, list)) and len(row) == 2:
        return NormalisedReview(
            ai_status=_status_label(row[0]),
            human_status=_status_label(row[1]),
            decision=HumanDecision.ACCEPTED.value,
        )

    assessment = _get(row, "assessment", None)
    ai_status = _first(row, ("ai_status", "assessment_status", "predicted_status"), "")
    ai_risk = _first(row, ("ai_risk", "assessment_risk_level", "predicted_risk"), "")
    if not ai_status and assessment is not None:
        ai_status = _get(assessment, "status", "")
    if not ai_risk and assessment is not None:
        ai_risk = _get(assessment, "risk_level", "")

    return NormalisedReview(
        ai_status=_status_label(ai_status),
        human_status=_status_label(_first(row, ("human_status", "final_status"), "")),
        ai_risk=_risk_label(ai_risk),
        human_risk=_risk_label(_first(row, ("human_risk", "final_risk_level"), "")),
        decision=str(_first(row, ("decision",), "") or ""),
    )


def cohens_kappa(
    rater_a: Sequence[str], rater_b: Sequence[str], labels: Optional[Sequence[str]] = None
) -> Optional[float]:
    """Cohen's kappa: chance-corrected agreement between two raters on nominal labels.

    ``kappa = (po - pe) / (1 - pe)`` where

    * ``po`` = observed agreement = (pairs where the two raters gave the same label) / n
    * ``pe`` = agreement expected by chance if the two raters assigned labels
      independently with their own observed marginal frequencies
      = ``sum over labels c of  (count_a(c) / n) * (count_b(c) / n)``

    Interpretation: 1.0 is perfect agreement, 0.0 is agreement no better than chance,
    negative values mean worse than chance.

    **The degenerate case is not fudged.** If both raters used exactly one label and it
    was the same label for every pair, then ``pe = 1``, the denominator is zero, and
    kappa is genuinely undefined - agreement cannot be corrected for chance when chance
    already predicts everything. This returns ``None`` in that case, rather than 1.0
    (which would claim perfect chance-corrected agreement from data containing no
    information about disagreement) or 0.0 (which would claim the raters did no better
    than chance when in fact they never disagreed). ``None`` is also returned for an
    empty input. Callers must render ``None`` as "undefined", never as a number.
    """
    if len(rater_a) != len(rater_b):
        raise ValueError("cohens_kappa requires two sequences of equal length")
    n = len(rater_a)
    if n == 0:
        return None

    a = np.array(list(rater_a), dtype=object)
    b = np.array(list(rater_b), dtype=object)
    universe = list(labels) if labels is not None else _label_universe(list(a), list(b))
    if not universe:
        return None

    po = float(np.sum(a == b)) / float(n)
    pe = 0.0
    for label in universe:
        pe += (float(np.sum(a == label)) / n) * (float(np.sum(b == label)) / n)

    denominator = 1.0 - pe
    if abs(denominator) < 1e-12:
        return None
    return (po - pe) / denominator


def agreement_metrics(
    reviews: Sequence[Any], include_pending: bool = False
) -> Dict[str, Any]:
    """Human/AI concordance over reviewed assessments.

    Pairs are (what the AI concluded, what the auditor finally concluded). A PENDING
    review is excluded by default: someone opened the review screen, which is not a
    human judgement, and counting it either way would corrupt the statistic. Pass
    ``include_pending=True`` to override, and check ``n_pending_excluded`` to see how
    many rows that decision removed.

    * ``status_agreement`` = pairs where ``final_status == ai_status`` / comparable pairs
    * ``risk_agreement``   = pairs where ``final_risk_level == ai_risk_level`` / comparable
      pairs. Reported separately from status because they are different judgements: an
      auditor routinely keeps the AI's conclusion and re-rates its severity, and a single
      blended agreement number would hide exactly that.
    * ``status_kappa`` / ``risk_kappa`` = :func:`cohens_kappa` on the same pairs.
      Raw agreement alone is inflated whenever one label dominates - with five statuses
      and a dataset that is mostly POTENTIAL_DEFICIENCY, two raters who both always
      guessed the majority label would score high raw agreement and zero kappa.
    * ``modification_rate`` = reviews whose *decision* is MODIFIED / completed reviews.
      This is the process measure. The outcome measure is ``status_change_rate``
      (= 1 - ``status_agreement``): an auditor can record MODIFIED while keeping the
      status, or ACCEPT while the status was defaulted, so the two are not complements.
    """
    pairs = [normalise_review(row) for row in (reviews or [])]
    pending = [pair for pair in pairs if pair.is_pending]
    considered = pairs if include_pending else [pair for pair in pairs if not pair.is_pending]

    status_pairs = [pair for pair in considered if pair.status_comparable]
    risk_pairs = [pair for pair in considered if pair.risk_comparable]

    status_matches = sum(1 for pair in status_pairs if pair.ai_status == pair.human_status)
    risk_matches = sum(1 for pair in risk_pairs if pair.ai_risk == pair.human_risk)

    status_kappa = cohens_kappa(
        [pair.ai_status for pair in status_pairs], [pair.human_status for pair in status_pairs]
    )
    risk_kappa = cohens_kappa(
        [pair.ai_risk for pair in risk_pairs], [pair.human_risk for pair in risk_pairs]
    )

    decisions: Dict[str, int] = {}
    for pair in considered:
        key = pair.decision or "(unspecified)"
        decisions[key] = decisions.get(key, 0) + 1

    completed = len(considered)
    modified = decisions.get(HumanDecision.MODIFIED.value, 0)
    status_agreement = _ratio(status_matches, len(status_pairs))

    return {
        "n_reviews": len(pairs),
        "n_completed": completed,
        "n_pending_excluded": 0 if include_pending else len(pending),
        "n_status_pairs": len(status_pairs),
        "n_risk_pairs": len(risk_pairs),
        "status_agreement": status_agreement,
        "status_kappa": status_kappa,
        "risk_agreement": _ratio(risk_matches, len(risk_pairs)),
        "risk_kappa": risk_kappa,
        "modification_rate": _ratio(modified, completed),
        "acceptance_rate": _ratio(decisions.get(HumanDecision.ACCEPTED.value, 0), completed),
        "rejection_rate": _ratio(decisions.get(HumanDecision.REJECTED.value, 0), completed),
        "more_evidence_requested_rate": _ratio(
            decisions.get(HumanDecision.MORE_EVIDENCE_REQUESTED.value, 0), completed
        ),
        "status_change_rate": None if status_agreement is None else 1.0 - status_agreement,
        "decision_counts": decisions,
        "definitions": {
            "status_agreement": "reviews where final_status == AI status / comparable reviews",
            "status_kappa": "(po - pe) / (1 - pe); None when pe == 1 (single-category degenerate case)",
            "modification_rate": "reviews with decision MODIFIED / completed reviews",
            "pending": "PENDING reviews are excluded from every figure above by default",
        },
    }


# ------------------------------------------------------------------------------ timing
def _distribution(values: Sequence[float]) -> Dict[str, Optional[float]]:
    """Mean / median / p95 / min / max, or ``None`` everywhere for an empty sample.

    ``p95`` is ``numpy.percentile(values, 95)`` with linear interpolation between the
    two neighbouring order statistics - not the nearest-rank definition. The two differ
    materially at small ``n``, and at fewer than :data:`MIN_PERCENTILE_SAMPLE`
    observations a 95th percentile is close to the maximum regardless of definition,
    which :func:`compute_metrics` raises as a caveat.
    """
    if not values:
        return {"mean": None, "median": None, "p95": None, "min": None, "max": None, "total": None}
    arr = np.asarray(list(values), dtype=float)
    return {
        "mean": float(np.mean(arr)),
        "median": float(np.median(arr)),
        "p95": float(np.percentile(arr, 95)),
        "min": float(np.min(arr)),
        "max": float(np.max(arr)),
        "total": float(np.sum(arr)),
    }


def timing_metrics(results: Sequence[Any], include_by_mode: bool = True) -> Dict[str, Any]:
    """Latency and token cost per assessment.

    ``latency_ms`` is wall-clock time for one control assessment as recorded by the
    engine, which for mode C covers three LLM calls plus retrieval plus validation, and
    for mode A covers one call with no retrieval. Comparing modes on latency therefore
    compares *pipelines*, not model speed, and against the offline mock provider it
    compares Python execution, not inference - a mock-based latency figure says nothing
    about how long a real model would take and must never be presented as if it did.

    Rows with ``latency_ms <= 0`` are excluded from the latency distribution (the field
    was never populated) but still counted in ``n``; ``n_timed`` is the real denominator.
    Token counts are summed over all rows including zeros, because a zero-token row from
    the mock provider is a genuine measurement of that provider.
    """
    items = normalise_results(results)
    latencies = [float(item.latency_ms) for item in items if item.latency_ms > 0]

    block: Dict[str, Any] = {
        "n": len(items),
        "n_timed": len(latencies),
        "latency_ms": _distribution(latencies),
        "prompt_tokens": _distribution([float(item.prompt_tokens) for item in items]),
        "completion_tokens": _distribution([float(item.completion_tokens) for item in items]),
        "total_tokens": _distribution([float(item.total_tokens) for item in items]),
        "definitions": {
            "latency_ms": "wall-clock milliseconds for one control assessment, end to end",
            "p95": "numpy.percentile(values, 95), linear interpolation",
            "excluded": "rows with latency_ms <= 0 are excluded from the latency distribution",
        },
    }

    if include_by_mode:
        grouped = group_by_mode(items)
        if len(grouped) > 1:
            block["by_mode"] = {
                mode: timing_metrics(rows, include_by_mode=False) for mode, rows in grouped.items()
            }
    return block


# ------------------------------------------------------------------- mode comparison
def group_by_mode(results: Sequence[Any]) -> Dict[str, List[NormalisedResult]]:
    """Group results by experiment mode, ordered A, B, C then anything unrecognised."""
    items = normalise_results(results)
    grouped: Dict[str, List[NormalisedResult]] = {}
    for item in items:
        grouped.setdefault(item.mode, []).append(item)

    def sort_key(mode: str) -> Tuple[int, str]:
        member = ExperimentMode.coerce(mode, None)
        if member is not None:
            return (list(ExperimentMode).index(member), mode)
        return (len(ExperimentMode) + 1, mode)

    return {mode: grouped[mode] for mode in sorted(grouped, key=sort_key)}


#: Columns of the A/B/C comparison table, in reading order: how often it was right,
#: then how it errs, then whether its answers are traceable, then what it cost.
_COMPARISON_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("n", "classification.n_scored"),
    ("accuracy", "classification.accuracy"),
    ("macro_f1", "classification.macro_f1"),
    ("weighted_f1", "classification.weighted_f1"),
    ("deficiency_precision", "deficiency_detection.precision"),
    ("deficiency_recall", "deficiency_detection.recall"),
    ("deficiency_fpr", "deficiency_detection.fpr"),
    ("deficiency_fnr", "deficiency_detection.fnr"),
    ("citation_rate", "evidence.citation_rate"),
    ("grounding_rate", "evidence.grounding_rate"),
    ("fabrication_rate", "evidence.fabrication_rate"),
    ("unsupported_claim_rate", "evidence.unsupported_claim_rate"),
    ("hallucination_rate", "evidence.hallucination_rate"),
    ("retrieval_recall", "evidence.retrieval_recall"),
    ("missing_evidence_detection_rate", "evidence.missing_evidence_detection_rate"),
    ("latency_median_ms", "timing.latency_ms.median"),
    ("latency_p95_ms", "timing.latency_ms.p95"),
    ("mean_total_tokens", "timing.total_tokens.mean"),
)


def _dig(payload: Mapping[str, Any], path: str) -> Any:
    node: Any = payload
    for part in path.split("."):
        if not isinstance(node, _MappingABC):
            return None
        node = node.get(part)
    return node


def compare_modes(results_by_mode: Mapping[Any, Sequence[Any]]) -> pd.DataFrame:
    """The A/B/C table: one row per experiment mode, one column per headline metric.

    ``results_by_mode`` maps a mode (an :class:`ExperimentMode`, its value, or any label)
    to that mode's results. Rows come out in A, B, C order regardless of insertion order.
    Every cell is computed by the same functions as :func:`compute_metrics`, so a number
    in this table and the same number in a persisted run's metrics blob cannot disagree.

    Undefined cells are ``NaN``, not 0 - see the module docstring. Transpose the frame
    (``.T``) for a thesis table with metrics down the side and modes across the top.

    This returns figures only, never a verdict: with the mandated datasets each row is
    computed from six observations, so a difference between two rows is a description of
    these runs and not evidence of a difference between the approaches.
    """
    rows: List[Dict[str, Any]] = []
    index: List[str] = []

    ordered = sorted(
        results_by_mode.keys(),
        key=lambda key: (
            list(ExperimentMode).index(ExperimentMode.coerce(key, None))
            if ExperimentMode.coerce(key, None) is not None
            else len(ExperimentMode) + 1,
            str(key),
        ),
    )

    for key in ordered:
        payload = compute_metrics(results_by_mode[key], include_by_mode=False)
        row: Dict[str, Any] = {}
        for column, path in _COMPARISON_COLUMNS:
            value = _dig(payload, path)
            row[column] = np.nan if value is None else value
        row["caveats"] = len(payload.get("caveats", []))
        rows.append(row)
        index.append(_mode_label(key) or str(key))

    columns = [column for column, _ in _COMPARISON_COLUMNS] + ["caveats"]
    if not rows:
        return pd.DataFrame(columns=columns, index=pd.Index([], name="mode"))
    return pd.DataFrame(rows, index=pd.Index(index, name="mode"), columns=columns)


# ------------------------------------------------------------------------- top level
def _build_caveats(
    items: Sequence[NormalisedResult],
    classification: Mapping[str, Any],
    evidence: Mapping[str, Any],
    agreement: Optional[Mapping[str, Any]],
    timing: Mapping[str, Any],
) -> List[str]:
    """Assemble the automatic warnings that must travel with these figures.

    Every caveat is triggered by a condition in the data, not by an author remembering
    to write it down. The thresholds are :data:`MIN_SAMPLE_SIZE`,
    :data:`MIN_CLASS_SUPPORT`, :data:`MIN_KAPPA_PAIRS` and
    :data:`MIN_PERCENTILE_SAMPLE`; they are conventional rules of thumb, not tests, and
    clearing them would not make a result significant.
    """
    caveats: List[str] = []
    n = len(items)
    n_scored = int(classification.get("n_scored") or 0)

    if n == 0:
        caveats.append("No results supplied: every metric below is undefined.")
        return caveats

    if n < MIN_SAMPLE_SIZE:
        caveats.append(
            "n = {0} is below {1}. Every proportion here has a confidence interval wider than "
            "the differences it would be used to compare; report these as descriptive figures "
            "for these runs, not as estimates of general performance.".format(n, MIN_SAMPLE_SIZE)
        )

    unscored = n - n_scored
    if unscored:
        caveats.append(
            "{0} of {1} results have no ground-truth status and are excluded from every "
            "classification metric.".format(unscored, n)
        )

    thin = [
        "{0} (support {1})".format(label, cell["support"])
        for label, cell in classification.get("per_class", {}).items()
        if 0 < cell["support"] < MIN_CLASS_SUPPORT
    ]
    if thin:
        caveats.append(
            "Classes with fewer than {0} ground-truth cases: {1}. Per-class precision and "
            "recall for these move by large steps with a single row.".format(
                MIN_CLASS_SUPPORT, ", ".join(thin)
            )
        )

    absent = [
        label
        for label, cell in classification.get("per_class", {}).items()
        if cell["support"] == 0
    ]
    if absent:
        caveats.append(
            "Labels predicted but never present in the ground truth: {0}. They contribute a "
            "zero row to macro averages.".format(", ".join(absent))
        )

    missing_labels = [label for label in STATUS_ORDER if label not in classification.get("labels", [])]
    if missing_labels:
        caveats.append(
            "Statuses absent from both ground truth and predictions, so untested here: "
            "{0}.".format(", ".join(missing_labels))
        )

    errors = sum(1 for item in items if item.has_error)
    if errors:
        caveats.append(
            "{0} of {1} results recorded an execution error; a row with no prediction is "
            "scored as '{2}' and counts against accuracy.".format(errors, n, NO_PREDICTION_LABEL)
        )

    mismatched = sum(
        1
        for item in items
        if item.stored_status_correct is not None
        and item.stored_status_correct != item.status_correct
    )
    if mismatched:
        caveats.append(
            "{0} results have a stored status_correct flag that disagrees with a direct "
            "comparison of expected and predicted status; the flag was ignored.".format(mismatched)
        )

    if not evidence.get("citations_total"):
        caveats.append(
            "No citations were recorded at all, so grounding_rate and fabrication_rate are "
            "undefined rather than zero."
        )
    if not evidence.get("retrieval_markers_total"):
        caveats.append(
            "No dataset supplied key_evidence_markers, so retrieval_recall is undefined."
        )
    if not evidence.get("missing_evidence_cases"):
        caveats.append(
            "No ground-truth INSUFFICIENT_EVIDENCE case is present, so "
            "missing_evidence_detection_rate is undefined."
        )

    timed = int(timing.get("n_timed") or 0)
    if 0 < timed < MIN_PERCENTILE_SAMPLE:
        caveats.append(
            "Latency p95 is computed from {0} observations; below {1} it is effectively the "
            "maximum, not a percentile.".format(timed, MIN_PERCENTILE_SAMPLE)
        )

    if agreement is not None:
        pairs = int(agreement.get("n_status_pairs") or 0)
        if pairs == 0:
            caveats.append("No completed human reviews: every human/AI agreement figure is undefined.")
        elif pairs < MIN_KAPPA_PAIRS:
            caveats.append(
                "Cohen's kappa is computed on {0} review pairs, fewer than {1}; the chance term "
                "is estimated from the same handful of observations and the value is not "
                "interpretable as chance-corrected agreement.".format(pairs, MIN_KAPPA_PAIRS)
            )
        if pairs and agreement.get("status_kappa") is None:
            caveats.append(
                "Cohen's kappa for status is undefined: both raters used a single identical "
                "category, so expected chance agreement is 1 and the correction divides by zero."
            )

    return caveats


#: Shipped inside every metrics blob so a number can never be read without its formula.
_TOP_LEVEL_DEFINITIONS: Dict[str, str] = {
    "n": "results supplied to compute_metrics, before any exclusion",
    "n_scored": "results with a ground-truth status; the denominator of every classification metric",
    "no_prediction": (
        "a result with a ground truth but no predicted status is scored as the label "
        "'{0}' rather than dropped".format(NO_PREDICTION_LABEL)
    ),
    "undefined_rates": "a rate whose denominator is zero is reported as null, never as 0.0",
    "ground_truth": (
        "expected_status is the answer key authored by the synthetic dataset generator; "
        "accuracy here is accuracy against a designed answer, not against an audited reality"
    ),
}


def grounded_accuracy_metrics(
    results: Sequence[Any],
    exempt_insufficient: bool = False,
) -> Dict[str, Any]:
    """Accuracy that only credits a prediction an auditor could actually defend.

    Plain accuracy treats "right for no reason" and "right and evidenced" as the same
    success. In an audit they are not the same thing at all: a conclusion that cannot be
    traced to a verified quotation is not a working paper, it is an opinion. This metric
    therefore counts a prediction as a success only when BOTH conditions hold:

        (a) ``predicted_status == expected_status``, and
        (b) the assessment carries at least one VERIFIED citation
            (``verified_citation_count >= 1``).

    Formally, with N scored rows::

        grounded_accuracy = |{i : correct(i) AND verified_citations(i) >= 1}| / N

    The 2x2 breakdown is reported alongside it, because the interesting cell is
    ``correct_but_ungrounded`` - the rows where the system reached the right answer by a
    route nobody can check. A configuration that scores well on accuracy and badly here
    is producing lucky guesses, and that distinction is invisible in accuracy alone.

    ``exempt_insufficient`` controls one genuine judgement call. When the ground truth is
    INSUFFICIENT_EVIDENCE, one could argue a correct abstention needs no supporting
    citation. This implementation defaults to ``False`` - i.e. it still demands a verified
    citation - because the system is designed to cite the evidence it *did* read when
    explaining what is missing, and an abstention with no citation is indistinguishable
    from a failure to retrieve anything at all. Set it to ``True`` to report the lenient
    variant; both are computed by :func:`compute_metrics` so a write-up can state either
    and say which was used.

    Rows carrying an error, or lacking either status, are excluded from the denominator
    exactly as in :func:`classification_metrics`.
    """
    items = normalise_results(results)
    scored = [i for i in items if not i.has_error and i.expected_status and i.predicted_status]
    n = len(scored)

    cells = {
        "correct_and_grounded": 0,
        "correct_but_ungrounded": 0,
        "incorrect_but_grounded": 0,
        "incorrect_and_ungrounded": 0,
    }
    for item in scored:
        correct = item.predicted_status == item.expected_status
        exempt = exempt_insufficient and item.expected_status == AssessmentStatus.INSUFFICIENT_EVIDENCE.value
        grounded = exempt or int(item.verified_citation_count or 0) >= 1
        if correct and grounded:
            cells["correct_and_grounded"] += 1
        elif correct:
            cells["correct_but_ungrounded"] += 1
        elif grounded:
            cells["incorrect_but_grounded"] += 1
        else:
            cells["incorrect_and_ungrounded"] += 1

    accuracy = _ratio(cells["correct_and_grounded"] + cells["correct_but_ungrounded"], n)
    grounded = _ratio(cells["correct_and_grounded"], n)
    return {
        "n_scored": n,
        "grounded_accuracy": grounded,
        "plain_accuracy": accuracy,
        #: How much of the headline accuracy rests on conclusions nobody can check.
        "ungrounded_credit": _ratio(cells["correct_but_ungrounded"], n),
        "exempt_insufficient": bool(exempt_insufficient),
        "breakdown": cells,
        "definition": (
            "A prediction counts as a success only if it matches the ground truth AND the "
            "assessment carries at least one citation that the validator marked VERIFIED. "
            "grounded_accuracy = correct_and_grounded / n_scored."
        ),
    }


def compute_metrics(
    results: Sequence[Any],
    reviews: Optional[Sequence[Any]] = None,
    labels: Optional[Sequence[str]] = None,
    include_by_mode: bool = True,
) -> Dict[str, Any]:
    """The full metric set for one evaluation run, as a JSON-serialisable dict.

    ``results`` may be ORM ``EvaluationResult`` rows, plain dicts with the same keys, or
    already-normalised :class:`NormalisedResult` objects - mixed freely. ``reviews`` is
    optional and adds the human/AI concordance block; without it that block is omitted
    rather than reported as zeros.

    The returned dict is designed to be stored verbatim in ``EvaluationRun.metrics`` and
    contains no numpy scalars, no DataFrames and no ``NaN`` - undefined values are
    ``None``. Sections:

    ``n`` / ``n_scored`` / ``n_errors``
        Sample sizes. Present unconditionally: this function will not emit a metric
        without the count it was computed from.
    ``classification``
        Per-class and averaged multi-class metrics - :func:`classification_metrics`.
    ``confusion_matrix``
        Nested ``{expected: {predicted: count}}`` - :func:`confusion_matrix`. Use
        :func:`confusion_matrix_frame` for the same thing as a DataFrame.
    ``deficiency_detection`` / ``deficiency_detection_excluding_insufficient``
        The binary framing both ways round - :func:`deficiency_detection_metrics`. Report
        both; the first counts INSUFFICIENT_EVIDENCE as a negative and so depresses
        recall, the second removes those rows as abstentions.
    ``evidence``
        Citation, grounding, fabrication and retrieval figures - :func:`evidence_metrics`.
    ``timing``
        Latency and token distributions - :func:`timing_metrics`.
    ``human_agreement``
        Present only when ``reviews`` is supplied - :func:`agreement_metrics`.
    ``by_mode``
        Present only when the results carry more than one experiment mode; the same
        blocks recomputed per mode.
    ``caveats``
        Automatic warnings - see :func:`_build_caveats`. An empty list means no threshold
        was crossed, which is not the same as "these numbers are reliable".
    """
    items = normalise_results(results)

    classification = classification_metrics(items, labels)
    evidence = evidence_metrics(items)
    timing = timing_metrics(items, include_by_mode=include_by_mode)
    agreement = agreement_metrics(reviews) if reviews is not None else None

    payload: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n": len(items),
        "n_scored": classification["n_scored"],
        "n_errors": sum(1 for item in items if item.has_error),
        "datasets": sorted({item.dataset_id for item in items if item.dataset_id}),
        "modes": sorted({item.mode for item in items if item.mode}),
        "labels": classification["labels"],
        "classification": classification,
        "confusion_matrix": confusion_matrix(items, labels),
        "confusion_matrix_orientation": "rows = expected (ground truth), columns = predicted",
        "deficiency_detection": deficiency_detection_metrics(items, "negative"),
        "deficiency_detection_excluding_insufficient": deficiency_detection_metrics(
            items, "excluded"
        ),
        "grounded_accuracy": grounded_accuracy_metrics(items, exempt_insufficient=False),
        "grounded_accuracy_lenient": grounded_accuracy_metrics(items, exempt_insufficient=True),
        "evidence": evidence,
        "timing": timing,
        "definitions": _TOP_LEVEL_DEFINITIONS,
    }

    if agreement is not None:
        payload["human_agreement"] = agreement

    if include_by_mode:
        grouped = group_by_mode(items)
        if len(grouped) > 1:
            payload["by_mode"] = {
                mode: compute_metrics(rows, include_by_mode=False) for mode, rows in grouped.items()
            }

    payload["caveats"] = _build_caveats(items, classification, evidence, agreement, timing)
    return payload


__all__ = [
    "MIN_CLASS_SUPPORT",
    "MIN_KAPPA_PAIRS",
    "MIN_PERCENTILE_SAMPLE",
    "MIN_SAMPLE_SIZE",
    "NO_PREDICTION_LABEL",
    "POSITIVE_STATUSES",
    "STATUS_ORDER",
    "NormalisedResult",
    "NormalisedReview",
    "agreement_metrics",
    "classification_metrics",
    "cohens_kappa",
    "compare_modes",
    "compute_metrics",
    "confusion_matrix",
    "confusion_matrix_frame",
    "deficiency_detection_metrics",
    "evidence_metrics",
    "grounded_accuracy_metrics",
    "group_by_mode",
    "normalise_result",
    "normalise_results",
    "normalise_review",
    "per_class_frame",
    "results_to_frame",
    "timing_metrics",
]
