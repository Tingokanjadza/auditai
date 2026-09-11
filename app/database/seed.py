"""Loading the synthetic control library into the database.

Both entry points (FastAPI startup and the Streamlit app) call :func:`bootstrap` before
serving anything, and Streamlit in particular re-runs its script on every interaction.
Seeding therefore has to be *idempotent by construction* rather than by a "have we run
yet?" flag: controls are upserted on their business key ``control_id`` and the demo
project is looked up by name before being created. Calling ``bootstrap()`` a hundred
times leaves exactly the same rows as calling it once.

The library itself lives in ``data/controls/control_library.json`` rather than in code
so that a researcher can edit, extend or replace the control set without touching the
application, and so that the file can be cited as a study artefact. It is entirely
synthetic - see the ``disclaimer`` key inside the file.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database.base import init_db, session_scope
from app.database.models import AuditProject, Control, ProjectControl
from app.schemas.enums import ProjectStatus, RiskLevel

#: Default library filename inside ``settings.controls_dir``.
LIBRARY_FILENAME = "control_library.json"

#: The five controls the demo project is scoped to. These are the controls the
#: evaluation datasets are written against, so the demo project and the experiment
#: harness exercise the same requirements.
MANDATED_CONTROL_REFS: List[str] = [
    "CONTROL-001",
    "CONTROL-002",
    "CONTROL-003",
    "CONTROL-004",
    "CONTROL-005",
]

DEMO_PROJECT_NAME = "Privileged Access Management Audit"
DEMO_AUDIT_AREA = "Identity and Access Management"

#: Columns copied straight from a library entry onto a :class:`Control` row.
_CONTROL_FIELDS = (
    "name",
    "objective",
    "description",
    "risk_addressed",
    "expected_evidence",
    "assessment_criteria",
    "framework_refs",
    "category",
    "control_type",
    "control_frequency",
    "inherent_risk",
    "privilege_level",
    "data_sensitivity",
    "retrieval_keywords",
    "source",
)

_LIST_FIELDS = ("expected_evidence", "assessment_criteria", "framework_refs", "retrieval_keywords")

#: Streamlit reruns and uvicorn workers can enter ``bootstrap`` concurrently on the same
#: SQLite file; the lock keeps a single process from racing itself into an
#: IntegrityError. Cross-process races are still handled defensively below.
_BOOTSTRAP_LOCK = threading.Lock()


def library_path(path: Optional[str] = None) -> Path:
    """Resolve the control library file, defaulting to ``settings.controls_dir``."""
    if path:
        return Path(path).expanduser()
    return Path(get_settings().controls_dir) / LIBRARY_FILENAME


def load_control_library(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Read and normalise the synthetic control library.

    Accepts either the documented ``{"disclaimer": ..., "controls": [...]}`` shape or a
    bare list, so a researcher can drop in a trimmed file without ceremony. Normalising
    here (rather than in the DB layer) means every consumer - seeding, the query builder,
    the report - sees clean types: lists are lists of non-empty strings, the two 1-5
    scores are clamped integers, and ``inherent_risk`` is a valid :class:`RiskLevel`.
    """
    target = library_path(path)
    if not target.exists():
        raise FileNotFoundError(f"Control library not found at {target}")

    with target.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    if isinstance(payload, dict):
        raw_controls = payload.get("controls", [])
    elif isinstance(payload, list):
        raw_controls = payload
    else:  # pragma: no cover - malformed file
        raise ValueError(f"Unexpected control library structure in {target}: {type(payload)!r}")

    controls: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}
    for index, entry in enumerate(raw_controls):
        if not isinstance(entry, dict):
            raise ValueError(f"Control library entry {index} is not an object")
        ref = str(entry.get("control_id", "")).strip()
        if not ref:
            raise ValueError(f"Control library entry {index} has no control_id")
        if ref in seen:
            raise ValueError(f"Duplicate control_id {ref} at entries {seen[ref]} and {index}")
        seen[ref] = index
        controls.append(_normalise_entry(entry, ref))
    return controls


def library_disclaimer(path: Optional[str] = None) -> str:
    """The library's own provenance statement, surfaced in the UI and in reports."""
    target = library_path(path)
    if not target.exists():
        return ""
    with target.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict):
        return str(payload.get("disclaimer", ""))
    return ""


def _normalise_entry(entry: Dict[str, Any], ref: str) -> Dict[str, Any]:
    out: Dict[str, Any] = dict(entry)
    out["control_id"] = ref
    out["name"] = str(entry.get("name", ref)).strip()
    for key in ("objective", "description", "risk_addressed"):
        out[key] = str(entry.get(key, "") or "").strip()
    for key in _LIST_FIELDS:
        out[key] = _as_str_list(entry.get(key))
    out["category"] = str(entry.get("category", "General") or "General").strip()
    out["control_type"] = str(entry.get("control_type", "Preventive") or "Preventive").strip()
    out["control_frequency"] = str(entry.get("control_frequency", "Continuous") or "Continuous").strip()
    out["inherent_risk"] = RiskLevel.coerce(entry.get("inherent_risk"), RiskLevel.MEDIUM).value
    out["privilege_level"] = _clamp_score(entry.get("privilege_level"), default=3)
    out["data_sensitivity"] = _clamp_score(entry.get("data_sensitivity"), default=3)
    out["source"] = str(entry.get("source", "synthetic-library") or "synthetic-library").strip()
    out["is_active"] = bool(entry.get("is_active", True))
    return out


def _as_str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _clamp_score(value: Any, default: int = 3) -> int:
    """Keep the 1-5 risk inputs inside the range the prototype risk model assumes."""
    try:
        score = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(5, score))


