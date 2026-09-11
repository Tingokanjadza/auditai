"""Run AI control assessments and inspect one in full.

This page is where the system's claim is either made good or exposed. Everything else in
the application prepares evidence or records what a human decided; here a control is
actually assessed by a model, and the result is laid out so that an auditor can see
*where every part of it came from* before deciding whether to believe any of it.

Why the detail view is built the way it is
------------------------------------------
An unstructured model answer blends four different kinds of statement into one
paragraph: what the control demands, what the evidence literally says, what the model
concluded, and what nobody has checked. The four-way panel
(:func:`app.frontend.components.four_way_panel`) refuses that blend, and the sections
below it exist to make the same refusal at a lower level - the risk band is shown with
the arithmetic that produced it, the citations with the verdict of a mechanical re-check,
the retrieval with what was actually put in front of the model, and the raw response with
nothing removed. A reviewer who disagrees with the conclusion can find the exact step
they disagree with.

What this page deliberately does not do
---------------------------------------
It never presents an assessment as a decision. Every run is persisted with
``human_review_required`` set, the AI's conclusion is always labelled as a proposal, and
the auditor's conclusion is recorded on the Human Review page against a row this page
never edits. Keeping the two apart is what makes agreement measurable rather than
overwritten.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence

import streamlit as st

from app.frontend import components, data_access, state, theme

#: Namespace for this page's filters in ``st.session_state`` (see app.frontend.state).
PAGE = "assessments"

#: One line per experimental condition saying what it holds constant, because the
#: choice in the run panel *is* the independent variable of the study and an auditor
#: picking a mode from a bare code has no way to know what they are choosing.
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

_STATUS_HELP = (
    "AI status is the model's proposal. Auditor status appears only once a review has "
    "been recorded on the Human Review page."
)


def _page_link(path: str, label: str, icon: str = "") -> None:
    """A link to another page, silently omitted when that page is not installed.

    ``st.page_link`` raises when its target is not a registered page, and the shell
    deliberately skips page modules that are absent (see
    ``app.frontend.streamlit_app.PAGE_SPECS``). A missing sibling page must not take
    this one down with it.
    """
    try:
        st.page_link(path, label=label, icon=icon or None)
    except Exception:  # noqa: BLE001 - navigation is a convenience, never a dependency
        st.caption("{0} (that page is not installed in this build)".format(label))


def _go(path: str) -> None:
    """Navigate to another page, reporting rather than raising when it is absent."""
    try:
        st.switch_page(path)
    except Exception:  # noqa: BLE001
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


# ---- shared page furniture
def _selected_project() -> Optional[int]:
    """The engagement chosen in the sidebar, or None with an explanation drawn."""
    project_id = state.current_project_id()
    if project_id is not None:
        return project_id
    components.empty_state(
        "No audit project selected",
        "Choose an engagement in the sidebar, or create one on the Audit Projects page. "
        "An assessment is always run against the evidence held by one project.",
    )
    return None


def _mode_picker(key: str, default: Optional[str] = None) -> str:
    """Experiment-mode selector with the long label and the condition's explanation."""
    modes = data_access.experiment_modes()
    values = [item["value"] for item in modes]
    labels = {item["value"]: item["label"] for item in modes}
    current = default or state.get_filter(PAGE, "mode", data_access.default_mode())
    index = values.index(current) if current in values else values.index(data_access.default_mode())

    chosen = st.selectbox(
        "Experimental condition",
        options=values,
        index=index,
        format_func=lambda value: labels.get(value, value),
        key=key,
        help=(
            "Which pipeline configuration answers. The three conditions are the "
            "independent variable of this study; see the note below the selector."
        ),
    )
    components.note(MODE_NOTES.get(chosen, ""))
    return str(chosen)


