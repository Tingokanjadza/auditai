"""Reports - generate, read and download the working paper for one audit project.

The report is the artefact that leaves the tool, so it is also the last place where an
AI proposal could quietly become an audit finding. ``app.audit.report`` prevents that in
the document itself - the AI assessment and the auditor's conclusion are printed as
separate sections, and controls nobody has reviewed are listed as pending rather than
counted as conclusions. This page keeps that visible at the point of generation: before
the form it says how many assessed controls still lack an auditor decision, and beside
each report it shows how many controls were actually concluded by a person.

Previewing rather than only downloading matters for the same reason. A report that is
downloaded unread is a report whose limitations section was never read.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# A page module is exec'd by ``st.Page`` in the entry script's process, where the
# repository root is already on ``sys.path``. Repeated here so the module also imports
# cleanly when loaded directly (a test, ``streamlit run`` on this file).
_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st  # noqa: E402

from app.frontend import components, data_access, state, theme  # noqa: E402

#: Radio labels -> the format token the generator accepts. HTML first: it opens in any
#: browser and prints as-is, which is what an auditor filing a working paper wants.
_FORMAT_CHOICES = (("HTML (.html)", "html"), ("Markdown (.md)", "markdown"))
_FORMAT_LABELS = {"html": "HTML", "markdown": "Markdown"}
_MIME = {"markdown": "text/markdown", "html": "text/html"}
_EXTENSION = {"markdown": "md", "html": "html"}

_DEFAULT_AUTHOR = "Auditor"


# ---- small helpers
def _format_label(fmt: Any) -> str:
    token = str(fmt or "").strip().lower()
    return _FORMAT_LABELS.get(token, token.title() or "-")


def _safe_filename(title: Any, report_id: Any, fmt: str) -> str:
    """``"IT audit report - Q3 access review"`` -> ``"IT_audit_report_-_Q3_access_review_report_7.html"``."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", str(title or "audit")).strip("_")[:60] or "audit"
    return "{0}_report_{1}.{2}".format(stem, report_id, _EXTENSION.get(fmt, "txt"))


def _report_dir_text() -> str:
    """Where the generator wrote the file. Over HTTP the server keeps its paths to itself."""
    if data_access.use_api():
        return "the server's report folder"
    try:
        from app.config import get_settings

        return str(get_settings().report_dir)
    except Exception:  # noqa: BLE001 - a caption must never take the page down
        return "the report folder"


def _has_evaluation_assessments(project_id: int) -> bool:
    """Whether the research harness left assessments on this audit project.

    ``include_evaluation`` on the generator folds those rows into the document, so the
    checkbox is only offered when there is something for it to include. A read failure
    answers False: the option is then simply absent, and the report is still generated.
    """
    try:
        with_eval = data_access.list_assessments(project_id=project_id, include_evaluation=True)
    except data_access.DataAccessError:
        return False
    if any(row.get("evaluation_run_id") is not None for row in with_eval if "evaluation_run_id" in row):
        return True
    if any("evaluation_run_id" in row for row in with_eval):
        return False
    try:
        without = data_access.list_assessments(project_id=project_id, include_evaluation=False)
    except data_access.DataAccessError:
        return False
    return len(with_eval) > len(without)


def _selected_project() -> Optional[int]:
    project_id = state.current_project_id()
    if project_id is not None:
        return project_id
    components.empty_state(
        "No audit project selected",
        "Choose an audit project in the sidebar. A report is always the report of one "
        "audit, over the controls in its scope.",
    )
    components.next_steps([("Open Audit Projects", "views/audit_projects.py", ":material/folder_managed:")])
    return None


