"""Audit projects - the register of audits and the working audit's scope.

An audit project is the unit everything else in this application hangs from: evidence
is uploaded into a project, controls are scoped to a project, assessments and reports
are written against a project. This page is where one is created, described, scoped and
retired.

How a new audit is created
--------------------------
:func:`open_new_audit_dialog` is a ``st.dialog``. It is exported because the Home page
opens the same dialog from its "Start an audit" path, so a first-time auditor sees one
form wherever they begin. The dialog asks for the two things an audit cannot exist
without (a name and an area), offers the standard set of five controls so nobody has to
know the control library before they start, and tucks the optional details away. On
success it makes the new audit the working project and lands on the Evidence page,
which is the next thing to do.

Why the working project is switched through ``state``
-----------------------------------------------------
The shell owns the sidebar project selector (see ``app.frontend.streamlit_app``). A page
that wants to change the working project calls :func:`state.request_project_switch` and
reruns; the shell moves the selector before it is drawn. Writing the project directly
from here would be overwritten by the selector one rerun later. The register table at the
bottom of this page uses that handshake: selecting a row switches the working audit.

Deleting is destructive and says so
-----------------------------------
``delete_project`` cascades to the project's evidence rows, its assessments, its
citations, its human reviews and its reports, and removes the stored evidence and report
files from disk. The confirmation therefore prints what will be destroyed as counts
rather than a generic "are you sure", and stays behind a checkbox.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from datetime import date, datetime  # noqa: E402
from typing import Any, Callable, Dict, List, Optional, Sequence  # noqa: E402

import streamlit as st  # noqa: E402

from app.frontend import components, data_access, state, theme  # noqa: E402

PAGE = "projects"

#: The panels under the current audit's header. A segmented control rather than
#: ``st.tabs`` because the chosen panel survives the reruns every button press causes.
_PANEL_SCOPE = "Controls in scope"
_PANEL_EDIT = "Edit details"
_PANEL_DELETE = "Delete"
_PANELS = [_PANEL_SCOPE, _PANEL_EDIT, _PANEL_DELETE]

_TABLE_COLUMNS = [
    "name",
    "audit_area",
    "status_label",
    "period_label",
    "controls_in_scope",
    "evidence_files",
    "controls_assessed",
    "auditor_name",
    "created_at",
    "is_demo",
]

_DATE_CLEAR_NOTE = "Clearing a date is not supported yet."


# ---- small helpers
def _read(loader: Callable[[], Any], fallback: Any, what: str) -> Any:
    try:
        return loader()
    except data_access.DataAccessError as exc:
        components.error_with_remedy("Could not load {0}.".format(what), exc)
        return fallback


def _iso(value: Any) -> Optional[str]:
    """Dates leave this page as ISO-8601 strings.

    In-process the service layer would accept a ``date`` object directly, but the same
    call goes over HTTP when ``USE_API`` is set and JSON has no date type. Converting
    here keeps one code path for both transports.
    """
    return value.isoformat() if value is not None else None


def _as_date(value: Any) -> Optional[date]:
    """Parse a stored ISO timestamp back into a ``date`` for ``st.date_input``."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _control_label(control: Dict[str, Any]) -> str:
    """``"CONTROL-001 · Multi-Factor Authentication for Privileged Accounts"``."""
    return "{0} · {1}".format(control.get("control_id", ""), control.get("name", "") or "")


