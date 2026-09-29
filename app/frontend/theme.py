"""The single source of colour for the whole interface.

Every status pill, risk band, chart series and panel border in this application resolves
its colour through a constant in this module. That is not tidiness for its own sake: an
IT audit console is read for hours at a time and a reader learns the colours as a
vocabulary, so ``POTENTIAL_DEFICIENCY`` amber in a table, in a badge and in a Plotly
chart must be the *same* amber or the vocabulary stops meaning anything. Exporting the
tokens as plain Python constants (rather than only as CSS) is what lets the chart layer
and the HTML layer share one palette instead of two that drift.

Contrast
--------
Auditors read this screen for long stretches, so every semantic colour is chosen to
clear WCAG AA (4.5:1) as *text* on each of the three grounds it is ever painted on:
``BG``, ``SURFACE`` and ``SURFACE_ALT``. The ratio recorded beside each token is the
**worst** of those three (always the one against ``SURFACE_ALT``, the lightest), so it
is a floor rather than a flattering figure, and a later edit can be re-measured against
it rather than guessed at. Badges paint that same colour as text over a low-alpha tint
of itself, which lightens the ground by a fraction of a percent and leaves the ratio
effectively unchanged.

A note on how the dark theme is achieved
----------------------------------------
Streamlit's own theme comes from ``.streamlit/config.toml``, which this module may not
write. The CSS below therefore paints every surface explicitly rather than relying on a
configured dark base, so the console looks the same whether Streamlit itself is running
in its light or dark theme. :data:`CONFIG_TOML` holds the equivalent configuration for
whoever does own that file; applying it is an improvement, not a prerequisite.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import streamlit as st

from app.schemas.enums import (
    AssessmentStatus,
    CitationVerdict,
    ConfidenceLevel,
    EvidenceSufficiency,
    ExperimentMode,
    HumanDecision,
    ParseStatus,
    RiskLevel,
)

# ---- structural palette (contrast ratios measured against BG)
BG = "#0B0F14"  #: page ground - deep ink slate
BG_HEADER = "#0E141B"
SURFACE = "#151D28"  #: cards, panels, sidebar
SURFACE_ALT = "#1B2532"  #: table headers, code blocks, nested panels
SURFACE_HOVER = "#22303F"
BORDER = "#2A3949"
BORDER_STRONG = "#3B4E63"
TEXT = "#E8EEF4"  #: 13.2:1
TEXT_MUTED = "#A3B4C6"  #: 7.3:1
TEXT_FAINT = "#8899AC"  #: 5.3:1 - captions and labels only, never body text
ACCENT = "#58A6FF"  #: 6.1:1 - the one accent, used sparingly
ACCENT_HOVER = "#79BBFF"  #: 7.6:1
#: Not a text colour: a filled button ground, so it is exempt from the floor above.
ACCENT_DIM = "#1F4B73"

# ---- semantic hues
GREEN = "#3FB950"  #: 6.1:1
AMBER = "#E0A82E"  #: 7.2:1
ORANGE = "#F0883E"  #: 6.1:1
RED = "#F85149"  #: 4.6:1
VIOLET = "#A371F7"  #: 4.6:1
GREY = "#8B949E"  #: 5.0:1
STEEL = "#58A6FF"

#: Assessment outcomes. INSUFFICIENT_EVIDENCE is deliberately violet rather than a
#: shade of red or green: "we cannot tell from this evidence" is a distinct third
#: answer, and colouring it like a near-miss deficiency would nudge a reader into
#: reading it as one.
STATUS_COLORS: Dict[str, str] = {
    AssessmentStatus.EFFECTIVE.value: GREEN,
    AssessmentStatus.POTENTIAL_DEFICIENCY.value: AMBER,
    AssessmentStatus.INSUFFICIENT_EVIDENCE.value: VIOLET,
    AssessmentStatus.NOT_EFFECTIVE.value: RED,
    AssessmentStatus.NOT_APPLICABLE.value: GREY,
}

#: Short labels for tight cells. The full enum value is always the tooltip.
STATUS_LABELS: Dict[str, str] = {
    AssessmentStatus.EFFECTIVE.value: "Effective",
    AssessmentStatus.POTENTIAL_DEFICIENCY.value: "Potential deficiency",
    AssessmentStatus.INSUFFICIENT_EVIDENCE.value: "Insufficient evidence",
    AssessmentStatus.NOT_EFFECTIVE.value: "Not effective",
    AssessmentStatus.NOT_APPLICABLE.value: "Not applicable",
}

RISK_COLORS: Dict[str, str] = {
    RiskLevel.LOW.value: GREEN,
    RiskLevel.MEDIUM.value: AMBER,
    RiskLevel.HIGH.value: ORANGE,
    RiskLevel.CRITICAL.value: RED,
    RiskLevel.NOT_RATED.value: GREY,
}

CONFIDENCE_COLORS: Dict[str, str] = {
    ConfidenceLevel.LOW.value: ORANGE,
    ConfidenceLevel.MEDIUM.value: AMBER,
    ConfidenceLevel.HIGH.value: GREEN,
}

#: NONE is red rather than grey: "no evidence at all" is the worst case for an audit
#: conclusion, not a neutral one.
SUFFICIENCY_COLORS: Dict[str, str] = {
    EvidenceSufficiency.SUFFICIENT.value: GREEN,
    EvidenceSufficiency.PARTIAL.value: AMBER,
    EvidenceSufficiency.INSUFFICIENT.value: ORANGE,
    EvidenceSufficiency.NONE.value: RED,
}

VERDICT_COLORS: Dict[str, str] = {
    CitationVerdict.VERIFIED.value: GREEN,
    CitationVerdict.PARTIAL.value: AMBER,
    CitationVerdict.UNVERIFIED.value: GREY,
    CitationVerdict.FABRICATED.value: RED,
}

DECISION_COLORS: Dict[str, str] = {
    HumanDecision.PENDING.value: GREY,
    HumanDecision.ACCEPTED.value: GREEN,
    HumanDecision.MODIFIED.value: AMBER,
    HumanDecision.REJECTED.value: RED,
    HumanDecision.MORE_EVIDENCE_REQUESTED.value: VIOLET,
}

MODE_COLORS: Dict[str, str] = {
    ExperimentMode.A_RAW_LLM.value: GREY,
    ExperimentMode.B_RAG.value: STEEL,
    ExperimentMode.C_RAG_WORKFLOW.value: VIOLET,
}

PARSE_STATUS_COLORS: Dict[str, str] = {
    ParseStatus.PARSED.value: GREEN,
    ParseStatus.PENDING.value: AMBER,
    ParseStatus.FAILED.value: RED,
    ParseStatus.UNSUPPORTED.value: ORANGE,
}

#: The four kinds of statement the whole project exists to keep apart. These colours
#: are used by the signature four-way panel and nowhere else, so the reader learns
#: them as a shape rather than as a status.
FOUR_WAY_COLORS: Dict[str, str] = {
    "requires": STEEL,
    "proves": GREEN,
    "infers": AMBER,
    "human": VIOLET,
}

FOUR_WAY_TITLES: Dict[str, str] = {
    "requires": "The control REQUIRES",
    "proves": "The evidence PROVES",
    "infers": "The AI INFERS",
    "human": "A HUMAN must verify",
}

#: Provenance colours: everything AI-generated carries the amber "unreviewed" hue,
#: everything a human concluded carries the accent. Used by the AI-vs-human panel and
#: by the disclaimer banner.
AI_COLOR = AMBER
HUMAN_COLOR = ACCENT

#: Categorical sequence for charts that are not keyed by a status or a risk band.
CHART_SEQUENCE: List[str] = [ACCENT, GREEN, AMBER, VIOLET, ORANGE, "#56D4DD", RED, GREY]

#: Streamlit theme equivalent of the tokens above, for whoever owns
#: ``.streamlit/config.toml``. Purely informational here.
CONFIG_TOML = """[theme]
base = "dark"
primaryColor = "{accent}"
backgroundColor = "{bg}"
secondaryBackgroundColor = "{surface}"
textColor = "{text}"
borderColor = "{border}"
font = "sans-serif"
""".format(accent=ACCENT, bg=BG, surface=SURFACE, text=TEXT, border=BORDER)


def hex_to_rgba(color: str, alpha: float) -> str:
    """``"#58A6FF", 0.15`` -> ``"rgba(88, 166, 255, 0.15)"``.

    Tints are computed here rather than written as CSS ``color-mix`` so that the badge
    backgrounds resolve identically in the browser and in any exported HTML, and so a
    palette change never leaves a hard-coded rgba behind.
    """
    text = (color or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    if len(text) != 6:
        return "rgba(139, 148, 158, {0})".format(round(float(alpha), 3))
    red, green, blue = (int(text[index : index + 2], 16) for index in (0, 2, 4))
    return "rgba({0}, {1}, {2}, {3})".format(red, green, blue, round(float(alpha), 3))


def _lookup(table: Dict[str, str], value: Any, default: str) -> str:
    """Tolerant palette lookup: an enum member, a raw string or ``None`` all resolve."""
    if value is None:
        return default
    key = str(getattr(value, "value", value)).strip().upper().replace(" ", "_").replace("-", "_")
    return table.get(key, default)


def status_color(status: Any) -> str:
    return _lookup(STATUS_COLORS, status, GREY)


def status_label(status: Any) -> str:
    key = str(getattr(status, "value", status) or "").strip().upper()
    return STATUS_LABELS.get(key, key.replace("_", " ").title() or "Not assessed")


def risk_color(level: Any) -> str:
    return _lookup(RISK_COLORS, level, GREY)


def confidence_color(level: Any) -> str:
    return _lookup(CONFIDENCE_COLORS, level, GREY)


def sufficiency_color(level: Any) -> str:
    return _lookup(SUFFICIENCY_COLORS, level, GREY)


def verdict_color(verdict: Any) -> str:
    return _lookup(VERDICT_COLORS, verdict, GREY)


def decision_color(decision: Any) -> str:
    return _lookup(DECISION_COLORS, decision, GREY)


def mode_color(mode: Any) -> str:
    return _lookup(MODE_COLORS, mode, GREY)


def parse_status_color(value: Any) -> str:
    return _lookup(PARSE_STATUS_COLORS, value, GREY)


def status_color_map() -> Dict[str, str]:
    """``color_discrete_map`` for a Plotly chart keyed by assessment status."""
    return dict(STATUS_COLORS)


def risk_color_map() -> Dict[str, str]:
    return dict(RISK_COLORS)


def plotly_layout(**overrides: Any) -> Dict[str, Any]:
    """Layout kwargs that make a Plotly figure sit inside the console rather than on it.

    Transparent paper (so the figure inherits whatever panel it is placed in), muted
    gridlines, and the same type colours as the surrounding HTML. Pass to
    ``fig.update_layout(**plotly_layout())``.
    """
    layout: Dict[str, Any] = {
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "font": {
            "color": TEXT_MUTED,
            "family": "'Inter', 'Segoe UI', system-ui, -apple-system, sans-serif",
            "size": 12,
        },
        "title": {"font": {"color": TEXT, "size": 15}},
        "margin": {"l": 48, "r": 20, "t": 44, "b": 40},
        "colorway": list(CHART_SEQUENCE),
        "hoverlabel": {
            "bgcolor": SURFACE_ALT,
            "bordercolor": BORDER_STRONG,
            "font": {"color": TEXT, "size": 12},
        },
        "legend": {"bgcolor": "rgba(0,0,0,0)", "font": {"color": TEXT_MUTED, "size": 11}},
        "xaxis": {
            "gridcolor": BORDER,
            "zerolinecolor": BORDER,
            "linecolor": BORDER_STRONG,
            "tickfont": {"color": TEXT_FAINT, "size": 11},
        },
        "yaxis": {
            "gridcolor": BORDER,
            "zerolinecolor": BORDER,
            "linecolor": BORDER_STRONG,
            "tickfont": {"color": TEXT_FAINT, "size": 11},
        },
    }
    layout.update(overrides)
    return layout


def style_figure(fig: Any, **overrides: Any) -> Any:
    """Apply :func:`plotly_layout` to a figure and return it, so calls can chain."""
    try:
        fig.update_layout(**plotly_layout(**overrides))
    except Exception:  # noqa: BLE001 - a chart must never take the page down
        pass
    return fig


# ---- CSS
def _badge_rules() -> str:
    """One CSS class per palette entry, generated so HTML never hard-codes a colour."""
    groups: List[Tuple[str, Dict[str, str]]] = [
        ("status", STATUS_COLORS),
        ("risk", RISK_COLORS),
        ("conf", CONFIDENCE_COLORS),
        ("suff", SUFFICIENCY_COLORS),
        ("verdict", VERDICT_COLORS),
        ("decision", DECISION_COLORS),
        ("mode", MODE_COLORS),
        ("parse", PARSE_STATUS_COLORS),
    ]
    rules: List[str] = []
    for prefix, table in groups:
        for key, color in table.items():
            rules.append(
                ".ia-badge.ia-{prefix}-{key} {{ color: {color}; "
                "background: {tint}; border-color: {edge}; }}".format(
                    prefix=prefix,
                    key=key.lower().replace("_", "-"),
                    color=color,
                    tint=hex_to_rgba(color, 0.14),
                    edge=hex_to_rgba(color, 0.42),
                )
            )
    for key, color in FOUR_WAY_COLORS.items():
        rules.append(
            ".ia-quad-{key} {{ --quad: {color}; --quad-tint: {tint}; --quad-edge: {edge}; }}".format(
                key=key,
                color=color,
                tint=hex_to_rgba(color, 0.10),
                edge=hex_to_rgba(color, 0.38),
            )
        )
    return "\n".join(rules)


def _css() -> str:
    return _CSS_TEMPLATE.format(
        bg=BG,
        bg_header=BG_HEADER,
        surface=SURFACE,
        surface_alt=SURFACE_ALT,
        surface_hover=SURFACE_HOVER,
        border=BORDER,
        border_strong=BORDER_STRONG,
        text=TEXT,
        text_muted=TEXT_MUTED,
        text_faint=TEXT_FAINT,
        accent=ACCENT,
        accent_hover=ACCENT_HOVER,
        accent_dim=ACCENT_DIM,
        accent_tint=hex_to_rgba(ACCENT, 0.14),
        accent_edge=hex_to_rgba(ACCENT, 0.45),
        ai=AI_COLOR,
        ai_tint=hex_to_rgba(AI_COLOR, 0.10),
        ai_edge=hex_to_rgba(AI_COLOR, 0.45),
        human=HUMAN_COLOR,
        human_tint=hex_to_rgba(HUMAN_COLOR, 0.10),
        red=RED,
        green=GREEN,
        amber=AMBER,
        shadow=hex_to_rgba("#000000", 0.35),
        badges=_badge_rules(),
    )


_CSS_TEMPLATE = """
/* ---- base surfaces ------------------------------------------------------ */
html, body, [data-testid="stAppViewContainer"], [data-testid="stMain"] {{
    background: {bg};
    color: {text};
}}
[data-testid="stAppViewContainer"] * {{ font-variant-ligatures: none; }}
[data-testid="stHeader"] {{ background: {bg_header}; border-bottom: 1px solid {border}; }}
[data-testid="stToolbar"] {{ color: {text_muted}; }}
[data-testid="stDecoration"] {{ display: none; }}
[data-testid="stAppDeployButton"] {{ display: none; }}
footer {{ visibility: hidden; height: 0; }}
[data-testid="stMain"] .block-container {{ padding-top: 4.2rem; padding-bottom: 4rem; max-width: 1500px; }}

