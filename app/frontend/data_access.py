"""The one door between the Streamlit pages and everything behind them.

Ten page modules need projects, controls, evidence, assessments, reviews, reports and
evaluation runs. If each of them opened its own session and wrote its own query, the UI
would slowly become a second, divergent implementation of :mod:`app.audit.service`. So
every page calls this module and nothing else: **no page imports SQLAlchemy, a model, or
``app.audit.service``.**

Two backends, one function set
------------------------------
By default this module calls :mod:`app.audit.service`, :mod:`app.audit.engine`,
:mod:`app.audit.report` and the evaluation harness *in this process*, opening its own
short-lived session per call. ``streamlit run app/frontend/streamlit_app.py`` is then a
complete working application with no backend to start. When the ``USE_API`` environment
variable is true the same functions route through :mod:`app.frontend.api_client` to the
FastAPI service instead. The signatures and the return shapes are identical in both
modes, which is the only reason a page can be written once.

Why everything comes back as plain dictionaries
-----------------------------------------------
Returning an ORM object from a function whose session has closed is the classic
Streamlit + SQLAlchemy failure: the object survives, the row does not, and the next
attribute access raises ``DetachedInstanceError`` several reruns later, far from the
cause. Every value that leaves this module is therefore serialised **inside** the
session scope that produced it, using the ``*_to_dict`` helpers the service layer
already provides. Nothing bound to a session ever crosses this boundary.

Caching
-------
Streamlit re-runs the whole script on every keystroke, so an uncached dashboard would
re-query on each one. Reads are memoised per browser session under a *version* counter;
every mutating function in this module bumps that counter, so the first read after a
write cannot be served from the cache. A short TTL bounds staleness from writes made
elsewhere - by the FastAPI process, or by a second browser tab - which the counter
cannot see. Outside a Streamlit script run (a script, a test) the memo is skipped
entirely and every call goes to the database.
"""

from __future__ import annotations

import copy
import json
import os
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import streamlit as st

from app.config import Settings, get_settings, reload_settings
from app.schemas.enums import (
    AssessmentStatus,
    ConfidenceLevel,
    EvidenceSufficiency,
    EvidenceType,
    ExperimentMode,
    HumanDecision,
    ProjectStatus,
    RiskLevel,
)

#: Read fresh at most this often per browser session, even with no write in between.
CACHE_TTL_SECONDS = 20.0

#: Columns that make an assessment row large. Kept out of list responses and returned
#: only by :func:`get_assessment`, so a 200-row table does not carry 200 raw prompts.
_HEAVY_FIELDS = ("raw_response", "prompt_snapshot")

_CACHE_KEY = "_ia_da_cache"
_VERSION_KEY = "_ia_da_version"


# ---- errors
class DataAccessError(RuntimeError):
    """Anything a page should show the user rather than crash on."""


class NotFoundError(DataAccessError):
    """The requested project, control, assessment, file or report does not exist."""


class InvalidInputError(DataAccessError):
    """The caller supplied a value the service layer will not accept."""


class BackendUnavailableError(DataAccessError):
    """API mode is on and the backend could not be reached."""


class FeatureUnavailableError(DataAccessError):
    """The requested capability is not present in this build (e.g. the eval runner)."""


# ---- mode
def use_api() -> bool:
    """True when the UI should talk to the FastAPI backend rather than the service layer.

    Read from the environment on every call rather than cached, so the Settings page can
    flip it without a restart. ``USE_API`` is not a :class:`~app.config.Settings` field:
    it selects a *transport*, and putting it there would imply the service layer cares.
    """
    return str(os.environ.get("USE_API", "")).strip().lower() in {"1", "true", "yes", "on"}


def backend_mode() -> str:
    return "api" if use_api() else "in-process"


def backend_label() -> str:
    if use_api():
        return "FastAPI backend at {0}".format(get_settings().api_base_url)
    return "In-process service layer (no backend required)"


# ---- caching
def cache_version() -> int:
    if not _session_available():
        return 0
    return int(st.session_state.get(_VERSION_KEY, 0))


def invalidate_cache() -> int:
    """Discard every memoised read. Called by every write in this module."""
    if not _session_available():
        return 0
    st.session_state[_VERSION_KEY] = cache_version() + 1
    st.session_state[_CACHE_KEY] = {}
    return int(st.session_state[_VERSION_KEY])


def _session_available() -> bool:
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        return get_script_run_ctx() is not None
    except Exception:  # noqa: BLE001 - private API; absence must not be fatal
        return False


def _memo(key: Tuple[Any, ...], producer: Callable[[], Any]) -> Any:
    """Memoise one read for this browser session.

    A deep copy is handed out so that a page mutating the list it was given - sorting
    it, popping a row - cannot corrupt what the next page reads.
    """
    if not _session_available():
        return producer()
    cache = st.session_state.setdefault(_CACHE_KEY, {})
    version = cache_version()
    cache_key = repr(key)
    hit = cache.get(cache_key)
    if hit is not None:
        stored_version, stored_at, value = hit
        if stored_version == version and (time.time() - stored_at) < CACHE_TTL_SECONDS:
            return copy.deepcopy(value)
    value = producer()
    cache[cache_key] = (version, time.time(), copy.deepcopy(value))
    return value


# ---- in-process plumbing
def _session_scope():
    from app.database.base import session_scope

    return session_scope()


def _translate(exc: Exception) -> DataAccessError:
    """Map a service-layer or transport failure onto this module's vocabulary."""
    from app.audit import service as audit_service

    if isinstance(exc, audit_service.NotFoundError):
        return NotFoundError(str(exc))
    if isinstance(exc, audit_service.InvalidInputError):
        return InvalidInputError(str(exc))
    from app.frontend.api_client import ApiError

    if isinstance(exc, ApiError):
        if exc.status_code == 404:
            return NotFoundError(str(exc))
        if exc.status_code in (400, 409, 422):
            return InvalidInputError(str(exc))
        return BackendUnavailableError(str(exc))
    return DataAccessError(str(exc))


def _run(fn: Callable[..., Any]) -> Any:
    """Execute a ``session -> value`` callable in a scope, translating failures."""
    try:
        with _session_scope() as session:
            return fn(session)
    except DataAccessError:
        raise
    except Exception as exc:  # noqa: BLE001 - the UI shows the message, never a traceback
        raise _translate(exc) from exc


def _api() -> Any:
    from app.frontend.api_client import get_client

    return get_client()


def _api_call(fn: Callable[[Any], Any]) -> Any:
    try:
        return fn(_api())
    except DataAccessError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


# ---- bootstrap and demo data
def bootstrap_database(overwrite_controls: bool = False) -> Dict[str, Any]:
    """Create the schema and upsert the synthetic control library.

    Idempotent by construction (see :mod:`app.database.seed`), so the entry point can
    call it on first run without tracking whether it has run before. In API mode the
    backend does this at startup and there is nothing for the UI to do.
    """
    if use_api():
        return {"skipped": True, "reason": "The API process seeds the database at startup."}
    from app.database.base import init_db
    from app.database.seed import bootstrap

    init_db()
    try:
        with _session_scope() as session:
            result = dict(bootstrap(session, overwrite=overwrite_controls))
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
    invalidate_cache()
    return result


#: Datasets ingested by :func:`load_demo_project`. DATASET-005 and DATASET-006 are
#: excluded on purpose: both describe the *same* control as DATASET-001 under a
#: different condition (005 omits the MFA attribute entirely, 006 shows full
#: compliance), so putting their files in one project would leave three contradictory
#: accounts of CONTROL-001 in the same evidence set. They remain available to the
#: evaluation harness, which gives each dataset an isolated project of its own.
DEMO_DATASET_IDS: List[str] = ["DATASET-001", "DATASET-002", "DATASET-003", "DATASET-004"]


