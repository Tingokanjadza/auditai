"""Dashboard - the audit console home.

What this page is for
---------------------
An auditor arriving at the console needs three answers before anything else: how much
of the engagement has been looked at, what is outstanding, and what the machine has
*not* been able to settle. Everything here serves one of those three questions, and
nothing here is a conclusion: every figure is computed over AI-generated assessments
that no auditor has necessarily signed off yet.

The distinction this page refuses to blur
-----------------------------------------
INSUFFICIENT_EVIDENCE is not a deficiency. A deficiency says "the control did not
operate as required"; insufficient evidence says "the evidence supplied does not permit
that judgement either way". They call for different work - one goes into the findings
register, the other goes back to the client as an evidence request - and a dashboard
that adds them into a single "problems" number would be making a methodological error
in the reader's name. So they are counted apart, coloured apart (violet, not a shade of
red), described apart in the attention queue, and the note under the metric row says so
in words.

Why "evidence coverage" is phrased so carefully
-----------------------------------------------
This system does not map evidence files to controls: an ``EvidenceFile`` belongs to a
project, and which evidence bears on which control is decided at assessment time by
retrieval. The coverage panel therefore reports what is actually knowable - whether an
assessment has been run for a control and whether it managed to cite anything - and
says explicitly that an unassessed control's coverage is unknown rather than zero.
"""

from __future__ import annotations

import sys
from pathlib import Path

# A page module is exec'd by ``st.Page`` in the entry script's process, where the
# repository root is already on ``sys.path``. It is repeated here so the module also
# imports cleanly when it is loaded directly (a test, ``streamlit run`` on this file).
_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from typing import Any, Callable, Dict, List, Optional, Sequence  # noqa: E402

import streamlit as st  # noqa: E402

from app.frontend import components, data_access, state, theme  # noqa: E402
from app.schemas.enums import (  # noqa: E402
    DEFICIENCY_STATUSES,
    HIGH_RISK_LEVELS,
    RISK_ORDER,
    AssessmentStatus,
    RiskLevel,
)

#: Filter namespace for :mod:`app.frontend.state`.
PAGE = "dashboard"

#: Deficiency statuses and high-risk bands, as sets of raw values. Imported from the
#: enum module rather than restated so the dashboard cannot drift from the definitions
#: the service layer and the report use.
_DEFICIENCY_VALUES = {status.value for status in DEFICIENCY_STATUSES}
_HIGH_RISK_VALUES = {level.value for level in HIGH_RISK_LEVELS}

#: How severe a status is when ordering the attention queue. NOT_EFFECTIVE outranks
#: POTENTIAL_DEFICIENCY because it is a concluded failure rather than an indication;
#: INSUFFICIENT_EVIDENCE sits below both because it is not a failure at all, only an
#: unanswered question - but above EFFECTIVE, because the question is still open.
_STATUS_SEVERITY: Dict[str, int] = {
    AssessmentStatus.NOT_EFFECTIVE.value: 4,
    AssessmentStatus.POTENTIAL_DEFICIENCY.value: 3,
    AssessmentStatus.INSUFFICIENT_EVIDENCE.value: 2,
    AssessmentStatus.NOT_APPLICABLE.value: 1,
    AssessmentStatus.EFFECTIVE.value: 0,
}

#: Rows shown in the attention queue before it is truncated with a count.
_QUEUE_LIMIT = 8

_ACTIVITY_LIMIT = 25


# ---- small helpers
def _read(loader: Callable[[], Any], fallback: Any, what: str) -> Any:
    """Run one data-access read, reporting failure on the page instead of raising."""
    try:
        return loader()
    except data_access.DataAccessError as exc:
        st.error("Could not load {0}: {1}".format(what, exc))
        return fallback


def _page_exists(module_name: str) -> bool:
    """Whether a sibling page module is installed in this build.

    The pages are written by several contributors and the shell skips any that are
    absent, so a jump-to link has to check before it offers to navigate: ``switch_page``
    to a page that is not in the navigation raises.
    """
    return (Path(__file__).resolve().parent / module_name).exists()


