"""Controls - the control library browser and editor.

The library is the audit programme: a control's objective, its assessment criteria and
the evidence it expects are what an assessment is judged *against*, and they are read
from here rather than from the model's own restatement of them (see
``data_access.build_four_way``). That makes this page the place where the standard being
applied is written down and can be argued with, which is the point of having a library
at all.

Two labels this page insists on
-------------------------------
* **Framework references are illustrative.** The bundled library carries ISO, NIST,
  COBIT and CIS strings so that a control looks like one an auditor would recognise, but
  they were written for this prototype and are not an authoritative mapping. Every one
  is shown under that heading, never as a compliance claim.
* **Privilege level and data sensitivity are model inputs, not facts.** They are 1-5
  scores that feed the prototype risk model (``app.audit.risk``), and they are labelled
  as such wherever they appear, because a number on a screen invites being read as a
  measurement.

Retiring rather than deleting
-----------------------------
There is no delete. ``set_control_active(False)`` retires a control: it disappears from
the pickers but every assessment and report that cited it still resolves to a row. A
control reference printed in a working paper must not become a dangling identifier.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from typing import Any, Callable, Dict, List, Optional, Sequence  # noqa: E402

import streamlit as st  # noqa: E402

from app.frontend import components, data_access, state, theme  # noqa: E402

PAGE = "controls"

#: 1-5 ladders shown next to the two exposure scores, so a reader knows what a 4 means.
_SCORE_HELP = (
    "1-5 input to the prototype risk model: higher means more privileged access or more "
    "sensitive data in the domain the control protects."
)


def _read(loader: Callable[[], Any], fallback: Any, what: str) -> Any:
    try:
        return loader()
    except data_access.DataAccessError as exc:
        st.error("Could not load {0}: {1}".format(what, exc))
        return fallback


def _lines(text: str) -> List[str]:
    """One entry per line - used for the long list fields."""
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def _terms(text: str) -> List[str]:
    """Comma- or newline-separated terms - used for retrieval keywords."""
    raw = str(text or "").replace("\n", ",")
    return [term.strip() for term in raw.split(",") if term.strip()]


# ---- library listing
def _filters() -> Dict[str, Any]:
    categories = _read(lambda: data_access.control_categories(active_only=False), [], "the categories")
    left, middle, right = st.columns([2, 3, 1], gap="small")
    with left:
        category = st.selectbox(
            "Category", options=["Any"] + list(categories), index=0, key="controls_category"
        )
    with middle:
        search = st.text_input(
            "Search",
            value=str(state.get_filter(PAGE, "search", "") or ""),
            placeholder="Reference, name or objective",
            key="controls_search",
        )
    with right:
        include_retired = st.checkbox("Include retired", value=False, key="controls_retired")
    state.set_filter(PAGE, "category", category)
    state.set_filter(PAGE, "search", search)
    return {
        "category": None if category == "Any" else category,
        "search": search.strip(),
        "active_only": not include_retired,
    }


def _library_table(controls: Sequence[Dict[str, Any]], project_id: Optional[int]) -> None:
    scoped_refs: set = set()
    latest: Dict[str, Dict[str, Any]] = {}
    if project_id is not None:
        scoped_refs = {
            str(row.get("control_id"))
            for row in _read(
                lambda: data_access.list_scoped_controls(project_id), [], "the project scope"
            )
        }
        latest = {
            str(row.get("control_ref", "")): row
            for row in _read(
                lambda: data_access.list_assessments(
                    project_id=project_id, latest_per_control=True
                ),
                [],
                "the assessments",
            )
        }

    components.df_table(
        [
            {
                "control_id": control.get("control_id"),
                "name": control.get("name"),
                "category": control.get("category"),
                "control_type": control.get("control_type"),
                "inherent_risk": control.get("inherent_risk"),
                "in_scope": str(control.get("control_id")) in scoped_refs,
                "status": (latest.get(str(control.get("control_id"))) or {}).get(
                    "status", "not assessed"
                ),
                "criteria": len(control.get("assessment_criteria", []) or []),
                "is_active": bool(control.get("is_active", True)),
            }
            for control in controls
        ],
        columns=[
            "control_id",
            "name",
            "category",
            "control_type",
            "inherent_risk",
            "in_scope",
            "status",
            "criteria",
            "is_active",
        ],
        column_config={
            # The shared default is "small", which clips a CONTROL-0NN reference.
            "control_id": st.column_config.TextColumn("Control", width="medium"),
            "control_type": st.column_config.TextColumn("Type", width="small"),
            "inherent_risk": st.column_config.TextColumn("Inherent risk", width="small"),
            "in_scope": st.column_config.CheckboxColumn("In scope", width="small"),
            "status": st.column_config.TextColumn("Latest AI status", width="medium"),
            "criteria": st.column_config.NumberColumn("Criteria", width="small", format="%d"),
            "is_active": st.column_config.CheckboxColumn("Active", width="small"),
        },
        empty_message="No control matches these filters.",
    )
    if project_id is None:
        st.caption(
            "Select an engagement in the sidebar to see which of these controls are in "
            "its scope and what the latest assessment concluded."
        )
    else:
        st.caption(
            "“In scope” and “Latest AI status” are relative to {0}. A status of "
            "“not assessed” means no assessment exists - not that the control passed.".format(
                state.current_project_name()
            )
        )


# ---- detail
def _detail(control: Dict[str, Any], project_id: Optional[int]) -> None:
    ref = str(control.get("control_id", ""))
    components.section_header(
        "{0}  {1}".format(ref, control.get("name", "")),
        subtitle=str(control.get("objective", "") or ""),
        eyebrow="Control definition",
    )
    in_scope = False
    if project_id is not None:
        in_scope = ref in {
            str(row.get("control_id"))
            for row in _read(
                lambda: data_access.list_scoped_controls(project_id), [], "the project scope"
            )
        }
    components.badges(
        components.plain_badge(str(control.get("category", ""))),
        components.plain_badge(str(control.get("control_type", ""))),
        components.plain_badge(str(control.get("control_frequency", ""))),
        components.risk_badge(control.get("inherent_risk")),
        components.plain_badge("in scope" if in_scope else "not in scope", theme.GREEN if in_scope else theme.GREY)
        if project_id is not None
        else "",
        components.plain_badge("retired", theme.AMBER) if not control.get("is_active", True) else "",
    )

    _scope_controls_row(ref, project_id, in_scope)
    _latest_assessment(ref, project_id)

    left, right = st.columns([3, 2], gap="medium")
    with left:
        st.markdown("**Description**")
        st.write(str(control.get("description", "") or "(none recorded)"))
        st.markdown("**Risk addressed**")
        st.write(str(control.get("risk_addressed", "") or "(none recorded)"))

        criteria = list(control.get("assessment_criteria", []) or [])
        st.markdown("**Assessment criteria**")
        if criteria:
            # Numbered because the criteria are referred to by number in an assessment
            # and in the four-way panel; an unordered list would lose that reference.
            for index, item in enumerate(criteria, start=1):
                st.markdown("{0}. {1}".format(index, item))
        else:
            st.caption(
                "No criteria recorded. A control with no testable criteria cannot be "
                "assessed consistently."
            )
    with right:
        expected = list(control.get("expected_evidence", []) or [])
        st.markdown("**Evidence this control expects**")
        if expected:
            for item in expected:
                st.markdown("- {0}".format(item))
        else:
            st.caption("None recorded.")

        st.markdown("**Framework references**")
        refs = list(control.get("framework_refs", []) or [])
        if refs:
            for item in refs:
                st.markdown("- {0}".format(item))
        else:
            st.caption("None recorded.")
        components.note(
            "Illustrative cross-references written for this research prototype. They are "
            "not an authoritative mapping to ISO/IEC 27001, NIST SP 800-53, COBIT or CIS, "
            "and this application cannot state compliance with any of them."
        )

        st.markdown("**Risk model inputs**")
        components.kv_grid(
            {
                "Inherent risk": control.get("inherent_risk", ""),
                "Privilege level": "{0} / 5".format(control.get("privilege_level", "")),
                "Data sensitivity": "{0} / 5".format(control.get("data_sensitivity", "")),
            }
        )
        components.note("{0} {1}".format(_SCORE_HELP, components.RISK_MODEL_NOTE))

        st.markdown("**Retrieval keywords**")
        keywords = list(control.get("retrieval_keywords", []) or [])
        if keywords:
            components.badges(*[components.plain_badge(word) for word in keywords])
            st.caption(
                "Seed terms for the retrieval queries built for this control "
                "(app.rag.query_builder). They steer which evidence is put in front of "
                "the model; they are not part of the audit requirement."
            )
        else:
            st.caption("None recorded - retrieval will fall back to the control's own wording.")

    components.kv_grid(
        {
            "Library source": control.get("source", ""),
            "Scoped to": "{0} project(s)".format(control.get("scoped_project_count", 0)),
            "Created": control.get("created_at", ""),
            "Last updated": control.get("updated_at", ""),
        }
    )

    with st.expander("Edit this control", expanded=False):
        _edit_form(control)
    _retirement(control)


def _scope_controls_row(ref: str, project_id: Optional[int], in_scope: bool) -> None:
    if project_id is None:
        return
    left, right = st.columns([1, 4], gap="small")
    with left:
        if in_scope:
            if st.button("Remove from scope", key="controls_unscope_{0}".format(ref), width="stretch"):
                _unscope(project_id, ref)
        else:
            if st.button(
                "Add to scope",
                key="controls_scope_{0}".format(ref),
                type="primary",
                width="stretch",
            ):
                _scope(project_id, ref)
    with right:
        st.caption("Scope is relative to {0}.".format(state.current_project_name()))


def _scope(project_id: int, ref: str) -> None:
    try:
        data_access.scope_controls(project_id, [ref], actor=state.auditor_name())
    except data_access.DataAccessError as exc:
        st.error("Could not add {0} to scope: {1}".format(ref, exc))
        return
    state.flash("{0} is now in scope for {1}.".format(ref, state.current_project_name()), "success")
    st.rerun()


def _unscope(project_id: int, ref: str) -> None:
    try:
        data_access.unscope_control(project_id, ref, actor=state.auditor_name())
    except data_access.DataAccessError as exc:
        st.error("Could not remove {0} from scope: {1}".format(ref, exc))
        return
    state.flash(
        "{0} is out of scope. Assessments already written against it are unchanged.".format(ref),
        "success",
    )
    st.rerun()


def _latest_assessment(ref: str, project_id: Optional[int]) -> None:
    if project_id is None:
        return
    row = _read(
        lambda: data_access.latest_assessment_for_control(project_id, ref),
        None,
        "the latest assessment",
    )
    if not row:
        st.caption(
            "No assessment has been run for this control in {0}.".format(
                state.current_project_name()
            )
        )
        return
    components.ai_disclaimer_banner(
        "Latest machine-generated assessment of this control in this engagement.",
        mode=row.get("experiment_mode"),
        compact=True,
    )
    components.badges(
        components.status_badge(row.get("status")),
        components.risk_badge(row.get("risk_level"), row.get("risk_score")),
        components.confidence_badge(row.get("confidence")),
        components.sufficiency_badge(row.get("evidence_sufficiency")),
        components.decision_badge(row.get("review_decision")),
        components.plain_badge("{0} citation(s)".format(row.get("citation_count", 0))),
    )
    finding = str(row.get("finding", "") or "")
    if finding:
        st.markdown(
            '<div class="ia-note">{0}</div>'.format(components.escape(finding)),
            unsafe_allow_html=True,
        )
    st.caption(
        "Open the Assessments or Human Review page to see the cited evidence and record "
        "a decision. Nothing here is an audit conclusion."
    )


# ---- create and edit
def _control_form(prefix: str, control: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Shared field set for creating and editing. Returns the payload on submit."""
    data = dict(control or {})
    editing = bool(control)
    risk_levels = data_access.risk_levels()
    current_risk = str(data.get("inherent_risk", "MEDIUM"))

    with st.form("{0}_form".format(prefix)):
        top_left, top_right = st.columns([1, 2], gap="small")
        with top_left:
            ref = st.text_input(
                "Control reference",
                value=str(data.get("control_id", "")),
                disabled=editing,
                help=(
                    "Immutable once created: it is the identifier printed in every "
                    "assessment and report."
                    if editing
                    else "For example CONTROL-015."
                ),
            )
        with top_right:
            name = st.text_input("Name", value=str(data.get("name", "")))
        objective = st.text_area("Objective", value=str(data.get("objective", "")), height=80)
        description = st.text_area("Description", value=str(data.get("description", "")), height=110)
        risk_addressed = st.text_area(
            "Risk addressed", value=str(data.get("risk_addressed", "")), height=80
        )
        criteria = st.text_area(
            "Assessment criteria (one per line)",
            value="\n".join(data.get("assessment_criteria", []) or []),
            height=140,
            help="Each line is one testable criterion. These are what the evidence is judged against.",
        )
        expected = st.text_area(
            "Expected evidence (one per line)",
            value="\n".join(data.get("expected_evidence", []) or []),
            height=120,
        )
        framework = st.text_area(
            "Framework references (one per line)",
            value="\n".join(data.get("framework_refs", []) or []),
            height=80,
            help=(
                "Illustrative only. This prototype does not certify compliance with any "
                "framework, so label anything entered here accordingly."
            ),
        )
        keywords = st.text_area(
            "Retrieval keywords (comma or newline separated)",
            value=", ".join(data.get("retrieval_keywords", []) or []),
            height=70,
            help="Seed terms for retrieval - column headings and domain vocabulary work best.",
        )
        row = st.columns(3, gap="small")
        with row[0]:
            category = st.text_input("Category", value=str(data.get("category", "General")))
        with row[1]:
            control_type = st.text_input(
                "Control type", value=str(data.get("control_type", "Preventive"))
            )
        with row[2]:
            frequency = st.text_input(
                "Frequency", value=str(data.get("control_frequency", "Continuous"))
            )
        scores = st.columns(3, gap="small")
        with scores[0]:
            inherent = st.selectbox(
                "Inherent risk",
                options=risk_levels,
                index=risk_levels.index(current_risk) if current_risk in risk_levels else 0,
            )
        with scores[1]:
            privilege = st.slider(
                "Privilege level",
                min_value=1,
                max_value=5,
                value=int(data.get("privilege_level", 3) or 3),
                help=_SCORE_HELP,
            )
        with scores[2]:
            sensitivity = st.slider(
                "Data sensitivity",
                min_value=1,
                max_value=5,
                value=int(data.get("data_sensitivity", 3) or 3),
                help=_SCORE_HELP,
            )
        submitted = st.form_submit_button(
            "Save changes" if editing else "Add to the library", type="primary"
        )

    if not submitted:
        return None
    payload: Dict[str, Any] = {
        "name": name.strip(),
        "objective": objective.strip(),
        "description": description.strip(),
        "risk_addressed": risk_addressed.strip(),
        "assessment_criteria": _lines(criteria),
        "expected_evidence": _lines(expected),
        "framework_refs": _lines(framework),
        "retrieval_keywords": _terms(keywords),
        "category": category.strip() or "General",
        "control_type": control_type.strip() or "Preventive",
        "control_frequency": frequency.strip() or "Continuous",
        "inherent_risk": inherent,
        "privilege_level": int(privilege),
        "data_sensitivity": int(sensitivity),
    }
    if not editing:
        payload["control_id"] = ref.strip().upper()
    return payload


