"""The research bench: the synthetic suite, the A/B/C experiments and their metrics.

This page exists to make a claim checkable, not to make one look good. It is kept apart
from the auditor's path on purpose: nothing here reads or writes the audit project chosen
in the sidebar, and its research figures (accuracy, F1, hallucination rates) appear
nowhere else in the console. Three rules are enforced by its layout rather than left to
the reader's care.

**No metric appears without its sample size.** Every tile carries ``n``, every table
states what it was computed over, and the automatic caveats attached by
``app.evaluation.metrics.compute_metrics`` are printed at the top of a run's results
rather than tucked into an appendix. With six synthetic datasets none of these figures is
statistically meaningful, and a percentage shown alone invites exactly the over-claiming
this project is meant to avoid.

**Undefined is not zero.** A rate with an empty denominator arrives here as ``None`` or
``NaN`` and is rendered "n/a". Cohen's kappa is undefined when both raters used a single
label, and is shown as undefined rather than as 1.0.

**What is being measured is stated.** These are synthetic datasets authored to have a
known answer, executed against whichever provider is configured - by default a
deterministic rule-based stand-in. Accuracy here measures whether the pipeline reaches
the planted conclusion on unambiguous evidence. It is not a measurement of language-model
capability, and it does not generalise to real audit evidence.

Widget keys on this page start with ``eval_``. They are deliberately *not* one of
``state.PROJECT_SCOPED_KEY_PREFIXES``: the bench does not depend on the working audit
project, so switching project must not reset a half-configured experiment.
"""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd
import streamlit as st

from app.frontend import components, data_access, state, theme

#: Extensions whose bytes are text a reviewer can read directly in the browser. Anything
#: else (pdf, docx, xlsx) is reported by name and size only - a hex dump of a .docx would
#: tell a reader nothing about whether the generated evidence is right.
_PREVIEWABLE = {".csv", ".txt", ".md", ".json"}

_PREVIEW_CHARS = 3000

#: Session key holding ``{dataset_id: [generated file paths]}`` for the preview blocks.
#: One fixed key rather than one key per dataset, so a regeneration can replace exactly
#: the paths it superseded and nothing stale survives.
_PREVIEW_KEY = "eval_preview_paths"

#: The run picker's widget key. Fixed, with an ``on_change`` callback, so Streamlit keeps
#: the widget stable across reruns; the value is reconciled with ``state.current_run_id``
#: *before* the widget is instantiated (see :func:`_render_runs`).
_RUN_PICK_KEY = "eval_open_run"

#: The letter a condition is known by in the study write-up, for narrow table columns.
_MODE_SHORT: Dict[str, str] = {
    "A_RAW_LLM": "A",
    "B_RAG": "B",
    "C_RAG_WORKFLOW": "C",
}

_SUITE_NOTE = (
    "Six synthetic datasets, each authored so that the correct conclusion is known in "
    "advance. They describe no real organisation, system or person. DATASET-006 is an "
    "addition beyond the five cases required by the study brief: without a control that "
    "is genuinely effective there is no true-negative class, and a system that answered "
    "'deficiency' unconditionally would score perfectly on the other five."
)

_RUNS_SHOWN = 50


# ---- lazy access to the parts of the harness the facade does not expose
def _datasets_module() -> Any:
    """The dataset generator, when this build can reach it.

    Dataset *metadata* comes through :mod:`app.frontend.data_access` like every other
    read. Dataset *generation* does not, because the facade exposes no generator entry
    point and the generator writes files to the local filesystem - which is meaningful
    only when the UI runs in the same process as the data. Over HTTP this returns None
    and the page says so rather than pretending.
    """
    if data_access.use_api():
        return None
    try:
        from app.evaluation import datasets as module

        return module
    except Exception:  # noqa: BLE001 - absence is a fact to report, not a crash
        return None


def _metrics_module() -> Any:
    """``app.evaluation.metrics``, used only for the agreement statistics.

    Human/AI concordance is not part of an evaluation run (the harness has no human in
    it), so it has to be computed here from the reviews an auditor actually recorded.
    The definitions of agreement and of Cohen's kappa live in the metrics module and are
    imported rather than re-implemented, so the page and the write-up cannot drift apart.
    """
    try:
        from app.evaluation import metrics as module

        return module
    except Exception:  # noqa: BLE001
        return None


# ---- formatting helpers
def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return False


def _pct(value: Any, digits: int = 0) -> str:
    """A rate as a percentage, or 'n/a' when its denominator was empty."""
    if _is_missing(value):
        return "n/a"
    try:
        return "{0:.{1}f}%".format(float(value) * 100.0, digits)
    except (TypeError, ValueError):
        return "n/a"


def _num(value: Any, digits: int = 2) -> str:
    if _is_missing(value):
        return "n/a"
    try:
        return "{0:.{1}f}".format(float(value), digits)
    except (TypeError, ValueError):
        return str(value)


def _clean_frame(rows: Sequence[Dict[str, Any]]) -> pd.DataFrame:
    """Comparison rows as a frame with NaN rendered as 'n/a' rather than as a number."""
    frame = pd.DataFrame(list(rows))
    if frame.empty:
        return frame
    return frame.where(pd.notnull(frame), None)


def _mode_label(value: Any) -> str:
    """The friendly name of an experimental condition; never the raw token."""
    return components.label("mode", value) or components.label("mode", "UNKNOWN")


def _mode_short(value: Any) -> str:
    """'A', 'B' or 'C' for a narrow column; the full label for anything unexpected."""
    token = str(getattr(value, "value", value) or "").strip().upper()
    return _MODE_SHORT.get(token, _mode_label(value))


def _status_label(value: Any) -> str:
    return components.label("status", value)


def _risk_label(value: Any) -> str:
    return components.label("risk", value)


def _run_status_label(value: Any) -> str:
    # Run statuses ('completed', 'failed', ...) have no LABELS table; label() title-cases.
    return components.label("run_status", value)


