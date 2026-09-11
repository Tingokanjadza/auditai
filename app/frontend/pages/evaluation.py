"""The research dashboard: the synthetic suite, the A/B/C experiments and their metrics.

This page exists to make a claim checkable, not to make one look good. Three rules are
enforced by its layout rather than left to the reader's care.

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
"""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence

import pandas as pd
import streamlit as st

from app.frontend import components, data_access, state, theme

#: Extensions whose bytes are text a reviewer can read directly in the browser. Anything
#: else (pdf, docx, xlsx) is reported by name and size only - a hex dump of a .docx would
#: tell a reader nothing about whether the generated evidence is right.
_PREVIEWABLE = {".csv", ".txt", ".md", ".json"}

_PREVIEW_CHARS = 3000

_SUITE_NOTE = (
    "Six synthetic datasets, each authored so that the correct conclusion is known in "
    "advance. They describe no real organisation, system or person. DATASET-006 is an "
    "addition beyond the five mandated cases: without a control that is genuinely "
    "effective there is no true-negative class, and a system that answered "
    "'deficiency' unconditionally would score perfectly on the other five."
)


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


# ---- the dataset catalogue
def _render_datasets(datasets: List[Dict[str, Any]]) -> None:
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
            "expected_status": row.get("expected_status", ""),
            "expected_risk": row.get("expected_risk", ""),
            "mandated": bool(row.get("mandated")),
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
            "mandated",
            "files",
            "exception_ratio",
            "markers",
        ],
        column_config={
            "dataset_id": st.column_config.TextColumn("Dataset", width="small"),
            "expected_status": st.column_config.TextColumn("Ground truth status", width="medium"),
            "expected_risk": st.column_config.TextColumn("Ground truth risk", width="small"),
            "mandated": st.column_config.CheckboxColumn("Mandated case", width="small"),
            "files": st.column_config.NumberColumn("Files", width="small", format="%d"),
            "exception_ratio": st.column_config.NumberColumn(
                "Exception rate", width="small", format="%.2f"
            ),
            "markers": st.column_config.NumberColumn("Retrieval markers", width="small", format="%d"),
        },
        key="eval_datasets",
    )

    module = _datasets_module()
    generate_col, note_col = st.columns([1, 3], gap="small")
    with generate_col:
        if st.button(
            "Generate all dataset files",
            key="eval_gen_all",
            disabled=module is None,
            help="Writes the synthetic evidence files to disk. Deterministic: same bytes every time.",
        ):
            _generate([row["dataset_id"] for row in datasets])
    with note_col:
        if module is None:
            st.caption(
                "File generation is available only when the console runs in-process; the "
                "API exposes no endpoint that writes files to the server's filesystem."
            )
        else:
            st.caption(
                "Generation is seeded and idempotent, so regenerating produces byte-identical "
                "files. The evaluation runner generates whatever it needs on its own - this "
                "button exists so the files can be inspected before a run."
            )

    for row in datasets:
        with st.expander(
            "{0} · {1} → {2}".format(
                row.get("dataset_id", ""), row.get("name", ""), row.get("expected_status", "")
            ),
            expanded=False,
        ):
            _render_dataset_detail(row)


