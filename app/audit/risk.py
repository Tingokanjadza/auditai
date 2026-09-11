"""Prototype risk scoring for an assessed control.

Why this module exists
----------------------
An auditor reading "HIGH RISK" on a screen must be able to ask *why* and get a real
answer. A single number emitted by a language model cannot answer that question: it is
unstable across runs, it cannot be recomputed, and it cannot be argued with. So risk in
this system is **not** taken from the model. It is computed here, deterministically,
from five named factors, with published weights, and every rating carries a rationale
string that decomposes the score factor by factor.

The model's own suggested band (``model_risk_level``) is retained as an *advisory
signal* only. It can escalate a rating (a conservative, one-directional concession) but
it can never lower one, and it can never override the status-driven adjustments below.

Design tension this resolves
----------------------------
The natural way to combine "how exposed is this system" with "did the control work" is
multiplicative, but a multiplicative model is hard to explain in a report. A weighted
additive model is explainable, at the cost of leaving a residual floor under a control
that tested effective (a maximally privileged, maximally sensitive domain would still
score in the MEDIUM band on the strength of its exposure factors alone). Rather than
hide that, the model applies two explicit, named adjustments - a ceiling for EFFECTIVE
and a floor for INSUFFICIENT_EVIDENCE - and reports both in ``factors`` and in the
rationale so a reader can see exactly where the arithmetic was overridden and why.

Honest scope
------------
This is a research prototype. The weights, the 1-5 factor ladders and the band
thresholds are the author's own defensible choices, calibrated so that the ordering of
outcomes is sensible; they are not derived from, endorsed by, or equivalent to COBIT,
ISO 27005, NIST SP 800-30, FAIR or any other published methodology. Every artefact this
module produces is labelled :data:`RISK_MODEL_LABEL` for exactly that reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from app.schemas.enums import RISK_ORDER, AssessmentStatus, RiskLevel

#: Must appear on every risk rating this system shows or exports.
RISK_MODEL_LABEL = "Prototype research risk scoring model - not an official industry framework."

#: Contribution of each 1-5 factor to the 0-100 score. Deliberately weighted so that
#: what the *evidence says about the control* (severity + likelihood = 55%) outweighs
#: the static exposure of the domain (impact + privilege + sensitivity = 45%).
FACTOR_WEIGHTS: Dict[str, float] = {
    "control_failure_severity": 0.30,
    "likelihood": 0.25,
    "impact": 0.20,
    "privilege_level": 0.15,
    "data_sensitivity": 0.10,
}

#: Lower bound of each band on the 0-100 scale.
BAND_THRESHOLDS: Dict[str, float] = {
    RiskLevel.LOW.value: 0.0,
    RiskLevel.MEDIUM.value: 25.0,
    RiskLevel.HIGH.value: 50.0,
    RiskLevel.CRITICAL.value: 75.0,
}

#: How badly the control is established to have failed, purely from the conclusion.
#: INSUFFICIENT_EVIDENCE sits at the midpoint on purpose - see ``STATUS_SCORE_FLOOR``.
SEVERITY_BY_STATUS: Dict[str, float] = {
    AssessmentStatus.EFFECTIVE.value: 1.0,
    AssessmentStatus.NOT_APPLICABLE.value: 1.0,
    AssessmentStatus.INSUFFICIENT_EVIDENCE.value: 3.0,
    AssessmentStatus.POTENTIAL_DEFICIENCY.value: 4.0,
    AssessmentStatus.NOT_EFFECTIVE.value: 5.0,
}

#: Base likelihood that the underlying risk materialises, before the observed
#: exception rate is taken into account.
LIKELIHOOD_BY_STATUS: Dict[str, float] = {
    AssessmentStatus.EFFECTIVE.value: 1.0,
    AssessmentStatus.NOT_APPLICABLE.value: 1.0,
    AssessmentStatus.INSUFFICIENT_EVIDENCE.value: 3.0,
    AssessmentStatus.POTENTIAL_DEFICIENCY.value: 3.5,
    AssessmentStatus.NOT_EFFECTIVE.value: 4.5,
}

#: Inherent risk of the domain the control protects, as a 1-5 point value.
INHERENT_POINTS: Dict[str, float] = {
    RiskLevel.LOW.value: 1.0,
    RiskLevel.MEDIUM.value: 3.0,
    RiskLevel.HIGH.value: 4.0,
    RiskLevel.CRITICAL.value: 5.0,
    RiskLevel.NOT_RATED.value: 3.0,
}

#: Impact is a blend of the control's declared inherent risk and the two asset-level
#: columns, so that "MFA on 500 domain admins" outranks "MFA on a reporting portal"
#: even when both are tagged HIGH.
IMPACT_BLEND = {"inherent_risk": 0.60, "privilege_level": 0.20, "data_sensitivity": 0.20}

#: An observed exception rate is direct evidence about how often the control fails, so
#: it raises likelihood. Capped: a 30% exception rate is already the maximum uplift,
#: because beyond that the *status* is doing the work, not the ratio.
EXCEPTION_UPLIFT_SLOPE = 5.0
EXCEPTION_UPLIFT_CAP = 1.5

#: "We cannot tell whether this control operates" is itself a risk position. Without
#: this floor an unknown control over a low-sensitivity system could be reported as LOW,
#: which would reward the system for producing an uninformative answer - exactly the
#: failure mode this research is trying to avoid.
STATUS_SCORE_FLOOR: Dict[str, float] = {
    AssessmentStatus.INSUFFICIENT_EVIDENCE.value: BAND_THRESHOLDS[RiskLevel.MEDIUM.value],
}

#: Conversely, when the evidence supports that the control operates as required, the
#: residual risk *attributable to this control* is reported as LOW regardless of the
#: domain's inherent exposure. The exposure factors are still reported in ``factors``
#: so the auditor can see what is at stake if the conclusion is later overturned.
STATUS_SCORE_CEILING: Dict[str, float] = {
    AssessmentStatus.EFFECTIVE.value: BAND_THRESHOLDS[RiskLevel.MEDIUM.value] - 1.0,
}

_FACTOR_LABELS: Dict[str, str] = {
    "control_failure_severity": "control failure severity",
    "likelihood": "likelihood",
    "impact": "impact",
    "privilege_level": "privilege level",
    "data_sensitivity": "data sensitivity",
}


def _clamp(value: float, low: float = 1.0, high: float = 5.0) -> float:
    return max(low, min(high, float(value)))


def _get(control: Any, key: str, default: Any) -> Any:
    """Read a field from either a ``Control`` ORM row or a plain dict.

    The risk model is used by the engine (ORM rows), by the evaluation harness (which
    may hold detached or dict-shaped controls) and by tests, so it must not care.
    """
    if control is None:
        return default
    if isinstance(control, dict):
        value = control.get(key, default)
    else:
        value = getattr(control, key, default)
    return default if value is None else value


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class RiskFactors:
    """The five 1-5 factors, plus the observed exception rate that shaped them."""

    impact: float = 3.0
    likelihood: float = 3.0
    privilege_level: float = 3.0
    data_sensitivity: float = 3.0
    control_failure_severity: float = 3.0
    exception_ratio: Optional[float] = None

    def as_mapping(self) -> Dict[str, float]:
        return {
            "control_failure_severity": self.control_failure_severity,
            "likelihood": self.likelihood,
            "impact": self.impact,
            "privilege_level": self.privilege_level,
            "data_sensitivity": self.data_sensitivity,
        }

    def contributions(self) -> Dict[str, float]:
        """Points each factor adds to the 0-100 score. These sum to the raw score.

        A factor at its floor (1) contributes nothing; at its ceiling (5) it
        contributes its full weight in points.
        """
        values = self.as_mapping()
        return {
            name: FACTOR_WEIGHTS[name] * ((values[name] - 1.0) / 4.0) * 100.0
            for name in FACTOR_WEIGHTS
        }

    def raw_score(self) -> float:
        return sum(self.contributions().values())

    def to_dict(self) -> Dict[str, Any]:
        values = self.as_mapping()
        contributions = self.contributions()
        return {
            "values": {name: round(values[name], 2) for name in values},
            "weights": dict(FACTOR_WEIGHTS),
            "contributions": {name: round(contributions[name], 2) for name in contributions},
            "exception_ratio": self.exception_ratio,
            "scale": "each factor 1-5; contribution = weight x (value - 1) / 4 x 100",
        }


@dataclass
class RiskAssessment:
    """A computed risk rating that can explain itself."""

    level: RiskLevel = RiskLevel.NOT_RATED
    score: float = 0.0
    factors: Dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    band_thresholds: Dict[str, float] = field(default_factory=lambda: dict(BAND_THRESHOLDS))
    model_label: str = RISK_MODEL_LABEL

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level.value,
            "score": round(self.score, 2),
            "factors": self.factors,
            "rationale": self.rationale,
            "band_thresholds": dict(self.band_thresholds),
            "model_label": self.model_label,
        }


def band_for_score(score: float) -> RiskLevel:
    """Map a 0-100 score onto a band. Thresholds are inclusive lower bounds."""
    if score >= BAND_THRESHOLDS[RiskLevel.CRITICAL.value]:
        return RiskLevel.CRITICAL
    if score >= BAND_THRESHOLDS[RiskLevel.HIGH.value]:
        return RiskLevel.HIGH
    if score >= BAND_THRESHOLDS[RiskLevel.MEDIUM.value]:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def _next_band(level: RiskLevel) -> RiskLevel:
    """The band immediately above ``level``; CRITICAL is its own ceiling."""
    ladder = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]
    if level not in ladder:
        return RiskLevel.MEDIUM
    index = ladder.index(level)
    return ladder[min(index + 1, len(ladder) - 1)]


def derive_factors(
    control: Any,
    status: Union[AssessmentStatus, str, None],
    exception_ratio: Optional[float] = None,
) -> RiskFactors:
    """Turn a control definition plus an assessment conclusion into the five factors.

    ``control`` may be a :class:`app.database.models.Control` row or a dict with the
    same keys (``inherent_risk``, ``privilege_level``, ``data_sensitivity``).
    """
    resolved = AssessmentStatus.coerce(status, AssessmentStatus.INSUFFICIENT_EVIDENCE)

    privilege = _clamp(_as_int(_get(control, "privilege_level", 3), 3))
    sensitivity = _clamp(_as_int(_get(control, "data_sensitivity", 3), 3))
    inherent = RiskLevel.coerce(_get(control, "inherent_risk", RiskLevel.MEDIUM.value), RiskLevel.MEDIUM)
    inherent_points = INHERENT_POINTS.get(inherent.value, 3.0)

    impact = _clamp(
        IMPACT_BLEND["inherent_risk"] * inherent_points
        + IMPACT_BLEND["privilege_level"] * privilege
        + IMPACT_BLEND["data_sensitivity"] * sensitivity
    )

    ratio: Optional[float] = None
    if exception_ratio is not None:
        try:
            ratio = max(0.0, min(1.0, float(exception_ratio)))
        except (TypeError, ValueError):
            ratio = None

    uplift = 0.0
    if ratio is not None and resolved is not AssessmentStatus.NOT_APPLICABLE:
        uplift = min(EXCEPTION_UPLIFT_CAP, EXCEPTION_UPLIFT_SLOPE * ratio)

    likelihood = _clamp(LIKELIHOOD_BY_STATUS.get(resolved.value, 3.0) + uplift)
    severity = _clamp(SEVERITY_BY_STATUS.get(resolved.value, 3.0))

    return RiskFactors(
        impact=impact,
        likelihood=likelihood,
        privilege_level=privilege,
        data_sensitivity=sensitivity,
        control_failure_severity=severity,
        exception_ratio=ratio,
    )


def score_risk(
    control: Any,
    status: Union[AssessmentStatus, str, None],
    exception_ratio: Optional[float] = None,
    model_risk_level: Union[RiskLevel, str, None] = None,
) -> RiskAssessment:
    """Score one assessed control.

    ``exception_ratio`` is the fraction of the tested population that failed (0.0-1.0),
    when it is known; ``model_risk_level`` is whatever band the LLM proposed, which is
    recorded and may escalate but never lower the computed rating.
    """
    resolved = AssessmentStatus.coerce(status, AssessmentStatus.INSUFFICIENT_EVIDENCE)
    control_ref = str(_get(control, "control_id", "") or "")
    control_name = str(_get(control, "name", "") or "")
    suggested = RiskLevel.coerce(model_risk_level, None)

    factors = derive_factors(control, resolved, exception_ratio)
    raw_score = factors.raw_score()
    score = raw_score
    adjustments: List[Dict[str, Any]] = []

    # A control that is out of scope carries no rating at all; rating it would put a
    # number next to a control nobody tested.
    if resolved is AssessmentStatus.NOT_APPLICABLE:
        rationale = _build_rationale(
            control_ref=control_ref,
            control_name=control_name,
            status=resolved,
            factors=factors,
            raw_score=raw_score,
            final_score=0.0,
            level=RiskLevel.NOT_RATED,
            adjustments=[
                {
                    "name": "not_applicable",
                    "detail": "Control marked NOT_APPLICABLE; no risk band is assigned to an untested control.",
                }
            ],
            suggested=suggested,
        )
        return RiskAssessment(
            level=RiskLevel.NOT_RATED,
            score=0.0,
            factors=_factors_payload(factors, raw_score, 0.0, [], suggested, RiskLevel.NOT_RATED),
            rationale=rationale,
        )

    floor = STATUS_SCORE_FLOOR.get(resolved.value)
    if floor is not None and score < floor:
        adjustments.append(
            {
                "name": "insufficient_evidence_floor",
                "from": round(score, 2),
                "to": round(floor, 2),
                "detail": (
                    "Raised to the MEDIUM floor: an unverifiable control is an open risk, "
                    "not a low one."
                ),
            }
        )
        score = floor

    ceiling = STATUS_SCORE_CEILING.get(resolved.value)
    if ceiling is not None and score > ceiling:
        adjustments.append(
            {
                "name": "effective_ceiling",
                "from": round(score, 2),
                "to": round(ceiling, 2),
                "detail": (
                    "Capped at the LOW band: the evidence supports that the control operates, "
                    "so the domain's inherent exposure is treated as mitigated for this control."
                ),
            }
        )
        score = ceiling

    level = band_for_score(score)

    # Advisory escalation only, and capped at a single band. The model may nudge a
    # rating upward (under-rating is the more damaging error) but it may not set the
    # rating: allowing an unvalidated model output to jump three bands would put risk
    # back in the model's hands, which is precisely what this module exists to prevent.
    # It also may not undo the EFFECTIVE ceiling - a model that calls a control
    # effective and then asks for a high band is contradicting itself, and that belongs
    # in front of a human rather than inside the arithmetic.
    if suggested is not None and suggested is not RiskLevel.NOT_RATED:
        if RISK_ORDER.get(suggested, 0) > RISK_ORDER.get(level, 0) and ceiling is None:
            escalated_level = _next_band(level)
            escalated = BAND_THRESHOLDS[escalated_level.value]
            adjustments.append(
                {
                    "name": "model_escalation",
                    "from": round(score, 2),
                    "to": round(escalated, 2),
                    "detail": (
                        "The model proposed {0}, above the computed {1}; the rating is raised one "
                        "band to {2}. The model's suggestion is advisory and cannot move it "
                        "further.".format(suggested.value, level.value, escalated_level.value)
                    ),
                }
            )
            score = max(score, escalated)
            level = escalated_level
        elif suggested is not level:
            adjustments.append(
                {
                    "name": "model_disagreement_recorded",
                    "detail": (
                        "The model proposed {0}; the computed band {1} stands. Recorded for the "
                        "human reviewer, not applied.".format(suggested.value, level.value)
                    ),
                }
            )

    score = round(max(0.0, min(100.0, score)), 2)
    rationale = _build_rationale(
        control_ref=control_ref,
        control_name=control_name,
        status=resolved,
        factors=factors,
        raw_score=raw_score,
        final_score=score,
        level=level,
        adjustments=adjustments,
        suggested=suggested,
    )
    return RiskAssessment(
        level=level,
        score=score,
        factors=_factors_payload(factors, raw_score, score, adjustments, suggested, level),
        rationale=rationale,
    )


def _factors_payload(
    factors: RiskFactors,
    raw_score: float,
    final_score: float,
    adjustments: List[Dict[str, Any]],
    suggested: Optional[RiskLevel],
    level: RiskLevel,
) -> Dict[str, Any]:
    payload = factors.to_dict()
    payload.update(
        {
            "raw_score": round(raw_score, 2),
            "final_score": round(final_score, 2),
            "adjustments": adjustments,
            "model_suggested_risk_level": suggested.value if suggested is not None else None,
            "model_agrees_with_computed": (suggested is level) if suggested is not None else None,
            "band_thresholds": dict(BAND_THRESHOLDS),
            "model_label": RISK_MODEL_LABEL,
        }
    )
    return payload


def _build_rationale(
    control_ref: str,
    control_name: str,
    status: AssessmentStatus,
    factors: RiskFactors,
    raw_score: float,
    final_score: float,
    level: RiskLevel,
    adjustments: List[Dict[str, Any]],
    suggested: Optional[RiskLevel],
) -> str:
    """Render the score decomposition an auditor can argue with line by line."""
    header = control_ref or "(unreferenced control)"
    if control_name:
        header = "{0} - {1}".format(header, control_name)

    lines: List[str] = [
        RISK_MODEL_LABEL,
        "Control: {0}".format(header),
        "Assessment status: {0}".format(status.value),
        "Factors (each 1-5); contribution = weight x (value - 1) / 4 x 100 points:",
    ]

    values = factors.as_mapping()
    contributions = factors.contributions()
    for name in FACTOR_WEIGHTS:
        lines.append(
            "  - {label}: {value:.1f}/5 [weight {weight:.0f}%] contributes {points:.1f} points".format(
                label=_FACTOR_LABELS[name],
                value=values[name],
                weight=FACTOR_WEIGHTS[name] * 100,
                points=contributions[name],
            )
        )

    if factors.exception_ratio:
        lines.append(
            "  Observed exception rate {0:.1%} raised likelihood above its base value for this status.".format(
                factors.exception_ratio
            )
        )
    elif factors.exception_ratio == 0.0:
        lines.append("  No exceptions were observed in the tested population, so likelihood was not raised.")

    lines.append("Weighted total: {0:.1f} / 100".format(raw_score))
    for adjustment in adjustments:
        if "from" in adjustment and "to" in adjustment:
            lines.append(
                "Adjustment [{name}] {frm:.1f} -> {to:.1f}: {detail}".format(
                    name=adjustment["name"],
                    frm=float(adjustment["from"]),
                    to=float(adjustment["to"]),
                    detail=adjustment["detail"],
                )
            )
        else:
            lines.append("Note [{0}]: {1}".format(adjustment["name"], adjustment["detail"]))

    if level is RiskLevel.NOT_RATED:
        lines.append("Final: NOT_RATED (no score assigned).")
    else:
        lines.append(
            "Final score {0:.1f} falls in band {1} (bands: LOW 0-24.9, MEDIUM 25-49.9, "
            "HIGH 50-74.9, CRITICAL 75-100).".format(final_score, level.value)
        )

    if suggested is not None and suggested is not RiskLevel.NOT_RATED:
        lines.append("Model-suggested band for comparison: {0}.".format(suggested.value))

    lines.append(
        "This rating is a research prototype output and requires auditor review before it is relied upon."
    )
    return "\n".join(lines)


__all__ = [
    "BAND_THRESHOLDS",
    "FACTOR_WEIGHTS",
    "RISK_MODEL_LABEL",
    "RiskAssessment",
    "RiskFactors",
    "band_for_score",
    "derive_factors",
    "score_risk",
]
