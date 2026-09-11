"""The human review queue: where a machine proposal becomes (or fails to become) a finding.

Nothing this system produces is an audit conclusion until an auditor records one here.
The AI assessment row is never edited: the auditor's decision is written as a separate
``HumanReview`` row against it, which is what makes agreement between the two
*measurable* rather than overwritten - the point of the study.

Three design decisions on this page are research decisions, not cosmetic ones.

**The AI output sits beside the form, not inside it.** The reviewer reads the proposal
and its citations on the left and writes their own conclusion on the right. The fields
are not pre-filled with the model's text. Pre-filling would (a) invite the reviewer to
accept wording they have not written, which is the automation bias this project is
supposed to study rather than induce, and (b) record model prose as human prose in the
audit trail. A blank field means "left as the AI stated it", which
``app.audit.service.record_human_review`` already treats correctly.

**The review clock is honest about what it measures.** ``review_seconds`` is one of the
study's measurements, so it is wall clock from the moment this item was last opened to
the moment the decision is submitted - including any time the reviewer spent away from
the screen, because a browser-side timer with no idle detection cannot know the
difference. The measured value is shown on screen before submission and can be discarded
with one checkbox, so a duration inflated by an interruption or shortened by a page
reload is never recorded silently.

**Agreement is reported with its denominator.** A percentage over three reviews is not a
finding about anything; the count is always printed beside it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from app.frontend import components, data_access, state, theme

#: The four decisions an auditor can record. PENDING exists in the vocabulary as the
#: state of an item nobody has looked at; it is not offered as an outcome, because
#: submitting "pending" is indistinguishable from not submitting at all.
_DECISIONS = ("ACCEPTED", "MODIFIED", "REJECTED", "MORE_EVIDENCE_REQUESTED")

_DECISION_LABELS: Dict[str, str] = {
    "ACCEPTED": "Accept finding - the AI conclusion stands as the audit conclusion",
    "MODIFIED": "Modify finding - the conclusion is mine, informed by the AI output",
    "REJECTED": "Reject finding - the AI conclusion is wrong",
    "MORE_EVIDENCE_REQUESTED": "Request more evidence - not concludable on what is held",
}

_DECISION_NOTES: Dict[str, str] = {
    "ACCEPTED": (
        "Recorded as agreement on both the status and the risk level unless you change "
        "them below."
    ),
    "MODIFIED": (
        "Agreement is measured on the outcome, not the editing: changing the wording but "
        "keeping the status still counts as agreeing about the control's condition."
    ),
    "REJECTED": (
        "Rejecting the reasoning while landing on the same status still counts as "
        "agreement on the conclusion. Change the final status if you disagree with it."
    ),
    "MORE_EVIDENCE_REQUESTED": (
        "The final status defaults to INSUFFICIENT_EVIDENCE: an auditor asking for more "
        "evidence has not concluded, and inheriting the AI's status here would inflate "
        "the agreement figure with a decision you did not make."
    ),
}

#: Which item the clock is currently running for. Changing item restarts the clock, so
#: the recorded duration is "time since this item was last opened", never the sum of
#: every visit plus everything done in between.
_OPEN_KEY = "review_open_assessment_id"


def _go(path: str) -> None:
    """Navigate to another page, reporting rather than raising when it is absent."""
    try:
        st.switch_page(path)
    except Exception:  # noqa: BLE001 - navigation is a convenience, never a dependency
        st.warning("That page is not installed in this build: {0}".format(path))


def _with_control(detail: Dict[str, Any]) -> Dict[str, Any]:
    """Make sure the REQUIRES column carries the control library's own wording.

    ``data_access.get_assessment`` attaches the control definition when the console runs
    in-process, but the API's assessment payload has no nested control, so over HTTP the
    four-way panel would show a REQUIRES column holding nothing but the model's
    restatement - precisely the substitution that panel exists to prevent. Fetching the
    control through the facade and recomputing the split costs one cached read and keeps
    both transports showing the same thing.
    """
    if detail.get("control") or not detail.get("control_ref"):
        return detail
    try:
        control = data_access.get_control(detail["control_ref"])
    except data_access.DataAccessError:
        return detail
    if not control:
        return detail
    detail["control"] = control
    detail["four_way"] = data_access.build_four_way(detail)
    return detail


def _selected_project() -> Optional[int]:
    project_id = state.current_project_id()
    if project_id is not None:
        return project_id
    components.empty_state(
        "No audit project selected",
        "Choose an engagement in the sidebar to see its review queue.",
    )
    return None


def _open_item(assessment_id: int) -> None:
    """Mark this item as the one on screen and (re)start its review clock."""
    previous = st.session_state.get(_OPEN_KEY)
    if previous is not None and int(previous) != int(assessment_id):
        state.clear_review_timer(int(previous))
        state.start_review_timer(int(assessment_id), restart=True)
    else:
        state.start_review_timer(int(assessment_id))
    st.session_state[_OPEN_KEY] = int(assessment_id)


def _render_queue_metrics(project_id: int, pending: List[Dict[str, Any]]) -> None:
    try:
        stats = data_access.dashboard_stats(project_id)
    except data_access.DataAccessError as exc:
        st.error("Could not read the review statistics: {0}".format(exc))
        return

    completed = int(stats.get("completed_reviews", 0) or 0)
    agreement = stats.get("human_ai_agreement_rate")
    risk_agreement = stats.get("human_ai_risk_agreement_rate")
    components.metric_row(
        [
            {
                "label": "Awaiting review",
                "value": len(pending),
                "color": theme.AI_COLOR,
                "caption": "Latest assessment per control with no recorded decision.",
            },
            {"label": "Reviews completed", "value": completed},
            {
                "label": "Status agreement",
                "value": "n/a" if not completed else "{0:.0%}".format(float(agreement or 0.0)),
                "caption": "n = {0} completed review(s).".format(completed),
                "color": theme.HUMAN_COLOR,
            },
            {
                "label": "Risk agreement",
                "value": "n/a" if not completed else "{0:.0%}".format(float(risk_agreement or 0.0)),
                "caption": "n = {0}.".format(completed),
                "color": theme.HUMAN_COLOR,
            },
            {
                "label": "Outputs flagged as fabrication",
                "value": stats.get("flagged_hallucinations", 0),
                "color": theme.verdict_color("FABRICATED"),
            },
        ]
    )
    definitions = dict(stats.get("definitions") or {})
    components.note(definitions.get("human_ai_agreement_rate", ""))
    if completed and completed < 5:
        components.note(
            "With {0} completed review(s) the agreement figure is a description of those "
            "reviews and nothing more. It is not an estimate of how often this system and "
            "an auditor would agree.".format(completed)
        )


def _render_ai_side(detail: Dict[str, Any]) -> None:
    """The proposal under review, with its evidence, in the reviewer's own reading order."""
    components.ai_disclaimer_banner(
        "You are reviewing a machine-generated proposal. Verify each quotation against "
        "its source before accepting anything below.",
        mode=detail.get("experiment_mode"),
        provider=str(detail.get("llm_provider", "")),
        model=str(detail.get("llm_model", "")),
    )
    components.badges(
        components.status_badge(detail.get("status")),
        components.risk_badge(detail.get("risk_level"), detail.get("risk_score")),
        components.confidence_badge(detail.get("confidence")),
        components.sufficiency_badge(detail.get("evidence_sufficiency")),
        components.mode_badge(detail.get("experiment_mode")),
    )
    components.kv_grid(
        {
            "Assessment": detail.get("assessment", ""),
            "Finding": detail.get("finding", ""),
            "Risk statement": detail.get("risk", ""),
            "Reasoning": detail.get("reasoning", ""),
            "Recommendation": detail.get("recommendation", ""),
        }
    )

    validation = dict(detail.get("validation_report") or {})
    fabricated = int(validation.get("fabricated", 0) or 0)
    if fabricated:
        st.error(
            "{0} citation(s) on this assessment could not be matched to the evidence they "
            "point at. That is a fabrication signal, and it is the strongest reason on "
            "this page to reject rather than accept.".format(fabricated)
        )
    claims = list(validation.get("unsupported_claims", []) or [])
    if claims:
        st.warning(
            "Numbers with no counterpart in the retrieved evidence: {0}".format(
                "; ".join(str(item) for item in claims)
            )
        )

    missing = list(detail.get("missing_evidence", []) or [])
    if missing:
        st.markdown("**The model says this evidence is missing**")
        for item in missing:
            st.markdown("- {0}".format(item))

    outstanding = list(detail.get("human_verification_required", []) or [])
    if outstanding:
        st.markdown("**The model says these points need human verification**")
        for item in outstanding:
            st.markdown("- {0}".format(item))

    citations = list(detail.get("citations") or [])
    st.markdown("**Cited evidence ({0})**".format(len(citations)))
    if not citations:
        st.caption("Nothing was cited, so nothing in this proposal is traceable to a source.")
    for position, citation in enumerate(citations, start=1):
        components.citation_card(
            citation, index=position, context_loader=data_access.chunk_context
        )