def _run_option_label(row: Dict[str, Any], run_id: Any) -> str:
    """'#7 · <name>' for a picker; the condition's label stands in for a blank name.

    The page's default run name already starts with the condition's friendly label, so
    the mode is not repeated in front of it.
    """
    mode_text = _mode_label(row.get("experiment_mode"))
    name = str(row.get("name") or "").strip()
    if not name:
        return "#{0} · {1}".format(run_id, mode_text)
    if name.startswith(mode_text):
        return "#{0} · {1}".format(run_id, name)
    return "#{0} · {1} · {2}".format(run_id, mode_text, name)


# ---- the dataset catalogue
def _render_datasets(datasets: List[Dict[str, Any]]) -> None:
    if not datasets:
        components.empty_state(
            "The dataset catalogue is empty",
            "The synthetic suite could not be read in this build, so there is nothing to "
            "generate or preview.",
        )
        return

    components.section_header(
        "Synthetic evaluation suite",
        subtitle="{0} datasets with a known correct answer.".format(len(datasets)),
    )
    components.note(_SUITE_NOTE)

    table = [
        {
            "dataset_id": row.get("dataset_id", ""),
            "name": row.get("name", ""),
            "control_ref": row.get("control_ref", ""),
            "expected_status": _status_label(row.get("expected_status")),
            "expected_risk": _risk_label(row.get("expected_risk")),
            "required": bool(row.get("mandated")),
            "files": len(row.get("file_names", []) or []),
            "exception_ratio": row.get("exception_ratio"),
            "markers": len(row.get("key_evidence_markers", []) or []),
        }
        for row in datasets
    ]
    components.df_table(
        table,
        columns=[
            "dataset_id",
            "name",
            "control_ref",
            "expected_status",
            "expected_risk",
            "required",
            "files",
            "exception_ratio",
            "markers",
        ],
        column_config={
            "dataset_id": st.column_config.TextColumn("Dataset", width="small"),
            "name": st.column_config.TextColumn("Name", width="medium"),
            "control_ref": st.column_config.TextColumn("Control", width="small"),
            "expected_status": st.column_config.TextColumn("Ground truth status", width="medium"),
            "expected_risk": st.column_config.TextColumn("Ground truth risk", width="small"),
            "required": st.column_config.CheckboxColumn(
                "Required by study brief",
                width="small",
                help="Ticked for the five cases the study brief mandates; DATASET-006 was added as the true-negative control.",
            ),
            "files": st.column_config.NumberColumn("Files", width="small", format="%d"),
            "exception_ratio": st.column_config.NumberColumn(
                "Exception rate", width="small", format="%.2f"
            ),
            "markers": st.column_config.NumberColumn("Retrieval markers", width="small", format="%d"),
        },
        key="eval_datasets",
    )

    if _datasets_module() is None:
        st.caption(
            "File generation is available only when the console runs in-process; the "
            "API exposes no endpoint that writes files to the server's filesystem. The "
            "evaluation runner generates whatever it needs on its own."
        )
    else:
        st.caption(
            "Open a dataset below to generate its files and read them before a run. "
            "Generation is seeded and idempotent, so regenerating produces byte-identical "
            "files. The evaluation runner generates whatever it needs on its own."
        )

    for row in datasets:
        with st.expander(
            "{0} · {1} → {2}".format(
                row.get("dataset_id", ""),
                row.get("name", ""),
                _status_label(row.get("expected_status")) or "-",
            ),
            expanded=False,
        ):
            _render_dataset_detail(row)


def _render_dataset_detail(dataset: Dict[str, Any]) -> None:
    required = bool(dataset.get("mandated"))
    components.badges(
        components.plain_badge(str(dataset.get("dataset_id", ""))),
        components.status_badge(dataset.get("expected_status")),
        components.risk_badge(dataset.get("expected_risk")),
        components.plain_badge(
            "required by study brief" if required else "added beyond the study brief",
            theme.ACCENT if required else theme.VIOLET,
        ),
    )
    components.kv_grid(
        {
            "Control under test": dataset.get("control_ref", ""),
            "Expected finding": dataset.get("expected_finding", ""),
            "Expects a declaration of missing evidence": dataset.get("expected_missing_evidence"),
            "Population": ", ".join(
                "{0}={1}".format(key, value)
                for key, value in (dataset.get("population") or {}).items()
                if not isinstance(value, (list, dict))
            ),
            "Exception rows (1-based, header = row 1)": ", ".join(
                str(item) for item in (dataset.get("exception_rows") or [])
            ),
            "Generator seed": dataset.get("seed", ""),
        }
    )
    st.markdown("**Why this case is in the suite**")
    st.caption(str(dataset.get("rationale", "")))
    st.markdown("**What it does and does not show**")
    st.caption(str(dataset.get("notes", "")))

    files = list(dataset.get("files") or [])
    if files:
        components.df_table(
            [
                {
                    "filename": item.get("filename", ""),
                    "evidence_type": components.label("evidence_type", item.get("evidence_type")),
                    "role": item.get("role", ""),
                    "description": item.get("description", ""),
                }
                for item in files
            ],
            columns=["filename", "evidence_type", "role", "description"],
            column_config={
                "filename": st.column_config.TextColumn("File", width="medium"),
                "evidence_type": st.column_config.TextColumn("Evidence type", width="small"),
                "role": st.column_config.TextColumn("Role", width="small"),
                "description": st.column_config.TextColumn("Description", width="large"),
            },
            key="eval_ds_files_{0}".format(dataset.get("dataset_id", "")),
        )

    markers = list(dataset.get("key_evidence_markers") or [])
    if markers:
        st.markdown("**Retrieval markers**")
        st.caption(
            "Strings a correct retrieval must surface. The runner scores retrieval by "
            "literal substring match against the evidence actually put in front of the model."
        )
        for marker in markers:
            st.markdown("- `{0}`".format(str(marker).replace("`", "'")))

    if _datasets_module() is None:
        return
    dataset_id = str(dataset.get("dataset_id", ""))
    if st.button(
        "Generate and preview this dataset",
        key="eval_gen_{0}".format(dataset_id),
        help="Writes this dataset's synthetic evidence files to disk and shows them below. Deterministic: same bytes every time.",
    ):
        _generate(dataset_id)

    paths = _preview_paths(dataset_id)
    if paths:
        _render_file_previews([Path(text) for text in paths])


def _preview_paths(dataset_id: str) -> List[str]:
    previews = st.session_state.get(_PREVIEW_KEY) or {}
    return list(previews.get(dataset_id) or [])


