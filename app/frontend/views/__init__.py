"""Page modules for the audit console.

Each module here is executed as a script by ``st.Page`` (see
``app.frontend.streamlit_app.PAGE_SPECS``), so the filenames are part of the navigation
contract: every ``PAGE_SPECS`` entry is registered unconditionally, and ``st.Page``
raises at startup when the file it names does not exist.

Each module defines ``render()`` and calls it under ``if __name__ == "__main__":``.
Streamlit execs a page script into a module named ``__main__``, so the guard runs the
page in the application while leaving the module importable by a test.

The directory is called ``views`` rather than ``pages`` on purpose: a ``pages/`` folder
next to the entry script triggers Streamlit's legacy multipage auto-discovery, which
routes a deep link such as ``/settings`` straight to the page file before
``st.navigation`` has run - no sidebar, no theme, no project selector.

The package deliberately holds no shared helper code. Pages reach the rest of the
application only through :mod:`app.frontend.data_access`, render through
:mod:`app.frontend.components`, and keep selections in :mod:`app.frontend.state`; a
utility layer here would become a fourth place to look for behaviour that already has a
home.
"""

from __future__ import annotations