def _render_timer(assessment_id: int) -> float:
    """Show what the clock currently reads and return it."""
    elapsed = state.review_elapsed_seconds(assessment_id)
    components.metric_card(
        "Time on this item",
        "{0:.0f}s".format(elapsed),
        caption=(
            "Wall clock since you last opened this item, refreshed when the page reruns. "
            "It includes time spent away from the screen."
        ),
        color=theme.HUMAN_COLOR,
    )
    return elapsed


def _render_decision_form(detail: Dict[str, Any], reviewer: str) -> None:
    """The auditor's decision. Nothing here is pre-filled with the model's words."""
    assessment_id = int(detail["id"])
    suffix = str(assessment_id)

    decision = st.radio(
        "Decision",
        options=list(_DECISIONS),
        format_func=lambda value: _DECISION_LABELS.get(value, value),
        key="review_decision_{0}".format(suffix),
        help="Recorded exactly as chosen; the wording you write below is recorded separately.",
    )
    components.note(_DECISION_NOTES.get(decision, ""))

    statuses = data_access.assessment_statuses()
    ai_status = str(detail.get("status", ""))
    default_status = (
        "INSUFFICIENT_EVIDENCE" if decision == "MORE_EVIDENCE_REQUESTED" else ai_status
    )
    risks = data_access.risk_levels()
    ai_risk = str(detail.get("risk_level", ""))

    elapsed = _render_timer(assessment_id)

    # The evidence-request box can be filled from the model's own missing-evidence list,
    # but only by an explicit click: adopting AI text must always be an act, never a
    # default.
    request_key = "review_requested_{0}".format(suffix)
    missing = list(detail.get("missing_evidence", []) or [])
    if missing and st.button(
        "Copy the model's missing-evidence list into the request box",
        key="review_copy_missing_{0}".format(suffix),
    ):
        st.session_state[request_key] = "\n".join(str(item) for item in missing)
        st.rerun()

    with st.form("review_form_{0}".format(suffix), clear_on_submit=False):
        status_col, risk_col = st.columns(2, gap="small")
        with status_col:
            final_status = st.selectbox(
                "Final status (yours)",
                options=statuses,
                index=statuses.index(default_status) if default_status in statuses else 0,
                key="review_status_{0}".format(suffix),
            )
        with risk_col:
            final_risk = st.selectbox(
                "Final risk level (yours)",
                options=risks,
                index=risks.index(ai_risk) if ai_risk in risks else 0,
                key="review_risk_{0}".format(suffix),
                help=components.RISK_MODEL_NOTE,
            )

        final_finding = st.text_area(
            "Final finding",
            key="review_finding_{0}".format(suffix),
            height=110,
            placeholder="Leave blank to let the AI finding stand unchanged.",
            help=(
                "Deliberately not pre-filled with the AI text. A blank field is recorded "
                "as 'the AI wording stands'; anything you type is recorded as yours."
            ),
        )
        final_recommendation = st.text_area(
            "Final recommendation",
            key="review_reco_{0}".format(suffix),
            height=90,
            placeholder="Leave blank to let the AI recommendation stand unchanged.",
        )
        comments = st.text_area(
            "Review comments",
            key="review_comments_{0}".format(suffix),
            height=80,
            placeholder="Why you reached this decision. Recorded in the audit trail.",
        )
        requested_evidence = st.text_area(
            "Evidence requested (one per line)",
            key=request_key,
            height=80,
            placeholder="Exception register with approver and expiry date",
        )

        rating_col, flag_col = st.columns([2, 3], gap="small")
        with rating_col:
            usefulness = st.select_slider(
                "How useful was the AI output?",
                options=["Not rated", "1", "2", "3", "4", "5"],
                value="Not rated",
                key="review_rating_{0}".format(suffix),
                help="1 = misleading, 5 = ready to accept. Recorded for the study; optional.",
            )
        with flag_col:
            flagged = st.checkbox(
                "Flag this output as a fabrication or unsupported claim",
                key="review_flag_{0}".format(suffix),
            )
            hallucination_note = st.text_input(
                "What was fabricated?",
                key="review_flag_note_{0}".format(suffix),
                placeholder="Required if you tick the box above.",
            )

        record_timing = st.checkbox(
            "Record the {0:.0f}s timing with this review".format(elapsed),
            value=True,
            key="review_timing_{0}".format(suffix),
            help=(
                "Untick if you were interrupted, or if this page was reloaded while the "
                "item was open. The review is then stored with no duration rather than "
                "with a wrong one."
            ),
        )
        submitted = st.form_submit_button("Record decision", type="primary")

    if not submitted:
        return

    if flagged and not hallucination_note.strip():
        st.error(
            "Describe what was fabricated before submitting. An unexplained flag cannot "
            "be checked later, and this field is one of the study's measurements."
        )
        return

    # Read the clock at submission, not at render: the value shown above is as of the
    # last rerun, and the reviewer may have spent time in the form since then.
    seconds = state.review_elapsed_seconds(assessment_id) if record_timing else 0.0
    try:
        data_access.record_review(
            assessment_id,
            reviewer_name=reviewer,
            decision=decision,
            final_status=final_status,
            final_risk_level=final_risk,
            final_finding=final_finding.strip(),
            final_recommendation=final_recommendation.strip(),
            comments=comments.strip(),
            requested_evidence=[
                line.strip() for line in (requested_evidence or "").splitlines() if line.strip()
            ],
            review_seconds=seconds,
            usefulness_rating=None if usefulness == "Not rated" else int(usefulness),
            flagged_hallucination=bool(flagged),
            hallucination_note=hallucination_note.strip(),
        )
    except data_access.DataAccessError as exc:
        st.error("The decision was not recorded: {0}".format(exc))
        return

    state.clear_review_timer(assessment_id)
    st.session_state.pop(_OPEN_KEY, None)
    state.set_current_assessment(assessment_id)
    state.flash(
        "Decision recorded for {0}: {1}{2}.".format(
            detail.get("control_ref", ""),
            decision,
            " (timing not recorded)" if not record_timing else " in {0:.0f}s".format(seconds),
        ),
        "success",
    )
    st.rerun()


