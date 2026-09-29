"""Findings: every control whose latest assessment points to a problem, in one register.

A findings register is the working paper an audit is eventually judged on, so two
properties matter more than looks.

**One row per control.** The register is built from the *latest* assessment of each
control. Re-running a control, or running it under all three experimental conditions,
must not turn one deficiency into three entries in a report. The population is fetched
once, unfiltered, as the latest assessment per control, and every filter on this page
narrows that population rather than asking the database a different question - so a
control that was found deficient yesterday and effective today is gone from the
register, exactly as it is gone from the dashboard counts.

**The AI's conclusion and the auditor's conclusion sit in different columns and are
never merged.** A register that showed a single "status" would quietly promote a machine
proposal to an audit finding the moment nobody looked. Here an unreviewed row reads
"Awaiting decision" in the auditor column, and that is the honest state of it.

The register defaults to deficiencies (potential deficiency, not effective) because that
is what a finding is. "Insufficient evidence" can be folded in through the status filter:
it is not a deficiency - the system is saying it cannot tell - but it is an open item an
auditor has to clear, and leaving it off the page entirely would hide the outcome this
project exists to make expressible.
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

from typing import Any, Dict, List, Optional, Sequence  # noqa: E402

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from app.frontend import components, data_access, state, theme  # noqa: E402
from app.schemas.enums import (  # noqa: E402
    DEFICIENCY_STATUSES,
    RISK_ORDER,
    AssessmentStatus,
    HumanDecision,
)

#: Filter namespace for :mod:`app.frontend.state`. Widget keys use the ``find_`` prefix
#: from :data:`state.PROJECT_SCOPED_KEY_PREFIXES` so they reset on a project switch.
PAGE = "findings"

_DEFICIENCY_VALUES: List[str] = [status.value for status in DEFICIENCY_STATUSES]
_INSUFFICIENT = AssessmentStatus.INSUFFICIENT_EVIDENCE.value

#: The statuses a row may carry to appear on this page at all. Anything else (effective,
#: not applicable) is not a finding and not an open item.
_REGISTER_STATUSES: List[str] = _DEFICIENCY_VALUES + [_INSUFFICIENT]

_RISK_RANK: Dict[str, int] = {level.value: rank for level, rank in RISK_ORDER.items()}

_SORTS = (
    "Risk (highest first)",
    "Risk score (highest first)",
    "Control reference",
    "Most recently assessed",
)
_REVIEW_STATES = ("Any", "Awaiting review", "Reviewed")

_PENDING = HumanDecision.PENDING.value

# Widget keys. Every one starts with "find_" so a project switch pops them.
K_STATUS = "find_status"
K_RISK = "find_risk"
K_REVIEW = "find_review_state"
K_SORT = "find_sort"
K_CONTROLS = "find_controls"
K_RESET = "find_reset"
K_WIDEN = "find_widen_status"
K_TABLE = "find_table"
K_CSV = "find_csv"


# ---- navigation helpers
def _page_link(page: str, label_text: str, icon: str = "") -> None:
    """``st.page_link`` that degrades to a caption when the target is not registered.

    Under ``streamlit.testing`` and in a build that skips a sibling page the target is
    unknown to the navigation, and ``st.page_link`` raises rather than draws.
    """
    try:
        st.page_link(page, label=label_text, icon=icon or None)
    except Exception:  # noqa: BLE001 - navigation is a convenience, never a dependency
        st.caption("{0} (open it from the sidebar)".format(label_text))


def _open(assessment_id: int, project_id: Any) -> None:
    """Jump to one assessment, switching audit project if the row lives elsewhere.

    ``state.open_assessment`` ends in ``st.switch_page``, which raises a
    ``RerunException`` (a ``BaseException``) on success - it is not caught here. Only the
    ``StreamlitAPIException`` raised when the Assessments page is not registered is.
    """
    try:
        state.open_assessment(assessment_id, project_id=project_id)
    except Exception:  # noqa: BLE001 - see docstring
        st.warning("The Assessments page is not installed in this build.")


# ---- filter state (widget keys seeded from the page's remembered filters)
def _seed(key: str, name: str, default: Any) -> None:
    """Seed a widget key from the remembered filter before the widget is drawn.

    Streamlit drops a widget's value when the widget is not drawn in a run, so a filter
    would forget itself every time the auditor visited another page and came back. The
    remembered value lives in :mod:`app.frontend.state` under this page's namespace; it
    is written into the widget key only when the key is absent, which is also what
    keeps Streamlit from warning about a default and a session value disagreeing.
    """
    if key not in st.session_state:
        st.session_state[key] = state.get_filter(PAGE, name, default)


def _reset_filters() -> None:
    """``on_click`` for "Reset filters": runs before any widget exists in the new run."""
    state.clear_filters(PAGE)
    state.clear_project_scoped_keys(("find_",))


def _widen_statuses() -> None:
    """``on_click`` for the empty state's "Include insufficient evidence" button."""
    st.session_state[K_STATUS] = list(_REGISTER_STATUSES)
    state.set_filter(PAGE, "status", list(_REGISTER_STATUSES))


