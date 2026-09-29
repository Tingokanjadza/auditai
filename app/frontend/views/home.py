"""Home - where an auditor starts.

What this page is for
---------------------
A first-time auditor opens the console and must be able to see value without choosing a
model, configuring embeddings or understanding retrieval. So the page offers two doors
- "Start an audit" and "Try the demo audit" - then answers, for the audit they are
working in, three plain questions: how far along is it, what should I do next, and what
is waiting for my decision. Everything that used to make this screen a dashboard (tiles,
distribution charts, evidence coverage, the activity trail) is still here, folded into
one expander at the bottom for the auditor who wants the figures.

Nothing on this page is a conclusion. Every count is taken over AI-generated
assessments that no auditor has necessarily signed off yet, and every row in the
attention list is a machine's suggestion queued for a human decision.

The distinction this page refuses to blur
-----------------------------------------
INSUFFICIENT_EVIDENCE is not a deficiency. A deficiency says "the control did not
operate as required"; insufficient evidence says "the evidence supplied does not permit
that judgement either way". They call for different work - one goes into the findings
register, the other goes back to the client as an evidence request - so they are counted
apart, coloured apart and described apart in the attention list.

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

from datetime import datetime  # noqa: E402
from typing import Any, Callable, Dict, List, Optional, Sequence  # noqa: E402

import streamlit as st  # noqa: E402
from streamlit.errors import StreamlitAPIException  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.frontend import components, data_access, state, theme  # noqa: E402
from app.schemas.enums import (  # noqa: E402
    DEFICIENCY_STATUSES,
    HIGH_RISK_LEVELS,
    RISK_ORDER,
    AssessmentStatus,
    RiskLevel,
)

#: Deficiency statuses and high-risk bands, as sets of raw values. Imported from the
#: enum module rather than restated so this page cannot drift from the definitions the
#: service layer and the report use.
_DEFICIENCY_VALUES = {status.value for status in DEFICIENCY_STATUSES}
_HIGH_RISK_VALUES = {level.value for level in HIGH_RISK_LEVELS}

#: How severe a status is when ordering the attention list. NOT_EFFECTIVE outranks
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

#: Rows shown in the attention list before it points at the full review queue.
_QUEUE_LIMIT = 6

_ACTIVITY_LIMIT = 25

#: Where the two doors and the attention list send people.
_ASSESSMENTS_PAGE = "views/assessments.py"
_REVIEW_PAGE = "views/human_review.py"
_PROJECTS_PAGE = "views/audit_projects.py"

FOOTER = (
    "AI-generated assessments require human verification. Nothing on this page is an "
    "audit conclusion or a statement of compliance."
)


# ---- small helpers
def _read(loader: Callable[[], Any], fallback: Any, what: str) -> Any:
    """Run one data-access read, reporting failure on the page instead of raising."""
    try:
        return loader()
    except data_access.DataAccessError as exc:
        components.error_with_remedy("Could not load {0}.".format(what), exc)
        return fallback


def _switch(page: str) -> None:
    """``st.switch_page`` that reports a missing sibling page instead of crashing.

    Only ``StreamlitAPIException`` (the "page not registered" case) is caught: the
    rerun Streamlit raises to perform the switch derives from ``BaseException`` and
    must pass through.
    """
    try:
        st.switch_page(page)
    except StreamlitAPIException:
        state.flash("That page is not installed in this build ({0}).".format(page), "warning")
        st.rerun()


def _greeting(name: str, now: Optional[datetime] = None) -> str:
    moment = now or datetime.now()
    if moment.hour < 12:
        salutation = "Good morning"
    elif moment.hour < 18:
        salutation = "Good afternoon"
    else:
        salutation = "Good evening"
    first = (name or "").strip().split()
    return "{0}, {1}".format(salutation, first[0] if first else "Auditor")


# ---- header and the two doors
def _render_header(provider: Dict[str, Any]) -> None:
    settings = get_settings()
    eyebrow = settings.app_short_name
    if provider.get("demo_mode"):
        eyebrow += " · Demo mode"
    components.section_header(
        _greeting(state.auditor_name()),
        subtitle="Review evidence, identify potential control issues, and document your decisions.",
        eyebrow=eyebrow,
    )


def _start_audit() -> None:
    """Open the "New audit" dialog owned by the Audit projects page.

    Imported lazily so this page does not depend on that module at import time; when
    the dialog is not present in this build the auditor is sent to the page instead.
    """
    try:
        from app.frontend.views.audit_projects import open_new_audit_dialog
    except (ImportError, AttributeError):
        open_new_audit_dialog = None  # type: ignore[assignment]
    if callable(open_new_audit_dialog):
        open_new_audit_dialog()
        return
    _switch(_PROJECTS_PAGE)


def _render_paths(first_run: Dict[str, Any]) -> None:
    left, right = st.columns(2, gap="medium")
    with left:
        with st.container(border=True):
            st.markdown("**Start a new audit**")
            st.caption("Upload evidence and assess it against the controls you choose.")
            if st.button("Start an audit", key="home_start_audit", type="primary", width="stretch"):
                _start_audit()
    with right:
        with st.container(border=True):
            st.markdown("**Try a guided demo**")
            st.caption("Explore a sample audit on synthetic evidence. No API key required.")
            demo_id = first_run.get("demo_project_id")
            demo_ready = False
            if demo_id is not None:
                stage = _read(lambda: data_access.project_stage(int(demo_id)), {}, "the demo audit")
                demo_ready = int(stage.get("evidence_files", 0) or 0) > 0
            if demo_ready:
                if st.button("Open the demo audit", key="home_open_demo", width="stretch"):
                    state.request_project_switch(int(demo_id))
                    _switch(_ASSESSMENTS_PAGE)
            else:
                components.load_demo_control(key="home_try_demo", label="Try the demo audit")


# ---- overview and next steps
def _render_overview(project_id: Optional[int], stage: Dict[str, Any]) -> None:
    components.section_header("Your audit overview")
    overview = _read(data_access.portfolio_overview, {}, "the audit overview")
    components.metric_row(
        [
            {
                "label": "Active audits",
                "value": int(overview.get("active_audits", 0) or 0),
                "caption": "Audit projects not yet completed or archived.",
            },
            {
                "label": "Awaiting review",
                "value": int(overview.get("awaiting_review", 0) or 0),
                "color": theme.AI_COLOR,
                "caption": "AI assessments with no auditor decision yet, across every audit.",
            },
        ],
        columns=2,
    )
    if project_id is None or not stage:
        return
    components.section_header("Current audit: {0}".format(state.current_project_name()))
    components.workflow_strip(stage, compact=False)


def _render_next_steps() -> None:
    components.section_header("What do you need to do?")
    components.next_steps(
        [
            ("Review findings", _REVIEW_PAGE, ":material/rate_review:"),
            ("View evidence", "views/evidence.py", ":material/inventory_2:"),
            ("Open audit reports", "views/reports.py", ":material/summarize:"),
            ("How it works", "views/how_it_works.py", ":material/school:"),
        ]
    )


# ---- the attention list
def _queue_reason(row: Dict[str, Any]) -> Dict[str, str]:
    """Why this assessment needs the auditor, in one line.

    Carried with the row rather than inferred from the badges, because "high-risk
    finding" and "cannot conclude - evidence insufficient" are different pieces of work
    and the list is useless if the reader has to work out which is which.
    """
    status = str(row.get("status", "") or "")
    risk = str(row.get("risk_level", "") or "")
    if status in _DEFICIENCY_VALUES and risk in _HIGH_RISK_VALUES:
        return {"text": "High-risk finding, no auditor decision recorded", "color": theme.risk_color(risk)}
    if status == AssessmentStatus.INSUFFICIENT_EVIDENCE.value:
        return {
            "text": "Cannot conclude - the evidence does not permit a judgement",
            "color": theme.status_color(status),
        }
    if status in _DEFICIENCY_VALUES:
        return {"text": "Finding awaiting your decision", "color": theme.status_color(status)}
    return {"text": "AI assessment awaiting your decision", "color": theme.AI_COLOR}


def _queue_sort_key(row: Dict[str, Any]) -> Any:
    status = str(row.get("status", "") or "")
    risk = RiskLevel.coerce(row.get("risk_level"), RiskLevel.NOT_RATED)
    return (
        -_STATUS_SEVERITY.get(status, 0),
        -RISK_ORDER.get(risk, 0),
        -float(row.get("risk_score", 0.0) or 0.0),
        str(row.get("control_ref", "")),
    )


def _queue_row(row: Dict[str, Any]) -> None:
    reason = _queue_reason(row)
    assessment_id = row.get("id")
    control_ref = str(row.get("control_ref", "") or "")

    detail, action = st.columns([6, 1], gap="small")
    with detail:
        st.markdown(
            '<div class="ia-quad-lead">{0}  {1}</div>'.format(
                components.escape(control_ref or "?"), components.escape(row.get("control_name", ""))
            ),
            unsafe_allow_html=True,
        )
        components.badges(
            components.status_badge(row.get("status")),
            components.risk_badge(row.get("risk_level"), row.get("risk_score")),
            components.plain_badge(reason["text"], reason["color"]),
        )
        finding = str(row.get("finding", "") or "").strip()
        if finding:
            st.markdown(
                '<div class="ia-note">{0}</div>'.format(components.escape(finding)),
                unsafe_allow_html=True,
            )
    with action:
        if st.button(
            "Review",
            key="home_review_{0}".format(assessment_id),
            width="stretch",
            help="Open this assessment and record your decision.",
        ):
            state.open_assessment(assessment_id, project_id=row.get("project_id"))


def _render_attention(project_id: int, pending: int) -> None:
    components.section_header(
        "Requires your attention",
        subtitle="Most severe first. Nothing here is settled until you record a decision.",
    )
    rows = _read(lambda: data_access.pending_reviews(project_id), [], "the review queue")
    if not rows:
        st.caption("Nothing is waiting for a decision in this audit.")
        return
    components.ai_disclaimer_banner(
        "Every row below is a machine-generated assessment queued for your review.",
        compact=True,
    )
    queued = sorted((dict(row) for row in rows), key=_queue_sort_key)
    for row in queued[:_QUEUE_LIMIT]:
        _queue_row(row)
    components.next_steps(
        [("Open the full review queue ({0})".format(max(pending, len(queued))), _REVIEW_PAGE, ":material/rate_review:")]
    )


# ---- figures: headline tiles
def _metrics(stats: Dict[str, Any]) -> None:
    assessed = int(stats.get("controls_assessed", 0) or 0)
    in_scope = int(stats.get("controls_in_scope", 0) or 0)
    potential = int(stats.get("potential_deficiencies", 0) or 0)
    not_effective = int(stats.get("not_effective", 0) or 0)
    definitions = dict(stats.get("definitions", {}) or {})

    components.metric_row(
        [
            {
                "label": "Controls in scope",
                "value": in_scope,
                "caption": "Library controls linked to this audit.",
                "help_text": "Distinct controls scoped to this audit project.",
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
                "label": components.label("status", AssessmentStatus.EFFECTIVE),
                "value": int(stats.get("effective", 0) or 0),
                "color": theme.status_color(AssessmentStatus.EFFECTIVE.value),
                "caption": "The AI concluded the control operated as required.",
                "help_text": "Not an audit conclusion until an auditor records one.",
            },
            {
                "label": "Deficiencies",
                "value": potential + not_effective,
                "delta": "of which {0} concluded not effective".format(not_effective),
                "color": theme.status_color(AssessmentStatus.POTENTIAL_DEFICIENCY.value),
                "caption": "Potential deficiencies plus controls concluded not effective.",
                "help_text": (
                    "Latest assessment per control with the outcome '{0}' or '{1}'.".format(
                        components.label("status", AssessmentStatus.POTENTIAL_DEFICIENCY),
                        components.label("status", AssessmentStatus.NOT_EFFECTIVE),
                    )
                ),
            },
            {
                "label": components.label("status", AssessmentStatus.INSUFFICIENT_EVIDENCE),
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
                "caption": "Deficiency rated {0} or {1} on the prototype model.".format(
                    components.label("risk", RiskLevel.HIGH), components.label("risk", RiskLevel.CRITICAL)
                ),
                "help_text": definitions.get("high_risk_findings", ""),
            },
            {
                "label": "Reviews pending",
                "value": int(stats.get("pending_human_reviews", 0) or 0),
                "color": theme.AI_COLOR,
                "caption": "No auditor decision recorded yet.",
                "help_text": definitions.get("pending_human_reviews", ""),
            },
            {
                "label": "Evidence files",
                "value": int(stats.get("evidence_files", 0) or 0),
                "caption": "Uploaded into this audit.",
            },
        ],
        columns=4,
    )
    components.note(
        "Insufficient evidence is counted separately from deficiencies throughout this "
        "console. A deficiency is a judgement that the control did not operate as "
        "required; insufficient evidence is the absence of a basis for any judgement. "
        + str(definitions.get("counting_basis", "") or "")
    )


# ---- figures: distribution charts
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


def _distributions(project_id: int) -> None:
    statuses = _read(lambda: data_access.status_breakdown(project_id), {}, "the outcome distribution")
    risks = _read(lambda: data_access.risk_breakdown(project_id), {}, "the risk distribution")

    left, right = st.columns(2, gap="medium")
    with left:
        components.section_header("Assessment outcomes", subtitle="Latest assessment per control.")
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
        components.section_header("Risk distribution", subtitle="Prototype research risk model.")
        order = sorted(
            data_access.risk_levels(),
            key=lambda value: RISK_ORDER.get(RiskLevel.coerce(value, RiskLevel.NOT_RATED), 0),
            reverse=True,
        )
        _bar_chart(
            [components.label("risk", level) for level in order],
            [int(risks.get(level, 0) or 0) for level in order],
            [theme.risk_color(level) for level in order],
            "control(s)",
        )
        st.caption(components.RISK_MODEL_NOTE)


# ---- figures: evidence coverage
def _coverage(project_id: int) -> None:
    components.section_header(
        "Evidence coverage",
        subtitle="Which in-scope controls have evidence the assessment could actually cite.",
    )
    controls = _read(lambda: data_access.list_scoped_controls(project_id), [], "the scoped controls")
    if not controls:
        components.empty_state(
            "No controls in scope",
            "Choose the controls for this audit on the Audit projects page before assessing anything.",
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
            {"label": "Evidence files", "value": len(files), "caption": "Uploaded into this audit."},
            {
                "label": "Controls with cited evidence",
                "value": len(covered),
                "color": theme.GREEN,
                "caption": "The assessment quoted at least one stored passage.",
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
            "This audit has no evidence yet. Every control in scope is untestable until "
            "evidence is uploaded on the Evidence page."
        )

    if uncited:
        st.markdown("**Assessed with no citation - evidence to obtain**")
        components.df_table(
            [
                {
                    "control_ref": item["control_ref"],
                    "control_name": item["control_name"],
                    "status": components.label("status", item["status"]),
                    "evidence_sufficiency": components.label("sufficiency", item["evidence_sufficiency"]),
                    "requested": "; ".join(item["missing_evidence"][:3]) or "(the model listed nothing)",
                }
                for item in uncited
            ],
            columns=["control_ref", "control_name", "status", "evidence_sufficiency", "requested"],
            column_config={
                "control_ref": st.column_config.TextColumn("Control", width="small"),
                "control_name": st.column_config.TextColumn("Name", width="medium"),
                "status": st.column_config.TextColumn("Outcome", width="small"),
                "evidence_sufficiency": st.column_config.TextColumn("Evidence", width="small"),
                "requested": st.column_config.TextColumn("Evidence the model asked for", width="large"),
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
        "audit, and which evidence bears on a control is decided by retrieval when the "
        "control is assessed. Coverage above is therefore what the pipeline managed to "
        "cite, and a control that has not been assessed is reported as unknown rather "
        "than as uncovered."
    )


# ---- figures: activity trail
def _activity(project_id: int) -> None:
    components.section_header(
        "Recent activity", subtitle="Append-only trail of who did what, newest first."
    )
    rows = _read(
        lambda: data_access.list_activity(project_id=project_id, limit=_ACTIVITY_LIMIT),
        [],
        "the activity trail",
    )
    if not rows:
        st.caption("Nothing has been recorded against this audit yet.")
        return
    components.df_table(
        [
            {
                "created_at": row.get("created_at"),
                "action": str(row.get("action", "")).replace("_", " ").title(),
                "entity": "{0} {1}".format(
                    str(row.get("entity_type", "")).replace("_", " ").title(), row.get("entity_id") or ""
                ).strip(),
                "actor": row.get("actor", ""),
                "actor_type": str(row.get("actor_type", "") or "").replace("_", " ").title(),
                "detail": _detail_summary(row.get("details")),
            }
            for row in rows
        ],
        columns=["created_at", "action", "entity", "actor", "actor_type", "detail"],
        column_config={
            "created_at": st.column_config.DatetimeColumn("When", format="D MMM YYYY HH:mm"),
            "action": st.column_config.TextColumn("Action", width="medium"),
            "entity": st.column_config.TextColumn("Record", width="small"),
            "actor": st.column_config.TextColumn("Who", width="medium"),
            "actor_type": st.column_config.TextColumn("AI / human", width="small"),
            "detail": st.column_config.TextColumn("Detail", width="large"),
        },
        height=320,
    )
    st.caption(
        "The actor is a declared name, not an authenticated identity: this prototype has no login."
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
        parts.append("{0}: {1}".format(str(key).replace("_", " "), rendered))
    if len(details) > limit:
        parts.append("+{0} more".format(len(details) - limit))
    return " · ".join(parts)


# ---- figures: definitions
def _definitions(stats: Dict[str, Any], provider: Dict[str, Any]) -> None:
    st.markdown("**How these figures are counted**")
    definitions = dict(stats.get("definitions", {}) or {})
    components.kv_grid({key.replace("_", " ").title(): value for key, value in definitions.items()})
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
            "Answering now": components.provider_display_name(provider.get("active_provider", "")),
        }
    )
    if provider.get("fell_back_to_mock"):
        st.error(
            "{0} was configured, but {1} is answering. {2}".format(
                components.provider_display_name(provider.get("configured_provider")),
                components.provider_display_name(provider.get("active_provider")),
                components.PROVIDER_MISMATCH_NOTE,
            )
        )


def _render_figures(project_id: Optional[int], provider: Dict[str, Any]) -> None:
    """Everything the old dashboard showed, behind one expander for those who want it."""
    with st.expander("Figures and activity for this audit", expanded=False):
        if project_id is None:
            st.caption(
                "Start an audit or open the demo audit above, and its figures, evidence "
                "coverage and activity trail will appear here."
            )
            return
        stats = _read(lambda: data_access.dashboard_stats(project_id), {}, "the headline figures")
        if not stats:
            return
        figures, coverage, activity, definitions = st.tabs(
            ["Figures", "Evidence coverage", "Activity", "Definitions"]
        )
        with figures:
            _metrics(stats)
            st.markdown("")
            _distributions(project_id)
        with coverage:
            _coverage(project_id)
        with activity:
            _activity(project_id)
        with definitions:
            _definitions(stats, provider)


# ---- page
def render() -> None:
    provider = _read(data_access.provider_badge, {}, "the provider status")
    first_run = data_access.first_run_state()
    if first_run.get("error"):
        st.error("Could not read the database: {0}".format(first_run["error"]))
    project_id = state.current_project_id()
    stage: Dict[str, Any] = {}
    if project_id is not None:
        stage = _read(lambda: data_access.project_stage(project_id), {}, "where this audit stands")

    _render_header(provider)
    _render_paths(first_run)

    st.markdown("")
    _render_overview(project_id, stage)

    st.markdown("")
    _render_next_steps()

    pending = int(stage.get("pending_reviews", 0) or 0)
    if project_id is not None and pending > 0:
        st.markdown("")
        _render_attention(project_id, pending)

    st.markdown("")
    _render_figures(project_id, provider)

    st.caption(FOOTER)


if __name__ == "__main__":
    render()
