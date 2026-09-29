"""Entry point for the audit console.

    .venv/bin/python -m streamlit run app/frontend/streamlit_app.py

This script owns everything that is true on every page: the page configuration, the
stylesheet, the database bootstrap, the navigation, the sidebar (which audit, who is
reviewing, which model is answering) and the "Next step" line above each workflow page.
A view module owns its own content and nothing else.

Why the view modules live in ``views/`` and not ``pages/``
----------------------------------------------------------
Streamlit treats a directory literally named ``pages/`` next to the entry script as its
legacy automatic multipage mechanism and would register every file in it a second time,
alphabetically, with filenames for titles. Naming the directory ``views/`` opts out of
that so :func:`st.navigation` is the only thing deciding what appears in the sidebar and
in what order. Every view is registered unconditionally: a missing module is a build
error that should be seen, not a page that silently disappears from the menu.

Why the project selector does the pending-switch dance
------------------------------------------------------
An auditor works inside one audit project at a time, so the selector lives here and
every page reads the same answer from :mod:`app.frontend.state`. Streamlit keys a
selectbox on ``key`` + ``options`` and, once drawn, the widget's stored value beats the
``index=`` argument on every later run - so a page that changed the project itself would
be overridden by the sidebar one rerun later. Pages therefore call
:func:`app.frontend.state.request_project_switch` and rerun; this shell pops the request
with :func:`app.frontend.state.take_pending_project_switch` and writes it into the
widget's own session-state key *before* the selectbox is instantiated (writing a widget
key before its widget exists in the run is permitted; afterwards it raises). The
selectbox is then drawn already pointing at the requested project and its value flows
into :func:`app.frontend.state.set_current_project` as on any other run.

Why there is exactly one provider block
---------------------------------------
The offline provider is a rule-based stand-in, not a language model. The sidebar says
which provider actually answered in one place - "Demo mode" in plain words when the mock
was asked for, and red with a remedy when a real provider was configured but the mock
answered because it was unusable. Two blocks saying the same thing in different words
would train the eye to skip both; one block, always in the same spot, is the claim a
screenshot of this console has to get right.
"""

from __future__ import annotations

import sys
from pathlib import Path

# ``streamlit run`` puts this file's own directory on sys.path, not the repository root,
# so ``import app...`` has to be made possible before any application import happens.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from typing import Any, Dict, List, Optional, Tuple  # noqa: E402

import streamlit as st  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.frontend import components, data_access, state, theme  # noqa: E402

HERE = Path(__file__).resolve().parent

#: Every view, in sidebar order: ``(section, module path relative to this file, title,
#: material icon, url path)``. The url path ``""`` marks the default page. The "Audit"
#: section is the workflow an auditor walks through; the other sections stand outside
#: any one audit project and therefore get no "Next step" line.
PAGE_SPECS: Tuple[Tuple[str, str, str, str, str], ...] = (
    ("Home", "views/home.py", "Home", ":material/home:", ""),
    ("Audit", "views/audit_projects.py", "Audit projects", ":material/folder_managed:", "projects"),
    ("Audit", "views/controls.py", "Controls", ":material/checklist:", "controls"),
    ("Audit", "views/evidence.py", "Evidence", ":material/inventory_2:", "evidence"),
    ("Audit", "views/assessments.py", "Assessments", ":material/fact_check:", "assessments"),
    ("Audit", "views/human_review.py", "Review queue", ":material/rate_review:", "review"),
    ("Audit", "views/findings.py", "Findings", ":material/flag:", "findings"),
    ("Audit", "views/reports.py", "Reports", ":material/summarize:", "reports"),
    ("Learn", "views/how_it_works.py", "How it works", ":material/school:", "how-it-works"),
    ("Research", "views/evaluation.py", "Experiments", ":material/science:", "experiments"),
    ("Setup", "views/settings.py", "Settings", ":material/tune:", "settings"),
)

#: The section whose pages belong to one audit project's workflow.
WORKFLOW_SECTION = "Audit"

#: Url paths of the pages that show the compact workflow strip.
#: Pages that render the full workflow strip themselves; the shell's compact line is
#: skipped there.
FULL_STRIP_PAGES = frozenset({"projects"})
WORKFLOW_URL_PATHS = frozenset(
    url_path for section, _, _, _, url_path in PAGE_SPECS if section == WORKFLOW_SECTION
)

#: Titles of the same pages, used when a navigation result carries no url path.
WORKFLOW_TITLES = frozenset(
    title for section, _, title, _, _ in PAGE_SPECS if section == WORKFLOW_SECTION
)

HOME_PAGE = "views/home.py"
SETTINGS_PAGE = "views/settings.py"

GLOBAL_STATEMENT = "AI assists, the auditor decides."

#: Widget key of the sidebar name field. Not project-scoped: the auditor's name follows
#: them from one audit project to the next.
K_SIDEBAR_AUDITOR = "sidebar_auditor_name"


