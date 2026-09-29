"""Controls - the control library browser, scoping selector and editor.

The library is the audit programme: a control's objective, its assessment criteria and
the evidence it expects are what an assessment is judged *against*, and they are read
from here rather than from the model's own restatement of them (see
``data_access.build_four_way``). That makes this page the place where the standard being
applied is written down and can be argued with, which is the point of having a library
at all.

The table at the top doubles as the scope picker: tick the controls an audit project
should cover and press one button. The single-control detail below it is where a control
is read in full, edited or retired.

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

import re  # noqa: E402
from typing import Any, Callable, Dict, List, Optional, Sequence  # noqa: E402

import streamlit as st  # noqa: E402

from app.frontend import components, data_access, state, theme  # noqa: E402

PAGE = "controls"

#: Session key set just before the rerun that follows "Add selected", so the run after
#: it can draw the next-step links under the button. Popped on read.
_K_JUST_ADDED = "controls_just_added"

#: 1-5 ladders shown next to the two exposure scores, so a reader knows what a 4 means.
_SCORE_HELP = (
    "1-5 input to the prototype risk model: higher means more privileged access or more "
    "sensitive data in the domain the control protects."
)

#: The framework caveat and the risk-model caveat, said once.
_LIBRARY_NOTE = (
    "Framework references are illustrative cross-references written for this research "
    "prototype, not an authoritative mapping to ISO/IEC 27001, NIST SP 800-53, COBIT or "
    "CIS, and this application cannot state compliance with any of them. Inherent risk, "
    "privilege level and data sensitivity are inputs to the prototype risk model, not "
    "measurements. {0}"
).format(components.RISK_MODEL_NOTE)

_REF_PATTERN = re.compile(r"^CONTROL-(\d+)$", re.IGNORECASE)


def _read(loader: Callable[[], Any], fallback: Any, what: str) -> Any:
    try:
        return loader()
    except data_access.DataAccessError as exc:
        components.error_with_remedy("Could not load {0}.".format(what), exc)
        return fallback


def _lines(text: str) -> List[str]:
    """One entry per line - used for the long list fields."""
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def _terms(text: str) -> List[str]:
    """Comma- or newline-separated terms - used for retrieval keywords."""
    raw = str(text or "").replace("\n", ",")
    return [term.strip() for term in raw.split(",") if term.strip()]


def _page_link(page: str, label_text: str, icon: str = "") -> None:
    """``st.page_link`` that degrades to a caption when the target is not registered."""
    try:
        st.page_link(page, label=label_text, icon=icon or None)
    except Exception:  # noqa: BLE001 - navigation is a convenience, never a dependency
        st.caption("{0} (that page is not installed in this build)".format(label_text))


def _next_free_reference(controls: Sequence[Dict[str, Any]]) -> str:
    """``CONTROL-0NN`` one past the highest numbered reference in the library."""
    highest = 0
    for control in controls:
        match = _REF_PATTERN.match(str(control.get("control_id", "")).strip())
        if match:
            highest = max(highest, int(match.group(1)))
    return "CONTROL-{0:03d}".format(highest + 1)


# ---- project context shared by the table and the detail
def _project_context(project_id: Optional[int]) -> Dict[str, Any]:
    """What the current audit knows about each control: scope membership and the
    latest AI assessment (which carries the latest auditor decision)."""
    context: Dict[str, Any] = {"scoped": set(), "latest": {}}
    if project_id is None:
        return context
    context["scoped"] = {
        str(row.get("control_id"))
        for row in _read(lambda: data_access.list_scoped_controls(project_id), [], "the audit scope")
    }
    context["latest"] = {
        str(row.get("control_ref", "")): row
        for row in _read(
            lambda: data_access.list_assessments(project_id=project_id, latest_per_control=True),
            [],
            "the assessments",
        )
    }
    return context


def _decision_text(latest: Optional[Dict[str, Any]]) -> str:
    """Friendly auditor-decision label for a control's latest assessment, or "-"."""
    if not latest:
        return "-"
    return components.label("decision", latest.get("review_decision") or "PENDING") or "Awaiting decision"


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