def _jump(module_name: str, assessment_id: Optional[int] = None, control_ref: str = "") -> None:
    """Select an assessment and open the page that works on it."""
    if assessment_id is not None:
        state.set_current_assessment(int(assessment_id))
    if control_ref:
        state.set_current_control_ref(control_ref)
    if _page_exists(module_name):
        st.switch_page("pages/{0}".format(module_name))
    state.flash(
        "Selected {0}. The {1} page is not installed in this build.".format(
            control_ref or "assessment {0}".format(assessment_id), module_name
        ),
        "warning",
    )
    st.rerun()


def _scope() -> Optional[int]:
    """Which project the figures cover: the selected engagement, or all of them.

    Kept as a page filter rather than derived silently from the sidebar, because
    "3 potential deficiencies" means something quite different across a whole database
    than within one engagement, and the reader has to be able to tell which they are
    looking at.
    """
    project_id = state.current_project_id()
    if project_id is None:
        return None
    # The label is captured rather than read inside ``format_func``: Streamlit re-invokes
    # that callable outside the script run, where the session state it would read is not
    # available, and a label that changes between calls breaks the widget's own lookup.
    labels = {"project": state.current_project_name(), "all": "All engagements"}
    choice = st.radio(
        "Figures cover",
        options=["project", "all"],
        index=0 if state.get_filter(PAGE, "scope", "project") == "project" else 1,
        format_func=lambda value: labels.get(value, value),
        horizontal=True,
        key="dashboard_scope",
        label_visibility="collapsed",
    )
    state.set_filter(PAGE, "scope", choice)
    return project_id if choice == "project" else None


# ---- headline figures
def _metrics(stats: Dict[str, Any]) -> None:
    assessed = int(stats.get("controls_assessed", 0) or 0)
    in_scope = int(stats.get("controls_in_scope", 0) or 0)
    deficiencies = int(stats.get("potential_deficiencies", 0) or 0)
    not_effective = int(stats.get("not_effective", 0) or 0)
    definitions = dict(stats.get("definitions", {}) or {})

    components.metric_row(
        [
            {
                "label": "Controls in scope",
                "value": in_scope,
                "caption": "Library controls linked to the engagement.",
                "help_text": "Distinct controls scoped to this project.",
            },
            {
                "label": "Controls assessed",
                "value": assessed,
                "delta": "{0} of {1} in scope".format(assessed, in_scope) if in_scope else "",
                "color": theme.ACCENT,
                "caption": "Counted over the latest assessment per control.",
                "help_text": definitions.get("counting_basis", ""),
            },
            {
                "label": "Effective",
                "value": int(stats.get("effective", 0) or 0),
                "color": theme.status_color(AssessmentStatus.EFFECTIVE.value),
                "caption": "AI concluded the control operated as required.",
                "help_text": "Not an audit conclusion until an auditor records one.",
            },
            {
                "label": "Potential deficiencies",
                "value": deficiencies,
                "delta": "{0} concluded not effective".format(not_effective) if not_effective else "",
                "color": theme.status_color(AssessmentStatus.POTENTIAL_DEFICIENCY.value),
                "caption": "Control indicated as not operating as required.",
                "help_text": "POTENTIAL_DEFICIENCY on the latest assessment per control.",
            },
            {
                "label": "Insufficient evidence",
                "value": int(stats.get("insufficient_evidence", 0) or 0),
                "color": theme.status_color(AssessmentStatus.INSUFFICIENT_EVIDENCE.value),
                "caption": "Not a deficiency - the evidence does not settle it either way.",
                "help_text": (
                    "The control could not be tested from the evidence supplied. The "
                    "work this implies is an evidence request, not a finding."
                ),
            },
            {
                "label": "High-risk findings",
                "value": int(stats.get("high_risk_findings", 0) or 0),
                "color": theme.risk_color(RiskLevel.HIGH.value),
                "caption": "Deficiency at HIGH or CRITICAL on the prototype model.",
                "help_text": definitions.get("high_risk_findings", ""),
            },
            {
                "label": "Reviews pending",
                "value": int(stats.get("pending_human_reviews", 0) or 0),
                "color": theme.AI_COLOR,
                "caption": "No auditor decision recorded yet.",
                "help_text": definitions.get("pending_human_reviews", ""),
            },
        ],
        columns=4,
    )
    components.note(
        "Insufficient evidence is counted separately from deficiencies throughout this "
        "console. A deficiency is a judgement that the control did not operate as "
        "required; insufficient evidence is the absence of a basis for any judgement. "
        + definitions.get("counting_basis", "")
    )