def _render_dataset_detail(dataset: Dict[str, Any]) -> None:
    components.badges(
        components.plain_badge(str(dataset.get("dataset_id", ""))),
        components.status_badge(dataset.get("expected_status")),
        components.risk_badge(dataset.get("expected_risk")),
        components.plain_badge(
            "mandated case" if dataset.get("mandated") else "added beyond the mandated five",
            theme.ACCENT if dataset.get("mandated") else theme.VIOLET,
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
                    "evidence_type": item.get("evidence_type", ""),
                    "role": item.get("role", ""),
                    "description": item.get("description", ""),
                }
                for item in files
            ],
            columns=["filename", "evidence_type", "role", "description"],
            column_config={
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
            st.markdown("- `{0}`".format(marker.replace("`", "'")))

    module = _datasets_module()
    if module is None:
        return
    dataset_id = str(dataset.get("dataset_id", ""))
    if st.button("Generate and preview this dataset", key="eval_gen_{0}".format(dataset_id)):
        _generate([dataset_id], preview=True)

    for path_text in st.session_state.get("eval_generated_{0}".format(dataset_id), []) or []:
        _render_file_preview(Path(path_text))


def _generate(dataset_ids: Sequence[str], preview: bool = False) -> None:
    """Write the synthetic files to disk and remember the paths for previewing."""
    module = _datasets_module()
    if module is None:
        st.error("The dataset generator is not available in this build.")
        return
    written: List[str] = []
    with st.spinner("Generating synthetic evidence files…"):
        for dataset_id in dataset_ids:
            try:
                paths = module.generate_dataset(dataset_id)
            except Exception as exc:  # noqa: BLE001 - one bad dataset must not stop the rest
                st.error("{0} could not be generated: {1}".format(dataset_id, exc))
                continue
            texts = [str(path) for path in paths]
            written.extend(texts)
            if preview:
                st.session_state["eval_generated_{0}".format(dataset_id)] = texts
    if written:
        st.success(
            "Wrote {0} file(s). All of it is fabricated research data and represents no "
            "real organisation.".format(len(written))
        )


def _render_file_preview(path: Path) -> None:
    """Show a generated file so the planted condition can be checked by eye."""
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
    with st.expander("{0} - {1:.1f} KB".format(path.name, size / 1024.0), expanded=False):
        st.code(text[:_PREVIEW_CHARS], language=None)
        if len(text) > _PREVIEW_CHARS:
            st.caption(
                "First {0} of {1} characters.".format(_PREVIEW_CHARS, len(text))
            )


# ---- running experiments
def _render_runner(datasets: List[Dict[str, Any]]) -> None:
    availability = data_access.evaluation_available()
    components.section_header(
        "Run an experiment",
        subtitle="Each run creates an isolated project per dataset, ingests its files and assesses the control.",
    )
    if not availability.get("available"):
        st.warning(
            "Experiments cannot be launched from this build: {0}".format(
                availability.get("reason", "the evaluation runner is unavailable")
            )
        )
        return

    ids = [row["dataset_id"] for row in datasets]
    modes = data_access.experiment_modes()
    mode_values = [item["value"] for item in modes]
    mode_labels = {item["value"]: item["label"] for item in modes}

    left, right = st.columns([2, 2], gap="medium")
    with left:
        chosen_modes = st.multiselect(
            "Experimental conditions",
            options=mode_values,
            default=mode_values,
            format_func=lambda value: mode_labels.get(value, value),
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
        placeholder="e.g. baseline-2024-06-30",
        help="Stored on the run so a figure in the write-up can be traced back to it.",
    )

    st.caption(
        "Runs execute synchronously in this process. Against the offline provider the "
        "whole suite takes seconds; against a hosted model it takes minutes, and the "
        "browser must stay open. The evaluation projects are kept afterwards so the "
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
                mode, index, len(modes), time.perf_counter() - started
            )
        )
        try:
            run = data_access.run_evaluation(
                mode=mode,
                dataset_ids=list(dataset_ids),
                run_name="{0} [{1}]".format(run_name, mode) if run_name else "",
            )
            completed.append("{0} → run #{1} ({2})".format(mode, run.get("id"), run.get("status", "")))
            state.set_current_run(int(run.get("id")) if run.get("id") is not None else None)
        except data_access.DataAccessError as exc:
            failed.append("{0}: {1}".format(mode, exc))
        progress.progress(index / float(len(modes)))

    progress.empty()
    line.empty()
    elapsed = time.perf_counter() - started
    for message in completed:
        state.flash(message, "success")
    for message in failed:
        state.flash(message, "error")
    state.flash(
        "{0} condition(s) finished in {1:.1f}s. Progress is reported per condition: a run "
        "is one blocking call and cannot report per-dataset progress from inside it.".format(
            len(completed), elapsed
        ),
        "info",
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
            "status": label,
            "precision": cell.get("precision"),
            "recall": cell.get("recall"),
            "f1": cell.get("f1"),
            "support": cell.get("support"),
            "true_positives": cell.get("true_positives"),
            "false_positives": cell.get("false_positives"),
            "false_negatives": cell.get("false_negatives"),
        }
        for label, cell in per_class.items()
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
        },
        key="eval_per_class",
    )
    components.note(str(classification.get("zero_division_convention", "")))