body, [data-testid="stAppViewContainer"] {{
    font-family: 'Inter', 'Segoe UI', system-ui, -apple-system, 'Helvetica Neue', sans-serif;
    font-size: 0.93rem;
}}
h1, h2, h3, h4, h5, h6 {{ color: {text}; letter-spacing: -0.01em; font-weight: 600; }}
h1 {{ font-size: 1.6rem; }}
h2 {{ font-size: 1.28rem; }}
h3 {{ font-size: 1.06rem; }}
p, li, span, label, div {{ color: {text}; }}
a, a:visited {{ color: {accent}; text-decoration: none; }}
a:hover {{ color: {accent_hover}; text-decoration: underline; }}
code, kbd, pre {{
    font-family: 'JetBrains Mono', 'SF Mono', ui-monospace, Menlo, Consolas, monospace;
    font-size: 0.82rem;
}}
[data-testid="stMain"] code {{ background: {surface_alt}; color: {accent_hover}; border-radius: 3px; }}
pre, [data-testid="stCodeBlock"] {{ background: {surface_alt} !important; border: 1px solid {border}; border-radius: 6px; }}
hr {{ border-color: {border}; }}

/* ---- sidebar ------------------------------------------------------------ */
[data-testid="stSidebar"] {{
    background: {surface};
    border-right: 1px solid {border};
}}
[data-testid="stSidebar"] > div:first-child {{ padding-top: 0.6rem; }}
[data-testid="stSidebarNav"] {{ border-bottom: 1px solid {border}; }}
[data-testid="stSidebarNav"] a {{ border-radius: 6px; }}
[data-testid="stSidebarNav"] a[aria-current="page"] {{
    background: {accent_tint};
    box-shadow: inset 2px 0 0 0 {accent};
}}
[data-testid="stSidebar"] label, [data-testid="stSidebar"] p {{ color: {text_muted}; }}
[data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3 {{ color: {text}; }}
[data-testid="stSidebarCollapseButton"] button {{ color: {text_muted}; }}

/* ---- inputs and controls ------------------------------------------------ */
[data-baseweb="input"], [data-baseweb="textarea"], [data-baseweb="select"] > div,
[data-testid="stTextInput"] input, [data-testid="stNumberInput"] input,
[data-testid="stTextArea"] textarea, [data-testid="stDateInput"] input {{
    background: {surface_alt} !important;
    border-color: {border} !important;
    color: {text} !important;
}}
[data-baseweb="input"]:focus-within, [data-baseweb="select"] > div:focus-within,
[data-baseweb="textarea"]:focus-within {{
    border-color: {accent} !important;
    box-shadow: 0 0 0 2px {accent_tint};
}}
input::placeholder, textarea::placeholder {{ color: {text_faint} !important; }}
[data-baseweb="popover"] [role="listbox"], [data-baseweb="menu"], [data-baseweb="popover"] > div {{
    background: {surface_alt} !important;
    border: 1px solid {border} !important;
    color: {text} !important;
}}
[data-baseweb="menu"] li:hover, [role="option"]:hover {{ background: {surface_hover} !important; }}
[data-baseweb="tag"] {{ background: {accent_dim} !important; color: {text} !important; }}
[data-testid="stWidgetLabel"] p, [data-testid="stWidgetLabel"] label {{
    color: {text_muted}; font-size: 0.8rem; font-weight: 500; letter-spacing: 0.01em;
}}
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p {{ color: {text_faint}; }}
[data-testid="stFileUploaderDropzone"] {{
    background: {surface_alt}; border: 1px dashed {border_strong}; color: {text_muted};
}}
[data-testid="stSliderTickBar"], [data-testid="stThumbValue"] {{ color: {text_faint}; }}

/* ---- buttons ------------------------------------------------------------ */
.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {{
    background: {surface_alt};
    color: {text};
    border: 1px solid {border_strong};
    border-radius: 6px;
    font-weight: 500;
    font-size: 0.85rem;
    padding: 0.32rem 0.85rem;
    transition: background 120ms ease, border-color 120ms ease;
}}
.stButton > button:hover, .stDownloadButton > button:hover, .stFormSubmitButton > button:hover {{
    background: {surface_hover};
    border-color: {accent};
    color: {text};
}}
.stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"],
.stDownloadButton > button[kind="primary"] {{
    background: {accent_dim};
    border-color: {accent};
    color: {text};
}}
.stButton > button[kind="primary"]:hover {{ background: {accent}; color: {bg}; }}
.stButton > button:focus-visible {{ outline: 2px solid {accent}; outline-offset: 2px; }}

/* ---- tabs --------------------------------------------------------------- */
.stTabs [data-baseweb="tab-list"] {{
    gap: 0.15rem;
    border-bottom: 1px solid {border};
    background: transparent;
}}
.stTabs [data-baseweb="tab"] {{
    background: transparent;
    color: {text_faint};
    border-radius: 6px 6px 0 0;
    padding: 0.42rem 0.95rem;
    font-size: 0.85rem;
    font-weight: 500;
}}
.stTabs [data-baseweb="tab"]:hover {{ background: {surface}; color: {text}; }}
.stTabs [aria-selected="true"] {{
    background: {surface};
    color: {text} !important;
    box-shadow: inset 0 -2px 0 0 {accent};
}}
.stTabs [data-baseweb="tab-highlight"] {{ background: transparent; }}

/* ---- expanders ---------------------------------------------------------- */
[data-testid="stExpander"] {{
    background: {surface};
    border: 1px solid {border};
    border-radius: 8px;
    overflow: hidden;
}}
[data-testid="stExpander"] details {{ background: transparent; }}
[data-testid="stExpander"] summary {{
    background: {surface_alt};
    color: {text};
    font-size: 0.85rem;
    font-weight: 500;
    padding: 0.5rem 0.8rem;
}}
[data-testid="stExpander"] summary:hover {{ background: {surface_hover}; color: {accent_hover}; }}

/* ---- metric cards ------------------------------------------------------- */
[data-testid="stMetric"] {{
    background: {surface};
    border: 1px solid {border};
    border-left: 3px solid {accent};
    border-radius: 8px;
    padding: 0.7rem 0.9rem;
}}
[data-testid="stMetricLabel"] p {{
    color: {text_faint};
    font-size: 0.72rem !important;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    font-weight: 600;
}}
[data-testid="stMetricValue"] {{ color: {text}; font-size: 1.55rem; font-weight: 600; }}
[data-testid="stMetricDelta"] {{ font-size: 0.75rem; }}

/* ---- dataframes and tables ---------------------------------------------- */
[data-testid="stDataFrame"], [data-testid="stDataFrameResizable"], [data-testid="stTable"] {{
    border: 1px solid {border};
    border-radius: 8px;
    overflow: hidden;
    /* glide-data-grid reads these custom properties when it mounts. */
    --gdg-bg-cell: {surface};
    --gdg-bg-cell-medium: {surface_alt};
    --gdg-bg-header: {surface_alt};
    --gdg-bg-header-hovered: {surface_hover};
    --gdg-bg-header-has-focus: {surface_hover};
    --gdg-text-dark: {text};
    --gdg-text-medium: {text_muted};
    --gdg-text-light: {text_faint};
    --gdg-text-header: {text_muted};
    --gdg-border-color: {border};
    --gdg-horizontal-border-color: {border};
    --gdg-header-bottom-border-color: {border_strong};
    --gdg-accent-color: {accent};
    --gdg-accent-light: {accent_dim};
    --gdg-bg-search-result: {accent_dim};
}}
[data-testid="stTable"] table {{ color: {text}; }}
[data-testid="stTable"] thead th {{
    background: {surface_alt}; color: {text_muted};
    text-transform: uppercase; font-size: 0.72rem; letter-spacing: 0.06em;
}}
[data-testid="stTable"] tbody tr:nth-child(even) {{ background: {surface}; }}

/* ---- alerts ------------------------------------------------------------- */
[data-testid="stAlert"] {{ border-radius: 8px; border: 1px solid {border}; background: {surface}; }}
[data-testid="stAlert"] p {{ color: {text}; }}

/* ---- console primitives (used by app.frontend.components) --------------- */
.ia-badge {{
    display: inline-flex;
    align-items: center;
    gap: 0.32em;
    padding: 0.14em 0.55em;
    border-radius: 4px;
    border: 1px solid transparent;
    font-size: 0.74rem;
    font-weight: 600;
    letter-spacing: 0.02em;
    /* Badges carry plain-language labels ("Potential deficiency"), so they are not
       shouted in capitals; the raw token stays in the tooltip. */
    text-transform: none;
    line-height: 1.5;
    white-space: nowrap;
    vertical-align: middle;
}}
.ia-badge.ia-plain {{ color: {text_muted}; background: {surface_alt}; border-color: {border}; }}
mark.ia-mark {{
    background: {ai_tint}; color: {text}; border-bottom: 2px solid {ai};
    padding: 0 0.1em; border-radius: 2px;
}}
.ia-next-step {{ color: {accent}; font-size: 0.74rem; margin: 0.2rem 0 0.35rem 0; }}
.ia-text {{ color: {text}; font-size: 0.88rem; line-height: 1.55; margin: 0.15rem 0 0.5rem 0; white-space: pre-wrap; }}
.ia-text.ia-text-muted {{ color: {text_muted}; }}
.ia-list {{ margin: 0.2rem 0 0.5rem 0; padding-left: 1.15rem; }}
.ia-list li {{ color: {text}; font-size: 0.86rem; line-height: 1.55; margin-bottom: 0.3rem; }}
.ia-list.ia-list-muted li {{ color: {text_muted}; }}
.ia-badge .ia-badge-sub {{ opacity: 0.78; font-weight: 500; letter-spacing: 0.02em; }}
.ia-badge-row {{ display: flex; flex-wrap: wrap; gap: 0.35rem; align-items: center; margin: 0.15rem 0 0.5rem 0; }}

.ia-section {{
    display: flex; align-items: baseline; gap: 0.6rem;
    border-bottom: 1px solid {border};
    margin: 0.4rem 0 0.9rem 0; padding-bottom: 0.4rem;
}}
.ia-section-title {{ font-size: 1.02rem; font-weight: 600; color: {text}; letter-spacing: -0.01em; }}
.ia-section-sub {{ font-size: 0.8rem; color: {text_faint}; }}
.ia-eyebrow {{
    font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.09em;
    color: {text_faint}; font-weight: 600; margin-bottom: 0.15rem;
}}

/* Cards, quadrants and panes sit in st.columns of unequal natural height. The column
   itself already stretches; what does not is the stack of Streamlit wrappers between it
   and our element, so height:100% is chained through them - and only inside columns that
   actually contain one of our cards, so no other layout is disturbed. Browsers without
   :has() simply keep the natural heights. */
[data-testid="stColumn"]:is(:has(> div > div > div > .ia-card), :has(.ia-card), :has(.ia-quad), :has(.ia-pane)) {{
    display: flex;
    flex-direction: column;
}}
[data-testid="stColumn"]:is(:has(.ia-card), :has(.ia-quad), :has(.ia-pane))
  :is([data-testid="stVerticalBlock"], [data-testid="stElementContainer"],
      [data-testid="stMarkdown"], [data-testid="stMarkdownContainer"]) {{ height: 100%; }}

.ia-card {{
    background: {surface};
    border: 1px solid {border};
    border-radius: 8px;
    padding: 0.75rem 0.9rem;
    height: 100%;
}}
.ia-metric {{ border-left: 3px solid {accent}; }}
.ia-metric-label {{
    font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.075em;
    color: {text_faint}; font-weight: 600;
}}
.ia-metric-value {{ font-size: 1.5rem; font-weight: 600; color: {text}; line-height: 1.25; margin-top: 0.15rem; }}
.ia-metric-delta {{ font-size: 0.75rem; color: {text_muted}; margin-top: 0.1rem; }}
.ia-metric-caption {{ font-size: 0.72rem; color: {text_faint}; margin-top: 0.25rem; line-height: 1.4; }}

.ia-ai-banner {{
    display: flex; align-items: flex-start; gap: 0.6rem;
    background: {ai_tint};
    border: 1px solid {ai_edge};
    border-left: 3px solid {ai};
    border-radius: 6px;
    padding: 0.55rem 0.8rem;
    margin: 0.2rem 0 0.85rem 0;
}}
.ia-ai-banner .ia-ai-mark {{
    color: {ai}; font-weight: 700; font-size: 0.72rem; letter-spacing: 0.09em;
    text-transform: uppercase; white-space: nowrap; padding-top: 0.05rem;
}}
.ia-ai-banner .ia-ai-text {{ color: {text_muted}; font-size: 0.8rem; line-height: 1.45; }}
.ia-ai-banner.ia-compact {{ padding: 0.35rem 0.6rem; margin-bottom: 0.5rem; }}

.ia-quad {{
    background: {surface};
    border: 1px solid {border};
    border-top: 3px solid var(--quad, {accent});
    border-radius: 8px;
    padding: 0.7rem 0.85rem;
    height: 100%;
}}
.ia-quad-head {{
    display: flex; align-items: center; justify-content: space-between; gap: 0.5rem;
    margin-bottom: 0.45rem;
}}
.ia-quad-title {{
    color: var(--quad, {accent});
    font-size: 0.74rem; font-weight: 700; letter-spacing: 0.09em; text-transform: uppercase;
}}
.ia-quad-count {{
    color: {text_faint}; font-size: 0.7rem; font-weight: 600;
    background: var(--quad-tint); border: 1px solid var(--quad-edge);
    border-radius: 10px; padding: 0.02rem 0.45rem;
}}
.ia-quad-lead {{ color: {text}; font-size: 0.84rem; line-height: 1.5; margin-bottom: 0.5rem; }}
.ia-quad ul {{ margin: 0.2rem 0 0.4rem 0; padding-left: 1.05rem; }}
.ia-quad li {{ color: {text_muted}; font-size: 0.81rem; line-height: 1.5; margin-bottom: 0.28rem; }}
.ia-quad .ia-quad-empty {{ color: {text_faint}; font-size: 0.8rem; font-style: italic; }}
.ia-quad-note {{
    margin-top: 0.5rem; padding-top: 0.45rem; border-top: 1px dashed {border};
    color: {text_faint}; font-size: 0.72rem; line-height: 1.45;
}}

.ia-cite {{
    background: {surface};
    border: 1px solid {border};
    border-left: 3px solid {border_strong};
    border-radius: 6px;
    padding: 0.6rem 0.8rem;
    margin-bottom: 0.5rem;
}}
.ia-cite-head {{
    display: flex; flex-wrap: wrap; align-items: center; gap: 0.45rem;
    margin-bottom: 0.4rem;
}}
.ia-cite-file {{ color: {text}; font-size: 0.83rem; font-weight: 600; }}
.ia-cite-loc {{
    color: {text_faint}; font-size: 0.75rem;
    font-family: 'JetBrains Mono', ui-monospace, Menlo, monospace;
}}
.ia-quote {{
    border-left: 2px solid {border_strong};
    background: {surface_alt};
    color: {text};
    padding: 0.45rem 0.7rem;
    margin: 0.15rem 0 0.4rem 0;
    border-radius: 0 4px 4px 0;
    font-size: 0.82rem;
    line-height: 1.55;
    white-space: pre-wrap;
    word-break: break-word;
}}
.ia-cite-why {{ color: {text_muted}; font-size: 0.78rem; line-height: 1.45; }}
.ia-chunk {{
    background: {surface_alt}; border: 1px solid {border}; border-radius: 6px;
    padding: 0.5rem 0.7rem; color: {text_muted};
    font-family: 'JetBrains Mono', ui-monospace, Menlo, monospace;
    font-size: 0.74rem; line-height: 1.55; white-space: pre-wrap; word-break: break-word;
    max-height: 22rem; overflow-y: auto;
}}
.ia-chunk-focus {{ border-color: {accent}; color: {text}; }}

.ia-split {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; }}
.ia-pane {{ background: {surface}; border: 1px solid {border}; border-radius: 8px; padding: 0.7rem 0.85rem; }}
.ia-pane-ai {{ border-top: 3px solid {ai}; }}
.ia-pane-human {{ border-top: 3px solid {human}; }}
.ia-pane-title {{
    font-size: 0.74rem; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;
    margin-bottom: 0.5rem;
}}
.ia-pane-ai .ia-pane-title {{ color: {ai}; }}
.ia-pane-human .ia-pane-title {{ color: {human}; }}
.ia-field {{ margin-bottom: 0.55rem; }}
.ia-field-label {{
    font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.08em;
    color: {text_faint}; font-weight: 600;
}}
.ia-field-value {{ color: {text}; font-size: 0.84rem; line-height: 1.5; margin-top: 0.1rem; white-space: pre-wrap; }}
.ia-field.ia-diverged {{
    background: {ai_tint}; border-left: 2px solid {ai};
    border-radius: 0 4px 4px 0; padding: 0.3rem 0.5rem; margin-left: -0.5rem;
}}
.ia-diverge-list {{ margin: 0.35rem 0 0 0; padding-left: 1.05rem; }}
.ia-diverge-list li {{ color: {text_muted}; font-size: 0.8rem; margin-bottom: 0.2rem; }}