# ---- database seeding
def seed_controls(session: Session, overwrite: bool = False, path: Optional[str] = None) -> int:
    """Upsert the library into the ``controls`` table; return the number of rows written.

    With ``overwrite=False`` (the default) an existing control is left exactly as it is,
    so an auditor's local edits survive a restart and a repeat call writes nothing. With
    ``overwrite=True`` the library file wins and every field is refreshed - that is the
    "reset control library" action, and the only way an edited definition goes back to
    the published one.
    """
    entries = load_control_library(path)
    existing = {
        control.control_id: control
        for control in session.execute(select(Control)).scalars().all()
    }

    written = 0
    for entry in entries:
        control = existing.get(entry["control_id"])
        if control is None:
            session.add(_build_control(entry))
            written += 1
        elif overwrite:
            _apply_entry(control, entry)
            written += 1

    if written:
        try:
            session.commit()
        except IntegrityError:
            # Another process seeded the same rows between the SELECT and the COMMIT.
            # The end state is identical either way, so treat it as a no-op.
            session.rollback()
            return 0
    return written


def _build_control(entry: Dict[str, Any]) -> Control:
    control = Control(control_id=entry["control_id"])
    _apply_entry(control, entry)
    return control


def _apply_entry(control: Control, entry: Dict[str, Any]) -> None:
    for field in _CONTROL_FIELDS:
        setattr(control, field, entry[field])
    control.is_active = entry["is_active"]


def seed_demo_project(session: Session, control_refs: Optional[Sequence[str]] = None) -> AuditProject:
    """Create (or return) the demo audit project with the mandated controls in scope.

    The demo project exists so that the application is not an empty shell on first run:
    a reader of the thesis can start the UI and immediately see a realistic project with
    controls scoped, ready for evidence. Its period is fixed synthetic metadata, not a
    reference to any real engagement.
    """
    refs = list(control_refs) if control_refs is not None else MANDATED_CONTROL_REFS
    settings = get_settings()

    project = (
        session.execute(select(AuditProject).where(AuditProject.name == DEMO_PROJECT_NAME))
        .scalars()
        .first()
    )
    if project is None:
        project = AuditProject(
            name=DEMO_PROJECT_NAME,
            audit_area=DEMO_AUDIT_AREA,
            description=(
                "Synthetic demonstration engagement covering the design and operating "
                "effectiveness of controls over privileged access, account lifecycle, "
                "patching, change approval and password configuration. All data in this "
                "project is fabricated for research purposes."
            ),
            period_start=datetime(2025, 1, 1, tzinfo=timezone.utc),
            period_end=datetime(2025, 6, 30, tzinfo=timezone.utc),
            auditor_name=settings.default_auditor_name,
            status=ProjectStatus.FIELDWORK.value,
            scope_note=(
                "Scope limited to the five library controls used by the evaluation "
                "datasets. Synthetic evidence only - no real system was examined."
            ),
            is_demo=True,
        )
        session.add(project)
        try:
            session.commit()
        except IntegrityError:  # pragma: no cover - concurrent bootstrap
            session.rollback()
            project = (
                session.execute(select(AuditProject).where(AuditProject.name == DEMO_PROJECT_NAME))
                .scalars()
                .first()
            )
            if project is None:
                raise

    _scope_refs(session, project, refs)
    return project


def _scope_refs(session: Session, project: AuditProject, refs: Sequence[str]) -> int:
    """Link the named library controls to ``project``, skipping ones already scoped."""
    controls = (
        session.execute(select(Control).where(Control.control_id.in_(list(refs))))
        .scalars()
        .all()
    )
    by_ref = {control.control_id: control for control in controls}
    already = {
        row.control_id
        for row in session.execute(
            select(ProjectControl).where(ProjectControl.project_id == project.id)
        )
        .scalars()
        .all()
    }

    added = 0
    for ref in refs:
        control = by_ref.get(ref)
        if control is None or control.id in already:
            continue
        session.add(
            ProjectControl(
                project_id=project.id,
                control_id=control.id,
                scope_note="Scoped by demo seed data.",
            )
        )
        added += 1

    if added:
        try:
            session.commit()
        except IntegrityError:  # pragma: no cover - concurrent bootstrap
            session.rollback()
            return 0
    return added


def bootstrap(session: Optional[Session] = None, overwrite: bool = False) -> Dict[str, Any]:
    """Seed controls and the demo project. Safe to call on every application start.

    Pass a ``session`` when the caller already owns one (tests, a FastAPI request, a
    temporary database). With no session this opens its own and also calls
    :func:`init_db`, because the only caller that has no session is an application
    starting from scratch. ``init_db`` is deliberately *not* called when a session is
    supplied: that session may be bound to a different engine, and creating tables on
    the default engine as a side effect would be surprising.
    """
    if session is not None:
        return _bootstrap_with(session, overwrite)

    with _BOOTSTRAP_LOCK:
        init_db()
        with session_scope() as own_session:
            return _bootstrap_with(own_session, overwrite)


def _bootstrap_with(session: Session, overwrite: bool) -> Dict[str, Any]:
    controls_written = seed_controls(session, overwrite=overwrite)
    project = seed_demo_project(session)
    total_controls = session.execute(select(Control)).scalars().all()
    return {
        "controls_written": controls_written,
        "controls_total": len(total_controls),
        "demo_project_id": project.id,
        "demo_project_name": project.name,
        "scoped_control_refs": [
            link.control.control_id for link in project.scoped_controls if link.control is not None
        ],
    }


__all__ = [
    "DEMO_AUDIT_AREA",
    "DEMO_PROJECT_NAME",
    "LIBRARY_FILENAME",
    "MANDATED_CONTROL_REFS",
    "bootstrap",
    "library_disclaimer",
    "library_path",
    "load_control_library",
    "seed_controls",
    "seed_demo_project",
]
