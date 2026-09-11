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

Why provenance is shouted rather than mentioned
-----------------------------------------------
This repository contains material that reads exactly like organisational evidence and is
not: generated datasets, and historical case studies reconstructed from publicly
documented failure patterns under invented organisation names. Realism is the point of
those files and it is also the risk, because a realistic account listing quoted out of
context becomes a claim about a real company. So provenance is not a discreet metadata
field on this page. It is a column on every row, a banner over the list whenever anything
non-synthetic is present, a full-width banner on the detail view, and a required choice at
upload. ``EvidenceType`` says what an artefact *is*; ``EvidenceProvenance`` says where it
*came from*, and only the second one determines what may be claimed on the strength of it.
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from typing import Any, Callable, Dict, List, Optional, Sequence  # noqa: E402

import streamlit as st  # noqa: E402

from app.frontend import components, data_access, state, theme  # noqa: E402
from app.schemas.enums import (  # noqa: E402
    EvidenceProvenance,
    provenance_badge_text,
    provenance_label,
)

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
    "provenance",
    "size_kb",
    "sha256_short",
    "uploaded_at",
    "parse_status",
    "page_count",
    "row_count",
    "chunk_count",
]

#: Colour per provenance, chosen against the meaning each one has *in this application*
#: rather than by severity:
#:
#: * SYNTHETIC is the quiet default - it is what almost everything here is, and a warning
#:   that fires on every row is a warning nobody reads.
#: * HISTORICAL_PUBLIC is amber-orange: attention, not error. The file is legitimate, and
#:   the one thing that must never happen is a reader taking it for real evidence.
#: * ORGANISATIONAL is red, which looks backwards until you remember what this prototype
#:   is: no authentication, no access control, no encryption at rest, evidence written to
#:   local disk in the clear. Real audit evidence *here* is a condition to flag, not a
#:   badge of quality.
_PROVENANCE_COLORS = {
    EvidenceProvenance.SYNTHETIC.value: theme.GREY,
    EvidenceProvenance.HISTORICAL_PUBLIC.value: theme.ORANGE,
    EvidenceProvenance.ORGANISATIONAL.value: theme.RED,
}

#: Headline shown on the provenance banner. Deliberately blunt for the reconstruction: it
#: is the sentence a reader must not be able to skim past.
_PROVENANCE_HEADLINES = {
    EvidenceProvenance.SYNTHETIC.value: "SYNTHETIC TEST DATA",
    EvidenceProvenance.HISTORICAL_PUBLIC.value: (
        "SYNTHETIC HISTORICAL RECONSTRUCTION - NOT REAL ORGANISATIONAL EVIDENCE"
    ),
    EvidenceProvenance.ORGANISATIONAL.value: "REAL ORGANISATIONAL EVIDENCE",
}

#: Extra line appended to the banner, saying what the auditor must actually do about it.
_PROVENANCE_ACTIONS = {
    EvidenceProvenance.HISTORICAL_PUBLIC.value: (
        "A finding drawn from this file is a statement about the reconstruction. It is not "
        "a finding about any real organisation and must never be reported as one."
    ),
    EvidenceProvenance.ORGANISATIONAL.value: (
        "This prototype has no authentication, no access control and no encryption at rest, "
        "and it writes evidence to local disk in the clear. Real audit evidence should not "
        "be held here."
    ),
}

#: Set once the data-access facade has been inspected. See :func:`_facade_takes_provenance`.
_FACADE_TAKES_PROVENANCE: Optional[bool] = None


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


# ---- provenance
def _provenance_of(record: Any) -> str:
    """Provenance of one evidence record, defaulting the way the enum defaults.

    A row written before the column existed, or by a transport that does not carry it yet,
    reads as SYNTHETIC rather than as blank. That is the safe direction to be wrong in: an
    artefact under-trusted is a nuisance, an artefact over-trusted is a fabricated audit
    record.
    """
    raw = dict(record or {}).get("provenance")
    return EvidenceProvenance.coerce(raw, EvidenceProvenance.SYNTHETIC).value