# ---- run panel
def _render_run_panel(project_id: int, scoped: Sequence[Dict[str, Any]]) -> None:
    """Pick controls, pick a condition, run, and watch it happen."""
    if not scoped:
        st.warning(
            "No control is in scope for this engagement, so there is nothing to assess. "
            "Add controls to the project's scope first."
        )
        _page_link("pages/controls.py", "Go to the control library", ":material/checklist:")
        return

    refs = [str(row.get("control_id")) for row in scoped]
    names = {
        str(row.get("control_id")): "{0} - {1}".format(row.get("control_id"), row.get("name", ""))
        for row in scoped
    }

    left, right = st.columns([3, 2], gap="medium")
    with left:
        chosen_refs = st.multiselect(
            "Controls to assess",
            options=refs,
            default=refs,
            format_func=lambda ref: names.get(ref, ref),
            key="assess_controls",
            help="Every control currently in this project's scope is selected by default.",
        )
    with right:
        mode = _mode_picker("assess_mode")

    st.caption(
        "Each control is assessed in its own engine call so progress can be reported "
        "between controls. Results are persisted; nothing here becomes an audit "
        "conclusion until an auditor records one."
    )

    disabled = not chosen_refs
    if st.button(
        "Run assessment ({0} control(s))".format(len(chosen_refs)),
        type="primary",
        key="assess_run",
        disabled=disabled,
    ):
        _run(project_id, chosen_refs, mode)


def _run(project_id: int, refs: Sequence[str], mode: str) -> None:
    """Execute the run, reporting progress and honest wall-clock timing."""
    total = len(refs)
    progress = st.progress(0.0, text="Starting…")
    status_line = st.empty()
    started = time.perf_counter()

    def report(done: int, count: int, control_ref: str) -> None:
        elapsed = time.perf_counter() - started
        progress.progress(
            min(1.0, done / float(count or 1)),
            text="{0} of {1} complete - last: {2}".format(done, count, control_ref),
        )
        status_line.caption("{0:.1f}s elapsed".format(elapsed))

    try:
        results = data_access.run_project_assessment(
            project_id, mode=mode, control_refs=list(refs), persist=True, progress=report
        )
    except data_access.DataAccessError as exc:
        progress.empty()
        st.error("The run could not be started: {0}".format(exc))
        return

    elapsed = time.perf_counter() - started
    progress.empty()
    status_line.empty()

    failed = [row for row in results if row.get("error")]
    succeeded = [row for row in results if not row.get("error") and row.get("assessment_id")]
    state.set_filter(PAGE, "mode", mode)
    if succeeded:
        # Open the newest result straight away: an auditor who has just run one control
        # wants to read it, not to hunt for it in a table.
        state.set_current_assessment(int(succeeded[-1]["assessment_id"]))
    state.flash(
        "Assessed {0} of {1} control(s) in {2:.1f}s. Every result requires auditor review.".format(
            len(succeeded), total, elapsed
        ),
        "success" if not failed else "warning",
    )
    for row in failed:
        state.flash(
            "{0} did not complete: {1}".format(row.get("control_ref", "?"), row.get("error", "")),
            "error",
        )
    st.rerun()


