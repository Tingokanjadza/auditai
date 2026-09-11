"""Page modules for the audit console.

Each module here is executed as a script by ``st.Page`` (see
``app.frontend.streamlit_app.PAGE_SPECS``), so the filenames are part of the navigation
contract: a module at any other name is skipped and its entry never appears in the
sidebar.

Dashboard, Audit Projects, Controls and Evidence define ``render()`` and call it under
``if __name__ == "__main__":``. Streamlit execs a page script into a module named
``__main__``, so the guard runs the page in the application while leaving the module
importable - by a test, or by ``projects.py``, which is the navigation entry that
delegates to ``audit_projects.py``.

The package deliberately holds no shared helper code. Pages reach the rest of the
application only through :mod:`app.frontend.data_access`, render through
:mod:`app.frontend.components`, and keep selections in :mod:`app.frontend.state`; a
utility layer here would become a fourth place to look for behaviour that already has a
home.
"""

from __future__ import annotations