def _provenance_badge(value: Any) -> str:
    """Badge HTML for one provenance. Render with :func:`components.badges`."""
    resolved = EvidenceProvenance.coerce(value, EvidenceProvenance.SYNTHETIC).value
    return components.plain_badge(provenance_badge_text(resolved), _PROVENANCE_COLORS[resolved])


def _provenance_banner(value: Any, compact: bool = False) -> None:
    """The full-width provenance disclosure.

    Written as markup rather than as ``st.info``/``st.warning`` because the Streamlit
    callouts all look alike, and this is the one notice on the page that must not read as
    another parser warning. The disclosure sentence comes from
    ``app.schemas.enums.PROVENANCE_LABELS`` so that the console, the reports and the
    database never drift into three different wordings of the same disclaimer.
    """
    resolved = EvidenceProvenance.coerce(value, EvidenceProvenance.SYNTHETIC).value
    color = _PROVENANCE_COLORS[resolved]
    lines = [provenance_label(resolved)]
    action = _PROVENANCE_ACTIONS.get(resolved)
    if action and not compact:
        lines.append(action)
    body = "".join(
        '<div style="margin-top:0.35rem;color:{0};">{1}</div>'.format(
            theme.TEXT_MUTED, components.escape(line)
        )
        for line in lines
    )
    st.markdown(
        '<div style="border:1px solid {border};border-left:4px solid {color};'
        'background:{bg};border-radius:6px;padding:0.7rem 0.9rem;margin:0.35rem 0 0.9rem 0;">'
        '<div style="color:{color};font-weight:700;letter-spacing:0.04em;font-size:0.82rem;">'
        "{headline}</div>{body}</div>".format(
            border=theme.BORDER,
            color=color,
            bg=theme.hex_to_rgba(color, 0.10),
            headline=components.escape(_PROVENANCE_HEADLINES[resolved]),
            body=body,
        ),
        unsafe_allow_html=True,
    )


def _provenance_summary(files: Sequence[Dict[str, Any]]) -> None:
    """Count the engagement's evidence by provenance, and warn about the non-synthetic.

    The counts are always shown; the banner only appears when something other than plain
    synthetic test data is held. An engagement mixing a reconstruction with real evidence
    is the case this exists to make impossible to miss.
    """
    counts: Dict[str, int] = {}
    for record in files:
        value = _provenance_of(record)
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return

    # Defaulting an absent provenance to SYNTHETIC is right for an old row and wrong for a
    # transport that simply does not report the field: every reconstruction would then read
    # as ordinary test data, which is the one misreading this page exists to prevent. The
    # REST response model has no provenance field yet, so say so instead of showing a
    # confident count that is an artefact of the transport.
    if files and not any("provenance" in dict(record) for record in files):
        st.warning(
            "The REST API does not report evidence provenance yet, so every file below is "
            "shown as {0} whatever it actually is. Run the console in-process (USE_API "
            "unset) to see the recorded provenance.".format(
                provenance_badge_text(EvidenceProvenance.SYNTHETIC)
            )
        )
        return

    components.badges(
        *[
            components.plain_badge(
                "{0}: {1}".format(provenance_badge_text(value), counts[value]),
                _PROVENANCE_COLORS[value],
            )
            for value in EvidenceProvenance.values()
            if value in counts
        ]
    )
    for value in (
        EvidenceProvenance.ORGANISATIONAL.value,
        EvidenceProvenance.HISTORICAL_PUBLIC.value,
    ):
        if counts.get(value):
            _provenance_banner(value)


def _facade_takes_provenance() -> bool:
    """Whether ``data_access.upload_evidence`` can carry a provenance through ingestion.

    ``app.evidence.service.ingest_file`` does not yet accept a ``provenance`` argument and
    is owned by another part of the build, so the facade above it cannot pass one either.
    Rather than hard-code that fact and quietly rot once it changes, the signature is
    inspected: the moment the parameter appears the page starts using it and the follow-up
    write in :func:`_record_provenance` stops running.
    """
    global _FACADE_TAKES_PROVENANCE
    if _FACADE_TAKES_PROVENANCE is None:
        try:
            parameters = inspect.signature(data_access.upload_evidence).parameters
            _FACADE_TAKES_PROVENANCE = "provenance" in parameters
        except (TypeError, ValueError):  # pragma: no cover - defensive
            _FACADE_TAKES_PROVENANCE = False
    return bool(_FACADE_TAKES_PROVENANCE)