def _remember_preview(dataset_id: str, paths: Sequence[str]) -> None:
    """Replace the remembered preview paths for one dataset (clearing the old ones)."""
    previews = dict(st.session_state.get(_PREVIEW_KEY) or {})
    previews[dataset_id] = list(paths)
    st.session_state[_PREVIEW_KEY] = previews


def _generate(dataset_id: str) -> None:
    """Write one dataset's synthetic files to disk and remember the paths for previewing."""
    module = _datasets_module()
    if module is None:
        st.error("The dataset generator is not available in this build.")
        return
    with st.spinner("Generating synthetic evidence files…"):
        try:
            paths = module.generate_dataset(dataset_id)
        except Exception as exc:  # noqa: BLE001 - report, do not take the page down
            st.error("{0} could not be generated: {1}".format(dataset_id, exc))
            return
    texts = [str(path) for path in paths]
    _remember_preview(dataset_id, texts)
    st.success(
        "Wrote {0} file(s) for {1}. All of it is fabricated research data and represents "
        "no real organisation. Next: read the previews below, then run the experiment "
        "from the 'Run and results' tab.".format(len(texts), dataset_id)
    )


def _render_file_previews(paths: Sequence[Path]) -> None:
    """Show the generated files so the planted condition can be checked by eye.

    One tab per file rather than one expander per file: this block lives inside the
    dataset's expander and Streamlit does not allow expanders to nest.
    """
    if not paths:
        return
    st.markdown("**Generated files**")
    names: List[str] = []
    for index, path in enumerate(paths, start=1):
        name = path.name or "file {0}".format(index)
        if name in names:
            name = "{0} ({1})".format(name, index)
        names.append(name)
    for tab, path in zip(st.tabs(names), paths):
        with tab:
            _render_file_preview(path)


def _render_file_preview(path: Path) -> None:
    try:
        size = path.stat().st_size
    except OSError as exc:
        st.caption("{0}: not readable ({1})".format(path.name, exc))
        return

    if path.suffix.lower() not in _PREVIEWABLE:
        st.caption(
            "{0} - {1:.1f} KB. Binary format; open it in its own application to inspect it.".format(
                path.name, size / 1024.0
            )
        )
        return
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        st.caption("{0}: not readable ({1})".format(path.name, exc))
        return
    st.caption("{0} - {1:.1f} KB".format(path.name, size / 1024.0))
    st.code(text[:_PREVIEW_CHARS], language=None)
    if len(text) > _PREVIEW_CHARS:
        st.caption("First {0} of {1} characters.".format(_PREVIEW_CHARS, len(text)))


# ---- running experiments
def _render_runner(datasets: List[Dict[str, Any]]) -> None:
    availability = data_access.evaluation_available()
    components.section_header(
        "Run an experiment",
        subtitle="Each run creates an isolated research project per dataset, ingests its files and assesses the control.",
    )
    if not availability.get("available"):
        st.warning(
            "Experiments cannot be launched from this build: {0}".format(
                availability.get("reason", "the evaluation runner is unavailable")
            )
        )
        return
    if not datasets:
        st.caption("No datasets are available, so there is nothing to run.")
        return

    ids = [row["dataset_id"] for row in datasets]
    mode_values = [item["value"] for item in data_access.experiment_modes()]

    left, right = st.columns([2, 2], gap="medium")
    with left:
        chosen_modes = st.multiselect(
            "Experimental conditions",
            options=mode_values,
            default=mode_values,
            format_func=_mode_label,
            key="eval_modes",
            help="One run is recorded per condition, so they can be compared row by row.",
        )
    with right:
        chosen_datasets = st.multiselect(
            "Datasets",
            options=ids,
            default=ids,
            key="eval_ds_pick",
        )
    run_name = st.text_input(
        "Run name (optional)",
        key="eval_run_name",
        placeholder="e.g. baseline-2026-09-30",
        help="Stored on the run so a figure in the write-up can be traced back to it.",
    )

    st.caption(
        "Runs execute synchronously in this process. Against the offline provider the "
        "whole suite takes seconds; against a hosted model it takes minutes, and the "
        "browser must stay open. The research projects are kept afterwards so the "
        "prompts and responses behind every metric remain auditable."
    )

    if st.button(
        "Run {0} condition(s) over {1} dataset(s)".format(len(chosen_modes), len(chosen_datasets)),
        type="primary",
        key="eval_run_btn",
        disabled=not chosen_modes or not chosen_datasets,
    ):
        _run_experiments(chosen_modes, chosen_datasets, run_name)


def _run_experiments(modes: Sequence[str], dataset_ids: Sequence[str], run_name: str) -> None:
    progress = st.progress(0.0, text="Starting…")
    line = st.empty()
    started = time.perf_counter()
    completed: List[str] = []
    failed: List[str] = []

    for index, mode in enumerate(modes, start=1):
        line.caption(
            "Running {0} ({1} of {2}) - {3:.0f}s elapsed".format(
                _mode_label(mode), index, len(modes), time.perf_counter() - started
            )
        )
        # A blank name would make the runner title the run by its raw mode token; the
        # page names it by the condition's friendly label instead.
        name = (
            "{0} [{1}]".format(run_name.strip(), _mode_short(mode))
            if run_name.strip()
            else "{0} over {1} dataset(s)".format(_mode_label(mode), len(dataset_ids))
        )
        try:
            run = data_access.run_evaluation(
                mode=mode,
                dataset_ids=list(dataset_ids),
                run_name=name,
            )
            completed.append(
                "{0} → run #{1} ({2})".format(
                    _mode_label(mode), run.get("id"), _run_status_label(run.get("status")) or "recorded"
                )
            )
            if run.get("id") is not None:
                state.set_current_run(int(run.get("id")))
        except data_access.DataAccessError as exc:
            failed.append("{0}: {1}".format(_mode_label(mode), exc))
        progress.progress(index / float(len(modes)))

    progress.empty()
    line.empty()
    elapsed = time.perf_counter() - started
    if completed:
        state.flash(
            "{0} condition(s) finished in {1:.1f}s: {2}. Next: the latest run is open "
            "below - read its caveats first, then compare the conditions in the "
            "'Compare runs' tab.".format(len(completed), elapsed, "; ".join(completed)),
            "success",
        )
    if failed:
        state.flash(
            "{0} condition(s) failed: {1}".format(len(failed), "; ".join(failed)),
            "error",
        )
    st.rerun()