# ---- the population
def _risk_rank(row: Dict[str, Any]) -> int:
    return _RISK_RANK.get(str(row.get("risk_level", "")).upper(), 0)


def _is_reviewed(row: Dict[str, Any]) -> bool:
    if row.get("is_reviewed"):
        return True
    decision = str(row.get("review_decision", "") or "").upper()
    return bool(decision) and decision != _PENDING


def _load_population(project_id: int) -> List[Dict[str, Any]]:
    """The latest assessment per control that points to a problem.

    One unfiltered fetch (latest per control across every status), deduplicated by
    control as a belt-and-braces measure, then narrowed to the register's vocabulary.
    Effective controls are dropped here because they are not open items; nothing else is
    dropped, so the tiles can count deficiencies and insufficient evidence separately.
    """
    rows = data_access.list_assessments(project_id=project_id, latest_per_control=True)
    latest: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        ref = str(row.get("control_ref") or row.get("control_id") or row.get("id"))
        current = latest.get(ref)
        if current is None or _sort_stamp(row) > _sort_stamp(current):
            latest[ref] = dict(row)
    return [
        row
        for row in latest.values()
        if str(row.get("status", "")).upper() in _REGISTER_STATUSES
    ]


def _sort_stamp(row: Dict[str, Any]) -> Any:
    return (str(row.get("created_at", "") or ""), int(row.get("id", 0) or 0))


def _apply_filters(
    rows: Sequence[Dict[str, Any]],
    statuses: Sequence[str],
    risks: Sequence[str],
    controls: Sequence[str],
    review_state: str,
    sort_by: str,
) -> List[Dict[str, Any]]:
    out = list(rows)
    if statuses:
        out = [row for row in out if str(row.get("status", "")).upper() in statuses]
    if risks:
        out = [row for row in out if str(row.get("risk_level", "")).upper() in risks]
    if controls:
        out = [row for row in out if row.get("control_ref") in controls]
    if review_state == "Awaiting review":
        out = [row for row in out if not _is_reviewed(row)]
    elif review_state == "Reviewed":
        out = [row for row in out if _is_reviewed(row)]

    if sort_by == "Risk (highest first)":
        out.sort(
            key=lambda row: (_risk_rank(row), float(row.get("risk_score", 0.0) or 0.0)),
            reverse=True,
        )
    elif sort_by == "Risk score (highest first)":
        out.sort(key=lambda row: float(row.get("risk_score", 0.0) or 0.0), reverse=True)
    elif sort_by == "Control reference":
        out.sort(key=lambda row: str(row.get("control_ref", "")))
    else:
        out.sort(key=_sort_stamp, reverse=True)
    return out


# ---- wording
def _auditor_conclusion(row: Dict[str, Any]) -> str:
    """The auditor column, in words: "Accepted · Not effective" or "Awaiting decision"."""
    if not _is_reviewed(row):
        return components.label("decision", _PENDING)
    decision = components.label("decision", row.get("review_decision"))
    final = components.label("status", row.get("final_status"))
    return "{0} · {1}".format(decision, final) if final else decision


def _expander_title(row: Dict[str, Any]) -> str:
    if _is_reviewed(row):
        lead = "Reviewed: {0}".format(components.label("decision", row.get("review_decision")))
    else:
        lead = "Awaiting review"
    parts = [
        lead,
        str(row.get("control_ref", "") or ""),
        str(row.get("control_name", "") or ""),
        components.label("status", row.get("status")),
        components.label("risk", row.get("risk_level")),
    ]
    return " · ".join(part for part in parts if part)