# ---- results table
def _render_results(project_id: int) -> Optional[int]:
    """Filterable table of this project's assessments; returns the id to open, if any."""
    filter_row = st.columns([2, 2, 2, 2, 1], gap="small")
    with filter_row[0]:
        statuses = st.multiselect(
            "AI status",
            options=data_access.assessment_statuses(),
            default=state.get_filter(PAGE, "status", []),
            key="assess_f_status",
        )
    with filter_row[1]:
        risks = st.multiselect(
            "Risk level",
            options=data_access.risk_levels(),
            default=state.get_filter(PAGE, "risk", []),
            key="assess_f_risk",
        )
    with filter_row[2]:
        modes = st.multiselect(
            "Condition",
            options=[item["value"] for item in data_access.experiment_modes()],
            default=state.get_filter(PAGE, "modes", []),
            key="assess_f_mode",
        )
    with filter_row[3]:
        review_choice = st.selectbox(
            "Review state",
            options=["Any", "Awaiting review", "Reviewed"],
            index=["Any", "Awaiting review", "Reviewed"].index(
                state.get_filter(PAGE, "reviewed", "Any")
            ),
            key="assess_f_reviewed",
        )
    with filter_row[4]:
        latest_only = st.checkbox(
            "Latest only",
            value=bool(state.get_filter(PAGE, "latest", False)),
            key="assess_f_latest",
            help="One row per control - the most recent assessment of it.",
        )

    state.set_filter(PAGE, "status", statuses)
    state.set_filter(PAGE, "risk", risks)
    state.set_filter(PAGE, "modes", modes)
    state.set_filter(PAGE, "reviewed", review_choice)
    state.set_filter(PAGE, "latest", latest_only)

    reviewed = {"Any": None, "Awaiting review": False, "Reviewed": True}[review_choice]
    try:
        rows = data_access.list_assessments(
            project_id=project_id,
            status=statuses or None,
            risk_level=risks or None,
            mode=modes or None,
            reviewed=reviewed,
            latest_per_control=latest_only,
        )
    except data_access.DataAccessError as exc:
        st.error("Could not read the assessments: {0}".format(exc))
        return None

    if not rows:
        components.empty_state(
            "No assessment matches these filters",
            "Clear the filters, or run an assessment from the panel above.",
        )
        return None

    table = [
        {
            "id": row.get("id"),
            "control_ref": row.get("control_ref", ""),
            "control_name": row.get("control_name", ""),
            "status": row.get("status", ""),
            "final_status": row.get("final_status", "") or "-",
            "risk_level": row.get("risk_level", ""),
            "risk_score": float(row.get("risk_score", 0.0) or 0.0),
            "confidence": row.get("confidence", ""),
            "evidence_sufficiency": row.get("evidence_sufficiency", ""),
            "citation_count": int(row.get("citation_count", 0) or 0),
            "fabricated_citations": int(row.get("fabricated_citations", 0) or 0),
            "experiment_mode": row.get("experiment_mode", ""),
            "is_reviewed": bool(row.get("is_reviewed")),
            "latency_ms": int(row.get("latency_ms", 0) or 0),
            "created_at": row.get("created_at"),
        }
        for row in rows
    ]
    frame = components.df_table(
        table,
        columns=[
            "id",
            "control_ref",
            "control_name",
            "status",
            "final_status",
            "risk_level",
            "risk_score",
            "confidence",
            "evidence_sufficiency",
            "citation_count",
            "fabricated_citations",
            "experiment_mode",
            "is_reviewed",
            "latency_ms",
            "created_at",
        ],
        key="assess_table",
    )
    components.note(_STATUS_HELP)
    if frame is not None:
        components.download_row(
            "Download this table (CSV)",
            frame.to_csv(index=False),
            "assessments_project_{0}.csv".format(project_id),
            mime="text/csv",
            key="assess_csv",
        )

    labels = {
        int(row["id"]): "#{0} · {1} · {2} · {3}".format(
            row["id"], row["control_ref"], row["status"], row["experiment_mode"]
        )
        for row in table
        if row.get("id") is not None
    }
    ids = list(labels)
    current = state.current_assessment_id()
    if current not in ids:
        current = ids[0]
    chosen = st.selectbox(
        "Open an assessment",
        options=ids,
        index=ids.index(current),
        format_func=lambda value: labels.get(value, str(value)),
        # The key carries the current selection deliberately. A fixed key would make
        # Streamlit hand back the widget's own remembered value on every later rerun and
        # ignore ``index`` - so an assessment that has just been run, or one opened from
        # the Findings page, would be set as current and then immediately overridden by
        # this widget's stale value. Keying by the current id recreates the widget
        # whenever the selection changes elsewhere.
        key="assess_open_{0}".format(current),
    )
    if int(chosen) != int(current):
        state.set_current_assessment(int(chosen))
        st.rerun()
    return int(current)