.ia-kv {{ display: grid; grid-template-columns: minmax(8rem, auto) 1fr; gap: 0.25rem 0.9rem; }}
.ia-kv dt {{
    color: {text_faint}; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em;
    font-weight: 600; padding-top: 0.12rem;
}}
.ia-kv dd {{ color: {text}; font-size: 0.83rem; margin: 0; word-break: break-word; }}

.ia-empty {{
    border: 1px dashed {border_strong};
    background: {surface};
    border-radius: 8px;
    padding: 2rem 1.5rem;
    text-align: center;
}}
.ia-empty-title {{ color: {text}; font-size: 1rem; font-weight: 600; margin-bottom: 0.3rem; }}
.ia-empty-text {{ color: {text_faint}; font-size: 0.84rem; line-height: 1.55; max-width: 46rem; margin: 0 auto; }}

.ia-note {{
    color: {text_faint}; font-size: 0.74rem; line-height: 1.5;
    border-left: 2px solid {border_strong}; padding-left: 0.6rem; margin: 0.4rem 0;
}}
.ia-bar {{ background: {surface_alt}; border-radius: 3px; height: 6px; overflow: hidden; }}
.ia-bar > span {{ display: block; height: 100%; background: {accent}; }}

/* ---- sidebar identity block --------------------------------------------- */
.ia-brand {{ padding: 0.2rem 0 0.7rem 0; border-bottom: 1px solid {border}; margin-bottom: 0.7rem; }}
.ia-brand-name {{ color: {text}; font-size: 0.98rem; font-weight: 700; letter-spacing: -0.01em; }}
.ia-brand-sub {{ color: {text_faint}; font-size: 0.72rem; line-height: 1.45; margin-top: 0.2rem; }}
.ia-brand-rule {{ color: {accent}; font-size: 0.72rem; font-weight: 600; margin-top: 0.45rem; }}
.ia-provider {{
    border: 1px solid {border}; border-radius: 6px; padding: 0.45rem 0.6rem;
    background: {surface_alt}; margin: 0.5rem 0;
}}
.ia-provider-line {{ display: flex; align-items: center; gap: 0.4rem; margin-bottom: 0.25rem; }}
.ia-provider-detail {{ color: {text_faint}; font-size: 0.71rem; line-height: 1.45; word-break: break-word; }}
.ia-provider.ia-mock {{ border-color: {ai_edge}; background: {ai_tint}; }}

