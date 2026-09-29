"""The review queue: where a machine proposal becomes (or fails to become) a finding.

Nothing this system produces is an audit conclusion until an auditor records one here.
The AI assessment row is never edited: the auditor's decision is written as a separate
``HumanReview`` row against it, which is what makes agreement between the two
*measurable* rather than overwritten - the point of the study.

This page owns only the *queue*: which assessment is on screen, what comes next once a
decision is recorded, and the history of decisions already made. The assessment itself -
the AI disclaimer banner, the alerts, the four numbered sections with their source
passages, and the decision form - is drawn by
:func:`app.frontend.assessment_view.render_assessment`, shared with the Assessments page
so the two never drift apart. The research decisions behind the form (nothing pre-filled
with the model's words, the honest review clock, agreement reported with its denominator)
live in :mod:`app.frontend.review_form` and are documented there.

How the queue picker keeps its place
------------------------------------
Streamlit widgets remember their own value, and a widget key may only be written *before*
the widget is instantiated in a run. Three things therefore happen at the top of every
run, in this order: if another page asked to open a specific assessment
(``state.current_assessment_id()`` differs from the id this page last showed), that id
wins; else if the remembered pick has left the queue (a decision was just recorded), the
cursor moves to the item that took its place; else the widget's own value stands. Only
then is the selectbox drawn.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st

from app.frontend import assessment_view, components, data_access, state, theme

#: The selectbox's widget key. Written before the widget is drawn, never after.
_PICK_KEY = "review_pick"
#: Position of the item on screen in the queue, so the next one can be opened once a
#: decision removes it.
_CURSOR_KEY = "review_cursor"
#: The id this page drew last run. A ``state.current_assessment_id()`` that differs from
#: it means another page (Home, Findings, Assessments) asked for that item.
_SHOWN_KEY = "review_shown"
#: An already-decided assessment opened from another page, pinned at the head of the
#: queue, and the decision it carried when pinned (a new one releases the pin).
_EXTRA_KEY = "review_extra"
_EXTRA_REVIEW_KEY = "review_extra_stamp"

_ASSESSMENTS_PAGE = "views/assessments.py"
_REPORTS_PAGE = "views/reports.py"


# ---- helpers
def _queue_label(row: Dict[str, Any]) -> str:
    """``CONTROL-004 · Change Management Approval — AI: Potential deficiency, High risk``."""
    ref = str(row.get("control_ref", "") or "")
    name = str(row.get("control_name", "") or "").strip()
    status = components.label("status", row.get("status")) or "No conclusion"
    risk = components.label("risk", row.get("risk_level"))
    head = " · ".join(part for part in (ref, name) if part) or "Assessment #{0}".format(row.get("id", ""))
    verdict = status + (", {0} risk".format(risk) if risk and risk != "Not rated" else "")
    return "{0} — AI: {1}".format(head, verdict)


def _selected_project() -> Optional[int]:
    project_id = state.current_project_id()
    if project_id is not None:
        return project_id
    components.empty_state(
        "No audit project selected",
        "Choose an audit project in the sidebar to see its review queue.",
    )
    return None


def _load_detail(assessment_id: int, retry_key: str = "review_retry_detail") -> Optional[Dict[str, Any]]:
    try:
        return data_access.get_assessment(int(assessment_id))
    except data_access.DataAccessError as exc:
        components.error_with_remedy("Could not load the assessment.", exc, retry_key=retry_key)
        return None


def _review_stamp(detail: Dict[str, Any]) -> str:
    """Identifies the latest decision on an assessment, so a new one can be noticed."""
    review = dict(detail.get("latest_review") or {})
    return "{0}:{1}".format(review.get("id", ""), review.get("decision", ""))


def _unpin() -> None:
    st.session_state.pop(_EXTRA_KEY, None)
    st.session_state.pop(_EXTRA_REVIEW_KEY, None)


def _belongs_here(detail: Optional[Dict[str, Any]], project_id: int) -> bool:
    if detail is None:
        return False
    owner = detail.get("project_id")
    return owner is None or int(owner) == int(project_id)


def _pinned_extra(project_id: int, ids: List[int]) -> Optional[Dict[str, Any]]:
    """The assessment another page asked for, when it is not in the queue.

    Home and Findings open an assessment with ``state.open_assessment`` or
    ``state.set_current_assessment``; when it already carries a decision it is not in the
    queue, and it is rendered at the head instead so the auditor lands on what they
    clicked. It stays pinned there across the reruns caused by using the form, and is
    released when a new decision is recorded on it (so the queue advances) or when the
    auditor picks another item. Anything from another project is dropped: the project
    switch that would have carried it here has already cleared the selection, so a stale
    id is all it can be.
    """
    current = state.current_assessment_id()
    shown = st.session_state.get(_SHOWN_KEY)
    externally_requested = current is not None and (shown is None or int(shown) != int(current))

    if externally_requested and current not in ids:
        detail = _load_detail(current, retry_key="review_retry_requested")
        if not _belongs_here(detail, project_id):
            state.set_current_assessment(None)
            _unpin()
            return None
        st.session_state[_EXTRA_KEY] = int(current)
        st.session_state[_EXTRA_REVIEW_KEY] = _review_stamp(detail or {})
        return detail

    pinned = st.session_state.get(_EXTRA_KEY)
    if pinned is None:
        return None
    if int(pinned) in ids:
        _unpin()
        return None
    detail = _load_detail(int(pinned), retry_key="review_retry_pinned")
    if not _belongs_here(detail, project_id):
        _unpin()
        return None
    if _review_stamp(detail or {}) != st.session_state.get(_EXTRA_REVIEW_KEY):
        # A decision was just recorded on the pinned item: release it and let the cursor
        # open the next queued item.
        _unpin()
        return None
    return detail


def _choose_target(options: List[int], ids: List[int]) -> None:
    """Write the selectbox's value for this run, following the rules in the module docstring."""
    current = state.current_assessment_id()
    shown = st.session_state.get(_SHOWN_KEY)
    picked = st.session_state.get(_PICK_KEY)

    externally_requested = (
        current is not None
        and current in options
        and (shown is None or int(shown) != int(current))
    )
    if externally_requested:
        st.session_state[_PICK_KEY] = int(current)
        return
    if picked in options:
        return
    # The remembered pick has left the queue (a decision was recorded) or was never made:
    # open the item now sitting at the old position, or the last one if the queue shrank
    # past it.
    cursor = st.session_state.get(_CURSOR_KEY)
    if ids and cursor is not None:
        position = max(0, min(int(cursor), len(ids) - 1))
        st.session_state[_PICK_KEY] = int(ids[position])
    else:
        st.session_state[_PICK_KEY] = int(options[0])


