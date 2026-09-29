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
import inspect
import re
from datetime import timezone, datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

import pandas as pd
import streamlit as st

from app.frontend import theme
from app.schemas.enums import (
    AssessmentStatus,
    CitationVerdict,
    ConfidenceLevel,
    EvidenceSufficiency,
    EvidenceType,
    ExperimentMode,
    HumanDecision,
    ParseStatus,
    ProjectStatus,
    RiskLevel,
)

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
#: Must agree with ``app.frontend.data_access.PROVIDER_DISPLAY_NAMES`` (the facade keeps
#: its own copy so it never imports the widgets); ``tests/test_frontend_contracts.py``
#: checks the two tables stay identical. The mock's name says what it is - offline
#: rules - so the badge and the mismatch alert cannot present it as a model.
PROVIDER_DISPLAY_NAMES: Dict[str, str] = {
    "mock": "Demo mode (offline rules)",
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


def _token(value: Any) -> str:
    """Normalise an enum member, a raw string or ``None`` to the ``UPPER_SNAKE`` token."""
    return _label_of(value).upper().replace(" ", "_").replace("-", "_")


# ---- friendly labels: the single source of the words an auditor reads
#: Every enum token this console ever shows, mapped to the phrase an auditor reads in its
#: place. Raw tokens such as ``POTENTIAL_DEFICIENCY`` or ``C_RAG_WORKFLOW`` are kept for
#: tooltips, filters and exports; they are never the primary text of a badge or a
#: heading. One dictionary, so a wording change happens in one place and so a test can
#: assert that every member of every enum has an entry.
LABELS: Dict[str, Dict[str, str]] = {
    "status": {
        AssessmentStatus.EFFECTIVE.value: "Effective",
        AssessmentStatus.POTENTIAL_DEFICIENCY.value: "Potential deficiency",
        AssessmentStatus.INSUFFICIENT_EVIDENCE.value: "Insufficient evidence",
        AssessmentStatus.NOT_EFFECTIVE.value: "Not effective",
        AssessmentStatus.NOT_APPLICABLE.value: "Not applicable",
        "NOT_ASSESSED": "Not assessed",
    },
    "risk": {
        RiskLevel.LOW.value: "Low",
        RiskLevel.MEDIUM.value: "Medium",
        RiskLevel.HIGH.value: "High",
        RiskLevel.CRITICAL.value: "Critical",
        RiskLevel.NOT_RATED.value: "Not rated",
    },
    "decision": {
        HumanDecision.PENDING.value: "Awaiting decision",
        HumanDecision.ACCEPTED.value: "Accepted",
        HumanDecision.MODIFIED.value: "Modified",
        HumanDecision.REJECTED.value: "Rejected",
        HumanDecision.MORE_EVIDENCE_REQUESTED.value: "More evidence requested",
    },
    "sufficiency": {
        EvidenceSufficiency.SUFFICIENT.value: "Sufficient",
        EvidenceSufficiency.PARTIAL.value: "Partly sufficient",
        EvidenceSufficiency.INSUFFICIENT.value: "Not sufficient",
        EvidenceSufficiency.NONE.value: "No evidence",
    },
    "verdict": {
        CitationVerdict.VERIFIED.value: "Verified quote",
        CitationVerdict.PARTIAL.value: "Partly matched",
        CitationVerdict.UNVERIFIED.value: "Not found in evidence",
        CitationVerdict.FABRICATED.value: "Fabricated - not in the evidence",
    },
    "mode": {
        ExperimentMode.A_RAW_LLM.value: "Mode A - raw model (research baseline)",
        ExperimentMode.B_RAG.value: "Mode B - retrieval only (research)",
        ExperimentMode.C_RAG_WORKFLOW.value: "Full audit workflow (recommended)",
        "UNKNOWN": "Mode unknown",
    },
    "project_status": {
        ProjectStatus.PLANNING.value: "Planning",
        ProjectStatus.FIELDWORK.value: "Fieldwork",
        ProjectStatus.REVIEW.value: "In review",
        ProjectStatus.COMPLETED.value: "Completed",
        ProjectStatus.ARCHIVED.value: "Archived",
    },
    "evidence_type": {
        EvidenceType.POLICY.value: "Policy",
        EvidenceType.STANDARD.value: "Standard",
        EvidenceType.CONFIGURATION_EXPORT.value: "Configuration export",
        EvidenceType.SYSTEM_REPORT.value: "System report",
        EvidenceType.USER_LISTING.value: "User listing",
        EvidenceType.TICKET_EXPORT.value: "Ticket export",
        EvidenceType.LOG_EXTRACT.value: "Log extract",
        EvidenceType.SCREENSHOT_NARRATIVE.value: "Screenshot narrative",
        EvidenceType.INTERVIEW_NOTES.value: "Interview notes",
        EvidenceType.OTHER.value: "Other",
    },
    "parse": {
        ParseStatus.PENDING.value: "Not parsed yet",
        ParseStatus.PARSED.value: "Parsed",
        ParseStatus.FAILED.value: "Parsing failed",
        ParseStatus.UNSUPPORTED.value: "Unsupported file type",
    },
    "confidence": {
        ConfidenceLevel.LOW.value: "Low confidence",
        ConfidenceLevel.MEDIUM.value: "Medium confidence",
        ConfidenceLevel.HIGH.value: "High confidence",
        "UNKNOWN": "Confidence unknown",
    },
}


def label(kind: str, value: Any) -> str:
    """The phrase an auditor reads for an enum token.

    ``kind`` is one of the keys of :data:`LABELS`; ``value`` may be an enum member, a raw
    string in any casing, or ``None``. A token with no entry is returned title-cased with
    its underscores turned into spaces rather than raised on, so an unexpected value from
    a newer backend degrades to something readable instead of taking the page down. An
    empty value returns "".
    """
    token = _token(value)
    if not token:
        return ""
    table = LABELS.get(kind, {})
    if token in table:
        return table[token]
    return token.replace("_", " ").title()


def when(value: Any) -> str:
    """``"2026-09-18T14:03:22+00:00"`` -> ``"18 Sep 2026 14:03"``; "" for empty.

    Accepts an ISO string, a ``datetime`` or a pandas timestamp. Anything that cannot be
    parsed is returned as the text it came in as, trimmed, so a table never shows a
    blank where a stored value exists.
    """
    if value is None:
        return ""
    if isinstance(value, float) and value != value:  # NaN
        return ""
    text = str(value).strip()
    if not text or text.lower() in ("none", "nat", "nan"):
        return ""
    moment: Optional[datetime] = None
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            parsed = pd.to_datetime(text, errors="coerce", utc=False)
        except Exception:  # noqa: BLE001 - odd inputs degrade to the raw text
            parsed = None
        if parsed is not None and not pd.isna(parsed):
            moment = parsed.to_pydatetime()
    if moment is None:
        return text
    # Stored timestamps are UTC (see app.database.base); show them in the viewer's
    # local time so "when did I run this" reads the way the clock on the wall does.
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        moment = moment.astimezone()
    except (ValueError, OverflowError):  # pragma: no cover - absurd dates
        pass
    return "{0} {1}".format(moment.day, moment.strftime("%b %Y %H:%M"))


# ---- badges (return HTML)
#: Badge CSS prefix -> :data:`LABELS` kind. The CSS prefixes predate the labels table
#: and are baked into the stylesheet, so they are mapped rather than renamed.
_BADGE_KINDS: Dict[str, str] = {
    "status": "status",
    "risk": "risk",
    "conf": "confidence",
    "suff": "sufficiency",
    "verdict": "verdict",
    "decision": "decision",
    "mode": "mode",
    "parse": "parse",
}


def _badge(kind: str, value: Any, sub: str = "", fallback: str = "-") -> str:
    """One pill. The visible text is the friendly label; the raw token is the tooltip."""
    raw = _token(value) or _token(fallback)
    css = "ia-{0}-{1}".format(kind, _slug(value)) if _label_of(value) else "ia-plain"
    text = label(_BADGE_KINDS.get(kind, kind), raw) or raw.replace("_", " ").title() or "-"
    sub_html = ' <span class="ia-badge-sub">{0}</span>'.format(_esc(sub)) if sub else ""
    return '<span class="ia-badge {css}" title="{title}">{label}{sub}</span>'.format(
        css=css, title=_esc(raw), label=_esc(text), sub=sub_html
    )


def status_badge(status: Any) -> str:
    """Assessment outcome pill. Returns HTML - render with :func:`badges`."""
    return _badge("status", status, fallback="NOT_ASSESSED")


def risk_badge(risk_level: Any, score: Optional[float] = None) -> str:
    """Risk band pill, optionally carrying the 0-100 prototype score."""
    sub = "" if score is None else "{0:.0f}".format(float(score))
    return _badge("risk", risk_level, sub=sub, fallback="NOT_RATED")


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
    """Pipeline configuration the assessment was produced under.

    An absent or unrecognised mode reads "Mode unknown". It is never assumed to be the
    full workflow: a row that does not say which condition produced it must not be
    displayed as though it had the safety rails.
    """
    return _badge("mode", mode, fallback="UNKNOWN")


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
                # A stable key: ``hash()`` is salted per process, so a key built from it
                # would change between runs and Streamlit would lose the widget's state.
                key=action_key or "empty_action_{0}".format(_key_slug(title + " " + message)),
                type=action_type,
                width="stretch",
            )
        )