# ---- run results
def _render_run_metrics(run: Dict[str, Any]) -> None:
    metrics = dict(run.get("metrics") or {})
    if not metrics:
        st.warning("This run recorded no metrics. It may have failed before scoring.")
        return

    n = metrics.get("n", 0)
    n_scored = metrics.get("n_scored", 0)
    classification = dict(metrics.get("classification") or {})
    evidence = dict(metrics.get("evidence") or {})
    deficiency = dict(metrics.get("deficiency_detection") or {})
    timing = dict(metrics.get("timing") or {})

    caveats = list(metrics.get("caveats") or [])
    if caveats:
        st.warning(
            "Caveats attached to these figures by app.evaluation.metrics - they travel "
            "with the numbers wherever the numbers go:"
        )
        for caveat in caveats:
            st.markdown("- {0}".format(caveat))

    components.metric_row(
        [
            {
                "label": "Accuracy",
                "value": _pct(classification.get("accuracy")),
                "caption": "n = {0} scored of {1} result(s).".format(n_scored, n),
                "color": theme.ACCENT,
            },
            {
                "label": "Macro F1",
                "value": _num(classification.get("macro_f1")),
                "caption": "Unweighted mean over {0} observed class(es).".format(
                    len(classification.get("labels", []) or [])
                ),
            },
            {
                "label": "Deficiency recall",
                "value": _pct(deficiency.get("recall")),
                "caption": "n = {0}; {1} true positive(s).".format(
                    deficiency.get("n", 0), deficiency.get("true_positives", 0)
                ),
            },
            {
                "label": "Grounding rate",
                "value": _pct(evidence.get("grounding_rate")),
                "caption": "{0} verified of {1} citation(s).".format(
                    evidence.get("citations_verified", 0), evidence.get("citations_total", 0)
                ),
            },
            {
                "label": "Fabrication rate",
                "value": _pct(evidence.get("fabrication_rate")),
                "caption": "{0} fabricated citation(s).".format(evidence.get("citations_fabricated", 0)),
                "color": theme.verdict_color("FABRICATED"),
            },
            {
                "label": "Median latency",
                "value": "{0} ms".format(_num((timing.get("latency_ms") or {}).get("median"), 0)),
                "caption": "n = {0} timed assessment(s).".format(timing.get("n_timed", 0)),
            },
        ],
        columns=6,
    )
    components.note(
        "Latency is wall clock around this Python pipeline, including retrieval and "
        "persistence. It is not an inference-time measurement and must never be reported "
        "as one."
    )


def _render_per_class(metrics: Dict[str, Any]) -> None:
    classification = dict(metrics.get("classification") or {})
    per_class = dict(classification.get("per_class") or {})
    if not per_class:
        return
    components.section_header(
        "Per-class precision, recall and F1",
        subtitle="One row per assessment status, with the ground-truth support it rests on.",
    )
    rows = [
        {
            "status": _status_label(token) or str(token),
            "precision": cell.get("precision"),
            "recall": cell.get("recall"),
            "f1": cell.get("f1"),
            "support": cell.get("support"),
            "true_positives": cell.get("true_positives"),
            "false_positives": cell.get("false_positives"),
            "false_negatives": cell.get("false_negatives"),
        }
        for token, cell in per_class.items()
    ]
    components.df_table(
        rows,
        columns=[
            "status",
            "precision",
            "recall",
            "f1",
            "support",
            "true_positives",
            "false_positives",
            "false_negatives",
        ],
        column_config={
            "status": st.column_config.TextColumn("Status", width="medium"),
            "precision": st.column_config.NumberColumn("Precision", format="%.2f", width="small"),
            "recall": st.column_config.NumberColumn("Recall", format="%.2f", width="small"),
            "f1": st.column_config.NumberColumn("F1", format="%.2f", width="small"),
            "support": st.column_config.NumberColumn("Support (n)", format="%d", width="small"),
            "true_positives": st.column_config.NumberColumn("True positives", format="%d", width="small"),
            "false_positives": st.column_config.NumberColumn("False positives", format="%d", width="small"),
            "false_negatives": st.column_config.NumberColumn("False negatives", format="%d", width="small"),
        },
        key="eval_per_class",
    )
    components.note(str(classification.get("zero_division_convention", "")))


def _render_confusion(metrics: Dict[str, Any]) -> None:
    matrix = dict(metrics.get("confusion_matrix") or {})
    if not matrix:
        return
    import plotly.graph_objects as go

    expected_tokens = list(matrix.keys())
    predicted_tokens = sorted({key for row in matrix.values() for key in row})
    values = [
        [int(matrix[expected].get(predicted, 0)) for predicted in predicted_tokens]
        for expected in expected_tokens
    ]

    components.section_header(
        "Confusion matrix",
        subtitle=str(metrics.get("confusion_matrix_orientation", "")),
    )
    figure = go.Figure(
        data=go.Heatmap(
            z=values,
            x=[_status_label(token) or str(token) for token in predicted_tokens],
            y=[_status_label(token) or str(token) for token in expected_tokens],
            colorscale=[[0.0, theme.SURFACE_ALT], [1.0, theme.ACCENT]],
            showscale=False,
            text=values,
            texttemplate="%{text}",
            hovertemplate="expected %{y}<br>predicted %{x}<br>count %{z}<extra></extra>",
        )
    )
    # The axes are styled through update_*axes rather than through plotly_layout
    # overrides, because an "xaxis" override would replace the theme's axis block
    # wholesale and take the gridline colours with it.
    theme.style_figure(figure, height=90 + 60 * max(1, len(expected_tokens)))
    # theme.plotly_layout supplies a title font but no title text, and plotly.js renders
    # a text-less title object as the literal string "undefined". The chart is titled by
    # the section header above it, so the title is cleared explicitly.
    figure.update_layout(title_text="")
    figure.update_xaxes(title_text="predicted")
    figure.update_yaxes(title_text="expected (ground truth)", autorange="reversed")
    st.plotly_chart(figure, key="eval_cm")
    st.caption(
        "Cells count datasets, not controls or documents. n = {0}.".format(metrics.get("n_scored", 0))
    )


