"""Audit Projects - the engagement register and its scope.

An engagement is the unit everything else in this application hangs from: evidence is
uploaded into a project, controls are scoped to a project, assessments and reports are
written against a project. This page is where one is created, described, scoped and
retired.

Why there is no project selector here
-------------------------------------
The shell owns the working engagement (see ``app.frontend.streamlit_app``), because an
auditor moves between evidence, findings and review inside *one* engagement and a
per-page selector would reset that on every hop. A second selector on this page would
also be a second source of truth: the sidebar's own widget re-asserts its stored value
on every rerun, so a page-driven switch would silently revert. The list below is
therefore an overview of every engagement, and the detail panel always shows the one
selected in the sidebar.

Deleting is destructive and says so
-----------------------------------
``delete_project`` cascades to the project's evidence rows, its assessments, its
citations, its human reviews and its reports. The confirmation therefore prints what
will be destroyed as counts rather than a generic "are you sure": an auditor deleting an
engagement should be able to see that they are deleting nine evidence files and five
assessments with it.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from typing import Any, Callable, Dict, List, Optional  # noqa: E402

import streamlit as st  # noqa: E402

from app.frontend import components, data_access, state, theme  # noqa: E402

PAGE = "projects"

_TABLE_COLUMNS = [
    "name",
    "audit_area",
    "status",
    "period_label",
    "controls_in_scope",
    "evidence_files",
    "assessments",
    "auditor_name",
    "created_at",
    "is_demo",
]


def _read(loader: Callable[[], Any], fallback: Any, what: str) -> Any:
    try:
        return loader()
    except data_access.DataAccessError as exc:
        st.error("Could not load {0}: {1}".format(what, exc))
        return fallback


def _iso(value: Any) -> Optional[str]:
    """Dates leave this page as ISO-8601 strings.

    In-process the service layer would accept a ``date`` object directly, but the same
    call goes over HTTP when ``USE_API`` is set and JSON has no date type. Converting
    here keeps one code path for both transports.
    """
    return value.isoformat() if value is not None else None


# ---- create
def _create_form() -> None:
    with st.expander("New audit project", expanded=False):
        st.caption(
            "An engagement holds the controls in scope, the evidence gathered for them "
            "and every assessment written against them."
        )
        controls = _read(lambda: data_access.list_controls(active_only=True), [], "the control library")
        labels = {
            str(control.get("control_id")): "{0}  {1}".format(
                control.get("control_id"), control.get("name", "")
            )
            for control in controls
        }
        with st.form("project_create", clear_on_submit=True):
            name = st.text_input("Project name", placeholder="Privileged Access Management Audit")
            area = st.text_input("Audit area", placeholder="Identity and access management")
            description = st.text_area(
                "Description",
                placeholder="Scope, systems covered, why this engagement was raised.",
                height=90,
            )
            left, middle, right = st.columns(3)
            with left:
                period_start = st.date_input("Period start", value=None, key="project_create_start")
            with middle:
                period_end = st.date_input("Period end", value=None, key="project_create_end")
            with right:
                status = st.selectbox("Status", options=data_access.project_statuses(), index=0)
            auditor = st.text_input("Lead auditor", value=state.auditor_name())
            refs = st.multiselect(
                "Controls in scope (optional)",
                options=sorted(labels),
                format_func=lambda ref: labels.get(ref, ref),
                help="Controls can be scoped later from the detail panel below.",
            )
            scope_note = st.text_input(
                "Scope note (applied to the controls selected above)",
                placeholder="Sampled the full population for the period.",
            )
            submitted = st.form_submit_button("Create project", type="primary")

        if submitted:
            _create(name, area, description, period_start, period_end, auditor, status, refs, scope_note)


def _create(
    name: str,
    area: str,
    description: str,
    period_start: Any,
    period_end: Any,
    auditor: str,
    status: str,
    refs: List[str],
    scope_note: str,
) -> None:
    if not name.strip() or not area.strip():
        st.error("A project needs both a name and an audit area.")
        return
    try:
        project = data_access.create_project(
            name=name.strip(),
            audit_area=area.strip(),
            description=description.strip(),
            period_start=_iso(period_start),
            period_end=_iso(period_end),
            auditor_name=auditor.strip(),
            status=status,
            scope_note=scope_note.strip(),
            control_refs=list(refs),
            actor=state.auditor_name(),
        )
    except data_access.DataAccessError as exc:
        st.error("Could not create the project: {0}".format(exc))
        return
    state.flash(
        "Created “{0}” with {1} control(s) in scope. Select it in the sidebar to work "
        "in it.".format(project.get("name", ""), len(refs)),
        "success",
    )
    st.rerun()


# ---- list
def _project_list() -> List[Dict[str, Any]]:
    components.section_header(
        "All engagements",
        subtitle="Every audit project in this database, newest first.",
        eyebrow="Register",
    )
    left, middle, right = st.columns([2, 3, 1], gap="small")
    with left:
        status = st.selectbox(
            "Status",
            options=["Any"] + data_access.project_statuses(),
            index=0,
            key="projects_status_filter",
        )
    with middle:
        search = st.text_input(
            "Search",
            value=str(state.get_filter(PAGE, "search", "") or ""),
            placeholder="Name or audit area",
            key="projects_search",
        )
    with right:
        include_demo = st.checkbox("Include demo", value=True, key="projects_include_demo")
    state.set_filter(PAGE, "status", status)
    state.set_filter(PAGE, "search", search)

    projects = _read(
        lambda: data_access.list_projects(
            status=None if status == "Any" else status,
            include_demo=include_demo,
            search=search.strip(),
        ),
        [],
        "the project list",
    )
    if not projects:
        st.caption("No project matches these filters.")
        return []

    components.df_table(
        projects,
        columns=_TABLE_COLUMNS,
        column_config={
            # Ten columns on a 1280px screen: everything that can be narrow is narrow, so
            # the counts stay visible without scrolling the table sideways.
            "name": st.column_config.TextColumn("Project", width="medium"),
            "audit_area": st.column_config.TextColumn("Audit area", width="medium"),
            "status": st.column_config.TextColumn("Status", width="small"),
            "period_label": st.column_config.TextColumn("Period", width="medium"),
            "controls_in_scope": st.column_config.NumberColumn("Controls", width="small", format="%d"),
            "evidence_files": st.column_config.NumberColumn("Evidence", width="small", format="%d"),
            "assessments": st.column_config.NumberColumn("Assessments", width="small", format="%d"),
            "auditor_name": st.column_config.TextColumn("Lead auditor", width="small"),
            "is_demo": st.column_config.CheckboxColumn("Demo", width="small"),
            "created_at": st.column_config.DatetimeColumn(
                "Created", width="small", format="YYYY-MM-DD"
            ),
        },
    )
    st.caption(
        "The detail panel below shows the engagement selected in the sidebar. Switch "
        "engagements there."
    )
    return projects


# ---- detail
def _detail(project_id: int) -> None:
    project = _read(lambda: data_access.get_project(project_id), None, "the project")
    if not project:
        st.warning(
            "The selected engagement (id {0}) no longer exists. Choose another in the "
            "sidebar.".format(project_id)
        )
        return

    components.section_header(
        str(project.get("name", "")),
        subtitle=str(project.get("audit_area", "") or ""),
        eyebrow="Engagement detail",
    )
    components.badges(
        components.plain_badge(str(project.get("status", "")), theme.ACCENT),
        components.plain_badge("demo data", theme.AI_COLOR) if project.get("is_demo") else "",
        components.plain_badge("{0} control(s) in scope".format(project.get("controls_in_scope", 0))),
        components.plain_badge("{0} evidence file(s)".format(project.get("evidence_files", 0))),
    )

    stats = _read(lambda: data_access.dashboard_stats(project_id), {}, "the project figures")
    assessed = int(stats.get("controls_assessed", 0) or 0)
    in_scope = int(stats.get("controls_in_scope", 0) or 0)
    components.metric_row(
        [
            {"label": "Controls in scope", "value": in_scope},
            {
                "label": "Assessed",
                "value": "{0} / {1}".format(assessed, in_scope) if in_scope else assessed,
                "color": theme.ACCENT,
            },
            {
                "label": "Evidence files",
                "value": int(stats.get("evidence_files", 0) or 0),
                "caption": "{0} chunk(s) indexed".format(stats.get("evidence_chunks", 0)),
            },
            {
                "label": "Reviews pending",
                "value": int(stats.get("pending_human_reviews", 0) or 0),
                "color": theme.AI_COLOR,
                "caption": "No auditor decision recorded yet.",
            },
        ],
        columns=4,
    )
    if in_scope:
        st.progress(
            min(1.0, assessed / float(in_scope)),
            text="{0} of {1} in-scope control(s) have an assessment".format(assessed, in_scope),
        )

    components.kv_grid(
        {
            "Description": project.get("description", ""),
            "Period": project.get("period_label", ""),
            "Lead auditor": project.get("auditor_name", ""),
            "Scope note": project.get("scope_note", ""),
            "Created": project.get("created_at", ""),
            "Last updated": project.get("updated_at", ""),
        }
    )
    if project.get("is_demo"):
        components.note(
            "Demonstration engagement. Its evidence was generated by the synthetic "
            "dataset generator and represents no real organisation."
        )

    scope_tab, progress_tab, edit_tab, delete_tab = st.tabs(
        ["Controls in scope", "Assessment progress", "Edit", "Delete"]
    )
    with scope_tab:
        _scope_panel(project_id)
    with progress_tab:
        _progress_panel(project_id)
    with edit_tab:
        _edit_form(project)
    with delete_tab:
        _delete_panel(project)


def _scope_panel(project_id: int) -> None:
    scoped = _read(lambda: data_access.list_scoped_controls(project_id), [], "the scoped controls")
    if scoped:
        for control in scoped:
            ref = str(control.get("control_id", ""))
            row, action = st.columns([6, 1], gap="small")
            with row:
                components.badges(
                    components.plain_badge(ref, theme.ACCENT),
                    components.plain_badge(str(control.get("category", ""))),
                    components.risk_badge(control.get("inherent_risk")),
                    components.plain_badge("retired", theme.GREY) if not control.get("is_active") else "",
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
                    key="unscope_{0}_{1}".format(project_id, ref),
                    width="stretch",
                    help="Take this control out of scope. Past assessments are kept.",
                ):
                    _unscope(project_id, ref)
    else:
        st.caption("No control is in scope for this engagement yet.")

    st.markdown("---")
    candidates = _read(
        lambda: data_access.list_unscoped_controls(project_id, active_only=True),
        [],
        "the control library",
    )
    if not candidates:
        st.caption("Every active control in the library is already in scope.")
        return
    labels = {
        str(control.get("control_id")): "{0}  {1}".format(
            control.get("control_id"), control.get("name", "")
        )
        for control in candidates
    }
    with st.form("scope_controls_{0}".format(project_id), clear_on_submit=True):
        refs = st.multiselect(
            "Add controls to scope",
            options=sorted(labels),
            format_func=lambda ref: labels.get(ref, ref),
        )
        note = st.text_input(
            "Scope note",
            placeholder="Why these controls are in scope, or how they will be tested.",
            help="Stored on each link created by this submission.",
        )
        if st.form_submit_button("Add to scope", type="primary") and refs:
            _scope(project_id, refs, note)


def _scope(project_id: int, refs: List[str], note: str) -> None:
    try:
        data_access.scope_controls(
            project_id, refs, scope_note=note.strip(), actor=state.auditor_name()
        )
    except data_access.DataAccessError as exc:
        st.error("Could not scope those controls: {0}".format(exc))
        return
    state.flash("Added {0} control(s) to scope.".format(len(refs)), "success")
    st.rerun()


def _unscope(project_id: int, ref: str) -> None:
    try:
        removed = data_access.unscope_control(project_id, ref, actor=state.auditor_name())
    except data_access.DataAccessError as exc:
        st.error("Could not remove {0} from scope: {1}".format(ref, exc))
        return
    state.flash(
        "{0} is no longer in scope. Assessments already written against it are "
        "unchanged.".format(ref)
        if removed
        else "{0} was not in scope.".format(ref),
        "success" if removed else "info",
    )
    st.rerun()


def _progress_panel(project_id: int) -> None:
    scoped = _read(lambda: data_access.list_scoped_controls(project_id), [], "the scoped controls")
    if not scoped:
        st.caption("Scope some controls first; progress is measured against them.")
        return
    assessments = _read(
        lambda: data_access.list_assessments(project_id=project_id, latest_per_control=True),
        [],
        "the assessments",
    )
    by_ref = {str(row.get("control_ref", "")): row for row in assessments}

    components.ai_disclaimer_banner(
        "The status and risk columns are machine-generated. The decision column is the "
        "auditor's, and is the only one that concludes anything.",
        compact=True,
    )
    components.df_table(
        [
            {
                "control_ref": control.get("control_id"),
                "control_name": control.get("name"),
                "status": (by_ref.get(str(control.get("control_id"))) or {}).get(
                    "status", "not assessed"
                ),
                "risk_level": (by_ref.get(str(control.get("control_id"))) or {}).get("risk_level", ""),
                "evidence_sufficiency": (by_ref.get(str(control.get("control_id"))) or {}).get(
                    "evidence_sufficiency", ""
                ),
                "citation_count": (by_ref.get(str(control.get("control_id"))) or {}).get(
                    "citation_count", 0
                ),
                "decision": (by_ref.get(str(control.get("control_id"))) or {}).get(
                    "review_decision", ""
                ),
                "mode_label": (by_ref.get(str(control.get("control_id"))) or {}).get("mode_label", ""),
            }
            for control in scoped
        ],
        columns=[
            "control_ref",
            "control_name",
            "status",
            "risk_level",
            "evidence_sufficiency",
            "citation_count",
            "decision",
            "mode_label",
        ],
        column_config={"decision": st.column_config.TextColumn("Auditor decision", width="medium")},
    )
    st.caption(
        "A control with no row in the assessment table has not been assessed; that is not "
        "the same as having been assessed and found effective."
    )


def _edit_form(project: Dict[str, Any]) -> None:
    project_id = int(project["id"])
    statuses = data_access.project_statuses()
    current_status = str(project.get("status", statuses[0]))
    with st.form("project_edit_{0}".format(project_id)):
        name = st.text_input("Project name", value=str(project.get("name", "")))
        area = st.text_input("Audit area", value=str(project.get("audit_area", "")))
        description = st.text_area(
            "Description", value=str(project.get("description", "") or ""), height=90
        )
        left, middle, right = st.columns(3)
        with left:
            start = st.date_input(
                "Period start",
                value=_as_date(project.get("period_start")),
                key="project_edit_start_{0}".format(project_id),
            )
        with middle:
            end = st.date_input(
                "Period end",
                value=_as_date(project.get("period_end")),
                key="project_edit_end_{0}".format(project_id),
            )
        with right:
            status = st.selectbox(
                "Status",
                options=statuses,
                index=statuses.index(current_status) if current_status in statuses else 0,
            )
        auditor = st.text_input("Lead auditor", value=str(project.get("auditor_name", "") or ""))
        scope_note = st.text_area(
            "Scope note", value=str(project.get("scope_note", "") or ""), height=70
        )
        if st.form_submit_button("Save changes", type="primary"):
            _update(
                project_id,
                name=name.strip(),
                audit_area=area.strip(),
                description=description,
                period_start=_iso(start),
                period_end=_iso(end),
                auditor_name=auditor.strip(),
                status=status,
                scope_note=scope_note,
            )
    st.caption(
        "Only these fields are editable. The demo flag, identifiers and timestamps are "
        "owned by the application, not by the form."
    )


def _as_date(value: Any) -> Any:
    """Parse a stored ISO timestamp back into a ``date`` for ``st.date_input``."""
    if not value:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _update(project_id: int, **fields: Any) -> None:
    if not fields.get("name") or not fields.get("audit_area"):
        st.error("A project needs both a name and an audit area.")
        return
    try:
        data_access.update_project(project_id, actor=state.auditor_name(), **fields)
    except data_access.DataAccessError as exc:
        st.error("Could not save the changes: {0}".format(exc))
        return
    state.flash("Saved.", "success")
    st.rerun()


def _delete_panel(project: Dict[str, Any]) -> None:
    project_id = int(project["id"])
    st.warning(
        "Deleting this engagement also deletes everything recorded under it: "
        "{0} evidence file(s) and their parsed chunks, {1} assessment(s) with their "
        "citations and human reviews, and any report generated for it. This cannot be "
        "undone.".format(project.get("evidence_files", 0), project.get("assessments", 0))
    )
    if project.get("is_demo"):
        st.caption(
            "This is the demonstration engagement. It is recreated empty the next time "
            "the application starts; its ingested evidence is not."
        )
    confirm = st.checkbox(
        "I understand that the evidence, assessments and reviews above will be deleted.",
        key="project_delete_confirm_{0}".format(project_id),
    )
    if st.button(
        "Delete this engagement",
        key="project_delete_{0}".format(project_id),
        disabled=not confirm,
    ):
        _delete(project_id, str(project.get("name", "")))


def _delete(project_id: int, name: str) -> None:
    try:
        deleted = data_access.delete_project(project_id)
    except data_access.DataAccessError as exc:
        st.error("Could not delete the project: {0}".format(exc))
        return
    if deleted:
        state.set_current_project(None)
    state.flash(
        "Deleted “{0}” and everything recorded under it.".format(name)
        if deleted
        else "Project {0} no longer exists.".format(project_id),
        "success" if deleted else "info",
    )
    st.rerun()


# ---- page
def render() -> None:
    components.section_header(
        "Audit projects",
        subtitle="Engagements, their scope and their progress.",
        eyebrow="Engagement",
    )
    _create_form()
    projects = _project_list()

    st.markdown("---")
    project_id = state.current_project_id()
    if project_id is None:
        components.empty_state(
            "No engagement selected",
            "Choose one in the sidebar to see its scope, evidence and progress, or "
            "create a new engagement above."
            if projects
            else "Create an engagement above, or load the demonstration data from the "
            "sidebar, to get started.",
        )
        return
    _detail(project_id)


if __name__ == "__main__":
    render()