def _key_slug(text: str, limit: int = 64) -> str:
    """A deterministic, widget-key-safe slug of arbitrary text."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return (slug[:limit].rstrip("-")) or "item"


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
        extras.append(label("mode", mode))
    if provider:
        extras.append(
            "Answered by {0}{1}".format(
                provider_display_name(provider), " / " + _label_of(model) if model else ""
            )
        )
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


#: The wording of the plain-language mock state. "Demo mode" is what an auditor reads;
#: the sub-line says, in words, that no language model is involved, so the friendlier
#: label cannot soften the claim.
DEMO_MODE_LABEL = "DEMO MODE"
DEMO_MODE_NOTE = (
    "Offline rule-based assistant - not an AI language model. Results measure the "
    "workflow, not model quality."
)


def provider_banner(info: Mapping[str, Any], compact: bool = False, plain: bool = False) -> None:
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

    ``plain=True`` renders the second state as "DEMO MODE" with :data:`DEMO_MODE_NOTE`
    instead of the research wording. It changes nothing about the other two states: a
    mismatch is red and spelled out whichever vocabulary the page asked for.
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

    if plain and is_mock and not mismatched:
        line = [plain_badge(DEMO_MODE_LABEL, color)]
        detail = DEMO_MODE_NOTE
    else:
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
    expander_label: str = "",
    highlight: bool = False,
) -> None:
    """One model citation, rendered so that it can actually be checked.

    Shows the source filename, the precise locator, the verbatim quotation and the
    verdict the citation validator reached - and then offers the full stored chunk, with
    its neighbours when a ``context_loader`` is supplied, because a quoted fragment is
    only verifiable if the reviewer can see what surrounded it.

    ``context_loader`` takes a ``chunk_id`` and returns the mapping produced by
    ``app.frontend.data_access.chunk_context``. Without one, the card falls back to the
    chunk text carried on the citation row. ``expander_label`` replaces the default
    expander title; ``highlight`` marks the quoted text inside the stored chunk so the
    reviewer's eye lands on the passage being claimed.
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

    if expander_label:
        title = expander_label
    else:
        title = "{0}  -  chunk {1}".format(
            "Show the source chunk in context" if context_loader else "Show the full source chunk",
            chunk_id if chunk_id is not None else "?",
        )
    marked = quote if highlight else ""
    with st.expander(title, expanded=expanded):
        context: Mapping[str, Any] = {}
        if context_loader is not None and chunk_id is not None:
            try:
                context = context_loader(int(chunk_id)) or {}
            except Exception as exc:  # noqa: BLE001 - a viewer must not break the page
                st.warning("Could not load the surrounding context: {0}".format(exc))
        if context.get("found"):
            _render_chunk_context(context, int(chunk_id), highlight=marked)
        else:
            text = str(data.get("chunk_text") or "")
            if text:
                _write('<div class="ia-chunk ia-chunk-focus">{0}</div>'.format(_mark(text, marked)))
            else:
                st.info(
                    "This citation does not resolve to a stored evidence chunk. That is "
                    "itself the finding: there is nothing in the evidence store to check it "
                    "against."
                )


