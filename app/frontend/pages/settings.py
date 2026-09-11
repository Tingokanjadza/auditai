"""Read-only configuration, in plain language, plus the data-management actions.

Two things make this page worth writing carefully rather than dumping a settings object
onto the screen.

**A number in a thesis is only reproducible if the configuration that produced it is
recorded.** Which provider answered, which retrieval strategy ran, at what ``top_k``,
with which chunk size, against which prompt version, and at what citation-match
threshold: those are the parameters of the experiment, not preferences. They are shown
here together so a screenshot of this page is a complete statement of the conditions.

**Secrets are shown as fingerprints or as nothing at all, never as values.**
``app.config.Settings`` masks the OpenAI-side keys before they reach any display path;
``ANTHROPIC_API_KEY`` is reported as present or absent only, because "yes, a key is set"
is the entire question this page exists to answer about it; the database connection
string is masked again here in case it carries a password; and every remaining field
whose name looks like a credential is reduced to presence before the raw dump renders it.
Nothing on this page can be copied and used to authenticate as anybody.

The page is deliberately read-only. Configuration comes from the environment and the
``.env`` file, and a settings screen that wrote to them would let a browser session
silently change the conditions a recorded result was produced under. What to edit, and
where, is spelled out instead.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

import streamlit as st

from app.frontend import components, data_access, state

#: What has to be typed before the destructive action runs. A checkbox is too easy to
#: hit by accident for something that deletes an engagement's evidence and findings.
_RESET_PHRASE = "DELETE ALL DATA"

_ENV_EXAMPLE = """# .env - copy from .env.example, then restart the application.

# Offline default: a deterministic rule-based stand-in, not a language model.
LLM_PROVIDER=mock

# To use Claude instead - see the Claude block below for the optional settings.
# LLM_PROVIDER=claude
# ANTHROPIC_API_KEY=sk-ant-your-key-here

# To use a hosted OpenAI-compatible model instead:
# LLM_PROVIDER=openai
# LLM_MODEL=gpt-4o-mini
# LLM_API_KEY=sk-your-key-here
# LLM_BASE_URL=https://api.openai.com/v1     # or any OpenAI-compatible endpoint

# Retrieval and chunking - changing these changes what the model is shown.
RETRIEVAL_STRATEGY=HYBRID
RETRIEVAL_TOP_K=12
CHUNK_SIZE=1200
CHUNK_OVERLAP=180

# A citation counts as verified at or above this similarity to its stored chunk.
CITATION_MATCH_THRESHOLD=0.6
"""

#: The exact lines to paste to turn Claude on. Kept separate from the full example so
#: that the instruction on the page is "paste this", not "find the right two lines in
#: that block and uncomment them".
_ENV_CLAUDE = """# --- Enable Claude -------------------------------------------------------
# Paste into .env, replace the key, then restart the application.
LLM_PROVIDER=claude
ANTHROPIC_API_KEY=sk-ant-your-key-here

# Optional. Defaults shown - omit any line you do not want to change.
ANTHROPIC_MODEL=claude-opus-5
ANTHROPIC_EFFORT=high          # low | medium | high | xhigh | max
ANTHROPIC_THINKING=true        # adaptive thinking; the model sizes its own reasoning