# ---- detail view
def _render_detail(assessment_id: int) -> None:
    try:
        detail = data_access.get_assessment(assessment_id)
    except data_access.DataAccessError as exc:
        st.error("Could not load assessment {0}: {1}".format(assessment_id, exc))
        return
    if detail is None:
        st.warning("Assessment {0} no longer exists.".format(assessment_id))
        state.set_current_assessment(None)
        return
    detail = _with_control(detail)

    components.section_header(
        "{0} - {1}".format(detail.get("control_ref", ""), detail.get("control_name", "")),
        subtitle="Assessment #{0}, produced {1}".format(detail.get("id"), detail.get("created_at", "")),
        eyebrow="AI-generated assessment - not an audit conclusion",
    )
    components.badges(
        components.status_badge(detail.get("status")),
        components.risk_badge(detail.get("risk_level"), detail.get("risk_score")),
        components.confidence_badge(detail.get("confidence"), detail.get("confidence_score")),
        components.sufficiency_badge(detail.get("evidence_sufficiency")),
        components.mode_badge(detail.get("experiment_mode")),
        components.decision_badge(detail.get("review_decision")),
    )

    _render_alerts(detail)
    components.four_way_panel(detail, context_loader=data_access.chunk_context, show_citations=False)

    st.markdown("")
    components.section_header(
        "AI assessment beside the auditor's conclusion",
        subtitle="The two records are kept separate; neither overwrites the other.",
    )
    components.ai_vs_human_panel(detail)
    if not detail.get("is_reviewed"):
        _page_link(
            "pages/human_review.py",
            "Record an auditor decision on this assessment",
            ":material/rate_review:",
        )

    _render_conclusion(detail)
    _render_risk(detail)
    _render_citations(detail)
    _render_retrieval(detail)
    _render_validation(detail)
    _render_workflow(detail)
    _render_provenance(detail)


def _render_alerts(detail: Dict[str, Any]) -> None:
    """Everything a reviewer must not be allowed to scroll past."""
    validation = dict(detail.get("validation_report") or {})
    fabricated = int(validation.get("fabricated", 0) or 0)
    total = int(validation.get("total", 0) or 0)

    if fabricated:
        st.error(
            "FABRICATED CITATION: {0} of {1} citation(s) on this assessment could not be "
            "matched to the evidence they point at. Treat the conclusion as unsupported "
            "until each one has been checked by hand - a fabricated citation is not a "
            "formatting problem, it is the model asserting evidence that is not there.".format(
                fabricated, total
            )
        )
    if detail.get("error"):
        st.error("The engine recorded an error on this run: {0}".format(detail["error"]))

    rails = list(validation.get("rails_applied", []) or [])
    if validation.get("status_downgraded"):
        st.warning(
            "A safety rail changed the conclusion. The model answered {0}; the system "
            "recorded {1} because the evidence did not support the stronger claim.".format(
                validation.get("original_status", "?"), detail.get("status", "?")
            )
        )
    elif rails:
        st.info("Safety rails applied: {0}.".format(", ".join(str(item) for item in rails)))

    if total == 0:
        st.warning(
            "This assessment cites no evidence at all. Nothing in it is traceable to a "
            "source document."
        )


def _render_conclusion(detail: Dict[str, Any]) -> None:
    components.section_header(
        "Conclusion, sufficiency and what the model says is missing",
        subtitle="The model's own words, unedited.",
    )
    left, right = st.columns([3, 2], gap="medium")
    with left:
        components.kv_grid(
            {
                "Assessment": detail.get("assessment", ""),
                "Finding": detail.get("finding", ""),
                "Risk statement": detail.get("risk", ""),
                "Reasoning": detail.get("reasoning", ""),
                "Recommendation": detail.get("recommendation", ""),
            }
        )
    with right:
        components.metric_row(
            [
                {
                    "label": "Evidence sufficiency",
                    "value": detail.get("evidence_sufficiency", "-"),
                    "color": theme.sufficiency_color(detail.get("evidence_sufficiency")),
                    "caption": "The model's judgement of whether the evidence supplied can settle this control.",
                },
                {
                    "label": "Confidence",
                    "value": detail.get("confidence", "-"),
                    "color": theme.confidence_color(detail.get("confidence")),
                    "caption": "Self-reported. It is not a calibrated probability.",
                },
            ],
            columns=2,
        )
        missing = list(detail.get("missing_evidence", []) or [])
        st.markdown("**Evidence the model says it did not have**")
        if missing:
            for item in missing:
                st.markdown("- {0}".format(item))
            st.caption(
                "This is the request list an auditor would send to the control owner. It "
                "is also the reason a conclusion here may be provisional."
            )
        else:
            st.caption("The model named nothing as missing, which is itself worth checking.")


