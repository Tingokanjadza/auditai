"""Evidence - ingest artefacts and read what the parser actually stored.

Why the chunk browser is the important half of this page
--------------------------------------------------------
A citation in this system points at an ``EvidenceChunk``: a chunk id, a locator, and the
exact text the validator matched a quotation against. When an auditor checks whether the
model quoted the evidence faithfully, *this* is the ground truth they are checking
against - not the original PDF, which the pipeline never re-reads after ingestion. So the
chunk browser is not a debugging view. It is the working paper: it shows every stored
chunk with its locator, lets the auditor search inside them, and lets them jump straight
to a chunk id copied from a citation, including one that belongs to a different file in
the engagement.

What the upload panel reports
-----------------------------
Ingestion can partly fail: a file may store but not parse, parse but produce no chunks,
or parse with warnings from the extractor. ``ingest_file`` records all of those on the
row rather than raising, so the upload result here prints the parse status, the chunk,
page and row counts and every warning. A file that produced no chunks is invisible to
retrieval no matter how relevant it looks in the list, and the auditor needs to be told
so at the moment they upload it.

Deleting evidence is an audit-relevant act
------------------------------------------
Deleting a file removes its chunks. Citations that pointed at those chunks survive with
an unresolved chunk id - the same signal a fabricated citation produces - so the
confirmation says exactly that before the button is enabled.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from typing import Any, Callable, Dict, List, Optional, Sequence  # noqa: E402

import streamlit as st  # noqa: E402

from app.frontend import components, data_access, state, theme  # noqa: E402

PAGE = "evidence"

#: Fallback when the settings facade cannot be read; the real list comes from
#: ``settings_summary()['limits']['supported_upload_extensions']``.
_FALLBACK_EXTENSIONS = [".csv", ".docx", ".json", ".md", ".pdf", ".txt", ".xls", ".xlsx"]

_PAGE_SIZES = [10, 25, 50, 100]

#: Key of the file selector. Named because the chunk browser has to write it (see
#: ``render``) to follow a citation into a different file.
_SELECT_KEY = "evidence_detail_select"

_TABLE_COLUMNS = [
    "filename",
    "evidence_type",
    "size_kb",
    "sha256_short",
    "uploaded_at",
    "parse_status",
    "page_count",
    "row_count",
    "chunk_count",
]


def _read(loader: Callable[[], Any], fallback: Any, what: str) -> Any:
    try:
        return loader()
    except data_access.DataAccessError as exc:
        st.error("Could not load {0}: {1}".format(what, exc))
        return fallback


def _extensions() -> List[str]:
    summary = _read(data_access.settings_summary, {}, "the upload limits")
    limits = dict(summary.get("limits", {}) or {})
    raw = list(limits.get("supported_upload_extensions", []) or _FALLBACK_EXTENSIONS)
    return sorted(str(item).lstrip(".").lower() for item in raw)


def _max_upload_mb() -> Any:
    summary = _read(data_access.settings_summary, {}, "the upload limits")
    return dict(summary.get("limits", {}) or {}).get("max_upload_mb", "?")


# ---- upload
def _upload_panel(project_id: int, expanded: bool) -> None:
    with st.expander("Upload evidence", expanded=expanded):
        st.caption(
            "Files are stored, hashed, parsed into locator-bearing chunks and indexed for "
            "retrieval in one step. Accepted: {0}. Limit {1} MB per file.".format(
                ", ".join(_extensions()), _max_upload_mb()
            )
        )
        # ``clear_on_submit`` matters here: an uploader keeps its files across reruns, so
        # without it the next interaction on the page would ingest the same file again.
        with st.form("evidence_upload_{0}".format(project_id), clear_on_submit=True):
            files = st.file_uploader(
                "Evidence files",
                type=_extensions(),
                accept_multiple_files=True,
                key="evidence_uploader_{0}".format(project_id),
            )
            left, right = st.columns([1, 2], gap="small")
            with left:
                evidence_type = st.selectbox(
                    "Evidence type",
                    options=data_access.evidence_types(),
                    index=data_access.evidence_types().index("OTHER"),
                    help=(
                        "Applied to every file in this submission. The type is recorded on "
                        "the row and helps retrieval tell a policy from a configuration "
                        "export."
                    ),
                )
            with right:
                description = st.text_input(
                    "Description",
                    placeholder="What this artefact is, where it came from, and as at when.",
                )
            synthetic = st.checkbox(
                "Mark as synthetic (generated for research use, represents no real organisation)",
                value=False,
            )
            submitted = st.form_submit_button("Ingest", type="primary")

        if submitted:
            _ingest(project_id, files or [], evidence_type, description, synthetic)


def _ingest(
    project_id: int,
    files: Sequence[Any],
    evidence_type: str,
    description: str,
    synthetic: bool,
) -> None:
    if not files:
        st.warning("Choose at least one file before pressing Ingest.")
        return
    results: List[Dict[str, Any]] = []
    failures: List[Dict[str, str]] = []
    with st.spinner("Storing, parsing, chunking and indexing…"):
        for item in files:
            try:
                record = data_access.upload_evidence(
                    project_id,
                    item.getvalue(),
                    item.name,
                    evidence_type=evidence_type,
                    description=description.strip(),
                    uploaded_by=state.auditor_name(),
                    is_synthetic=bool(synthetic),
                )
                results.append(record)
            except data_access.DataAccessError as exc:
                failures.append({"filename": getattr(item, "name", "?"), "error": str(exc)})

    for failure in failures:
        st.error("{0} was not ingested: {1}".format(failure["filename"], failure["error"]))
    if results:
        components.section_header(
            "Ingestion result",
            subtitle="What the parser made of each file. Chunks are what retrieval can find.",
            eyebrow="Upload",
        )
    for record in results:
        _ingest_result(record)
    if results:
        state.set_current_evidence(int(results[-1].get("id")))


def _ingest_result(record: Dict[str, Any]) -> None:
    chunks = int(record.get("chunk_count", 0) or 0)
    warnings = list((record.get("extra_metadata") or {}).get("warnings", []) or [])
    components.badges(
        components.plain_badge(str(record.get("filename", "")), theme.ACCENT),
        components.parse_badge(record.get("parse_status")),
        components.plain_badge(str(record.get("evidence_type", ""))),
        components.plain_badge(
            "{0} chunk(s)".format(chunks), theme.GREEN if chunks else theme.RED
        ),
        components.plain_badge("{0} page(s)".format(record.get("page_count", 0))),
        components.plain_badge("{0} row(s)".format(record.get("row_count", 0))),
        components.plain_badge("sha {0}".format(str(record.get("sha256", ""))[:12])),
    )
    if not chunks:
        st.error(
            "No chunk was stored for this file, so nothing in it can be retrieved or "
            "cited. {0}".format(record.get("parse_error") or "")
        )
    elif record.get("parse_error"):
        st.warning(str(record.get("parse_error")))
    for warning in warnings:
        st.warning("Parser warning: {0}".format(warning))


# ---- listing
def _filters() -> Dict[str, Any]:
    left, middle, right = st.columns([2, 2, 3], gap="small")
    with left:
        evidence_type = st.selectbox(
            "Evidence type", options=["Any"] + data_access.evidence_types(), index=0, key="evidence_type_filter"
        )
    with middle:
        parse_status = st.selectbox(
            "Parse status", options=["Any"] + _parse_statuses(), index=0, key="evidence_parse_filter"
        )
    with right:
        search = st.text_input(
            "Search filenames",
            value=str(state.get_filter(PAGE, "search", "") or ""),
            key="evidence_search",
        )
    state.set_filter(PAGE, "type", evidence_type)
    state.set_filter(PAGE, "search", search)
    return {
        "evidence_type": None if evidence_type == "Any" else evidence_type,
        "parse_status": None if parse_status == "Any" else parse_status,
        "search": search.strip(),
    }


def _parse_statuses() -> List[str]:
    from app.schemas.enums import ParseStatus

    return ParseStatus.values()


def _evidence_table(files: Sequence[Dict[str, Any]]) -> None:
    components.df_table(
        files,
        columns=_TABLE_COLUMNS,
        column_config={
            # Nine columns have to fit a 1280px screen, so the two widest defaults are
            # narrowed here rather than left to scroll off the right-hand edge.
            "filename": st.column_config.TextColumn("File", width="medium"),
            "evidence_type": st.column_config.TextColumn("Type", width="small"),
            "sha256_short": st.column_config.TextColumn(
                "SHA-256",
                width="small",
                help=(
                    "First 12 hex characters. Select a cell to copy it; the full digest, "
                    "with a copy button, is in the detail panel below."
                ),
            ),
            "uploaded_at": st.column_config.DatetimeColumn(
                "Uploaded", width="small", format="YYYY-MM-DD HH:mm"
            ),
            "parse_status": st.column_config.TextColumn("Parsed", width="small"),
            "page_count": st.column_config.NumberColumn("Pages", width="small", format="%d"),
            "row_count": st.column_config.NumberColumn("Rows", width="small", format="%d"),
        },
        empty_message="No evidence matches these filters.",
    )


# ---- detail and chunk browser
def _detail(evidence_file_id: int) -> None:
    evidence = _read(
        lambda: data_access.get_evidence(evidence_file_id), None, "the evidence record"
    )
    if not evidence:
        st.warning("That evidence file no longer exists.")
        state.set_current_evidence(None)
        return

    components.evidence_provenance_panel(evidence)
    st.caption("SHA-256 of the stored bytes - the integrity anchor of this evidence trail:")
    st.code(str(evidence.get("sha256", "")), language=None)

    warnings = list((evidence.get("extra_metadata") or {}).get("warnings", []) or [])
    for warning in warnings:
        st.warning("Parser warning: {0}".format(warning))
    if evidence.get("parse_error"):
        st.error(str(evidence.get("parse_error")))

    chunks = _read(
        lambda: data_access.list_chunks(evidence_file_id), [], "the parsed chunks"
    )
    _chunk_browser(evidence, chunks)
    _delete_panel(evidence)


def _chunk_browser(evidence: Dict[str, Any], chunks: Sequence[Dict[str, Any]]) -> None:
    components.section_header(
        "Parsed chunks",
        subtitle="Exactly what is stored, and what a citation is checked against.",
        eyebrow="Ground truth",
    )
    if not chunks:
        components.empty_state(
            "Nothing was stored for this file",
            "The parser produced no chunk, so no part of this artefact can be retrieved "
            "or cited. Check the parse status and warnings above.",
        )
        return

    source_types = sorted({str(chunk.get("source_type", "")) for chunk in chunks if chunk.get("source_type")})
    jump_key = "chunk_jump_{0}".format(evidence.get("id"))
    # A jump that crossed into this file carries the chunk id with it. Seeding the
    # widget's key has to happen before the widget exists, which is why it is done here
    # and not where the jump was requested.
    pending = state.get_filter(PAGE, "pending_focus", None)
    if pending is not None:
        st.session_state[jump_key] = str(pending)
        state.set_filter(PAGE, "pending_focus", None)

    top = st.columns([3, 2, 2, 1], gap="small")
    with top[0]:
        needle = st.text_input(
            "Search within these chunks",
            value=str(state.get_filter(PAGE, "chunk_search", "") or ""),
            placeholder="A value, a column heading, a phrase from a policy",
            key="chunk_search_{0}".format(evidence.get("id")),
        )
    with top[1]:
        kind = st.selectbox(
            "Chunk kind",
            options=["Any"] + source_types,
            index=0,
            key="chunk_kind_{0}".format(evidence.get("id")),
            help="How the parser produced the chunk: a PDF page, a table row range, a table summary…",
        )
    with top[2]:
        target = st.text_input(
            "Jump to chunk id",
            placeholder="e.g. 42",
            key=jump_key,
            help="Paste the chunk id from a citation. Ids in other files of this engagement are found too.",
        )
    with top[3]:
        page_size = st.selectbox(
            "Per page",
            options=_PAGE_SIZES,
            index=1,
            key="chunk_page_size_{0}".format(evidence.get("id")),
        )
    state.set_filter(PAGE, "chunk_search", needle)

    focus = _resolve_jump(target, chunks)

    filtered = _filter_chunks(chunks, needle, kind)
    if not filtered:
        st.caption(
            "No chunk in this file matches that search. {0} chunk(s) are stored.".format(len(chunks))
        )
        return

    total_pages = max(1, (len(filtered) + int(page_size) - 1) // int(page_size))
    page_key = "chunk_page_{0}".format(evidence.get("id"))
    # A keyed widget ignores its ``value`` argument once session state holds one, so the
    # page has to be moved by writing the key *before* the widget is created - both to
    # follow a jump and to keep a remembered page inside a narrowed result set.
    stored = st.session_state.get(page_key)
    if isinstance(stored, int) and stored > total_pages:
        st.session_state[page_key] = total_pages
    if focus is not None:
        positions = [
            index
            for index, chunk in enumerate(filtered)
            if int(chunk.get("chunk_id", -1)) == focus
        ]
        if positions:
            st.session_state[page_key] = positions[0] // int(page_size) + 1
        else:
            st.info(
                "Chunk {0} is in this file but is filtered out by the search or chunk-kind "
                "filter above.".format(focus)
            )
    page_number = 1
    if total_pages > 1:
        page_number = int(
            st.number_input(
                "Page",
                min_value=1,
                max_value=total_pages,
                value=int(st.session_state.get(page_key, 1) or 1),
                step=1,
                key=page_key,
            )
        )
    st.caption(
        "Showing {0} of {1} stored chunk(s), page {2} of {3}.".format(
            len(filtered), len(chunks), page_number, total_pages
        )
    )

    start = (page_number - 1) * int(page_size)
    for chunk in filtered[start : start + int(page_size)]:
        _chunk_card(chunk, needle, focus)


def _resolve_jump(target: str, chunks: Sequence[Dict[str, Any]]) -> Optional[int]:
    """Resolve a chunk id typed by the auditor, following it into another file if needed."""
    text = str(target or "").strip()
    if not text:
        return None
    if not text.isdigit():
        st.warning("A chunk id is a whole number, as printed on a citation.")
        return None
    chunk_id = int(text)
    if any(int(chunk.get("chunk_id", -1)) == chunk_id for chunk in chunks):
        return chunk_id

    context = _read(
        lambda: data_access.chunk_context(chunk_id, window=0), {}, "that chunk"
    )
    if not context.get("found"):
        st.warning(
            "No chunk {0} exists in this database. A citation pointing at it would be "
            "unresolved - which is exactly what a fabricated citation looks like.".format(chunk_id)
        )
        return None
    other_file = context.get("evidence_file_id")
    st.info(
        "Chunk {0} belongs to {1}, not to this file.".format(
            chunk_id, context.get("filename", "another file")
        )
    )
    if other_file is not None and st.button(
        "Open {0}".format(context.get("filename", "that file")),
        key="chunk_follow_{0}".format(chunk_id),
    ):
        # The file selector for this page was already drawn in this run, and a widget's
        # stored value cannot be changed after it exists. The request is therefore parked
        # and consumed at the top of the next run, before the selector is created.
        state.set_filter(PAGE, "pending_open", int(other_file))
        state.set_filter(PAGE, "pending_focus", int(chunk_id))
        state.set_current_evidence(int(other_file))
        state.flash("Opened {0} at chunk {1}.".format(context.get("filename", ""), chunk_id), "info")
        st.rerun()
    return None


def _filter_chunks(
    chunks: Sequence[Dict[str, Any]], needle: str, kind: str
) -> List[Dict[str, Any]]:
    text = str(needle or "").strip().lower()
    out: List[Dict[str, Any]] = []
    for chunk in chunks:
        if kind and kind != "Any" and str(chunk.get("source_type", "")) != kind:
            continue
        if text and text not in "{0}\n{1}".format(
            chunk.get("text", ""), chunk.get("locator_text", "")
        ).lower():
            continue
        out.append(chunk)
    return out


def _chunk_card(chunk: Dict[str, Any], needle: str, focus: Optional[int]) -> None:
    chunk_id = int(chunk.get("chunk_id", chunk.get("id", 0)) or 0)
    is_focus = focus is not None and chunk_id == focus
    locator = str(chunk.get("locator_text", "") or "")

    components.badges(
        components.plain_badge("chunk {0}".format(chunk_id), theme.ACCENT if is_focus else ""),
        components.plain_badge("index {0}".format(chunk.get("chunk_index", ""))),
        components.plain_badge(str(chunk.get("source_type", ""))),
        components.plain_badge("{0} chars".format(chunk.get("char_count", 0))),
        # Only the in-process transport reports embedding state per chunk; over HTTP the
        # per-file count in the provenance panel above is the answer instead.
        components.plain_badge("indexed", theme.GREEN) if chunk.get("has_embedding") else "",
        components.plain_badge("jump target", theme.ACCENT) if is_focus else "",
    )
    # The locator is rendered above the text and in the citation typeface, because it is
    # the part an auditor copies into a working paper.
    st.markdown(
        '<div class="ia-cite-loc" style="margin-bottom:0.25rem;">{0}</div>'.format(
            components.escape(locator or "(no locator recorded)")
        ),
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="ia-chunk{focus}">{body}</div>'.format(
            focus=" ia-chunk-focus" if is_focus else "",
            body=_highlight(str(chunk.get("text", "")), needle),
        ),
        unsafe_allow_html=True,
    )


def _highlight(text: str, needle: str) -> str:
    """Escape chunk text for HTML, then mark the search term inside it.

    Escaping happens first and the needle is escaped the same way, so the two line up
    and no fragment of an uploaded document can close a tag. Evidence text is untrusted
    input.
    """
    escaped = components.escape(text)
    term = str(needle or "").strip()
    if not term:
        return escaped
    pattern = re.compile(re.escape(components.escape(term)), re.IGNORECASE)
    return pattern.sub(
        lambda match: '<span style="background:{0};color:{1};">{2}</span>'.format(
            theme.hex_to_rgba(theme.ACCENT, 0.28), theme.TEXT, match.group(0)
        ),
        escaped,
    )


def _delete_panel(evidence: Dict[str, Any]) -> None:
    evidence_file_id = int(evidence.get("id"))
    with st.expander("Delete this evidence file", expanded=False):
        st.warning(
            "Deleting removes the stored bytes and all {0} parsed chunk(s). Any citation "
            "that pointed at one of those chunks stays on its assessment but stops "
            "resolving - the same signal a fabricated citation produces. This cannot be "
            "undone.".format(evidence.get("chunks_stored", evidence.get("chunk_count", 0)))
        )
        confirm = st.checkbox(
            "I understand that existing citations into this file will no longer resolve.",
            key="evidence_delete_confirm_{0}".format(evidence_file_id),
        )
        if st.button(
            "Delete {0}".format(evidence.get("filename", "")),
            key="evidence_delete_{0}".format(evidence_file_id),
            disabled=not confirm,
        ):
            _delete(evidence_file_id, str(evidence.get("filename", "")))


def _delete(evidence_file_id: int, filename: str) -> None:
    try:
        removed = data_access.delete_evidence(evidence_file_id)
    except data_access.DataAccessError as exc:
        st.error("Could not delete the file: {0}".format(exc))
        return
    if removed:
        state.set_current_evidence(None)
    state.flash(
        "Deleted {0} and its chunks.".format(filename)
        if removed
        else "That evidence file no longer exists.",
        "success" if removed else "info",
    )
    st.rerun()


# ---- page
def render() -> None:
    components.section_header(
        "Evidence",
        subtitle="What was supplied, what the parser stored, and what a citation can point at.",
        eyebrow="Engagement",
    )
    project_id = state.current_project_id()
    if project_id is None:
        components.empty_state(
            "No engagement selected",
            "Evidence belongs to an engagement. Select one in the sidebar, or create one "
            "on the Audit Projects page, before uploading anything.",
        )
        return

    st.caption(
        "Evidence is scoped to one engagement. These are the artefacts held for {0}.".format(
            state.current_project_name()
        )
    )

    stats = _read(
        lambda: data_access.project_evidence_stats(project_id), {}, "the evidence figures"
    )
    files = _read(lambda: data_access.list_evidence(project_id=project_id), [], "the evidence list")
    unparsed = [row for row in files if not int(row.get("chunk_count", 0) or 0)]
    components.metric_row(
        [
            {"label": "Evidence files", "value": int(stats.get("evidence_files", 0) or 0)},
            {
                "label": "Stored chunks",
                "value": int(stats.get("evidence_chunks", 0) or 0),
                "color": theme.ACCENT,
                "caption": "Retrievable, citable units.",
            },
            {"label": "Total size", "value": "{0} MB".format(stats.get("total_mb", 0.0))},
            {
                "label": "Not retrievable",
                "value": len(unparsed),
                "color": theme.RED if unparsed else theme.GREEN,
                "caption": "Files that produced no chunk and so cannot be cited.",
                "help_text": (
                    "A file with no stored chunk is invisible to retrieval however "
                    "relevant it looks in the list."
                ),
            },
        ],
        columns=4,
    )
    _upload_panel(project_id, expanded=not files)

    if not files:
        components.empty_state(
            "No evidence in this engagement",
            "Upload policies, configuration exports, user listings or ticket extracts "
            "above, or load the demonstration evidence from the sidebar. Nothing can be "
            "assessed until there is something to cite.",
        )
        return

    filters = _filters()
    filtered = _read(
        lambda: data_access.list_evidence(
            project_id=project_id,
            evidence_type=filters["evidence_type"],
            parse_status=filters["parse_status"],
            search=filters["search"],
        ),
        [],
        "the evidence list",
    )
    _evidence_table(filtered)

    if unparsed:
        st.warning(
            "{0} file(s) produced no chunks and are therefore invisible to retrieval: "
            "{1}.".format(len(unparsed), ", ".join(str(row.get("filename")) for row in unparsed))
        )

    st.markdown("---")
    options = [int(row["id"]) for row in (filtered or files)]
    labels = {int(row["id"]): str(row.get("filename", "")) for row in (filtered or files)}
    current = state.current_evidence_id()
    # A "follow this chunk into its own file" request from the previous run is applied
    # here, before the selector exists: once a keyed widget has been created, its stored
    # value wins over the ``index`` argument and cannot be changed until the next run.
    pending = state.get_filter(PAGE, "pending_open", None)
    if pending is not None:
        state.set_filter(PAGE, "pending_open", None)
        if int(pending) in options:
            st.session_state[_SELECT_KEY] = int(pending)
            current = int(pending)
    chosen = st.selectbox(
        "Open an evidence file",
        options=options,
        index=options.index(current) if current in options else 0,
        format_func=lambda value: labels.get(value, str(value)),
        key=_SELECT_KEY,
    )
    state.set_current_evidence(int(chosen))
    _detail(int(chosen))


if __name__ == "__main__":
    render()