# ---- distribution charts
def _bar_chart(
    labels: Sequence[str],
    values: Sequence[int],
    colors: Sequence[str],
    hover_noun: str,
    height: int = 260,
) -> None:
    """One horizontal bar chart in the console palette, or a caption when empty."""
    if not any(int(value) for value in values):
        st.caption("Nothing has been assessed yet, so there is no distribution to show.")
        return
    try:
        import plotly.graph_objects as go
    except Exception as exc:  # noqa: BLE001 - a missing chart must not take the page down
        st.caption("Chart unavailable ({0}). Counts: {1}".format(exc, dict(zip(labels, values))))
        return

    figure = go.Figure(
        go.Bar(
            x=list(values),
            y=list(labels),
            orientation="h",
            marker_color=list(colors),
            text=[str(value) for value in values],
            textposition="outside",
            cliponaxis=False,
            hovertemplate="%{y}: %{x} " + hover_noun + "<extra></extra>",
        )
    )
    # ``theme.style_figure`` replaces whole layout keys, so per-axis tweaks are applied
    # afterwards through update_*axes, which merge instead.
    theme.style_figure(figure, height=height, showlegend=False, margin={"l": 4, "r": 24, "t": 8, "b": 24})
    figure.update_yaxes(autorange="reversed", ticksuffix="  ")
    figure.update_xaxes(title_text="", rangemode="tozero", dtick=1)
    st.plotly_chart(figure, theme=None, config={"displayModeBar": False})


def _distributions(project_id: Optional[int]) -> None:
    statuses = _read(
        lambda: data_access.status_breakdown(project_id), {}, "the status distribution"
    )
    risks = _read(lambda: data_access.risk_breakdown(project_id), {}, "the risk distribution")

    left, right = st.columns(2, gap="medium")
    with left:
        components.section_header(
            "Assessment outcomes",
            subtitle="Latest assessment per control.",
        )
        order = data_access.assessment_statuses()
        _bar_chart(
            [theme.status_label(status) for status in order],
            [int(statuses.get(status, 0) or 0) for status in order],
            [theme.status_color(status) for status in order],
            "control(s)",
        )
        st.caption(
            "Insufficient evidence is the violet band. It is a separate outcome, not a "
            "milder deficiency."
        )
    with right:
        components.section_header(
            "Risk distribution",
            subtitle="Prototype research risk model.",
        )
        order = sorted(
            data_access.risk_levels(),
            key=lambda value: RISK_ORDER.get(RiskLevel.coerce(value, RiskLevel.NOT_RATED), 0),
            reverse=True,
        )
        _bar_chart(
            [level.replace("_", " ").title() for level in order],
            [int(risks.get(level, 0) or 0) for level in order],
            [theme.risk_color(level) for level in order],
            "control(s)",
        )
        st.caption(components.RISK_MODEL_NOTE)


