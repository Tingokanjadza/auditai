"""Run AI control assessments and read one in full.

This page is where the system's claim is either made good or exposed. Everything else in
the application prepares evidence or records what a human decided; here a control is
actually assessed by a model, and the result is laid out so that an auditor can see
*where every part of it came from* before deciding whether to believe any of it.

What the page owns and what it borrows
--------------------------------------
The page owns three things: the run panel (which controls, which pipeline, the button),
the results table (the selector), and the glue that loads the selected assessment. The
assessment screen itself - the plain finding summary, the four numbered sections with
the source passages, the auditor's decision form and the research detail folded away
underneath - is drawn by :func:`app.frontend.assessment_view.render_assessment`, which
the Human Review page calls as well. One screen, two doors, and the safety rails hold on
both: exactly one AI disclaimer banner above the AI output, every model string escaped,
a fabricated citation shown as a failure, insufficient evidence kept apart from a
deficiency, and the AI and human records displayed side by side and never merged.

What this page deliberately does not do
---------------------------------------
It never presents an assessment as a decision. Every run is persisted with the
human-review flag set, the AI's conclusion is always labelled as a proposal, and the
auditor's conclusion is a separate record that this page never edits. Nothing runs until
the auditor presses the button.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd
import streamlit as st

from app.frontend import assessment_view, components, data_access, state

#: Namespace for this page's filters in ``st.session_state`` (see app.frontend.state).
PAGE = "assessments"

#: One line per pipeline configuration saying what it holds constant. The choice in the
#: research options *is* the independent variable of the study, and an auditor picking a
#: configuration from a bare code has no way to know what they are choosing.
MODE_NOTES: Dict[str, str] = {
    "A_RAW_LLM": (
        "Baseline. No retrieval: the raw file text is truncated into the prompt with no "
        "chunk identifiers and no citation scaffolding, and no safety rail corrects the "
        "answer. This is the condition the other two are measured against."
    ),
    "B_RAG": (
        "Adds retrieval, the full control definition and a structured output schema. "
        "Citations are validated and the verdicts recorded, but nothing is downgraded - "
        "so B isolates what retrieval and structure contribute on their own."
    ),
    "C_RAG_WORKFLOW": (
        "Adds the audit workflow on top of B: an evidence-sufficiency pre-check, a "
        "self-critique pass, safety rails that downgrade a conclusion the evidence does "
        "not support, prototype risk scoring, and a mandatory human-review flag."
    ),
}

#: The three ways of choosing which controls to run.
_WHICH_NOT_ASSESSED = "Not yet assessed"
_WHICH_ALL = "All in scope"
_WHICH_CHOOSE = "Choose..."
_WHICH_OPTIONS: Tuple[str, ...] = (_WHICH_NOT_ASSESSED, _WHICH_ALL, _WHICH_CHOOSE)

_REVIEW_ANY = "Any"
_REVIEW_AWAITING = "Awaiting decision"
_REVIEW_DECIDED = "Decided"
_REVIEW_OPTIONS: Tuple[str, ...] = (_REVIEW_ANY, _REVIEW_AWAITING, _REVIEW_DECIDED)

#: The columns an auditor reads, in order. Every value is a friendly label already.
_AUDITOR_COLUMNS: Tuple[str, ...] = (
    "Control",
    "Name",
    "AI conclusion",
    "Risk",
    "Evidence",
    "Fabricated quotes",
    "Auditor decision",
    "When",
)

#: Added by the "Show research columns" toggle.
_RESEARCH_COLUMNS: Tuple[str, ...] = (
    "id",
    "Confidence",
    "Citations",
    "Pipeline",
    "Latency (ms)",
    "Risk score",
)

#: Session-state keys this page owns beyond its widgets. All carry the page prefix so a
#: project switch clears them (see state.PROJECT_SCOPED_KEY_PREFIXES).
_K_LAST_RUN = "assess_last_run"
_K_TABLE_LAST = "assess_table_last_pick"

#: Filter widget keys, so "Clear filters" can pop exactly these.
_FILTER_KEYS: Tuple[str, ...] = (
    "assess_f_status",
    "assess_f_risk",
    "assess_f_mode",
    "assess_f_reviewed",
)


# ---- shared page furniture
def _selected_project() -> Optional[int]:
    """The audit project chosen in the sidebar, or None with an explanation drawn."""
    project_id = state.current_project_id()
    if project_id is not None:
        return project_id
    components.empty_state(
        "No audit project selected",
        "Choose an audit project in the sidebar, or start one on the Audit Projects page. "
        "An assessment is always run against the evidence held by one audit project.",
    )
    components.next_steps([("Go to Audit Projects", "views/audit_projects.py", ":material/folder_managed:")])
    return None


def _latest_by_control(rows: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """``control_ref -> latest assessment row`` from a latest-per-control listing."""
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        ref = str(row.get("control_ref", "") or "")
        if ref and ref not in out:
            out[ref] = dict(row)
    return out


def _plural(count: int, singular: str, plural: str = "") -> str:
    return "{0} {1}".format(count, singular if count == 1 else (plural or singular + "s"))


def _remedy_for(error: str) -> str:
    """One plain next step for a failed control, chosen from the error text."""
    lowered = str(error or "").lower()
    provider_markers = (
        "api key",
        "api_key",
        "credential",
        "auth",
        "401",
        "403",
        "provider",
        "not configured",
        "connection",
        "timed out",
        "timeout",
        "rate limit",
        "unavailable",
        "model",
    )
    if any(marker in lowered for marker in provider_markers):
        return "Check the AI provider on Settings."
    if "evidence" in lowered or "no chunks" in lowered or "nothing to read" in lowered:
        return "Upload evidence on the Evidence page."
    return ""


# ---- run panel
def _mode_picker() -> str:
    """The pipeline selector, folded under 'Research options' with each condition explained."""
    modes = data_access.experiment_modes()
    values = [str(item["value"]) for item in modes]
    default = data_access.default_mode()
    remembered = str(state.get_filter(PAGE, "mode", default) or default)
    index = values.index(remembered) if remembered in values else values.index(default)
    help_text = "\n\n".join(
        "**{0}** - {1}".format(components.label("mode", value), MODE_NOTES.get(value, ""))
        for value in values
    )
    chosen = st.selectbox(
        "Pipeline",
        options=values,
        index=index,
        format_func=lambda value: components.label("mode", value),
        key="assess_mode",
        help=help_text,
    )
    chosen = str(chosen or default)
    if chosen != default:
        components.note(MODE_NOTES.get(chosen, ""))
    return chosen


def _render_run_panel(
    project_id: int,
    scoped: Sequence[Dict[str, Any]],
    latest: Dict[str, Dict[str, Any]],
    has_evidence: bool,
) -> None:
    """Pick controls, optionally pick a pipeline, and run - always visible at the top."""
    refs_all = [str(row.get("control_id")) for row in scoped if row.get("control_id")]
    assessed = [ref for ref in refs_all if ref in latest]
    unassessed = [ref for ref in refs_all if ref not in latest]
    st.markdown(
        "**{0}** in scope · **{1}** assessed · **{2}** not yet assessed".format(
            _plural(len(refs_all), "control"), len(assessed), len(unassessed)
        )
    )

    if not refs_all:
        st.warning(
            "No control is in scope for this audit project, so there is nothing to assess. "
            "Choose the controls first."
        )
        components.next_steps(
            [
                ("Choose the controls", "views/audit_projects.py", ":material/checklist:"),
                ("Browse the control library", "views/controls.py", ":material/library_books:"),
            ]
        )
        return

    names = {
        str(row.get("control_id")): str(row.get("name", "") or "") for row in scoped
    }

    def describe(ref: str) -> str:
        last = latest.get(ref)
        stamp = components.when(last.get("created_at")) if last else ""
        return "{0} · {1} · last assessed {2}".format(ref, names.get(ref, ""), stamp or "never")

    default_which = _WHICH_NOT_ASSESSED if unassessed else _WHICH_ALL
    # A remembered "Not yet assessed" is useless once everything has been assessed; move
    # the selector on before the widget is drawn (writing a widget key is only allowed
    # before its widget exists in the run).
    if not unassessed and st.session_state.get("assess_which") == _WHICH_NOT_ASSESSED:
        st.session_state["assess_which"] = _WHICH_ALL
    # Streamlit warns when a keyed widget is given a default while its key already
    # holds a value, so the default is only supplied on the first draw.
    which = st.segmented_control(
        "Which controls",
        options=list(_WHICH_OPTIONS),
        default=None if "assess_which" in st.session_state else default_which,
        key="assess_which",
        help="'Not yet assessed' runs only the controls with no assessment in this audit project.",
    )
    which = str(which or default_which)

    if which == _WHICH_CHOOSE:
        chosen_refs = [
            str(ref)
            for ref in st.multiselect(
                "Controls to assess",
                options=refs_all,
                default=unassessed or refs_all,
                format_func=describe,
                key="assess_controls",
            )
        ]
    elif which == _WHICH_ALL:
        chosen_refs = list(refs_all)
    else:
        chosen_refs = list(unassessed)
        if not chosen_refs:
            st.caption("Every control in scope has been assessed. Choose 'All in scope' to run them again.")

    with st.expander("Research options", expanded=False):
        mode = _mode_picker()

    run_disabled = not chosen_refs
    run_help: Optional[str] = None
    if not has_evidence:
        run_disabled = True
        run_help = "Upload evidence first"
    elif not chosen_refs:
        run_help = "Choose at least one control"

    run_label = "Run assessment ({0})".format(_plural(len(chosen_refs), "control"))
    clicked = st.button(run_label, type="primary", key="assess_run", disabled=run_disabled, help=run_help)

    if not has_evidence:
        st.info(
            "This audit project holds no evidence yet. Upload the files the controls should "
            "be tested against, then come back and press Run assessment."
        )
        components.next_steps([("Upload evidence", "views/evidence.py", ":material/inventory_2:")])
        if chosen_refs and st.button(
            "Run anyway (shows how missing evidence is reported)",
            key="assess_run_anyway",
            help="Every control will come back as Insufficient evidence - that is the honest answer, not a deficiency.",
        ):
            clicked = True

    if clicked and chosen_refs:
        _run(project_id, chosen_refs, mode, has_evidence)


def _run(project_id: int, refs: Sequence[str], mode: str, has_evidence: bool) -> None:
    """Execute the run inside a status box, one line per control, then hand over to review."""
    total = len(refs)
    slots: Dict[str, Any] = {}

    with st.status("Assessing...", expanded=True) as box:

        def on_start(index: int, count: int, control_ref: str) -> None:
            box.update(label="Assessing {0} ({1} of {2})...".format(control_ref, index, count))
            slot = st.empty()
            slots[str(control_ref)] = slot
            slot.markdown("Assessing {0} ({1} of {2})...".format(control_ref, index, count))

        def on_done(done: int, count: int, control_ref: str) -> None:
            slot = slots.get(str(control_ref))
            if slot is not None:
                slot.markdown("{0} finished ({1} of {2}).".format(control_ref, done, count))

        try:
            results = data_access.run_project_assessment(
                project_id,
                mode=mode,
                control_refs=list(refs),
                persist=True,
                progress=on_done,
                on_start=on_start,
            )
        except data_access.DataAccessError as exc:
            box.update(label="The run could not be started", state="error", expanded=True)
            components.error_with_remedy("The run could not be started.", exc)
            remedy = _remedy_for(str(exc))
            if remedy:
                st.caption(remedy)
            return

        # Replace each control's progress line with what the assistant concluded, in
        # plain words, so the auditor reads the run's outcome without leaving the box.
        for row in results:
            ref = str(row.get("control_ref", "") or "?")
            slot = slots.get(ref)
            if slot is None:
                slot = st.empty()
            error = str(row.get("error", "") or "")
            if error:
                remedy = _remedy_for(error)
                slot.markdown(
                    ":material/error: **{0}** did not complete: {1}{2}".format(
                        ref, error, (" " + remedy) if remedy else ""
                    )
                )
                continue
            slot.markdown(
                ":material/check: **{0}** · {1} · {2} risk".format(
                    ref,
                    components.label("status", row.get("status")) or "No conclusion",
                    components.label("risk", row.get("risk_level")) or "Not rated",
                )
            )
        if not has_evidence:
            st.caption(
                "No evidence was uploaded, so every result reads Insufficient evidence. That "
                "is the correct answer for an empty evidence set - it is not a deficiency."
            )

        failed = [row for row in results if row.get("error")]
        succeeded = [row for row in results if not row.get("error") and row.get("assessment_id")]
        box.update(
            label="Assessed {0} of {1}".format(len(succeeded), total),
            state="complete" if not failed else "error",
            expanded=bool(failed),
        )

    state.set_filter(PAGE, "mode", mode)
    if succeeded:
        # Open the first new result straight away: an auditor who has just run the
        # assistant wants to read what it said, not hunt for it in a table.
        state.set_current_assessment(int(succeeded[0]["assessment_id"]))
    state.flash(
        "Assessed {0}. Every result needs your review.".format(_plural(len(succeeded), "control")),
        "success" if not failed else "warning",
    )
    for row in failed:
        remedy = _remedy_for(str(row.get("error", "")))
        state.flash(
            "{0} did not complete: {1}{2}".format(
                row.get("control_ref", "?"), row.get("error", ""), (" " + remedy) if remedy else ""
            ),
            "error",
        )
    # The next-step links are drawn on the run that follows, after the flash.
    st.session_state[_K_LAST_RUN] = {"succeeded": len(succeeded), "failed": len(failed)}
    st.rerun()


def _render_after_run() -> None:
    """The 'what now' links that follow a completed run, shown once."""
    summary = st.session_state.pop(_K_LAST_RUN, None)
    if not isinstance(summary, dict) or not summary.get("succeeded"):
        return
    components.next_steps(
        [
            ("Review now", "views/human_review.py", ":material/rate_review:"),
            ("See findings", "views/findings.py", ":material/flag:"),
        ]
    )


# ---- results table
def _clear_filters() -> None:
    for key in _FILTER_KEYS:
        st.session_state.pop(key, None)
    state.clear_filters(PAGE)


def _table_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """One assessment as the auditor reads it, with the raw fields kept for the CSV."""
    reviewed = bool(row.get("is_reviewed"))
    decision = components.label("decision", row.get("review_decision")) if reviewed else "Awaiting decision"
    return {
        "id": row.get("id"),
        "Control": str(row.get("control_ref", "") or ""),
        "Name": str(row.get("control_name", "") or ""),
        "AI conclusion": components.label("status", row.get("status")) or "No conclusion",
        "Risk": components.label("risk", row.get("risk_level")) or "Not rated",
        "Evidence": components.label("sufficiency", row.get("evidence_sufficiency")) or "-",
        "Fabricated quotes": int(row.get("fabricated_citations", 0) or 0),
        "Auditor decision": decision,
        "When": components.when(row.get("created_at")),
        "Confidence": components.label("confidence", row.get("confidence")) or "-",
        "Citations": int(row.get("citation_count", 0) or 0),
        "Pipeline": components.label("mode", row.get("experiment_mode")),
        "Latency (ms)": int(row.get("latency_ms", 0) or 0),
        "Risk score": float(row.get("risk_score", 0.0) or 0.0),
        # Raw tokens, for the export only.
        "status_code": str(row.get("status", "") or ""),
        "risk_code": str(row.get("risk_level", "") or ""),
        "sufficiency_code": str(row.get("evidence_sufficiency", "") or ""),
        "decision_code": str(row.get("review_decision", "") or ""),
        "pipeline_code": str(row.get("experiment_mode", "") or ""),
        "auditor_conclusion": components.label("status", row.get("final_status")) if reviewed else "",
        "created_at": row.get("created_at"),
        "project_id": row.get("project_id"),
    }


def _render_filters() -> Tuple[Optional[List[str]], Optional[List[str]], Optional[List[str]], Optional[bool]]:
    """The secondary filters, tucked into a popover. Returns the four query arguments."""
    with st.popover("Filter", help="Narrow the table. The row you have open stays open."):
        statuses = st.multiselect(
            "AI conclusion",
            options=data_access.assessment_statuses(),
            format_func=lambda value: components.label("status", value),
            key="assess_f_status",
        )
        risks = st.multiselect(
            "Risk",
            options=data_access.risk_levels(),
            format_func=lambda value: components.label("risk", value),
            key="assess_f_risk",
        )
        review_choice = st.selectbox(
            "Auditor decision",
            options=list(_REVIEW_OPTIONS),
            key="assess_f_reviewed",
        )
        modes = st.multiselect(
            "Pipeline (research)",
            options=[str(item["value"]) for item in data_access.experiment_modes()],
            format_func=lambda value: components.label("mode", value),
            key="assess_f_mode",
        )
        if st.button("Clear filters", key="assess_f_clear"):
            _clear_filters()
            st.rerun()
    reviewed = {_REVIEW_ANY: None, _REVIEW_AWAITING: False, _REVIEW_DECIDED: True}.get(
        str(review_choice or _REVIEW_ANY)
    )
    return (
        [str(item) for item in statuses] or None,
        [str(item) for item in risks] or None,
        [str(item) for item in modes] or None,
        reviewed,
    )


def _render_results(project_id: int, all_rows: Sequence[Dict[str, Any]], latest_count: int) -> List[int]:
    """The table that selects the assessment to read. Returns the ids it is showing."""
    controls_row = st.columns([2, 2, 2, 3], gap="small")
    with controls_row[0]:
        every_run = st.toggle(
            "Show every run",
            key="assess_f_all_runs",
            help="Off: one row per control, its most recent assessment. On: every assessment ever run here.",
        )
    with controls_row[1]:
        research = st.toggle(
            "Show research columns",
            key="assess_f_research",
            help="Adds the id, confidence, citation count, pipeline, latency and risk score.",
        )
    with controls_row[2]:
        statuses, risks, modes, reviewed = _render_filters()
    latest_only = not bool(every_run)

    hidden = max(0, len(all_rows) - int(latest_count))
    if latest_only:
        st.caption(
            "Showing the most recent assessment per control; {0} hidden.".format(
                _plural(hidden, "older run")
            )
        )
    else:
        st.caption("Showing every run, newest first.")

    try:
        rows = data_access.list_assessments(
            project_id=project_id,
            status=statuses,
            risk_level=risks,
            mode=modes,
            reviewed=reviewed,
            latest_per_control=latest_only,
        )
    except data_access.DataAccessError as exc:
        components.error_with_remedy("Could not read the assessments.", exc, retry_key="assess_retry_list")
        return []

    if not rows:
        filtered = any(value for value in (statuses, risks, modes)) or reviewed is not None
        if filtered:
            components.empty_state(
                "No assessment matches these filters",
                "Open Filter and press Clear filters, or run an assessment from the panel above.",
            )
        else:
            components.empty_state(
                "No assessment yet",
                "Press Run assessment above. Every result will appear here for you to read and review.",
            )
        return []

    table = [_table_row(dict(row)) for row in rows]
    ids = [int(item["id"]) for item in table if item.get("id") is not None]
    shown = list(_AUDITOR_COLUMNS) + (list(_RESEARCH_COLUMNS) if research else [])
    column_config = {
        "id": st.column_config.NumberColumn("ID", width="small", format="%d"),
        "Control": st.column_config.TextColumn("Control", width="small"),
        "Name": st.column_config.TextColumn("Name", width="medium"),
        "AI conclusion": st.column_config.TextColumn("AI conclusion", width="medium", help="The assistant's proposal - not an audit conclusion."),
        "Risk": st.column_config.TextColumn("Risk", width="small"),
        "Evidence": st.column_config.TextColumn("Evidence", width="small", help="Whether the evidence supplied could settle the control."),
        "Fabricated quotes": st.column_config.NumberColumn("Fabricated quotes", width="small", format="%d", help="Quotes that could not be found in the evidence. Any number above zero is a failure."),
        "Auditor decision": st.column_config.TextColumn("Auditor decision", width="medium"),
        "When": st.column_config.TextColumn("When", width="medium"),
        "Confidence": st.column_config.TextColumn("Confidence", width="small"),
        "Citations": st.column_config.NumberColumn("Citations", width="small", format="%d"),
        "Pipeline": st.column_config.TextColumn("Pipeline", width="medium"),
        "Latency (ms)": st.column_config.NumberColumn("Latency (ms)", width="small", format="%d"),
        "Risk score": st.column_config.ProgressColumn("Risk score", min_value=0, max_value=100, format="%.0f", width="small"),
    }
    event = components.df_table(
        table,
        columns=shown,
        column_config=column_config,
        key="assess_table",
        on_select="rerun",
        selection_mode="single-row",
    )
    st.caption("Select a row to read the assessment below.")

    # The table is the selector. Only a *new* pick is acted on: the widget keeps its
    # selection across reruns, and re-applying it would override a choice made through
    # the selectbox fallback or a jump from another page.
    picked: Optional[int] = None
    try:
        positions = list(event.selection.rows) if event is not None else []
    except Exception:  # noqa: BLE001 - an event without a selection payload
        positions = []
    if positions and 0 <= int(positions[0]) < len(ids):
        picked = ids[int(positions[0])]
    last_pick = st.session_state.get(_K_TABLE_LAST)
    if picked is not None and picked != last_pick:
        st.session_state[_K_TABLE_LAST] = picked
        state.set_current_assessment(picked)
    elif picked is None and last_pick is not None:
        st.session_state[_K_TABLE_LAST] = None

    frame = pd.DataFrame(table)
    components.download_row(
        "Download this table (CSV)",
        frame.to_csv(index=False),
        "assessments_project_{0}.csv".format(project_id),
        mime="text/csv",
        key="assess_csv",
    )

    labels = {
        int(item["id"]): "#{0} · {1} · {2} · {3}".format(
            item["id"], item["Control"], item["AI conclusion"], item["When"]
        )
        for item in table
        if item.get("id") is not None
    }
    current = state.current_assessment_id()
    if current is None and ids:
        # Nothing chosen yet: open the newest row and say so, rather than leaving an
        # empty screen under a table full of results.
        current = ids[0]
        state.set_current_assessment(current)
        st.caption("Showing the most recent assessment. Select another row to open it.")
    options: List[int] = list(ids)
    if current is not None and current not in options:
        options.insert(0, int(current))
        labels[int(current)] = "#{0} · (not in the current filter)".format(current)
    chosen = st.selectbox(
        "Open an assessment",
        options=options,
        index=options.index(int(current)) if current in options else None,
        format_func=lambda value: labels.get(int(value), str(value)),
        placeholder="Or pick one here",
        # Keyed by the current id: a fixed key would hand back the widget's stale value
        # and override a selection made in the table or a jump from another page.
        key="assess_open_{0}".format(current if current is not None else "none"),
    )
    if chosen is not None and (current is None or int(chosen) != int(current)):
        state.set_current_assessment(int(chosen))
        st.rerun()
    return ids


# ---- detail
def _render_detail(assessment_id: int, in_table: bool) -> None:
    try:
        detail = data_access.get_assessment(assessment_id)
    except data_access.DataAccessError as exc:
        components.error_with_remedy(
            "Could not load assessment {0}.".format(assessment_id), exc, retry_key="assess_retry_detail"
        )
        return
    if detail is None:
        st.warning("Assessment {0} no longer exists. Pick another row above.".format(assessment_id))
        state.set_current_assessment(None)
        return
    if not in_table:
        st.caption("Not in the current filter")
    assessment_view.render_assessment(
        detail, reviewer=state.auditor_name(), show_review_form=True, key_prefix="assess"
    )


# ---- page
def render() -> None:
    components.section_header(
        "Assessments",
        subtitle=(
            "Run the assistant over the controls in this audit, then read exactly where "
            "every part of its answer came from."
        ),
        eyebrow="Assessment",
    )
    project_id = _selected_project()
    if project_id is None:
        return

    try:
        scoped = data_access.list_scoped_controls(project_id, active_only=True)
        evidence = data_access.list_evidence(project_id=project_id)
        latest_rows = data_access.list_assessments(project_id=project_id, latest_per_control=True)
        all_rows = data_access.list_assessments(project_id=project_id)
    except data_access.DataAccessError as exc:
        components.error_with_remedy("Could not read this audit project.", exc, retry_key="assess_retry_page")
        return

    latest = _latest_by_control(latest_rows)
    _render_after_run()
    _render_run_panel(project_id, scoped, latest, has_evidence=bool(evidence))

    st.markdown("")
    components.section_header(
        "Results",
        subtitle="Every assessment recorded for {0}. Each one is a proposal until you record a decision.".format(
            state.current_project_name()
        ),
    )
    ids = _render_results(project_id, all_rows, latest_count=len(latest_rows))

    current = state.current_assessment_id()
    if current is None:
        # Nothing selected and nothing to select: the empty state above says what to do.
        return

    st.markdown("---")
    _render_detail(int(current), in_table=int(current) in ids)


if __name__ == "__main__":
    render()