def _render_risk(detail: Dict[str, Any]) -> None:
    """The prototype risk rating with the arithmetic that produced it."""
    payload = dict(detail.get("risk_factors") or {})
    # The engine stores RiskAssessment.to_dict(), whose factor detail sits one level
    # down under "factors"; a hand-built or older row may carry the inner shape
    # directly. Both are accepted so the panel never renders empty on a valid row.
    inner = dict(payload.get("factors") or payload)
    values = dict(inner.get("values") or {})
    weights = dict(inner.get("weights") or {})
    contributions = dict(inner.get("contributions") or {})
    bands = dict(payload.get("band_thresholds") or inner.get("band_thresholds") or {})
    rationale = str(payload.get("rationale") or "")

    components.section_header(
        "Prototype risk rating",
        subtitle="Five weighted factors, each 1-5, scored 0-100 and banded.",
    )
    if not values:
        st.caption("No risk factor breakdown was recorded for this assessment.")
        components.note(components.RISK_MODEL_NOTE)
        return

    pills = [
        components.risk_badge(detail.get("risk_level"), detail.get("risk_score")),
        components.plain_badge(
            "exception ratio {0:.1%}".format(float(inner["exception_ratio"]))
            if inner.get("exception_ratio") is not None
            else "no exception ratio in the evidence"
        ),
    ]
    if inner.get("model_suggested_risk_level"):
        pills.append(
            components.plain_badge(
                "model proposed {0}".format(inner["model_suggested_risk_level"]), theme.AI_COLOR
            )
        )
    components.badges(*pills)

    rows = [
        {
            "factor": name.replace("_", " "),
            "value_1_5": float(values.get(name, 0.0)),
            "weight": float(weights.get(name, 0.0)),
            "points": float(contributions.get(name, 0.0)),
        }
        for name in sorted(values, key=lambda key: -float(contributions.get(key, 0.0)))
    ]
    left, right = st.columns([3, 2], gap="medium")
    with left:
        components.df_table(
            rows,
            columns=["factor", "value_1_5", "weight", "points"],
            column_config={
                "factor": st.column_config.TextColumn("Factor", width="medium"),
                "value_1_5": st.column_config.NumberColumn("Value (1-5)", format="%.1f", width="small"),
                "weight": st.column_config.NumberColumn("Weight", format="%.2f", width="small"),
                "points": st.column_config.ProgressColumn(
                    "Points of 100", min_value=0.0, max_value=40.0, format="%.1f"
                ),
            },
            key="assess_risk_factors",
        )
        st.caption(str(inner.get("scale", "")))
    with right:
        if bands:
            components.kv_grid(
                {
                    "Band thresholds": ", ".join(
                        "{0} >= {1:.0f}".format(key, float(value)) for key, value in bands.items()
                    ),
                    "Raw score": inner.get("raw_score", ""),
                    "Final score": inner.get("final_score", ""),
                }
            )
        for adjustment in inner.get("adjustments", []) or []:
            st.caption(
                "Adjustment - {0}: {1}".format(
                    adjustment.get("name", ""), adjustment.get("detail", "")
                )
            )
        if inner.get("model_agrees_with_computed") is False:
            st.info(
                "The model's own risk level differs from the computed band. The computed "
                "band stands; the disagreement is recorded for the reviewer rather than "
                "resolved automatically."
            )

    with st.expander("Full risk rationale", expanded=False):
        st.text(rationale or "No rationale was recorded.")
    components.note(components.RISK_MODEL_NOTE)


