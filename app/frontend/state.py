"""Named, typed accessors for Streamlit's session state.

``st.session_state`` is a single flat namespace shared by ten page modules written by
different hands. Left to itself that becomes ten spellings of "the project we are
looking at", and a filter set on one page silently resurfacing on another. Every key
this application stores is therefore declared here as a constant with a getter and a
setter, so the namespace is greppable and a page never has to guess.

Two things are deliberately *not* stored here:

* **Widget values.** Streamlit already owns those under the widget's own ``key``. Copying
  them into a second key means two sources of truth and a rerun where they disagree.
* **Anything fetched from the database.** Session state is per browser session and
  survives reruns; caching a row here would outlive the write that invalidated it. Reads
  go through :mod:`app.frontend.data_access`, which owns its own invalidation.

Everything degrades to a no-op outside a Streamlit script run (a plain ``python -c``
import, a test), so importing this module never requires a running server.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import streamlit as st

# ---- key names. One constant per key; nothing else may write these.
K_PROJECT_ID = "ia_project_id"
K_PROJECT = "ia_project"
K_ASSESSMENT_ID = "ia_assessment_id"
K_CONTROL_REF = "ia_control_ref"
K_EVIDENCE_ID = "ia_evidence_id"
K_REPORT_ID = "ia_report_id"
K_RUN_ID = "ia_evaluation_run_id"
K_AUDITOR = "ia_auditor_name"
K_FILTERS = "ia_filters"
K_REVIEW_TIMERS = "ia_review_timers"
K_FLASHES = "ia_flashes"
K_BOOTSTRAPPED = "ia_bootstrapped"

#: Filters are namespaced by page so that "status" on Findings and "status" on
#: Assessments do not collide, which is the failure this module exists to prevent.
_FILTER_SEPARATOR = "::"


def available() -> bool:
    """True when there is a Streamlit script run to hold state.

    Every accessor below checks this, so the same page code can be imported and
    unit-tested outside a server without guarding each call.
    """
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        return get_script_run_ctx() is not None
    except Exception:  # noqa: BLE001 - private API; absence must not be fatal
        return False


def _get(key: str, default: Any = None) -> Any:
    if not available():
        return default
    return st.session_state.get(key, default)


def _set(key: str, value: Any) -> Any:
    if available():
        st.session_state[key] = value
    return value


def init_state(auditor_name: str = "") -> None:
    """Create every key with a safe default. Idempotent; call once per script run."""
    if not available():
        return
    defaults: Dict[str, Any] = {
        K_PROJECT_ID: None,
        K_PROJECT: None,
        K_ASSESSMENT_ID: None,
        K_CONTROL_REF: "",
        K_EVIDENCE_ID: None,
        K_REPORT_ID: None,
        K_RUN_ID: None,
        K_AUDITOR: auditor_name,
        K_FILTERS: {},
        K_REVIEW_TIMERS: {},
        K_FLASHES: [],
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value
    if auditor_name and not st.session_state.get(K_AUDITOR):
        st.session_state[K_AUDITOR] = auditor_name


# ---- current project
def current_project_id() -> Optional[int]:
    value = _get(K_PROJECT_ID)
    return int(value) if value is not None else None


def set_current_project(project: Any) -> Optional[int]:
    """Select the working project.

    Accepts an id or a project dictionary; the dictionary is kept alongside so the
    sidebar can render the project's name without a query on every rerun. Selecting a
    *different* project clears the selections that only make sense inside the previous
    one - an assessment id from another project would otherwise open a detail view
    belonging to a project the user has navigated away from.
    """
    if project is None:
        project_id: Optional[int] = None
        record: Optional[Dict[str, Any]] = None
    elif isinstance(project, dict):
        raw = project.get("id")
        project_id = int(raw) if raw is not None else None
        record = dict(project)
    else:
        project_id = int(project)
        record = None

    if project_id != current_project_id():
        _set(K_ASSESSMENT_ID, None)
        _set(K_EVIDENCE_ID, None)
        _set(K_REPORT_ID, None)
        _set(K_CONTROL_REF, "")
        clear_filters()

    _set(K_PROJECT_ID, project_id)
    _set(K_PROJECT, record)
    return project_id


def current_project() -> Optional[Dict[str, Any]]:
    """The cached project record, if the selection was made with a dictionary."""
    value = _get(K_PROJECT)
    return dict(value) if isinstance(value, dict) else None


def current_project_name(default: str = "No project selected") -> str:
    record = current_project()
    if record and record.get("name"):
        return str(record["name"])
    project_id = current_project_id()
    return "Project {0}".format(project_id) if project_id is not None else default


def has_project() -> bool:
    return current_project_id() is not None


# ---- current selections within a project
def current_assessment_id() -> Optional[int]:
    value = _get(K_ASSESSMENT_ID)
    return int(value) if value is not None else None


def set_current_assessment(assessment_id: Optional[int]) -> Optional[int]:
    return _set(K_ASSESSMENT_ID, int(assessment_id) if assessment_id is not None else None)


def current_control_ref() -> str:
    return str(_get(K_CONTROL_REF, "") or "")


def set_current_control_ref(control_ref: str) -> str:
    return _set(K_CONTROL_REF, str(control_ref or ""))


def current_evidence_id() -> Optional[int]:
    value = _get(K_EVIDENCE_ID)
    return int(value) if value is not None else None


def set_current_evidence(evidence_file_id: Optional[int]) -> Optional[int]:
    return _set(K_EVIDENCE_ID, int(evidence_file_id) if evidence_file_id is not None else None)


def current_report_id() -> Optional[int]:
    value = _get(K_REPORT_ID)
    return int(value) if value is not None else None


def set_current_report(report_id: Optional[int]) -> Optional[int]:
    return _set(K_REPORT_ID, int(report_id) if report_id is not None else None)


def current_run_id() -> Optional[int]:
    value = _get(K_RUN_ID)
    return int(value) if value is not None else None


def set_current_run(run_id: Optional[int]) -> Optional[int]:
    return _set(K_RUN_ID, int(run_id) if run_id is not None else None)


# ---- the acting auditor
def auditor_name(default: str = "") -> str:
    """Who the review rows will be attributed to.

    A free-text name rather than an authenticated identity: this is a research
    prototype with no login, and pretending otherwise in the data model would overstate
    what the audit trail can establish.
    """
    return str(_get(K_AUDITOR, default) or default)


def set_auditor_name(name: str) -> str:
    return _set(K_AUDITOR, str(name or "").strip())


# ---- filters
def get_filter(page: str, name: str, default: Any = None) -> Any:
    filters = _get(K_FILTERS, {}) or {}
    return filters.get("{0}{1}{2}".format(page, _FILTER_SEPARATOR, name), default)


def set_filter(page: str, name: str, value: Any) -> Any:
    filters = dict(_get(K_FILTERS, {}) or {})
    filters["{0}{1}{2}".format(page, _FILTER_SEPARATOR, name)] = value
    _set(K_FILTERS, filters)
    return value


def get_filters(page: str) -> Dict[str, Any]:
    """Every filter belonging to one page, with the page prefix stripped."""
    prefix = "{0}{1}".format(page, _FILTER_SEPARATOR)
    filters = _get(K_FILTERS, {}) or {}
    return {
        key[len(prefix) :]: value for key, value in filters.items() if key.startswith(prefix)
    }


def clear_filters(page: str = "") -> None:
    """Clear one page's filters, or every page's when ``page`` is empty."""
    if not page:
        _set(K_FILTERS, {})
        return
    prefix = "{0}{1}".format(page, _FILTER_SEPARATOR)
    filters = {
        key: value
        for key, value in (_get(K_FILTERS, {}) or {}).items()
        if not key.startswith(prefix)
    }
    _set(K_FILTERS, filters)


# ---- review timer
def start_review_timer(assessment_id: int, restart: bool = False) -> float:
    """Mark when the auditor opened this assessment.

    ``HumanReview.review_seconds`` is one of the study's measurements, so the clock has
    to start when the review screen is first drawn and survive the reruns caused by
    typing in the form. It is wall clock from opening the assessment to submitting the
    decision, which includes any time the auditor spent away from the screen - stated
    here because that upper-bound reading is the only honest one for a browser-side
    timer with no idle detection.
    """
    timers = dict(_get(K_REVIEW_TIMERS, {}) or {})
    key = str(int(assessment_id))
    if restart or key not in timers:
        timers[key] = time.time()
        _set(K_REVIEW_TIMERS, timers)
    return float(timers[key])


def review_elapsed_seconds(assessment_id: int) -> float:
    """Seconds since the timer started, or 0.0 when it never did."""
    timers = _get(K_REVIEW_TIMERS, {}) or {}
    started = timers.get(str(int(assessment_id)))
    if not started:
        return 0.0
    return max(0.0, round(time.time() - float(started), 1))


def clear_review_timer(assessment_id: int) -> None:
    timers = dict(_get(K_REVIEW_TIMERS, {}) or {})
    timers.pop(str(int(assessment_id)), None)
    _set(K_REVIEW_TIMERS, timers)


# ---- one-shot messages that must survive a rerun
def flash(message: str, kind: str = "success") -> None:
    """Queue a message to be shown after the rerun that follows a write.

    ``st.success`` called immediately before ``st.rerun`` is never painted, so a
    confirmation has to outlive one script run to be seen at all.
    """
    if not message:
        return
    queue = list(_get(K_FLASHES, []) or [])
    queue.append({"message": str(message), "kind": str(kind or "info")})
    _set(K_FLASHES, queue)


def pop_flashes() -> List[Dict[str, str]]:
    """Return and clear the queue. Call once per script run, near the top."""
    queue = list(_get(K_FLASHES, []) or [])
    _set(K_FLASHES, [])
    return queue


def render_flashes() -> None:
    """Draw and clear any queued messages using the matching Streamlit callout."""
    for item in pop_flashes():
        kind = item.get("kind", "info")
        message = item.get("message", "")
        if kind == "success":
            st.success(message)
        elif kind == "warning":
            st.warning(message)
        elif kind == "error":
            st.error(message)
        else:
            st.info(message)


__all__ = [
    "K_ASSESSMENT_ID",
    "K_AUDITOR",
    "K_FILTERS",
    "K_PROJECT_ID",
    "auditor_name",
    "available",
    "clear_filters",
    "clear_review_timer",
    "current_assessment_id",
    "current_control_ref",
    "current_evidence_id",
    "current_project",
    "current_project_id",
    "current_project_name",
    "current_report_id",
    "current_run_id",
    "flash",
    "get_filter",
    "get_filters",
    "has_project",
    "init_state",
    "pop_flashes",
    "render_flashes",
    "review_elapsed_seconds",
    "set_auditor_name",
    "set_current_assessment",
    "set_current_control_ref",
    "set_current_evidence",
    "set_current_project",
    "set_current_report",
    "set_current_run",
    "set_filter",
    "start_review_timer",
]