# ---- the attention queue
def _queue_reason(row: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Why this assessment is in the queue, or None when it is not.

    The reason is carried with the row rather than being inferred from the badges,
    because "high-risk finding" and "cannot conclude - evidence insufficient" are
    different pieces of work and the queue is useless if the reader has to work out
    which is which.
    """
    status = str(row.get("status", "") or "")
    risk = str(row.get("risk_level", "") or "")
    reviewed = bool(row.get("is_reviewed"))

    if status in _DEFICIENCY_VALUES and risk in _HIGH_RISK_VALUES:
        return {
            "text": "High-risk finding" + ("" if reviewed else ", no auditor decision recorded"),
            "color": theme.risk_color(risk),
        }
    if status == AssessmentStatus.INSUFFICIENT_EVIDENCE.value:
        return {
            "text": "Cannot conclude - evidence does not permit a judgement",
            "color": theme.status_color(status),
        }
    if status in _DEFICIENCY_VALUES and not reviewed:
        return {
            "text": "Finding awaiting auditor decision",
            "color": theme.status_color(status),
        }
    if not reviewed:
        return {"text": "AI assessment awaiting auditor decision", "color": theme.AI_COLOR}
    return None


def _queue_sort_key(row: Dict[str, Any]) -> Any:
    status = str(row.get("status", "") or "")
    risk = RiskLevel.coerce(row.get("risk_level"), RiskLevel.NOT_RATED)
    return (
        -_STATUS_SEVERITY.get(status, 0),
        -RISK_ORDER.get(risk, 0),
        -float(row.get("risk_score", 0.0) or 0.0),
        bool(row.get("is_reviewed")),
        str(row.get("control_ref", "")),
    )


def _attention_queue(project_id: Optional[int]) -> None:
    components.section_header(
        "Requires your attention",
        subtitle="Most severe first. Nothing here is settled until an auditor records a decision.",
        eyebrow="Queue",
    )
    rows = _read(
        lambda: data_access.list_assessments(project_id=project_id, latest_per_control=True),
        [],
        "the assessment queue",
    )
    queued: List[Dict[str, Any]] = []
    for row in rows:
        reason = _queue_reason(row)
        if reason is not None:
            item = dict(row)
            item["_reason"] = reason
            queued.append(item)

    if not queued:
        if rows:
            st.success(
                "Nothing is outstanding: every assessed control has an auditor decision "
                "and none is a high-risk finding."
            )
        else:
            components.empty_state(
                "No assessments yet",
                "Run an assessment from the Assessments page, or load the demonstration "
                "engagement from the sidebar, and anything needing attention will collect here.",
            )
        return

    components.ai_disclaimer_banner(
        "Every row below is a machine-generated assessment queued for auditor review.",
        compact=True,
    )
    queued.sort(key=_queue_sort_key)
    for row in queued[:_QUEUE_LIMIT]:
        _queue_row(row)
    if len(queued) > _QUEUE_LIMIT:
        st.caption(
            "Showing the {0} most severe of {1} outstanding item(s).".format(
                _QUEUE_LIMIT, len(queued)
            )
        )


def _queue_row(row: Dict[str, Any]) -> None:
    reason = dict(row.get("_reason", {}))
    assessment_id = row.get("id")
    control_ref = str(row.get("control_ref", "") or "")

    detail, action = st.columns([6, 1], gap="small")
    with detail:
        components.badges(
            components.plain_badge(control_ref or "?", theme.ACCENT),
            components.status_badge(row.get("status")),
            components.risk_badge(row.get("risk_level"), row.get("risk_score")),
            components.plain_badge(reason.get("text", ""), reason.get("color", "")),
            components.decision_badge(row.get("review_decision")),
        )
        st.markdown(
            '<div class="ia-quad-lead">{0}</div>'.format(
                components.escape(row.get("control_name", ""))
            ),
            unsafe_allow_html=True,
        )
        finding = str(row.get("finding", "") or "").strip()
        if finding:
            st.markdown(
                '<div class="ia-note">{0}</div>'.format(components.escape(finding)),
                unsafe_allow_html=True,
            )
    with action:
        target = "human_review.py" if not row.get("is_reviewed") else "assessments.py"
        if st.button(
            "Open",
            key="dash_open_{0}".format(assessment_id),
            width="stretch",
            help="Open this assessment for review.",
        ):
            _jump(target, assessment_id=assessment_id, control_ref=control_ref)


# ---- evidence coverage
def _coverage(project_id: Optional[int]) -> None:
    components.section_header(
        "Evidence coverage",
        subtitle="Which in-scope controls have evidence the pipeline could actually cite.",
        eyebrow="Scope",
    )
    if project_id is None:
        st.caption(
            "Select an engagement in the sidebar to see coverage; it is a property of one "
            "project's scope, not of the whole database."
        )
        return

    controls = _read(
        lambda: data_access.list_scoped_controls(project_id), [], "the scoped controls"
    )
    if not controls:
        components.empty_state(
            "No controls in scope",
            "Scope controls to this engagement on the Audit Projects page before "
            "assessing anything.",
        )
        return

    assessments = _read(
        lambda: data_access.list_assessments(project_id=project_id, latest_per_control=True),
        [],
        "the assessments",
    )
    by_ref = {str(row.get("control_ref", "")): row for row in assessments}
    files = _read(lambda: data_access.list_evidence(project_id=project_id), [], "the evidence")

    uncited: List[Dict[str, Any]] = []
    unassessed: List[Dict[str, Any]] = []
    covered: List[Dict[str, Any]] = []
    for control in controls:
        ref = str(control.get("control_id", ""))
        assessment = by_ref.get(ref)
        entry = {
            "control_ref": ref,
            "control_name": control.get("name", ""),
            "status": (assessment or {}).get("status", ""),
            "evidence_sufficiency": (assessment or {}).get("evidence_sufficiency", ""),
            "citations": int((assessment or {}).get("citation_count", 0) or 0),
            "verified": int((assessment or {}).get("verified_citations", 0) or 0),
            "missing_evidence": list((assessment or {}).get("missing_evidence", []) or []),
        }
        if assessment is None:
            unassessed.append(entry)
        elif entry["citations"] == 0:
            uncited.append(entry)
        else:
            covered.append(entry)

    components.metric_row(
        [
            {
                "label": "Evidence files",
                "value": len(files),
                "caption": "Ingested into this engagement.",
            },
            {
                "label": "Controls with cited evidence",
                "value": len(covered),
                "color": theme.GREEN,
                "caption": "The assessment quoted at least one stored chunk.",
            },
            {
                "label": "Assessed, nothing cited",
                "value": len(uncited),
                "color": theme.status_color(AssessmentStatus.INSUFFICIENT_EVIDENCE.value),
                "caption": "An evidence gap, not a control failure.",
            },
            {
                "label": "Not assessed",
                "value": len(unassessed),
                "color": theme.GREY,
                "caption": "Coverage unknown - nothing has been run.",
            },
        ],
        columns=4,
    )

    if not files:
        st.warning(
            "This engagement has no evidence at all. Every control in scope is therefore "
            "untestable until evidence is uploaded on the Evidence page."
        )

    if uncited:
        st.markdown("**Assessed with no citation - evidence to obtain**")
        components.df_table(
            [
                {
                    "control_ref": item["control_ref"],
                    "control_name": item["control_name"],
                    "status": item["status"],
                    "evidence_sufficiency": item["evidence_sufficiency"],
                    "requested": "; ".join(item["missing_evidence"][:3])
                    or "(the model listed nothing)",
                }
                for item in uncited
            ],
            columns=["control_ref", "control_name", "status", "evidence_sufficiency", "requested"],
            column_config={
                "requested": st.column_config.TextColumn("Evidence the model asked for", width="large")
            },
            empty_message="Nothing to show.",
        )
        st.caption(
            "The requested-evidence column is the model's own list and is itself "
            "AI-generated; treat it as a prompt for the auditor, not as a complete one."
        )

    if unassessed:
        st.markdown("**In scope, not yet assessed**")
        components.badges(
            *[
                components.plain_badge("{0}  {1}".format(item["control_ref"], item["control_name"]))
                for item in unassessed
            ]
        )

    components.note(
        "This application does not map evidence files to controls: a file belongs to the "
        "engagement, and which evidence bears on a control is decided by retrieval when "
        "the control is assessed. Coverage above is therefore what the pipeline managed "
        "to cite, and a control that has not been assessed is reported as unknown rather "
        "than as uncovered."
    )


# ---- activity trail
def _activity(project_id: Optional[int]) -> None:
    components.section_header(
        "Recent activity",
        subtitle="Append-only trail of who did what, newest first.",
        eyebrow="Audit trail",
    )
    rows = _read(
        lambda: data_access.list_activity(project_id=project_id, limit=_ACTIVITY_LIMIT),
        [],
        "the activity trail",
    )
    if not rows:
        st.caption("Nothing has been recorded against this scope yet.")
        return
    components.df_table(
        [
            {
                "created_at": row.get("created_at"),
                "action": str(row.get("action", "")).replace("_", " ").title(),
                "entity": "{0} {1}".format(
                    row.get("entity_type", ""), row.get("entity_id") or ""
                ).strip(),
                "actor": row.get("actor", ""),
                "actor_type": row.get("actor_type", ""),
                "detail": _detail_summary(row.get("details")),
            }
            for row in rows
        ],
        columns=["created_at", "action", "entity", "actor", "actor_type", "detail"],
        column_config={
            "action": st.column_config.TextColumn("Action", width="medium"),
            "entity": st.column_config.TextColumn("Entity", width="small"),
            "actor": st.column_config.TextColumn("Actor", width="medium"),
            "actor_type": st.column_config.TextColumn("AI / human", width="small"),
            "detail": st.column_config.TextColumn("Detail", width="large"),
        },
        height=320,
    )
    st.caption(
        "The actor is a declared name, not an authenticated identity: this prototype has "
        "no login."
    )


def _detail_summary(details: Any, limit: int = 3) -> str:
    """Flatten an activity ``details`` blob into one readable cell."""
    if not isinstance(details, dict) or not details:
        return ""
    parts: List[str] = []
    for key, value in list(details.items())[:limit]:
        if isinstance(value, (list, tuple)):
            rendered = ", ".join(str(item) for item in value[:3]) or "none"
        elif isinstance(value, dict):
            rendered = ", ".join(sorted(value)[:3])
        else:
            rendered = str(value)
        if len(rendered) > 60:
            rendered = rendered[:59] + "…"
        parts.append("{0}: {1}".format(key, rendered))
    if len(details) > limit:
        parts.append("+{0} more".format(len(details) - limit))
    return " · ".join(parts)


# ---- page
def render() -> None:
    project_name = state.current_project_name("all engagements")
    components.section_header(
        "Audit console",
        subtitle="AI assists, the auditor decides. Nothing on this page is an audit conclusion.",
        eyebrow="Dashboard",
    )
    project_id = _scope()
    st.caption(
        "Figures cover {0}.".format(
            project_name if project_id is not None else "every engagement in this database"
        )
    )

    provider = _read(data_access.provider_badge, {}, "the provider status")
    components.ai_disclaimer_banner(
        "Every status, risk band and finding summarised here was produced by an "
        "automated system from the evidence supplied and requires auditor verification.",
        provider=str(provider.get("active_provider", "") or ""),
        model=str(provider.get("active_model", "") or ""),
    )
    if provider.get("is_mock"):
        st.caption(components.MOCK_PROVIDER_NOTE)

    stats = _read(lambda: data_access.dashboard_stats(project_id), {}, "the headline figures")
    if not stats:
        return
    _metrics(stats)

    st.markdown("")
    _distributions(project_id)

    st.markdown("")
    _attention_queue(project_id)

    st.markdown("")
    _coverage(project_id)

    st.markdown("")
    _activity(project_id)

    st.markdown("---")
    definitions = dict(stats.get("definitions", {}) or {})
    with st.expander("How these figures are counted", expanded=False):
        components.kv_grid(
            {
                key.replace("_", " ").title(): value
                for key, value in definitions.items()
            }
        )
        components.kv_grid(
            {
                "Citations verified": "{0} of {1}".format(
                    stats.get("citations_verified", 0), stats.get("citations_total", 0)
                ),
                "Citation grounding rate": "{0:.0%}".format(
                    float(stats.get("citation_grounding_rate", 0.0) or 0.0)
                ),
                "Mean latency": "{0} ms".format(stats.get("avg_latency_ms", 0)),
                "Assessments counted": stats.get("assessments_counted", 0),
                "Completed reviews": stats.get("completed_reviews", 0),
                "Reviewer flagged a fabrication": stats.get("flagged_hallucinations", 0),
            }
        )
        components.note(components.RISK_MODEL_NOTE)


if __name__ == "__main__":
    render()
