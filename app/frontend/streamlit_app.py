"""Entry point for the audit console.

    .venv/bin/python -m streamlit run app/frontend/streamlit_app.py

This script owns everything that is true on every page: the page configuration, the
stylesheet, the database bootstrap, the sidebar (identity, working project, which model
is answering) and the navigation. A page module owns its own content and nothing else.

Why the shell holds the project selector
----------------------------------------
An auditor works inside one engagement at a time. If each page asked "which project?"
the selection would reset every time they moved between evidence and findings, which is
exactly the navigation an audit involves. The selector therefore lives here and writes
to :mod:`app.frontend.state`, so every page reads the same answer.

Why the provider badge is loud
------------------------------
The offline provider is a deterministic rule-based stand-in, not a language model. A
screenshot of this console taken during a research demonstration must not be mistakable
for a model run, so the sidebar states which provider actually answered - including the
case where a real one was configured but unavailable and the factory fell back.
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
PAGES_DIR = HERE / "pages"

#: The ten pages, in the order the audit workflow runs: plan the engagement, gather
#: evidence, assess, triage findings, review, then report and measure. Each entry is
#: ``(section, module path relative to this file, title, material icon)``. A page whose
#: module is not present yet is skipped rather than crashing the shell, so the
#: application is usable while the page modules are still being written.
PAGE_SPECS: Tuple[Tuple[str, str, str, str], ...] = (
    ("Overview", "pages/dashboard.py", "Dashboard", ":material/dashboard:"),
    ("Engagement", "pages/projects.py", "Audit Projects", ":material/folder_managed:"),
    ("Engagement", "pages/controls.py", "Controls", ":material/checklist:"),
    ("Engagement", "pages/evidence.py", "Evidence", ":material/inventory_2:"),
    ("Assessment", "pages/assessments.py", "Assessments", ":material/fact_check:"),
    ("Assessment", "pages/findings.py", "Findings", ":material/flag:"),
    ("Assessment", "pages/human_review.py", "Human Review", ":material/rate_review:"),
    ("Research", "pages/evaluation.py", "Evaluation", ":material/science:"),
    ("Research", "pages/reports.py", "Reports", ":material/summarize:"),
    ("Research", "pages/settings.py", "Settings", ":material/tune:"),
)

GLOBAL_STATEMENT = "AI assists, the auditor decides."


@st.cache_resource(show_spinner=False)
def _bootstrap() -> Dict[str, Any]:
    """Create the schema and seed the control library once per server process.

    ``st.cache_resource`` rather than a session flag: seeding is a property of the
    process, not of a browser tab, and Streamlit reruns this script on every keystroke.
    Seeding is idempotent anyway (see :mod:`app.database.seed`); the cache just stops it
    being attempted a thousand times.
    """
    try:
        return {"ok": True, "result": data_access.bootstrap_database()}
    except Exception as exc:  # noqa: BLE001 - the sidebar reports this rather than crashing
        return {"ok": False, "error": str(exc)}


def _configure() -> None:
    settings = get_settings()
    st.set_page_config(
        page_title="{0} - Audit Console".format(settings.app_short_name),
        page_icon=":material/security:",
        layout="wide",
        initial_sidebar_state="expanded",
        menu_items={
            "About": (
                "{0} v{1}\n\n"
                "Research prototype. Every assessment it produces is AI-generated and "
                "requires review by a qualified auditor before it becomes an audit "
                "conclusion. All bundled data is synthetic.".format(
                    settings.app_name, settings.app_version
                )
            )
        },
    )
    theme.inject_theme()


def _render_brand() -> None:
    settings = get_settings()
    st.markdown(
        '<div class="ia-brand">'
        '<div class="ia-brand-name">{name}</div>'
        '<div class="ia-brand-sub">{sub}</div>'
        '<div class="ia-brand-rule">{rule}</div>'
        "</div>".format(
            name=components.escape(settings.app_short_name),
            sub=components.escape(
                "LLM-assisted IT audit risk and control assessment - research prototype v{0}".format(
                    settings.app_version
                )
            ),
            rule=components.escape(GLOBAL_STATEMENT),
        ),
        unsafe_allow_html=True,
    )


def _render_provider() -> None:
    try:
        info = data_access.provider_badge()
    except data_access.DataAccessError as exc:
        st.warning("Provider status unavailable: {0}".format(exc))
        return
    components.provider_banner(info)
    if info.get("is_mock"):
        st.caption(components.MOCK_PROVIDER_NOTE)


def _render_project_selector() -> None:
    """The engagement every page works inside. Persisted across pages by state.py."""
    try:
        projects = data_access.list_projects()
    except data_access.DataAccessError as exc:
        st.error("Cannot read projects: {0}".format(exc))
        return

    if not projects:
        st.caption("No audit project exists yet.")
        return

    ids: List[Optional[int]] = [int(item["id"]) for item in projects]
    labels = {
        int(item["id"]): "{0}{1}".format(item.get("name", "?"), " (demo)" if item.get("is_demo") else "")
        for item in projects
    }
    current = state.current_project_id()
    index = ids.index(current) if current in ids else 0

    chosen = st.selectbox(
        "Audit project",
        options=ids,
        index=index,
        format_func=lambda value: labels.get(value, str(value)),
        key="sidebar_project_select",
    )
    record = next((item for item in projects if int(item["id"]) == int(chosen)), None)
    state.set_current_project(record or chosen)

    if record:
        st.caption(
            "{0} · {1} · {2} control(s) in scope".format(
                record.get("audit_area", ""),
                record.get("status", ""),
                record.get("controls_in_scope", 0),
            )
        )


def _render_auditor() -> None:
    settings = get_settings()
    current = state.auditor_name(settings.default_auditor_name)
    name = st.text_input(
        "Reviewing auditor",
        value=current,
        key="sidebar_auditor_name",
        help=(
            "Attributed to every human review you record. This prototype has no login, "
            "so this is a declared name, not an authenticated identity."
        ),
    )
    state.set_auditor_name(name)


def _render_demo_tools() -> None:
    """The first-run escape hatch, also reachable once data exists."""
    with st.expander("Demo data", expanded=False):
        st.caption(
            "Creates the demonstration engagement, scopes its controls and ingests the "
            "generated synthetic evidence. Everything it creates is fabricated for "
            "research use. Safe to press twice - files already ingested are skipped."
        )
        if st.button(
            "Load demo project + synthetic evidence",
            key="sidebar_load_demo",
            type="primary",
            width="stretch",
        ):
            _load_demo()


def _load_demo() -> None:
    with st.spinner("Generating synthetic evidence and ingesting it…"):
        try:
            summary = data_access.load_demo_project()
        except data_access.DataAccessError as exc:
            st.error("Could not load the demo project: {0}".format(exc))
            return
    ingested = len(summary.get("ingested", []) or [])
    skipped = len(summary.get("skipped", []) or [])
    failures = summary.get("failures", []) or []
    state.set_current_project(summary.get("project_id"))
    state.flash(
        "Demo project ready: {0}. {1} file(s) ingested, {2} already present.".format(
            summary.get("project_name", ""), ingested, skipped
        ),
        "success",
    )
    for failure in failures:
        state.flash(
            "Could not ingest {0}: {1}".format(failure.get("filename"), failure.get("error")),
            "warning",
        )
    st.rerun()


def _render_backend_note(boot: Dict[str, Any]) -> None:
    st.markdown("---")
    st.caption("Backend: {0}".format(data_access.backend_label()))
    if not boot.get("ok"):
        st.error("Database bootstrap failed: {0}".format(boot.get("error", "")))
    st.caption(
        "Every assessment in this application is AI-generated and requires auditor "
        "review. Nothing here is assurance or a statement of compliance."
    )


def _build_navigation() -> Any:
    """Assemble ``st.navigation`` from the page modules that are present."""
    groups: Dict[str, List[Any]] = {}
    for section, relative_path, title, icon in PAGE_SPECS:
        if not (HERE / relative_path).exists():
            continue
        groups.setdefault(section, []).append(st.Page(relative_path, title=title, icon=icon))
    if not groups:
        return st.navigation(
            [st.Page(_first_run_page, title="Get started", icon=":material/security:")]
        )
    return st.navigation(groups)


def _first_run_page() -> None:
    """Shown when no page module is installed yet - the shell still has to be usable.

    It reports the state of the database rather than assuming it is empty, so that a
    build without page modules is still an honest window onto what is actually stored.
    """
    components.section_header(
        "Audit console",
        subtitle="The workspace pages are not installed in this build.",
        eyebrow=get_settings().app_name,
    )
    st.write(
        "The application shell, theme, shared components and data-access facade are "
        "loaded. Page modules are expected under `app/frontend/pages/`:"
    )
    st.code("\n".join(path for _, path, _, _ in PAGE_SPECS), language="text")

    project_id = state.current_project_id()
    if project_id is None:
        return
    try:
        stats = data_access.dashboard_stats(project_id)
    except data_access.DataAccessError as exc:
        st.error("Cannot read the dashboard figures: {0}".format(exc))
        return
    components.section_header(
        state.current_project_name(),
        subtitle="Read through the data-access facade, so the database is reachable.",
        eyebrow="Selected engagement",
    )
    components.metric_row(
        [
            {"label": "Controls in scope", "value": stats["controls_in_scope"]},
            {"label": "Assessed", "value": stats["controls_assessed"], "color": theme.ACCENT},
            {
                "label": "Potential deficiencies",
                "value": stats["potential_deficiencies"],
                "color": theme.status_color("POTENTIAL_DEFICIENCY"),
            },
            {
                "label": "Pending human review",
                "value": stats["pending_human_reviews"],
                "color": theme.AI_COLOR,
                "caption": "No AI assessment is an audit conclusion until an auditor records one.",
            },
            {"label": "Evidence files", "value": stats["evidence_files"]},
        ]
    )
    components.note(stats["definitions"]["counting_basis"])


def _render_first_run_callout(first_run: Dict[str, Any]) -> None:
    """The nothing-to-look-at call to action, shown above whatever page is displayed.

    The condition is "no evidence and no assessments", not "no projects": bootstrapping
    creates the demonstration project, so a brand-new database always has one and is
    still empty in every way an auditor would notice.
    """
    if first_run.get("projects"):
        st.warning(
            "No evidence has been ingested and no control has been assessed yet. Load "
            "the demonstration evidence to see the full workflow - synthetic policies, "
            "configuration exports and user listings, then assessments and the human "
            "review queue - or upload your own on the Evidence page."
        )
    else:
        st.warning(
            "This database has no audit project yet. Load the demonstration engagement, "
            "or create a project of your own on the Audit Projects page."
        )
    if components.empty_state(
        "Nothing to audit yet",
        "The demo engagement scopes five synthetic IT controls and ingests generated "
        "policy documents, configuration exports and user listings. All of it is "
        "fabricated for research use and represents no real organisation.",
        action_label="Load demo project + synthetic evidence",
        action_key="first_run_load_demo",
    ):
        _load_demo()


def main() -> None:
    _configure()
    settings = get_settings()
    state.init_state(auditor_name=settings.default_auditor_name)
    boot = _bootstrap()

    with st.sidebar:
        _render_brand()

    page = _build_navigation()

    with st.sidebar:
        _render_project_selector()
        _render_auditor()
        _render_provider()
        _render_demo_tools()
        _render_backend_note(boot)

    state.render_flashes()

    # The empty-database case is handled by the shell rather than by each page, so the
    # call to action is present no matter where the user landed.
    try:
        first_run = data_access.first_run_state()
    except Exception:  # noqa: BLE001 - a failed count must not block the page
        first_run = {"needs_data": False}
    if first_run.get("needs_data"):
        _render_first_run_callout(first_run)
        st.markdown("---")

    page.run()


main()
