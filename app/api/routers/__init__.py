"""HTTP routers.

Each module here owns one resource group and does three things only: validate the
request with a model from :mod:`app.schemas.api`, delegate to
:mod:`app.audit.service` (or, for ingestion and orchestration, to
:mod:`app.evidence.service`, :mod:`app.audit.engine` and :mod:`app.audit.report`), and
serialise what comes back. There is no query, no counting and no audit judgement in this
package - the Streamlit front end calls those same service functions in-process, and any
rule implemented here instead of there would exist in one front end and not the other.

Errors are not caught locally either. ``NotFoundError``, ``InvalidInputError`` and the
evidence-ingestion errors are translated to status codes once, by the handlers
registered in :mod:`app.api.main`, so every endpoint fails in the same shape.
"""

from __future__ import annotations

__all__ = [
    "assessments",
    "controls",
    "dashboard",
    "evaluation",
    "evidence",
    "projects",
    "reports",
    "reviews",
    "settings",
]