def _record_provenance(record: Dict[str, Any], provenance: str) -> Dict[str, Any]:
    """Apply the auditor's provenance choice to a row ingestion has just written.

    A stopgap, and documented as one. Until ``ingest_file`` takes the value directly there
    is a short window in which the row carries the column default, and this second write
    closes it. Two things are deliberate: the write is skipped entirely when the console is
    talking to a remote API (the database may not be this process's to touch, and guessing
    would be worse than reporting), and a failure is shown to the auditor rather than
    swallowed - an artefact silently recorded as SYNTHETIC when the auditor declared it
    ORGANISATIONAL is exactly the confusion this whole feature exists to prevent.
    """
    if _provenance_of(record) == provenance:
        return record
    if data_access.use_api():
        st.error(
            "{0} was stored, but its provenance was recorded as {1} rather than {2}: the "
            "REST API does not carry provenance on upload yet. Correct it before relying "
            "on this file.".format(
                record.get("filename", "The file"), _provenance_of(record), provenance
            )
        )
        return record

    try:
        from app.database.base import session_scope
        from app.database.models import EvidenceFile

        with session_scope() as session:
            row = session.get(EvidenceFile, int(record.get("id")))
            if row is None:
                raise LookupError("the evidence row disappeared between ingestion and this write")
            row.provenance = provenance
        updated = dict(record)
        updated["provenance"] = provenance
        return updated
    except Exception as exc:  # noqa: BLE001 - the auditor sees the message, never a traceback
        st.error(
            "{0} was stored, but its provenance could not be recorded ({1}). It is held as "
            "{2}.".format(record.get("filename", "The file"), exc, _provenance_of(record))
        )
        return record


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
            # Provenance is a required choice rather than a checkbox default, and it sits
            # beside the evidence type because the two answer different questions about the
            # same file: what it is, and where it came from.
            provenance = st.selectbox(
                "Provenance",
                options=EvidenceProvenance.values(),
                index=EvidenceProvenance.values().index(EvidenceProvenance.SYNTHETIC.value),
                format_func=provenance_badge_text,
                help=(
                    "Where this artefact came from, which is not the same question as what "
                    "it is. It determines what may be claimed on the strength of it, and it "
                    "is shown on every screen and in every report that cites the file."
                ),
            )
            st.caption(provenance_label(provenance))
            if provenance != EvidenceProvenance.SYNTHETIC.value:
                _provenance_banner(provenance)
            submitted = st.form_submit_button("Ingest", type="primary")

        if submitted:
            _ingest(project_id, files or [], evidence_type, description, provenance)


def _ingest(
    project_id: int,
    files: Sequence[Any],
    evidence_type: str,
    description: str,
    provenance: str,
) -> None:
    if not files:
        st.warning("Choose at least one file before pressing Ingest.")
        return
    results: List[Dict[str, Any]] = []
    failures: List[Dict[str, str]] = []
    # ``is_synthetic`` is no longer a separate question for the auditor to answer. It is
    # the older boolean that provenance subsumes, and the two must never contradict each
    # other on the same row, so it is derived: anything that did not come from an audited
    # entity is synthetic, whether this project fabricated it or reconstructed it from
    # public history.
    synthetic = provenance != EvidenceProvenance.ORGANISATIONAL.value
    extra: Dict[str, Any] = {"provenance": provenance} if _facade_takes_provenance() else {}
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
                    is_synthetic=synthetic,
                    **extra
                )
                results.append(_record_provenance(record, provenance))
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
        _provenance_badge(_provenance_of(record)),
        components.plain_badge(
            "{0} chunk(s)".format(chunks), theme.GREEN if chunks else theme.RED
        ),
        components.plain_badge("{0} page(s)".format(record.get("page_count", 0))),
        components.plain_badge("{0} row(s)".format(record.get("row_count", 0))),
        components.plain_badge("sha {0}".format(str(record.get("sha256", ""))[:12])),
    )
    if _provenance_of(record) != EvidenceProvenance.SYNTHETIC.value:
        _provenance_banner(_provenance_of(record), compact=True)
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
    left, middle, provenance_column, right = st.columns([2, 2, 2, 3], gap="small")
    with left:
        evidence_type = st.selectbox(
            "Evidence type", options=["Any"] + data_access.evidence_types(), index=0, key="evidence_type_filter"
        )
    with middle:
        parse_status = st.selectbox(
            "Parse status", options=["Any"] + _parse_statuses(), index=0, key="evidence_parse_filter"
        )
    with provenance_column:
        provenance = st.selectbox(
            "Provenance",
            options=["Any"] + EvidenceProvenance.values(),
            index=0,
            format_func=lambda value: "Any" if value == "Any" else provenance_badge_text(value),
            key="evidence_provenance_filter",
            help="Isolate the reconstructed material, or everything that is not it.",
        )
    with right:
        search = st.text_input(
            "Search filenames",
            value=str(state.get_filter(PAGE, "search", "") or ""),
            key="evidence_search",
        )
    state.set_filter(PAGE, "type", evidence_type)
    state.set_filter(PAGE, "search", search)
    state.set_filter(PAGE, "provenance", provenance)
    return {
        "evidence_type": None if evidence_type == "Any" else evidence_type,
        "parse_status": None if parse_status == "Any" else parse_status,
        "provenance": None if provenance == "Any" else provenance,
        "search": search.strip(),
    }