def _render_citations(detail: Dict[str, Any]) -> None:
    citations = list(detail.get("citations") or [])
    components.section_header(
        "Cited evidence",
        subtitle="Each quotation with its locator, its verification verdict and the stored chunk it came from.",
    )
    if not citations:
        st.caption("No citation was recorded, so there is nothing here to check.")
        return
    for position, citation in enumerate(citations, start=1):
        components.citation_card(
            citation, index=position, context_loader=data_access.chunk_context
        )


def _render_retrieval(detail: Dict[str, Any]) -> None:
    """What was actually put in front of the model, and what it went on to cite."""
    engine = dict((detail.get("validation_report") or {}).get("engine") or {})
    chunk_ids = [int(value) for value in (detail.get("retrieved_chunk_ids") or [])]
    cited_ids = {
        int(item["chunk_id"])
        for item in (detail.get("citations") or [])
        if item.get("chunk_id") is not None
    }

    components.section_header(
        "Retrieval transparency",
        subtitle="The evidence the model was shown, in the order it was ranked.",
    )
    components.kv_grid(
        {
            "Strategy": detail.get("retrieval_strategy", ""),
            "top_k": detail.get("retrieval_top_k", 0),
            "Chunks supplied": len(chunk_ids),
            "Chunks cited": len(cited_ids),
        }
    )
    if engine.get("raw_evidence_note"):
        components.note(str(engine["raw_evidence_note"]))
    raw_evidence = dict(engine.get("raw_evidence") or {})
    if raw_evidence.get("truncated"):
        st.warning(
            "The evidence did not fit the prompt: {0} of {1} chunk(s) were shown "
            "({2} of {3} characters). Anything in the dropped chunks could not have "
            "influenced this answer.".format(
                raw_evidence.get("chunks_shown", 0),
                raw_evidence.get("chunks_available", 0),
                raw_evidence.get("shown_chars", 0),
                raw_evidence.get("total_chars", 0),
            )
        )

    if not chunk_ids:
        st.caption("No chunk identifiers were recorded against this assessment.")
        return

    rows: List[Dict[str, Any]] = []
    for rank, chunk_id in enumerate(chunk_ids, start=1):
        try:
            chunk = data_access.get_chunk(chunk_id) or {}
        except data_access.DataAccessError:
            chunk = {}
        rows.append(
            {
                "rank": rank,
                "chunk_id": chunk_id,
                "cited": chunk_id in cited_ids,
                "filename": chunk.get("filename", "(chunk no longer stored)"),
                "locator": chunk.get("locator_text", ""),
                "source_type": chunk.get("source_type", ""),
                "chars": int(chunk.get("char_count", 0) or 0),
            }
        )
    components.df_table(
        rows,
        columns=["rank", "chunk_id", "cited", "filename", "locator", "source_type", "chars"],
        column_config={
            "rank": st.column_config.NumberColumn("Rank", width="small", format="%d"),
            "chunk_id": st.column_config.NumberColumn("Chunk", width="small", format="%d"),
            "cited": st.column_config.CheckboxColumn("Cited by the model", width="small"),
            "locator": st.column_config.TextColumn("Locator", width="large"),
            "source_type": st.column_config.TextColumn("Kind", width="small"),
            "chars": st.column_config.NumberColumn("Characters", width="small", format="%d"),
        },
        key="assess_retrieval",
    )
    components.note(
        "Rank is the order the retriever returned. Per-chunk retrieval scores are not "
        "persisted on the assessment row, so they cannot be shown here for a stored run - "
        "what is shown is which chunks were supplied and which of them the model went on "
        "to cite."
    )

    queries = list(detail.get("retrieval_queries") or [])
    if queries:
        with st.expander("The {0} queries retrieval was run with".format(len(queries)), expanded=False):
            for query in queries:
                st.markdown("- {0}".format(query))


