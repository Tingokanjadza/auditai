"""HTTP transport for the Streamlit UI when it is pointed at the FastAPI backend.

The interface is designed to run in one process: :mod:`app.frontend.data_access` calls
:mod:`app.audit.service` directly and ``streamlit run`` alone is the whole application.
This module exists for the second, optional configuration - ``USE_API=true`` - where the
same pages talk to :mod:`app.api` over HTTP, so that the deployed shape (a UI process
and a service process) can be demonstrated and measured.

Design rules
------------
* **No connection at import time.** Constructing a client builds nothing; the first
  request opens the first socket. The application must stay importable offline.
* **One place for every path.** :data:`ROUTES` is the complete map from a facade
  operation to an HTTP method and path, checked against the router definitions in
  ``app/api/routers/``. A route change is a one-line fix here rather than a hunt through
  the pages.
* **Envelopes are unwrapped here, not in the pages.** Several endpoints wrap their
  payload (``{"counts": ...}``, ``{"run": ...}``, ``{"rows": ...}``). The methods below
  return the same shape the in-process facade returns, so no page can tell the two
  transports apart.
* **Errors are translated, not leaked.** Every failure - transport, timeout, 4xx, 5xx -
  becomes an :class:`ApiError` carrying a message a page can display. ``httpx`` never
  escapes this module.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import httpx

from app.config import Settings, get_settings

#: Every operation the facade performs, as ``(method, path template)``. Verified against
#: the routers registered by ``app.api.main``; ``{}`` fields come from the call
#: arguments.
ROUTES: Dict[str, Tuple[str, str]] = {
    "health": ("GET", "/health"),
    "settings": ("GET", "/api/v1/settings"),
    "provider_health": ("GET", "/api/v1/settings/providers"),
    # projects
    "list_projects": ("GET", "/api/v1/projects"),
    "get_project": ("GET", "/api/v1/projects/{project_id}"),
    "create_project": ("POST", "/api/v1/projects"),
    "update_project": ("PATCH", "/api/v1/projects/{project_id}"),
    "delete_project": ("DELETE", "/api/v1/projects/{project_id}"),
    "scoped_controls": ("GET", "/api/v1/projects/{project_id}/controls"),
    "scope_controls": ("POST", "/api/v1/projects/{project_id}/controls"),
    "unscope_control": ("DELETE", "/api/v1/projects/{project_id}/controls/{control_ref}"),
    # control library
    "list_controls": ("GET", "/api/v1/controls"),
    "control_categories": ("GET", "/api/v1/controls/categories"),
    "get_control": ("GET", "/api/v1/controls/{control_ref}"),
    "create_control": ("POST", "/api/v1/controls"),
    "update_control": ("PATCH", "/api/v1/controls/{control_ref}"),
    "activate_control": ("POST", "/api/v1/controls/{control_ref}/activate"),
    "deactivate_control": ("POST", "/api/v1/controls/{control_ref}/deactivate"),
    # evidence
    "upload_evidence": ("POST", "/api/v1/evidence/upload"),
    "list_evidence": ("GET", "/api/v1/evidence"),
    "evidence_stats": ("GET", "/api/v1/evidence/stats"),
    "chunk_context": ("GET", "/api/v1/evidence/chunks/{chunk_id}"),
    "get_evidence": ("GET", "/api/v1/evidence/{evidence_file_id}"),
    "list_chunks": ("GET", "/api/v1/evidence/{evidence_file_id}/chunks"),
    "delete_evidence": ("DELETE", "/api/v1/evidence/{evidence_file_id}"),
    # assessments
    "run_assessment": ("POST", "/api/v1/assessments/run"),
    "list_assessments": ("GET", "/api/v1/assessments"),
    "list_findings": ("GET", "/api/v1/assessments/findings"),
    "get_assessment": ("GET", "/api/v1/assessments/{assessment_id}"),
    # human review
    "record_review": ("POST", "/api/v1/reviews"),
    "list_reviews": ("GET", "/api/v1/reviews"),
    "pending_reviews": ("GET", "/api/v1/reviews/pending"),
    # dashboard
    "dashboard_stats": ("GET", "/api/v1/dashboard/stats"),
    "status_breakdown": ("GET", "/api/v1/dashboard/status-breakdown"),
    "risk_breakdown": ("GET", "/api/v1/dashboard/risk-breakdown"),
    "activity": ("GET", "/api/v1/dashboard/activity"),
    # reports
    "generate_report": ("POST", "/api/v1/reports/generate"),
    "list_reports": ("GET", "/api/v1/reports"),
    "get_report": ("GET", "/api/v1/reports/{report_id}"),
    "download_report": ("GET", "/api/v1/reports/{report_id}/download"),
    # evaluation
    "list_datasets": ("GET", "/api/v1/evaluation/datasets"),
    "run_evaluation": ("POST", "/api/v1/evaluation/run"),
    "list_runs": ("GET", "/api/v1/evaluation/runs"),
    "compare_runs": ("GET", "/api/v1/evaluation/compare"),
    "get_run": ("GET", "/api/v1/evaluation/runs/{run_id}"),
}


class ApiError(RuntimeError):
    """A backend call did not succeed. Carries the status code when there was one."""

    def __init__(self, message: str, status_code: Optional[int] = None, path: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.path = path


class ApiClient:
    """Thin synchronous client over the audit API.

    One instance is reused for the life of the Streamlit process (see
    :func:`get_client`) so that connections are pooled; ``httpx.Client`` is thread-safe,
    which matters because Streamlit runs each browser session on its own thread.
    """

    def __init__(
        self,
        base_url: str = "",
        timeout: Optional[float] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        resolved = settings or get_settings()
        self.base_url = (base_url or resolved.api_base_url).rstrip("/")
        self.timeout = float(timeout if timeout is not None else resolved.api_request_timeout)
        self._client: Optional[httpx.Client] = None

    # ---- transport
    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def request(
        self,
        operation: str,
        path_params: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Optional[Any] = None,
        files: Optional[Any] = None,
        data: Optional[Mapping[str, Any]] = None,
        raw: bool = False,
    ) -> Any:
        """Perform one :data:`ROUTES` operation and return the decoded body."""
        try:
            method, template = ROUTES[operation]
        except KeyError:
            raise ApiError("No API route is defined for operation {0!r}.".format(operation))

        path = template.format(**(path_params or {}))
        try:
            response = self._http().request(
                method,
                path,
                params=_clean_params(params),
                json=json_body,
                files=files,
                data=data,
            )
        except httpx.TimeoutException as exc:
            raise ApiError(
                "The audit API did not respond within {0:.0f}s ({1}). A full assessment or "
                "evaluation run can exceed this; raise API_REQUEST_TIMEOUT, or unset "
                "USE_API to run the workflow in-process.".format(self.timeout, path),
                path=path,
            ) from exc
        except httpx.HTTPError as exc:
            raise ApiError(
                "Cannot reach the audit API at {0}: {1}. Start it with "
                "`python run.py api`, or unset USE_API to run the UI in-process.".format(
                    self.base_url, exc
                ),
                path=path,
            ) from exc

        if response.status_code >= 400:
            raise ApiError(
                "{0} {1} -> {2}: {3}".format(method, path, response.status_code, _detail(response)),
                status_code=response.status_code,
                path=path,
            )
        if raw:
            return response.content
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise ApiError(
                "The audit API returned a non-JSON body for {0}.".format(path), path=path
            ) from exc

    # ---- configuration
    def health(self) -> Dict[str, Any]:
        return _as_dict(self.request("health"))

    def settings_summary(self) -> Dict[str, Any]:
        return _as_dict(self.request("settings"))

    def provider_health(self) -> Dict[str, Any]:
        return _as_dict(self.request("provider_health"))

    # ---- projects
    def list_projects(self, **params: Any) -> List[Dict[str, Any]]:
        return _as_list(self.request("list_projects", params=params))

    def get_project(self, project_id: int) -> Optional[Dict[str, Any]]:
        return _optional_dict(self.request("get_project", {"project_id": int(project_id)}))

    def create_project(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        return _as_dict(self.request("create_project", json_body=_clean_body(payload)))

    def update_project(self, project_id: int, payload: Mapping[str, Any]) -> Dict[str, Any]:
        return _as_dict(
            self.request(
                "update_project", {"project_id": int(project_id)}, json_body=_clean_body(payload)
            )
        )

    def delete_project(self, project_id: int) -> bool:
        payload = _as_dict(self.request("delete_project", {"project_id": int(project_id)}))
        return bool(payload.get("deleted", True))

    # ---- control library
    def list_controls(self, **params: Any) -> List[Dict[str, Any]]:
        return _as_list(self.request("list_controls", params=params))

    def get_control(self, control_ref: Any) -> Optional[Dict[str, Any]]:
        return _optional_dict(self.request("get_control", {"control_ref": control_ref}))

    def create_control(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        return _as_dict(self.request("create_control", json_body=_clean_body(payload)))

    def update_control(self, control_ref: Any, payload: Mapping[str, Any]) -> Dict[str, Any]:
        return _as_dict(
            self.request("update_control", {"control_ref": control_ref}, json_body=_clean_body(payload))
        )

    def set_control_active(self, control_ref: Any, active: bool, actor: str = "") -> Dict[str, Any]:
        operation = "activate_control" if active else "deactivate_control"
        return _as_dict(
            self.request(operation, {"control_ref": control_ref}, params={"actor": actor or None})
        )

    def control_categories(self, **params: Any) -> List[str]:
        payload = self.request("control_categories", params=params)
        return [str(value) for value in payload] if isinstance(payload, list) else []

    def scoped_controls(self, project_id: int) -> List[str]:
        """Returns control *references*: the scope endpoint reports refs, not full rows."""
        payload = _as_dict(self.request("scoped_controls", {"project_id": int(project_id)}))
        return [str(ref) for ref in payload.get("scoped_control_refs", [])]

    def scope_controls(
        self, project_id: int, control_refs: Sequence[Any], scope_note: str = "", actor: str = ""
    ) -> Dict[str, Any]:
        return _as_dict(
            self.request(
                "scope_controls",
                {"project_id": int(project_id)},
                json_body={
                    "control_refs": [str(ref) for ref in control_refs],
                    "scope_note": scope_note,
                    "actor": actor,
                },
            )
        )

    def unscope_control(self, project_id: int, control_ref: Any, actor: str = "") -> bool:
        self.request(
            "unscope_control",
            {"project_id": int(project_id), "control_ref": control_ref},
            params={"actor": actor or None},
        )
        return True

    # ---- evidence
    def list_evidence(self, **params: Any) -> List[Dict[str, Any]]:
        return _as_list(self.request("list_evidence", params=params))

    def get_evidence(self, evidence_file_id: int) -> Optional[Dict[str, Any]]:
        return _optional_dict(
            self.request("get_evidence", {"evidence_file_id": int(evidence_file_id)})
        )

    def evidence_stats(self, project_id: int) -> Dict[str, Any]:
        return _as_dict(self.request("evidence_stats", params={"project_id": int(project_id)}))

    def upload_evidence(
        self,
        project_id: int,
        data: bytes,
        filename: str,
        evidence_type: str = "OTHER",
        description: str = "",
        uploaded_by: str = "",
        is_synthetic: bool = False,
        provenance: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Multipart upload. The bytes are sent as-is; nothing is written locally first.

        ``provenance`` travels as an ordinary form field. It is omitted when ``None`` so
        the server applies its own default (SYNTHETIC) rather than receiving the string
        "None".
        """
        form: Dict[str, str] = {
            "project_id": str(int(project_id)),
            "evidence_type": str(evidence_type),
            "description": description or "",
            "uploaded_by": uploaded_by or "",
            "is_synthetic": "true" if is_synthetic else "false",
        }
        if provenance:
            form["provenance"] = str(getattr(provenance, "value", provenance))
        return _as_dict(
            self.request(
                "upload_evidence",
                files={"file": (filename, bytes(data), "application/octet-stream")},
                data=form,
            )
        )

    def delete_evidence(self, evidence_file_id: int) -> bool:
        payload = _as_dict(
            self.request("delete_evidence", {"evidence_file_id": int(evidence_file_id)})
        )
        return bool(payload.get("deleted", True))

    def list_chunks(self, evidence_file_id: int, **params: Any) -> List[Dict[str, Any]]:
        return _as_list(
            self.request(
                "list_chunks", {"evidence_file_id": int(evidence_file_id)}, params=params
            )
        )

    def chunk_context(self, chunk_id: int, window: int = 1) -> Dict[str, Any]:
        return _as_dict(
            self.request("chunk_context", {"chunk_id": int(chunk_id)}, params={"window": int(window)})
        )

    # ---- assessments
    def list_assessments(self, **params: Any) -> List[Dict[str, Any]]:
        return _as_list(self.request("list_assessments", params=params))

    def get_assessment(self, assessment_id: int) -> Optional[Dict[str, Any]]:
        return _optional_dict(self.request("get_assessment", {"assessment_id": int(assessment_id)}))

    def list_findings(self, **params: Any) -> List[Dict[str, Any]]:
        return _as_list(self.request("list_findings", params=params))

    def run_assessment(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        """Returns the run record, unwrapped from the ``{persisted, assessment, run}`` envelope."""
        envelope = _as_dict(self.request("run_assessment", json_body=_clean_body(payload)))
        run = dict(envelope.get("run") or {})
        run.setdefault("assessment_id", (envelope.get("assessment") or {}).get("id"))
        run["persisted"] = bool(envelope.get("persisted"))
        run["notice"] = envelope.get("notice", "")
        return run

    # ---- human review
    def record_review(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        return _as_dict(self.request("record_review", json_body=_clean_body(payload)))

    def list_reviews(self, **params: Any) -> List[Dict[str, Any]]:
        return _as_list(self.request("list_reviews", params=params))

    def pending_reviews(self, **params: Any) -> List[Dict[str, Any]]:
        return _as_list(self.request("pending_reviews", params=params))

    # ---- dashboard
    def dashboard_stats(self, **params: Any) -> Dict[str, Any]:
        return _as_dict(self.request("dashboard_stats", params=params))

    def status_breakdown(self, **params: Any) -> Dict[str, int]:
        payload = _as_dict(self.request("status_breakdown", params=params))
        return {str(k): int(v) for k, v in (payload.get("counts") or {}).items()}

    def risk_breakdown(self, **params: Any) -> Dict[str, int]:
        payload = _as_dict(self.request("risk_breakdown", params=params))
        return {str(k): int(v) for k, v in (payload.get("counts") or {}).items()}

    def activity(self, **params: Any) -> List[Dict[str, Any]]:
        return _as_list(self.request("activity", params=params))

    # ---- reports
    def list_reports(self, **params: Any) -> List[Dict[str, Any]]:
        return _as_list(self.request("list_reports", params=params))

    def get_report(self, report_id: int) -> Optional[Dict[str, Any]]:
        return _optional_dict(self.request("get_report", {"report_id": int(report_id)}))

    def download_report(self, report_id: int) -> bytes:
        return bytes(self.request("download_report", {"report_id": int(report_id)}, raw=True) or b"")

    def generate_report(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        return _as_dict(self.request("generate_report", json_body=_clean_body(payload)))

    # ---- evaluation
    def list_datasets(self) -> List[Dict[str, Any]]:
        return _as_list(self.request("list_datasets"))

    def list_runs(self, **params: Any) -> List[Dict[str, Any]]:
        return _as_list(self.request("list_runs", params=params))

    def get_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        return _optional_dict(self.request("get_run", {"run_id": int(run_id)}))

    def run_evaluation(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        return _as_dict(self.request("run_evaluation", json_body=_clean_body(payload)))

    def compare_runs(self, run_ids: Sequence[int]) -> List[Dict[str, Any]]:
        payload = _as_dict(
            self.request("compare_runs", params={"run_ids": [int(value) for value in run_ids]})
        )
        return [dict(row) for row in (payload.get("rows") or [])]


# ---- helpers
def _clean_params(params: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """Drop ``None`` values so an unset filter is absent rather than the string 'None'."""
    if not params:
        return None
    out: Dict[str, Any] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif isinstance(value, (list, tuple, set)):
            out[key] = [str(item) for item in value]
        else:
            out[key] = value
    return out or None


def _clean_body(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Drop ``None`` fields from a JSON body.

    FastAPI partial-update endpoints use ``exclude_unset``, so sending an explicit null
    for an untouched field would be a change request rather than an omission. Dates are
    serialised to ISO strings because ``json`` cannot encode them.
    """
    out: Dict[str, Any] = {}
    for key, value in payload.items():
        if value is None:
            continue
        out[key] = value.isoformat() if hasattr(value, "isoformat") else value
    return out


def _detail(response: httpx.Response) -> str:
    """FastAPI's ``{"detail": ...}`` if present, otherwise a truncated body."""
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - an error page is not required to be JSON
        return response.text[:300]
    if isinstance(payload, dict) and "detail" in payload:
        return str(payload["detail"])[:500]
    return json.dumps(payload)[:300]


def _as_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _optional_dict(value: Any) -> Optional[Dict[str, Any]]:
    return dict(value) if isinstance(value, dict) else None


def _as_list(value: Any) -> List[Dict[str, Any]]:
    """Accept a bare list or a paginated ``{"items": [...]}`` envelope."""
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        for key in ("items", "results", "rows", "data"):
            if isinstance(value.get(key), list):
                return list(value[key])
    return []


_CLIENT: Optional[ApiClient] = None


def get_client(settings: Optional[Settings] = None) -> ApiClient:
    """Process-wide client. Built on first use, never at import."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = ApiClient(settings=settings)
    return _CLIENT


def reset_client() -> None:
    """Drop the pooled client so a changed base URL takes effect."""
    global _CLIENT
    if _CLIENT is not None:
        _CLIENT.close()
    _CLIENT = None


__all__ = ["ROUTES", "ApiClient", "ApiError", "get_client", "reset_client"]