def _render_deficiency(metrics: Dict[str, Any]) -> None:
    inclusive = dict(metrics.get("deficiency_detection") or {})
    exclusive = dict(metrics.get("deficiency_detection_excluding_insufficient") or {})
    if not inclusive and not exclusive:
        return
    insufficient = _status_label("INSUFFICIENT_EVIDENCE")
    components.section_header(
        "Deficiency detection",
        subtitle="The same predictions read as a binary question: did the system raise a deficiency?",
    )
    both = [
        ("'{0}' counted as 'no deficiency'".format(insufficient), inclusive),
        ("'{0}' excluded as an abstention".format(insufficient), exclusive),
    ]
    rows = []
    for framing, block in both:
        rows.append(
            {
                "framing": framing,
                "n": block.get("n", 0),
                "precision": block.get("precision"),
                "recall": block.get("recall"),
                "fpr": block.get("fpr"),
                "fnr": block.get("fnr"),
                "tp": block.get("true_positives", 0),
                "fp": block.get("false_positives", 0),
                "tn": block.get("true_negatives", 0),
                "fn": block.get("false_negatives", 0),
            }
        )
    components.df_table(
        rows,
        columns=["framing", "n", "precision", "recall", "fpr", "fnr", "tp", "fp", "tn", "fn"],
        column_config={
            "framing": st.column_config.TextColumn("Framing", width="large"),
            "n": st.column_config.NumberColumn("n", width="small", format="%d"),
            "precision": st.column_config.NumberColumn("Precision", format="%.2f", width="small"),
            "recall": st.column_config.NumberColumn("Recall", format="%.2f", width="small"),
            "fpr": st.column_config.NumberColumn(
                "FPR",
                format="%.2f",
                width="small",
                help="False positive rate: effective controls the system flagged as deficient, as a share of all effective controls.",
            ),
            "fnr": st.column_config.NumberColumn(
                "FNR",
                format="%.2f",
                width="small",
                help="False negative rate: deficient controls the system did not flag, as a share of all deficient controls.",
            ),
            "tp": st.column_config.NumberColumn("TP", width="small", format="%d", help="True positives"),
            "fp": st.column_config.NumberColumn("FP", width="small", format="%d", help="False positives"),
            "tn": st.column_config.NumberColumn("TN", width="small", format="%d", help="True negatives"),
            "fn": st.column_config.NumberColumn("FN", width="small", format="%d", help="False negatives"),
        },
        key="eval_deficiency",
    )
    definition = str(inclusive.get("definition") or exclusive.get("definition") or "")
    if definition:
        components.note(definition)
    components.note(
        "Report both framings. The first depresses recall by counting an honest "
        "'I cannot tell' as a missed deficiency; the second removes those rows entirely."
    )


def _render_evidence_metrics(metrics: Dict[str, Any]) -> None:
    evidence = dict(metrics.get("evidence") or {})
    if not evidence:
        return
    components.section_header(
        "Citation, grounding and missing-evidence detection",
        subtitle="Traceability measured mechanically, with every denominator named.",
    )
    components.metric_row(
        [
            {
                "label": "Citation rate",
                "value": _pct(evidence.get("citation_rate")),
                "caption": "{0} of n = {1} assessment(s) cited anything.".format(
                    evidence.get("assessments_with_citation", 0), evidence.get("n", 0)
                ),
            },
            {
                "label": "Grounding rate",
                "value": _pct(evidence.get("grounding_rate")),
                "caption": "verified / all citations; a partly matched quote counts as 0. n = {0} citation(s).".format(
                    evidence.get("citations_total", 0)
                ),
            },
            {
                "label": "Unsupported-claim rate",
                "value": _pct(evidence.get("unsupported_claim_rate")),
                "caption": "{0} claim(s) over n = {1} assessment(s).".format(
                    evidence.get("unsupported_claims_total", 0), evidence.get("n", 0)
                ),
            },
            {
                "label": "Hallucination rate",
                "value": _pct(evidence.get("hallucination_rate")),
                "caption": "Any fabricated citation or unsupported number. n = {0}.".format(
                    evidence.get("n", 0)
                ),
                "color": theme.verdict_color("FABRICATED"),
            },
            {
                "label": "Retrieval recall",
                "value": _pct(evidence.get("retrieval_recall")),
                "caption": "{0} of {1} marker(s) surfaced, over {2} dataset(s).".format(
                    evidence.get("retrieval_markers_hit", 0),
                    evidence.get("retrieval_markers_total", 0),
                    evidence.get("retrieval_datasets_scored", 0),
                ),
            },
            {
                "label": "Missing-evidence detection",
                "value": _pct(evidence.get("missing_evidence_detection_rate")),
                "caption": "{0} of {1} ground-truth '{2}' case(s).".format(
                    evidence.get("missing_evidence_detected", 0),
                    evidence.get("missing_evidence_cases", 0),
                    _status_label("INSUFFICIENT_EVIDENCE"),
                ),
                "color": theme.status_color("INSUFFICIENT_EVIDENCE"),
            },
        ],
        columns=6,
    )
    components.note(
        "Retrieval recall is measured against a handful of marker strings in projects "
        "holding two or three files, where top_k retrieves nearly everything. Read it as "
        "a ceiling, not as evidence that ranking works."
    )