def load_demo_project(dataset_ids: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Seed the demo project and ingest the generated synthetic evidence into it.

    This is the "there is nothing here yet" button. It generates the synthetic files on
    disk (deterministically - same bytes every time), then ingests each one with its
    declared evidence type so retrieval can tell a policy from an export. Re-running it
    adds nothing: a file already ingested into the project under the same name is
    skipped, so the button is safe to press twice.

    Everything it creates is fabricated for research use. No real system, account or
    person is represented anywhere in it.
    """
    # Seeding runs locally in both modes. The API deliberately exposes no endpoint that
    # writes demonstration data into an audit database, and adding one would mean any
    # client that can reach the port could fabricate evidence rows. The UI and the API
    # read the same DATABASE_URL in the supported deployment, so the seeded project is
    # visible over HTTP immediately afterwards.
    from app.evaluation import datasets as dataset_module
    from app.evidence.service import ingest_file
    from app.database.base import init_db
    from app.database.seed import bootstrap

    init_db()
    chosen = list(dataset_ids or DEMO_DATASET_IDS)
    settings = get_settings()

    try:
        with _session_scope() as session:
            seeded = dict(bootstrap(session, overwrite=False))
            project_id = int(seeded["demo_project_id"])

            from app.audit import service as audit_service

            existing = {
                item.filename for item in audit_service.list_evidence(session, project_id=project_id)
            }

            ingested: List[Dict[str, Any]] = []
            skipped: List[str] = []
            failures: List[Dict[str, str]] = []
            for dataset_id in chosen:
                dataset = dataset_module.get_dataset(dataset_id)
                paths = dataset_module.generate_dataset(dataset_id)
                for path in paths:
                    declared = dataset.file(path.name)
                    if path.name in existing:
                        skipped.append(path.name)
                        continue
                    try:
                        record = ingest_file(
                            session,
                            project_id,
                            path.read_bytes(),
                            path.name,
                            evidence_type=declared.evidence_type if declared else EvidenceType.OTHER,
                            description=(
                                "{0} - synthetic evidence generated for {1}.".format(
                                    declared.description if declared else "Synthetic artefact",
                                    dataset_id,
                                )
                            ),
                            uploaded_by=settings.default_auditor_name,
                            is_synthetic=True,
                        )
                        existing.add(path.name)
                        ingested.append(
                            {
                                "filename": record.filename,
                                "dataset_id": dataset_id,
                                "evidence_type": record.evidence_type,
                                "parse_status": record.parse_status,
                                "chunks": int(record.chunk_count),
                            }
                        )
                    except Exception as exc:  # noqa: BLE001 - one bad file must not stop the seed
                        failures.append({"filename": path.name, "error": str(exc)})

            summary = {
                "project_id": project_id,
                "project_name": seeded.get("demo_project_name", ""),
                "controls_total": seeded.get("controls_total", 0),
                "scoped_control_refs": seeded.get("scoped_control_refs", []),
                "datasets": chosen,
                "ingested": ingested,
                "skipped": skipped,
                "failures": failures,
                "note": (
                    "All evidence in this project is synthetic and was generated by "
                    "app.evaluation.generator. It represents no real organisation."
                ),
            }
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc

    invalidate_cache()
    return summary


def database_is_empty() -> bool:
    """True when no audit project exists at all.

    Narrower than it sounds, and deliberately not the test the shell uses for its call to
    action - see :func:`first_run_state`.
    """
    try:
        return not list_projects()
    except DataAccessError:
        return False


def first_run_state() -> Dict[str, Any]:
    """Whether this database yet holds anything an auditor could look at.

    "No projects" is the wrong test for a first-run prompt here, because seeding the
    control library also creates the demonstration *project* (see
    :func:`app.database.seed.bootstrap`). A freshly created database therefore always has
    a project, and it is still completely empty in every sense that matters: no evidence
    has been ingested and no control has been assessed. ``needs_data`` is that condition,
    and it is what the "load the demo" prompt actually addresses.

    A read failure reports ``needs_data`` False rather than True: prompting someone to
    seed a database that could not be read would be the wrong instruction, and the error
    is returned so the caller can show it instead.
    """
    try:
        projects = list_projects()
        stats = dashboard_stats()
    except DataAccessError as exc:
        return {
            "projects": 0,
            "evidence_files": 0,
            "controls_assessed": 0,
            "needs_data": False,
            "error": str(exc),
        }
    evidence_files = int(stats.get("evidence_files", 0) or 0)
    controls_assessed = int(stats.get("controls_assessed", 0) or 0)
    return {
        "projects": len(projects),
        "evidence_files": evidence_files,
        "controls_assessed": controls_assessed,
        "needs_data": (not projects) or (evidence_files == 0 and controls_assessed == 0),
        "error": "",
    }


# ---- configuration and provider status
#: Stated in-process too, because the risk it describes is a property of the prototype
#: rather than of the HTTP surface: there is no login, no authorisation and no audit of
#: who is sitting at the browser.
_LOCAL_SECURITY_NOTE = (
    "This prototype has no authentication and no authorisation. The reviewing auditor's "
    "name is a declared label, not a verified identity. Run it locally with synthetic "
    "data only."
)


def settings_summary() -> Dict[str, Any]:
    """Redacted configuration for the Settings page - the same shape from either transport.

    The API returns a deliberately curated view (see ``app/api/routers/settings.py``):
    no filesystem paths, no connection string, the API key reduced to a masked
    fingerprint. The in-process branch rebuilds that same structure rather than handing
    back ``Settings.redacted_dict()``, because a Settings page whose fields moved when
    ``USE_API`` was flipped would in effect be two pages.

    ``redacted_settings`` carries the full field-by-field dump when the UI is running
    in-process - same machine, same filesystem, so the paths are the operator's own - and
    is empty over HTTP, where the API declines to disclose them. The key is always
    present so a page can say "not available from the API" rather than fail on a missing
    field. Secrets are masked by :meth:`app.config.Settings.redacted_dict` in both cases
    and are never echoed here.
    """
    if use_api():
        payload = dict(_api_call(lambda client: client.settings_summary()))
        payload.setdefault("redacted_settings", {})
        return payload

    from app.config import SUPPORTED_UPLOAD_EXTENSIONS

    settings = get_settings()
    return {
        "app_name": settings.app_name,
        "app_short_name": settings.app_short_name,
        "app_version": settings.app_version,
        "environment": settings.environment,
        "debug": settings.debug,
        "provider": dict(settings.provider_summary()),
        "limits": {
            "max_upload_mb": settings.max_upload_mb,
            "max_upload_bytes": settings.max_upload_bytes,
            "supported_upload_extensions": list(SUPPORTED_UPLOAD_EXTENSIONS),
            "max_evidence_chars": settings.max_evidence_chars,
            "api_request_timeout_seconds": settings.api_request_timeout,
        },
        "force_human_review": settings.force_human_review,
        "citation_match_threshold": settings.citation_match_threshold,
        "default_auditor_name": settings.default_auditor_name,
        "database_backend": settings.database_url.split(":", 1)[0],
        "security_note": _LOCAL_SECURITY_NOTE,
        "redacted_settings": dict(settings.redacted_dict()),
    }


def refresh_settings() -> Dict[str, Any]:
    """Re-read the environment after the Settings page changed it."""
    reload_settings()
    invalidate_cache()
    return settings_summary()


def provider_badge() -> Dict[str, Any]:
    """What the sidebar needs to say about the model, unambiguously.

    ``is_mock`` is the field that matters. The offline provider is a deterministic
    rule-based stand-in, not a language model, and a demonstration run against it
    measures the pipeline rather than model capability. The UI must never let that be
    mistaken, including - especially - when a real provider was *selected* but is
    unconfigured and the factory quietly fell back.
    """

    def produce() -> Dict[str, Any]:
        if use_api():
            health = _api_call(lambda client: client.provider_health())
        else:
            from app.llm.factory import provider_health

            health = dict(provider_health())

        active = str(health.get("active_provider", "") or "")
        configured = str(health.get("configured_provider", "") or "")
        fell_back = bool(health.get("fell_back_to_mock"))
        is_mock = active == "mock"
        if is_mock and fell_back:
            label = "MOCK PROVIDER (fallback)"
            detail = (
                "{0} was selected but is not configured, so the offline deterministic "
                "stand-in is answering. {1}".format(configured or "A remote provider", health.get("fallback_reason", ""))
            ).strip()
        elif is_mock:
            label = "MOCK PROVIDER"
            detail = (
                "Deterministic rule-based stand-in, not a language model. Results "
                "measure the pipeline, not model quality."
            )
        else:
            label = "{0} / {1}".format(active or "provider", health.get("active_model") or "default")
            detail = "Live model calls. Outputs still require auditor review."
        return {
            "active_provider": active,
            "configured_provider": configured,
            "active_model": str(health.get("active_model", "") or ""),
            "is_mock": is_mock,
            "fell_back_to_mock": fell_back,
            "label": label,
            "detail": detail,
            "settings": dict(health.get("settings", {}) or {}),
            "available_providers": list(health.get("available_providers", []) or []),
        }

    return _memo(("provider_badge", use_api()), produce)


def health() -> Dict[str, Any]:
    """Backend reachability plus the provider summary, for the Settings page."""
    if use_api():
        try:
            payload = _api_call(lambda client: client.health())
            return {"mode": "api", "reachable": True, "detail": payload}
        except DataAccessError as exc:
            return {"mode": "api", "reachable": False, "detail": {"error": str(exc)}}
    return {"mode": "in-process", "reachable": True, "detail": {"provider": provider_badge()}}


# ---- enum vocabularies (so pages never re-declare an option list)
def assessment_statuses() -> List[str]:
    return AssessmentStatus.values()


def risk_levels() -> List[str]:
    return RiskLevel.values()


def confidence_levels() -> List[str]:
    return ConfidenceLevel.values()


def sufficiency_levels() -> List[str]:
    return EvidenceSufficiency.values()


def evidence_types() -> List[str]:
    return EvidenceType.values()


def project_statuses() -> List[str]:
    return ProjectStatus.values()


def human_decisions() -> List[str]:
    return HumanDecision.values()


def experiment_modes() -> List[Dict[str, str]]:
    """The three experimental conditions with their long labels, in order."""
    return [{"value": mode.value, "label": mode.label} for mode in ExperimentMode]


def default_mode() -> str:
    return ExperimentMode.C_RAG_WORKFLOW.value


# ---- projects
def list_projects(
    status: Any = None,
    include_demo: bool = True,
    search: str = "",
) -> List[Dict[str, Any]]:
    def produce() -> List[Dict[str, Any]]:
        if use_api():
            return _api_call(
                lambda client: client.list_projects(
                    status=status, include_demo=include_demo, search=search or None
                )
            )

        def query(session: Any) -> List[Dict[str, Any]]:
            from app.audit import service as audit_service

            rows = audit_service.list_projects(
                session, status=status, include_demo=include_demo, search=search
            )
            return [audit_service.project_to_dict(session, row) for row in rows]

        return _run(query)

    return _memo(("projects", status, include_demo, search, use_api()), produce)


def get_project(project_id: int) -> Optional[Dict[str, Any]]:
    def produce() -> Optional[Dict[str, Any]]:
        if use_api():
            return _api_call(lambda client: client.get_project(project_id))

        def query(session: Any) -> Optional[Dict[str, Any]]:
            from app.audit import service as audit_service

            project = audit_service.get_project(session, project_id)
            return audit_service.project_to_dict(session, project) if project is not None else None

        return _run(query)

    return _memo(("project", int(project_id), use_api()), produce)


def create_project(
    name: str,
    audit_area: str,
    description: str = "",
    period_start: Any = None,
    period_end: Any = None,
    auditor_name: str = "",
    status: Optional[str] = None,
    scope_note: str = "",
    control_refs: Optional[Sequence[Any]] = None,
    actor: str = "",
) -> Dict[str, Any]:
    payload = {
        "name": name,
        "audit_area": audit_area,
        "description": description,
        "period_start": period_start,
        "period_end": period_end,
        "auditor_name": auditor_name,
        "status": status,
        "scope_note": scope_note,
        "control_refs": list(control_refs or []),
    }
    if use_api():
        result = _api_call(lambda client: client.create_project(payload))
    else:

        def write(session: Any) -> Dict[str, Any]:
            from app.audit import service as audit_service

            project = audit_service.create_project(
                session,
                name=name,
                audit_area=audit_area,
                description=description,
                period_start=period_start,
                period_end=period_end,
                auditor_name=auditor_name,
                status=status,
                scope_note=scope_note,
                control_refs=list(control_refs or []) or None,
                actor=actor,
            )
            return audit_service.project_to_dict(session, project)

        result = _run(write)
    invalidate_cache()
    return result


def update_project(project_id: int, actor: str = "", **fields: Any) -> Dict[str, Any]:
    if use_api():
        result = _api_call(lambda client: client.update_project(project_id, fields))
    else:

        def write(session: Any) -> Dict[str, Any]:
            from app.audit import service as audit_service

            project = audit_service.update_project(session, project_id, actor=actor, **fields)
            return audit_service.project_to_dict(session, project)

        result = _run(write)
    invalidate_cache()
    return result


def delete_project(project_id: int) -> bool:
    if use_api():
        result = _api_call(lambda client: client.delete_project(project_id))
    else:

        def write(session: Any) -> bool:
            from app.audit import service as audit_service

            return bool(audit_service.delete_project(session, project_id))

        result = _run(write)
    invalidate_cache()
    return bool(result)


# ---- control library
def list_controls(
    category: Any = None,
    active_only: bool = True,
    search: str = "",
    project_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    def produce() -> List[Dict[str, Any]]:
        if use_api():
            return _api_call(
                lambda client: client.list_controls(
                    category=category,
                    active_only=active_only,
                    search=search or None,
                    project_id=project_id,
                )
            )

        def query(session: Any) -> List[Dict[str, Any]]:
            from app.audit import service as audit_service

            rows = audit_service.list_controls(
                session,
                category=category,
                active_only=active_only,
                search=search,
                project_id=project_id,
            )
            return [audit_service.control_to_dict(row) for row in rows]

        return _run(query)

    return _memo(("controls", category, active_only, search, project_id, use_api()), produce)


def get_control(control_id_or_ref: Any) -> Optional[Dict[str, Any]]:
    def produce() -> Optional[Dict[str, Any]]:
        if use_api():
            return _api_call(lambda client: client.get_control(control_id_or_ref))

        def query(session: Any) -> Optional[Dict[str, Any]]:
            from app.audit import service as audit_service

            control = audit_service.get_control(session, control_id_or_ref)
            return audit_service.control_to_dict(control) if control is not None else None

        return _run(query)

    return _memo(("control", str(control_id_or_ref), use_api()), produce)


def create_control(data: Mapping[str, Any], actor: str = "") -> Dict[str, Any]:
    if use_api():
        result = _api_call(lambda client: client.create_control(dict(data)))
    else:

        def write(session: Any) -> Dict[str, Any]:
            from app.audit import service as audit_service

            control = audit_service.create_control(session, dict(data), actor=actor)
            return audit_service.control_to_dict(control)

        result = _run(write)
    invalidate_cache()
    return result


def update_control(control_id_or_ref: Any, actor: str = "", **fields: Any) -> Dict[str, Any]:
    if use_api():
        result = _api_call(lambda client: client.update_control(control_id_or_ref, fields))
    else:

        def write(session: Any) -> Dict[str, Any]:
            from app.audit import service as audit_service

            control = audit_service.update_control(session, control_id_or_ref, actor=actor, **fields)
            return audit_service.control_to_dict(control)

        result = _run(write)
    invalidate_cache()
    return result


def set_control_active(control_id_or_ref: Any, active: bool, actor: str = "") -> Dict[str, Any]:
    """Retire or restore a library control. Retiring is a soft delete: past findings
    still resolve to the control they were written against."""
    if use_api():
        result = _api_call(
            lambda client: client.set_control_active(control_id_or_ref, bool(active), actor=actor)
        )
    else:

        def write(session: Any) -> Dict[str, Any]:
            from app.audit import service as audit_service

            control = audit_service.set_control_active(
                session, control_id_or_ref, bool(active), actor=actor
            )
            return audit_service.control_to_dict(control)

        result = _run(write)
    invalidate_cache()
    return result


def control_categories(active_only: bool = True) -> List[str]:
    def produce() -> List[str]:
        if use_api():
            return _api_call(lambda client: client.control_categories(active_only=active_only))

        def query(session: Any) -> List[str]:
            from app.audit import service as audit_service

            return list(audit_service.control_categories(session, active_only=active_only))

        return _run(query)

    return _memo(("control_categories", active_only, use_api()), produce)


def list_scoped_controls(project_id: int) -> List[Dict[str, Any]]:
    def produce() -> List[Dict[str, Any]]:
        if use_api():
            # The scope endpoint returns references only; the control list endpoint
            # narrowed by project returns the full rows the pages need.
            return _api_call(lambda client: client.list_controls(project_id=int(project_id)))

        def query(session: Any) -> List[Dict[str, Any]]:
            from app.audit import service as audit_service

            rows = audit_service.list_scoped_controls(session, project_id)
            return [audit_service.control_to_dict(row) for row in rows]

        return _run(query)

    return _memo(("scoped_controls", int(project_id), use_api()), produce)


def list_unscoped_controls(project_id: int, active_only: bool = True) -> List[Dict[str, Any]]:
    """Library controls not yet in this project's scope - the "add control" picker."""

    def produce() -> List[Dict[str, Any]]:
        if use_api():
            scoped = {str(row.get("control_id")) for row in list_scoped_controls(project_id)}
            return [
                row
                for row in list_controls(active_only=active_only)
                if str(row.get("control_id")) not in scoped
            ]

        def query(session: Any) -> List[Dict[str, Any]]:
            from app.audit import service as audit_service

            rows = audit_service.list_unscoped_controls(session, project_id, active_only=active_only)
            return [audit_service.control_to_dict(row) for row in rows]

        return _run(query)

    return _memo(("unscoped_controls", int(project_id), active_only, use_api()), produce)


def scope_controls(
    project_id: int, control_refs: Sequence[Any], scope_note: str = "", actor: str = ""
) -> int:
    """Put library controls in scope for a project. Returns how many links now exist."""
    if use_api():
        payload = _api_call(
            lambda client: client.scope_controls(
                project_id, control_refs, scope_note=scope_note, actor=actor
            )
        )
        count = int(payload.get("controls_in_scope", len(list(control_refs))))
    else:

        def write(session: Any) -> int:
            from app.audit import service as audit_service

            links = audit_service.scope_controls(
                session, project_id, list(control_refs), scope_note=scope_note, actor=actor
            )
            return len(links)

        count = int(_run(write))
    invalidate_cache()
    return count


def unscope_control(project_id: int, control_id_or_ref: Any, actor: str = "") -> bool:
    if use_api():
        result = _api_call(
            lambda client: client.unscope_control(project_id, control_id_or_ref, actor=actor)
        )
    else:

        def write(session: Any) -> bool:
            from app.audit import service as audit_service

            return bool(audit_service.unscope_control(session, project_id, control_id_or_ref, actor=actor))

        result = _run(write)
    invalidate_cache()
    return bool(result)


# ---- evidence
def list_evidence(
    project_id: Optional[int] = None,
    evidence_type: Any = None,
    parse_status: Any = None,
    search: str = "",
) -> List[Dict[str, Any]]:
    def produce() -> List[Dict[str, Any]]:
        if use_api():
            return _api_call(
                lambda client: client.list_evidence(
                    project_id=project_id,
                    evidence_type=evidence_type,
                    parse_status=parse_status,
                    search=search or None,
                )
            )

        def query(session: Any) -> List[Dict[str, Any]]:
            from app.audit import service as audit_service

            rows = audit_service.list_evidence(
                session,
                project_id=project_id,
                evidence_type=evidence_type,
                parse_status=parse_status,
                search=search,
            )
            return [audit_service.evidence_to_dict(row) for row in rows]

        return _run(query)

    return _memo(("evidence", project_id, evidence_type, parse_status, search, use_api()), produce)


def get_evidence(evidence_file_id: int) -> Optional[Dict[str, Any]]:
    """File record plus the chunk-level facts that make it citable (see
    :func:`app.audit.service.evidence_metadata`)."""

    def produce() -> Optional[Dict[str, Any]]:
        if use_api():
            return _api_call(lambda client: client.get_evidence(evidence_file_id))

        def query(session: Any) -> Optional[Dict[str, Any]]:
            from app.audit import service as audit_service

            if audit_service.get_evidence_file(session, evidence_file_id) is None:
                return None
            return dict(audit_service.evidence_metadata(session, evidence_file_id))

        return _run(query)

    return _memo(("evidence_meta", int(evidence_file_id), use_api()), produce)


def upload_evidence(
    project_id: int,
    data: bytes,
    filename: str,
    evidence_type: Any = EvidenceType.OTHER,
    description: str = "",
    uploaded_by: str = "",
    is_synthetic: bool = False,
) -> Dict[str, Any]:
    """Store, parse, chunk and index one uploaded artefact.

    A file that cannot be parsed still returns a record, with ``parse_status`` FAILED or
    UNSUPPORTED and the reason attached - the UI shows that rather than pretending the
    upload vanished. Only a rejected upload (missing project, over the size limit)
    raises.
    """
    resolved_type = str(getattr(evidence_type, "value", evidence_type) or EvidenceType.OTHER.value)
    if use_api():
        result = _api_call(
            lambda client: client.upload_evidence(
                project_id,
                data,
                filename,
                evidence_type=resolved_type,
                description=description,
                uploaded_by=uploaded_by,
            )
        )
    else:

        def write(session: Any) -> Dict[str, Any]:
            from app.audit import service as audit_service
            from app.evidence.service import ingest_file

            record = ingest_file(
                session,
                project_id,
                data,
                filename,
                evidence_type=resolved_type,
                description=description,
                uploaded_by=uploaded_by,
                is_synthetic=is_synthetic,
            )
            return audit_service.evidence_to_dict(record)

        result = _run(write)
    invalidate_cache()
    return result


def delete_evidence(evidence_file_id: int) -> bool:
    """Remove an evidence file, its chunks and the stored bytes.

    Assessments that cited a deleted chunk keep their citation rows, whose ``chunk_id``
    becomes NULL - the same signal a fabricated citation produces. The Evidence page
    warns about this before deleting; it is not hidden here.
    """
    if use_api():
        result = _api_call(lambda client: client.delete_evidence(evidence_file_id))
    else:

        def write(session: Any) -> bool:
            from app.evidence.service import delete_evidence as remove

            return bool(remove(session, evidence_file_id))

        result = _run(write)
    invalidate_cache()
    return bool(result)


def list_chunks(
    evidence_file_id: int, limit: Optional[int] = None, offset: int = 0
) -> List[Dict[str, Any]]:
    def produce() -> List[Dict[str, Any]]:
        if use_api():
            rows = _api_call(
                lambda client: client.list_chunks(evidence_file_id, limit=limit, offset=offset)
            )
            return [_normalise_chunk(dict(row)) for row in rows]

        def query(session: Any) -> List[Dict[str, Any]]:
            from app.audit import service as audit_service

            rows = audit_service.list_chunks(session, evidence_file_id, limit=limit, offset=offset)
            return [_chunk_dict(row) for row in rows]

        return _run(query)

    return _memo(("chunks", int(evidence_file_id), limit, offset, use_api()), produce)


def get_chunk(chunk_id: int) -> Optional[Dict[str, Any]]:
    def produce() -> Optional[Dict[str, Any]]:
        if use_api():
            context = chunk_context(chunk_id, window=0)
            return _normalise_chunk(dict(context.get("chunk", {}))) if context.get("found") else None

        def query(session: Any) -> Optional[Dict[str, Any]]:
            from app.audit import service as audit_service

            chunk = audit_service.get_chunk(session, chunk_id)
            return _chunk_dict(chunk) if chunk is not None else None

        return _run(query)

    return _memo(("chunk", int(chunk_id), use_api()), produce)


def chunk_context(chunk_id: int, window: int = 1) -> Dict[str, Any]:
    """The cited chunk plus its neighbours, so a quote can be read in context.

    This is what the citation card's expander shows. A quoted fragment is only
    checkable if the reviewer can see what surrounded it.
    """

    def produce() -> Dict[str, Any]:
        if use_api():
            return _normalise_context(
                _api_call(lambda client: client.chunk_context(chunk_id, window=window))
            )

        def query(session: Any) -> Dict[str, Any]:
            from app.evidence.service import get_chunk_context

            return _normalise_context(dict(get_chunk_context(session, chunk_id, window=window)))

        return _run(query)

    return _memo(("chunk_context", int(chunk_id), int(window), use_api()), produce)


def project_evidence_stats(project_id: int) -> Dict[str, Any]:
    def produce() -> Dict[str, Any]:
        if use_api():
            return _api_call(lambda client: client.evidence_stats(int(project_id)))

        def query(session: Any) -> Dict[str, Any]:
            from app.audit import service as audit_service

            return dict(audit_service.project_evidence_stats(session, project_id))

        return _run(query)

    return _memo(("evidence_stats", int(project_id), use_api()), produce)


# ---- assessments
def list_assessments(
    project_id: Optional[int] = None,
    control_id: Optional[Any] = None,
    status: Any = None,
    risk_level: Any = None,
    mode: Any = None,
    reviewed: Optional[bool] = None,
    include_evaluation: bool = False,
    latest_per_control: bool = False,
    limit: Optional[int] = None,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """AI assessments, newest first, without the heavy prompt/response columns.

    ``include_evaluation`` defaults to False here although the service layer defaults it
    to True: an auditor looking at a project's assessments should not see rows the
    research harness produced against throwaway evaluation projects. The Evaluation page
    passes True explicitly.
    """

    def produce() -> List[Dict[str, Any]]:
        if use_api():
            rows = _api_call(
                lambda client: client.list_assessments(
                    project_id=project_id,
                    control_ref=control_id,
                    status=status,
                    risk_level=risk_level,
                    mode=mode,
                    reviewed=reviewed,
                    include_evaluation=include_evaluation,
                    latest_per_control=latest_per_control,
                    limit=limit,
                    offset=offset,
                )
            )
            return [_lighten(dict(row)) for row in rows]

        def query(session: Any) -> List[Dict[str, Any]]:
            from app.audit import service as audit_service

            rows = audit_service.list_assessments(
                session,
                project_id=project_id,
                control_id=control_id,
                status=status,
                risk_level=risk_level,
                mode=mode,
                reviewed=reviewed,
                include_evaluation=include_evaluation,
                latest_per_control=latest_per_control,
                limit=limit,
                offset=offset,
            )
            return [_assessment_summary(row) for row in rows]

        return _run(query)

    return _memo(
        (
            "assessments",
            project_id,
            str(control_id),
            str(status),
            str(risk_level),
            str(mode),
            reviewed,
            include_evaluation,
            latest_per_control,
            limit,
            offset,
            use_api(),
        ),
        produce,
    )


def get_assessment(assessment_id: int) -> Optional[Dict[str, Any]]:
    """One assessment in full: citations with their chunk text, the control it was
    written against, every human review, and the two derived views the pages render
    (:func:`build_four_way` and :func:`build_ai_vs_human`)."""

    def produce() -> Optional[Dict[str, Any]]:
        if use_api():
            payload = _api_call(lambda client: client.get_assessment(assessment_id))
            if payload is None:
                return None
            data = dict(payload)
        else:

            def query(session: Any) -> Optional[Dict[str, Any]]:
                from app.audit import service as audit_service

                assessment = audit_service.get_assessment(session, assessment_id)
                if assessment is None:
                    return None
                detail = audit_service.assessment_to_dict(assessment, include_citations=True)
                detail["control"] = (
                    audit_service.control_to_dict(assessment.control)
                    if assessment.control is not None
                    else {}
                )
                detail["reviews"] = [
                    audit_service.review_to_dict(review) for review in assessment.reviews
                ]
                project = audit_service.get_project(session, assessment.project_id)
                detail["project_name"] = project.name if project is not None else ""
                return detail

            data = _run(query)
            if data is None:
                return None

        _decorate_assessment(data)
        data["four_way"] = build_four_way(data)
        data["ai_vs_human"] = build_ai_vs_human(data)
        return data

    return _memo(("assessment", int(assessment_id), use_api()), produce)


def latest_assessment_for_control(
    project_id: int, control_id_or_ref: Any, mode: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    rows = list_assessments(
        project_id=project_id, control_id=control_id_or_ref, mode=mode, limit=1
    )
    return rows[0] if rows else None


def list_findings(
    project_id: Optional[int] = None,
    high_risk_only: bool = False,
    include_evaluation: bool = False,
) -> List[Dict[str, Any]]:
    """Latest assessments that concluded on a deficiency - the Findings page population."""

    def produce() -> List[Dict[str, Any]]:
        if use_api():
            rows = _api_call(
                lambda client: client.list_findings(
                    project_id=project_id,
                    high_risk_only=high_risk_only,
                    include_evaluation=include_evaluation,
                )
            )
            return [_lighten(dict(row)) for row in rows]

        def query(session: Any) -> List[Dict[str, Any]]:
            from app.audit import service as audit_service

            rows = audit_service.list_findings(
                session,
                project_id=project_id,
                high_risk_only=high_risk_only,
                include_evaluation=include_evaluation,
            )
            return [_assessment_summary(row) for row in rows]

        return _run(query)

    return _memo(("findings", project_id, high_risk_only, include_evaluation, use_api()), produce)


def run_assessment(
    project_id: int,
    control_id_or_ref: Any,
    mode: Any = ExperimentMode.C_RAG_WORKFLOW,
    persist: bool = True,
) -> Dict[str, Any]:
    """Assess one control end to end and return the run result as a dictionary.

    Never raises for a model or parsing failure: the engine records those on the
    persisted row and the returned dictionary carries ``error``, because an audit run
    that silently loses a control is worse than one that says which control did not
    complete. Only an unknown project or control raises.
    """
    resolved_mode = str(getattr(mode, "value", mode) or ExperimentMode.C_RAG_WORKFLOW.value)
    if use_api():
        result = _api_call(
            lambda client: client.run_assessment(
                {
                    "project_id": int(project_id),
                    "control_ref": str(control_id_or_ref),
                    "mode": resolved_mode,
                    "persist": bool(persist),
                }
            )
        )
    else:

        def write(session: Any) -> Dict[str, Any]:
            from app.audit.engine import AssessmentEngine

            engine = AssessmentEngine(session)
            outcome = engine.assess_control(
                project_id, control_id_or_ref, mode=resolved_mode, persist=persist
            )
            return dict(outcome.to_dict())

        result = _run(write)
    invalidate_cache()
    return result


def run_project_assessment(
    project_id: int,
    mode: Any = ExperimentMode.C_RAG_WORKFLOW,
    control_refs: Optional[Sequence[Any]] = None,
    persist: bool = True,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> List[Dict[str, Any]]:
    """Assess every scoped control (or a named subset), one at a time.

    ``progress(done, total, control_ref)`` is called after each control so the page can
    drive a progress bar. Controls are assessed in separate engine calls rather than via
    ``assess_project`` for exactly that reason - a run over twelve controls with no
    feedback looks like a hung application.
    """
    resolved_mode = str(getattr(mode, "value", mode) or ExperimentMode.C_RAG_WORKFLOW.value)
    refs = [str(ref) for ref in control_refs] if control_refs else [
        str(row.get("control_id")) for row in list_scoped_controls(project_id)
    ]
    total = len(refs)

    if use_api():
        # The API exposes one control per request. Looping here rather than adding a
        # batch endpoint keeps the two transports on the same contract, and it is what
        # lets the progress callback report after each control instead of after all of
        # them.
        out: List[Dict[str, Any]] = []
        for index, ref in enumerate(refs, start=1):
            try:
                out.append(
                    _api_call(
                        lambda client, ref=ref: client.run_assessment(
                            {
                                "project_id": int(project_id),
                                "control_ref": ref,
                                "mode": resolved_mode,
                                "persist": bool(persist),
                            }
                        )
                    )
                )
            except DataAccessError as exc:
                out.append({"control_ref": ref, "error": str(exc), "status": "", "assessment_id": None})
            if progress is not None:
                progress(index, total, ref)
        invalidate_cache()
        return out

    def write(session: Any) -> List[Dict[str, Any]]:
        from app.audit.engine import AssessmentEngine

        engine = AssessmentEngine(session)
        out: List[Dict[str, Any]] = []
        for index, ref in enumerate(refs, start=1):
            try:
                outcome = engine.assess_control(project_id, ref, mode=resolved_mode, persist=persist)
                out.append(dict(outcome.to_dict()))
            except Exception as exc:  # noqa: BLE001 - one bad control must not end the run
                out.append({"control_ref": ref, "error": str(exc), "status": "", "assessment_id": None})
            if progress is not None:
                progress(index, total, ref)
        return out

    results = _run(write)
    invalidate_cache()
    return results


# ---- human review
def record_review(
    assessment_id: int,
    reviewer_name: str,
    decision: Any,
    final_status: Optional[str] = None,
    final_risk_level: Optional[str] = None,
    final_finding: str = "",
    final_recommendation: str = "",
    comments: str = "",
    requested_evidence: Optional[Sequence[str]] = None,
    review_seconds: float = 0.0,
    usefulness_rating: Optional[int] = None,
    flagged_hallucination: bool = False,
    hallucination_note: str = "",
) -> Dict[str, Any]:
    """Record the auditor's decision. The AI assessment row is never edited.

    Agreement flags (``agreed_with_ai_status`` / ``agreed_with_ai_risk``) are derived by
    the service layer on write, not supplied here, so the concordance metric cannot be
    set from the UI.
    """
    payload: Dict[str, Any] = {
        "assessment_id": int(assessment_id),
        "reviewer_name": reviewer_name,
        "decision": str(getattr(decision, "value", decision) or ""),
        "final_status": final_status,
        "final_risk_level": final_risk_level,
        "final_finding": final_finding,
        "final_recommendation": final_recommendation,
        "comments": comments,
        "requested_evidence": list(requested_evidence or []),
        "review_seconds": float(review_seconds or 0.0),
        "usefulness_rating": usefulness_rating,
        "flagged_hallucination": bool(flagged_hallucination),
        "hallucination_note": hallucination_note,
    }
    if use_api():
        result = _api_call(lambda client: client.record_review(payload))
    else:

        def write(session: Any) -> Dict[str, Any]:
            from app.audit import service as audit_service

            review = audit_service.record_human_review(
                session,
                assessment_id=int(assessment_id),
                reviewer_name=reviewer_name,
                decision=payload["decision"],
                final_status=final_status,
                final_risk_level=final_risk_level,
                final_finding=final_finding,
                final_recommendation=final_recommendation,
                comments=comments,
                requested_evidence=list(requested_evidence or []),
                review_seconds=float(review_seconds or 0.0),
                usefulness_rating=usefulness_rating,
                flagged_hallucination=bool(flagged_hallucination),
                hallucination_note=hallucination_note,
            )
            return audit_service.review_to_dict(review)

        result = _run(write)
    invalidate_cache()
    return result


def list_reviews(
    project_id: Optional[int] = None,
    assessment_id: Optional[int] = None,
    reviewer_name: str = "",
    include_pending: bool = True,
) -> List[Dict[str, Any]]:
    def produce() -> List[Dict[str, Any]]:
        if use_api():
            return _api_call(
                lambda client: client.list_reviews(
                    project_id=project_id,
                    assessment_id=assessment_id,
                    reviewer_name=reviewer_name or None,
                    include_pending=include_pending,
                )
            )

        def query(session: Any) -> List[Dict[str, Any]]:
            from app.audit import service as audit_service

            rows = audit_service.list_reviews(
                session,
                project_id=project_id,
                assessment_id=assessment_id,
                reviewer_name=reviewer_name,
                include_pending=include_pending,
            )
            return [audit_service.review_to_dict(row) for row in rows]

        return _run(query)

    return _memo(
        ("reviews", project_id, assessment_id, reviewer_name, include_pending, use_api()), produce
    )


def pending_reviews(project_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """The review queue: the latest assessment per control with no completed decision."""

    def produce() -> List[Dict[str, Any]]:
        if use_api():
            rows = _api_call(lambda client: client.pending_reviews(project_id=project_id))
            return [_lighten(dict(row)) for row in rows]

        def query(session: Any) -> List[Dict[str, Any]]:
            from app.audit import service as audit_service

            rows = audit_service.pending_reviews(session, project_id=project_id)
            return [_assessment_summary(row) for row in rows]

        return _run(query)

    return _memo(("pending_reviews", project_id, use_api()), produce)


# ---- dashboard
def dashboard_stats(project_id: Optional[int] = None) -> Dict[str, Any]:
    """Headline figures, with the ``definitions`` block that says what each one counts."""

    def produce() -> Dict[str, Any]:
        if use_api():
            return _api_call(lambda client: client.dashboard_stats(project_id=project_id))

        def query(session: Any) -> Dict[str, Any]:
            from app.audit import service as audit_service

            return dict(audit_service.dashboard_stats(session, project_id=project_id))

        return _run(query)

    return _memo(("dashboard", project_id, use_api()), produce)


def status_breakdown(project_id: Optional[int] = None) -> Dict[str, int]:
    def produce() -> Dict[str, int]:
        if use_api():
            return _api_call(lambda client: client.status_breakdown(project_id=project_id))

        def query(session: Any) -> Dict[str, int]:
            from app.audit import service as audit_service

            return dict(audit_service.status_breakdown(session, project_id=project_id))

        return _run(query)

    return _memo(("status_breakdown", project_id, use_api()), produce)


def risk_breakdown(project_id: Optional[int] = None) -> Dict[str, int]:
    def produce() -> Dict[str, int]:
        if use_api():
            return _api_call(lambda client: client.risk_breakdown(project_id=project_id))

        def query(session: Any) -> Dict[str, int]:
            from app.audit import service as audit_service

            return dict(audit_service.risk_breakdown(session, project_id=project_id))

        return _run(query)

    return _memo(("risk_breakdown", project_id, use_api()), produce)


def list_activity(
    project_id: Optional[int] = None,
    entity_type: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """The append-only trail of who did what, newest first. ``actor_type`` is AI or HUMAN."""

    def produce() -> List[Dict[str, Any]]:
        if use_api():
            return _api_call(
                lambda client: client.activity(
                    project_id=project_id, entity_type=entity_type, limit=limit
                )
            )

        def query(session: Any) -> List[Dict[str, Any]]:
            from app.audit import service as audit_service

            rows = audit_service.list_activity(
                session, project_id=project_id, entity_type=entity_type, limit=limit
            )
            return [row.to_dict() for row in rows]

        return _run(query)

    return _memo(("activity", project_id, entity_type, limit, use_api()), produce)


# ---- reports
def list_reports(project_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Report index without the rendered body, which can be hundreds of kilobytes."""

    def produce() -> List[Dict[str, Any]]:
        if use_api():
            rows = _api_call(lambda client: client.list_reports(project_id=project_id))
            return [_report_summary(dict(row)) for row in rows]

        def query(session: Any) -> List[Dict[str, Any]]:
            from app.audit import service as audit_service

            return [_report_summary(row.to_dict()) for row in audit_service.list_reports(session, project_id=project_id)]

        return _run(query)

    return _memo(("reports", project_id, use_api()), produce)


def get_report(report_id: int) -> Optional[Dict[str, Any]]:
    """One report including its rendered ``content`` - what the download button serves."""

    def produce() -> Optional[Dict[str, Any]]:
        if use_api():
            return _api_call(lambda client: client.get_report(report_id))

        def query(session: Any) -> Optional[Dict[str, Any]]:
            from app.audit import service as audit_service

            report = audit_service.get_report(session, report_id)
            return report.to_dict() if report is not None else None

        return _run(query)

    return _memo(("report", int(report_id), use_api()), produce)


def generate_report(
    project_id: int,
    generated_by: str = "",
    fmt: str = "markdown",
    title: str = "",
    include_evaluation: bool = False,
) -> Dict[str, Any]:
    """Build, render, write to disk and persist an audit report for one project."""
    if use_api():
        result = _api_call(
            lambda client: client.generate_report(
                {
                    "project_id": int(project_id),
                    "generated_by": generated_by,
                    "format": fmt,
                    "title": title,
                    "include_evaluation": bool(include_evaluation),
                }
            )
        )
    else:

        def write(session: Any) -> Dict[str, Any]:
            from app.audit.report import generate_report as build

            row = build(
                session,
                project_id,
                generated_by=generated_by,
                fmt=fmt,
                title=title,
                include_evaluation=include_evaluation,
            )
            return row.to_dict()

        result = _run(write)
    invalidate_cache()
    return result


# ---- evaluation harness
def evaluation_available() -> Dict[str, Any]:
    """Whether experiments can be launched from the UI in this build.

    ``app.evaluation.runner`` is written by a separate task. The Evaluation page must
    render its dataset catalogue and past runs either way, so absence is reported as a
    fact rather than raised as an import error.
    """
    if use_api():
        return {"available": True, "reason": "", "module": "api"}
    try:
        import app.evaluation.runner  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - absence is the answer, not a failure
        return {
            "available": False,
            "reason": "app.evaluation.runner is not importable: {0}".format(exc),
            "module": "app.evaluation.runner",
        }
    return {"available": True, "reason": "", "module": "app.evaluation.runner"}


def list_datasets() -> List[Dict[str, Any]]:
    """The synthetic evaluation suite and its ground truth. Reads metadata only - no
    file is generated and no heavy parser is imported by this call."""

    def produce() -> List[Dict[str, Any]]:
        if use_api():
            return _api_call(lambda client: client.list_datasets())
        from app.evaluation.datasets import dataset_manifest

        return list(dataset_manifest())

    return _memo(("datasets", use_api()), produce)


def list_evaluation_runs(limit: int = 50) -> List[Dict[str, Any]]:
    """Past experiment runs, newest first, with their persisted metrics."""

    def produce() -> List[Dict[str, Any]]:
        if use_api():
            return _api_call(lambda client: client.list_runs(limit=limit))

        def query(session: Any) -> List[Dict[str, Any]]:
            from sqlalchemy import select

            from app.database.models import EvaluationRun

            statement = (
                select(EvaluationRun)
                .order_by(EvaluationRun.started_at.desc(), EvaluationRun.id.desc())
                .limit(int(limit))
            )
            rows = list(session.execute(statement).scalars().all())
            return [_run_summary(row) for row in rows]

        return _run(query)

    return _memo(("eval_runs", limit, use_api()), produce)


def get_evaluation_run(run_id: int) -> Optional[Dict[str, Any]]:
    """One run with every scored result attached."""

    def produce() -> Optional[Dict[str, Any]]:
        if use_api():
            return _api_call(lambda client: client.get_run(run_id))

        def query(session: Any) -> Optional[Dict[str, Any]]:
            from app.database.models import EvaluationRun

            run = session.get(EvaluationRun, int(run_id))
            if run is None:
                return None
            data = _run_summary(run)
            data["results"] = [result.to_dict() for result in run.results]
            return data

        return _run(query)

    return _memo(("eval_run", int(run_id), use_api()), produce)


def run_evaluation(
    mode: Any = ExperimentMode.C_RAG_WORKFLOW,
    dataset_ids: Optional[Sequence[str]] = None,
    run_name: str = "",
) -> Dict[str, Any]:
    """Run one experiment over the synthetic datasets and persist the scored results."""
    resolved_mode = str(getattr(mode, "value", mode) or ExperimentMode.C_RAG_WORKFLOW.value)
    if use_api():
        result = _api_call(
            lambda client: client.run_evaluation(
                {
                    "mode": resolved_mode,
                    "dataset_ids": list(dataset_ids or []) or None,
                    "run_name": run_name,
                }
            )
        )
        invalidate_cache()
        return result

    status = evaluation_available()
    if not status["available"]:
        raise FeatureUnavailableError(
            "Experiments cannot be started from the UI in this build. {0}".format(status["reason"])
        )

    def write(session: Any) -> Dict[str, Any]:
        from app.evaluation.runner import run_experiment

        run = run_experiment(
            session,
            mode=resolved_mode,
            dataset_ids=list(dataset_ids) if dataset_ids else None,
            run_name=run_name,
        )
        return _run_summary(run)

    result = _run(write)
    invalidate_cache()
    return result


def compare_evaluation_runs(run_ids: Sequence[int]) -> List[Dict[str, Any]]:
    """One flat row per run, for an A/B/C comparison table.

    Uses ``app.evaluation.runner.compare_runs`` when it exists; otherwise falls back to
    flattening the metrics already persisted on each run, so the comparison still works
    in a build without the runner.
    """
    ids = [int(value) for value in run_ids]
    if not ids:
        return []
    if use_api():
        return _api_call(lambda client: client.compare_runs(ids))
    if evaluation_available()["available"]:
        try:

            def query(session: Any) -> List[Dict[str, Any]]:
                from app.evaluation.runner import compare_runs

                frame = compare_runs(session, ids)
                return [
                    {str(key): value for key, value in record.items()}
                    for record in frame.to_dict(orient="records")
                ]

            return _run(query)
        except Exception:  # noqa: BLE001 - fall through to the metrics already stored
            pass
    rows: List[Dict[str, Any]] = []
    for run_id in ids:
        run = get_evaluation_run(run_id)
        if run is None:
            continue
        row: Dict[str, Any] = {
            "run_id": run.get("id"),
            "name": run.get("name"),
            "mode": run.get("experiment_mode"),
            "llm_model": run.get("llm_model"),
            "results": len(run.get("results", []) or []),
        }
        for key, value in (run.get("metrics") or {}).items():
            if isinstance(value, (int, float, str, bool)) or value is None:
                row[key] = value
        rows.append(row)
    return rows


# ---- derived views shared by every page
def build_four_way(assessment: Mapping[str, Any]) -> Dict[str, Any]:
    """Split one assessment into REQUIRES / PROVES / INFERS / HUMAN-VERIFIES.

    This is the split the whole project argues for, so it is computed once, here, rather
    than assembled slightly differently on each page that shows it.

    * **REQUIRES** comes from the control library, not from the model. The requirement
      is a fact about the audit programme; letting the model's restatement stand in for
      it would make the reference move with the output it is supposed to be checked
      against. The model's own restatement is carried alongside as
      ``model_restatement`` so a reviewer can see whether it understood the control.
    * **PROVES** is the cited evidence *with its verification verdict*, never the
      model's prose. A FABRICATED citation appears here as a failure, not as proof.
    * **INFERS** is everything the model concluded beyond the quotes, including the
      status and risk level themselves.
    * **HUMAN VERIFIES** is what the system says it cannot settle: the model's own
      verification list, the evidence it says is missing, and any mechanically detected
      unsupported claim.
    """
    control = dict(assessment.get("control") or {})
    validation = dict(assessment.get("validation_report") or assessment.get("validation") or {})
    citations = [dict(item) for item in (assessment.get("citations") or [])]

    verified = [c for c in citations if str(c.get("verdict", "")).upper() == "VERIFIED"]
    fabricated = [c for c in citations if str(c.get("verdict", "")).upper() == "FABRICATED"]
    partial = [c for c in citations if str(c.get("verdict", "")).upper() == "PARTIAL"]

    return {
        "requires": {
            "control_ref": assessment.get("control_ref", "") or control.get("control_id", ""),
            "control_name": assessment.get("control_name", "") or control.get("name", ""),
            "objective": control.get("objective", ""),
            "criteria": list(control.get("assessment_criteria", []) or []),
            "expected_evidence": list(control.get("expected_evidence", []) or []),
            "framework_refs": list(control.get("framework_refs", []) or []),
            "model_restatement": assessment.get("model_control_requirement", ""),
            "source": "Control library definition",
        },
        "proves": {
            "citations": citations,
            "verified": len(verified),
            "partial": len(partial),
            "fabricated": len(fabricated),
            "total": len(citations),
            "grounding_rate": validation.get("grounding_rate", 0.0),
            "grounding_rate_strict": validation.get("grounding_rate_strict", 0.0),
            "sufficiency": assessment.get("evidence_sufficiency", ""),
            "statement": assessment.get("assessment", ""),
            "source": "Verbatim quotes, mechanically re-checked against the stored chunk",
        },
        "infers": {
            "status": assessment.get("status", ""),
            "risk_level": assessment.get("risk_level", ""),
            "risk_score": assessment.get("risk_score", 0.0),
            "confidence": assessment.get("confidence", ""),
            "finding": assessment.get("finding", ""),
            "risk": assessment.get("risk", ""),
            "inferences": list(assessment.get("inferences", []) or []),
            "reasoning": assessment.get("reasoning", ""),
            "source": "Model conclusion - not established by the quotes above",
        },
        "human": {
            "items": list(assessment.get("human_verification_required", []) or []),
            "missing_evidence": list(assessment.get("missing_evidence", []) or []),
            "unsupported_claims": list(validation.get("unsupported_claims", []) or []),
            "recommendation": assessment.get("recommendation", ""),
            "reviewed": bool(assessment.get("is_reviewed")),
            "source": "Outstanding before this can become an audit conclusion",
        },
    }


#: Fields compared when deciding whether an auditor diverged from the AI.
_DIVERGENCE_FIELDS: Tuple[Tuple[str, str, str], ...] = (
    ("status", "final_status", "Conclusion"),
    ("risk_level", "final_risk_level", "Risk level"),
    ("finding", "final_finding", "Finding"),
    ("recommendation", "final_recommendation", "Recommendation"),
)


def build_ai_vs_human(assessment: Mapping[str, Any]) -> Dict[str, Any]:
    """Side-by-side AI assessment and final auditor conclusion, with the differences named.

    A field counts as diverged only when the auditor actually wrote something and it
    differs from the AI text. A blank ``final_finding`` means "left as it stood", not
    "the auditor concluded nothing", and colouring it as a disagreement would inflate
    the divergence the study reports.
    """
    review = dict(assessment.get("latest_review") or {}) or None
    ai = {
        "status": assessment.get("status", ""),
        "risk_level": assessment.get("risk_level", ""),
        "finding": assessment.get("finding", ""),
        "recommendation": assessment.get("recommendation", ""),
        "confidence": assessment.get("confidence", ""),
        "label": "AI-generated assessment (not a final audit conclusion)",
    }
    if not review:
        return {
            "ai": ai,
            "human": None,
            "reviewed": False,
            "divergences": [],
            "agreed_status": None,
            "agreed_risk": None,
            "label": "Awaiting auditor review",
        }

    human = {
        "status": review.get("final_status", ""),
        "risk_level": review.get("final_risk_level", ""),
        "finding": review.get("final_finding", ""),
        "recommendation": review.get("final_recommendation", ""),
        "decision": review.get("decision", ""),
        "reviewer_name": review.get("reviewer_name", ""),
        "comments": review.get("comments", ""),
        "review_seconds": review.get("review_seconds", 0.0),
        "usefulness_rating": review.get("usefulness_rating"),
        "flagged_hallucination": bool(review.get("flagged_hallucination")),
        "label": "Final auditor assessment",
    }

    divergences: List[Dict[str, str]] = []
    for ai_key, human_key, label in _DIVERGENCE_FIELDS:
        ai_value = str(assessment.get(ai_key, "") or "").strip()
        human_value = str(review.get(human_key, "") or "").strip()
        if human_value and human_value != ai_value:
            divergences.append(
                {"field": ai_key, "label": label, "ai": ai_value, "human": human_value}
            )

    return {
        "ai": ai,
        "human": human,
        "reviewed": str(review.get("decision", "")) != HumanDecision.PENDING.value,
        "divergences": divergences,
        "agreed_status": bool(review.get("agreed_with_ai_status")),
        "agreed_risk": bool(review.get("agreed_with_ai_risk")),
        "label": "AI assessment versus final auditor conclusion",
    }


# ---- serialisation helpers (always run inside a session scope)
def _chunk_dict(chunk: Any) -> Dict[str, Any]:
    """Chunk row without its embedding blob, which is bytes and never displayed."""
    data = chunk.to_dict()
    data.pop("embedding", None)
    data["has_embedding"] = bool(getattr(chunk, "embedding", None))
    return _normalise_chunk(data)


def _normalise_chunk(data: Dict[str, Any]) -> Dict[str, Any]:
    """Give every chunk dictionary both ``chunk_id`` and ``id``.

    ``app.evidence.service`` and the API serialise a chunk with ``chunk_id``; the ORM row
    calls the same value ``id``. Rather than make each page remember which transport it
    is talking to, both keys are always present and carry the same number. ``chunk_id``
    is the canonical one - it is what a citation points at.
    """
    identifier = data.get("chunk_id", data.get("id"))
    if identifier is not None:
        data["chunk_id"] = int(identifier)
        data["id"] = int(identifier)
    return data


def _normalise_context(context: Dict[str, Any]) -> Dict[str, Any]:
    """Apply :func:`_normalise_chunk` to every chunk nested in a context payload."""
    for key in ("chunks", "before", "after"):
        context[key] = [_normalise_chunk(dict(item)) for item in (context.get(key) or [])]
    if isinstance(context.get("chunk"), dict):
        context["chunk"] = _normalise_chunk(dict(context["chunk"]))
    return context


def _assessment_summary(assessment: Any) -> Dict[str, Any]:
    from app.audit import service as audit_service

    data = audit_service.assessment_to_dict(assessment, include_citations=False)
    return _lighten(data)


def _lighten(data: Dict[str, Any]) -> Dict[str, Any]:
    """Strip the heavy columns and reduce the validation blob to its scalar summary."""
    for field in _HEAVY_FIELDS:
        data.pop(field, None)
    report = dict(data.pop("validation_report", {}) or {})
    # Both grounding rates are carried: the headline one (which half-credits PARTIAL
    # matches) and the strict one (verified / total). app.audit.validators states that
    # the strict figure is the one to quote in a write-up because it rests on the match
    # threshold alone, so a page must be able to show it without re-deriving it.
    data["validation"] = {
        "total": report.get("total", 0),
        "verified": report.get("verified", 0),
        "partial": report.get("partial", 0),
        "fabricated": report.get("fabricated", 0),
        "grounding_rate": report.get("grounding_rate", 0.0),
        "grounding_rate_strict": report.get("grounding_rate_strict", 0.0),
        "has_hallucination": report.get("has_hallucination", False),
        "unsupported_claims": list(report.get("unsupported_claims", []) or []),
        "rails_applied": list(report.get("rails_applied", []) or []),
        "status_downgraded": report.get("status_downgraded", False),
        "original_status": report.get("original_status", ""),
    }
    _decorate_assessment(data)
    return data


def _decorate_assessment(data: Dict[str, Any]) -> Dict[str, Any]:
    """Add the display fields every page would otherwise recompute."""
    mode = ExperimentMode.coerce(data.get("experiment_mode"), ExperimentMode.C_RAG_WORKFLOW)
    data["mode_label"] = mode.label
    review = data.get("latest_review") or {}
    data["review_decision"] = review.get("decision", HumanDecision.PENDING.value)
    data["final_status"] = review.get("final_status", "")
    data["final_risk_level"] = review.get("final_risk_level", "")
    data["agreed_with_ai_status"] = review.get("agreed_with_ai_status")

    validation = data.get("validation") or data.get("validation_report") or {}
    data["grounding_rate"] = validation.get("grounding_rate", 0.0)
    data["grounding_rate_strict"] = validation.get("grounding_rate_strict", 0.0)
    data["fabricated_citations"] = validation.get("fabricated", 0)
    data["verified_citations"] = validation.get("verified", 0)
    if "citation_count" not in data:
        data["citation_count"] = len(data.get("citations", []) or [])

    raw = data.get("raw_response")
    if raw and not data.get("model_control_requirement"):
        data["model_control_requirement"] = _model_requirement(raw)
    return data


def _model_requirement(raw_response: str) -> str:
    """The model's own restatement of the control, recovered from its raw JSON.

    Best effort by design: ``Assessment`` has no column for it, and a model that
    returned prose instead of JSON simply has none to recover. Returning "" is the
    correct answer in that case, not an error.
    """
    try:
        from app.llm.base import extract_json

        payload = extract_json(str(raw_response))
    except Exception:  # noqa: BLE001 - unparseable output is expected, not exceptional
        return ""
    value = payload.get("control_requirement") if isinstance(payload, dict) else None
    return str(value or "")


def _report_summary(data: Dict[str, Any]) -> Dict[str, Any]:
    """Report row without its rendered body, which can be hundreds of kilobytes.

    The API's list endpoint already omits the body and reports ``content_chars``
    itself, so those fields are only derived here when the body was actually present -
    overwriting them with a length of zero would tell the page the report was empty.
    """
    content = data.pop("content", None)
    if content is not None:
        data["content_chars"] = len(content)
    data.setdefault("content_chars", 0)
    if "stored_path" in data:
        data["has_file"] = bool(data.pop("stored_path"))
    data.setdefault("has_file", False)
    return data


def _run_summary(run: Any) -> Dict[str, Any]:
    data = run.to_dict()
    data["result_count"] = len(run.results)
    mode = ExperimentMode.coerce(data.get("experiment_mode"), None)
    data["mode_label"] = mode.label if mode is not None else str(data.get("experiment_mode", ""))
    return data


def to_json(value: Any) -> str:
    """Pretty JSON for the raw-output viewers, never raising on an odd type."""
    try:
        return json.dumps(value, indent=2, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return str(value)


__all__ = [
    "CACHE_TTL_SECONDS",
    "DEMO_DATASET_IDS",
    "BackendUnavailableError",
    "DataAccessError",
    "FeatureUnavailableError",
    "InvalidInputError",
    "NotFoundError",
    "assessment_statuses",
    "backend_label",
    "backend_mode",
    "bootstrap_database",
    "build_ai_vs_human",
    "build_four_way",
    "cache_version",
    "chunk_context",
    "compare_evaluation_runs",
    "confidence_levels",
    "control_categories",
    "create_control",
    "create_project",
    "dashboard_stats",
    "database_is_empty",
    "default_mode",
    "delete_evidence",
    "delete_project",
    "evaluation_available",
    "evidence_types",
    "experiment_modes",
    "first_run_state",
    "generate_report",
    "get_assessment",
    "get_chunk",
    "get_control",
    "get_evaluation_run",
    "get_evidence",
    "get_project",
    "get_report",
    "health",
    "human_decisions",
    "invalidate_cache",
    "latest_assessment_for_control",
    "list_activity",
    "list_assessments",
    "list_chunks",
    "list_controls",
    "list_datasets",
    "list_evaluation_runs",
    "list_evidence",
    "list_findings",
    "list_projects",
    "list_reports",
    "list_reviews",
    "list_scoped_controls",
    "list_unscoped_controls",
    "load_demo_project",
    "pending_reviews",
    "project_evidence_stats",
    "project_statuses",
    "provider_badge",
    "record_review",
    "refresh_settings",
    "risk_breakdown",
    "risk_levels",
    "run_assessment",
    "run_evaluation",
    "run_project_assessment",
    "scope_controls",
    "set_control_active",
    "settings_summary",
    "status_breakdown",
    "sufficiency_levels",
    "to_json",
    "unscope_control",
    "update_control",
    "update_project",
    "upload_evidence",
    "use_api",
]