# ---- pre-flight
def _render_preflight(stage: Dict[str, Any]) -> None:
    """Say, before the form, what the document is going to be able to conclude."""
    pending = int(stage.get("pending_reviews", 0) or 0)
    assessed = int(stage.get("controls_assessed", 0) or 0)
    if assessed == 0:
        st.info(
            "No control in this audit project has been assessed yet, so a report generated "
            "now would list every control as not assessed. Run the AI assessment first."
        )
        components.next_steps([("Go to Assessments", "views/assessments.py", ":material/fact_check:")])
        return
    if pending > 0:
        st.warning(
            "{0} assessed control(s) have no auditor decision yet. They will appear as "
            "'Pending auditor review' and are excluded from every conclusive figure.".format(pending)
        )
        components.next_steps([("Review them now", "views/human_review.py", ":material/rate_review:")])
        return
    st.success("All {0} assessed controls have an auditor decision.".format(assessed))


# ---- generator
def _render_generator(project_id: int) -> None:
    components.section_header(
        "Generate a report",
        subtitle="One document per run. Earlier reports are kept below.",
    )
    audit_name = state.current_project_name("this audit")
    default_title = "IT audit report - {0}".format(audit_name)
    offer_evaluation = _has_evaluation_assessments(project_id)
    author = state.auditor_name()

    with st.form("report_form"):
        left, right = st.columns([3, 2], gap="medium")
        with left:
            title = st.text_input(
                "Report title (optional)",
                key="report_title",
                placeholder=default_title,
                help="Left blank, the report is titled '{0}'.".format(default_title),
            )
        with right:
            fmt_label = st.radio(
                "Format",
                options=[choice[0] for choice in _FORMAT_CHOICES],
                key="report_format",
                horizontal=True,
                help=(
                    "HTML opens in any browser and prints as-is; choose Markdown to paste "
                    "into another document"
                ),
            )
        include_evaluation = False
        if offer_evaluation:
            include_evaluation = st.checkbox(
                "Include the research experiment appendix",
                key="report_eval",
                help=(
                    "This audit also carries assessments produced by the research "
                    "experiments. Tick to add them to the document; leave off for a "
                    "working paper about the controls."
                ),
            )
        st.caption(
            (
                "Generated by {0} (change your name in the sidebar)".format(author)
                if author
                else "Generated by: no name yet - enter your name in the sidebar so the report records who produced it"
            )
        )
        with st.expander("More options", expanded=False):
            override = st.text_input(
                "Record a different name on this report",
                key="report_author_override",
                placeholder=author or _DEFAULT_AUTHOR,
                help="A declared name, not an authenticated identity. Leave blank to use the sidebar name.",
            )
        submitted = st.form_submit_button("Generate report", type="primary")

    if not submitted:
        return

    fmt = dict(_FORMAT_CHOICES).get(str(fmt_label), "html")
    generated_by = (override or "").strip() or author or _DEFAULT_AUTHOR
    with st.spinner("Building the report..."):
        try:
            report = data_access.generate_report(
                project_id,
                generated_by=generated_by,
                fmt=fmt,
                title=(title or "").strip() or default_title,
                include_evaluation=bool(include_evaluation),
            )
        except data_access.DataAccessError as exc:
            components.error_with_remedy("The report could not be generated.", exc)
            return

    report_id = int(report.get("id")) if report.get("id") is not None else None
    state.set_current_report(report_id)
    # The history table's remembered selection would otherwise re-select the previous
    # report on the very next run; forgetting the pick lets the new report show.
    st.session_state["report_history_pick"] = None
    state.flash("Report #{0} generated.".format(report_id), "success")
    st.rerun()


# ---- the report
def _render_headline_tiles(stats: Dict[str, Any]) -> None:
    components.metric_row(
        [
            {
                "label": "Concluded by an auditor",
                "value": stats.get("controls_concluded_by_auditor", 0),
                "color": theme.HUMAN_COLOR,
                "caption": "Controls with a recorded auditor decision.",
            },
            {
                "label": "Pending review",
                "value": stats.get("controls_pending_review", 0),
                "color": theme.AI_COLOR,
                "caption": "Assessed by the AI, no auditor decision yet. Not counted as conclusions.",
            },
            {
                "label": "Confirmed deficiencies",
                "value": stats.get("confirmed_deficiencies", 0),
                "color": theme.status_color("POTENTIAL_DEFICIENCY"),
                "caption": "Auditor-confirmed only; AI proposals are not counted.",
            },
        ],
        columns=3,
    )


