"""The plain-language assessment screen, shared by the Assessments and Human Review pages.

One AI assessment, laid out the way an auditor reads it: what we found, then four
numbered sections that keep four kinds of statement apart - what the control requires
(from the control library, not the model), what the evidence shows (verbatim quotes,
each mechanically re-checked), what the AI infers (unproven by construction), and what
still needs a human to verify - and then the auditor's own decision, on the same screen.
The research detail (risk arithmetic, retrieval, validation counts, workflow telemetry,
raw response and prompt) is kept intact but folded away under one expander at the
bottom, so it is there for whoever needs it and out of the way for everyone else.

Both pages call :func:`render_assessment`; neither draws the assessment itself any more.
That is what keeps the two views identical, and it is what guarantees the rails hold on
both: exactly one AI disclaimer banner above the AI output, every model string escaped
before it touches HTML, a fabricated citation shown as a failure rather than as proof,
INSUFFICIENT_EVIDENCE framed as "we cannot tell" rather than as a deficiency, and the
AI record and the human record displayed side by side and never merged.

Nothing here runs anything. The screen renders what is stored; the only write it can
cause is the auditor's decision, and that requires the auditor to press the button in
the form.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import streamlit as st

from app.frontend import components, data_access, review_form, state, theme

_esc = components.escape


# ---- small plain-language primitives (all escaped)
def _para(text: Any, muted: bool = False) -> None:
    value = str(text or "").strip()
    if not value:
        return
    st.markdown(
        '<div class="ia-text{0}">{1}</div>'.format(" ia-text-muted" if muted else "", _esc(value)),
        unsafe_allow_html=True,
    )


def _bullets(items: Sequence[Any], muted: bool = False) -> None:
    entries = [str(item).strip() for item in (items or []) if str(item or "").strip()]
    if not entries:
        return
    st.markdown(
        '<ul class="ia-list{0}">{1}</ul>'.format(
            " ia-list-muted" if muted else "",
            "".join("<li>{0}</li>".format(_esc(item)) for item in entries),
        ),
        unsafe_allow_html=True,
    )


def _eyebrow(text: str) -> None:
    st.markdown('<div class="ia-eyebrow">{0}</div>'.format(_esc(text)), unsafe_allow_html=True)


def _completed_review(detail: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The latest review when it carries a real decision; None while it is PENDING."""
    review = dict(detail.get("latest_review") or {})
    if not review:
        return None
    if str(review.get("decision", "PENDING") or "PENDING") == "PENDING":
        return None
    return review