{badges}

/* ---- print --------------------------------------------------------------
   Auditors print working papers. On paper the console inverts to black on white,
   the chrome disappears, and every panel keeps its coloured left edge so the
   status vocabulary survives a monochrome printer as a position rather than a hue. */
@media print {{
    html, body, [data-testid="stAppViewContainer"], [data-testid="stMain"] {{
        background: #ffffff !important; color: #111111 !important;
    }}
    [data-testid="stSidebar"], [data-testid="stHeader"], [data-testid="stToolbar"],
    .stButton, .stDownloadButton, [data-testid="stFileUploaderDropzone"] {{ display: none !important; }}
    [data-testid="stMain"] .block-container {{ max-width: 100%; padding: 0 !important; }}
    h1, h2, h3, h4, p, li, span, div, dd, .ia-field-value, .ia-metric-value {{ color: #111111 !important; }}
    .ia-section-sub, .ia-metric-label, .ia-field-label, .ia-cite-loc, .ia-kv dt,
    .ia-quad-note, .ia-note, .ia-empty-text {{ color: #444444 !important; }}
    .ia-card, .ia-quad, .ia-cite, .ia-pane, [data-testid="stMetric"], [data-testid="stExpander"] {{
        background: #ffffff !important;
        border: 1px solid #999999 !important;
        box-shadow: none !important;
        page-break-inside: avoid;
    }}
    .ia-quote, .ia-chunk {{ background: #f4f4f4 !important; color: #111111 !important; border-color: #999999 !important; }}
    .ia-badge {{ background: #ffffff !important; border: 1px solid #333333 !important; color: #111111 !important; }}
    .ia-ai-banner {{ background: #ffffff !important; border: 2px solid #111111 !important; }}
    .ia-ai-banner .ia-ai-mark, .ia-ai-banner .ia-ai-text {{ color: #111111 !important; }}
    [data-testid="stExpander"] details {{ display: block; }}
    [data-testid="stExpander"] details > div {{ display: block !important; }}
    .ia-chunk {{ max-height: none; overflow: visible; }}
}}
"""


def inject_theme(hide_chrome: bool = True) -> None:
    """Write the console stylesheet into the page.

    Called once per script run from the entry point, before anything else renders.
    Streamlit rebuilds the DOM on every rerun, so this is re-injected each time rather
    than guarded by a "have we done this" flag - a guard would leave the page unstyled
    after the first interaction.
    """
    css = _css()
    if not hide_chrome:
        css = css.replace('[data-testid="stAppDeployButton"] { display: none; }', "")
    st.markdown("<style>{0}</style>".format(css), unsafe_allow_html=True)


#: Exhaustive and maintained by hand. Every token a page or a chart is expected to reach
#: for is listed, because "which colours may I use?" should be answerable by reading one
#: list rather than by scrolling the module.
__all__ = [
    "ACCENT",
    "ACCENT_DIM",
    "ACCENT_HOVER",
    "AI_COLOR",
    "AMBER",
    "BG",
    "BG_HEADER",
    "BORDER",
    "BORDER_STRONG",
    "CHART_SEQUENCE",
    "CONFIDENCE_COLORS",
    "CONFIG_TOML",
    "DECISION_COLORS",
    "FOUR_WAY_COLORS",
    "FOUR_WAY_TITLES",
    "GREEN",
    "GREY",
    "HUMAN_COLOR",
    "MODE_COLORS",
    "ORANGE",
    "PARSE_STATUS_COLORS",
    "RED",
    "RISK_COLORS",
    "STATUS_COLORS",
    "STATUS_LABELS",
    "STEEL",
    "SUFFICIENCY_COLORS",
    "SURFACE",
    "SURFACE_ALT",
    "SURFACE_HOVER",
    "TEXT",
    "TEXT_FAINT",
    "TEXT_MUTED",
    "VERDICT_COLORS",
    "VIOLET",
    "confidence_color",
    "decision_color",
    "hex_to_rgba",
    "inject_theme",
    "mode_color",
    "parse_status_color",
    "plotly_layout",
    "risk_color",
    "risk_color_map",
    "status_color",
    "status_color_map",
    "status_label",
    "style_figure",
    "sufficiency_color",
    "verdict_color",
]