def _mark(text: str, needle: str) -> str:
    """Escape ``text`` and wrap every case-insensitive occurrence of ``needle`` in <mark>.

    Both strings are escaped *before* the search, so the only markup that can appear in
    the result is the ``<mark>`` element added here - a quotation containing ``<`` cannot
    open a tag, and a chunk containing one cannot either.
    """
    escaped = _esc(text)
    target = _esc((needle or "").strip())
    if not target:
        return escaped
    pattern = re.compile(re.escape(target), re.IGNORECASE)
    return pattern.sub(lambda match: '<mark class="ia-mark">{0}</mark>'.format(match.group(0)), escaped)


def _render_chunk_context(
    context: Mapping[str, Any], focus_chunk_id: int, highlight: str = ""
) -> None:
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
        marker = "cited passage" if is_focus else "surrounding text"
        body = _mark(str(chunk.get("text", "")), highlight) if is_focus else _esc(chunk.get("text", ""))
        _write(
            '<div class="ia-eyebrow">{0} &middot; {1}</div>'
            '<div class="ia-chunk{focus}">{2}</div>'.format(
                _esc(marker),
                _esc(chunk.get("locator_text", "")),
                body,
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
        plain_badge(label("evidence_type", data.get("evidence_type")) or "Other"),
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
            label("evidence_type", data.get("evidence_type")), data.get("extension", "")
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
#: Columns that hold a timestamp but arrive from the API as ISO strings. Coerced to real
#: datetimes before drawing so the ``DatetimeColumn`` formats in the default config apply
#: instead of the raw string being shown.
_TIMESTAMP_COLUMNS = frozenset(
    {"created", "generated_at", "uploaded_at", "completed_at", "started_at"}
)


def _coerce_timestamps(frame: "pd.DataFrame") -> "pd.DataFrame":
    for name in list(frame.columns):
        text = str(name)
        if not (text.endswith("_at") or text in _TIMESTAMP_COLUMNS):
            continue
        if frame[name].dtype != object:
            continue
        try:
            frame[name] = pd.to_datetime(frame[name], errors="coerce", utc=True)
        except Exception:  # noqa: BLE001 - an unparseable column is left as it was
            continue
    return frame


def df_table(
    rows: Union[Sequence[Mapping[str, Any]], "pd.DataFrame"],
    columns: Optional[Sequence[str]] = None,
    column_config: Optional[Mapping[str, Any]] = None,
    height: Optional[int] = None,
    key: Optional[str] = None,
    empty_message: str = "Nothing to show.",
    hide_index: bool = True,
    on_select: Any = None,
    selection_mode: Any = None,
) -> Any:
    """Render a list of dictionaries as a dataframe with consistent column handling.

    ``columns`` both selects and orders; a name that is not present is created empty
    rather than raising, so a page can list the columns it wants without first checking
    which of them a given query produced. Columns that hold ISO timestamps are coerced
    to datetimes so the shared ``DatetimeColumn`` formats apply.

    Returns the frame that was drawn (or ``None`` when there was nothing), so a caller
    can reuse it for a download button. When ``on_select`` is given (``"rerun"`` or a
    callback) it is passed through to ``st.dataframe`` together with
    ``selection_mode`` and the dataframe's selection event is returned instead; read
    ``event.selection.rows`` for the selected positional indices into the frame.
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
    frame = _coerce_timestamps(frame.copy())

    config: Dict[str, Any] = dict(_DEFAULT_COLUMN_CONFIG)
    config.update(dict(column_config or {}))
    config = {name: spec for name, spec in config.items() if name in frame.columns}

    # ``height`` is only forwarded when set: Streamlit 1.50 rejects an explicit None,
    # and "let the table size itself" is the sane default for an unspecified height.
    extra: Dict[str, Any] = {"height": int(height)} if height else {}
    if on_select is not None:
        extra["on_select"] = on_select
        if selection_mode is not None:
            extra["selection_mode"] = selection_mode
    result = st.dataframe(
        frame,
        width="stretch",
        hide_index=hide_index,
        column_config=config or None,
        key=key,
        **extra
    )
    if on_select is not None:
        return result
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


# ---- navigation and workflow
def _safe_page_link(page: str, label_text: str, icon: str = "", help_text: str = "") -> None:
    """``st.page_link`` that degrades to a caption when the target page is not installed.

    ``st.page_link`` raises when its target is not a registered page and the shell skips
    page modules that are absent; a missing sibling must not take the caller down.
    """
    try:
        st.page_link(page, label=label_text, icon=icon or None, help=help_text or None)
    except Exception:  # noqa: BLE001 - navigation is a convenience, never a dependency
        st.caption("{0} (that page is not installed in this build)".format(label_text))


#: Material icons for the five workflow steps, keyed by ``step["key"]`` as
#: ``data_access.project_stage`` names them.
WORKFLOW_ICONS: Dict[str, str] = {
    "scope": ":material/checklist:",
    "evidence": ":material/inventory_2:",
    "assess": ":material/fact_check:",
    "review": ":material/rate_review:",
    "report": ":material/summarize:",
}

WORKFLOW_COMPLETE_MESSAGE = "All steps complete - open the report"



#: Public name for pages that need one guarded link outside next_steps().
safe_page_link = _safe_page_link

def _step_label(step: Mapping[str, Any], number: int) -> str:
    name = str(step.get("label") or step.get("key") or "").strip()
    if step.get("done"):
        return "✓ {0} {1}".format(number, name)
    if step.get("current"):
        return "▸ {0} {1}".format(number, name)
    return "{0} {1}".format(number, name)


#: One-word names for the five steps, used where a five-column row has no room for
#: "Record your decisions". The full label is always shown alongside as a caption.
SHORT_STEP_LABELS: Dict[str, str] = {
    "scope": "Controls",
    "evidence": "Evidence",
    "assess": "Assess",
    "review": "Review",
    "report": "Report",
}


def _short_step_label(step: Mapping[str, Any], position: int) -> str:
    name = SHORT_STEP_LABELS.get(str(step.get("key") or ""), str(step.get("label") or ""))
    if step.get("done"):
        return "✓ {0} {1}".format(position, name)
    if step.get("current"):
        return "▸ {0} {1}".format(position, name)
    return "{0} {1}".format(position, name)


def workflow_strip(stage_info: Mapping[str, Any], compact: bool = False) -> None:
    """The five-step audit path with the finished steps ticked and the next one marked.

    Takes the mapping from ``app.frontend.data_access.project_stage``: ``stage`` plus an
    ordered ``steps`` list of ``{key, label, page, count_text, done, current}``. Each step
    is an ``st.page_link``, so the strip is also the navigation - an auditor who can see
    what comes next can click it. ``compact=True`` renders only the "Next step" line with
    one link, for pages that want a reminder rather than a map.

    Nothing here runs anything: the strip points at the page where the auditor presses
    the button.
    """
    info = dict(stage_info or {})
    steps: List[Dict[str, Any]] = [dict(step) for step in (info.get("steps") or [])]
    stage = str(info.get("stage") or "")
    current: Optional[Tuple[int, Dict[str, Any]]] = None
    for position, step in enumerate(steps, start=1):
        if step.get("current"):
            current = (position, step)
            break

    if stage == "reported" or (steps and current is None and all(s.get("done") for s in steps)):
        report_step = next((s for s in steps if s.get("key") == "report"), steps[-1] if steps else {})
        if compact:
            _safe_page_link(
                str(report_step.get("page") or "views/reports.py"),
                WORKFLOW_COMPLETE_MESSAGE,
                WORKFLOW_ICONS.get("report", ""),
            )
            return
        _write('<div class="ia-eyebrow ia-next-step">{0}</div>'.format(_esc(WORKFLOW_COMPLETE_MESSAGE)))
    elif current is not None:
        number, step = current
        headline = "Next step: {0}".format(_step_label(step, number).lstrip("▸ ").strip())
        if compact:
            count_text = str(step.get("count_text") or "").strip()
            line = headline + (" · " + count_text if count_text else "")
            _safe_page_link(str(step.get("page") or ""), line, WORKFLOW_ICONS.get(str(step.get("key")), ""))
            return
        _write('<div class="ia-eyebrow ia-next-step">{0}</div>'.format(_esc(headline)))
    elif compact:
        if steps:
            _safe_page_link(str(steps[0].get("page") or ""), "Start: {0}".format(steps[0].get("label", "")))
        return

    if not steps:
        st.caption("No workflow steps are available for this project.")
        return

    # The link carries a short name ("3 Assess") because st.page_link clips long labels
    # in a five-column row; the full step name and its count go in the caption below.
    columns = st.columns(len(steps), gap="small")
    for column, (position, step) in zip(columns, enumerate(steps, start=1)):
        with column:
            _safe_page_link(
                str(step.get("page") or ""),
                _short_step_label(step, position),
                WORKFLOW_ICONS.get(str(step.get("key")), ""),
            )
            parts = [str(step.get("label") or "").strip(), str(step.get("count_text") or "").strip()]
            caption = " · ".join(part for part in parts if part)
            if caption:
                st.caption(caption)


def next_steps(links: Sequence[Tuple[str, str, str]], primary_index: int = 0) -> None:
    """A row of page links, the primary one in bold.

    ``links`` is a list of ``(label, "views/x.py", icon)`` tuples. Nothing here runs an
    action - these are places to go, and the auditor chooses to go there.
    """
    items = [tuple(item) for item in links if item]
    if not items:
        return
    columns = st.columns(len(items), gap="small")
    for position, (column, item) in enumerate(zip(columns, items)):
        label_text = str(item[0])
        page = str(item[1]) if len(item) > 1 else ""
        icon = str(item[2]) if len(item) > 2 and item[2] else ""
        if position == primary_index:
            label_text = "**{0}**".format(label_text)
        with column:
            _safe_page_link(page, label_text, icon)


def error_with_remedy(message: str, exc: Optional[BaseException] = None, retry_key: str = "") -> None:
    """An error the auditor can act on: what went wrong, what to do, and the raw text.

    ``str(exc)`` from :mod:`app.frontend.data_access` is already a plain sentence with a
    next step (see ``_translate``); the untranslated text lives on ``exc.detail`` when
    the facade kept it and is shown under "Technical details" rather than inline. With
    ``retry_key`` a "Try again" button clears the read cache and reruns.
    """
    remedy = str(exc).strip() if exc is not None else ""
    text = str(message or "").strip()
    if remedy and remedy not in text:
        text = (text + " " if text else "") + remedy
    st.error(text or "Something went wrong.")

    raw = ""
    if exc is not None:
        raw = str(getattr(exc, "detail", "") or "").strip()
        if not raw or raw == remedy:
            raw = "{0}: {1}".format(type(exc).__name__, remedy or "(no message)")
    if raw:
        with st.expander("Technical details", expanded=False):
            st.code(raw, language="text")

    if retry_key and st.button("Try again", key=retry_key):
        from app.frontend import data_access

        data_access.invalidate_cache()
        st.rerun()


def _accepts_keyword(fn: Callable[..., Any], name: str) -> bool:
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):  # builtins and mocks
        return False


def load_demo_control(
    key: str, label: str = "Try the demo audit", help_text: str = ""
) -> None:
    """The "Try the demo audit" button: seed the demo project, then open Assessments.

    Nothing happens until the button is pressed. On click the synthetic evidence is
    generated and ingested with one progress line per file, the demo project becomes the
    working project, and the auditor lands on the Assessments page with a message that
    tells them the next thing to press. The demo project is never assessed here: running
    the model is a separate, explicit click on that page.
    """
    if not st.button(label, key=key, type="primary", help=help_text or None, width="stretch"):
        return

    from app.frontend import data_access, state

    summary: Optional[Dict[str, Any]] = None
    with st.status("Loading the demo audit...", expanded=True) as status:

        def progress(done: int, total: int, filename: str) -> None:
            st.write("{0} of {1} - {2}".format(int(done), int(total), filename))

        try:
            if _accepts_keyword(data_access.load_demo_project, "progress"):
                summary = dict(data_access.load_demo_project(progress=progress))
            else:
                summary = dict(data_access.load_demo_project())
        except data_access.DataAccessError as exc:
            status.update(label="The demo audit could not be loaded", state="error", expanded=True)
            error_with_remedy("The demo audit could not be loaded.", exc)
            return
        loaded = len(summary.get("ingested", []) or []) + len(summary.get("skipped", []) or [])
        for failure in summary.get("failures", []) or []:
            st.warning(
                "{0} was not loaded: {1}".format(failure.get("filename", "?"), failure.get("error", ""))
            )
        status.update(
            label="Demo audit ready: {0} file(s) loaded".format(loaded), state="complete", expanded=False
        )

    project_id = summary.get("project_id")
    if project_id is not None:
        switch = getattr(state, "request_project_switch", None)
        if callable(switch):
            switch(int(project_id))
        else:  # the shell contract is being implemented concurrently; fall back to today's API
            state.set_current_project(int(project_id))
    state.flash(
        "Demo audit ready: {0} files loaded. Next: press Run assessment.".format(loaded), "success"
    )
    st.switch_page("views/assessments.py")


__all__ = [
    "AI_DISCLAIMER",
    "AI_DISCLAIMER_BODY",
    "DEMO_MODE_LABEL",
    "DEMO_MODE_NOTE",
    "LABELS",
    "MOCK_PROVIDER_NOTE",
    "PROVIDER_DISPLAY_NAMES",
    "PROVIDER_MISMATCH_NOTE",
    "RISK_MODEL_NOTE",
    "WORKFLOW_COMPLETE_MESSAGE",
    "WORKFLOW_ICONS",
    "ai_disclaimer_banner",
    "ai_vs_human_panel",
    "badges",
    "citation_card",
    "confidence_badge",
    "decision_badge",
    "df_table",
    "download_row",
    "empty_state",
    "error_with_remedy",
    "escape",
    "evidence_provenance_panel",
    "four_way_panel",
    "kv_grid",
    "label",
    "load_demo_control",
    "metric_card",
    "metric_row",
    "mode_badge",
    "next_steps",
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
    "when",
    "workflow_strip",
]