def _render_provenance(report: Dict[str, Any], stats: Dict[str, Any]) -> None:
    """The remaining figures, the provenance grid and the counting basis - in one place."""
    agreement = stats.get("human_ai_status_agreement_rate")
    components.metric_row(
        [
            {
                "label": "Controls reported",
                "value": stats.get("controls_reported", 0),
                "caption": "{0} in scope, {1} assessed.".format(
                    stats.get("controls_in_scope", 0), stats.get("controls_assessed", 0)
                ),
            },
            {
                "label": "Confirmed high risk",
                "value": stats.get("confirmed_high_risk_findings", 0),
                "color": theme.risk_color("HIGH"),
                "caption": "Auditor-confirmed only.",
            },
            {
                "label": "Auditor departed from the AI",
                "value": stats.get("controls_status_divergent", 0),
                "color": theme.AI_COLOR,
                "caption": "Status agreement {0}.".format(
                    "n/a" if agreement is None else "{0:.0%}".format(float(agreement))
                ),
            },
            {
                "label": "Citations in the report",
                "value": stats.get("citations_total", 0),
                "caption": "{0} not verified.".format(stats.get("citations_unverified", 0)),
            },
        ],
        columns=4,
    )
    verdicts = dict(stats.get("citation_verdicts") or {})
    components.kv_grid(
        {
            "Generated": components.when(report.get("generated_at")),
            "Generated by": report.get("generated_by", ""),
            "Format": _format_label(report.get("format")),
            "Prompt version": stats.get("prompt_version", ""),
            "Application version": stats.get("app_version", ""),
            "Providers / models": "{0} / {1}".format(
                ", ".join(
                    components.provider_display_name(item) for item in (stats.get("llm_providers") or [])
                )
                or "not recorded",
                ", ".join(str(item) for item in (stats.get("llm_models") or [])) or "not recorded",
            ),
            "Experiment conditions": ", ".join(
                components.label("mode", item) for item in (stats.get("experiment_modes") or [])
            ),
            "Evidence files referenced": "{0} ({1} synthetic)".format(
                stats.get("evidence_files", 0), stats.get("evidence_synthetic_files", 0)
            ),
            "Citation checks": ", ".join(
                "{0}: {1}".format(components.label("verdict", key), value) for key, value in verdicts.items()
            ),
        }
    )
    notes = [str(stats.get("counting_basis", "") or "").strip(), str(stats.get("risk_model_label", "") or "").strip()]
    components.note(" ".join(text for text in notes if text))


def _render_report(report: Dict[str, Any]) -> None:
    stats = dict(report.get("summary_stats") or {})
    fmt = str(report.get("format") or "markdown").lower()
    content = str(report.get("content") or "")
    title = str(report.get("title") or "Audit report")

    components.section_header(
        title,
        subtitle="Generated {0} · {1} · by {2}".format(
            components.when(report.get("generated_at")) or "-",
            _format_label(fmt),
            report.get("generated_by") or "unnamed",
        ),
        eyebrow="Report #{0}".format(report.get("id")),
    )

    if content:
        components.download_row(
            "Download report ({0})".format(_format_label(fmt)),
            content,
            _safe_filename(title, report.get("id"), fmt),
            mime=_MIME.get(fmt, "text/plain"),
            key="report_download",
        )
    if report.get("has_file") or report.get("stored_path"):
        st.caption("Also saved to {0}".format(_report_dir_text()))

    if stats:
        _render_headline_tiles(stats)
    else:
        st.caption("This report recorded no summary figures.")

    components.ai_disclaimer_banner(
        "This document contains AI-generated assessments. Sections that carry an auditor "
        "conclusion are labelled as such inside the document; everything else is a "
        "proposal awaiting review."
    )
    if stats.get("used_mock_provider"):
        st.warning(
            "At least one assessment in this report was produced in Demo mode (offline "
            "rules), not by a language model. " + components.MOCK_PROVIDER_NOTE
        )

    if not content:
        st.caption("This report has no stored content.")
    elif fmt == "html":
        # Rendered inside an isolated frame rather than injected into the page: the
        # document brings its own stylesheet, and evidence text quoted inside it is
        # untrusted input that must not be able to touch the console's own DOM.
        from streamlit.components.v1 import html as embed_html

        embed_html(content, height=720, scrolling=True)
    else:
        with st.container(height=720, border=True):
            # unsafe_allow_html stays off: the report is Markdown, and any HTML inside it
            # came from parsed evidence.
            st.markdown(content)

    if stats:
        with st.expander("Provenance and how the figures are counted", expanded=False):
            _render_provenance(report, stats)