def _control_options(controls: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    return {str(control.get("control_id")): _control_label(control) for control in controls}


def _standard_control_refs(available: Sequence[str]) -> List[str]:
    """The seeded five-control set, restricted to controls that exist and are active."""
    from app.database.seed import MANDATED_CONTROL_REFS

    known = set(available)
    return [ref for ref in MANDATED_CONTROL_REFS if ref in known]


def _quoted(text: Any) -> str:
    return "'{0}'".format(str(text or "").strip())


# ---- create
@st.dialog("New audit")
def open_new_audit_dialog() -> None:
    """The one form that creates an audit project. Also opened from the Home page.

    Streamlit reruns only this dialog when a widget inside it changes, so a validation
    error keeps the auditor's input. Success switches the working project and moves to
    the Evidence page: an audit with controls and no evidence has exactly one next step.
    """
    controls = _read(lambda: data_access.list_controls(active_only=True), [], "the control library")
    labels = _control_options(controls)
    options = sorted(labels)
    standard = _standard_control_refs(options)
    sidebar_name = state.auditor_name()

    with st.form("projects_new_audit", clear_on_submit=False):
        name = st.text_input(
            "Audit name",
            placeholder="e.g. Privileged access review 2026",
            help="Required. Names must be unique.",
        )
        area = st.text_input(
            "Audit area",
            placeholder="e.g. Identity and access management",
            help="Required. The business or technology area this audit covers.",
        )
        refs = st.multiselect(
            "Controls to test",
            options=options,
            format_func=lambda ref: labels.get(ref, ref),
            help="Leave empty to start with the standard set. Controls can be added later.",
        )
        use_standard = st.checkbox(
            "Start with the standard set of five controls",
            value=True,
            help=(
                "Used when no controls are chosen above: {0}.".format(", ".join(standard))
                if standard
                else "The standard set is not available in this control library."
            ),
        )
        your_name = ""
        if not sidebar_name:
            your_name = st.text_input(
                "Your name",
                placeholder="Who is creating this audit",
                help="Recorded as the person who created the audit. It also fills the sidebar.",
            )
        with st.expander("More details", expanded=False):
            description = st.text_area(
                "Description",
                placeholder="Scope, systems covered, why this audit was raised.",
                height=90,
            )
            left, right = st.columns(2)
            with left:
                period_start = st.date_input("Period start", value=None)
            with right:
                period_end = st.date_input("Period end", value=None)
            lead = st.text_input(
                "Lead auditor",
                value=sidebar_name,
                help="Defaults to the name in the sidebar",
            )
        submitted = st.form_submit_button("Create audit", type="primary")

    if not submitted:
        return

    name = str(name or "").strip()
    area = str(area or "").strip()
    if not name or not area:
        st.error("An audit needs both a name and an audit area.")
        return

    your_name = str(your_name or "").strip()
    if your_name:
        state.set_auditor_name(your_name)
    lead = str(lead or "").strip() or your_name
    actor = state.auditor_name() or lead or "Auditor"

    chosen = list(refs)
    if not chosen and use_standard:
        chosen = list(standard)

    try:
        project = data_access.create_project(
            name=name,
            audit_area=area,
            description=str(description or "").strip(),
            period_start=_iso(period_start),
            period_end=_iso(period_end),
            auditor_name=lead,
            control_refs=chosen,
            actor=actor,
        )
    except data_access.DataAccessError as exc:
        components.error_with_remedy("The audit could not be created.", exc)
        return

    state.request_project_switch(project["id"])
    state.flash("Audit {0} created. Next: upload evidence.".format(_quoted(name)), "success")
    st.switch_page("views/evidence.py")


# ---- detail
def _detail(project: Dict[str, Any], projects: List[Dict[str, Any]]) -> None:
    project_id = int(project["id"])
    components.section_header(
        str(project.get("name", "")),
        subtitle=str(project.get("audit_area", "") or ""),
        eyebrow="Current audit project",
    )
    components.badges(
        components.plain_badge(
            components.label("project_status", project.get("status")) or "Status unknown",
            theme.ACCENT,
        ),
        components.plain_badge("Demo", theme.AI_COLOR) if project.get("is_demo") else "",
    )

    stage = _read(lambda: data_access.project_stage(project_id), {}, "the audit's progress")
    in_scope = int(stage.get("controls_in_scope", project.get("controls_in_scope", 0)) or 0)
    assessed = int(stage.get("controls_assessed", project.get("controls_assessed", 0)) or 0)
    components.metric_row(
        [
            {"label": "Controls in scope", "value": in_scope},
            {
                "label": "Assessed",
                "value": "{0} of {1}".format(assessed, in_scope),
                "color": theme.ACCENT,
            },
            {
                "label": "Evidence files",
                "value": int(stage.get("evidence_files", project.get("evidence_files", 0)) or 0),
            },
            {
                "label": "Awaiting review",
                "value": int(stage.get("pending_reviews", 0) or 0),
                "color": theme.AI_COLOR,
                "caption": "AI assessments with no auditor decision yet.",
            },
        ],
        columns=4,
    )
    if stage:
        components.workflow_strip(stage, compact=False)

    components.kv_grid(
        {
            "Description": project.get("description", ""),
            "Period": project.get("period_label", ""),
            "Lead auditor": project.get("auditor_name", ""),
            "Scope note": project.get("scope_note", ""),
            "Created": components.when(project.get("created_at")),
            "Last updated": components.when(project.get("updated_at")),
        }
    )
    if project.get("is_demo"):
        components.note(
            "Demo audit. Its evidence was generated by the synthetic dataset generator and "
            "represents no real organisation."
        )

    panel = st.segmented_control(
        "Audit details",
        options=_PANELS,
        default=_PANEL_SCOPE,
        selection_mode="single",
        key="projects_detail_tab",
        label_visibility="collapsed",
    )
    if panel == _PANEL_EDIT:
        _edit_form(project)
    elif panel == _PANEL_DELETE:
        _delete_panel(project, projects)
    else:
        _scope_panel(project_id)


# ---- scope
def _scope_panel(project_id: int) -> None:
    scoped = _read(
        lambda: data_access.list_scoped_controls(project_id, active_only=False),
        [],
        "the controls in scope",
    )
    if scoped:
        for control in scoped:
            ref = str(control.get("control_id", ""))
            row, action = st.columns([6, 1], gap="small")
            with row:
                components.badges(
                    components.plain_badge(ref, theme.ACCENT),
                    components.plain_badge(str(control.get("category", "") or "")),
                    components.risk_badge(control.get("inherent_risk")),
                    components.plain_badge("Retired", theme.GREY) if not control.get("is_active") else "",
                )
                st.markdown(
                    '<div class="ia-quad-lead">{0}</div>'.format(
                        components.escape(control.get("name", ""))
                    ),
                    unsafe_allow_html=True,
                )
                objective = str(control.get("objective", "") or "")
                if objective:
                    st.markdown(
                        '<div class="ia-note">{0}</div>'.format(components.escape(objective)),
                        unsafe_allow_html=True,
                    )
            with action:
                if st.button(
                    "Remove",
                    key="projects_unscope_{0}".format(ref),
                    width="stretch",
                    help="Take this control out of scope. Past assessments are kept.",
                ):
                    _unscope(project_id, ref)
    else:
        st.caption("No control is in scope yet. Add at least one below, then upload evidence.")

    st.markdown("---")
    candidates = _read(
        lambda: data_access.list_unscoped_controls(project_id, active_only=True),
        [],
        "the control library",
    )
    if not candidates:
        st.caption("Every active control in the library is already in scope.")
        return
    labels = _control_options(candidates)
    with st.form("projects_scope_add", clear_on_submit=True):
        refs = st.multiselect(
            "Add controls",
            options=sorted(labels),
            format_func=lambda ref: labels.get(ref, ref),
        )
        note = st.text_input(
            "Why these controls are in scope",
            placeholder="Optional - e.g. sampled the full population for the period.",
        )
        submitted = st.form_submit_button("Add", type="primary")
    if submitted:
        if not refs:
            st.warning("Choose at least one control to add.")
        else:
            _scope(project_id, list(refs), note)


def _scope(project_id: int, refs: List[str], note: str) -> None:
    try:
        data_access.scope_controls(
            project_id, refs, scope_note=str(note or "").strip(), actor=state.auditor_name()
        )
    except data_access.DataAccessError as exc:
        components.error_with_remedy("Those controls could not be added.", exc)
        return
    state.flash(
        "Added {0} control(s) to scope. Next: upload evidence, then run the assessment.".format(
            len(refs)
        ),
        "success",
    )
    st.rerun()


def _unscope(project_id: int, ref: str) -> None:
    try:
        removed = data_access.unscope_control(project_id, ref, actor=state.auditor_name())
    except data_access.DataAccessError as exc:
        components.error_with_remedy("{0} could not be removed from scope.".format(ref), exc)
        return
    state.flash(
        "{0} is no longer in scope. Assessments already written against it are "
        "unchanged.".format(ref)
        if removed
        else "{0} was not in scope.".format(ref),
        "success" if removed else "info",
    )
    st.rerun()


# ---- edit
def _edit_form(project: Dict[str, Any]) -> None:
    project_id = int(project["id"])
    statuses = data_access.project_statuses()
    current_status = str(project.get("status", "") or "").upper()
    with st.form("projects_edit"):
        name = st.text_input("Audit name", value=str(project.get("name", "") or ""))
        area = st.text_input("Audit area", value=str(project.get("audit_area", "") or ""))
        description = st.text_area(
            "Description", value=str(project.get("description", "") or ""), height=90
        )
        left, middle, right = st.columns(3)
        with left:
            start = st.date_input("Period start", value=_as_date(project.get("period_start")))
        with middle:
            end = st.date_input("Period end", value=_as_date(project.get("period_end")))
        with right:
            status = st.selectbox(
                "Status",
                options=statuses,
                index=statuses.index(current_status) if current_status in statuses else 0,
                format_func=lambda value: components.label("project_status", value),
            )
        auditor = st.text_input("Lead auditor", value=str(project.get("auditor_name", "") or ""))
        scope_note = st.text_area(
            "Scope note", value=str(project.get("scope_note", "") or ""), height=70
        )
        submitted = st.form_submit_button("Save changes", type="primary")
    st.caption(
        "Only these fields are editable. The demo flag, identifiers and timestamps are "
        "owned by the application. " + _DATE_CLEAR_NOTE
    )
    if submitted:
        _save_changes(
            project,
            name=name,
            audit_area=area,
            description=description,
            period_start=start,
            period_end=end,
            auditor_name=auditor,
            status=status,
            scope_note=scope_note,
        )


def _changed_fields(project: Dict[str, Any], **fields: Any) -> Dict[str, Any]:
    """Only what differs from the stored record; cleared dates are reported, not sent.

    The service skips ``None`` values, so a date the auditor cleared cannot be written
    through either transport today. Those keys come back under ``_cleared`` so the form
    can say so instead of silently keeping the old value.
    """
    changes: Dict[str, Any] = {}
    cleared: List[str] = []
    for key in ("name", "audit_area", "description", "auditor_name", "scope_note"):
        new = str(fields.get(key, "") or "").strip()
        old = str(project.get(key, "") or "").strip()
        if new != old:
            changes[key] = new
    for key in ("period_start", "period_end"):
        new_date = fields.get(key)
        old_date = _as_date(project.get(key))
        if new_date is None and old_date is not None:
            cleared.append(key)
        elif new_date != old_date:
            changes[key] = _iso(new_date)
    new_status = str(fields.get("status", "") or "").upper()
    if new_status and new_status != str(project.get("status", "") or "").upper():
        changes["status"] = new_status
    if cleared:
        changes["_cleared"] = cleared
    return changes


def _save_changes(project: Dict[str, Any], **fields: Any) -> None:
    if not str(fields.get("name", "") or "").strip() or not str(fields.get("audit_area", "") or "").strip():
        st.error("An audit needs both a name and an audit area.")
        return
    changes = _changed_fields(project, **fields)
    cleared = changes.pop("_cleared", [])
    if cleared:
        st.caption(
            "{0} {1} kept its previous value.".format(
                _DATE_CLEAR_NOTE, " and ".join(key.replace("_", " ") for key in cleared)
            )
        )
    if not changes:
        st.info("Nothing changed.")
        return
    try:
        data_access.update_project(int(project["id"]), actor=state.auditor_name(), **changes)
    except data_access.DataAccessError as exc:
        components.error_with_remedy("The changes could not be saved.", exc)
        return
    state.flash(
        "Saved changes to {0}.".format(_quoted(fields.get("name") or project.get("name"))),
        "success",
    )
    st.rerun()


# ---- delete
def _delete_panel(project: Dict[str, Any], projects: List[Dict[str, Any]]) -> None:
    project_id = int(project["id"])
    st.warning(
        "Deleting this audit project also deletes everything recorded under it: "
        "{0} evidence file(s) and their parsed chunks, {1} assessment(s) with their "
        "citations and auditor decisions, and every report generated for it. The stored "
        "evidence and report files are removed from disk. This cannot be undone.".format(
            project.get("evidence_files", 0), project.get("assessments", 0)
        )
    )
    if project.get("is_demo"):
        st.caption(
            "This is the demo audit. Press 'Try the demo audit' on the Home page to load "
            "it again."
        )
    confirm = st.checkbox(
        "I understand that the evidence, assessments and decisions above will be deleted.",
        key="projects_delete_confirm",
    )
    if st.button("Delete this audit project", key="projects_delete", disabled=not confirm):
        _delete(project_id, str(project.get("name", "")), projects)


def _delete(project_id: int, name: str, projects: List[Dict[str, Any]]) -> None:
    remaining = [item for item in projects if int(item.get("id", -1)) != project_id]
    try:
        deleted = data_access.delete_project(project_id)
    except data_access.DataAccessError as exc:
        components.error_with_remedy("The audit project could not be deleted.", exc)
        return
    if not deleted:
        state.flash("Audit project {0} no longer exists.".format(project_id), "info")
        st.rerun()
        return
    if remaining:
        successor = remaining[0]
        state.request_project_switch(successor["id"])
        state.flash(
            "Deleted {0}. Now showing {1}.".format(_quoted(name), _quoted(successor.get("name"))),
            "success",
        )
    else:
        state.request_project_switch(None)
        state.flash(
            "Deleted {0}. No audit project remains - create a new audit or load the demo "
            "audit.".format(_quoted(name)),
            "success",
        )
    st.rerun()


# ---- register
def _register(projects: List[Dict[str, Any]]) -> None:
    with st.expander("All audit projects ({0})".format(len(projects)), expanded=False):
        statuses = data_access.project_statuses()
        left, middle, right = st.columns([2, 3, 1], gap="small")
        with left:
            status = st.selectbox(
                "Status",
                options=["Any"] + statuses,
                index=0,
                format_func=lambda value: value if value == "Any" else components.label("project_status", value),
                key="projects_status_filter",
            )
        with middle:
            search = st.text_input(
                "Search", placeholder="Name or audit area", key="projects_search"
            )
        with right:
            include_demo = st.checkbox("Include demo", value=True, key="projects_include_demo")

        rows = _read(
            lambda: data_access.list_projects(
                status=None if status == "Any" else status,
                include_demo=include_demo,
                search=str(search or "").strip(),
            ),
            [],
            "the audit project register",
        )
        if not rows:
            st.caption("No audit project matches these filters.")
            return

        display = [
            dict(row, status_label=components.label("project_status", row.get("status")))
            for row in rows
        ]
        event = components.df_table(
            display,
            columns=_TABLE_COLUMNS,
            column_config={
                "name": st.column_config.TextColumn("Audit", width="medium"),
                "audit_area": st.column_config.TextColumn("Audit area", width="medium"),
                "status_label": st.column_config.TextColumn("Status", width="small"),
                "period_label": st.column_config.TextColumn("Period", width="medium"),
                "controls_in_scope": st.column_config.NumberColumn("Controls", width="small", format="%d"),
                "evidence_files": st.column_config.NumberColumn("Evidence", width="small", format="%d"),
                "controls_assessed": st.column_config.NumberColumn("Assessed", width="small", format="%d"),
                "auditor_name": st.column_config.TextColumn("Lead auditor", width="small"),
                "is_demo": st.column_config.CheckboxColumn("Demo", width="small"),
                "created_at": st.column_config.DatetimeColumn(
                    "Created", width="small", format="D MMM YYYY"
                ),
            },
            key="projects_register",
            on_select="rerun",
            selection_mode="single-row",
        )
        st.caption("Select a row to make that audit the working project.")

        selected = _selected_rows(event)
        if not selected:
            return
        position = int(selected[0])
        if position < 0 or position >= len(rows):
            return
        chosen_id = int(rows[position]["id"])
        if chosen_id != state.current_project_id():
            state.request_project_switch(chosen_id)
            st.rerun()


def _selected_rows(event: Any) -> List[int]:
    """The positional indices a ``st.dataframe`` selection event carries, or []."""
    selection = getattr(event, "selection", None)
    if selection is None and isinstance(event, dict):
        selection = event.get("selection")
    if selection is None:
        return []
    rows = getattr(selection, "rows", None)
    if rows is None and isinstance(selection, dict):
        rows = selection.get("rows")
    return [int(index) for index in (rows or [])]


# ---- empty database
def _empty_database() -> None:
    """Two ways in, and nothing else on the page until one of them is taken."""
    if components.empty_state(
        "No audit project yet",
        "Create your first audit, or load the demo audit to see the workflow.",
        action_label="New audit",
        action_key="projects_empty_new",
    ):
        open_new_audit_dialog()
    _left, middle, _right = st.columns([1, 1, 1])
    with middle:
        components.load_demo_control(
            key="projects_try_demo",
            help_text="Loads a synthetic audit with evidence already uploaded. Nothing is assessed until you press Run assessment.",
        )


# ---- page
def render() -> None:
    components.section_header(
        "Audit projects",
        subtitle="Each audit holds its controls, evidence, assessments and decisions.",
        eyebrow="Audit project",
    )
    projects = _read(lambda: data_access.list_projects(), [], "the audit projects")
    if not projects:
        _empty_database()
        return

    if st.button("New audit", key="projects_new", type="primary"):
        open_new_audit_dialog()

    project_id = state.current_project_id()
    project = (
        _read(lambda: data_access.get_project(project_id), None, "the audit project")
        if project_id is not None
        else None
    )
    if project is None:
        components.empty_state(
            "No audit project selected",
            "Choose one in the register below to see its scope and progress, or create a "
            "new audit.",
        )
    else:
        _detail(project, projects)

    st.markdown("---")
    _register(projects)


if __name__ == "__main__":
    render()
