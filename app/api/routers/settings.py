"""Configuration, read-only.

Writing configuration over HTTP is not implemented, and its absence is a design decision
rather than an omission: this prototype has no authentication, and an unauthenticated
endpoint that could repoint the LLM base URL would let anyone who can reach the port
redirect every piece of audit evidence in the database to a server of their choosing.
Configuration therefore comes from the environment or ``.env`` and changes with a
restart, where the change is visible in the deployment rather than only in a log line.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body

from app.config import SUPPORTED_UPLOAD_EXTENSIONS, get_settings
from app.llm.factory import provider_health
from app.schemas.api import (
    ProviderHealth,
    SettingsResponse,
    SettingsUpdateRequest,
    SettingsUpdateResponse,
)

router = APIRouter(prefix="/settings", tags=["settings"])

_SECURITY_NOTE = (
    "This prototype has no authentication, no authorisation and no rate limiting. Run it "
    "on localhost with synthetic data only. Secrets are never returned by this API: the "
    "API key is shown as a masked fingerprint so an operator can confirm which key is "
    "configured, and the database URL is reduced to its backend name because it can carry "
    "credentials."
)

_HOW_TO_CHANGE = (
    "Edit .env (or the process environment) and restart the API. See .env.example for "
    "every supported variable."
)


@router.get(
    "",
    response_model=SettingsResponse,
    summary="Current configuration (secrets masked)",
    description=(
        "What the running process is configured to do. ``provider.llm_api_key`` is a masked "
        "fingerprint such as 'sk-...9f2c (len 51)' - never the key itself - and no filesystem "
        "path or connection string is included."
    ),
)
def get_settings_view() -> Dict[str, Any]:
    settings = get_settings()
    return {
        "app_name": settings.app_name,
        "app_short_name": settings.app_short_name,
        "app_version": settings.app_version,
        "environment": settings.environment,
        "debug": settings.debug,
        "provider": settings.provider_summary(),
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
        "security_note": _SECURITY_NOTE,
    }


@router.get(
    "/providers",
    response_model=ProviderHealth,
    summary="LLM provider status",
    description=(
        "Configuration-only status: no network call is made, so this is safe to poll and "
        "cannot leak the existence of a key to a remote endpoint. ``fell_back_to_mock`` "
        "true means a real provider was selected but is unconfigured, and results produced "
        "now measure the pipeline rather than a language model."
    ),
)
def providers() -> Dict[str, Any]:
    return provider_health()


@router.post(
    "",
    response_model=SettingsUpdateResponse,
    summary="Propose a configuration change (accepted, never applied)",
    description=(
        "Deliberately a no-op that always reports ``applied: false``.\n\n"
        "Changing provider configuration at runtime from an unauthenticated endpoint would "
        "be a security hole in a tool that holds audit evidence: anyone able to reach the "
        "port could point the model client at a server they control and exfiltrate every "
        "piece of evidence subsequently assessed. The endpoint exists so that a client "
        "receives that explanation instead of a 404 that reads like a missing feature."
    ),
)
def reject_settings_update(payload: SettingsUpdateRequest = Body(default=None)) -> Dict[str, Any]:
    requested = sorted((payload.changes or {}).keys()) if payload is not None else []
    return {
        "applied": False,
        "message": (
            "Configuration is read-only over HTTP. Nothing was changed. This API is "
            "unauthenticated, and an endpoint that could repoint the model client or the "
            "storage locations would expose the evidence this system holds."
        ),
        "requested_keys": requested,
        "how_to_change": _HOW_TO_CHANGE,
    }
