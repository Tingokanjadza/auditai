"""The console's vocabulary: every repeated visual element, defined once.

Ten pages show the same handful of things - a status, a risk band, an AI output that
must carry its warning, a citation that must be checkable, an AI conclusion beside the
auditor's. Each of those is a function here, taking data and rendering it, so the pages
stay about *audit workflow* rather than about markup, and so a change to how a citation
looks happens in one place.

Two conventions, both deliberate
--------------------------------
* **Badges return HTML; panels render.** ``status_badge`` and its siblings return a
  string, because a badge is almost always wanted *inside* another line - a table cell,
  a heading, a sentence. Pass the string to :func:`badges` (or to ``st.markdown(...,
  unsafe_allow_html=True)``). Everything else - cards, panels, banners, tables - writes
  to the page and returns ``None``, except :func:`empty_state`, which returns whether
  its call-to-action button was pressed.
* **Every string that came from a model, a file or a user is escaped.** These functions
  render raw HTML for layout, and evidence text is untrusted input: a quoted fragment
  from an uploaded document could otherwise close a tag. :func:`_esc` is applied to all
  of it without exception.

Nothing here reaches the database. A component that needs the surrounding chunk text
takes a ``context_loader`` callable instead, so the module stays pure and testable and
the page decides where the data comes from.
"""

from __future__ import annotations

import html
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Union

import pandas as pd
import streamlit as st

from app.frontend import theme

#: The line that must appear above every AI output in this application. It is a
#: constant rather than a literal in each page so that it cannot drift into a softer
#: wording on the page where it matters most.
AI_DISCLAIMER = "AI-generated - requires auditor review"

AI_DISCLAIMER_BODY = (
    "This assessment was produced by an automated system from the evidence supplied. "
    "It is not an audit conclusion, not assurance, and not a statement of compliance. "
    "A qualified auditor must verify every cited quotation against its source and "
    "record the final conclusion."
)

RISK_MODEL_NOTE = (
    "Risk scores come from a prototype research scoring model, not an official industry "
    "risk framework."
)

MOCK_PROVIDER_NOTE = (
    "The offline provider is a deterministic rule-based stand-in, not a language model. "
    "Results obtained with it measure this pipeline, not model quality."
)

#: Shown whenever the provider that answered is not the provider that was selected -
#: which in practice means "LLM_PROVIDER=claude, but ANTHROPIC_API_KEY is missing, so the
#: offline stand-in answered". A researcher who believes a run used Claude when it did
#: not would draw a conclusion about a language model from a rule engine, so this is the
#: one message on the screen that is allowed to shout.
PROVIDER_MISMATCH_NOTE = (
    "The provider that answered is NOT the provider that was configured. Nothing produced "
    "in this state may be reported as a result from the configured model."
)

#: Display names for the canonical provider ids in :mod:`app.llm.factory`. The console
#: says "Claude", not "claude"; an id that is not in here is shown verbatim rather than
#: guessed at, because inventing a friendly name for an unknown provider is exactly the
#: kind of helpfulness that would let a wrong provider look right.
PROVIDER_DISPLAY_NAMES: Dict[str, str] = {
    "mock": "Mock",
    "claude": "Claude",
    "anthropic": "Claude",
    "openai": "OpenAI",
}


# ---- text helpers
def escape(value: Any, default: str = "") -> str:
    """HTML-escape any value before it is interpolated into markup.

    Public because pages occasionally build a one-off snippet of their own; evidence
    text, filenames and model output are all untrusted input and must never reach
    ``st.markdown(unsafe_allow_html=True)`` unescaped.
    """
    text = "" if value is None else str(value)
    return html.escape(text) if text else html.escape(default)


#: Internal shorthand for the same function - this module interpolates constantly.
_esc = escape


def _clip(value: Any, limit: int = 320) -> str:
    text = "" if value is None else str(value).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _write(html_text: str) -> None:
    st.markdown(html_text, unsafe_allow_html=True)