def _render_history(project_id: int) -> None:
    """Completed decisions, with the AI conclusion they were recorded against."""
    try:
        reviews = data_access.list_reviews(project_id=project_id)
        assessments = data_access.list_assessments(project_id=project_id)
    except data_access.DataAccessError as exc:
        st.error("Could not read the review history: {0}".format(exc))
        return

    by_id = {int(row["id"]): row for row in assessments}
    completed = [row for row in reviews if str(row.get("decision", "")) != "PENDING"]
    if not completed:
        st.caption("No decision has been recorded on this engagement yet.")
        return

    rows: List[Dict[str, Any]] = []
    for review in completed:
        assessment = by_id.get(int(review.get("assessment_id", 0) or 0), {})
        rows.append(
            {
                "review_id": review.get("id"),
                "control_ref": assessment.get("control_ref", ""),
                "reviewer": review.get("reviewer_name", ""),
                "decision": review.get("decision", ""),
                "ai_status": assessment.get("status", ""),
                "final_status": review.get("final_status", ""),
                "agreed_status": bool(review.get("agreed_with_ai_status")),
                "ai_risk": assessment.get("risk_level", ""),
                "final_risk_level": review.get("final_risk_level", ""),
                "agreed_risk": bool(review.get("agreed_with_ai_risk")),
                "review_seconds": float(review.get("review_seconds", 0.0) or 0.0),
                "usefulness_rating": review.get("usefulness_rating"),
                "flagged_hallucination": bool(review.get("flagged_hallucination")),
                "created_at": review.get("created_at"),
            }
        )

    frame = pd.DataFrame(rows)
    components.df_table(
        frame,
        columns=[
            "review_id",
            "control_ref",
            "reviewer",
            "decision",
            "ai_status",
            "final_status",
            "agreed_status",
            "ai_risk",
            "final_risk_level",
            "agreed_risk",
            "review_seconds",
            "usefulness_rating",
            "flagged_hallucination",
            "created_at",
        ],
        column_config={
            "review_id": st.column_config.NumberColumn("Review", width="small", format="%d"),
            "reviewer": st.column_config.TextColumn("Reviewer", width="medium"),
            "ai_status": st.column_config.TextColumn("AI status", width="medium"),
            "agreed_status": st.column_config.CheckboxColumn("Status agreed", width="small"),
            "ai_risk": st.column_config.TextColumn("AI risk", width="small"),
            "agreed_risk": st.column_config.CheckboxColumn("Risk agreed", width="small"),
            "review_seconds": st.column_config.NumberColumn("Seconds", width="small", format="%.0f"),
            "usefulness_rating": st.column_config.NumberColumn("Usefulness", width="small", format="%d"),
            "flagged_hallucination": st.column_config.CheckboxColumn("Fabrication flagged", width="small"),
        },
        key="review_history",
    )
    timed = [row["review_seconds"] for row in rows if row["review_seconds"] > 0]
    components.note(
        "Durations are wall clock from opening an item to submitting the decision and "
        "include time away from the screen; {0} of {1} review(s) carry a recorded "
        "duration, and a reviewer who was interrupted may have chosen not to record "
        "one.".format(len(timed), len(rows))
    )
    components.download_row(
        "Download the review history (CSV)",
        frame.to_csv(index=False),
        "human_reviews_project_{0}.csv".format(project_id),
        mime="text/csv",
        key="review_csv",
    )