# ---- the queue
def _render_queue(project_id: int, pending: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Draw the picker and return the detail of the item to render, or None."""
    labels: Dict[int, str] = {int(row["id"]): _queue_label(row) for row in pending}
    ids = list(labels)
    by_id = {int(row["id"]): row for row in pending}

    extra = _pinned_extra(project_id, ids)
    options = list(ids)
    if extra is not None:
        extra_id = int(extra["id"])
        options = [extra_id] + ids
        labels[extra_id] = _queue_label(extra)

    if not options:
        _unpin()
        return None

    _choose_target(options, ids)
    if extra is not None and int(st.session_state.get(_PICK_KEY, -1)) != int(extra["id"]):
        # The auditor moved on to a queued item: the pinned one leaves the picker.
        _unpin()
        extra = None
        options = list(ids)

    picker_col, skip_col = st.columns([5, 1], gap="small", vertical_alignment="bottom")
    with picker_col:
        chosen = st.selectbox(
            "Assessment to review",
            options=options,
            format_func=lambda value: labels.get(int(value), "Assessment #{0}".format(value)),
            key=_PICK_KEY,
            help="The latest AI assessment for each control with no recorded decision.",
        )
    with skip_col:
        skip = st.button(
            "Skip to next",
            key="review_skip",
            width="stretch",
            disabled=len(options) < 2,
            help="Leave this item in the queue and open the next one.",
        )

    chosen_id = int(chosen if chosen is not None else options[0])
    position = options.index(chosen_id)
    st.session_state[_SHOWN_KEY] = chosen_id
    state.set_current_assessment(chosen_id)

    if chosen_id in by_id:
        st.session_state[_CURSOR_KEY] = ids.index(chosen_id)
        st.caption("Item {0} of {1} awaiting a decision".format(ids.index(chosen_id) + 1, len(ids)))
    else:
        st.session_state[_CURSOR_KEY] = 0
        st.caption(
            "Opened from another page · {0}".format(
                "{0} item{1} awaiting a decision".format(len(ids), "" if len(ids) == 1 else "s")
                if ids
                else "the queue is empty"
            )
        )

    if skip and len(options) > 1:
        # The selectbox is already drawn this run, so its key cannot be written here; the
        # request goes through the current-assessment id and is honoured at the top of
        # the next run by ``_choose_target``.
        following = options[(position + 1) % len(options)]
        state.set_current_assessment(int(following))
        st.rerun()

    if extra is not None and chosen_id == int(extra["id"]):
        return extra
    return _load_detail(chosen_id)


def _render_empty_queue(project_id: int) -> None:
    try:
        stage = data_access.project_stage(project_id)
    except data_access.DataAccessError as exc:
        components.error_with_remedy("Could not read where this audit stands.", exc, retry_key="review_retry_stage")
        return
    assessed = int(stage.get("controls_assessed", 0) or 0)
    if assessed == 0:
        components.empty_state("Nothing to review yet", "Run an assessment first.")
        components.next_steps([("Run the AI assessment", _ASSESSMENTS_PAGE, ":material/fact_check:")])
        return
    components.empty_state(
        "All {0} assessed control{1} have a decision".format(assessed, "" if assessed == 1 else "s"),
        "Every AI assessment in this audit project has been reviewed. The next step is the report.",
    )
    components.next_steps(
        [
            ("Generate the report", _REPORTS_PAGE, ":material/summarize:"),
            ("Assess more controls", _ASSESSMENTS_PAGE, ":material/fact_check:"),
        ],
        primary_index=0,
    )


# ---- completed reviews
def _history_frames(project_id: int) -> Tuple[Optional["pd.DataFrame"], Optional["pd.DataFrame"]]:
    """(compact display frame, full export frame) of completed decisions; (None, None) when empty."""
    reviews = data_access.list_reviews(project_id=project_id, include_pending=False)
    assessments = data_access.list_assessments(project_id=project_id)
    by_id = {int(row["id"]): row for row in assessments}
    completed = [row for row in reviews if str(row.get("decision", "")) != "PENDING"]
    if not completed:
        return None, None

    display: List[Dict[str, Any]] = []
    export: List[Dict[str, Any]] = []
    for review in completed:
        assessment = by_id.get(int(review.get("assessment_id", 0) or 0), {})
        ai_status = components.label("status", assessment.get("status")) or "-"
        final_status = components.label("status", review.get("final_status")) or "-"
        control = str(assessment.get("control_ref", "") or "")
        display.append(
            {
                "control": control,
                "decision": components.label("decision", review.get("decision")),
                "conclusion": "{0} -> {1}".format(ai_status, final_status),
                "agreed": bool(review.get("agreed_with_ai_status")),
                "reviewer": review.get("reviewer_name", ""),
                "when": components.when(review.get("created_at")),
            }
        )
        export.append(
            {
                "review_id": review.get("id"),
                "assessment_id": review.get("assessment_id"),
                "control_ref": control,
                "control_name": assessment.get("control_name", ""),
                "reviewer": review.get("reviewer_name", ""),
                "decision": review.get("decision", ""),
                "decision_label": components.label("decision", review.get("decision")),
                "ai_status": assessment.get("status", ""),
                "ai_status_label": ai_status,
                "final_status": review.get("final_status", ""),
                "final_status_label": final_status,
                "agreed_status": bool(review.get("agreed_with_ai_status")),
                "ai_risk": assessment.get("risk_level", ""),
                "ai_risk_label": components.label("risk", assessment.get("risk_level")),
                "final_risk_level": review.get("final_risk_level", ""),
                "final_risk_label": components.label("risk", review.get("final_risk_level")),
                "agreed_risk": bool(review.get("agreed_with_ai_risk")),
                "final_finding": review.get("final_finding", ""),
                "final_recommendation": review.get("final_recommendation", ""),
                "comments": review.get("comments", ""),
                "requested_evidence": " | ".join(str(item) for item in (review.get("requested_evidence") or [])),
                "review_seconds": float(review.get("review_seconds", 0.0) or 0.0),
                "usefulness_rating": review.get("usefulness_rating"),
                "flagged_hallucination": bool(review.get("flagged_hallucination")),
                "hallucination_note": review.get("hallucination_note", ""),
                "experiment_mode": assessment.get("experiment_mode", ""),
                "created_at": review.get("created_at"),
            }
        )
    return pd.DataFrame(display), pd.DataFrame(export)


def _render_history(project_id: int, display: Optional["pd.DataFrame"], export: Optional["pd.DataFrame"]) -> None:
    if display is None or export is None or display.empty:
        st.caption("No decision has been recorded on this audit project yet.")
        return
    components.df_table(
        display,
        columns=["control", "decision", "conclusion", "agreed", "reviewer", "when"],
        column_config={
            "control": st.column_config.TextColumn("Control", width="small"),
            "decision": st.column_config.TextColumn("Decision", width="medium"),
            "conclusion": st.column_config.TextColumn("AI conclusion -> your conclusion", width="large"),
            "agreed": st.column_config.CheckboxColumn("Agreed", width="small", help="Your conclusion equals the AI conclusion."),
            "reviewer": st.column_config.TextColumn("Reviewer", width="medium"),
            "when": st.column_config.TextColumn("When", width="medium"),
        },
        key="review_history",
    )
    timed = int((export["review_seconds"] > 0).sum())
    components.note(
        "Durations in the CSV are wall clock from opening an item to recording the decision "
        "and include time away from the screen; {0} of {1} review(s) carry a recorded "
        "duration, and a reviewer who was interrupted may have chosen not to record "
        "one.".format(timed, len(export))
    )
    components.download_row(
        "Download the review history (CSV)",
        export.to_csv(index=False),
        "human_reviews_project_{0}.csv".format(project_id),
        mime="text/csv",
        key="review_csv",
    )


# ---- agreement so far
def _render_agreement(project_id: int, pending_count: int) -> None:
    try:
        stats = data_access.dashboard_stats(project_id)
    except data_access.DataAccessError as exc:
        components.error_with_remedy("Could not read the review statistics.", exc, retry_key="review_retry_stats")
        return

    completed = int(stats.get("completed_reviews", 0) or 0)
    agreement = stats.get("human_ai_agreement_rate")
    risk_agreement = stats.get("human_ai_risk_agreement_rate")
    definitions = dict(stats.get("definitions") or {})
    agreement_definition = str(definitions.get("human_ai_agreement_rate", "") or "")

    components.metric_row(
        [
            {
                "label": "Awaiting review",
                "value": pending_count,
                "color": theme.AI_COLOR,
                "caption": "Latest assessment per control with no recorded decision.",
                "help_text": str(definitions.get("pending_human_reviews", "") or ""),
            },
            {
                "label": "Reviews completed",
                "value": completed,
                "caption": "Decisions other than 'awaiting decision'.",
            },
            {
                "label": "Conclusion agreement",
                "value": "n/a" if not completed else "{0:.0%}".format(float(agreement or 0.0)),
                "caption": "n = {0} completed review(s).".format(completed),
                "color": theme.HUMAN_COLOR,
                "help_text": agreement_definition,
            },
            {
                "label": "Risk agreement",
                "value": "n/a" if not completed else "{0:.0%}".format(float(risk_agreement or 0.0)),
                "caption": "n = {0}. Your risk level equals the AI's.".format(completed),
                "color": theme.HUMAN_COLOR,
            },
            {
                "label": "Outputs flagged as fabrication",
                "value": int(stats.get("flagged_hallucinations", 0) or 0),
                "color": theme.verdict_color("FABRICATED"),
                "caption": "Reviews where the auditor ticked the fabrication flag.",
            },
        ]
    )
    if agreement_definition:
        components.note("Conclusion agreement: " + agreement_definition)
    if completed and completed < 5:
        components.note(
            "With {0} completed review(s) the agreement figure is a description of those "
            "reviews and nothing more. It is not an estimate of how often this system and "
            "an auditor would agree.".format(completed)
        )
    elif not completed:
        components.note(
            "Agreement is reported with its denominator. Until a decision is recorded there "
            "is nothing to report."
        )


# ---- the page
def render() -> None:
    components.section_header(
        "Review queue",
        subtitle="Every AI assessment needs your decision before it counts. Work through the queue here.",
        eyebrow="Assessment",
    )
    project_id = _selected_project()
    if project_id is None:
        return

    reviewer = state.auditor_name().strip()
    if not reviewer:
        st.warning(
            "Enter your name in the sidebar before recording a decision. Every decision is "
            "attributed to a name - a declared one, since this prototype has no login."
        )

    try:
        pending = data_access.pending_reviews(project_id=project_id)
    except data_access.DataAccessError as exc:
        components.error_with_remedy("Could not read the review queue.", exc, retry_key="review_retry_queue")
        return

    detail = _render_queue(project_id, pending)
    if detail is None:
        if not pending:
            _render_empty_queue(project_id)
    else:
        if not pending:
            st.caption("The queue is empty; this item was opened from another page.")
        st.markdown("")
        # Draws the whole assessment: the single AI disclaimer banner, alerts, finding,
        # the four numbered sections and the decision form. It reruns the page itself once
        # a decision is recorded; the queue logic above then opens the next item.
        assessment_view.render_assessment(
            detail, reviewer=reviewer, show_review_form=True, key_prefix="review"
        )

    st.markdown("---")
    try:
        display, export = _history_frames(project_id)
    except data_access.DataAccessError as exc:
        components.error_with_remedy("Could not read the review history.", exc, retry_key="review_retry_history")
        return
    completed_count = 0 if export is None else len(export)
    history_tab, agreement_tab = st.tabs(
        ["Completed reviews ({0})".format(completed_count), "Agreement so far"]
    )
    with history_tab:
        _render_history(project_id, display, export)
    with agreement_tab:
        _render_agreement(project_id, len(pending))


if __name__ == "__main__":
    render()