@st.cache_resource(show_spinner=False)
def _bootstrap() -> Dict[str, Any]:
    """Create the schema and seed the control library once per server process.

    ``st.cache_resource`` rather than a session flag: seeding is a property of the
    process, not of a browser tab, and Streamlit reruns this script on every keystroke.
    Seeding is idempotent anyway (see :mod:`app.database.seed`); the cache just stops it
    being attempted a thousand times. The exception is deliberately allowed to escape:
    ``st.cache_resource`` does not cache a raised exception, so a failed bootstrap is
    retried on the next run and the call site can show the error and a Retry button.
    """
    return dict(data_access.bootstrap_database())


def _configure() -> None:
    settings = get_settings()
    st.set_page_config(
        page_title="{0} - audit console".format(settings.app_short_name),
        page_icon=":material/security:",
        layout="wide",
        initial_sidebar_state="expanded",
        menu_items={
            "About": (
                "{0} v{1} - {2}\n\n"
                "Research prototype. Every assessment {0} produces is AI-generated and "
                "must be reviewed by a qualified auditor before it becomes an audit "
                "conclusion; the AI record and the auditor's decision are kept "
                "separately. All bundled data is synthetic.".format(
                    settings.app_short_name, settings.app_version, settings.app_name
                )
            )
        },
    )
    theme.inject_theme()


def _page_link(page: str, label_text: str, icon: str = "") -> None:
    """``st.page_link`` that degrades to a caption when the target is not registered.

    Under ``streamlit.testing`` no page is registered, and a view module that is still
    being written must not take the sidebar down with it.
    """
    try:
        st.page_link(page, label=label_text, icon=icon or None)
    except Exception:  # noqa: BLE001 - navigation is a convenience, never a dependency
        st.caption(label_text)


def _render_brand() -> None:
    settings = get_settings()
    st.markdown(
        '<div class="ia-brand">'
        '<div class="ia-brand-name">{name}</div>'
        '<div class="ia-brand-sub">{sub}</div>'
        "</div>".format(
            name=components.escape(settings.app_short_name),
            sub=components.escape("AI-assisted IT audit - research prototype"),
        ),
        unsafe_allow_html=True,
    )


def _project_caption(record: Dict[str, Any]) -> str:
    """``audit area · friendly status · n controls in scope`` from the non-empty parts."""
    parts: List[str] = []
    area = str(record.get("audit_area", "") or "").strip()
    if area:
        parts.append(area)
    status = components.label("project_status", record.get("status"))
    if status:
        parts.append(status)
    count = record.get("controls_in_scope")
    if count is not None:
        count = int(count or 0)
        parts.append("{0} {1} in scope".format(count, "control" if count == 1 else "controls"))
    return " · ".join(parts)


def _render_project_selector() -> None:
    """The audit project every page works inside. Persisted across pages by state.py.

    The pending switch is applied to the widget key *before* the selectbox exists in
    this run (see the module docstring), so the unconditional
    :func:`state.set_current_project` after the widget is correct: the widget already
    holds the project the page asked for.
    """
    try:
        projects = data_access.list_projects()
    except data_access.DataAccessError as exc:
        components.error_with_remedy("The audit projects could not be read.", exc)
        return

    if not projects:
        state.set_current_project(None)
        st.caption("No audit project yet")
        _page_link(HOME_PAGE, "Start an audit or try the demo", ":material/home:")
        return

    ids: List[int] = [int(item["id"]) for item in projects]
    labels = {
        int(item["id"]): "{0}{1}".format(
            item.get("name", "?"), " (demo)" if item.get("is_demo") else ""
        )
        for item in projects
    }

    pending = state.take_pending_project_switch()
    if pending in ids:
        st.session_state[state.K_PROJECT_WIDGET] = pending
    elif st.session_state.get(state.K_PROJECT_WIDGET) not in ids:
        # The project the widget last pointed at was deleted: forget it rather than let
        # the selectbox be created with a value that is not among its options.
        st.session_state.pop(state.K_PROJECT_WIDGET, None)

    current = state.current_project_id()
    index = ids.index(current) if current in ids else 0
    chosen = st.selectbox(
        "Current audit",
        options=ids,
        index=(index if state.K_PROJECT_WIDGET not in st.session_state else 0),
        key=state.K_PROJECT_WIDGET,
        format_func=lambda value: labels.get(value, str(value)),
        help="Every page shows this audit project. Pick another to switch.",
    )
    record = next((item for item in projects if int(item["id"]) == int(chosen)), None)
    state.set_current_project(record or chosen)

    if record:
        caption = _project_caption(record)
        if caption:
            st.caption(caption)