def _render_validation(detail: Dict[str, Any]) -> None:
    """The mechanical grounding check, with the counts the rates are computed from."""
    validation = dict(detail.get("validation_report") or {})
    total = int(validation.get("total", 0) or 0)
    engine = dict(validation.get("engine") or {})

    components.section_header(
        "Citation validation",
        subtitle="Every quotation re-checked against the chunk it points at, after the model answered.",
    )
    components.metric_row(
        [
            {"label": "Citations", "value": total},
            {
                "label": "Verified",
                "value": validation.get("verified", 0),
                "color": theme.verdict_color("VERIFIED"),
            },
            {
                "label": "Partial",
                "value": validation.get("partial", 0),
                "color": theme.verdict_color("PARTIAL"),
                "caption": "Real retrieved text, not faithfully reproduced.",
            },
            {
                "label": "Fabricated",
                "value": validation.get("fabricated", 0),
                "color": theme.verdict_color("FABRICATED"),
                "caption": "No textual support in the chunk cited.",
            },
            {
                "label": "Grounding (strict)",
                "value": "{0:.0%}".format(float(validation.get("grounding_rate_strict", 0.0) or 0.0)),
                "caption": "verified / total. The figure to quote.",
            },
            {
                "label": "Grounding (headline)",
                "value": "{0:.0%}".format(float(validation.get("grounding_rate", 0.0) or 0.0)),
                "caption": "verified + half credit for partial.",
            },
        ],
        columns=6,
    )
    if total == 0:
        components.note(
            "Both rates are 0.0 because nothing was cited. Read them with the count: a "
            "rate of zero here means 'no citations', not 'nothing was grounded'."
        )

    # A citation that names no chunk is the fabrication signal under B and C - but under
    # A it is the condition itself, because A gives the model no chunk identifiers to
    # cite. Left unexplained, its 0% strict grounding rate reads as a quality finding
    # about the quotations, which it is not.
    citations = list(detail.get("citations") or [])
    unresolved = sum(1 for item in citations if item.get("chunk_id") is None)
    if unresolved and unresolved == len(citations) and str(
        detail.get("experiment_mode", "")
    ) == "A_RAW_LLM":
        components.note(
            "None of these citations names a stored chunk. Experiment A supplies no chunk "
            "identifiers, so the validator can confirm the quoted text appears in the "
            "evidence pasted into the prompt but cannot attribute it to a source - it "
            "records PARTIAL rather than VERIFIED. A strict grounding rate of 0% under "
            "this condition describes the condition, not the quality of the quotations, "
            "and an untraceable quotation is still untraceable."
        )
    elif unresolved:
        components.note(
            "{0} citation(s) name no stored chunk. Under a condition that supplies chunk "
            "identifiers, that is what a fabricated reference looks like.".format(unresolved)
        )

    claims = list(validation.get("unsupported_claims", []) or [])
    if claims:
        st.warning(
            "The validator found {0} number(s) in the narrative with no counterpart in "
            "the retrieved evidence:".format(len(claims))
        )
        for claim in claims:
            st.markdown("- {0}".format(claim))
    else:
        st.caption("No unsupported numeric claim was detected in the narrative.")

    if engine.get("rails_note"):
        components.note(str(engine["rails_note"]))
    if validation.get("threshold") is not None:
        components.note(
            "A quotation counts as VERIFIED at a similarity of {0:.0%} or above against "
            "its stored chunk, PARTIAL at half that. The check tests whether the quoted "
            "characters exist in the chunk cited - not whether the quote supports the "
            "conclusion drawn from it.".format(float(validation.get("threshold", 0.0) or 0.0))
        )