def _library_table(
    controls: Sequence[Dict[str, Any]], project_id: Optional[int], context: Dict[str, Any]
) -> List[str]:
    """Draw the library as a multi-select table. Returns the selected control references."""
    scoped = context["scoped"]
    latest = context["latest"]
    rows = []
    for control in controls:
        ref = str(control.get("control_id"))
        row_latest = latest.get(ref)
        rows.append(
            {
                "control_id": ref,
                "name": control.get("name"),
                "category": control.get("category"),
                "control_type": control.get("control_type"),
                "inherent_risk": components.label("risk", control.get("inherent_risk")),
                "in_scope": ref in scoped,
                "status": components.label(
                    "status", (row_latest or {}).get("status") or "NOT_ASSESSED"
                ),
                "decision": _decision_text(row_latest),
                "criteria": len(control.get("assessment_criteria", []) or []),
                "is_active": bool(control.get("is_active", True)),
            }
        )
    columns = ["control_id", "name", "category", "control_type", "inherent_risk"]
    if project_id is not None:
        columns += ["in_scope", "status", "decision"]
    columns += ["criteria", "is_active"]

    event = components.df_table(
        rows,
        columns=columns,
        column_config={
            # The shared default is "small", which clips a CONTROL-0NN reference.
            "control_id": st.column_config.TextColumn("Control", width="medium"),
            "control_type": st.column_config.TextColumn("Type", width="small"),
            "inherent_risk": st.column_config.TextColumn("Inherent risk", width="small"),
            "in_scope": st.column_config.CheckboxColumn("In this audit", width="small"),
            "status": st.column_config.TextColumn("Latest AI status", width="medium"),
            "decision": st.column_config.TextColumn("Auditor decision", width="medium"),
            "criteria": st.column_config.NumberColumn("Criteria", width="small", format="%d"),
            "is_active": st.column_config.CheckboxColumn("Active", width="small"),
        },
        key="controls_table",
        empty_message="No control matches these filters.",
        on_select="rerun",
        selection_mode="multi-row",
    )
    if project_id is None:
        st.caption(
            "Select an audit project in the sidebar to see which controls it covers and "
            "what the AI assessment and the auditor concluded."
        )
    else:
        st.caption(
            "Tick rows to add them to {0}. “Not assessed” means no assessment has been run "
            "yet - not that the control passed. Retired controls are not offered when "
            "scoping.".format(state.current_project_name())
        )

    selected: List[str] = []
    try:
        indices = list(event.selection.rows) if event is not None else []
    except Exception:  # noqa: BLE001 - no selection event outside a live dataframe
        indices = []
    for index in indices:
        if 0 <= int(index) < len(rows):
            selected.append(str(rows[int(index)]["control_id"]))
    return selected


def _add_selected(
    selected: Sequence[str],
    controls: Sequence[Dict[str, Any]],
    project_id: Optional[int],
    context: Dict[str, Any],
) -> None:
    """The one-button scoping action under the table."""
    by_ref = {str(control.get("control_id")): control for control in controls}
    already = [ref for ref in selected if ref in context["scoped"]]
    retired = [
        ref
        for ref in selected
        if ref not in context["scoped"] and not (by_ref.get(ref) or {}).get("is_active", True)
    ]
    addable = [ref for ref in selected if ref not in already and ref not in retired]
    count = len(addable)

    left, right = st.columns([2, 3], gap="small")
    with left:
        clicked = st.button(
            "Add selected to the current audit ({0})".format(count),
            key="controls_add_selected",
            type="primary",
            disabled=(count == 0 or project_id is None),
            width="stretch",
        )
    with right:
        note = st.text_input(
            "Why these controls are in scope",
            key="controls_scope_note",
            placeholder="Optional - recorded on the audit project",
        )

    if project_id is None:
        st.caption("Select an audit project in the sidebar, then tick the controls it should cover.")
    elif not selected:
        st.caption("Tick one or more rows in the table to add them to {0}.".format(state.current_project_name()))
    else:
        parts: List[str] = []
        if already:
            parts.append("{0} of the selected control(s) already belong to this audit".format(len(already)))
        if retired:
            parts.append("{0} retired control(s) will be skipped".format(len(retired)))
        if parts:
            st.caption("; ".join(parts) + ".")

    if clicked and project_id is not None and addable:
        try:
            data_access.scope_controls(
                project_id, addable, scope_note=note.strip(), actor=state.auditor_name()
            )
        except data_access.DataAccessError as exc:
            components.error_with_remedy("Could not add the selected controls.", exc)
            return
        state.flash(
            "Added {0} control(s) to {1}. Next: upload evidence.".format(
                len(addable), state.current_project_name()
            ),
            "success",
        )
        if state.available():
            st.session_state[_K_JUST_ADDED] = len(addable)
        st.rerun()

    if state.available() and st.session_state.pop(_K_JUST_ADDED, None):
        components.next_steps(
            [
                ("Upload evidence", "views/evidence.py", ":material/inventory_2:"),
                ("Run assessment", "views/assessments.py", ":material/fact_check:"),
            ]
        )


