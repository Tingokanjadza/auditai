"""Settings: which AI answers, where data lives, and the demo data actions.

Three tabs, in the order an auditor needs them:

* **AI provider** - what ``.env`` asked for, what is actually answering, and the exact
  lines to paste to switch Claude (or an OpenAI-compatible endpoint) on. The
  configured-versus-active distinction is the one thing this page must never blur: a
  ``.env`` that selects Claude without a key produces a console whose *configured*
  provider is Claude and whose *active* provider is the offline stand-in, and that state
  is shown in red on every tab.
* **Demo data & reset** - load the demo audit, restore the published control
  definitions, and the gated reset.
* **For the record** - the research parameters a recorded result depends on (retrieval,
  chunking, thresholds, versions, storage), each with a one-line definition, plus the
  redacted settings dump. A screenshot of that tab is a complete statement of the
  conditions a result was produced under.

**Secrets are shown as fingerprints or as nothing at all, never as values.**
``app.config.Settings`` masks the OpenAI-side keys before they reach any display path;
``ANTHROPIC_API_KEY`` is reported as present or absent only; the database connection
string is masked again here in case it carries a password; and every remaining field
whose name looks like a credential is reduced to presence before the raw dump renders it.

The page is read-only about configuration. It comes from the environment and the
``.env`` file, and a settings screen that wrote to them would let a browser session
silently change the conditions a recorded result was produced under. What to edit, and
where, is spelled out instead.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import streamlit as st

from app.frontend import components, data_access, state

#: What has to be typed before the destructive action runs. A checkbox is too easy to
#: hit by accident for something that deletes every audit project's evidence and findings.
_RESET_PHRASE = "DELETE ALL DATA"

#: The minimal lines to paste to turn Claude on. Kept minimal on purpose: the instruction
#: on the page is "paste these three lines", not "find the right lines in a long block".
_ENV_CLAUDE = """LLM_PROVIDER=claude
ANTHROPIC_API_KEY=sk-ant-your-key-here
ANTHROPIC_MODEL=claude-opus-5
"""

#: The optional Claude tuning lines, shown separately so they cannot be mistaken for
#: required ones.
_ENV_CLAUDE_OPTIONAL = """# Optional - defaults shown; omit any line you do not want to change.
ANTHROPIC_EFFORT=high          # low | medium | high | xhigh | max
ANTHROPIC_THINKING=true        # adaptive thinking; the model sizes its own reasoning
# Only for an Anthropic-compatible gateway or proxy. Leave unset for the real API.
# ANTHROPIC_BASE_URL=https://your-gateway.example/v1
"""

#: The OpenAI-compatible variant: OpenAI itself, or any endpoint that speaks its API
#: (Azure-compatible gateways, Ollama, vLLM, LM Studio, OpenRouter, ...).
_ENV_OPENAI = """LLM_PROVIDER=openai
LLM_MODEL=gpt-4o-mini
LLM_API_KEY=sk-your-key-here
LLM_BASE_URL=https://api.openai.com/v1     # or any OpenAI-compatible endpoint
"""

#: ``ANTHROPIC_BASE_URL`` is a conventional variable that developer tooling exports, and
#: pydantic-settings reads the process environment as well as ``.env``. An inherited value
#: silently sends every piece of audit evidence to a different endpoint than the one the
#: ``.env`` names, which is worth a sentence in plain language rather than a footnote.
_BASE_URL_NOTE = (
    "The base URL can arrive from your shell as well as from .env. Configuration is read "
    "from the process environment first, so if ANTHROPIC_BASE_URL (or ANTHROPIC_API_KEY) "
    "is exported in your shell profile, or by another tool that set it for this terminal, "
    "that value wins over the file. If the effective base URL shown on this page is not "
    "the one your .env names, check `env | grep ANTHROPIC` in the terminal you started "
    "the application from - something in your shell set it."
)

#: What is not available when the console talks to the API over HTTP.
_NOT_FROM_API = "not available from the API"


def _mask_dsn(url: str) -> str:
    """Hide any password inside a connection string before it is displayed.

    ``Settings.redacted_dict`` masks the API keys but returns ``database_url`` verbatim,
    which is right for a local SQLite path and wrong the moment someone points the
    prototype at a PostgreSQL instance with credentials in the URL.
    """
    text = str(url or "")
    return re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:***@", text)


#: A settings field is treated as a secret when one of its underscore-separated words is
#: one of these. Whole words rather than substrings, because ``llm_max_tokens`` contains
#: "token" and is a perfectly ordinary number: masking it would hide a parameter the
#: reproducibility record needs. Singular only, for the same reason - "tokens" is a count,
#: "token" is a credential.
_SECRET_NAME_WORDS = frozenset(
    {"key", "secret", "token", "password", "passwd", "credential", "credentials"}
)

#: Values that mean "nothing is configured". ``Settings.redacted_dict`` has already
#: replaced an unset key with the *string* ``"(not set)"`` by the time it reaches this
#: page, and a non-empty string is truthy - so a plain truth test here would report an
#: absent key as present, which is the one thing this panel must never do.
_ABSENT_MARKERS = ("", "(not set)", "none", "null")


def _is_configured(value: Any) -> bool:
    """Whether a (possibly already-masked) secret field holds anything at all."""
    if value is None:
        return False
    return str(value).strip().lower() not in _ABSENT_MARKERS


def _looks_like_a_secret(field_name: Any) -> bool:
    """Whether a settings field name names a credential rather than a parameter."""
    words = str(field_name).lower().replace("-", "_").split("_")
    return any(word in _SECRET_NAME_WORDS for word in words)


def _redact_secrets(values: Dict[str, Any]) -> Dict[str, Any]:
    """Second line of defence over whatever the configuration layer handed us.

    ``Settings.redacted_dict`` masks the fields it knows about, which is the right place
    for it. This is the belt to that pair of braces: the dump renders *every* setting, so
    a credential field added to ``Settings`` later - and not added to the masking list -
    would appear on screen in full the day it was introduced. Matching on the field name
    instead means the failure mode of a new secret is "shown as present", not "shown".
    """
    out: Dict[str, Any] = {}
    for key, value in values.items():
        if _looks_like_a_secret(key):
            out[key] = "(configured)" if _is_configured(value) else "(not set)"
        else:
            out[key] = value
    return out


def _yes_no(value: Any) -> str:
    return "yes" if bool(value) else "no"


def _prompt_metadata() -> Dict[str, Any]:
    """Prompt version and fingerprint, when this process holds the prompts itself.

    ``app.audit.prompts`` is a pure constants module - no database, no network - but it
    is still local state: over HTTP the prompts belong to the API process, and reading
    this process's copy would report a version that did not produce the results. Empty
    in API mode, and the page says why.
    """
    if data_access.use_api():
        return {}
    try:
        from app.audit.prompts import prompt_metadata

        payload = dict(prompt_metadata())
    except Exception:  # noqa: BLE001 - a missing constant must not break the page
        return {}
    return {
        "prompt_version": payload.get("prompt_version", ""),
        "fingerprint": payload.get("fingerprint", ""),
    }


def _provider_details(health: Dict[str, Any]) -> Dict[str, Any]:
    """The full :func:`app.llm.factory.provider_health` payload, however this console runs.

    In API mode it is already inside the ``/health`` body the facade fetched, so it is
    read from there rather than computed locally: over HTTP the provider that matters is
    the API process's, and this process's configuration would describe a provider that
    never sees an assessment. In-process the same function is called directly - it makes
    no network call and touches no database, which is what makes it safe on a page that
    re-renders on every keystroke.
    """
    if data_access.use_api():
        detail = dict(health.get("detail") or {})
        return dict(detail.get("provider") or {})
    try:
        from app.llm.factory import provider_health

        return dict(provider_health())
    except Exception:  # noqa: BLE001 - the panel degrades rather than breaking the page
        return {}


# ---- the status block at the top of every tab
def _render_status_block(provider: Dict[str, Any], details: Dict[str, Any]) -> None:
    """The provider banner plus, when a real provider was asked for and the offline
    stand-in answered instead, the red alert with what to do about it."""
    if not provider:
        return
    components.provider_banner(provider, plain=True)
    if provider.get("fell_back_to_mock"):
        configured = provider.get("configured_provider") or ""
        configured_name = (
            components.provider_display_name(configured) if configured else "A remote provider"
        )
        if str(configured).lower() in ("claude", "anthropic"):
            remedy = (
                "To fix it: add ANTHROPIC_API_KEY to .env (the 'Switch on Claude' box on the "
                "AI provider tab has the exact lines), make sure the anthropic package is "
                "installed, then restart the application."
            )
        else:
            remedy = (
                "To fix it: set LLM_API_KEY (and LLM_BASE_URL for a non-OpenAI endpoint) in "
                ".env, then restart the application."
            )
        parts = [
            "{0} was configured but is not usable, so **{1}** answered instead.".format(
                configured_name,
                components.provider_display_name(provider.get("active_provider")),
            ),
            components.PROVIDER_MISMATCH_NOTE,
            str(details.get("fallback_reason", "") or "").strip(),
            remedy,
        ]
        st.error(" ".join(part for part in parts if part), icon=":material/error:")


# ---- tab 1: AI provider
def _provider_rows(details: Dict[str, Any], summary_provider: Dict[str, Any]) -> Dict[str, Any]:
    """The grid rows: the three verbatim lines, then only the rows that belong to the
    configured provider. Nothing here is a secret: keys appear as present/absent or as
    the fingerprint ``Settings`` already masked."""
    configured = str(details.get("configured_provider", "") or "")
    active = str(details.get("active_provider", "") or "")
    per_provider = dict(details.get("providers") or {})
    rows: Dict[str, Any] = {
        "Configured provider (what .env asked for)": components.provider_display_name(configured),
        "Active provider (what answers)": components.provider_display_name(active),
        "Active model": details.get("active_model", "") or "(provider default)",
    }
    key = configured.lower()
    if key in ("claude", "anthropic"):
        claude = dict(per_provider.get("claude") or {})
        rows.update(
            {
                "Credentials present": _yes_no(claude.get("api_key_configured")),
                "Anthropic SDK installed": "{0} ({1})".format(
                    _yes_no(claude.get("sdk_installed")), claude.get("sdk_version", "unknown")
                ),
                "Claude model (ANTHROPIC_MODEL)": claude.get("model", ""),
                "Reasoning effort (ANTHROPIC_EFFORT)": claude.get("effort", ""),
                "Adaptive thinking (ANTHROPIC_THINKING)": "on" if claude.get("adaptive_thinking") else "off",
                "Effective base URL": claude.get("base_url", "") or "(Anthropic API)",
            }
        )
    elif key == "openai":
        rows.update(
            {
                "Credentials present": _yes_no(summary_provider.get("llm_configured")),
                "Model (LLM_MODEL)": summary_provider.get("llm_model", ""),
                "Base URL (LLM_BASE_URL)": summary_provider.get("llm_base_url", ""),
                "API key fingerprint": summary_provider.get("llm_api_key", ""),
            }
        )
    return rows


def _render_provider_tab(
    summary: Dict[str, Any],
    provider: Dict[str, Any],
    health: Dict[str, Any],
    details: Dict[str, Any],
) -> None:
    summary_provider = dict(summary.get("provider") or {})
    components.section_header(
        "Which AI answers",
        subtitle="The provider named in .env, and the one that actually answers your assessments.",
    )
    if details:
        components.kv_grid(_provider_rows(details, summary_provider), skip_empty=False)
    else:
        components.note(
            "The provider detail is not exposed by the API this console is pointed at. "
            "Read it from the API process: GET /api/v1/settings/providers."
        )
    if provider.get("is_mock") and not provider.get("fell_back_to_mock"):
        st.info(
            "Demo mode is the offline default. It reads your evidence with fixed rules and "
            "produces real citations, so every screen works without a key or a network - but "
            "no language model is involved. Switch on Claude below when you are ready.",
            icon=":material/info:",
        )

    components.section_header(
        "Where this console runs", subtitle="How the pages reach the audit database."
    )
    backend_rows: Dict[str, Any] = {
        "Backend": data_access.backend_label(),
        "Backend reachable": _yes_no(health.get("reachable", True)),
    }
    detail = dict(health.get("detail") or {})
    if "database_ok" in detail:
        backend_rows["Database reachable"] = _yes_no(detail.get("database_ok"))
        if detail.get("database_error"):
            backend_rows["Database error"] = str(detail.get("database_error"))
    backend_rows["Application"] = "{0} v{1}".format(
        summary.get("app_short_name") or summary.get("app_name", ""), summary.get("app_version", "")
    )
    backend_rows["Environment"] = "{0}{1}".format(
        summary.get("environment", ""), " (debug)" if summary.get("debug") else ""
    )
    available = provider.get("available_providers") or []
    if available:
        backend_rows["Providers available in this build"] = ", ".join(
            components.provider_display_name(item) for item in available
        )
    components.kv_grid(backend_rows)

    with st.expander("Switch on Claude", expanded=bool(provider.get("fell_back_to_mock"))):
        st.markdown(
            "1. Open the `.env` file in the project root (copy `.env.example` if it does "
            "not exist yet).\n"
            "2. Paste these lines and replace the key with your own:"
        )
        st.code(_ENV_CLAUDE, language="bash")
        st.markdown(
            "3. Restart the application (stop it, then run `python run.py` again). "
            "Settings are read once at startup, so a running process keeps the "
            "configuration it started with.\n"
            "4. Come back to this page. The banner at the top should read **Claude**. If it "
            "still reads Demo mode, the key is missing or wrong: the application does not "
            "refuse to run, it falls back and says so here in red."
        )
        st.caption(
            "The anthropic package must be installed for LLM_PROVIDER=claude to do anything. "
            "It is in requirements.txt, so `pip install -r requirements.txt` covers it."
        )
        with st.popover("Optional Claude settings"):
            st.code(_ENV_CLAUDE_OPTIONAL, language="bash")
            st.caption(
                "Claude is not called with a temperature. Reasoning depth is set by the "
                "effort level instead, so two runs at the same effort are comparable but "
                "neither is bit-for-bit reproducible the way an offline run is."
            )
        components.note(_BASE_URL_NOTE)

        st.markdown("**Using an OpenAI-compatible endpoint instead**")
        st.code(_ENV_OPENAI, language="bash")
        st.caption(
            "Works for OpenAI itself and for any endpoint that speaks its API - a gateway, "
            "Ollama, vLLM, LM Studio, OpenRouter. Restart the application afterwards, the "
            "same as for Claude."
        )
        components.note(
            "Nothing on this page writes to .env. Keys are read from the environment and are "
            "never written to the database, into a report, or into an evaluation run's "
            "configuration; ANTHROPIC_API_KEY is reported here as present or absent only."
        )


# ---- tab 2: demo data and reset
def _render_demo_tab() -> None:
    components.section_header(
        "Demo audit",
        subtitle="Everything the demo creates is synthetic. No real system, account or person is in it.",
    )
    st.caption(
        "Creates the demo audit project, puts five controls in scope and loads the "
        "generated policies, exports and listings as evidence. Safe to press twice: a file "
        "already loaded under the same name is skipped. Nothing is assessed until you press "
        "Run assessment on the Assessments page."
    )
    components.load_demo_control(
        key="settings_load_demo",
        label="Load the demo audit",
        help_text="Loads the synthetic evidence and opens the Assessments page.",
    )

    st.markdown("")
    components.section_header(
        "Control library",
        subtitle="The published control definitions that ship with the application.",
    )
    st.caption(
        "Rewrites every library control from the published definitions in "
        "data/controls/control_library.json. This **overwrites any edits you made to "
        "library controls** on the Controls page; controls you added yourself are kept, "
        "and no audit project, evidence or assessment is touched."
    )
    confirm_restore = st.checkbox(
        "I understand this overwrites my edits to library controls",
        key="settings_restore_confirm",
    )
    if st.button(
        "Restore the published control definitions",
        key="settings_restore_controls",
        disabled=not confirm_restore,
    ):
        _restore_controls()

    st.markdown("")
    components.section_header(
        "Reset", subtitle="Deletes every audit project. There is no undo and no backup."
    )
    st.caption(
        "Deletes every audit project and, with it, its evidence records and uploaded "
        "files, chunks, AI assessments, citations, auditor decisions, reports and report "
        "files on disk, then re-creates the control library and an empty demo audit "
        "project. Recorded evaluation runs keep their metrics, but the assessments behind "
        "them are gone, so their prompts and responses will no longer be inspectable."
    )
    with st.form("settings_reset", clear_on_submit=True):
        typed = st.text_input(
            "Type {0} to confirm".format(_RESET_PHRASE),
            key="settings_reset_phrase",
            placeholder=_RESET_PHRASE,
        )
        submitted = st.form_submit_button("Delete all audit data")
    if submitted:
        if str(typed or "").strip() == _RESET_PHRASE:
            _reset()
        else:
            st.error(
                "The phrase did not match, so nothing was deleted. Type {0} exactly, then "
                "press the button again.".format(_RESET_PHRASE)
            )


def _restore_controls() -> None:
    with st.spinner("Restoring the control library..."):
        try:
            result = dict(data_access.bootstrap_database(overwrite_controls=True))
        except data_access.DataAccessError as exc:
            components.error_with_remedy("The control library could not be restored.", exc)
            return
    if result.get("skipped"):
        state.flash(
            "Nothing was restored: {0}".format(
                result.get("reason", "the API process owns the control library.")
            ),
            "info",
        )
    else:
        state.flash(
            "Restored the control library: {0} control(s) rewritten from the published "
            "definitions ({1} in the library now).".format(
                int(result.get("controls_written", 0) or 0),
                int(result.get("controls_total", 0) or 0),
            ),
            "success",
        )
    st.rerun()


def _reset() -> None:
    """Delete every project through the facade, then re-seed the control library.

    The confirmation says only what actually happened: how many projects were deleted,
    and whether the library and demo project were re-created (they are not when the
    console talks to the API, whose process seeds them itself at startup).
    """
    try:
        projects = data_access.list_projects()
    except data_access.DataAccessError as exc:
        components.error_with_remedy("Could not read the audit projects, so nothing was deleted.", exc)
        return

    deleted = 0
    failures: List[str] = []
    bootstrap: Dict[str, Any] = {}
    with st.spinner("Deleting audit data..."):
        for project in projects:
            try:
                if data_access.delete_project(int(project["id"])):
                    deleted += 1
            except data_access.DataAccessError as exc:
                failures.append(
                    "Could not delete {0}: {1}".format(project.get("name", project.get("id")), exc)
                )
        try:
            bootstrap = dict(data_access.bootstrap_database())
        except data_access.DataAccessError as exc:
            failures.append("Could not re-create the control library: {0}".format(exc))

    state.set_current_assessment(None)
    state.set_current_report(None)
    state.set_current_run(None)
    recreated = bool(bootstrap) and not bootstrap.get("skipped") and bootstrap.get("demo_project_id") is not None
    if recreated:
        state.request_project_switch(int(bootstrap["demo_project_id"]))
        state.flash(
            "Deleted {0} audit project(s). The control library and demo project were "
            "re-created.".format(deleted),
            "success",
        )
    elif bootstrap.get("skipped"):
        state.flash(
            "Deleted {0} audit project(s). {1}".format(
                deleted, bootstrap.get("reason", "The API process re-creates the control library.")
            ),
            "success" if not failures else "warning",
        )
    else:
        state.flash("Deleted {0} audit project(s).".format(deleted), "success" if not failures else "warning")
    for message in failures:
        state.flash(message, "error")
    st.rerun()


# ---- tab 3: for the record
def _param_rows(rows: List[Any]) -> None:
    """A three-column table: parameter, value, one-line definition."""
    components.df_table(
        [
            {"Parameter": name, "Value": "" if value is None else str(value), "What it means": meaning}
            for name, value, meaning in rows
        ],
        columns=["Parameter", "Value", "What it means"],
        empty_message="No parameters to show.",
    )


def _render_record_tab(summary: Dict[str, Any]) -> None:
    provider = dict(summary.get("provider") or {})
    limits = dict(summary.get("limits") or {})
    redacted = dict(summary.get("redacted_settings") or {})
    prompts = _prompt_metadata()

    def local(key: str) -> Any:
        return redacted.get(key, _NOT_FROM_API)

    components.section_header(
        "Retrieval and chunking",
        subtitle="What the AI is shown, and how the evidence was cut up before it got there.",
    )
    _param_rows(
        [
            ("Retrieval strategy", provider.get("retrieval_strategy", ""),
             "How passages are found: keyword search, vector similarity, or both (hybrid)."),
            ("top_k", provider.get("retrieval_top_k", ""),
             "How many evidence passages the AI is shown for one control."),
            ("Candidate pool (candidate_k)", local("retrieval_candidate_k"),
             "How many passages are scored before the top_k are chosen."),
            ("Minimum retrieval score", local("retrieval_min_score"),
             "Passages scoring below this are never shown, even inside top_k."),
            ("Chunk size (characters)", local("chunk_size"),
             "How long each stored passage is."),
            ("Chunk overlap (characters)", local("chunk_overlap"),
             "How much neighbouring passages share, so a sentence is not cut in half."),
            ("Table rows per chunk", local("table_rows_per_chunk"),
             "How many spreadsheet or CSV rows make one passage."),
            ("Evidence budget per prompt", limits.get("max_evidence_chars", ""),
             "The most evidence text, in characters, handed to the AI in one call."),
            ("Raw evidence budget (condition A)", local("raw_evidence_char_budget"),
             "Same ceiling for the no-retrieval research condition, which gets raw evidence."),
            ("Embedding provider", provider.get("embedding_provider", ""),
             "What turns passages into vectors: local hashing (offline) or a hosted model."),
            ("Embedding model", provider.get("embedding_model", ""),
             "The vectoriser's name; 'hashing-vectorizer' means no network and no model."),
            ("Embedding dimensions", local("embedding_dim"),
             "The length of each vector."),
        ]
    )

    components.section_header(
        "Assessment guarantees and versions",
        subtitle="The thresholds and versions a recorded result depends on.",
    )
    _param_rows(
        [
            ("Human review", "always required (not configurable)",
             "Every AI assessment waits for an auditor's decision; the AI and human records never merge."),
            ("Citation match threshold", summary.get("citation_match_threshold", ""),
             "A quotation counts as verified at or above this similarity to its stored passage."),
            ("Prompt version", prompts.get("prompt_version", "") or "not exposed by the API - read it from the API process",
             "The version of the instructions the AI is given."),
            ("Prompt fingerprint", prompts.get("fingerprint", "") or "-",
             "A hash of the prompt text, so two runs can be shown to have used the same words."),
            ("Application version", summary.get("app_version", ""),
             "The code version that produced the result."),
            ("Demo-mode fabrication rate", provider.get("mock_hallucination_rate", ""),
             "Research lever: above zero the offline rules deliberately fabricate a citation at this rate so the validator can be tested. 0.0 means never."),
            ("Demo-mode seed", local("mock_seed"),
             "Fixes the offline rules' randomness so a run repeats exactly."),
        ]
    )
    components.note(
        "Changing the citation match threshold changes every grounding figure this system "
        "reports, so one result set is only comparable with another at the same threshold. "
        "A fabrication rate of zero in an offline run is a property of the stand-in, not a "
        "finding about grounding."
    )

    components.section_header("Storage and limits", subtitle="Where data lives and what will be accepted.")
    storage_rows: List[Any] = []
    if redacted:
        storage_rows.extend(
            [
                ("Database", _mask_dsn(redacted.get("database_url", "")),
                 "The audit database; any password in the connection string is masked."),
                ("Upload directory", redacted.get("upload_dir", ""),
                 "Where uploaded evidence files are stored."),
                ("Report directory", redacted.get("report_dir", ""),
                 "Where generated report files are written."),
                ("Synthetic data directory", redacted.get("synthetic_dir", ""),
                 "Where the demo and research datasets are generated."),
                ("Evaluation output directory", redacted.get("evaluation_output_dir", ""),
                 "Where research run exports are written."),
            ]
        )
    else:
        storage_rows.append(
            ("Database backend", summary.get("database_backend", ""),
             "The database engine; paths and the connection string are not disclosed over HTTP.")
        )
    storage_rows.extend(
        [
            ("Maximum upload size", "{0} MB".format(limits.get("max_upload_mb", "")),
             "Larger evidence files are refused."),
            ("Accepted file types", ", ".join(limits.get("supported_upload_extensions", []) or []),
             "Everything else is stored but marked unsupported."),
            ("API request timeout", "{0}s".format(limits.get("api_request_timeout_seconds", "")),
             "How long the console waits for the backend before giving up."),
        ]
    )
    _param_rows(storage_rows)
    if not redacted:
        components.note(
            "Filesystem paths and the connection string are not disclosed over HTTP by "
            "design, so they are shown only when the console runs in-process."
        )

    st.warning(summary.get("security_note", ""))

    with st.expander("Every setting this process holds, as a redacted dump", expanded=False):
        if not redacted:
            st.caption(
                "Not available over HTTP: the API deliberately does not disclose its "
                "filesystem paths or connection string."
            )
        else:
            safe = _redact_secrets(redacted)
            safe["database_url"] = _mask_dsn(safe.get("database_url", ""))
            st.code(data_access.to_json(safe), language="json")
    components.note(
        "Secrets never appear on this page. API keys arrive already masked by "
        "app.config.Settings.redacted_dict, and every field whose name looks like a "
        "credential is reduced to '(configured)' or '(not set)' before the dump renders. "
        "No key value is reachable from this screen."
    )


# ---- page
def render() -> None:
    components.section_header(
        "Settings",
        subtitle=(
            "Which AI answers, where data lives, and the demo data actions. Nothing here "
            "writes to your configuration."
        ),
        eyebrow="Setup",
    )
    try:
        summary = data_access.settings_summary()
    except data_access.DataAccessError as exc:
        components.error_with_remedy(
            "Could not read the configuration.", exc, retry_key="settings_retry_summary"
        )
        return

    provider: Dict[str, Any] = {}
    health: Dict[str, Any] = {}
    status_error: Optional[data_access.DataAccessError] = None
    try:
        provider = dict(data_access.provider_badge())
        health = dict(data_access.health())
    except data_access.DataAccessError as exc:
        status_error = exc
    details = _provider_details(health) if not status_error else {}

    tab_provider, tab_demo, tab_record = st.tabs(["AI provider", "Demo data & reset", "For the record"])

    with tab_provider:
        if status_error is not None:
            components.error_with_remedy("Could not read the provider status.", status_error)
        _render_status_block(provider, details)
        _render_provider_tab(summary, provider, health, details)

    with tab_demo:
        _render_status_block(provider, details)
        _render_demo_tab()

    with tab_record:
        _render_status_block(provider, details)
        _render_record_tab(summary)


if __name__ == "__main__":
    render()
