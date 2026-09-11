"""Navigation entry for the Audit Projects page.

Two names exist for one page, and neither could be dropped without breaking something.
``app.frontend.streamlit_app.PAGE_SPECS`` registers this path - a page module at any
other filename is silently skipped by the shell, and that file belongs to another
contributor - while the page itself was commissioned as ``audit_projects.py``. The
implementation lives there, under the name it was specified with; this module is the
two-line adapter that lets the navigation find it.

Whoever next owns ``streamlit_app.py`` can collapse the two by pointing ``PAGE_SPECS``
at ``pages/audit_projects.py`` and deleting this file.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.frontend.pages import audit_projects  # noqa: E402

if __name__ == "__main__":
    audit_projects.render()