def _render_auditor() -> None:
    """The declared name every decision is attributed to.

    A name entered elsewhere (the New audit dialog) is copied into the widget key before
    the widget is drawn so it appears here on the next run; afterwards the text input
    owns the value and :func:`state.set_auditor_name` mirrors it.
    """
    if st.session_state.get(K_SIDEBAR_AUDITOR, "") == "" and state.auditor_name():
        st.session_state[K_SIDEBAR_AUDITOR] = state.auditor_name()
    name = st.text_input(
        "Your name",
        key=K_SIDEBAR_AUDITOR,
        placeholder="Recorded on every decision you make",
        help=(
            "Attributed to every review you record and every audit project you create. "
            "This prototype has no login, so this is a declared name, not an "
            "authenticated identity."
        ),
    )
    state.set_auditor_name(name)
    if not state.auditor_name():
        st.caption(":orange[Enter your name before recording a review]")


def _fallback_remedy(info: Dict[str, Any]) -> str:
    configured_id = str(info.get("configured_provider", "") or "").strip().lower()
    configured = components.provider_display_name(info.get("configured_provider"))
    key_name = (
        "ANTHROPIC_API_KEY" if configured_id in ("claude", "anthropic") else "its API key"
    )
    return (
        "{0} was configured but Demo mode answered. Add {1} to .env and restart, or set "
        "LLM_PROVIDER=mock to run offline on purpose.".format(configured, key_name)
    )


def _render_provider() -> None:
    """The sidebar's answer to "which AI produced what I am looking at?".

    The banner names the provider that *answered*. When that is not the provider the
    ``.env`` selected - Claude configured, no ``ANTHROPIC_API_KEY``, so the offline
    stand-in ran - a native ``st.error`` underneath says so and says what to do about
    it. The alert is plain Streamlit rather than styled HTML so it survives a stylesheet
    that failed to load. A researcher must never believe a run used Claude when it did
    not.
    """
    try:
        info = dict(data_access.provider_badge())
    except data_access.DataAccessError as exc:
        st.warning("Provider status unavailable: {0}".format(exc))
        _page_link(SETTINGS_PAGE, "AI provider settings", ":material/tune:")
        return
    components.provider_banner(info, plain=True)
    if info.get("fell_back_to_mock"):
        st.error(_fallback_remedy(info), icon=":material/error:")
    _page_link(SETTINGS_PAGE, "AI provider settings", ":material/tune:")


def _render_footer() -> None:
    settings = get_settings()
    st.caption(GLOBAL_STATEMENT)
    st.caption(
        "{0} v{1} · research prototype · synthetic data only".format(
            settings.app_short_name, settings.app_version
        )
    )


def _build_navigation() -> Any:
    """Assemble ``st.navigation`` from every view, grouped by section, Home as default."""
    groups: Dict[str, List[Any]] = {}
    for section, relative_path, title, icon, url_path in PAGE_SPECS:
        groups.setdefault(section, []).append(
            st.Page(
                relative_path,
                title=title,
                icon=icon,
                url_path=url_path,
                default=(url_path == ""),
            )
        )
    return st.navigation(groups)


def _is_workflow_page(page: Any) -> bool:
    """True for the pages of the "Audit" section, where the "Next step" line belongs."""
    url_path = getattr(page, "url_path", None)
    if isinstance(url_path, str):
        return url_path.strip("/") in WORKFLOW_URL_PATHS
    return str(getattr(page, "title", "") or "") in WORKFLOW_TITLES


def _render_workflow_line(page: Any) -> None:
    """One "Next step" link above every workflow page; nothing when it cannot be read."""
    project_id = state.current_project_id()
    if project_id is None or not _is_workflow_page(page):
        return
    # The Audit projects page draws the full five-step strip in its detail panel, so a
    # second, compact copy above it would say the same thing twice.
    if str(getattr(page, "url_path", "") or "").strip("/") in FULL_STRIP_PAGES:
        return
    try:
        stage_info = data_access.project_stage(project_id)
    except data_access.DataAccessError:
        return
    components.workflow_strip(stage_info, compact=True)


def _render_bootstrap_failure(exc: BaseException) -> None:
    components.error_with_remedy("The database could not be initialised.", exc)
    st.caption(
        "Check DATABASE_URL in .env and that the data directory is writable, then retry."
    )
    if st.button("Retry", key="shell_bootstrap_retry", type="primary"):
        _bootstrap.clear()
        st.rerun()


def main() -> None:
    _configure()
    settings = get_settings()
    state.init_state(auditor_name=settings.default_auditor_name)

    with st.sidebar:
        _render_brand()

    page = _build_navigation()

    try:
        _bootstrap()
    except Exception as exc:  # noqa: BLE001 - shown with a remedy, then the run stops
        with st.sidebar:
            _render_footer()
        _render_bootstrap_failure(exc)
        st.stop()

    with st.sidebar:
        _render_project_selector()
        _render_auditor()
        _render_provider()
        _render_footer()

    state.render_flashes()
    _render_workflow_line(page)

    page.run()


main()
