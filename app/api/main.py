"""FastAPI application: the programmatic face of the audit prototype.

The app is thin by construction. Every route delegates to :mod:`app.audit.service`,
:mod:`app.evidence.service`, :mod:`app.audit.engine`, :mod:`app.audit.report` or
:mod:`app.evaluation`, which are the same modules the Streamlit interface calls
in-process. Nothing an auditor can do through the API is implemented twice.

Three application-level decisions live here rather than in the routers:

* **One error shape.** ``NotFoundError``, ``InvalidInputError`` and the evidence
  ingestion errors are translated to status codes once, at the bottom of this module, so
  no router needs a try/except and every failure - including an unhandled one - reaches
  the client as the same ``{"error": {...}}`` envelope.
* **Startup seeds the control library.** An empty control library makes every other
  endpoint useless, and seeding is idempotent by construction, so it runs on every start.
* **No authentication.** Stated in the OpenAPI description, in ``/health`` and in the
  settings payload, because a reader who only sees the interactive docs must not be left
  to discover it.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import Depends, FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routers import assessments, controls, dashboard, evaluation, evidence, projects, reports, reviews
from app.api.routers import settings as settings_router
from app.audit.service import InvalidInputError, NotFoundError, ServiceError
from app.config import get_settings
from app.database.base import get_db, init_db, session_scope
from app.evidence.service import EvidenceIngestError, EvidenceTooLargeError
from app.llm.factory import provider_health
from app.schemas.api import ErrorResponse, HealthResponse

logger = logging.getLogger(__name__)

#: Every route lives under this prefix so a future v2 can be served alongside v1.
API_V1_PREFIX = "/api/v1"

#: Repeated at the top of the OpenAPI description and in /health. A prototype that holds
#: audit evidence and has no authentication must say so where its users actually look.
SECURITY_WARNING = (
    "**No authentication.** This research prototype has no login, no authorisation and no "
    "rate limiting. Every endpoint is open to anyone who can reach the port. Bind it to "
    "localhost, use synthetic data only, and do not deploy it on a shared or public network."
)

DESCRIPTION = """
Programmatic access to the LLM-Assisted IT Audit Risk and Control Assessment System, a
university research prototype.

{warning}

### What this system does, and what it does not

It retrieves an auditor's evidence, asks a language model to assess one IT control
against that evidence, mechanically checks every citation the model produced against the
text it was actually shown, applies a prototype risk score, and records the result for a
human auditor to accept, modify or reject.

* **Every assessment is AI-generated and is not an audit conclusion.** Assessment
  payloads carry `source: "AI-generated"` and `requires_human_review: true`, and the
  human decision is stored as a separate record that never overwrites the AI one - that
  separation is what makes human/AI agreement measurable.
* **Risk levels come from a prototype research scoring model**, not from any official
  industry risk framework, and framework references in the control library are
  informative only.
* **`INSUFFICIENT_EVIDENCE` is a real outcome.** The system is built to say "this
  evidence does not establish how the control operates" rather than to reach a verdict
  regardless.
* With `LLM_PROVIDER=mock` the whole system runs offline against a deterministic
  rule-based stand-in. Results obtained that way measure the pipeline, not model quality.
  `GET /health` reports which provider actually answered.

### Conventions

* All endpoints are under `{prefix}`.
* Errors always return `{{"error": {{"type", "message", "status_code", "path", "detail"}}}}`.
* Long-running work (assessments, evaluation runs) is **synchronous**: the request blocks
  until it finishes. Endpoint descriptions state the latency to expect.