def _render_workflow(detail: Dict[str, Any]) -> None:
    """Per-step telemetry: which calls the condition actually made, and how long each took."""
    engine = dict((detail.get("validation_report") or {}).get("engine") or {})
    steps = list(engine.get("steps") or [])
    if not steps:
        return

    components.section_header(
        "Workflow steps",
        subtitle="What this experimental condition executed, in order.",
    )
    rows = [
        {
            "step": step.get("name", ""),
            "kind": step.get("kind", ""),
            "ok": bool(step.get("ok", True)),
            "latency_ms": int(step.get("latency_ms", 0) or 0),
            "prompt_tokens": int(step.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(step.get("completion_tokens", 0) or 0),
            "detail": ", ".join(
                "{0}={1}".format(key, value)
                for key, value in sorted((step.get("detail") or {}).items())
                if not isinstance(value, (list, dict))
            ),
        }
        for step in steps
    ]
    components.df_table(
        rows,
        columns=["step", "kind", "ok", "latency_ms", "prompt_tokens", "completion_tokens", "detail"],
        column_config={
            "step": st.column_config.TextColumn("Step", width="medium"),
            "kind": st.column_config.TextColumn("Kind", width="small"),
            "ok": st.column_config.CheckboxColumn("Completed", width="small"),
            "detail": st.column_config.TextColumn("Recorded detail", width="large"),
        },
        key="assess_steps",
    )
    latency = dict(engine.get("latency") or {})
    if latency.get("definition"):
        components.note(str(latency["definition"]))


def _render_provenance(detail: Dict[str, Any]) -> None:
    """Who produced this answer, at what cost, and the unedited output it came from."""
    components.section_header(
        "Provenance and raw output",
        subtitle="Kept verbatim so a researcher can audit the audit.",
    )
    components.kv_grid(
        {
            "Provider / model": "{0} / {1}".format(
                detail.get("llm_provider", ""), detail.get("llm_model", "")
            ),
            "Condition": detail.get("mode_label", detail.get("experiment_mode", "")),
            "Model calls": detail.get("llm_calls", 0),
            "Prompt / completion tokens": "{0} / {1}".format(
                detail.get("prompt_tokens", 0), detail.get("completion_tokens", 0)
            ),
            "End-to-end latency": "{0} ms".format(detail.get("latency_ms", 0)),
            "Project": detail.get("project_name", ""),
            "Evaluation run": detail.get("evaluation_run_id") or "not part of an experiment run",
        }
    )
    if str(detail.get("llm_provider", "")).lower() == "mock":
        components.note(components.MOCK_PROVIDER_NOTE)

    with st.expander("Raw model response", expanded=False):
        raw = detail.get("raw_response")
        if raw:
            st.code(str(raw), language="json")
        else:
            st.caption("No raw response was retained for this assessment.")
    with st.expander("Prompt sent to the model", expanded=False):
        prompt = detail.get("prompt_snapshot")
        if prompt:
            st.text(str(prompt))
        else:
            st.caption("No prompt snapshot was retained for this assessment.")


# ---- page
def render() -> None:
    components.section_header(
        "Assessments",
        subtitle="Run a control assessment, then read where every part of the answer came from.",
        eyebrow="Assessment",
    )
    project_id = _selected_project()
    if project_id is None:
        return

    try:
        scoped = data_access.list_scoped_controls(project_id)
        evidence = data_access.list_evidence(project_id=project_id)
        existing = data_access.list_assessments(project_id=project_id, limit=1)
    except data_access.DataAccessError as exc:
        st.error("Could not read this project: {0}".format(exc))
        return

    if not evidence:
        st.warning(
            "This project holds no evidence. An assessment run now would have nothing to "
            "read, and the honest result would be INSUFFICIENT_EVIDENCE for every control."
        )
        _page_link("pages/evidence.py", "Upload evidence", ":material/inventory_2:")

    # The run panel opens itself on an engagement that has never been assessed, and
    # stays shut once there are results to read.
    with st.expander("Run an assessment", expanded=not existing):
        _render_run_panel(project_id, scoped)

    st.markdown("")
    components.section_header(
        "Results",
        subtitle="Every assessment recorded for {0}.".format(state.current_project_name()),
    )
    selected = _render_results(project_id)

    current = state.current_assessment_id()
    if current is None and selected is not None:
        current = selected
    if current is None:
        return

    st.markdown("---")
    _render_detail(int(current))


render()