# Only for an Anthropic-compatible gateway or proxy. Leave it unset for the real API -
# and read the note under this block before you set it.
# ANTHROPIC_BASE_URL=https://your-gateway.example/v1
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
    for it. This is the belt to that pair of braces: the dump below renders *every*
    setting, so a credential field added to ``Settings`` later - and not added to the
    masking list - would appear on screen in full the day it was introduced. Matching on
    the field name instead means the failure mode of a new secret is "shown as present",
    not "shown".

    It reduces already-fingerprinted values to presence as well, which loses nothing: the
    fingerprint that lets an operator tell one key from another is still rendered above,
    from ``Settings.provider_summary``, where it is deliberate rather than incidental.
    """
    out: Dict[str, Any] = {}
    for key, value in values.items():
        if _looks_like_a_secret(key):
            out[key] = "(configured)" if _is_configured(value) else "(not set)"
        else:
            out[key] = value
    return out


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


def _render_provider_panel(details: Dict[str, Any]) -> None:
    """Everything about the model that a reproducibility claim depends on.

    Nothing here is a secret. The API key appears only as the boolean "is one
    configured" - not the value, not a prefix, not a length - because this page is
    screenshotted into a thesis appendix, and a fingerprint is a fingerprint even when it
    is mostly asterisks. The Claude-specific rows (effort, adaptive thinking, base URL)
    are shown only when Claude is the configured or the active provider, so the panel
    stays a statement about *this* run rather than a catalogue of every provider the
    build can construct.
    """
    if not details:
        components.note(
            "The provider detail is not exposed by the API this console is pointed at. "
            "Read it from the API process: GET /api/v1/settings/providers."
        )
        return

    configured = str(details.get("configured_provider", "") or "")
    active = str(details.get("active_provider", "") or "")
    per_provider = dict(details.get("providers") or {})
    claude = dict(per_provider.get("claude") or {})
    is_claude = "claude" in (configured.lower(), active.lower())

    rows: Dict[str, Any] = {
        "Configured provider (what .env asked for)": components.provider_display_name(configured),
        "Active provider (what answers)": components.provider_display_name(active),
        "Active model": details.get("active_model", "") or "(provider default)",
    }
    if is_claude:
        rows.update(
            {
                "ANTHROPIC_API_KEY configured": "yes" if claude.get("api_key_configured") else "no",
                "Anthropic SDK installed": "{0} ({1})".format(
                    "yes" if claude.get("sdk_installed") else "no",
                    claude.get("sdk_version", "unknown"),
                ),
                "Claude model": claude.get("model", ""),
                "Reasoning effort (ANTHROPIC_EFFORT)": claude.get("effort", ""),
                "Adaptive thinking (ANTHROPIC_THINKING)": "on"
                if claude.get("adaptive_thinking")
                else "off",
                "Effective base URL": claude.get("base_url", ""),
            }
        )
    components.kv_grid(rows, skip_empty=False)

    if is_claude:
        components.note(_BASE_URL_NOTE)
        components.note(
            "Claude is not called with a temperature. Current Claude models reject "
            "sampling parameters outright, so reasoning depth is set by the effort level "
            "above instead - which means two runs at the same effort are comparable, but "
            "neither is bit-for-bit reproducible the way an offline run is."
        )
        if not claude.get("api_key_configured"):
            st.info(
                "No ANTHROPIC_API_KEY is configured, so Claude cannot be called. The "
                "instructions further down this page say exactly what to add to .env.",
                icon=":material/key_off:",
            )


def _render_status(summary: Dict[str, Any]) -> None:
    try:
        provider = data_access.provider_badge()
        health = data_access.health()
    except data_access.DataAccessError as exc:
        st.error("Could not read the provider status: {0}".format(exc))
        return

    details = _provider_details(health)

    components.section_header(
        "What is answering right now",
        subtitle="The configuration a result produced today would have been produced under.",
    )
    components.provider_banner(provider)
    if provider.get("is_mock"):
        st.warning(
            "The offline provider is a **deterministic rule-based stand-in**, not a "
            "language model. It reads the retrieved evidence with hand-written rules and "
            "emits real citations, which is what makes the pipeline demonstrable without "
            "a network - but every accuracy figure obtained with it measures this "
            "pipeline, not model quality, and no result from it says anything about what "
            "a language model can do."
        )
    if provider.get("fell_back_to_mock"):
        st.error(
            "{0} was configured but is not usable, so **{1}** answered instead. {2} {3}".format(
                components.provider_display_name(provider.get("configured_provider"))
                if provider.get("configured_provider")
                else "A remote provider",
                components.provider_display_name(provider.get("active_provider")),
                components.PROVIDER_MISMATCH_NOTE,
                details.get("fallback_reason", ""),
            ).strip(),
            icon=":material/error:",
        )

    _render_provider_panel(details)

    components.kv_grid(
        {
            "Backend": data_access.backend_label(),
            "Backend reachable": health.get("reachable", True),
            "Application": "{0} v{1}".format(
                summary.get("app_name", ""), summary.get("app_version", "")
            ),
            "Environment": "{0}{1}".format(
                summary.get("environment", ""), " (debug)" if summary.get("debug") else ""
            ),
            "Providers available in this build": ", ".join(
                provider.get("available_providers", []) or []
            ),
        }
    )


def _render_configuration(summary: Dict[str, Any]) -> None:
    provider = dict(summary.get("provider") or {})
    limits = dict(summary.get("limits") or {})
    redacted = dict(summary.get("redacted_settings") or {})
    prompts = _prompt_metadata()

    components.section_header(
        "Model and embeddings",
        subtitle="Secrets are shown as fingerprints. The key itself is never rendered.",
    )
    components.kv_grid(
        {
            "LLM provider": provider.get("llm_provider", ""),
            "LLM model": provider.get("llm_model", ""),
            "LLM base URL": provider.get("llm_base_url", ""),
            "LLM API key": provider.get("llm_api_key", ""),
            "Provider configured": provider.get("llm_configured", ""),
            "Embedding provider": provider.get("embedding_provider", ""),
            "Embedding model": provider.get("embedding_model", ""),
            "Embedding dimensions": redacted.get("embedding_dim", "not available from the API"),
            "Mock hallucination rate": provider.get("mock_hallucination_rate", ""),
            "Mock seed": redacted.get("mock_seed", "not available from the API"),
        }
    )
    components.note(
        "The mock hallucination rate is a research lever: above zero the offline provider "
        "deliberately injects a fabricated citation or an unsupported number at that rate, "
        "so the citation validator can be exercised. At the default 0.0 it never does, "
        "which is why a fabrication rate of zero in an offline run is a property of the "
        "stand-in rather than a finding about grounding."
    )

    components.section_header(
        "Retrieval and chunking",
        subtitle="What the model is shown, and how the evidence was cut up before it got there.",
    )
    components.kv_grid(
        {
            "Retrieval strategy": provider.get("retrieval_strategy", ""),
            "top_k (chunks shown)": provider.get("retrieval_top_k", ""),
            "Candidate pool (candidate_k)": redacted.get("retrieval_candidate_k", "not available from the API"),
            "Minimum retrieval score": redacted.get("retrieval_min_score", "not available from the API"),
            "Chunk size (characters)": redacted.get("chunk_size", "not available from the API"),
            "Chunk overlap (characters)": redacted.get("chunk_overlap", "not available from the API"),
            "Table rows per chunk": redacted.get("table_rows_per_chunk", "not available from the API"),
            "Evidence budget per prompt": limits.get("max_evidence_chars", ""),
            "Raw evidence budget (condition A)": redacted.get(
                "raw_evidence_char_budget", "not available from the API"
            ),
        }
    )

    components.section_header(
        "Assessment guarantees and versions",
        subtitle="The thresholds and versions a recorded result depends on.",
    )
    components.kv_grid(
        {
            "Citation match threshold": summary.get("citation_match_threshold", ""),
            "Human review always required": summary.get("force_human_review", ""),
            "Prompt version": prompts.get("prompt_version", "")
            or "not exposed by the API - read it from the API process",
            "Prompt fingerprint": prompts.get("fingerprint", "") or "-",
            "Application version": summary.get("app_version", ""),
            "Default auditor name": summary.get("default_auditor_name", ""),
        }
    )
    components.note(
        "The citation match threshold decides what counts as a VERIFIED quotation. "
        "Changing it changes every grounding figure this system reports, so a result set "
        "is only comparable with another at the same threshold."
    )

    components.section_header("Storage and limits", subtitle="Where data lives and what will be accepted.")
    if redacted:
        components.kv_grid(
            {
                "Database": _mask_dsn(redacted.get("database_url", "")),
                "Upload directory": redacted.get("upload_dir", ""),
                "Report directory": redacted.get("report_dir", ""),
                "Synthetic data directory": redacted.get("synthetic_dir", ""),
                "Evaluation output directory": redacted.get("evaluation_output_dir", ""),
                "Maximum upload size": "{0} MB".format(limits.get("max_upload_mb", "")),
                "Accepted file types": ", ".join(limits.get("supported_upload_extensions", []) or []),
                "API request timeout": "{0}s".format(limits.get("api_request_timeout_seconds", "")),
            }
        )
    else:
        components.kv_grid(
            {
                "Database backend": summary.get("database_backend", ""),
                "Maximum upload size": "{0} MB".format(limits.get("max_upload_mb", "")),
                "Accepted file types": ", ".join(limits.get("supported_upload_extensions", []) or []),
                "API request timeout": "{0}s".format(limits.get("api_request_timeout_seconds", "")),
            }
        )
        components.note(
            "Filesystem paths and the connection string are not disclosed over HTTP by "
            "design, so they are shown only when the console runs in-process."
        )

    st.warning(summary.get("security_note", ""))


def _render_how_to_switch() -> None:
    components.section_header(
        "Changing the configuration",
        subtitle="Everything above is read from the environment. Nothing on this page writes to it.",
    )
    st.markdown(
        "1. Edit the `.env` file in the project root (copy `.env.example` if it does not "
        "exist yet).\n"
        "2. Set `LLM_PROVIDER` to `mock` for the offline stand-in, to `claude` for the "
        "Anthropic API, or to `openai` for any OpenAI-compatible endpoint, and set the "
        "matching key alongside it: `ANTHROPIC_API_KEY` for Claude, or `LLM_MODEL`, "
        "`LLM_API_KEY` and - for a non-OpenAI endpoint - `LLM_BASE_URL` for the rest.\n"
        "3. Restart the application. Settings are read once at startup, so a running "
        "process keeps the configuration it started with.\n"
        "4. Check the badge in the sidebar. If the key is missing or wrong the "
        "application starts anyway, falls back to the offline stand-in and says so - it "
        "does not refuse to run, so the badge is how you confirm the switch worked.\n"
        "5. Re-run any experiment whose numbers you intend to quote. Results produced "
        "under different settings are not comparable, and nothing recomputes them."
    )

    st.markdown("**Turning Claude on**")
    st.code(_ENV_CLAUDE, language="bash")
    components.note(_BASE_URL_NOTE)
    st.caption(
        "The `anthropic` package must be installed for `LLM_PROVIDER=claude` to do "
        "anything - it is in `requirements.txt`, so `pip install -r requirements.txt` "
        "covers it. Without either the package or the key the application falls back to "
        "the offline stand-in rather than failing to start."
    )

    st.markdown("**The rest of the file**")
    st.code(_ENV_EXAMPLE, language="bash")
    components.note(
        "The API key is read from the environment and is never written to the database, "
        "into a report, or into an evaluation run's configuration blob. `ANTHROPIC_API_KEY` "
        "is reported on this page as present or absent only - not even as a masked "
        "fingerprint - and it never appears in a log line, an error message or an API "
        "response."
    )
    if st.button("Re-read the environment now", key="settings_reload"):
        try:
            data_access.refresh_settings()
        except data_access.DataAccessError as exc:
            st.error("Could not re-read the configuration: {0}".format(exc))
            return
        state.flash(
            "Configuration re-read. A provider that was already constructed keeps "
            "answering until the process restarts.",
            "info",
        )
        st.rerun()


def _render_data_management() -> None:
    components.section_header(
        "Data management",
        subtitle="Every dataset and every demonstration file this application creates is synthetic.",
    )

    demo_col, gen_col = st.columns(2, gap="medium")
    with demo_col:
        st.markdown("**Demonstration engagement**")
        st.caption(
            "Creates the demo project, scopes five controls and ingests the generated "
            "synthetic policies, exports and listings. Safe to run twice: a file already "
            "ingested under the same name is skipped."
        )
        if st.button("Seed the demo project", key="settings_seed_demo", type="primary"):
            _seed_demo()

    with gen_col:
        st.markdown("**Synthetic evaluation datasets**")
        st.caption(
            "Writes the six evaluation datasets to the synthetic data directory. "
            "Deterministic - the same seed produces the same bytes - and nothing is "
            "ingested into an audit project by this action."
        )
        module = _datasets_module()
        if st.button(
            "Generate the dataset files",
            key="settings_gen_datasets",
            disabled=module is None,
        ):
            _generate_datasets(module)
        if module is None:
            st.caption(
                "Available only when the console runs in-process: the API exposes no "
                "endpoint that writes files to the server's filesystem."
            )

    st.markdown("")
    st.markdown("**Reset the database**")
    st.caption(
        "Deletes every audit project and, by cascade, its evidence records, chunks, "
        "assessments, citations, human reviews and reports, then re-creates the control "
        "library and the demonstration project. It does **not** delete the uploaded files "
        "under the upload directory, the generated synthetic files, or the recorded "
        "evaluation runs - those keep their metrics, but the assessments behind them are "
        "gone, so their prompts and responses will no longer be inspectable."
    )
    typed = st.text_input(
        "Type {0} to enable the reset".format(_RESET_PHRASE),
        key="settings_reset_phrase",
        placeholder=_RESET_PHRASE,
    )
    armed = typed.strip() == _RESET_PHRASE
    if st.button(
        "Delete all audit data",
        key="settings_reset",
        disabled=not armed,
        help="Irreversible. There is no undo and no backup." if armed else "Type the phrase first.",
    ):
        _reset()


def _datasets_module() -> Any:
    """The dataset generator, when this process can reach it (see the Evaluation page)."""
    if data_access.use_api():
        return None
    try:
        from app.evaluation import datasets as module

        return module
    except Exception:  # noqa: BLE001
        return None


def _seed_demo() -> None:
    with st.spinner("Generating and ingesting synthetic evidence…"):
        try:
            summary = data_access.load_demo_project()
        except data_access.DataAccessError as exc:
            st.error("The demo project could not be seeded: {0}".format(exc))
            return
    state.set_current_project(summary.get("project_id"))
    state.flash(
        "Demo project ready: {0}. {1} file(s) ingested, {2} already present.".format(
            summary.get("project_name", ""),
            len(summary.get("ingested", []) or []),
            len(summary.get("skipped", []) or []),
        ),
        "success",
    )
    for failure in summary.get("failures", []) or []:
        state.flash(
            "Could not ingest {0}: {1}".format(failure.get("filename"), failure.get("error")),
            "warning",
        )
    st.rerun()


def _generate_datasets(module: Any) -> None:
    if module is None:
        st.error("The dataset generator is not available in this build.")
        return
    with st.spinner("Writing the synthetic datasets…"):
        try:
            written = module.generate_all()
        except Exception as exc:  # noqa: BLE001 - the page reports it rather than crashing
            st.error("The datasets could not be generated: {0}".format(exc))
            return
    total = sum(len(paths) for paths in written.values())
    state.flash(
        "Wrote {0} file(s) across {1} dataset(s). All of it is fabricated research "
        "data.".format(total, len(written)),
        "success",
    )
    st.rerun()


def _reset() -> None:
    """Delete every project through the facade, then re-seed the control library."""
    try:
        projects = data_access.list_projects()
    except data_access.DataAccessError as exc:
        st.error("Could not read the projects: {0}".format(exc))
        return

    deleted = 0
    failures: List[str] = []
    with st.spinner("Deleting audit data…"):
        for project in projects:
            try:
                if data_access.delete_project(int(project["id"])):
                    deleted += 1
            except data_access.DataAccessError as exc:
                failures.append("{0}: {1}".format(project.get("name", project.get("id")), exc))
        try:
            data_access.bootstrap_database()
        except data_access.DataAccessError as exc:
            failures.append("re-seeding the control library: {0}".format(exc))

    state.set_current_project(None)
    state.set_current_assessment(None)
    state.set_current_report(None)
    state.set_current_run(None)
    state.flash(
        "Deleted {0} project(s) and re-created the control library and demo project.".format(deleted),
        "success",
    )
    for message in failures:
        state.flash(message, "error")
    st.rerun()


def render() -> None:
    components.section_header(
        "Settings",
        subtitle="Read-only configuration, and the data actions that set up or clear a demonstration.",
        eyebrow="Research",
    )
    try:
        summary = data_access.settings_summary()
    except data_access.DataAccessError as exc:
        st.error("Could not read the configuration: {0}".format(exc))
        return

    _render_status(summary)
    st.markdown("---")
    _render_configuration(summary)
    st.markdown("---")
    _render_how_to_switch()
    st.markdown("---")
    _render_data_management()

    with st.expander("Every setting this process holds, as a redacted dump", expanded=False):
        redacted = dict(summary.get("redacted_settings") or {})
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
            "API keys arrive here already masked by app.config.Settings.redacted_dict, "
            "and this page reduces every field whose name looks like a credential to "
            "'(configured)' or '(not set)' before rendering it. No key value is reachable "
            "from this screen."
        )


render()
