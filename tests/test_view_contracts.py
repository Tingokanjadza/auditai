"""Static contracts between the Streamlit shell and its view modules.

Why these are tests and not a checklist
----------------------------------------
The shell (``app/frontend/streamlit_app.py``) registers every entry of ``PAGE_SPECS``
unconditionally, and ``st.Page`` raises at startup when a file is missing, so a renamed
or dropped view breaks the whole console rather than one page. Likewise every
``st.page_link`` / ``st.switch_page`` target must be a registered page or the link is
dead at runtime. Neither failure is caught by importing a module, so the contracts are
pinned here where a rename shows up as a failing test instead of a blank browser tab.

The shell runs ``main()`` at import time (that is how Streamlit executes an entry
script), so its ``PAGE_SPECS`` literal is read with :mod:`ast` rather than imported.
"""
from __future__ import annotations

import ast
import importlib
import inspect
import re
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from app.frontend import state

FRONTEND_DIR = Path(__file__).resolve().parents[1] / "app" / "frontend"
VIEWS_DIR = FRONTEND_DIR / "views"
SHELL = FRONTEND_DIR / "streamlit_app.py"

#: Widget-key prefix each view must use. Project-scoped prefixes are popped by
#: :func:`app.frontend.state.clear_project_scoped_keys` on a project switch; the two
#: pages that stand outside any one audit project keep their own namespace.
KEY_PREFIXES: Dict[str, Tuple[str, ...]] = {
    "home": ("home_",),
    "audit_projects": ("projects_",),
    "controls": ("controls_",),
    "evidence": ("evidence_",),
    "assessments": ("assess_",),
    "human_review": ("review_",),
    "findings": ("find_",),
    "reports": ("report_",),
    "how_it_works": (),
    "evaluation": ("eval_",),
    "settings": ("settings_",),
}

#: Documentation placeholders that look like page paths but are not links.
PATH_EXEMPTIONS = frozenset({"views/x.py"})

_PAGE_PATH = re.compile(r'"(views/[a-z_]+\.py)"')
_WIDGET_KEY = re.compile(r'\bkey\s*=\s*f?"([A-Za-z0-9_]+)')


def _page_specs() -> List[Tuple[str, str, str, str, str]]:
    tree = ast.parse(SHELL.read_text(encoding="utf-8"))
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "PAGE_SPECS":
                return list(ast.literal_eval(node.value))
    raise AssertionError("PAGE_SPECS literal not found in streamlit_app.py")


PAGE_SPECS = _page_specs()
REGISTERED_PATHS = frozenset(spec[1] for spec in PAGE_SPECS)
VIEW_MODULES = sorted(p.stem for p in VIEWS_DIR.glob("*.py") if p.stem != "__init__")


def test_page_specs_have_the_documented_shape_and_unique_paths():
    assert PAGE_SPECS, "the shell registers no pages"
    for spec in PAGE_SPECS:
        assert len(spec) == 5, spec
        assert all(isinstance(part, str) for part in spec), spec
    paths = [spec[1] for spec in PAGE_SPECS]
    assert len(paths) == len(set(paths)), "a page is registered twice"
    url_paths = [spec[4] for spec in PAGE_SPECS]
    assert len(url_paths) == len(set(url_paths)), "two pages share a url path"
    # st.Page accepts url_path="" only for the default page, and there must be one.
    assert url_paths.count("") == 1
    assert PAGE_SPECS[0][1] == "views/home.py"


def test_every_registered_page_is_a_view_module_on_disk():
    for _, path, _, _, _ in PAGE_SPECS:
        assert path.startswith("views/"), path
        assert (FRONTEND_DIR / path).is_file(), f"{path} is registered but missing"
    registered_stems = {Path(spec[1]).stem for spec in PAGE_SPECS}
    assert registered_stems == set(VIEW_MODULES), (
        "views on disk and PAGE_SPECS disagree: "
        f"unregistered={set(VIEW_MODULES) - registered_stems}, "
        f"missing={registered_stems - set(VIEW_MODULES)}"
    )


@pytest.mark.parametrize("name", VIEW_MODULES)
def test_view_module_imports_defines_render_and_guards_main(name):
    module = importlib.import_module(f"app.frontend.views.{name}")
    assert callable(getattr(module, "render", None)), f"{name} has no render()"
    source = (VIEWS_DIR / f"{name}.py").read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in source, f"{name} lacks the __main__ guard"
    assert "from __future__ import annotations" in source, name


def test_every_page_link_target_is_registered():
    offenders = []
    for path in FRONTEND_DIR.rglob("*.py"):
        for target in _PAGE_PATH.findall(path.read_text(encoding="utf-8")):
            if target in PATH_EXEMPTIONS or target in REGISTERED_PATHS:
                continue
            offenders.append((path.relative_to(FRONTEND_DIR).as_posix(), target))
    assert not offenders, f"links to unregistered pages: {offenders}"


def test_no_frontend_file_references_the_legacy_pages_directory():
    for path in FRONTEND_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert '"pages/' not in text and "'pages/" not in text, path


@pytest.mark.parametrize("name", VIEW_MODULES)
def test_widget_keys_use_the_view_prefix(name):
    prefixes = KEY_PREFIXES[name]
    keys = _WIDGET_KEY.findall((VIEWS_DIR / f"{name}.py").read_text(encoding="utf-8"))
    if not prefixes:
        assert not keys, f"{name} declares widget keys but owns no prefix: {keys}"
        return
    bad = [k for k in keys if not k.startswith(prefixes)]
    assert not bad, f"{name} widget keys outside {prefixes}: {bad}"


def test_project_scoped_prefixes_cover_the_audit_pages():
    scoped = set(state.PROJECT_SCOPED_KEY_PREFIXES)
    for name in ("home", "audit_projects", "controls", "evidence", "assessments",
                 "human_review", "findings", "reports"):
        for prefix in KEY_PREFIXES[name]:
            assert prefix in scoped, f"{name} uses {prefix!r}, not reset on project switch"


def test_home_can_open_the_new_audit_dialog():
    from app.frontend.views import audit_projects

    dialog = audit_projects.open_new_audit_dialog
    assert callable(dialog)
    # st.dialog wraps the function and keeps the original reachable.
    assert hasattr(dialog, "__wrapped__"), "open_new_audit_dialog is not an st.dialog"
    assert not inspect.signature(dialog).parameters, "home calls it with no arguments"


def test_render_assessment_signature_matches_its_callers():
    from app.frontend import assessment_view

    params = inspect.signature(assessment_view.render_assessment).parameters
    assert list(params) == ["detail", "reviewer", "show_review_form", "key_prefix"]
    assert params["reviewer"].default == ""
    assert params["show_review_form"].default is True
    assert params["key_prefix"].default == "assess"


def test_ui_strings_say_audit_project_not_engagement():
    for path in FRONTEND_DIR.rglob("*.py"):
        assert "engagement" not in path.read_text(encoding="utf-8").lower(), path