def _render_confusion(metrics: Dict[str, Any]) -> None:
    matrix = dict(metrics.get("confusion_matrix") or {})
    if not matrix:
        return
    import plotly.graph_objects as go

    labels = list(matrix.keys())
    predicted_labels = sorted({key for row in matrix.values() for key in row})
    values = [[int(matrix[expected].get(predicted, 0)) for predicted in predicted_labels] for expected in labels]

    components.section_header(
        "Confusion matrix",
        subtitle=str(metrics.get("confusion_matrix_orientation", "")),
    )
    figure = go.Figure(
        data=go.Heatmap(
            z=values,
            x=predicted_labels,
            y=labels,
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
    theme.style_figure(figure, height=90 + 60 * max(1, len(labels)))
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
    components.section_header(
        "Deficiency detection",
        subtitle="The same predictions read as a binary question: did the system raise a deficiency?",
    )
    both = [
        ("INSUFFICIENT_EVIDENCE counted as 'no deficiency'", metrics.get("deficiency_detection") or {}),
        (
            "INSUFFICIENT_EVIDENCE excluded as an abstention",
            metrics.get("deficiency_detection_excluding_insufficient") or {},
        ),
    ]
    rows = []
    for label, block in both:
        rows.append(
            {
                "framing": label,
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
            "fpr": st.column_config.NumberColumn("FPR", format="%.2f", width="small"),
            "fnr": st.column_config.NumberColumn("FNR", format="%.2f", width="small"),
        },
        key="eval_deficiency",
    )
    definition = str((metrics.get("deficiency_detection") or {}).get("definition", ""))
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
                "caption": "verified / all citations; PARTIAL counts as 0. n = {0} citation(s).".format(
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
                "caption": "{0} of {1} ground-truth INSUFFICIENT case(s).".format(
                    evidence.get("missing_evidence_detected", 0),
                    evidence.get("missing_evidence_cases", 0),
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
            "expected_status": row.get("expected_status", ""),
            "predicted_status": row.get("predicted_status", ""),
            "status_correct": bool(row.get("status_correct")),
            "expected_risk": row.get("expected_risk", ""),
            "predicted_risk": row.get("predicted_risk", ""),
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
            "expected_status": st.column_config.TextColumn("Expected", width="medium"),
            "predicted_status": st.column_config.TextColumn("Predicted", width="medium"),
            "status_correct": st.column_config.CheckboxColumn("Correct", width="small"),
            "declared_missing_evidence": st.column_config.CheckboxColumn("Named missing evidence", width="small"),
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
        st.error("Could not load run {0}: {1}".format(run_id, exc))
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
            run.get("mode_label", run.get("experiment_mode", "")),
            run.get("status", ""),
            run.get("started_at", ""),
        ),
        eyebrow="Experiment result",
    )
    components.badges(
        components.mode_badge(run.get("experiment_mode")),
        components.plain_badge("{0} result(s)".format(run.get("result_count", 0))),
        components.plain_badge(
            "{0} / {1}".format(run.get("llm_provider", ""), run.get("llm_model", "")),
            theme.AI_COLOR if str(run.get("llm_provider", "")) == "mock" else theme.ACCENT,
        ),
        components.plain_badge("{0:.1f}s".format(float(run.get("duration_seconds", 0.0) or 0.0))),
    )
    if str(run.get("llm_provider", "")).lower() == "mock":
        st.info(
            "This run was executed against the deterministic offline provider. "
            + components.MOCK_PROVIDER_NOTE
        )
    if llm_config.get("fell_back_to_mock"):
        st.warning(
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


# ---- comparison across runs
def _render_comparison(runs: List[Dict[str, Any]]) -> None:
    if len(runs) < 2:
        st.caption(
            "Two or more recorded runs are needed for a comparison. Run the conditions "
            "you want to compare from the panel above."
        )
        return

    labels = {
        int(row["id"]): "#{0} · {1} · {2}".format(
            row["id"], row.get("experiment_mode", ""), row.get("name") or "unnamed"
        )
        for row in runs
    }
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
        st.error("Could not build the comparison: {0}".format(exc))
        return
    if not rows:
        st.caption("Nothing to compare.")
        return

    frame = _clean_frame(rows)
    display = frame.copy()
    for column in display.columns:
        if column in ("run_id", "n", "caveats"):
            continue
        if pd.api.types.is_float_dtype(display[column]):
            display[column] = display[column].map(lambda value: _num(value, 3))
    components.df_table(display, columns=list(display.columns), key="eval_compare")
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
        subtitle="Measured over the auditor decisions recorded in this database, not over experiment runs.",
    )
    metrics_module = _metrics_module()
    try:
        reviews = data_access.list_reviews()
        assessments = data_access.list_assessments(limit=None)
    except data_access.DataAccessError as exc:
        st.error("Could not read the reviews: {0}".format(exc))
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
        st.caption(
            "No auditor decision has been recorded yet, so there is nothing to compare. "
            "The evaluation harness has no human in it: this block is the only place the "
            "study's human/AI concordance can come from."
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
                "caption": "{0} review row(s) in total; PENDING excluded.".format(
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
                "caption": "Decisions recorded as MODIFIED.",
            },
        ],
        columns=6,
    )
    counts = dict(agreement.get("decision_counts") or {})
    if counts:
        components.badges(
            *[components.plain_badge("{0}: {1}".format(key, value)) for key, value in counts.items()]
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
        "Evaluation",
        subtitle="The synthetic suite, the A/B/C experiments, and every metric with its sample size.",
        eyebrow="Research",
    )
    st.info(
        "Research harness. Runs on this page create throwaway audit projects for the "
        "synthetic datasets, and their assessments are excluded from the operational "
        "figures on the Dashboard and in reports."
    )

    try:
        datasets = data_access.list_datasets()
    except data_access.DataAccessError as exc:
        st.error("Could not read the dataset catalogue: {0}".format(exc))
        datasets = []

    if datasets:
        _render_datasets(datasets)
    st.markdown("---")
    if datasets:
        _render_runner(datasets)

    st.markdown("---")
    try:
        runs = data_access.list_evaluation_runs()
    except data_access.DataAccessError as exc:
        st.error("Could not read past runs: {0}".format(exc))
        return

    components.section_header(
        "Recorded runs", subtitle="{0} run(s) stored in this database.".format(len(runs))
    )
    if not runs:
        components.empty_state(
            "No experiment has been run yet",
            "Run one or more conditions above. Each run persists its scored results and "
            "the configuration that produced them, so a figure can always be traced back.",
        )
        return

    table = [
        {
            "id": row.get("id"),
            "name": row.get("name", ""),
            "experiment_mode": row.get("experiment_mode", ""),
            "status": row.get("status", ""),
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
            "experiment_mode",
            "status",
            "n",
            "accuracy",
            "llm_model",
            "duration_seconds",
            "started_at",
        ],
        column_config={
            "n": st.column_config.NumberColumn("n scored", width="small", format="%d"),
            "accuracy": st.column_config.NumberColumn("Accuracy", width="small", format="%.2f"),
            "duration_seconds": st.column_config.NumberColumn("Seconds", width="small", format="%.1f"),
            "started_at": st.column_config.DatetimeColumn("Started", width="medium", format="YYYY-MM-DD HH:mm"),
        },
        key="eval_runs",
    )

    ids = [int(row["id"]) for row in runs if row.get("id") is not None]
    if ids:
        current = state.current_run_id()
        if current not in ids:
            current = ids[0]
        # Keyed by the current run so a run that has just finished is the one opened: a
        # fixed key would make Streamlit return the widget's remembered value and ignore
        # ``index``, leaving the previous run on screen.
        chosen = st.selectbox(
            "Open a run",
            options=ids,
            index=ids.index(current),
            format_func=lambda value: "#{0} · {1}".format(
                value,
                next((row.get("experiment_mode", "") for row in runs if int(row["id"]) == value), ""),
            ),
            key="eval_open_run_{0}".format(current),
        )
        if int(chosen) != int(current):
            state.set_current_run(int(chosen))
            st.rerun()
        state.set_current_run(int(current))
        st.markdown("---")
        _render_run_detail(int(current))

    st.markdown("---")
    components.section_header(
        "Compare conditions", subtitle="One row per run - the A/B/C table for the write-up."
    )
    _render_comparison(runs)

    st.markdown("---")
    _render_agreement()


render()