def render() -> None:
    components.section_header(
        "Human review",
        subtitle="Every AI assessment requires an auditor decision before it means anything.",
        eyebrow="Assessment",
    )
    project_id = _selected_project()
    if project_id is None:
        return

    reviewer = state.auditor_name().strip()
    if not reviewer:
        st.warning(
            "Enter a reviewing auditor in the sidebar first. Every decision is attributed "
            "to a name - a declared one, since this prototype has no login."
        )

    try:
        pending = data_access.pending_reviews(project_id=project_id)
    except data_access.DataAccessError as exc:
        st.error("Could not read the review queue: {0}".format(exc))
        return

    _render_queue_metrics(project_id, pending)

    st.markdown("")
    components.section_header(
        "Review queue",
        subtitle="{0} assessment(s) awaiting a decision.".format(len(pending)),
    )

    if not pending:
        if components.empty_state(
            "The queue is empty",
            "Every assessed control in this engagement has a recorded decision. Run more "
            "assessments to add to the queue.",
            action_label="Go to Assessments",
            action_key="review_go_assess",
        ):
            _go("pages/assessments.py")
        st.markdown("")
        components.section_header("Completed reviews", subtitle="The decision history for this engagement.")
        _render_history(project_id)
        return

    labels = {
        int(row["id"]): "{0} · {1} · risk {2} · {3}".format(
            row.get("control_ref", ""),
            row.get("status", ""),
            row.get("risk_level", ""),
            row.get("experiment_mode", ""),
        )
        for row in pending
    }
    ids = list(labels)
    current = state.current_assessment_id()
    if current not in ids:
        current = ids[0]
    # Keyed by the current item: with a fixed key Streamlit returns the widget's own
    # remembered value and ignores ``index``, so an assessment opened from the Findings
    # or Assessments page would be replaced by whatever was last picked here.
    chosen = st.selectbox(
        "Assessment to review",
        options=ids,
        index=ids.index(current),
        format_func=lambda value: labels.get(value, str(value)),
        key="review_pick_{0}".format(current),
    )
    if int(chosen) != int(current):
        state.set_current_assessment(int(chosen))
        st.rerun()
    state.set_current_assessment(int(current))
    _open_item(int(current))

    try:
        detail = data_access.get_assessment(int(current))
    except data_access.DataAccessError as exc:
        st.error("Could not load the assessment: {0}".format(exc))
        return
    if detail is None:
        st.warning("That assessment no longer exists.")
        state.set_current_assessment(None)
        return
    detail = _with_control(detail)

    components.section_header(
        "{0} - {1}".format(detail.get("control_ref", ""), detail.get("control_name", "")),
        subtitle="Assessment #{0}, {1}".format(detail.get("id"), detail.get("mode_label", "")),
        eyebrow="Under review",
    )
    components.four_way_panel(detail, context_loader=data_access.chunk_context, show_citations=False)

    st.markdown("")
    ai_column, human_column = st.columns([3, 2], gap="medium")
    with ai_column:
        components.section_header("AI proposal", subtitle="What the system produced, unedited.")
        _render_ai_side(detail)
    with human_column:
        components.section_header(
            "Your decision", subtitle="Recorded as a separate row; the AI output is not overwritten."
        )
        _render_decision_form(detail, reviewer or "unnamed reviewer")

    st.markdown("---")
    components.section_header(
        "Completed reviews", subtitle="The decision history for this engagement."
    )
    _render_history(project_id)


render()