def _register_frame(rows: Sequence[Dict[str, Any]]) -> pd.DataFrame:
    """The register as a flat frame.

    Friendly columns (``ai_conclusion``, ``auditor_conclusion``, ``risk``) are what the
    table shows; the raw ``status``, ``final_status``, ``review_decision`` and
    ``risk_level`` tokens travel alongside so the CSV carries both statuses and a
    reader of the export can always tell an AI proposal from an auditor conclusion.
    """
    return pd.DataFrame(
        [
            {
                "id": row.get("id"),
                "control_ref": row.get("control_ref", ""),
                "control_name": row.get("control_name", ""),
                "ai_conclusion": components.label("status", row.get("status")),
                "auditor_conclusion": _auditor_conclusion(row),
                "risk": components.label("risk", row.get("risk_level")),
                "risk_score": float(row.get("risk_score", 0.0) or 0.0),
                "citation_count": int(row.get("citation_count", 0) or 0),
                "fabricated_citations": int(row.get("fabricated_citations", 0) or 0),
                "created_at": row.get("created_at"),
                "status": row.get("status", ""),
                "final_status": row.get("final_status", "") or "",
                "review_decision": row.get("review_decision", "") or _PENDING,
                "agreed_with_ai_status": row.get("agreed_with_ai_status"),
                "risk_level": row.get("risk_level", ""),
                "final_risk_level": row.get("final_risk_level", "") or "",
                "confidence": row.get("confidence", ""),
                "evidence_sufficiency": row.get("evidence_sufficiency", ""),
                "verified_citations": int(row.get("verified_citations", 0) or 0),
                "experiment_mode": row.get("experiment_mode", ""),
                "finding": row.get("finding", ""),
                "recommendation": row.get("recommendation", ""),
            }
            for row in rows
        ]
    )


# ---- sections
def _render_tiles(population: Sequence[Dict[str, Any]]) -> None:
    deficiencies = [row for row in population if str(row.get("status", "")).upper() in _DEFICIENCY_VALUES]
    insufficient = [row for row in population if str(row.get("status", "")).upper() == _INSUFFICIENT]
    high_risk = [row for row in deficiencies if _risk_rank(row) >= _RISK_RANK["HIGH"]]
    unreviewed = [row for row in population if not _is_reviewed(row)]
    fabricated = [row for row in population if int(row.get("fabricated_citations", 0) or 0) > 0]

    components.metric_row(
        [
            {
                "label": "Deficiencies",
                "value": len(deficiencies),
                "color": theme.status_color("POTENTIAL_DEFICIENCY"),
                "caption": "Latest assessment per control, this audit project only.",
            },
            {
                "label": "High or critical",
                "value": len(high_risk),
                "color": theme.risk_color("HIGH"),
                "caption": "Prototype risk model, not an industry framework.",
            },
            {
                "label": "Insufficient evidence",
                "value": len(insufficient),
                "color": theme.status_color(_INSUFFICIENT),
                "caption": "Not deficiencies - controls the AI could not conclude on.",
            },
            {
                "label": "Awaiting your decision",
                "value": len(unreviewed),
                "color": theme.AI_COLOR,
                "caption": "AI proposals - not audit findings until you review them.",
            },
            {
                "label": "With a fabricated citation",
                "value": len(fabricated),
                "color": theme.verdict_color("FABRICATED"),
                "caption": "Check these by hand before relying on them.",
            },
        ]
    )