def ensure_control(detail: Dict[str, Any]) -> Dict[str, Any]:
    """Make sure section 1 carries the control library's own wording.

    ``data_access.get_assessment`` attaches the control definition in-process, but the
    API payload has no nested control, so over HTTP the REQUIRES section would hold
    nothing but the model's restatement - precisely the substitution it exists to
    prevent. One cached read through the facade keeps both transports showing the same
    thing. Replaces the ``_with_control`` helpers the two pages used to carry.
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


# ---- 1. header
def _render_header(detail: Dict[str, Any], review: Optional[Dict[str, Any]]) -> None:
    components.section_header(
        "{0} {1}".format(detail.get("control_ref", ""), detail.get("control_name", "")).strip(),
        subtitle="Assessment #{0}{1}".format(
            detail.get("id", ""),
            " · produced " + components.when(detail.get("created_at")) if detail.get("created_at") else "",
        ),
        eyebrow="AI-generated assessment - not an audit conclusion",
    )
    components.badges(
        components.status_badge(detail.get("status")),
        components.risk_badge(detail.get("risk_level"), detail.get("risk_score")),
        components.sufficiency_badge(detail.get("evidence_sufficiency")),
        components.decision_badge(review.get("decision")) if review else "",
    )
    # The one banner above the AI output. Never removed; context is appended to it.
    components.ai_disclaimer_banner(
        compact=True,
        mode=detail.get("experiment_mode"),
        provider=str(detail.get("llm_provider", "") or ""),
        model=str(detail.get("llm_model", "") or ""),
    )


# ---- 2. alerts
def _render_alerts(detail: Dict[str, Any]) -> None:
    """Everything a reviewer must not be allowed to scroll past."""
    validation = dict(detail.get("validation_report") or detail.get("validation") or {})
    fabricated = int(validation.get("fabricated", 0) or 0)
    total = int(validation.get("total", 0) or 0)

    if fabricated:
        st.error(
            "FABRICATED CITATION: {0} of {1} quote(s) in this assessment could not be "
            "found in the evidence they point at. Treat the conclusion as unsupported "
            "until each one has been checked by hand - a fabricated citation is not a "
            "formatting problem, it is the AI asserting evidence that is not there.".format(
                fabricated, total
            )
        )
    if detail.get("error"):
        st.error("The engine recorded an error on this run: {0}".format(detail["error"]))

    rails = list(validation.get("rails_applied", []) or [])
    if validation.get("status_downgraded"):
        st.warning(
            "A safety rail changed the conclusion. The model said {0}; the system "
            "recorded {1} because the evidence did not support the stronger claim.".format(
                components.label("status", validation.get("original_status")) or "an unknown status",
                components.label("status", detail.get("status")) or "an unknown status",
            )
        )
    else:
        # Rails arrive as "rail_id: sentence". The always-present human-review rail is
        # what the banner above already says, so only the rails that changed or
        # annotated this particular assessment are listed, as plain sentences.
        notable = []
        for item in rails:
            text = str(item)
            rail_id, _, sentence = text.partition(":")
            if rail_id.strip() == "human_review_enforced":
                continue
            notable.append((sentence or text).strip().rstrip("."))
        if notable:
            st.info("Safety checks applied to this assessment:\n\n" + "\n".join("- " + n + "." for n in notable))

    if total == 0:
        st.warning(
            "This assessment quotes no evidence at all. Nothing in it is traceable to a "
            "source document."
        )


# ---- 3. what did we find
def _render_finding(detail: Dict[str, Any]) -> None:
    components.section_header("What did we find?", subtitle="The AI's own words, unedited.")
    finding = str(detail.get("finding", "") or "").strip()
    summary = str(detail.get("assessment", "") or "").strip()
    if finding:
        _para(finding)
        if summary and summary != finding:
            _para(summary, muted=True)
    elif summary:
        _para(summary)
    else:
        st.caption("The AI recorded no finding text for this control.")

    recommendation = str(detail.get("recommendation", "") or "").strip()
    if recommendation:
        _eyebrow("Suggested next step (AI)")
        _para(recommendation)


# ---- 4. the four sections
def _render_requires(requires: Dict[str, Any]) -> None:
    with st.container(border=True):
        st.markdown("#### 1. What the control requires")
        st.caption("From the control library - not from the AI.")
        objective = str(requires.get("objective") or "").strip()
        criteria = [str(item) for item in (requires.get("criteria") or [])]
        expected = [str(item) for item in (requires.get("expected_evidence") or [])]
        refs = [str(item) for item in (requires.get("framework_refs") or [])]
        if objective:
            _para(objective)
        if criteria:
            _eyebrow("Assessment criteria")
            _bullets(criteria)
        if expected:
            _eyebrow("Evidence the control expects")
            _bullets(expected, muted=True)
        if refs:
            st.caption("References: {0}".format(", ".join(refs)))
        if not (objective or criteria or expected):
            st.caption("No requirement wording is recorded for this control in the library.")
        restatement = str(requires.get("model_restatement") or "").strip()
        if restatement:
            components.note("The AI restated the requirement as: " + restatement)


def _quotes_caption(proves: Dict[str, Any]) -> str:
    total = int(proves.get("total", 0) or 0)
    verified = int(proves.get("verified", 0) or 0)
    partial = int(proves.get("partial", 0) or 0)
    fabricated = int(proves.get("fabricated", 0) or 0)
    if total == 0:
        return "No quotes were taken from the evidence."
    text = "{0} of {1} quote{2} were found verbatim in the evidence".format(
        verified, total, "" if total == 1 else "s"
    )
    extras: List[str] = []
    if partial:
        extras.append("{0} partly matched".format(partial))
    if fabricated:
        extras.append("{0} fabricated".format(fabricated))
    if extras:
        text += " ({0})".format(", ".join(extras))
    return text + "."


def _render_proves(proves: Dict[str, Any]) -> None:
    citations = [dict(item) for item in (proves.get("citations") or [])]
    with st.container(border=True):
        st.markdown("#### 2. What the evidence shows")
        st.caption(_quotes_caption(proves))
        statement = str(proves.get("statement") or "").strip()
        if statement:
            _para(statement, muted=True)
        if not citations:
            st.caption("No evidence was cited. Nothing here is proven.")
            return
        for position, citation in enumerate(citations, start=1):
            components.citation_card(
                citation,
                index=position,
                context_loader=data_access.chunk_context,
                expander_label="Show the source passage",
                highlight=True,
            )


def _render_infers(infers: Dict[str, Any]) -> None:
    with st.container(border=True):
        st.markdown("#### 3. What the AI infers")
        st.caption("The AI's conclusions. Nothing here is established by the quotes above.")
        components.badges(
            components.status_badge(infers.get("status")),
            components.risk_badge(infers.get("risk_level"), infers.get("risk_score")),
            components.confidence_badge(infers.get("confidence")),
        )
        reasoning = str(infers.get("reasoning") or "").strip()
        if reasoning:
            _para(reasoning)
        risk_statement = str(infers.get("risk") or "").strip()
        if risk_statement:
            _eyebrow("Risk, as the AI describes it")
            _para(risk_statement, muted=True)
        inferences = [str(item) for item in (infers.get("inferences") or [])]
        if inferences:
            _eyebrow("Inferences")
            _bullets(inferences)
        if not (reasoning or risk_statement or inferences):
            st.caption("No inference was recorded beyond the cited evidence.")
        components.note("Confidence is self-reported by the AI. It is not a calibrated probability.")


def _render_human(human: Dict[str, Any], review: Optional[Dict[str, Any]]) -> None:
    with st.container(border=True):
        st.markdown("#### 4. What still needs verification")
        st.caption("What the system says it cannot settle - outstanding before this can become an audit conclusion.")
        items = [str(item) for item in (human.get("items") or [])]
        missing = [str(item) for item in (human.get("missing_evidence") or [])]
        claims = [str(item) for item in (human.get("unsupported_claims") or [])]
        if items:
            _eyebrow("Points the AI says a person must check")
            _bullets(items)
        if missing:
            _eyebrow("Evidence the AI says it did not have")
            _bullets(missing)
            st.caption("This is the request list an auditor would send to the control owner.")
        if claims:
            _eyebrow("Numbers with no counterpart in the evidence")
            _bullets(claims, muted=True)
        if not (items or missing or claims):
            st.caption("The AI listed nothing outstanding - which is itself worth checking.")
        if review:
            components.note(
                "An auditor has recorded a decision on this assessment: {0}.".format(
                    components.label("decision", review.get("decision"))
                )
            )
        else:
            components.note(
                "No auditor decision has been recorded. This is not an audit conclusion."
            )


def _render_four_sections(
    detail: Dict[str, Any], review: Optional[Dict[str, Any]], key_prefix: str
) -> None:
    four_way = dict(detail.get("four_way") or {}) or data_access.build_four_way(detail)
    _render_requires(dict(four_way.get("requires") or {}))
    _render_proves(dict(four_way.get("proves") or {}))
    _render_infers(dict(four_way.get("infers") or {}))
    _render_human(dict(four_way.get("human") or {}), review)


# ---- 5. auditor review
def _render_review(
    detail: Dict[str, Any],
    review: Optional[Dict[str, Any]],
    reviewer: str,
    show_review_form: bool,
    key_prefix: str,
) -> None:
    components.section_header(
        "Auditor review",
        subtitle="Your decision is recorded as a separate record; the AI output is never overwritten.",
    )
    if review:
        components.ai_vs_human_panel(detail)
        if show_review_form:
            with st.expander("Record a new decision", expanded=False):
                if review_form.render_review_form(detail, reviewer, key_prefix=key_prefix):
                    st.rerun()
        return
    if show_review_form:
        if review_form.render_review_form(detail, reviewer, key_prefix=key_prefix):
            st.rerun()
    else:
        st.caption("No auditor decision has been recorded yet.")


# ---- 6. details for researchers
def _render_risk(detail: Dict[str, Any], key_prefix: str) -> None:
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
                "model proposed {0}".format(
                    components.label("risk", inner["model_suggested_risk_level"])
                ),
                theme.AI_COLOR,
            )
        )
    components.badges(*pills)

    rows = [
        {
            "factor": str(name).replace("_", " "),
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
            key="{0}_risk_factors_{1}".format(key_prefix, detail.get("id")),
        )
        st.caption(str(inner.get("scale", "")))
    with right:
        if bands:
            components.kv_grid(
                {
                    "Band thresholds": ", ".join(
                        "{0} >= {1:.0f}".format(components.label("risk", key), float(value))
                        for key, value in bands.items()
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

    # Not an expander: this whole tab already sits inside one, and Streamlit does not
    # nest expanders.
    with st.popover("Full risk rationale"):
        st.text(rationale or "No rationale was recorded.")
    components.note(components.RISK_MODEL_NOTE)


def _render_retrieval(detail: Dict[str, Any], key_prefix: str) -> None:
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
        key="{0}_retrieval_{1}".format(key_prefix, detail.get("id")),
    )
    components.note(
        "Rank is the order the retriever returned. Per-chunk retrieval scores are not "
        "persisted on the assessment row, so they cannot be shown here for a stored run - "
        "what is shown is which chunks were supplied and which of them the model went on "
        "to cite."
    )

    queries = [str(item) for item in (detail.get("retrieval_queries") or [])]
    if queries:
        with st.popover("The {0} queries retrieval was run with".format(len(queries))):
            _bullets(queries)


def _render_validation(detail: Dict[str, Any]) -> None:
    """The mechanical grounding check, with the counts the rates are computed from."""
    validation = dict(detail.get("validation_report") or detail.get("validation") or {})
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
            "None of these citations names a stored chunk. Mode A supplies no chunk "
            "identifiers, so the validator can confirm the quoted text appears in the "
            "evidence pasted into the prompt but cannot attribute it to a source - it "
            "records a partial match rather than a verified one. A strict grounding rate "
            "of 0% under this condition describes the condition, not the quality of the "
            "quotations, and an untraceable quotation is still untraceable."
        )
    elif unresolved:
        components.note(
            "{0} citation(s) name no stored chunk. Under a condition that supplies chunk "
            "identifiers, that is what a fabricated reference looks like.".format(unresolved)
        )

    claims = [str(item) for item in (validation.get("unsupported_claims", []) or [])]
    if claims:
        st.warning(
            "The validator found {0} number(s) in the narrative with no counterpart in "
            "the retrieved evidence:".format(len(claims))
        )
        _bullets(claims)
    else:
        st.caption("No unsupported numeric claim was detected in the narrative.")

    if engine.get("rails_note"):
        components.note(str(engine["rails_note"]))
    if validation.get("threshold") is not None:
        components.note(
            "A quotation counts as verified at a similarity of {0:.0%} or above against "
            "its stored chunk, partly matched at half that. The check tests whether the "
            "quoted characters exist in the chunk cited - not whether the quote supports "
            "the conclusion drawn from it.".format(float(validation.get("threshold", 0.0) or 0.0))
        )


def _render_workflow(detail: Dict[str, Any], key_prefix: str) -> None:
    """Per-step telemetry: which calls the condition actually made, and how long each took."""
    engine = dict((detail.get("validation_report") or {}).get("engine") or {})
    steps = list(engine.get("steps") or [])
    components.section_header(
        "Workflow steps",
        subtitle="What this pipeline configuration executed, in order.",
    )
    if not steps:
        st.caption("No per-step telemetry was recorded for this assessment.")
        return
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
        key="{0}_steps_{1}".format(key_prefix, detail.get("id")),
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
    provider = str(detail.get("llm_provider", "") or "")
    components.kv_grid(
        {
            "Provider / model": "{0} / {1}".format(
                components.provider_display_name(provider) if provider else "",
                detail.get("llm_model", ""),
            ),
            "Pipeline configuration": components.label("mode", detail.get("experiment_mode")),
            "Configuration code": detail.get("experiment_mode", ""),
            "Model calls": detail.get("llm_calls", 0),
            "Prompt / completion tokens": "{0} / {1}".format(
                detail.get("prompt_tokens", 0), detail.get("completion_tokens", 0)
            ),
            "End-to-end latency": "{0} ms".format(detail.get("latency_ms", 0)),
            "Project": detail.get("project_name", ""),
            "Evaluation run": detail.get("evaluation_run_id") or "not part of an experiment run",
        }
    )
    if provider.lower() == "mock":
        components.note(components.MOCK_PROVIDER_NOTE)

    # Popovers rather than expanders: this tab already sits inside an expander.
    raw_col, prompt_col = st.columns(2, gap="small")
    with raw_col:
        with st.popover("Raw model response"):
            raw = detail.get("raw_response")
            if raw:
                st.code(str(raw), language="json")
            else:
                st.caption("No raw response was retained for this assessment.")
    with prompt_col:
        with st.popover("Prompt sent to the model"):
            prompt = detail.get("prompt_snapshot")
            if prompt:
                st.text(str(prompt))
            else:
                st.caption("No prompt snapshot was retained for this assessment.")


def _render_research_details(detail: Dict[str, Any], key_prefix: str) -> None:
    with st.expander(
        "Details for researchers (risk arithmetic, retrieval, validation, prompt)", expanded=False
    ):
        tabs = st.tabs(
            ["Risk arithmetic", "Retrieval", "Citation validation", "Workflow steps", "Provenance and raw output"]
        )
        with tabs[0]:
            _render_risk(detail, key_prefix)
        with tabs[1]:
            _render_retrieval(detail, key_prefix)
        with tabs[2]:
            _render_validation(detail)
        with tabs[3]:
            _render_workflow(detail, key_prefix)
        with tabs[4]:
            _render_provenance(detail)


# ---- the screen
def render_assessment(
    detail: Dict[str, Any],
    reviewer: str = "",
    show_review_form: bool = True,
    key_prefix: str = "assess",
) -> None:
    """Draw the whole plain-language assessment screen for one assessment.

    ``detail`` is the mapping from ``data_access.get_assessment`` (the control definition
    is fetched if it is missing, see :func:`ensure_control`). ``reviewer`` is the
    auditor's declared name, passed to the decision form; with a blank name the form's
    button is disabled and says why. ``show_review_form=False`` renders the auditor
    section read-only. ``key_prefix`` namespaces every widget key so the screen can be
    hosted by more than one page.

    Order, top to bottom: header with badges and the single AI disclaimer banner; the
    alerts nobody may scroll past; what the AI found; the four numbered sections; the
    auditor's review (side-by-side panel when a decision exists, then the form); and the
    research detail folded under one expander.
    """
    detail = ensure_control(dict(detail))
    review = _completed_review(detail)

    _render_header(detail, review)
    _render_alerts(detail)
    _render_finding(detail)
    st.markdown("")
    _render_four_sections(detail, review, key_prefix)
    st.markdown("")
    _render_review(detail, review, reviewer, show_review_form, key_prefix)
    st.markdown("")
    _render_research_details(detail, key_prefix)


__all__ = ["ensure_control", "render_assessment"]