* Timestamps are ISO-8601 strings.
""".format(warning=SECURITY_WARNING, prefix=API_V1_PREFIX)

TAGS_METADATA: List[Dict[str, Any]] = [
    {
        "name": "system",
        "description": "Liveness, provider status and the API's own metadata. Not under the versioned prefix.",
    },
    {
        "name": "projects",
        "description": (
            "Audit engagements and the control scope that defines what will be tested. "
            "Deleting a project cascades to its evidence, assessments, reviews and reports."
        ),
    },
    {
        "name": "controls",
        "description": (
            "The reusable control library. Controls are retired rather than deleted, because "
            "past assessments and reports cite them."
        ),
    },
    {
        "name": "evidence",
        "description": (
            "Upload, inventory and inspect evidence. Files are hashed on receipt, parsed into "
            "chunks that each carry a precise source locator, and indexed for retrieval. "
            "`GET /evidence/chunks/{chunk_id}` is what makes a citation clickable."
        ),
    },
    {
        "name": "assessments",
        "description": (
            "Run an AI assessment of one control and read the results with their citations "
            "and grounding verdicts. AI-generated throughout; never a final audit decision."
        ),
    },
    {
        "name": "reviews",
        "description": (
            "The human auditor's decision on an AI assessment, stored separately from it, "
            "plus the queue of assessments still awaiting review."
        ),
    },
    {
        "name": "dashboard",
        "description": (
            "Headline figures, status and risk breakdowns and the activity trail. Every "
            "response states the basis it was counted on."
        ),
    },
    {
        "name": "reports",
        "description": (
            "Generate, list, read and download audit reports. The renderer keeps the "
            "AI-generated assessment and the auditor's final assessment visibly separate."
        ),
    },
    {
        "name": "evaluation",
        "description": (
            "The research harness: synthetic datasets with known ground truth, experiment "
            "runs for modes A/B/C, and comparisons between them."
        ),
    },
    {
        "name": "settings",
        "description": (
            "Read-only configuration with secrets masked. Configuration cannot be changed "
            "over HTTP; the POST endpoint explains why."
        ),
    },
]


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Create the schema and seed the control library before the first request.

    Seeding is idempotent (controls are upserted on ``control_id``), so this is safe on
    every start. A seeding failure is logged and swallowed rather than aborting startup:
    the library file can be edited or replaced by a researcher, and a malformed one should
    leave a running API that can report the problem, not a process that will not boot.
    """
    settings = get_settings()
    init_db()
    try:
        from app.database.seed import bootstrap

        with session_scope() as session:
            summary = bootstrap(session)
        logger.info(
            "Startup: control library has %s control(s); demo project id=%s.",
            summary.get("controls_total"),
            summary.get("demo_project_id"),
        )
    except Exception:  # noqa: BLE001 - a broken library file must not prevent serving
        logger.exception("Startup seeding failed; the API is running with whatever the database already held.")

    health = provider_health(settings)
    if health.get("fell_back_to_mock"):
        logger.warning("LLM provider fell back to the offline mock: %s", health.get("fallback_reason"))
    logger.info("API ready: provider=%s model=%s", health.get("active_provider"), health.get("active_model"))
    yield


def create_app() -> FastAPI:
    """Build the application. A function so tests can construct a fresh instance."""
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        description=DESCRIPTION,
        version=settings.app_version,
        openapi_tags=TAGS_METADATA,
        lifespan=lifespan,
        contact={"name": "Research prototype - synthetic data only"},
        license_info={"name": "Academic research prototype"},
        responses={
            400: {"model": ErrorResponse, "description": "Invalid input."},
            404: {"model": ErrorResponse, "description": "Referenced record does not exist."},
            500: {"model": ErrorResponse, "description": "Unhandled server error."},
        },
    )

    # CORS is permissive for local development only: the Streamlit UI, the interactive
    # docs and any notebook all run on some other localhost port, and blocking them would
    # only be theatre while the API itself is unauthenticated. The regex still refuses
    # every non-loopback origin, so a page on the internet cannot drive this API through a
    # researcher's browser. Credentials are off because the API has no cookies or sessions.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$",
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    _register_routers(app)
    _register_exception_handlers(app)
    _register_system_routes(app)
    return app


def _register_routers(app: FastAPI) -> None:
    for module in (projects, controls, evidence, assessments, reviews, dashboard, reports, evaluation):
        app.include_router(module.router, prefix=API_V1_PREFIX)
    app.include_router(settings_router.router, prefix=API_V1_PREFIX)