# ---- detail
def _detail(control: Dict[str, Any], project_id: Optional[int], context: Dict[str, Any]) -> None:
    ref = str(control.get("control_id", ""))
    components.section_header(
        "{0}  {1}".format(ref, control.get("name", "")),
        subtitle=str(control.get("objective", "") or ""),
        eyebrow="Control definition",
    )
    in_scope = ref in context["scoped"]
    components.badges(
        components.plain_badge(str(control.get("category", ""))),
        components.plain_badge(str(control.get("control_type", ""))),
        components.plain_badge(str(control.get("control_frequency", ""))),
        components.risk_badge(control.get("inherent_risk")),
        components.plain_badge(
            "in this audit" if in_scope else "not in this audit",
            theme.GREEN if in_scope else theme.GREY,
        )
        if project_id is not None
        else "",
        components.plain_badge("retired", theme.AMBER) if not control.get("is_active", True) else "",
    )

    _scope_controls_row(ref, project_id, in_scope, bool(control.get("is_active", True)))
    _latest_assessment(ref, project_id, context)

    left, right = st.columns([3, 2], gap="medium")
    with left:
        st.markdown("**Description**")
        st.write(str(control.get("description", "") or "(none recorded)"))
        st.markdown("**Risk addressed**")
        st.write(str(control.get("risk_addressed", "") or "(none recorded)"))

        criteria = list(control.get("assessment_criteria", []) or [])
        st.markdown("**Assessment criteria**")
        if criteria:
            # Numbered because the criteria are referred to by number in an assessment;
            # an unordered list would lose that reference.
            for index, item in enumerate(criteria, start=1):
                st.markdown("{0}. {1}".format(index, item))
        else:
            st.caption(
                "No criteria recorded. A control with no testable criteria cannot be "
                "assessed consistently - add some under “Edit this control”."
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

        st.markdown("**Risk model inputs**")
        components.kv_grid(
            {
                "Inherent risk": components.label("risk", control.get("inherent_risk")),
                "Privilege level": "{0} / 5".format(control.get("privilege_level", "")),
                "Data sensitivity": "{0} / 5".format(control.get("data_sensitivity", "")),
            }
        )
        components.note(_LIBRARY_NOTE)

        st.markdown("**Retrieval keywords**")
        keywords = list(control.get("retrieval_keywords", []) or [])
        if keywords:
            components.badges(*[components.plain_badge(word) for word in keywords])
            st.caption(
                "Words used to find the relevant evidence for this control. They steer "
                "which passages the AI reads; they are not part of the audit requirement."
            )
        else:
            st.caption("None recorded - the control's own wording is used to find evidence.")

    components.kv_grid(
        {
            "Library source": control.get("source", ""),
            "Used in": "{0} audit project(s)".format(control.get("scoped_project_count", 0)),
            "Created": components.when(control.get("created_at")),
            "Last updated": components.when(control.get("updated_at")),
        }
    )

    if st.toggle("Edit this control", key="controls_edit_toggle_{0}".format(ref), value=False):
        _edit_form(control)
    _retirement(control)


def _scope_controls_row(ref: str, project_id: Optional[int], in_scope: bool, active: bool) -> None:
    if project_id is None:
        return
    left, right = st.columns([1, 4], gap="small")
    with left:
        if in_scope:
            if st.button("Remove from this audit", key="controls_unscope_{0}".format(ref), width="stretch"):
                _unscope(project_id, ref)
        elif active:
            if st.button(
                "Add to this audit",
                key="controls_scope_{0}".format(ref),
                type="primary",
                width="stretch",
            ):
                _scope(project_id, ref)
    with right:
        if not in_scope and not active:
            st.caption("Retired controls are not offered when scoping. Restore it first to add it.")


def _scope(project_id: int, ref: str) -> None:
    try:
        data_access.scope_controls(project_id, [ref], actor=state.auditor_name())
    except data_access.DataAccessError as exc:
        components.error_with_remedy("Could not add {0} to the audit.".format(ref), exc)
        return
    state.flash(
        "Added 1 control ({0}) to {1}. Next: upload evidence.".format(ref, state.current_project_name()),
        "success",
    )
    if state.available():
        st.session_state[_K_JUST_ADDED] = 1
    st.rerun()


def _unscope(project_id: int, ref: str) -> None:
    try:
        data_access.unscope_control(project_id, ref, actor=state.auditor_name())
    except data_access.DataAccessError as exc:
        components.error_with_remedy("Could not remove {0} from the audit.".format(ref), exc)
        return
    state.flash(
        "{0} is no longer part of {1}. Assessments already written against it are unchanged.".format(
            ref, state.current_project_name()
        ),
        "success",
    )
    st.rerun()


def _latest_assessment(ref: str, project_id: Optional[int], context: Dict[str, Any]) -> None:
    if project_id is None:
        return
    row = context["latest"].get(ref)
    if not row:
        st.caption("Not assessed yet in this audit.")
        if ref in context["scoped"]:
            _page_link("views/assessments.py", "Run assessment", ":material/fact_check:")
        return
    components.ai_disclaimer_banner(
        "Latest AI assessment of this control in this audit - requires auditor review.",
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
    produced = components.when(row.get("created_at"))
    st.caption(
        "Auditor decision: {0}.{1} Nothing here is an audit conclusion until an auditor "
        "records one.".format(_decision_text(row), " Produced {0}.".format(produced) if produced else "")
    )
    assessment_id = row.get("id")
    if assessment_id is None:
        return
    if state.current_assessment_id() == int(assessment_id):
        _page_link("views/assessments.py", "Open the assessment", ":material/fact_check:")
    elif st.button("Open assessment", key="controls_open_assessment_{0}".format(ref)):
        try:
            state.open_assessment(int(assessment_id), project_id=project_id)
        except Exception:  # noqa: BLE001 - the assessment is selected; only the jump failed
            st.rerun()


# ---- create and edit
def _control_form(
    prefix: str,
    control: Optional[Dict[str, Any]] = None,
    default_ref: str = "",
) -> Optional[Dict[str, Any]]:
    """Shared field set for creating and editing. Returns the payload on submit.

    Reference, name, objective and criteria are shown in full; everything an auditor
    can reasonably leave at its default sits inside "Optional details".
    """
    data = dict(control or {})
    editing = bool(control)
    risk_levels = data_access.risk_levels()
    current_risk = str(data.get("inherent_risk", "MEDIUM"))

    with st.form("{0}_form".format(prefix)):
        top_left, top_right = st.columns([1, 2], gap="small")
        with top_left:
            ref = st.text_input(
                "Control reference (required)",
                value=str(data.get("control_id", "") or default_ref),
                disabled=editing,
                help=(
                    "Fixed once created: it is the identifier printed in every "
                    "assessment and report."
                    if editing
                    else "The next free reference is filled in. Any unique reference works."
                ),
            )
        with top_right:
            name = st.text_input("Name (required)", value=str(data.get("name", "")))
        objective = st.text_area("Objective", value=str(data.get("objective", "")), height=80)
        criteria = st.text_area(
            "Assessment criteria (one per line)",
            value="\n".join(data.get("assessment_criteria", []) or []),
            height=140,
            help="Each line is one testable criterion. These are what the evidence is judged against.",
        )
        with st.expander("Optional details", expanded=False):
            description = st.text_area("Description", value=str(data.get("description", "")), height=110)
            risk_addressed = st.text_area(
                "Risk addressed", value=str(data.get("risk_addressed", "")), height=80
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
                    "framework."
                ),
            )
            keywords = st.text_area(
                "Retrieval keywords (comma or newline separated)",
                value=", ".join(data.get("retrieval_keywords", []) or []),
                height=70,
                help="Words that help find the right evidence - column headings and domain vocabulary work best.",
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
                    format_func=lambda value: components.label("risk", value),
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
    components.section_header(
        "Add a control to the library",
        subtitle="A control added here can be scoped to any audit project and assessed like a bundled one.",
    )
    if not st.toggle("Show the form", key="controls_show_create", value=False):
        return
    st.caption(
        "Write the assessment criteria as testable statements - they are what the "
        "evidence is judged against."
    )
    everything = _read(lambda: data_access.list_controls(active_only=False), [], "the control library")
    existing = {str(control.get("control_id", "")).strip().upper() for control in everything}
    payload = _control_form("controls_create", default_ref=_next_free_reference(everything))
    if payload is None:
        return
    ref = str(payload.get("control_id", ""))
    if not ref or not payload.get("name"):
        st.error("A control needs a reference and a name - both are marked (required).")
        return
    if ref in existing:
        st.error(
            "{0} already exists in the library. Choose another reference - {1} is free.".format(
                ref, _next_free_reference(everything)
            )
        )
        return
    try:
        control = data_access.create_control(payload, actor=state.auditor_name())
    except data_access.DataAccessError as exc:
        components.error_with_remedy("Could not create the control.", exc)
        return
    new_ref = str(control.get("control_id", ""))
    state.set_current_control_ref(new_ref)
    state.flash(
        "Added {0} to the library. Next: tick it in the table and add it to your audit.".format(new_ref),
        "success",
    )
    st.rerun()


def _edit_form(control: Dict[str, Any]) -> None:
    payload = _control_form("controls_edit_{0}".format(control.get("control_id", "")), control)
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
        components.error_with_remedy("Could not save the control.", exc)
        return
    state.flash("Saved {0}.".format(control.get("control_id", "")), "success")
    st.rerun()


def _retirement(control: Dict[str, Any]) -> None:
    ref = str(control.get("control_id", ""))
    active = bool(control.get("is_active", True))
    with st.expander("Retire or restore this control", expanded=False):
        if active:
            st.caption(
                "Retired controls are not offered when scoping. Nothing is deleted: "
                "assessments and reports that reference {0} still resolve to it, and it "
                "can be restored later.".format(ref)
            )
            confirm = st.checkbox(
                "I understand this control will no longer be offered when scoping an audit.",
                key="controls_retire_confirm_{0}".format(ref),
            )
            if st.button("Retire {0}".format(ref), key="controls_retire_{0}".format(ref), disabled=not confirm):
                _set_active(ref, False)
        else:
            st.caption("This control is retired and is not offered when scoping an audit.")
            if st.button("Restore {0}".format(ref), key="controls_restore_{0}".format(ref), type="primary"):
                _set_active(ref, True)


def _set_active(ref: str, active: bool) -> None:
    try:
        data_access.set_control_active(ref, active, actor=state.auditor_name())
    except data_access.DataAccessError as exc:
        components.error_with_remedy("Could not change {0}.".format(ref), exc)
        return
    state.flash("{0} is now {1}.".format(ref, "active" if active else "retired"), "success")
    st.rerun()


# ---- page
def render() -> None:
    components.section_header(
        "Control library",
        subtitle=(
            "What each control requires and how it is tested. Add controls to the current "
            "audit from here."
        ),
        eyebrow="Audit project",
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
    context = _project_context(project_id)
    selected = _library_table(controls, project_id, context)
    _add_selected(selected, controls, project_id, context)

    if controls:
        st.markdown("---")
        refs = [str(control.get("control_id")) for control in controls]
        if len(selected) == 1:
            chosen = selected[0]
            st.caption("Showing the control ticked in the table. Tick none to pick one from the list.")
        else:
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
        if control is not None:
            _detail(control, project_id, context)

    st.markdown("---")
    _create_form()


if __name__ == "__main__":
    render()