def _render_latency(run: Dict[str, Any]) -> None:
    results = list(run.get("results") or [])
    timing = dict((run.get("metrics") or {}).get("timing") or {})
    if not results:
        return
    import plotly.graph_objects as go

    components.section_header(
        "Latency per dataset",
        subtitle="Wall clock around the whole pipeline, per assessed control.",
    )
    ordered = sorted(results, key=lambda row: str(row.get("dataset_id", "")))
    figure = go.Figure(
        data=go.Bar(
            x=[int(row.get("latency_ms", 0) or 0) for row in ordered],
            y=[str(row.get("dataset_id", "")) for row in ordered],
            orientation="h",
            marker={"color": theme.ACCENT},
            hovertemplate="%{y}: %{x} ms<extra></extra>",
        )
    )
    theme.style_figure(figure, height=80 + 34 * len(ordered))
    figure.update_layout(title_text="")  # see the note in _render_confusion
    figure.update_xaxes(title_text="milliseconds")
    figure.update_yaxes(title_text="", autorange="reversed")
    st.plotly_chart(figure, key="eval_latency")

    distribution = dict(timing.get("latency_ms") or {})
    tokens = dict(timing.get("total_tokens") or {})
    components.kv_grid(
        {
            "n timed": timing.get("n_timed", 0),
            "Mean / median": "{0} ms / {1} ms".format(
                _num(distribution.get("mean"), 0), _num(distribution.get("median"), 0)
            ),
            "Min / max": "{0} ms / {1} ms".format(
                _num(distribution.get("min"), 0), _num(distribution.get("max"), 0)
            ),
            "p95": "{0} ms".format(_num(distribution.get("p95"), 0)),
            "Mean total tokens": _num(tokens.get("mean"), 0),
        }
    )
    components.note(
        "With this few observations a 95th percentile is effectively the maximum. These "
        "timings compare pipeline configurations executing Python; against the offline "
        "provider they contain no model inference at all."
    )


def _render_results_table(run: Dict[str, Any]) -> None:
    results = list(run.get("results") or [])
    if not results:
        st.caption("This run recorded no per-dataset results.")
        return
    components.section_header(
        "Per-dataset results",
        subtitle="Expected against predicted, one row per dataset - the rows every metric above is computed from.",
    )
    rows = [
        {
            "dataset_id": row.get("dataset_id", ""),
            "control_ref": row.get("control_ref", ""),
            "expected_status": _status_label(row.get("expected_status")),
            "predicted_status": _status_label(row.get("predicted_status")),
            "status_correct": bool(row.get("status_correct")),
            "expected_risk": _risk_label(row.get("expected_risk")),
            "predicted_risk": _risk_label(row.get("predicted_risk")),
            "citation_count": int(row.get("citation_count", 0) or 0),
            "verified_citation_count": int(row.get("verified_citation_count", 0) or 0),
            "fabricated_citation_count": int(row.get("fabricated_citation_count", 0) or 0),
            "unsupported_claim_count": int(row.get("unsupported_claim_count", 0) or 0),
            "declared_missing_evidence": bool(row.get("declared_missing_evidence")),
            "expected_evidence_hits": int(row.get("expected_evidence_hits", 0) or 0),
            "expected_evidence_total": int(row.get("expected_evidence_total", 0) or 0),
            "latency_ms": int(row.get("latency_ms", 0) or 0),
            "error": row.get("error") or "",
        }
        for row in results
    ]
    frame = pd.DataFrame(rows)
    components.df_table(
        frame,
        columns=list(frame.columns),
        column_config={
            "dataset_id": st.column_config.TextColumn("Dataset", width="small"),
            "control_ref": st.column_config.TextColumn("Control", width="small"),
            "expected_status": st.column_config.TextColumn("Expected", width="medium"),
            "predicted_status": st.column_config.TextColumn("Predicted", width="medium"),
            "status_correct": st.column_config.CheckboxColumn("Correct", width="small"),
            "expected_risk": st.column_config.TextColumn("Expected risk", width="small"),
            "predicted_risk": st.column_config.TextColumn("Predicted risk", width="small"),
            "citation_count": st.column_config.NumberColumn("Citations", width="small", format="%d"),
            "verified_citation_count": st.column_config.NumberColumn("Verified", width="small", format="%d"),
            "fabricated_citation_count": st.column_config.NumberColumn("Fabricated", width="small", format="%d"),
            "unsupported_claim_count": st.column_config.NumberColumn("Unsupported claims", width="small", format="%d"),
            "declared_missing_evidence": st.column_config.CheckboxColumn("Named missing evidence", width="small"),
            "expected_evidence_hits": st.column_config.NumberColumn("Markers hit", width="small", format="%d"),
            "expected_evidence_total": st.column_config.NumberColumn("Markers total", width="small", format="%d"),
            "latency_ms": st.column_config.NumberColumn("Latency (ms)", width="small", format="%d"),
            "error": st.column_config.TextColumn("Error", width="medium"),
        },
        key="eval_results",
    )
    components.download_row(
        "Download these results (CSV)",
        frame.to_csv(index=False),
        "evaluation_run_{0}_results.csv".format(run.get("id")),
        mime="text/csv",
        key="eval_results_csv",
    )


def _render_run_detail(run_id: int) -> None:
    try:
        run = data_access.get_evaluation_run(run_id)
    except data_access.DataAccessError as exc:
        components.error_with_remedy("Could not load run {0}.".format(run_id), exc)
        return
    if run is None:
        st.warning("Run {0} no longer exists.".format(run_id))
        state.set_current_run(None)
        return

    config = dict(run.get("config") or {})
    llm_config = dict(config.get("llm") or {})
    components.section_header(
        "Run #{0} - {1}".format(run.get("id"), run.get("name") or "(unnamed)"),
        subtitle="{0} · {1} · {2}".format(
            _mode_label(run.get("experiment_mode")),
            _run_status_label(run.get("status")) or "status unknown",
            components.when(run.get("started_at")) or "start time unknown",
        ),
        eyebrow="Experiment result",
    )
    provider = str(run.get("llm_provider", "") or "")
    components.badges(
        components.mode_badge(run.get("experiment_mode")),
        components.plain_badge("{0} result(s)".format(run.get("result_count", 0))),
        components.plain_badge(
            "{0} / {1}".format(
                components.provider_display_name(provider) or provider or "provider unknown",
                run.get("llm_model", "") or "model unknown",
            ),
            theme.AI_COLOR if provider.lower() == "mock" else theme.ACCENT,
        ),
        components.plain_badge("{0:.1f}s".format(float(run.get("duration_seconds", 0.0) or 0.0))),
    )
    if provider.lower() == "mock":
        st.info(
            "DEMO MODE - this run was executed against the deterministic offline provider. "
            + components.MOCK_PROVIDER_NOTE
        )
    if llm_config.get("fell_back_to_mock"):
        st.error(
            "A remote provider was configured but unavailable, so the offline stand-in "
            "answered. Do not report this run as a model result."
        )

    metrics = dict(run.get("metrics") or {})
    _render_run_metrics(run)
    _render_per_class(metrics)
    _render_confusion(metrics)
    _render_deficiency(metrics)
    _render_evidence_metrics(metrics)
    _render_latency(run)
    _render_results_table(run)

    with st.expander("Run configuration as recorded (prompt version, seeds, thresholds)", expanded=False):
        st.code(data_access.to_json(config), language="json")
    with st.expander("Full metric payload", expanded=False):
        st.code(data_access.to_json(metrics), language="json")