# ---- history
def _render_history(reports: List[Dict[str, Any]], current: Optional[int]) -> None:
    with st.expander("Previous reports ({0})".format(len(reports)), expanded=False):
        table = [
            {
                "id": row.get("id"),
                "title": row.get("title") or "Audit report",
                "format": _format_label(row.get("format")),
                "generated_at": row.get("generated_at"),
                "generated_by": row.get("generated_by") or "unnamed",
            }
            for row in reports
        ]
        event = components.df_table(
            table,
            columns=["id", "title", "format", "generated_at", "generated_by"],
            column_config={
                "id": st.column_config.NumberColumn("#", width="small", format="%d"),
                "title": st.column_config.TextColumn("Title", width="large"),
                "format": st.column_config.TextColumn("Format", width="small"),
                "generated_at": st.column_config.DatetimeColumn(
                    "Generated", width="medium", format="YYYY-MM-DD HH:mm"
                ),
                "generated_by": st.column_config.TextColumn("By", width="medium"),
            },
            key="report_history",
            on_select="rerun",
            selection_mode="single-row",
        )
        st.caption("Select a row to open that report above.")

    rows: List[int] = []
    try:
        rows = list(event.selection.rows) if event is not None else []
    except Exception:  # noqa: BLE001 - no selection event means nothing was picked
        rows = []
    if not rows:
        return
    picked = int(rows[0])
    # Only act on a *new* pick: the dataframe remembers its selection across reruns, and
    # re-applying it would drag the view back to the old row after a fresh generation.
    if st.session_state.get("report_history_pick") == picked:
        return
    st.session_state["report_history_pick"] = picked
    if picked < len(table) and table[picked].get("id") is not None:
        chosen = int(table[picked]["id"])
        if chosen != current:
            state.set_current_report(chosen)
            st.rerun()


# ---- page
def render() -> None:
    components.section_header(
        "Reports",
        subtitle=(
            "The working paper for this audit. AI assessments and auditor conclusions are "
            "kept in separate sections."
        ),
    )
    project_id = _selected_project()
    if project_id is None:
        return

    try:
        stage = data_access.project_stage(project_id)
    except data_access.DataAccessError as exc:
        components.error_with_remedy("Could not read where this audit stands.", exc, retry_key="report_retry_stage")
        return

    _render_preflight(stage)
    _render_generator(project_id)

    try:
        reports = data_access.list_reports(project_id=project_id)
    except data_access.DataAccessError as exc:
        components.error_with_remedy("Could not read the report history.", exc, retry_key="report_retry_history")
        return

    st.markdown("---")
    if not reports:
        components.empty_state(
            "No report yet for {0}".format(state.current_project_name("this audit")),
            "Press 'Generate report' above. Reports are kept, so a figure quoted in a "
            "write-up can always be traced back to the document it came from.",
        )
        return

    ids = [int(row["id"]) for row in reports if row.get("id") is not None]
    current = state.current_report_id()
    if current not in ids:
        current = ids[0] if ids else None
        state.set_current_report(current)

    report: Optional[Dict[str, Any]] = None
    if current is not None:
        try:
            report = data_access.get_report(int(current))
        except data_access.DataAccessError as exc:
            components.error_with_remedy("Could not load the report.", exc, retry_key="report_retry_report")
        else:
            if report is None:
                st.warning("That report no longer exists. Pick another one below.")
                state.set_current_report(None)

    if report is not None:
        _render_report(report)

    _render_history(reports, current)


if __name__ == "__main__":
    render()