def _label_of(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip()


def _slug(value: Any) -> str:
    return _label_of(value).upper().replace(" ", "_").replace("-", "_").lower().replace("_", "-")


# ---- badges (return HTML)
def _badge(kind: str, value: Any, sub: str = "", fallback: str = "-") -> str:
    label = _label_of(value) or fallback
    css = "ia-{0}-{1}".format(kind, _slug(value)) if _label_of(value) else "ia-plain"
    sub_html = ' <span class="ia-badge-sub">{0}</span>'.format(_esc(sub)) if sub else ""
    return '<span class="ia-badge {css}" title="{title}">{label}{sub}</span>'.format(
        css=css, title=_esc(label), label=_esc(label.replace("_", " ")), sub=sub_html
    )


def status_badge(status: Any) -> str:
    """Assessment outcome pill. Returns HTML - render with :func:`badges`."""
    return _badge("status", status, fallback="NOT ASSESSED")


def risk_badge(risk_level: Any, score: Optional[float] = None) -> str:
    """Risk band pill, optionally carrying the 0-100 prototype score."""
    sub = "" if score is None else "{0:.0f}".format(float(score))
    return _badge("risk", risk_level, sub=sub, fallback="NOT RATED")


def confidence_badge(confidence: Any, score: Optional[float] = None) -> str:
    sub = "" if score is None else "{0:.2f}".format(float(score))
    return _badge("conf", confidence, sub=sub, fallback="UNKNOWN")


def sufficiency_badge(sufficiency: Any) -> str:
    """How completely the evidence covers the control, as the model judged it."""
    return _badge("suff", sufficiency, fallback="NONE")


def verdict_badge(verdict: Any, match_score: Optional[float] = None) -> str:
    """Result of mechanically re-checking one citation against its stored chunk."""
    sub = "" if match_score is None else "{0:.0%}".format(float(match_score))
    return _badge("verdict", verdict, sub=sub, fallback="UNVERIFIED")


def decision_badge(decision: Any) -> str:
    return _badge("decision", decision, fallback="PENDING")


def mode_badge(mode: Any) -> str:
    """Experimental condition (A / B / C) the assessment was produced under."""
    return _badge("mode", mode, fallback="C_RAG_WORKFLOW")


def parse_badge(parse_status: Any) -> str:
    return _badge("parse", parse_status, fallback="PENDING")


def plain_badge(text: str, color: str = "") -> str:
    """An unclassified pill, for counts and one-off labels."""
    if color:
        style = 'style="color:{0};background:{1};border-color:{2};"'.format(
            color, theme.hex_to_rgba(color, 0.14), theme.hex_to_rgba(color, 0.42)
        )
        return '<span class="ia-badge" {0}>{1}</span>'.format(style, _esc(text))
    return '<span class="ia-badge ia-plain">{0}</span>'.format(_esc(text))


def badges(*items: str, align: str = "left") -> None:
    """Render a row of badge HTML strings."""
    parts = [item for item in items if item]
    if not parts:
        return
    justify = {"left": "flex-start", "right": "flex-end", "center": "center"}.get(align, "flex-start")
    _write(
        '<div class="ia-badge-row" style="justify-content:{0};">{1}</div>'.format(
            justify, "".join(parts)
        )
    )


# ---- structure
def section_header(title: str, subtitle: str = "", eyebrow: str = "") -> None:
    """A titled rule. Used instead of ``st.subheader`` so every section looks alike."""
    eyebrow_html = (
        '<div class="ia-eyebrow">{0}</div>'.format(_esc(eyebrow)) if eyebrow else ""
    )
    sub_html = '<span class="ia-section-sub">{0}</span>'.format(_esc(subtitle)) if subtitle else ""
    _write(
        '{eyebrow}<div class="ia-section"><span class="ia-section-title">{title}</span>{sub}</div>'.format(
            eyebrow=eyebrow_html, title=_esc(title), sub=sub_html
        )
    )


def metric_card(
    label: str,
    value: Any,
    delta: str = "",
    caption: str = "",
    color: str = "",
    help_text: str = "",
) -> None:
    """One figure with its label, in the console's card style.

    ``color`` paints the left edge, which is how a dashboard tile carries a status
    without needing a badge inside it.
    """
    edge = color or theme.ACCENT
    delta_html = '<div class="ia-metric-delta">{0}</div>'.format(_esc(delta)) if delta else ""
    caption_html = (
        '<div class="ia-metric-caption">{0}</div>'.format(_esc(caption)) if caption else ""
    )
    _write(
        '<div class="ia-card ia-metric" style="border-left-color:{edge};" title="{help}">'
        '<div class="ia-metric-label">{label}</div>'
        '<div class="ia-metric-value" style="color:{value_color};">{value}</div>'
        "{delta}{caption}</div>".format(
            edge=edge,
            help=_esc(help_text or label),
            label=_esc(label),
            value_color=color or theme.TEXT,
            value=_esc(value),
            delta=delta_html,
            caption=caption_html,
        )
    )


def metric_row(items: Sequence[Mapping[str, Any]], columns: int = 0) -> None:
    """A row of :func:`metric_card` tiles.

    Each item is a mapping with ``label`` and ``value`` plus the optional ``delta``,
    ``caption``, ``color`` and ``help_text`` keys :func:`metric_card` accepts.
    """
    tiles = [dict(item) for item in items if item]
    if not tiles:
        return
    count = columns if columns > 0 else len(tiles)
    for start in range(0, len(tiles), count):
        row = tiles[start : start + count]
        cols = st.columns(len(row), gap="small")
        for column, tile in zip(cols, row):
            with column:
                metric_card(
                    label=str(tile.get("label", "")),
                    value=tile.get("value", "-"),
                    delta=str(tile.get("delta", "") or ""),
                    caption=str(tile.get("caption", "") or ""),
                    color=str(tile.get("color", "") or ""),
                    help_text=str(tile.get("help_text", "") or ""),
                )


def kv_grid(pairs: Mapping[str, Any], skip_empty: bool = True) -> None:
    """A definition list. Used for provenance blocks and record headers."""
    rows: List[str] = []
    for key, value in pairs.items():
        text = "" if value is None else str(value)
        if skip_empty and not text.strip():
            continue
        rows.append("<dt>{0}</dt><dd>{1}</dd>".format(_esc(key), _esc(text)))
    if rows:
        _write('<dl class="ia-kv">{0}</dl>'.format("".join(rows)))


def note(text: str) -> None:
    """A quiet caveat. Used for definitions, limitations and model disclaimers."""
    if text:
        _write('<div class="ia-note">{0}</div>'.format(_esc(text)))


def empty_state(
    title: str,
    message: str = "",
    action_label: str = "",
    action_key: str = "",
    action_type: str = "primary",
) -> bool:
    """A dead end with a way out of it.

    Returns True on the script run where the call-to-action was clicked, so the page can
    act on it. With no ``action_label`` it is purely informational and always returns
    False.
    """
    _write(
        '<div class="ia-empty"><div class="ia-empty-title">{0}</div>'
        '<div class="ia-empty-text">{1}</div></div>'.format(_esc(title), _esc(message))
    )
    if not action_label:
        return False
    _left, middle, _right = st.columns([1, 1, 1])
    with middle:
        return bool(
            st.button(
                action_label,
                key=action_key or "empty_action_{0}".format(abs(hash(title)) % 100000),
                type=action_type,
                width="stretch",
            )
        )


# ---- the AI provenance banner
def ai_disclaimer_banner(
    detail: str = "",
    mode: Any = None,
    provider: str = "",
    model: str = "",
    compact: bool = False,
) -> None:
    """The banner that must sit above every AI output in this application.

    Named arguments are appended to the text rather than replacing it, so a caller can
    add context but cannot remove the warning.
    """
    extras: List[str] = []
    if mode:
        extras.append("Mode {0}".format(_label_of(mode)))
    if provider:
        extras.append("Provider {0}{1}".format(provider, "/" + model if model else ""))
    body = detail or ("" if compact else AI_DISCLAIMER_BODY)
    if extras:
        body = (body + "  " if body else "") + " · ".join(extras)
    _write(
        '<div class="ia-ai-banner{compact}">'
        '<span class="ia-ai-mark">{mark}</span>'
        '<span class="ia-ai-text">{body}</span></div>'.format(
            compact=" ia-compact" if compact else "",
            mark=_esc(AI_DISCLAIMER),
            body=_esc(body),
        )
    )


def provider_display_name(provider: Any) -> str:
    """``"claude"`` -> ``"Claude"``. An unrecognised id is returned as it was given."""
    key = _label_of(provider).lower()
    if not key:
        return "unknown"
    return PROVIDER_DISPLAY_NAMES.get(key, key)


def provider_label(info: Mapping[str, Any]) -> str:
    """The one line a screenshot of this console has to get right.

    It names ``active_provider`` - the provider that actually answered - and never
    ``configured_provider``. The distinction is the whole point: a ``.env`` selecting
    Claude with no key in it produces a mapping whose configured provider is Claude and
    whose active provider is the mock, and the badge must read *Mock*.

    Falls back to the pre-rendered ``label`` only when no active provider is present,
    which happens when a caller passes a mapping this component did not shape.
    """
    active = _label_of(info.get("active_provider"))
    if not active:
        return str(info.get("label") or "AI Provider: unknown")
    return "AI Provider: {0}".format(provider_display_name(active))


def provider_banner(info: Mapping[str, Any], compact: bool = False) -> None:
    """Say which model answered - loudly when the answer is "none", louder when it lies.

    Takes the mapping from ``app.frontend.data_access.provider_badge``:
    ``active_provider``, ``configured_provider``, ``active_model``, ``is_mock``,
    ``fell_back_to_mock`` and a human ``detail``.

    Three states, deliberately not three shades of the same colour:

    * a real provider answered - accent, neutral;
    * the mock answered because the mock was asked for - the AI colour and the
      rule-engine caveat, because a demonstration run must not read as a model run;
    * the mock answered because a *real provider was asked for and was unusable* - red,
      both provider names spelled out, and :data:`PROVIDER_MISMATCH_NOTE`. This is the
      case that silently invalidates an experiment, so it is the case that gets the loud
      treatment rather than a caption someone can miss.
    """
    active = _label_of(info.get("active_provider"))
    configured = _label_of(info.get("configured_provider"))
    is_mock = bool(info.get("is_mock", active.lower() == "mock"))
    # A mismatch is worth shouting about even if the fallback flag is missing, so the
    # provider names are compared directly as well.
    mismatched = bool(info.get("fell_back_to_mock")) or bool(
        configured and active and configured.lower() != active.lower()
    )
    model = _label_of(info.get("active_model"))
    detail = str(info.get("detail", "") or "")

    if mismatched:
        color = theme.RED
    elif is_mock:
        color = theme.AI_COLOR
    else:
        color = theme.ACCENT

    line = [plain_badge(provider_label(info), color)]
    if model and not is_mock:
        line.append(plain_badge(model))

    mismatch_html = ""
    if mismatched:
        mismatch_html = (
            '<div class="ia-provider-detail" style="color:{red};font-weight:700;">{headline}</div>'
            '<div class="ia-provider-detail" style="color:{red};">{note}</div>'
        ).format(
            red=theme.RED,
            headline=_esc(
                "Configured: {0} · Actually answering: {1}".format(
                    provider_display_name(configured) if configured else "unknown",
                    provider_display_name(active) if active else "unknown",
                )
            ),
            note=_esc(PROVIDER_MISMATCH_NOTE),
        )

    style = (
        ' style="border-color:{0};background:{1};"'.format(
            theme.RED, theme.hex_to_rgba(theme.RED, 0.12)
        )
        if mismatched
        else ""
    )
    _write(
        '<div class="ia-provider{mock}"{style}>'
        '<div class="ia-provider-line">{badges}</div>'
        "{mismatch}"
        '<div class="ia-provider-detail">{detail}</div></div>'.format(
            mock=" ia-mock" if is_mock else "",
            style=style,
            badges="".join(line),
            mismatch=mismatch_html,
            detail=_esc(detail if not compact else _clip(detail, 90)),
        )
    )


# ---- evidence and citations
def citation_card(
    citation: Mapping[str, Any],
    index: Optional[int] = None,
    context_loader: Optional[Callable[[int], Mapping[str, Any]]] = None,
    expanded: bool = False,
) -> None:
    """One model citation, rendered so that it can actually be checked.

    Shows the source filename, the precise locator, the verbatim quotation and the
    verdict the citation validator reached - and then offers the full stored chunk, with
    its neighbours when a ``context_loader`` is supplied, because a quoted fragment is
    only verifiable if the reviewer can see what surrounded it.

    ``context_loader`` takes a ``chunk_id`` and returns the mapping produced by
    ``app.frontend.data_access.chunk_context``. Without one, the card falls back to the
    chunk text carried on the citation row.
    """
    data = dict(citation)
    verdict = _label_of(data.get("verdict")) or "UNVERIFIED"
    edge = theme.verdict_color(verdict)
    chunk_id = data.get("chunk_id")
    resolved = bool(data.get("chunk_resolved", chunk_id is not None))

    number = "" if index is None else "[{0}] ".format(int(index))
    locator = str(data.get("locator_text") or data.get("locator") or "")
    supports = str(data.get("supports") or "")

    head = [
        '<span class="ia-cite-file">{0}{1}</span>'.format(
            _esc(number), _esc(data.get("filename") or "(no filename recorded)")
        )
    ]
    if locator:
        head.append('<span class="ia-cite-loc">{0}</span>'.format(_esc(locator)))
    head.append(verdict_badge(verdict, data.get("match_score")))
    if supports:
        head.append(plain_badge(supports))
    if not resolved:
        head.append(plain_badge("chunk not resolved", theme.RISK_COLORS["CRITICAL"]))

    quote = str(data.get("quoted_text") or "")
    quote_html = (
        '<div class="ia-quote">{0}</div>'.format(_esc(quote))
        if quote
        else '<div class="ia-quote" style="font-style:italic;">No quotation was supplied with this citation.</div>'
    )
    relevance = str(data.get("relevance") or "")
    why_html = '<div class="ia-cite-why">{0}</div>'.format(_esc(relevance)) if relevance else ""
    verification_note = str(data.get("verification_note") or "")
    note_html = (
        '<div class="ia-cite-why" style="color:{0};">{1}</div>'.format(edge, _esc(verification_note))
        if verification_note
        else ""
    )

    _write(
        '<div class="ia-cite" style="border-left-color:{edge};">'
        '<div class="ia-cite-head">{head}</div>{quote}{why}{note}</div>'.format(
            edge=edge, head="".join(head), quote=quote_html, why=why_html, note=note_html
        )
    )

    if chunk_id is None and not data.get("chunk_text"):
        return

    title = "Show the source chunk in context" if context_loader else "Show the full source chunk"
    with st.expander("{0}  -  chunk {1}".format(title, chunk_id if chunk_id is not None else "?"), expanded=expanded):
        context: Mapping[str, Any] = {}
        if context_loader is not None and chunk_id is not None:
            try:
                context = context_loader(int(chunk_id)) or {}
            except Exception as exc:  # noqa: BLE001 - a viewer must not break the page
                st.warning("Could not load the surrounding context: {0}".format(exc))
        if context.get("found"):
            _render_chunk_context(context, int(chunk_id))
        else:
            text = str(data.get("chunk_text") or "")
            if text:
                _write('<div class="ia-chunk ia-chunk-focus">{0}</div>'.format(_esc(text)))
            else:
                st.info(
                    "This citation does not resolve to a stored evidence chunk. That is "
                    "itself the finding: there is nothing in the evidence store to check it "
                    "against."
                )


def _render_chunk_context(context: Mapping[str, Any], focus_chunk_id: int) -> None:
    """The cited chunk with its neighbours, the cited one marked."""
    kv_grid(
        {
            "File": context.get("filename", ""),
            "Locator": context.get("locator_text", ""),
            "Source type": context.get("source_type", ""),
            "Chunk index": context.get("chunk_index", ""),
        }
    )
    for chunk in context.get("chunks", []) or []:
        is_focus = int(chunk.get("chunk_id", chunk.get("id", -1)) or -1) == focus_chunk_id
        marker = "cited chunk" if is_focus else "context"
        _write(
            '<div class="ia-eyebrow">{0} &middot; {1}</div>'
            '<div class="ia-chunk{focus}">{2}</div>'.format(
                _esc(marker),
                _esc(chunk.get("locator_text", "")),
                _esc(chunk.get("text", "")),
                focus=" ia-chunk-focus" if is_focus else "",
            )
        )


def evidence_provenance_panel(evidence: Mapping[str, Any], show_hash: bool = True) -> None:
    """Where this artefact came from and what the parser made of it.

    The SHA-256 is shown because it is the integrity anchor of the evidence trail: it is
    what lets a reader confirm that the file cited in a report is the file that was
    uploaded. The chunk figures are shown because "how many of these chunks are actually
    searchable" is the honest answer to whether the evidence can be cited at all.
    """
    data = dict(evidence)
    stored = int(data.get("chunks_stored", data.get("chunk_count", 0)) or 0)
    embedded = int(data.get("chunks_embedded", 0) or 0)

    section_header(
        str(data.get("filename", "(unnamed file)")),
        subtitle=str(data.get("description", "") or ""),
        eyebrow="Evidence provenance",
    )
    badges(
        parse_badge(data.get("parse_status")),
        plain_badge(_label_of(data.get("evidence_type")) or "OTHER"),
        plain_badge("synthetic", theme.AI_COLOR) if data.get("is_synthetic") else "",
        plain_badge("{0} chunks".format(stored)),
        plain_badge(
            "{0} embedded".format(embedded),
            theme.GREEN if embedded == stored and stored else theme.AMBER,
        ),
    )

    fields: Dict[str, Any] = {
        "Uploaded": data.get("uploaded_at", ""),
        "Uploaded by": data.get("uploaded_by", ""),
        "Type / extension": "{0} / {1}".format(
            _label_of(data.get("evidence_type")), data.get("extension", "")
        ),
        "Size": "{0} KB".format(data.get("size_kb", round(float(data.get("size_bytes", 0) or 0) / 1024.0, 1))),
        "Pages / rows": "{0} / {1}".format(data.get("page_count", 0), data.get("row_count", 0)),
        "Characters parsed": data.get("char_count", 0),
    }
    if show_hash:
        fields["SHA-256"] = data.get("sha256", "") or data.get("sha256_short", "")
    if data.get("sheet_names"):
        fields["Sheets"] = ", ".join(str(name) for name in data["sheet_names"])
    if data.get("chunks_by_source_type"):
        fields["Chunk kinds"] = ", ".join(
            "{0}: {1}".format(key, value) for key, value in data["chunks_by_source_type"].items()
        )
    if data.get("parse_error"):
        fields["Parse error"] = data["parse_error"]
    kv_grid(fields)

    if data.get("is_synthetic"):
        note(
            "Synthetic artefact generated for research use. It represents no real "
            "organisation, system or person."
        )


# ---- the signature views
def _quad(
    key: str,
    lead: str,
    items: Sequence[str],
    count: Optional[int] = None,
    empty: str = "Nothing recorded.",
    footer: str = "",
    extra_html: str = "",
) -> str:
    """Render one column of the four-way panel to HTML."""
    entries = [str(item) for item in items if str(item or "").strip()]
    body = (
        "<ul>{0}</ul>".format("".join("<li>{0}</li>".format(_esc(item)) for item in entries))
        if entries
        else '<div class="ia-quad-empty">{0}</div>'.format(_esc(empty))
    )
    shown = len(entries) if count is None else count
    lead_html = '<div class="ia-quad-lead">{0}</div>'.format(_esc(lead)) if lead else ""
    footer_html = '<div class="ia-quad-note">{0}</div>'.format(_esc(footer)) if footer else ""
    return (
        '<div class="ia-quad ia-quad-{key}">'
        '<div class="ia-quad-head"><span class="ia-quad-title">{title}</span>'
        '<span class="ia-quad-count">{count}</span></div>'
        "{lead}{extra}{body}{footer}</div>".format(
            key=key,
            title=_esc(theme.FOUR_WAY_TITLES[key]),
            count=shown,
            lead=lead_html,
            extra=extra_html,
            body=body,
            footer=footer_html,
        )
    )


def four_way_panel(
    assessment: Mapping[str, Any],
    four_way: Optional[Mapping[str, Any]] = None,
    context_loader: Optional[Callable[[int], Mapping[str, Any]]] = None,
    show_citations: bool = True,
) -> None:
    """The REQUIRES / PROVES / INFERS / HUMAN-VERIFIES split.

    This is the view the whole system is an argument for. An unstructured model answer
    blends four different kinds of statement into one paragraph - what the policy
    demands, what the evidence literally says, what the model concluded from it, and
    what nobody has checked - and an auditor reading that paragraph cannot tell them
    apart. Here they are four columns that cannot be confused:

    ``REQUIRES``
        Taken from the control library, not from the model, so the requirement does not
        move with the answer being judged against it.
    ``PROVES``
        Only quotations, each with the verdict of a mechanical re-check against the
        stored chunk. A fabricated citation shows up here as a failure, not as proof.
    ``INFERS``
        The model's conclusions, including the status and the risk band themselves.
        Everything in this column is unproven by construction.
    ``HUMAN VERIFIES``
        What the system states it cannot settle: the model's own verification list, the
        evidence it says is missing, and any unsupported numeric claim the validator
        caught.

    Pass ``four_way`` explicitly, or let it be read from ``assessment["four_way"]``,
    which ``data_access.get_assessment`` populates.
    """
    payload = dict(four_way or assessment.get("four_way") or {})
    if not payload:
        from app.frontend.data_access import build_four_way

        payload = build_four_way(assessment)

    requires = dict(payload.get("requires", {}))
    proves = dict(payload.get("proves", {}))
    infers = dict(payload.get("infers", {}))
    human = dict(payload.get("human", {}))

    ai_disclaimer_banner(
        "Columns 3 and 4 are machine-generated. Column 2 quotes the evidence verbatim and "
        "each quotation has been re-checked against its stored chunk; column 1 is the "
        "control library's own wording.",
        mode=assessment.get("experiment_mode"),
    )

    # ---- requires
    requirement_items: List[str] = []
    requirement_items.extend(str(item) for item in requires.get("criteria", []) or [])
    expected = [str(item) for item in requires.get("expected_evidence", []) or []]
    restatement = str(requires.get("model_restatement") or "")
    extra = ""
    if expected:
        extra = (
            '<div class="ia-eyebrow" style="margin-top:0.35rem;">Evidence the control expects</div>'
            "<ul>{0}</ul>".format("".join("<li>{0}</li>".format(_esc(item)) for item in expected))
        )

    # ---- proves
    citations = list(proves.get("citations", []) or [])
    quote_items = [
        "{0} - “{1}”".format(
            item.get("filename") or "unknown file", _clip(item.get("quoted_text"), 220)
        )
        for item in citations
    ]
    # Both rates are printed. The headline one half-credits a PARTIAL match, which is a
    # weighting choice; the strict one is verified/total and is the figure a write-up
    # should quote. Showing only the first would flatter the output.
    grounding = proves.get("grounding_rate", 0.0) or 0.0
    strict = proves.get("grounding_rate_strict", 0.0) or 0.0
    proves_footer = (
        "{0} of {1} citations verified against their chunk, {2} partial, {3} fabricated. "
        "Grounding rate {4:.0%} (verified + half of partial); {5:.0%} counting verified "
        "quotations only.".format(
            proves.get("verified", 0),
            proves.get("total", 0),
            proves.get("partial", 0),
            proves.get("fabricated", 0),
            float(grounding),
            float(strict),
        )
    )

    # ---- infers
    infer_items = [str(item) for item in infers.get("inferences", []) or []]
    if infers.get("finding"):
        infer_items.insert(0, "Finding: {0}".format(infers["finding"]))
    infers_extra = '<div class="ia-badge-row">{0}{1}{2}</div>'.format(
        status_badge(infers.get("status")),
        risk_badge(infers.get("risk_level"), infers.get("risk_score")),
        confidence_badge(infers.get("confidence")),
    )

    # ---- human verifies
    human_items = [str(item) for item in human.get("items", []) or []]
    for missing in human.get("missing_evidence", []) or []:
        human_items.append("Missing evidence: {0}".format(missing))
    for claim in human.get("unsupported_claims", []) or []:
        human_items.append("Unsupported claim detected by the validator: {0}".format(claim))

    columns = st.columns(4, gap="small")
    with columns[0]:
        _write(
            _quad(
                "requires",
                str(requires.get("objective") or ""),
                requirement_items,
                empty="No assessment criteria are recorded for this control.",
                footer=("Model restated it as: " + _clip(restatement, 240)) if restatement else "",
                extra_html=extra,
            )
        )
    with columns[1]:
        _write(
            _quad(
                "proves",
                str(proves.get("statement") or ""),
                quote_items,
                count=len(citations),
                empty="No evidence was cited. Nothing here is proven.",
                footer=proves_footer,
            )
        )
    with columns[2]:
        _write(
            _quad(
                "infers",
                str(infers.get("reasoning") or ""),
                infer_items,
                empty="No inference was recorded beyond the cited evidence.",
                footer="Everything in this column is the model's conclusion, not established fact.",
                extra_html=infers_extra,
            )
        )
    with columns[3]:
        _write(
            _quad(
                "human",
                str(human.get("recommendation") or ""),
                human_items,
                empty="The model listed nothing outstanding - which is itself worth checking.",
                footer=(
                    "An auditor has recorded a decision on this assessment."
                    if human.get("reviewed")
                    else "No auditor decision has been recorded. This is not an audit conclusion."
                ),
            )
        )

    if show_citations and citations:
        st.markdown("")
        section_header(
            "Cited evidence in full",
            subtitle="Each quotation with its locator, its verification verdict and the stored chunk it came from.",
        )
        for position, citation in enumerate(citations, start=1):
            citation_card(citation, index=position, context_loader=context_loader)


def ai_vs_human_panel(
    assessment: Mapping[str, Any],
    comparison: Optional[Mapping[str, Any]] = None,
) -> None:
    """The AI assessment beside the auditor's conclusion, with divergences marked.

    The two records are never merged. Keeping the model's answer intact next to the
    human one is what makes agreement measurable rather than overwritten, which is the
    point of the study; showing them side by side is what makes a disagreement visible
    to the next reader of the working paper.
    """
    payload = dict(comparison or assessment.get("ai_vs_human") or {})
    if not payload:
        from app.frontend.data_access import build_ai_vs_human

        payload = build_ai_vs_human(assessment)

    ai = dict(payload.get("ai", {}))
    human = payload.get("human")
    divergences = list(payload.get("divergences", []) or [])
    diverged_fields = {str(item.get("field")) for item in divergences}

    left, right = st.columns(2, gap="small")
    with left:
        _write(
            '<div class="ia-pane ia-pane-ai"><div class="ia-pane-title">{title}</div>'
            "{badges}{fields}</div>".format(
                title=_esc(ai.get("label", "AI-generated assessment")),
                badges='<div class="ia-badge-row">{0}{1}{2}</div>'.format(
                    status_badge(ai.get("status")),
                    risk_badge(ai.get("risk_level")),
                    confidence_badge(ai.get("confidence")),
                ),
                fields=_fields_html(
                    [
                        ("Finding", ai.get("finding", ""), "finding" in diverged_fields),
                        ("Recommendation", ai.get("recommendation", ""), "recommendation" in diverged_fields),
                    ]
                ),
            )
        )
    with right:
        if not human:
            _write(
                '<div class="ia-pane ia-pane-human"><div class="ia-pane-title">Final auditor assessment</div>'
                '<div class="ia-quad-empty">No auditor decision has been recorded. Until one is, '
                "this control has no audit conclusion - only a machine-generated proposal.</div></div>"
            )
        else:
            _write(
                '<div class="ia-pane ia-pane-human"><div class="ia-pane-title">{title}</div>'
                "{badges}{fields}</div>".format(
                    title=_esc(human.get("label", "Final auditor assessment")),
                    badges='<div class="ia-badge-row">{0}{1}{2}</div>'.format(
                        status_badge(human.get("status")),
                        risk_badge(human.get("risk_level")),
                        decision_badge(human.get("decision")),
                    ),
                    fields=_fields_html(
                        [
                            ("Finding", human.get("finding", ""), "finding" in diverged_fields),
                            (
                                "Recommendation",
                                human.get("recommendation", ""),
                                "recommendation" in diverged_fields,
                            ),
                            ("Reviewer", human.get("reviewer_name", ""), False),
                            ("Comments", human.get("comments", ""), False),
                        ]
                    ),
                )
            )

    if human:
        if divergences:
            items = "".join(
                "<li><strong>{0}</strong>: AI said {1} → auditor recorded {2}</li>".format(
                    _esc(item.get("label", item.get("field", ""))),
                    _esc(_clip(item.get("ai"), 160) or "(nothing)"),
                    _esc(_clip(item.get("human"), 160) or "(nothing)"),
                )
                for item in divergences
            )
            _write(
                '<div class="ia-note" style="border-left-color:{0};color:{1};">'
                "The auditor departed from the AI output in {2} field(s)."
                '<ul class="ia-diverge-list">{3}</ul></div>'.format(
                    theme.AI_COLOR, theme.TEXT_MUTED, len(divergences), items
                )
            )
        else:
            note("The auditor recorded the same conclusion as the AI on every compared field.")
        if human.get("flagged_hallucination"):
            st.error(
                "The reviewer flagged this output as containing a fabrication or an "
                "unsupported claim."
            )


def _fields_html(fields: Sequence[Any]) -> str:
    """``[(label, value, diverged), ...]`` -> the stacked field markup used by the panes."""
    parts: List[str] = []
    for label, value, diverged in fields:
        text = str(value or "").strip()
        if not text:
            continue
        parts.append(
            '<div class="ia-field{cls}"><div class="ia-field-label">{label}</div>'
            '<div class="ia-field-value">{value}</div></div>'.format(
                cls=" ia-diverged" if diverged else "", label=_esc(label), value=_esc(text)
            )
        )
    return "".join(parts)


# ---- tables
def df_table(
    rows: Union[Sequence[Mapping[str, Any]], "pd.DataFrame"],
    columns: Optional[Sequence[str]] = None,
    column_config: Optional[Mapping[str, Any]] = None,
    height: Optional[int] = None,
    key: Optional[str] = None,
    empty_message: str = "Nothing to show.",
    hide_index: bool = True,
) -> Optional["pd.DataFrame"]:
    """Render a list of dictionaries as a dataframe with consistent column handling.

    ``columns`` both selects and orders; a name that is not present is created empty
    rather than raising, so a page can list the columns it wants without first checking
    which of them a given query produced. Returns the frame that was drawn (or ``None``
    when there was nothing), so a caller can reuse it for a download button.
    """
    frame = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(list(rows or []))
    if frame.empty:
        st.caption(empty_message)
        return None
    if columns:
        wanted = list(columns)
        for name in wanted:
            if name not in frame.columns:
                frame[name] = None
        frame = frame[wanted]

    config: Dict[str, Any] = dict(_DEFAULT_COLUMN_CONFIG)
    config.update(dict(column_config or {}))
    config = {name: spec for name, spec in config.items() if name in frame.columns}

    # ``height`` is only forwarded when set: Streamlit 1.50 rejects an explicit None,
    # and "let the table size itself" is the sane default for an unspecified height.
    extra: Dict[str, Any] = {"height": int(height)} if height else {}
    st.dataframe(
        frame,
        width="stretch",
        hide_index=hide_index,
        column_config=config or None,
        key=key,
        **extra
    )
    return frame


#: Column presentation that should look the same on every page that shows these fields.
_DEFAULT_COLUMN_CONFIG: Dict[str, Any] = {
    "id": st.column_config.NumberColumn("ID", width="small", format="%d"),
    "control_id": st.column_config.TextColumn("Control", width="small"),
    "control_ref": st.column_config.TextColumn("Control", width="small"),
    "control_name": st.column_config.TextColumn("Control name", width="medium"),
    "name": st.column_config.TextColumn("Name", width="medium"),
    "status": st.column_config.TextColumn("AI status", width="medium"),
    "final_status": st.column_config.TextColumn("Auditor status", width="medium"),
    "risk_level": st.column_config.TextColumn("Risk", width="small"),
    "risk_score": st.column_config.ProgressColumn(
        "Risk score", min_value=0, max_value=100, format="%.0f", width="small"
    ),
    "confidence": st.column_config.TextColumn("Confidence", width="small"),
    "evidence_sufficiency": st.column_config.TextColumn("Sufficiency", width="small"),
    "citation_count": st.column_config.NumberColumn("Citations", width="small", format="%d"),
    "verified_citations": st.column_config.NumberColumn("Verified", width="small", format="%d"),
    "fabricated_citations": st.column_config.NumberColumn("Fabricated", width="small", format="%d"),
    "grounding_rate": st.column_config.ProgressColumn(
        "Grounding", min_value=0.0, max_value=1.0, format="%.0f%%", width="small"
    ),
    "latency_ms": st.column_config.NumberColumn("Latency (ms)", width="small", format="%d"),
    "created_at": st.column_config.DatetimeColumn("Created", width="medium", format="YYYY-MM-DD HH:mm"),
    "uploaded_at": st.column_config.DatetimeColumn("Uploaded", width="medium", format="YYYY-MM-DD HH:mm"),
    "generated_at": st.column_config.DatetimeColumn("Generated", width="medium", format="YYYY-MM-DD HH:mm"),
    "filename": st.column_config.TextColumn("File", width="large"),
    "size_kb": st.column_config.NumberColumn("Size (KB)", width="small", format="%.1f"),
    "chunk_count": st.column_config.NumberColumn("Chunks", width="small", format="%d"),
    "parse_status": st.column_config.TextColumn("Parsed", width="small"),
    "evidence_type": st.column_config.TextColumn("Evidence type", width="medium"),
    "is_reviewed": st.column_config.CheckboxColumn("Reviewed", width="small"),
    "decision": st.column_config.TextColumn("Decision", width="medium"),
    "mode_label": st.column_config.TextColumn("Experiment mode", width="medium"),
    "experiment_mode": st.column_config.TextColumn("Mode", width="small"),
    "category": st.column_config.TextColumn("Category", width="medium"),
    "audit_area": st.column_config.TextColumn("Audit area", width="medium"),
}


def download_row(
    label: str,
    data: Union[str, bytes],
    file_name: str,
    mime: str = "text/plain",
    key: Optional[str] = None,
) -> None:
    """A download button in the console's button style."""
    st.download_button(label, data=data, file_name=file_name, mime=mime, key=key)


__all__ = [
    "AI_DISCLAIMER",
    "AI_DISCLAIMER_BODY",
    "MOCK_PROVIDER_NOTE",
    "PROVIDER_DISPLAY_NAMES",
    "PROVIDER_MISMATCH_NOTE",
    "RISK_MODEL_NOTE",
    "ai_disclaimer_banner",
    "ai_vs_human_panel",
    "badges",
    "citation_card",
    "confidence_badge",
    "decision_badge",
    "df_table",
    "download_row",
    "empty_state",
    "escape",
    "evidence_provenance_panel",
    "four_way_panel",
    "kv_grid",
    "metric_card",
    "metric_row",
    "mode_badge",
    "note",
    "parse_badge",
    "plain_badge",
    "provider_banner",
    "provider_display_name",
    "provider_label",
    "risk_badge",
    "section_header",
    "status_badge",
    "sufficiency_badge",
    "verdict_badge",
]