# ---- recorded runs and the run picker
def _on_run_pick() -> None:
    """Selectbox callback: the picked run becomes the current one before the rerun."""
    value = st.session_state.get(_RUN_PICK_KEY)
    state.set_current_run(int(value) if value is not None else None)


def _run_picker_label(by_id: Dict[int, Dict[str, Any]], value: Any) -> str:
    """``format_func`` for the run picker; tolerates a value that is not a run id.

    Streamlit hands the picker its own option (an int). The testing harness hands the
    already-formatted string back instead, and a formatter that raised on it would take
    the whole page down for the sake of a label.
    """
    try:
        run_id = int(value)
    except (TypeError, ValueError):
        return str(value)
    return _run_option_label(by_id.get(run_id, {}), run_id)


def _render_runs(runs: List[Dict[str, Any]]) -> None:
    components.section_header(
        "Recorded runs",
        subtitle="The latest {0} runs stored in this database ({1} shown). Every run keeps its scored results and the configuration that produced them.".format(
            _RUNS_SHOWN, len(runs)
        ),
    )
    if not runs:
        components.empty_state(
            "No experiment has been run yet",
            "Next: pick the conditions and datasets above and press the Run button. Each "
            "run persists its scored results and configuration, so a figure can always be "
            "traced back.",
        )
        return

    table = [
        {
            "id": row.get("id"),
            "name": row.get("name", ""),
            "condition": _mode_short(row.get("experiment_mode")),
            "status": _run_status_label(row.get("status")),
            "n": (row.get("metrics") or {}).get("n_scored", row.get("result_count", 0)),
            "accuracy": ((row.get("metrics") or {}).get("classification") or {}).get("accuracy"),
            "llm_model": row.get("llm_model", ""),
            "duration_seconds": float(row.get("duration_seconds", 0.0) or 0.0),
            "started_at": row.get("started_at"),
        }
        for row in runs
    ]
    components.df_table(
        table,
        columns=[
            "id",
            "name",
            "condition",
            "status",
            "n",
            "accuracy",
            "llm_model",
            "duration_seconds",
            "started_at",
        ],
        column_config={
            "id": st.column_config.NumberColumn("Run", width="small", format="%d"),
            "name": st.column_config.TextColumn("Name", width="medium"),
            "condition": st.column_config.TextColumn(
                "Condition",
                width="small",
                help="A = {0}; B = {1}; C = {2}.".format(
                    _mode_label("A_RAW_LLM"), _mode_label("B_RAG"), _mode_label("C_RAG_WORKFLOW")
                ),
            ),
            "status": st.column_config.TextColumn("Status", width="small"),
            "n": st.column_config.NumberColumn("n scored", width="small", format="%d"),
            "accuracy": st.column_config.NumberColumn("Accuracy", width="small", format="%.2f"),
            "llm_model": st.column_config.TextColumn("Model", width="medium"),
            "duration_seconds": st.column_config.NumberColumn("Seconds", width="small", format="%.1f"),
            "started_at": st.column_config.DatetimeColumn("Started", width="medium", format="DD MMM YYYY HH:mm"),
        },
        key="eval_runs",
    )

    ids = [int(row["id"]) for row in runs if row.get("id") is not None]
    if not ids:
        return
    by_id = {int(row["id"]): row for row in runs if row.get("id") is not None}

    current: Optional[int] = state.current_run_id()
    if current not in ids:
        current = ids[0]
        state.set_current_run(current)
    # Reconcile the widget with the current run *before* the widget exists in this run:
    # a run that has just finished (or a stale id after a delete) must be what opens.
    # Writing the key after st.selectbox has been instantiated would raise.
    if st.session_state.get(_RUN_PICK_KEY) != current:
        st.session_state[_RUN_PICK_KEY] = current

    st.selectbox(
        "Open a run",
        options=ids,
        format_func=lambda value: _run_picker_label(by_id, value),
        key=_RUN_PICK_KEY,
        on_change=_on_run_pick,
        help="The newest run opens by default. Read its caveats before its numbers.",
    )
    st.markdown("---")
    _render_run_detail(int(current))


