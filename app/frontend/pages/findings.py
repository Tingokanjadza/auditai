"""The findings register: every open adverse conclusion in one place.

A findings register is the working paper an audit is eventually judged on, so two
properties matter more than looks.

**One row per control.** The register is built from the *latest* assessment of each
control (``app.audit.service.list_findings``). Re-running a control, or running it under
all three experimental conditions, must not turn one deficiency into three entries in a
report.

**The AI's conclusion and the auditor's conclusion sit in different columns and are
never merged.** A register that showed a single "status" would quietly promote a machine
proposal to an audit finding the moment nobody looked. Here an unreviewed row reads
"awaiting review" in the auditor column, and that is the honest state of it.

The register defaults to deficiencies (POTENTIAL_DEFICIENCY, NOT_EFFECTIVE) because that
is what a finding is. INSUFFICIENT_EVIDENCE can be folded in on request: it is not a
deficiency - the system is saying it cannot tell - but it is an open item an auditor has
to clear, and leaving it off the page entirely would hide the outcome this project
exists to make expressible.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from app.frontend import components, data_access, state, theme

PAGE = "findings"

#: Mirrors ``app.schemas.enums.RISK_ORDER``. Held locally because the data-access facade
#: exposes the risk vocabulary but not its ordering, and a page may not import the enum
#: module directly; if the facade grows an ordering helper, this should defer to it.
_RISK_RANK: Dict[str, int] = {
    "NOT_RATED": 0,
    "LOW": 1,
    "MEDIUM": 2,
    "HIGH": 3,
    "CRITICAL": 4,
}

_DEFICIENCY_STATUSES = ("POTENTIAL_DEFICIENCY", "NOT_EFFECTIVE")

_SORTS = (
    "Risk (highest first)",
    "Risk score (highest first)",
    "Control reference",
    "Most recently assessed",
)

#: Session key holding the finding ids whose cited evidence the user asked to see. The
#: citations are not loaded with the register: fetching every citation for every row
#: would make an unopened page pay for detail nobody asked for.
_EVIDENCE_KEY = "findings_evidence_open"


def _go(path: str) -> None:
    """Navigate to another page, reporting rather than raising when it is absent.

    ``st.switch_page`` raises when its target is not a registered page, and the shell
    deliberately skips page modules that are not installed.
    """
    try:
        st.switch_page(path)
    except Exception:  # noqa: BLE001 - navigation is a convenience, never a dependency
        st.warning("That page is not installed in this build: {0}".format(path))


def _selected_project() -> Optional[int]:
    project_id = state.current_project_id()
    if project_id is not None:
        return project_id
    components.empty_state(
        "No audit project selected",
        "Choose an engagement in the sidebar. The findings register is always the "
        "register of one engagement.",
    )
    return None


def _risk_rank(row: Dict[str, Any]) -> int:
    return _RISK_RANK.get(str(row.get("risk_level", "")).upper(), 0)


def _load(project_id: int, include_insufficient: bool) -> List[Dict[str, Any]]:
    """The register population: deficiencies, optionally with unresolvable controls."""
    rows = list(data_access.list_findings(project_id=project_id))
    if include_insufficient:
        seen = {int(row["id"]) for row in rows}
        extra = data_access.list_assessments(
            project_id=project_id,
            status="INSUFFICIENT_EVIDENCE",
            latest_per_control=True,
        )
        rows.extend(row for row in extra if int(row["id"]) not in seen)
    return rows


def _apply_filters(
    rows: List[Dict[str, Any]],
    statuses: List[str],
    risks: List[str],
    controls: List[str],
    review_state: str,
    sort_by: str,
) -> List[Dict[str, Any]]:
    out = list(rows)
    if statuses:
        out = [row for row in out if row.get("status") in statuses]
    if risks:
        out = [row for row in out if row.get("risk_level") in risks]
    if controls:
        out = [row for row in out if row.get("control_ref") in controls]
    if review_state == "Awaiting review":
        out = [row for row in out if not row.get("is_reviewed")]
    elif review_state == "Reviewed":
        out = [row for row in out if row.get("is_reviewed")]

    if sort_by == "Risk (highest first)":
        out.sort(key=lambda row: (_risk_rank(row), float(row.get("risk_score", 0.0) or 0.0)), reverse=True)
    elif sort_by == "Risk score (highest first)":
        out.sort(key=lambda row: float(row.get("risk_score", 0.0) or 0.0), reverse=True)
    elif sort_by == "Control reference":
        out.sort(key=lambda row: str(row.get("control_ref", "")))
    else:
        out.sort(key=lambda row: str(row.get("created_at", "")), reverse=True)
    return out


def _register_frame(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    """The register as a flat frame - what the table shows and what the CSV exports."""
    return pd.DataFrame(
        [
            {
                "id": row.get("id"),
                "control_ref": row.get("control_ref", ""),
                "control_name": row.get("control_name", ""),
                "status": row.get("status", ""),
                "final_status": row.get("final_status", "") or "awaiting review",
                "decision": row.get("review_decision", ""),
                "agreed_with_ai_status": row.get("agreed_with_ai_status"),
                "risk_level": row.get("risk_level", ""),
                "risk_score": float(row.get("risk_score", 0.0) or 0.0),
                "confidence": row.get("confidence", ""),
                "evidence_sufficiency": row.get("evidence_sufficiency", ""),
                "citation_count": int(row.get("citation_count", 0) or 0),
                "verified_citations": int(row.get("verified_citations", 0) or 0),
                "fabricated_citations": int(row.get("fabricated_citations", 0) or 0),
                "experiment_mode": row.get("experiment_mode", ""),
                "finding": row.get("finding", ""),
                "recommendation": row.get("recommendation", ""),
                "created_at": row.get("created_at"),
            }
            for row in rows
        ]
    )


def _render_summary(rows: List[Dict[str, Any]]) -> None:
    high_risk = [row for row in rows if _risk_rank(row) >= _RISK_RANK["HIGH"]]
    unreviewed = [row for row in rows if not row.get("is_reviewed")]
    fabricated = [row for row in rows if int(row.get("fabricated_citations", 0) or 0) > 0]
    uncited = [row for row in rows if int(row.get("citation_count", 0) or 0) == 0]

    components.metric_row(
        [
            {
                "label": "Findings listed",
                "value": len(rows),
                "caption": "Latest assessment per control, this project only.",
            },
            {
                "label": "High or critical",
                "value": len(high_risk),
                "color": theme.risk_color("HIGH"),
                "caption": "Prototype risk model, not an industry framework.",
            },
            {
                "label": "Awaiting auditor review",
                "value": len(unreviewed),
                "color": theme.AI_COLOR,
                "caption": "Not yet audit findings - AI proposals.",
            },
            {
                "label": "With a fabricated citation",
                "value": len(fabricated),
                "color": theme.verdict_color("FABRICATED"),
                "caption": "Check these by hand before relying on them.",
            },
            {
                "label": "With no citation at all",
                "value": len(uncited),
                "color": theme.AMBER,
                "caption": "Nothing in them is traceable to a source.",
            },
        ]
    )


def _render_row_detail(row: Dict[str, Any]) -> None:
    """One finding opened up: the text, the risk reasoning, and its evidence on request."""
    assessment_id = int(row.get("id"))
    components.badges(
        components.status_badge(row.get("status")),
        components.risk_badge(row.get("risk_level"), row.get("risk_score")),
        components.sufficiency_badge(row.get("evidence_sufficiency")),
        components.confidence_badge(row.get("confidence")),
        components.mode_badge(row.get("experiment_mode")),
        components.decision_badge(row.get("review_decision")),
    )
    components.ai_disclaimer_banner(
        "The finding and recommendation below are machine-generated proposals.",
        mode=row.get("experiment_mode"),
        compact=True,
    )
    components.kv_grid(
        {
            "Finding (AI)": row.get("finding", ""),
            "Risk statement (AI)": row.get("risk", ""),
            "Recommendation (AI)": row.get("recommendation", ""),
            "Auditor status": row.get("final_status", "") or "no auditor decision recorded",
            "Auditor risk": row.get("final_risk_level", "") or "-",
        }
    )

    missing = list(row.get("missing_evidence", []) or [])
    if missing:
        st.markdown("**Evidence the model says it did not have**")
        for item in missing:
            st.markdown("- {0}".format(item))

    rationale = str((row.get("risk_factors") or {}).get("rationale") or "")
    if rationale:
        with st.expander("Risk rationale (prototype model)", expanded=False):
            st.text(rationale)
        components.note(components.RISK_MODEL_NOTE)

    validation = dict(row.get("validation") or {})
    st.caption(
        "Citations: {0} total, {1} verified, {2} fabricated. Strict grounding {3:.0%}.".format(
            validation.get("total", 0),
            validation.get("verified", 0),
            validation.get("fabricated", 0),
            float(validation.get("grounding_rate_strict", 0.0) or 0.0),
        )
    )

    opened = set(st.session_state.get(_EVIDENCE_KEY, set()))
    button_col, link_col = st.columns([1, 2], gap="small")
    with button_col:
        if assessment_id not in opened:
            if st.button(
                "Show cited evidence",
                key="find_ev_{0}".format(assessment_id),
                help="Loads the citations for this finding only.",
            ):
                opened.add(assessment_id)
                st.session_state[_EVIDENCE_KEY] = opened
                st.rerun()
    with link_col:
        if st.button(
            "Open the full assessment",
            key="find_open_{0}".format(assessment_id),
            type="primary",
        ):
            state.set_current_assessment(assessment_id)
            _go("pages/assessments.py")

    if assessment_id in opened:
        try:
            detail = data_access.get_assessment(assessment_id)
        except data_access.DataAccessError as exc:
            st.error("Could not load the citations: {0}".format(exc))
            return
        citations = list((detail or {}).get("citations") or [])
        if not citations:
            st.caption("This finding cites no evidence.")
            return
        for position, citation in enumerate(citations, start=1):
            components.citation_card(
                citation, index=position, context_loader=data_access.chunk_context
            )


def render() -> None:
    components.section_header(
        "Findings register",
        subtitle="Open adverse conclusions for this engagement, AI proposal beside auditor conclusion.",
        eyebrow="Assessment",
    )
    project_id = _selected_project()
    if project_id is None:
        return

    controls_row = st.columns([2, 2, 2, 2, 2], gap="small")
    with controls_row[0]:
        statuses = st.multiselect(
            "Status",
            options=list(_DEFICIENCY_STATUSES) + ["INSUFFICIENT_EVIDENCE"],
            default=state.get_filter(PAGE, "status", []),
            key="find_status",
        )
    with controls_row[1]:
        risks = st.multiselect(
            "Risk level",
            options=data_access.risk_levels(),
            default=state.get_filter(PAGE, "risk", []),
            key="find_risk",
        )
    with controls_row[2]:
        review_state = st.selectbox(
            "Review state",
            options=["Any", "Awaiting review", "Reviewed"],
            index=["Any", "Awaiting review", "Reviewed"].index(
                state.get_filter(PAGE, "review_state", "Any")
            ),
            key="find_review",
        )
    with controls_row[3]:
        sort_by = st.selectbox(
            "Sort by",
            options=list(_SORTS),
            index=list(_SORTS).index(state.get_filter(PAGE, "sort", _SORTS[0])),
            key="find_sort",
        )
    with controls_row[4]:
        include_insufficient = st.checkbox(
            "Include INSUFFICIENT_EVIDENCE",
            value=bool(state.get_filter(PAGE, "include_insufficient", False)),
            key="find_insufficient",
            help=(
                "Not deficiencies - the system reporting that it cannot tell from the "
                "evidence supplied. Open items for the auditor all the same."
            ),
        )

    try:
        population = _load(project_id, include_insufficient)
    except data_access.DataAccessError as exc:
        st.error("Could not read the findings: {0}".format(exc))
        return

    control_options = sorted({str(row.get("control_ref", "")) for row in population if row.get("control_ref")})
    controls = st.multiselect(
        "Controls",
        options=control_options,
        default=[
            ref for ref in state.get_filter(PAGE, "controls", []) if ref in control_options
        ],
        key="find_controls",
    )

    state.set_filter(PAGE, "status", statuses)
    state.set_filter(PAGE, "risk", risks)
    state.set_filter(PAGE, "review_state", review_state)
    state.set_filter(PAGE, "sort", sort_by)
    state.set_filter(PAGE, "include_insufficient", include_insufficient)
    state.set_filter(PAGE, "controls", controls)

    if not population:
        if components.empty_state(
            "No findings recorded for this engagement",
            "Either no control has been assessed yet, or every assessed control came out "
            "EFFECTIVE. Run an assessment to populate the register.",
            action_label="Go to Assessments",
            action_key="find_go_assess",
        ):
            _go("pages/assessments.py")
        return

    rows = _apply_filters(population, statuses, risks, controls, review_state, sort_by)
    _render_summary(rows)

    if not rows:
        st.caption("No finding matches these filters.")
        return

    components.ai_disclaimer_banner(
        "Every row below is a machine-generated proposal until an auditor has recorded a "
        "decision on it. The 'Auditor status' column is the only column that carries an "
        "audit conclusion."
    )
    frame = _register_frame(rows)
    components.df_table(
        frame,
        columns=[
            "id",
            "control_ref",
            "control_name",
            "status",
            "final_status",
            "decision",
            "risk_level",
            "risk_score",
            "evidence_sufficiency",
            "citation_count",
            "fabricated_citations",
            "experiment_mode",
            "created_at",
        ],
        column_config={
            "decision": st.column_config.TextColumn("Review decision", width="medium"),
        },
        key="find_table",
    )
    components.download_row(
        "Download the register (CSV)",
        frame.to_csv(index=False),
        "findings_project_{0}.csv".format(project_id),
        mime="text/csv",
        key="find_csv",
    )
    components.note(
        "The CSV carries both statuses and the review decision, so a reader of the export "
        "can always tell an AI proposal from an auditor conclusion."
    )

    st.markdown("")
    components.section_header(
        "Findings in detail",
        subtitle="Each entry with its risk reasoning and, on request, the evidence it cites.",
    )
    for row in rows:
        title = "{0} · {1} · {2} · risk {3}".format(
            row.get("control_ref", ""),
            row.get("control_name", ""),
            row.get("status", ""),
            row.get("risk_level", ""),
        )
        with st.expander(title, expanded=False):
            _render_row_detail(row)


render()