def _create_form() -> None:
    with st.expander("Add a control to the library", expanded=False):
        st.caption(
            "A control added here behaves exactly like a seeded one: it can be scoped to "
            "an engagement and assessed. Its criteria are what the assessment is judged "
            "against, so write them as testable statements."
        )
        payload = _control_form("control_create")
        if payload is None:
            return
        if not payload.get("control_id") or not payload.get("name"):
            st.error("A control needs a reference and a name.")
            return
        try:
            control = data_access.create_control(payload, actor=state.auditor_name())
        except data_access.DataAccessError as exc:
            st.error("Could not create the control: {0}".format(exc))
            return
        state.set_current_control_ref(str(control.get("control_id", "")))
        state.flash("Added {0} to the library.".format(control.get("control_id", "")), "success")
        st.rerun()


def _edit_form(control: Dict[str, Any]) -> None:
    payload = _control_form("control_edit_{0}".format(control.get("control_id", "")), control)
    if payload is None:
        return
    if not payload.get("name"):
        st.error("A control needs a name.")
        return
    try:
        data_access.update_control(
            control.get("control_id"), actor=state.auditor_name(), **payload
        )
    except data_access.DataAccessError as exc:
        st.error("Could not save the control: {0}".format(exc))
        return
    state.flash("Saved {0}.".format(control.get("control_id", "")), "success")
    st.rerun()