def _by_provenance(
    files: Sequence[Dict[str, Any]], provenance: Optional[str]
) -> List[Dict[str, Any]]:
    """Apply the provenance filter in the page rather than in the query.

    ``data_access.list_evidence`` does not take a provenance argument, and that facade is
    owned elsewhere. Filtering here is correct rather than merely expedient at this scale -
    the list is already one engagement's evidence and is fully in memory - but it would
    need to move into the query if the page ever paginated.
    """
    if not provenance:
        return list(files)
    return [record for record in files if _provenance_of(record) == provenance]


def _parse_statuses() -> List[str]:
    from app.schemas.enums import ParseStatus

    return ParseStatus.values()


def _evidence_table(files: Sequence[Dict[str, Any]]) -> None:
    # A dataframe cell cannot carry a coloured pill, so the row-level provenance is made
    # unmissable by its wording instead: "HISTORICAL RECONSTRUCTION" reads as a warning on
    # its own, whereas the stored value "HISTORICAL_PUBLIC" does not. The banner drawn by
    # ``_provenance_summary`` above the table carries the colour and the full sentence.
    rows = []
    for record in files:
        row = dict(record)
        row["provenance"] = provenance_badge_text(_provenance_of(record))
        rows.append(row)
    components.df_table(
        rows,
        columns=_TABLE_COLUMNS,
        column_config={
            # Ten columns have to fit a 1280px screen, so the widest defaults are narrowed
            # here rather than left to scroll off the right-hand edge.
            "filename": st.column_config.TextColumn("File", width="medium"),
            "evidence_type": st.column_config.TextColumn("Type", width="small"),
            "provenance": st.column_config.TextColumn(
                "Provenance",
                width="medium",
                help=(
                    "Where the artefact came from, as distinct from what it is. "
                    "HISTORICAL RECONSTRUCTION means the file was generated from a "
                    "publicly documented failure pattern under an invented organisation "
                    "name; it is not any real organisation's evidence."
                ),
            ),
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

    # Above the shared provenance panel rather than inside it: that panel is owned by
    # ``app.frontend.components`` and reports how the file was parsed, while this banner is
    # the declaration of origin and has to be the first thing read on the screen.
    _provenance_banner(_provenance_of(evidence))
    components.evidence_provenance_panel(evidence)
    components.badges(_provenance_badge(_provenance_of(evidence)))
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
    # Drawn before the upload panel so that the state of the engagement's evidence is read
    # before anything is added to it.
    _provenance_summary(files)
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
    filtered = _by_provenance(
        _read(
            lambda: data_access.list_evidence(
                project_id=project_id,
                evidence_type=filters["evidence_type"],
                parse_status=filters["parse_status"],
                search=filters["search"],
            ),
            [],
            "the evidence list",
        ),
        filters["provenance"],
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