# ---- comparison across runs
def _render_comparison(runs: List[Dict[str, Any]]) -> None:
    components.section_header(
        "Compare conditions",
        subtitle="One row per run - the A/B/C table for the write-up.",
    )
    if len(runs) < 2:
        components.empty_state(
            "Two or more recorded runs are needed for a comparison",
            "Next: run the conditions you want to compare from the 'Run and results' tab, "
            "then come back here.",
        )
        return

    by_id = {int(row["id"]): row for row in runs if row.get("id") is not None}
    labels = {run_id: _run_option_label(row, run_id) for run_id, row in by_id.items()}
    default = list(labels)[:3]
    chosen = st.multiselect(
        "Runs to compare",
        options=list(labels),
        default=default,
        format_func=lambda value: labels.get(value, str(value)),
        key="eval_compare_pick",
    )
    if len(chosen) < 2:
        st.caption("Select at least two runs.")
        return

    try:
        rows = data_access.compare_evaluation_runs(chosen)
    except data_access.DataAccessError as exc:
        components.error_with_remedy("Could not build the comparison.", exc)
        return
    if not rows:
        st.caption("Nothing to compare.")
        return

    # Name the condition on every row. The runner keys its frame by mode but drops that
    # index on the way to records; the stored-metrics fallback carries a raw 'mode'.
    named: List[Dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        raw_mode = record.pop("mode", None)
        try:
            run_row = by_id.get(int(record.get("run_id")))
        except (TypeError, ValueError):
            run_row = None
        mode_value = (run_row or {}).get("experiment_mode", raw_mode)
        ordered: Dict[str, Any] = {"run_id": record.pop("run_id", None), "condition": _mode_label(mode_value)}
        for key in ("run_name", "name", "status"):
            if key in record:
                ordered[key] = record.pop(key)
        if "status" in ordered:
            ordered["status"] = _run_status_label(ordered["status"])
        ordered.update(record)
        named.append(ordered)

    frame = _clean_frame(named)
    display = frame.copy()
    for column in display.columns:
        if column in ("run_id", "n", "caveats", "results"):
            continue
        if pd.api.types.is_float_dtype(display[column]):
            display[column] = display[column].map(lambda value: _num(value, 3))
    components.df_table(
        display,
        columns=list(display.columns),
        column_config={
            "run_id": st.column_config.NumberColumn("Run", width="small", format="%d"),
            "condition": st.column_config.TextColumn("Condition", width="large"),
            "fpr": st.column_config.TextColumn("FPR", help="False positive rate.", width="small"),
            "fnr": st.column_config.TextColumn("FNR", help="False negative rate.", width="small"),
        },
        key="eval_compare",
    )
    sample = rows[0].get("n")
    per_row_points = 100.0 / float(sample) if sample else None
    components.note(
        "'n' is the number of scored datasets behind each row, and it is the same handful "
        "for every condition."
        + (
            " One dataset changing its answer moves accuracy by {0:.0f} percentage points, "
            "so treat any gap smaller than that as noise.".format(per_row_points)
            if per_row_points
            else ""
        )
    )
    components.download_row(
        "Download the comparison (CSV)",
        frame.to_csv(index=False),
        "evaluation_comparison.csv",
        mime="text/csv",
        key="eval_compare_csv",
    )


# ---- human/AI agreement, computed from real reviews
def _render_agreement() -> None:
    components.section_header(
        "Human/AI agreement",
        subtitle="Measured over every auditor decision recorded in this database, not over experiment runs.",
    )
    metrics_module = _metrics_module()
    try:
        reviews = data_access.list_reviews()
        assessments = data_access.list_assessments(limit=None)
    except data_access.DataAccessError as exc:
        components.error_with_remedy("Could not read the reviews.", exc)
        return

    by_id = {int(row["id"]): row for row in assessments}
    pairs = []
    for review in reviews:
        assessment = by_id.get(int(review.get("assessment_id", 0) or 0))
        if assessment is None:
            continue
        pairs.append(
            {
                "ai_status": assessment.get("status", ""),
                "ai_risk": assessment.get("risk_level", ""),
                "final_status": review.get("final_status", ""),
                "final_risk_level": review.get("final_risk_level", ""),
                "decision": review.get("decision", ""),
            }
        )

    if not pairs:
        components.empty_state(
            "No auditor decision has been recorded yet",
            "There is nothing to compare. The evaluation harness has no human in it: this "
            "block is the only place the study's human/AI concordance can come from. Next: "
            "record decisions in an audit project's Human review page.",
        )
        return
    if metrics_module is None:
        st.warning("app.evaluation.metrics is not importable, so agreement cannot be computed here.")
        return

    agreement = metrics_module.agreement_metrics(pairs)
    completed = int(agreement.get("n_completed", 0) or 0)
    components.metric_row(
        [
            {
                "label": "Completed reviews",
                "value": completed,
                "caption": "{0} review row(s) in total; those still awaiting a decision are excluded.".format(
                    agreement.get("n_reviews", 0)
                ),
            },
            {
                "label": "Status agreement",
                "value": _pct(agreement.get("status_agreement")),
                "caption": "n = {0} comparable pair(s).".format(agreement.get("n_status_pairs", 0)),
                "color": theme.HUMAN_COLOR,
            },
            {
                "label": "Cohen's kappa (status)",
                "value": _num(agreement.get("status_kappa")),
                "caption": "Undefined when both raters used one label.",
            },
            {
                "label": "Risk agreement",
                "value": _pct(agreement.get("risk_agreement")),
                "caption": "n = {0} pair(s).".format(agreement.get("n_risk_pairs", 0)),
                "color": theme.HUMAN_COLOR,
            },
            {
                "label": "Cohen's kappa (risk)",
                "value": _num(agreement.get("risk_kappa")),
            },
            {
                "label": "Modification rate",
                "value": _pct(agreement.get("modification_rate")),
                "caption": "Decisions recorded as '{0}'.".format(components.label("decision", "MODIFIED")),
            },
        ],
        columns=6,
    )
    counts = dict(agreement.get("decision_counts") or {})
    if counts:
        components.badges(
            *[
                components.plain_badge(
                    "{0}: {1}".format(components.label("decision", key) or str(key), value)
                )
                for key, value in counts.items()
            ]
        )
    for key, text in (agreement.get("definitions") or {}).items():
        components.note("{0}: {1}".format(key, text))
    if completed < 10:
        components.note(
            "Kappa over {0} review(s) is not a reliability estimate. It is reported "
            "because the study requires the statistic, and it is reported with its n so "
            "nobody mistakes it for one.".format(completed)
        )


# ---- page
def render() -> None:
    components.section_header(
        "Research experiments",
        subtitle=(
            "Runs the A/B/C benchmark on synthetic datasets with known answers. It does "
            "not use the audit selected in the sidebar and never touches audit data."
        ),
        eyebrow="Research",
    )

    try:
        datasets = data_access.list_datasets()
    except data_access.DataAccessError as exc:
        components.error_with_remedy("Could not read the dataset catalogue.", exc)
        datasets = []

    try:
        runs = data_access.list_evaluation_runs(limit=_RUNS_SHOWN)
    except data_access.DataAccessError as exc:
        components.error_with_remedy("Could not read past runs.", exc)
        runs = []

    tab_run, tab_compare, tab_datasets, tab_agreement = st.tabs(
        ["Run and results", "Compare runs", "Datasets", "Human/AI agreement"]
    )
    with tab_run:
        _render_runner(datasets)
        st.markdown("---")
        _render_runs(runs)
    with tab_compare:
        _render_comparison(runs)
    with tab_datasets:
        _render_datasets(datasets)
    with tab_agreement:
        _render_agreement()


if __name__ == "__main__":
    render()