def _retirement(control: Dict[str, Any]) -> None:
    ref = str(control.get("control_id", ""))
    active = bool(control.get("is_active", True))
    with st.expander("Retire or restore this control", expanded=False):
        if active:
            st.caption(
                "Retiring removes the control from the pickers and from new project "
                "scoping. Nothing is deleted: assessments and reports that reference {0} "
                "still resolve to it.".format(ref)
            )
            confirm = st.checkbox(
                "I understand this control will no longer be offered for scoping.",
                key="control_retire_confirm_{0}".format(ref),
            )
            if st.button("Retire {0}".format(ref), key="control_retire_{0}".format(ref), disabled=not confirm):
                _set_active(ref, False)
        else:
            st.caption("This control is retired and is hidden from the pickers.")
            if st.button("Restore {0}".format(ref), key="control_restore_{0}".format(ref), type="primary"):
                _set_active(ref, True)


def _set_active(ref: str, active: bool) -> None:
    try:
        data_access.set_control_active(ref, active, actor=state.auditor_name())
    except data_access.DataAccessError as exc:
        st.error("Could not change {0}: {1}".format(ref, exc))
        return
    state.flash("{0} is now {1}.".format(ref, "active" if active else "retired"), "success")
    st.rerun()


# ---- page
def render() -> None:
    components.section_header(
        "Control library",
        subtitle="The audit programme: what each control requires and how it is tested.",
        eyebrow="Engagement",
    )
    project_id = state.current_project_id()
    filters = _filters()
    controls = _read(
        lambda: data_access.list_controls(
            category=filters["category"],
            active_only=filters["active_only"],
            search=filters["search"],
        ),
        [],
        "the control library",
    )
    _library_table(controls, project_id)
    _create_form()

    if not controls:
        return

    st.markdown("---")
    refs = [str(control.get("control_id")) for control in controls]
    remembered = state.current_control_ref()
    chosen = st.selectbox(
        "Open a control",
        options=refs,
        index=refs.index(remembered) if remembered in refs else 0,
        format_func=lambda ref: "{0}  {1}".format(
            ref,
            next(
                (c.get("name", "") for c in controls if str(c.get("control_id")) == ref),
                "",
            ),
        ),
        key="controls_detail_select",
    )
    state.set_current_control_ref(chosen)
    control = next((c for c in controls if str(c.get("control_id")) == chosen), None)
    if control is None:
        st.caption("That control is no longer in the filtered list.")
        return
    _detail(control, project_id)


if __name__ == "__main__":
    render()