def _render_filters(population: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Draw the filter row and return the chosen values. Widget keys are ``find_*``."""
    _seed(K_STATUS, "status", list(_DEFICIENCY_VALUES))
    _seed(K_RISK, "risk", [])
    _seed(K_REVIEW, "review_state", _REVIEW_STATES[0])
    _seed(K_SORT, "sort", _SORTS[0])
    _seed(K_CONTROLS, "controls", [])

    # Values remembered from an earlier visit may name options that no longer exist.
    control_options = sorted(
        {str(row.get("control_ref", "")) for row in population if row.get("control_ref")}
    )
    st.session_state[K_STATUS] = [
        value for value in (st.session_state.get(K_STATUS) or []) if value in _REGISTER_STATUSES
    ]
    st.session_state[K_RISK] = [
        value for value in (st.session_state.get(K_RISK) or []) if value in data_access.risk_levels()
    ]
    st.session_state[K_CONTROLS] = [
        value for value in (st.session_state.get(K_CONTROLS) or []) if value in control_options
    ]
    if st.session_state.get(K_REVIEW) not in _REVIEW_STATES:
        st.session_state[K_REVIEW] = _REVIEW_STATES[0]
    if st.session_state.get(K_SORT) not in _SORTS:
        st.session_state[K_SORT] = _SORTS[0]

    row = st.columns([3, 2, 2, 2, 3, 1], gap="small", vertical_alignment="bottom")
    with row[0]:
        statuses = st.multiselect(
            "Status",
            options=list(_REGISTER_STATUSES),
            format_func=lambda value: components.label("status", value),
            key=K_STATUS,
            help=(
                "Findings are deficiencies. 'Insufficient evidence' is not a deficiency - "
                "it means the AI could not tell from the evidence supplied - but it is an "
                "open item you have to clear, so you can add it here."
            ),
        )
    with row[1]:
        risks = st.multiselect(
            "Risk",
            options=data_access.risk_levels(),
            format_func=lambda value: components.label("risk", value),
            key=K_RISK,
        )
    with row[2]:
        review_state = st.selectbox("Review state", options=list(_REVIEW_STATES), key=K_REVIEW)
    with row[3]:
        sort_by = st.selectbox("Sort by", options=list(_SORTS), key=K_SORT)
    with row[4]:
        controls = st.multiselect("Controls", options=control_options, key=K_CONTROLS)
    with row[5]:
        st.button(
            "Reset filters",
            key=K_RESET,
            on_click=_reset_filters,
            help="Back to deficiencies only, every risk, every control.",
            width="stretch",
        )

    state.set_filter(PAGE, "status", list(statuses))
    state.set_filter(PAGE, "risk", list(risks))
    state.set_filter(PAGE, "review_state", review_state)
    state.set_filter(PAGE, "sort", sort_by)
    state.set_filter(PAGE, "controls", list(controls))
    return {
        "statuses": [str(value).upper() for value in statuses],
        "risks": [str(value).upper() for value in risks],
        "review_state": review_state,
        "sort_by": sort_by,
        "controls": list(controls),
    }


def _render_register(rows: Sequence[Dict[str, Any]], project_id: int) -> None:
    components.ai_disclaimer_banner(
        "Every row is a machine-generated proposal until you record a decision on it. "
        "'Auditor conclusion' is the only column that carries an audit conclusion.",
        compact=True,
    )
    frame = _register_frame(rows)
    components.df_table(
        frame,
        columns=[
            "control_ref",
            "control_name",
            "ai_conclusion",
            "auditor_conclusion",
            "risk",
            "risk_score",
            "citation_count",
            "fabricated_citations",
            "created_at",
        ],
        column_config={
            "ai_conclusion": st.column_config.TextColumn("AI conclusion", width="medium"),
            "auditor_conclusion": st.column_config.TextColumn("Auditor conclusion", width="medium"),
            "risk": st.column_config.TextColumn("Risk", width="small"),
            "created_at": st.column_config.DatetimeColumn(
                "Assessed", width="medium", format="YYYY-MM-DD HH:mm"
            ),
        },
        key=K_TABLE,
    )
    components.download_row(
        "Download the register (CSV)",
        frame.to_csv(index=False),
        "findings_project_{0}.csv".format(project_id),
        mime="text/csv",
        key=K_CSV,
    )
    components.note(
        "The CSV carries the AI status, the auditor's final status and the review "
        "decision as separate columns, so a reader of the export can always tell an AI "
        "proposal from an auditor conclusion."
    )


def _render_citations(assessment_id: int) -> None:
    """The cited evidence for one finding, loaded only when the toggle is on.

    Drawn without :func:`components.citation_card` because that card opens an expander
    of its own and this section already sits inside one; the source passage is offered
    through a popover instead.
    """
    try:
        detail = data_access.get_assessment(assessment_id)
    except data_access.DataAccessError as exc:
        components.error_with_remedy("Could not load the citations.", exc)
        return
    citations = list((detail or {}).get("citations") or [])
    if not citations:
        st.caption("This finding cites no evidence. Nothing in it is traceable to a source.")
        return
    for position, citation in enumerate(citations, start=1):
        data = dict(citation)
        verdict = str(getattr(data.get("verdict"), "value", data.get("verdict")) or "UNVERIFIED")
        locator = str(data.get("locator_text") or data.get("locator") or "")
        quote = str(data.get("quoted_text") or "")
        head = [
            '<span class="ia-cite-file">[{0}] {1}</span>'.format(
                position, components.escape(data.get("filename") or "(no filename recorded)")
            )
        ]
        if locator:
            head.append('<span class="ia-cite-loc">{0}</span>'.format(components.escape(locator)))
        head.append(components.verdict_badge(verdict, data.get("match_score")))
        quote_html = (
            '<div class="ia-quote">{0}</div>'.format(components.escape(quote))
            if quote
            else '<div class="ia-quote" style="font-style:italic;">No quotation was supplied.</div>'
        )
        st.markdown(
            '<div class="ia-cite" style="border-left-color:{edge};">'
            '<div class="ia-cite-head">{head}</div>{quote}</div>'.format(
                edge=theme.verdict_color(verdict), head="".join(head), quote=quote_html
            ),
            unsafe_allow_html=True,
        )
        chunk_id = data.get("chunk_id")
        chunk_text = str(data.get("chunk_text") or "")
        if chunk_id is None and not chunk_text:
            continue
        with st.popover("Source passage {0}".format(position)):
            if not chunk_text:
                st.info(
                    "This citation does not resolve to a stored evidence chunk. That is "
                    "itself the finding: there is nothing in the evidence store to check "
                    "it against."
                )
            else:
                st.markdown(
                    '<div class="ia-chunk ia-chunk-focus">{0}</div>'.format(
                        components.escape(chunk_text)
                    ),
                    unsafe_allow_html=True,
                )


def _render_row_detail(row: Dict[str, Any]) -> None:
    """One finding opened up: the text, the risk reasoning, and its evidence on request."""
    assessment_id = int(row.get("id"))
    components.ai_disclaimer_banner(
        "The finding and recommendation below are machine-generated proposals.",
        mode=row.get("experiment_mode"),
        compact=True,
    )
    components.badges(
        components.status_badge(row.get("status")),
        components.risk_badge(row.get("risk_level"), row.get("risk_score")),
        components.sufficiency_badge(row.get("evidence_sufficiency")),
        components.confidence_badge(row.get("confidence")),
        components.decision_badge(row.get("review_decision") or _PENDING),
    )

    finding = str(row.get("finding") or "").strip()
    st.markdown("**What the AI found**")
    st.markdown(
        '<div class="ia-chunk">{0}</div>'.format(
            components.escape(finding or "The model recorded no finding text.")
        ),
        unsafe_allow_html=True,
    )
    components.kv_grid(
        {
            "Risk statement (AI)": row.get("risk", ""),
            "Recommendation (AI)": row.get("recommendation", ""),
            "Auditor conclusion": _auditor_conclusion(row),
            "Auditor risk": components.label("risk", row.get("final_risk_level")) or "-",
        }
    )

    missing = list(row.get("missing_evidence", []) or [])
    if missing:
        st.markdown("**Evidence the model says it did not have**")
        for item in missing:
            st.markdown("- {0}".format(components.escape(item)), unsafe_allow_html=True)

    validation = dict(row.get("validation") or {})
    fabricated = int(validation.get("fabricated", row.get("fabricated_citations", 0)) or 0)
    st.caption(
        "Citations: {0} total, {1} verified, {2} fabricated. Strict grounding {3:.0%}.".format(
            validation.get("total", row.get("citation_count", 0)),
            validation.get("verified", row.get("verified_citations", 0)),
            fabricated,
            float(validation.get("grounding_rate_strict", 0.0) or 0.0),
        )
    )
    if fabricated:
        st.error(
            "At least one citation is fabricated - the quoted text is not in the evidence. "
            "Treat this finding as unreliable until you have checked every citation."
        )

    rationale = str((row.get("risk_factors") or {}).get("rationale") or "")
    action_cols = st.columns([2, 2, 2, 3], gap="small", vertical_alignment="center")
    with action_cols[0]:
        if st.button(
            "Review this finding",
            key="find_review_{0}".format(assessment_id),
            type="primary",
            width="stretch",
            help="Opens the assessment with the decision form: Accept, Reject, Modify or More evidence.",
        ):
            _open(assessment_id, row.get("project_id"))
    with action_cols[1]:
        if st.button(
            "Open the full assessment",
            key="find_open_{0}".format(assessment_id),
            width="stretch",
        ):
            _open(assessment_id, row.get("project_id"))
    with action_cols[2]:
        if rationale:
            with st.popover("Why this risk rating?", width="stretch"):
                st.text(rationale)
                components.note(components.RISK_MODEL_NOTE)
    with action_cols[3]:
        show_evidence = st.toggle(
            "Show cited evidence",
            key="find_ev_{0}".format(assessment_id),
            help="Loads the citations for this finding only.",
        )
    if show_evidence:
        _render_citations(assessment_id)


def _render_details(rows: Sequence[Dict[str, Any]]) -> None:
    components.section_header(
        "Findings in detail",
        subtitle="Unreviewed findings first. Each entry with its risk reasoning and, on request, the evidence it cites.",
    )
    ordered = [row for row in rows if not _is_reviewed(row)] + [row for row in rows if _is_reviewed(row)]
    for row in ordered:
        with st.expander(_expander_title(row), expanded=False):
            _render_row_detail(row)


def _render_next_steps(stage: Dict[str, Any]) -> None:
    pending = int(stage.get("pending_reviews", 0) or 0)
    st.markdown("")
    st.caption("Next")
    components.next_steps(
        [
            (
                "Review queue ({0} awaiting)".format(pending),
                "views/human_review.py",
                ":material/rate_review:",
            ),
            ("Generate the report", "views/reports.py", ":material/summarize:"),
        ],
        primary_index=0 if pending else 1,
    )


# ---- the page
def render() -> None:
    components.section_header(
        "Findings",
        subtitle=(
            "Controls whose latest assessment points to a problem, with the auditor's "
            "conclusion beside the AI's."
        ),
        eyebrow="Assessment",
    )
    project_id = state.current_project_id()
    if project_id is None:
        components.empty_state(
            "No audit project selected",
            "Choose an audit project in the sidebar. The findings register is always the "
            "register of one audit project.",
        )
        return

    try:
        stage = data_access.project_stage(project_id)
        population = _load_population(project_id)
    except data_access.DataAccessError as exc:
        components.error_with_remedy("Could not read the findings.", exc, retry_key="find_retry")
        return

    if not population:
        if int(stage.get("controls_assessed", 0) or 0) == 0:
            components.empty_state(
                "No control has been assessed yet",
                "Findings appear here once the AI assessment has run. Next: open "
                "Assessments and press Run assessment.",
            )
            _page_link("views/assessments.py", "Go to Assessments", ":material/fact_check:")
        else:
            components.empty_state(
                "Every assessed control is effective",
                "The latest assessment of every control in this audit project concluded "
                "'Effective', so there is nothing to register. Next: record your decisions "
                "in the review queue, then generate the report.",
            )
            _render_next_steps(stage)
        return

    _render_tiles(population)
    filters = _render_filters(population)
    rows = _apply_filters(
        population,
        filters["statuses"],
        filters["risks"],
        filters["controls"],
        filters["review_state"],
        filters["sort_by"],
    )

    if len(rows) != len(population):
        st.caption("Showing {0} of {1} open items.".format(len(rows), len(population)))

    if not rows:
        hidden_insufficient = [
            row for row in population if str(row.get("status", "")).upper() == _INSUFFICIENT
        ]
        if hidden_insufficient and _INSUFFICIENT not in filters["statuses"]:
            components.empty_state(
                "Every assessed control is effective or lacks evidence",
                "No control was found deficient. {0} could not be concluded on because the "
                "evidence was insufficient - not a deficiency, but an open item to clear "
                "with the client.".format(
                    "{0} control{1}".format(
                        len(hidden_insufficient), "" if len(hidden_insufficient) == 1 else "s"
                    )
                ),
            )
            _left, middle, _right = st.columns([1, 1, 1])
            with middle:
                st.button(
                    "Include insufficient evidence",
                    key=K_WIDEN,
                    on_click=_widen_statuses,
                    type="primary",
                    width="stretch",
                )
        else:
            components.empty_state(
                "No finding matches these filters",
                "Widen the status, risk or review-state filters, or press Reset filters.",
            )
        _render_next_steps(stage)
        return

    _render_register(rows, project_id)
    st.markdown("")
    _render_details(rows)
    _render_next_steps(stage)


if __name__ == "__main__":
    render()