def _register_system_routes(app: FastAPI) -> None:
    @app.get(
        "/health",
        response_model=HealthResponse,
        tags=["system"],
        summary="Liveness, database and LLM provider status",
        description=(
            "Reports whether the database answers and which provider would serve the next "
            "assessment. Provider status is derived from configuration only - no network "
            "call is made - and no secret is included: the API key appears as a masked "
            "fingerprint and nowhere in full."
        ),
    )
    def health(session: Session = Depends(get_db)) -> Dict[str, Any]:
        settings = get_settings()
        database_ok = True
        database_error = ""
        try:
            session.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001 - the point of the check is to report this
            database_ok = False
            database_error = "{0}: {1}".format(type(exc).__name__, exc)
            logger.exception("Health check could not reach the database")

        return {
            "status": "ok" if database_ok else "degraded",
            "app_name": settings.app_name,
            "app_version": settings.app_version,
            "environment": settings.environment,
            "database_ok": database_ok,
            "database_error": database_error,
            "authentication": "none",
            "provider": provider_health(settings),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    @app.get(
        "/",
        tags=["system"],
        summary="What this service is and where its documentation lives",
    )
    def index() -> Dict[str, Any]:
        settings = get_settings()
        return {
            "name": settings.app_name,
            "version": settings.app_version,
            "api_prefix": API_V1_PREFIX,
            "docs": "/docs",
            "openapi": "/openapi.json",
            "health": "/health",
            "authentication": "none",
            "warning": SECURITY_WARNING.replace("**", ""),
        }


# ---- error handling
def _envelope(
    request: Request,
    exc: Exception,
    status_code: int,
    message: str,
    detail: Optional[Any] = None,
) -> JSONResponse:
    """The single error shape. Built here so no endpoint can invent its own.

    ``detail`` is passed through ``jsonable_encoder`` because a Pydantic validation error
    can carry a non-serialisable object in its context, and an error handler that itself
    raises while encoding would replace a clear 422 with an opaque 500.
    """
    body: Dict[str, Any] = {
        "error": {
            "type": type(exc).__name__,
            "message": message,
            "status_code": status_code,
            "path": request.url.path,
            "detail": jsonable_encoder(detail) if detail is not None else None,
        }
    }
    return JSONResponse(status_code=status_code, content=body)


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(NotFoundError)
    async def _not_found(request: Request, exc: NotFoundError) -> JSONResponse:
        return _envelope(request, exc, status.HTTP_404_NOT_FOUND, str(exc))

    @app.exception_handler(InvalidInputError)
    async def _invalid_input(request: Request, exc: InvalidInputError) -> JSONResponse:
        return _envelope(request, exc, status.HTTP_400_BAD_REQUEST, str(exc))

    @app.exception_handler(EvidenceTooLargeError)
    async def _too_large(request: Request, exc: EvidenceTooLargeError) -> JSONResponse:
        return _envelope(request, exc, status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(exc))

    @app.exception_handler(EvidenceIngestError)
    async def _ingest_failed(request: Request, exc: EvidenceIngestError) -> JSONResponse:
        # Nothing was stored when this is raised, so the client can safely retry.
        return _envelope(request, exc, status.HTTP_400_BAD_REQUEST, str(exc))

    @app.exception_handler(ServiceError)
    async def _service_error(request: Request, exc: ServiceError) -> JSONResponse:
        # Base class: reached only by a service failure that is neither of the two above.
        # exc_info is passed explicitly: an exception handler does not always run in
        # the frame that caught the exception, and logger.exception() would then record
        # "NoneType: None" instead of the traceback.
        logger.error("Unclassified service error on %s", request.url.path, exc_info=exc)
        return _envelope(request, exc, status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _envelope(
            request,
            exc,
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Request validation failed.",
            detail=exc.errors(),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        message = detail if isinstance(detail, str) else "Request failed."
        return _envelope(
            request,
            exc,
            exc.status_code,
            message,
            detail=None if isinstance(detail, str) else detail,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        """Last resort: log the traceback, return an envelope, reveal nothing internal.

        The message is deliberately generic - an exception string can carry a filesystem
        path or a fragment of a query - and the type name is included so a report of "it
        returned 500" can still be matched to a line in the log.
        """
        logger.error("Unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        return _envelope(
            request,
            exc,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Internal server error. The traceback was written to the server log.",
        )


app = create_app()

__all__ = ["API_V1_PREFIX", "DESCRIPTION", "SECURITY_WARNING", "TAGS_METADATA", "app", "create_app", "lifespan"]
