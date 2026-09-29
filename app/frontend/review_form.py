"""The auditor's decision form, shared by the Assessments and Human Review pages.

An AI assessment is a proposal until an auditor records a decision against it here.
The decision is written as a separate ``human_reviews`` row through
``data_access.record_review``; the ``assessments`` row is never edited, which is what
keeps agreement between the two *measurable* rather than overwritten. The agreement
flags themselves are derived by the service layer on write and cannot be set from this
form.

Three design decisions are research decisions, not cosmetic ones, and every one of
them survives the plain-language rewrite:

**Nothing is pre-filled with the model's words.** The finding, recommendation and
comment boxes start blank. Pre-filling would invite the reviewer to accept wording they
have not written - the automation bias this project studies rather than induces - and
would record model prose as human prose in the audit trail. A blank field means "left
as the AI stated it", which the service layer already treats correctly. The one way to
adopt model text (copying its missing-evidence list into the request box) is an explicit
click, never a default.

**The status defaults follow the decision.** "Accept" pre-selects the AI's conclusion,
because that is what accepting means. "Modify" and "Reject" pre-select *nothing*: the
reviewer must choose a conclusion, so a disagreement cannot be recorded as agreement by
leaving a default alone. "Request more evidence" locks the conclusion to Insufficient
evidence and the risk to Not rated: an auditor asking for more evidence has not
concluded, and inheriting the AI's status would inflate the agreement figure with a
decision they did not make.

**The review clock is honest about what it measures.** ``review_seconds`` is wall clock
from the moment the item was opened to the moment the decision is submitted, including
time away from the screen, because a browser-side timer cannot know the difference. It
can be discarded with one checkbox, so an inflated or truncated duration is never
recorded silently. It is read at submission, not at render.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import streamlit as st

from app.frontend import components, data_access, state

#: The four decisions an auditor can record. PENDING exists in the vocabulary as the
#: state of an item nobody has looked at; it is not offered as an outcome, because
#: submitting "pending" is indistinguishable from not submitting at all.
DECISIONS = ("ACCEPTED", "MODIFIED", "REJECTED", "MORE_EVIDENCE_REQUESTED")

DECISION_LABELS: Dict[str, str] = {
    "ACCEPTED": "Accept finding",
    "MODIFIED": "Modify finding",
    "REJECTED": "Reject finding",
    "MORE_EVIDENCE_REQUESTED": "Request more evidence",
}

DECISION_CAPTIONS: Dict[str, str] = {
    "ACCEPTED": "The AI conclusion stands as the audit conclusion.",
    "MODIFIED": "The conclusion is yours, informed by the AI output.",
    "REJECTED": "The AI conclusion is wrong.",
    "MORE_EVIDENCE_REQUESTED": "Not concludable on the evidence held.",
}

#: What each decision means for the agreement measurement. Shown under the radio so the
#: reviewer knows what their choice records before they record it.
DECISION_NOTES: Dict[str, str] = {
    "ACCEPTED": (
        "Recorded as agreement on both the conclusion and the risk level unless you "
        "change them below."
    ),
    "MODIFIED": (
        "Agreement is measured on the outcome, not the editing: changing the wording but "
        "keeping the conclusion still counts as agreeing about the control's condition."
    ),
    "REJECTED": (
        "Rejecting the reasoning while landing on the same conclusion still counts as "
        "agreement on the conclusion. Choose a different conclusion if you disagree with it."
    ),
    "MORE_EVIDENCE_REQUESTED": (
        "The conclusion is recorded as Insufficient evidence and the risk as Not rated: "
        "an auditor asking for more evidence has not concluded, and inheriting the AI's "
        "conclusion here would inflate the agreement figure with a decision you did not make."
    ),
}

_STATUS_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
_RISK_NOT_RATED = "NOT_RATED"
_RATING_OPTIONS = ["Not rated", "1", "2", "3", "4", "5"]


def _index_of(options: List[str], value: str) -> Optional[int]:
    return options.index(value) if value in options else None


def _lines(text: str) -> List[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def render_review_form(detail: Dict[str, Any], reviewer: str, key_prefix: str = "review") -> bool:
    """Draw the decision form for one assessment; return True when a decision was recorded.

    ``detail`` is the mapping from ``data_access.get_assessment``. ``reviewer`` is the
    auditor's declared name; with a blank name the submit button is disabled and the form
    says why, because every decision is attributed to a person. ``key_prefix`` namespaces
    every widget key so the form can appear on more than one page without collisions.

    The caller decides what happens after a recorded decision (typically ``st.rerun()``);
    a success message has already been queued with ``state.flash`` and the review clock
    for this item has been cleared.
    """
    assessment_id = int(detail["id"])
    sfx = "{0}_{1}".format(key_prefix, assessment_id)
    reviewer_name = str(reviewer or "").strip()
    control_ref = str(detail.get("control_ref", "") or "")

    # The clock runs from the first time this item is drawn. ``start_review_timer`` is
    # idempotent, so reruns caused by typing do not restart it.
    state.start_review_timer(assessment_id)

    decision = st.radio(
        "Your decision",
        options=list(DECISIONS),
        format_func=lambda value: DECISION_LABELS.get(value, components.label("decision", value)),
        captions=[DECISION_CAPTIONS[value] for value in DECISIONS],
        key="{0}_decision".format(sfx),
        help="Recorded exactly as chosen; the wording you write below is recorded separately.",
    )
    decision = str(decision or DECISIONS[0])
    components.note(DECISION_NOTES.get(decision, ""))

    statuses = [str(item) for item in data_access.assessment_statuses()]
    risks = [str(item) for item in data_access.risk_levels()]
    ai_status = str(detail.get("status", "") or "")
    ai_risk = str(detail.get("risk_level", "") or "")

    locked = decision == "MORE_EVIDENCE_REQUESTED"
    if decision == "ACCEPTED":
        status_index = _index_of(statuses, ai_status)
        risk_index = _index_of(risks, ai_risk)
    elif locked:
        status_index = _index_of(statuses, _STATUS_INSUFFICIENT)
        risk_index = _index_of(risks, _RISK_NOT_RATED)
    else:  # MODIFIED / REJECTED: the reviewer must choose
        status_index = None
        risk_index = None

    # Filling the request box from the model's own missing-evidence list is allowed only
    # by an explicit click: adopting AI text must always be an act, never a default.
    request_key = "{0}_requested".format(sfx)
    missing = [str(item) for item in (detail.get("missing_evidence", []) or [])]
    if locked and missing and st.button(
        "Copy the AI's missing-evidence list into the request box",
        key="{0}_copy_missing".format(sfx),
    ):
        st.session_state[request_key] = "\n".join(missing)
        st.rerun()

    with st.form("{0}_form".format(sfx), clear_on_submit=False):
        status_col, risk_col = st.columns(2, gap="small")
        with status_col:
            final_status = st.selectbox(
                "Your conclusion",
                options=statuses,
                index=status_index,
                format_func=lambda value: components.label("status", value),
                placeholder="Choose your conclusion",
                # Keyed by decision so the default follows the choice above: a widget
                # with a fixed key keeps its own remembered value and ignores ``index``.
                key="{0}_status_{1}_{2}".format(key_prefix, assessment_id, decision),
                disabled=locked,
            )
        with risk_col:
            final_risk = st.selectbox(
                "Your risk level",
                options=risks,
                index=risk_index,
                format_func=lambda value: components.label("risk", value),
                placeholder="Choose your risk level",
                key="{0}_risk_{1}_{2}".format(key_prefix, assessment_id, decision),
                disabled=locked,
                help=components.RISK_MODEL_NOTE,
            )

        final_finding = st.text_area(
            "Your finding",
            key="{0}_finding".format(sfx),
            height=110,
            placeholder="Leave blank to let the AI finding stand unchanged.",
            help=(
                "Deliberately not pre-filled with the AI text. A blank field is recorded "
                "as 'the AI wording stands'; anything you type is recorded as yours."
            ),
        )
        final_recommendation = st.text_area(
            "Your recommendation",
            key="{0}_reco".format(sfx),
            height=90,
            placeholder="Leave blank to let the AI recommendation stand unchanged.",
        )
        comments = st.text_area(
            "Comments",
            key="{0}_comments".format(sfx),
            height=80,
            placeholder="Why you reached this decision. Recorded in the audit trail.",
        )
        requested_evidence = ""
        if locked:
            requested_evidence = st.text_area(
                "Evidence you are requesting (one item per line)",
                key=request_key,
                height=90,
                placeholder="Exception register with approver and expiry date",
            )

        rating_col, flag_col = st.columns([2, 3], gap="small")
        with rating_col:
            usefulness = st.selectbox(
                "How useful was the AI output? (optional)",
                options=_RATING_OPTIONS,
                index=0,
                key="{0}_rating".format(sfx),
                help="1 = misleading, 5 = ready to accept. Recorded for the study; optional.",
            )
        with flag_col:
            flagged = st.checkbox(
                "Flag this output as containing a fabrication or an unsupported claim",
                key="{0}_flag".format(sfx),
            )
            hallucination_note = st.text_input(
                "What was fabricated?",
                key="{0}_flag_note".format(sfx),
                placeholder="Required if you tick the box above.",
            )

        record_timing = st.checkbox(
            "Record how long this review took",
            value=True,
            key="{0}_timing".format(sfx),
            help=(
                "The clock started when you opened this item and includes time away from "
                "the screen. Untick if you were interrupted, or if the page was reloaded "
                "while the item was open; the review is then stored with no duration "
                "rather than a wrong one."
            ),
        )
        submitted = st.form_submit_button(
            "Record decision", type="primary", disabled=not reviewer_name
        )
        if not reviewer_name:
            st.caption("Enter your name in the sidebar first")

    if not submitted or not reviewer_name:
        return False

    if flagged and not str(hallucination_note or "").strip():
        st.error(
            "Describe what was fabricated before recording. An unexplained flag cannot be "
            "checked later, and this field is one of the study's measurements."
        )
        return False
    if not locked and (final_status is None or final_risk is None):
        st.error("Choose your conclusion and your risk level before recording the decision.")
        return False
    if locked:
        final_status = _STATUS_INSUFFICIENT
        final_risk = _RISK_NOT_RATED

    # Read the clock at submission, not at render: the reviewer may have spent time in
    # the form since the page was last drawn.
    seconds = state.review_elapsed_seconds(assessment_id) if record_timing else 0.0
    try:
        data_access.record_review(
            assessment_id,
            reviewer_name=reviewer_name,
            decision=decision,
            final_status=str(final_status),
            final_risk_level=str(final_risk),
            final_finding=str(final_finding or "").strip(),
            final_recommendation=str(final_recommendation or "").strip(),
            comments=str(comments or "").strip(),
            requested_evidence=_lines(requested_evidence),
            review_seconds=seconds,
            usefulness_rating=None if usefulness == "Not rated" else int(usefulness),
            flagged_hallucination=bool(flagged),
            hallucination_note=str(hallucination_note or "").strip(),
        )
    except data_access.DataAccessError as exc:
        components.error_with_remedy("The decision was not recorded.", exc)
        return False

    state.clear_review_timer(assessment_id)
    state.flash(
        "Decision recorded: {0} for {1}".format(components.label("decision", decision), control_ref),
        "success",
    )
    return True


__all__ = [
    "DECISIONS",
    "DECISION_CAPTIONS",
    "DECISION_LABELS",
    "DECISION_NOTES",
    "render_review_form",
]
