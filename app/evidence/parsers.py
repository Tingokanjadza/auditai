"""Turn an uploaded artefact into citable chunks.

The parsers exist to answer one question for every fragment of evidence: *where exactly
did this come from?* A chunk that cannot name its page, sheet, row numbers or section is
not evidence an auditor can rely on, and it is precisely the kind of fragment a language
model can quote without anyone being able to check it. Every :class:`ParsedChunk`
produced here therefore carries a fully populated :class:`~app.rag.base.SourceLocator`.

Two conventions are load-bearing and are applied identically to CSV, XLSX and Word
tables:

* **Row numbers are 1-based spreadsheet rows as a human sees them**, header included.
  The header occupies row 1, the first data record is row 2. When an auditor is told an
  exception sits at row 14, opening the file in Excel and going to row 14 must show that
  exception. (For CSV this counts *records*, not physical lines, so a quoted field
  containing newlines still maps to the row Excel would display.)
* **Every tabular sheet also gets exactly one ``TABLE_SUMMARY`` chunk** describing the
  whole population - shape, dtypes, null counts and per-value counts for low-cardinality
  columns, with the source rows listed for rare values. That summary is what lets a model
  state "10 of 100 accounts show MFA_Status = Disabled, at rows 14, 27, 38" without being
  shown all 100 rows, and it is what stops it from guessing instead.

Robustness is a hard requirement rather than a nicety: an encrypted PDF, an empty sheet,
a malformed CSV, a zero-byte upload and a binary file with a ``.txt`` extension must each
produce a clean :class:`ParseResult` carrying a warning. Ingestion never raises on bad
input, because an audit tool that crashes on a strange file teaches its user to work
around it rather than record it.
"""

from __future__ import annotations

import io
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.config import SUPPORTED_UPLOAD_EXTENSIONS, get_settings
from app.evidence.chunking import (
    PackedChunk,
    TextBlock,
    detect_heading,
    format_cell,
    normalise_whitespace,
    pack_blocks,
    render_table_rows,
    window_text,
)
from app.rag.base import ParsedChunk, SourceLocator
from app.schemas.enums import SourceType

__all__ = ["ParseResult", "parse_file", "supported_extensions"]

#: A column with at most this many distinct values is profiled value-by-value in the
#: table summary. Above it, only a few examples are shown.
LOW_CARDINALITY_MAX = 15
#: A minority value occurring at most this many times gets its source rows listed
#: explicitly - this turns "10 accounts are non-compliant" into "rows 14, 27, 38, ...".
#: Majority values are excluded: their rows are the population range already stated.
ROW_LIST_MAX_OCCURRENCES = 30
#: Hard cap on how many row numbers are printed for one value.
ROW_LIST_MAX_PRINTED = 40

_ENCODINGS = ("utf-8", "utf-8-sig", "latin-1")
_TEXT_EXTENSIONS = (".txt", ".md")
_TABULAR_EXTENSIONS = (".csv", ".xlsx", ".xls")
_SETEXT_RULE = re.compile(r"^(=+|-{3,})$")
_JSON_TOP_KEY = re.compile(r'^  "([^"]+)"\s*:')


@dataclass
class ParseResult:
    """Everything one file yielded, including the reasons it yielded less than hoped."""

    chunks: List[ParsedChunk] = field(default_factory=list)
    page_count: int = 0
    row_count: int = 0
    char_count: int = 0
    extra_metadata: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    @property
    def ok(self) -> bool:
        """True when the file produced at least one retrievable chunk."""
        return bool(self.chunks)

    def warn(self, message: str) -> None:
        if message and message not in self.warnings:
            self.warnings.append(message)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chunk_count": self.chunk_count,
            "page_count": self.page_count,
            "row_count": self.row_count,
            "char_count": self.char_count,
            "extra_metadata": dict(self.extra_metadata),
            "warnings": list(self.warnings),
        }


def supported_extensions() -> List[str]:
    return list(SUPPORTED_UPLOAD_EXTENSIONS)


def parse_file(path: str, filename: str = "") -> ParseResult:
    """Parse ``path`` into locator-bearing chunks. Never raises on bad input.

    ``filename`` is the name the auditor uploaded; it is what appears in every citation,
    while ``path`` is where the bytes actually live. They differ because uploads are
    stored under content-addressed names.
    """
    source_path = str(path)
    name = filename or os.path.basename(source_path)
    extension = os.path.splitext(name)[1].lower() or os.path.splitext(source_path)[1].lower()

    result = ParseResult(extra_metadata={"filename": name, "extension": extension, "parser": ""})

    if extension not in SUPPORTED_UPLOAD_EXTENSIONS:
        result.extra_metadata["parser"] = "unsupported"
        result.warn(
            "Unsupported file extension '{0}'. Supported extensions: {1}.".format(
                extension or "(none)", ", ".join(SUPPORTED_UPLOAD_EXTENSIONS)
            )
        )
        return result

    if not os.path.isfile(source_path):
        result.warn("File not found on disk: {0}".format(source_path))
        return result

    size_bytes = os.path.getsize(source_path)
    result.extra_metadata["size_bytes"] = size_bytes
    if size_bytes == 0:
        result.warn("File is empty (0 bytes); no evidence could be extracted.")
        return result

    try:
        if extension == ".pdf":
            result.extra_metadata["parser"] = "pypdf"
            _parse_pdf(source_path, name, result)
        elif extension == ".docx":
            result.extra_metadata["parser"] = "python-docx"
            _parse_docx(source_path, name, result)
        elif extension in _TABULAR_EXTENSIONS:
            result.extra_metadata["parser"] = "pandas"
            _parse_tabular(source_path, name, extension, result)
        elif extension == ".json":
            result.extra_metadata["parser"] = "json"
            _parse_json(source_path, name, result)
        elif extension in _TEXT_EXTENSIONS:
            result.extra_metadata["parser"] = "text"
            _parse_text(source_path, name, result)
        else:  # defensive: SUPPORTED_UPLOAD_EXTENSIONS gained an entry with no parser
            result.warn("No parser is registered for '{0}' files.".format(extension))
    except Exception as exc:  # noqa: BLE001 - a bad upload must never break ingestion
        result.warn("{0} while parsing '{1}': {2}".format(type(exc).__name__, name, exc))

    for index, chunk in enumerate(result.chunks):
        chunk.chunk_index = index
    result.char_count = sum(len(chunk.text) for chunk in result.chunks)
    if not result.chunks and not result.warnings:
        result.warn("No readable content was extracted from '{0}'.".format(name))
    return result


# ---- shared low-level helpers
def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def _looks_binary(data: bytes, sample: int = 4096) -> bool:
    """Heuristic guard against a binary file wearing a text extension.

    A NUL byte is decisive; otherwise an unusual density of control characters is taken
    as binary. Both tests run on the raw bytes because ``latin-1`` will happily decode
    absolutely anything into mojibake, which would otherwise be indexed as "evidence".
    """
    if not data:
        return False
    head = data[:sample]
    if b"\x00" in head:
        return True
    allowed = (9, 10, 12, 13)
    control = sum(1 for byte in head if byte < 32 and byte not in allowed)
    return (control / float(len(head))) > 0.05


def _decode_text(data: bytes) -> Tuple[str, str, Optional[str]]:
    """Decode bytes trying utf-8, then utf-8-sig, then latin-1.

    Returns ``(text, encoding, warning)``. ``latin-1`` never fails, so it is the
    guaranteed last resort - but it is reported, because text recovered that way may be
    mojibake and an auditor should know the difference before quoting it.
    """
    for encoding in _ENCODINGS:
        try:
            text = data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        if encoding == "utf-8" and text.startswith("\ufeff"):
            text = text.lstrip("\ufeff")  # BOM decoded by utf-8 rather than utf-8-sig
        warning = None
        if encoding == "latin-1":
            warning = "File is not valid UTF-8; decoded as latin-1. Characters may be misrendered."
        return text, encoding, warning
    return data.decode("latin-1", errors="replace"), "latin-1", "File could not be decoded cleanly; unreadable bytes were replaced."


def _clip(value: Optional[str], limit: int) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _json_safe(value: Any) -> Any:
    """Coerce pandas/numpy scalars into something ``json.dumps`` and SQLite accept."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:  # noqa: BLE001
            return str(value)
    return str(value)


# ---- PDF
def _parse_pdf(path: str, filename: str, result: ParseResult) -> None:
    """One logical chunk per page, split further when a page exceeds ``chunk_size``.

    Headings are tracked across page boundaries: a policy section that begins on page 3
    still governs the text at the top of page 4, and an auditor asking "which section
    said that?" needs the answer to survive the page break.
    """
    from pypdf import PdfReader

    settings = get_settings()
    reader = PdfReader(path)

    if getattr(reader, "is_encrypted", False):
        result.extra_metadata["encrypted"] = True
        decrypted = False
        try:
            decrypted = bool(reader.decrypt(""))
        except Exception:  # noqa: BLE001 - pypdf raises several unrelated types here
            decrypted = False
        if not decrypted:
            result.warn(
                "PDF is encrypted and could not be opened without a password; no text was extracted."
            )
            return
        result.warn("PDF was encrypted with an empty password and was opened for parsing.")

    try:
        pages = list(reader.pages)
    except Exception as exc:  # noqa: BLE001
        result.warn("Could not read PDF pages: {0}: {1}".format(type(exc).__name__, exc))
        return

    result.page_count = len(pages)
    try:
        meta = reader.metadata or {}
        result.extra_metadata["pdf_metadata"] = {
            str(key).lstrip("/"): _json_safe(value) for key, value in dict(meta).items()
        }
    except Exception:  # noqa: BLE001
        result.extra_metadata["pdf_metadata"] = {}

    section: Optional[str] = None
    empty_pages: List[int] = []
    sections_seen: List[str] = []

    for page_number, page in enumerate(pages, start=1):
        try:
            raw = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            result.warn("Page {0} could not be extracted ({1}); it was skipped.".format(page_number, type(exc).__name__))
            continue

        text = normalise_whitespace(raw)
        if not text:
            empty_pages.append(page_number)
            continue

        blocks, section = _blocks_from_lines(text.split("\n"), section, sections_seen)
        packed = pack_blocks(blocks, chunk_size=settings.chunk_size, overlap=0)
        for part_index, part in enumerate(packed, start=1):
            locator = SourceLocator(
                filename=filename,
                source_type=SourceType.PDF_PAGE.value,
                page_number=page_number,
                section=part.section,
                paragraph_index=part.first_index or None,
            )
            result.chunks.append(
                ParsedChunk(
                    text=part.text,
                    locator=locator,
                    extra_metadata={
                        "page_part": part_index,
                        "page_parts": len(packed),
                        "sections": part.sections,
                    },
                )
            )

    if empty_pages:
        result.extra_metadata["empty_pages"] = empty_pages
        result.warn(
            "{0} of {1} page(s) contained no extractable text (likely scanned images); "
            "no OCR is performed.".format(len(empty_pages), len(pages))
        )
    result.extra_metadata["sections"] = sections_seen[:50]


def _blocks_from_lines(
    lines: Sequence[str],
    section: Optional[str],
    sections_seen: List[str],
) -> Tuple[List[TextBlock], Optional[str]]:
    """Group extracted lines into paragraph blocks, updating the current heading."""
    blocks: List[TextBlock] = []
    buffer: List[str] = []
    buffer_start = 1
    index = 0
    skip_next = False

    def flush(end_line: int) -> None:
        nonlocal buffer, index
        if not buffer:
            return
        index += 1
        blocks.append(
            TextBlock(
                text=" ".join(buffer).strip(),
                section=section,
                index=index,
                line_start=buffer_start,
                line_end=end_line,
            )
        )
        buffer = []

    total = len(lines)
    for position, line in enumerate(lines, start=1):
        if skip_next:
            skip_next = False
            continue
        stripped = line.strip()
        if not stripped:
            flush(position - 1)
            continue
        next_line = lines[position].strip() if position < total else ""
        prev_blank = position == 1 or not lines[position - 2].strip()
        heading = detect_heading(stripped, next_line, prev_blank)
        if heading:
            flush(position - 1)
            section = heading
            if heading not in sections_seen:
                sections_seen.append(heading)
            index += 1
            blocks.append(
                TextBlock(text=stripped, section=section, index=index, line_start=position, line_end=position)
            )
            if next_line and _SETEXT_RULE.match(next_line):
                skip_next = True
            continue
        if not buffer:
            buffer_start = position
        buffer.append(stripped)
    flush(total)
    return blocks, section


# ---- DOCX
def _parse_docx(path: str, filename: str, result: ParseResult) -> None:
    """Paragraphs grouped under their Word heading, plus every table as row chunks.

    Word documents carry their structure explicitly in paragraph styles, so headings are
    read from the style first and only guessed from the text when a document does not use
    them - which real-world policy documents frequently do not.
    """
    import docx
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    settings = get_settings()
    document = docx.Document(path)

    section: Optional[str] = None
    sections_seen: List[str] = []
    paragraph_index = 0
    table_index = 0
    table_row_total = 0
    pending: List[TextBlock] = []

    def flush_paragraphs() -> None:
        """Emit the paragraphs accumulated so far, keeping document order with tables."""
        if not pending:
            return
        # min_section_chunk_chars=0: a heading always starts a new chunk. Merging two
        # sections to reach a target size would leave locator.section naming only the
        # first of them, and a citation that points at the wrong section is worse than
        # a chunk that is smaller than ideal for retrieval.
        packed = pack_blocks(
            pending,
            chunk_size=settings.chunk_size,
            overlap=settings.chunk_overlap,
            flush_on_section_change=True,
            min_section_chunk_chars=0,
        )
        for part in packed:
            result.chunks.append(
                ParsedChunk(
                    text=part.text,
                    locator=SourceLocator(
                        filename=filename,
                        source_type=SourceType.DOCX_PARAGRAPH.value,
                        section=part.section,
                        paragraph_index=part.first_index or None,
                    ),
                    extra_metadata={
                        "paragraph_start": part.first_index,
                        "paragraph_end": part.last_index,
                        "sections": part.sections,
                    },
                )
            )
        del pending[:]

    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            paragraph = Paragraph(child, document)
            text = normalise_whitespace(paragraph.text)
            if not text:
                continue
            paragraph_index += 1
            style_name = ""
            try:
                style_name = paragraph.style.name or ""
            except Exception:  # noqa: BLE001 - a corrupt style reference must not stop parsing
                style_name = ""
            if style_name.startswith("Heading") or style_name in ("Title", "Subtitle"):
                section = text
            else:
                guessed = detect_heading(text)
                if guessed and len(text) <= 90:
                    section = guessed
            if section and section not in sections_seen:
                sections_seen.append(section)
            if "List" in style_name and not text.startswith(("-", "*", "•")):
                text = "- " + text
            pending.append(TextBlock(text=text, section=section, index=paragraph_index))
        elif child.tag == qn("w:tbl"):
            flush_paragraphs()
            table_index += 1
            table_row_total += _emit_docx_table(
                Table(child, document), table_index, section, filename, result, settings
            )

    flush_paragraphs()

    result.row_count = table_row_total
    result.extra_metadata["paragraph_count"] = paragraph_index
    result.extra_metadata["table_count"] = table_index
    result.extra_metadata["sections"] = sections_seen[:50]
    if paragraph_index == 0 and table_index == 0:
        result.warn("The Word document contains no text paragraphs or tables.")


def _emit_docx_table(
    table: Any,
    table_index: int,
    section: Optional[str],
    filename: str,
    result: ParseResult,
    settings: Any,
) -> int:
    """Emit one table as TABLE_ROWS chunks numbered the way the table reads on screen.

    The same convention as a spreadsheet is used deliberately: the header is table row 1
    and the first data row is table row 2, so a citation reads the same whether the
    population came from a Word table or a CSV export.
    """
    try:
        grid = [[cell.text for cell in row.cells] for row in table.rows]
    except Exception as exc:  # noqa: BLE001
        result.warn("Table {0} could not be read ({1}); it was skipped.".format(table_index, type(exc).__name__))
        return 0

    grid = [[normalise_whitespace(cell) for cell in row] for row in grid]
    grid = [row for row in grid if any(cell for cell in row)]
    if not grid:
        result.warn("Table {0} is empty; no chunk was created for it.".format(table_index))
        return 0

    table_label = "Table {0}".format(table_index)
    locator_section = "{0} > {1}".format(section, table_label) if section else table_label

    if len(grid) == 1:
        columns = ["column_{0}".format(i + 1) for i in range(len(grid[0]))]
        data_rows = grid
        row_numbers = [1]
    else:
        columns = [cell or "column_{0}".format(i + 1) for i, cell in enumerate(grid[0])]
        data_rows = grid[1:]
        row_numbers = [i + 2 for i in range(len(data_rows))]

    per_chunk = max(1, int(settings.table_rows_per_chunk))
    for start in range(0, len(data_rows), per_chunk):
        window_rows = data_rows[start : start + per_chunk]
        window_numbers = row_numbers[start : start + per_chunk]
        header_line = (
            "{0} of {1}{2} | header on table row 1 | this chunk covers table rows {3}-{4} of "
            "{5} data row(s)".format(
                table_label,
                filename,
                " | section '{0}'".format(section) if section else "",
                window_numbers[0],
                window_numbers[-1],
                len(data_rows),
            )
        )
        body = render_table_rows(columns, window_rows, window_numbers, row_label="row")
        result.chunks.append(
            ParsedChunk(
                text=header_line + "\n" + body,
                locator=SourceLocator(
                    filename=filename,
                    source_type=SourceType.TABLE_ROWS.value,
                    section=locator_section,
                    row_start=window_numbers[0],
                    row_end=window_numbers[-1],
                    row_numbers=list(window_numbers),
                    column_names=list(columns),
                ),
                extra_metadata={
                    "table_index": table_index,
                    "docx_source_type": SourceType.DOCX_TABLE.value,
                    "table_data_rows": len(data_rows),
                },
            )
        )
    return len(data_rows)


# ---- tabular (CSV / XLSX / XLS)
@dataclass
class _Sheet:
    """One sheet's data plus the mapping back to real spreadsheet row numbers."""

    name: str
    frame: Any
    #: ``row_numbers[i]`` is the 1-based spreadsheet row of ``frame`` row ``i``.
    row_numbers: List[int]
    header_row: int
    blank_rows: List[int] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def _parse_tabular(path: str, filename: str, extension: str, result: ParseResult) -> None:
    import pandas as pd  # imported lazily so a text-only install still parses text

    if extension == ".csv":
        sheets = _load_csv(path, filename, result, pd)
        if sheets is None:  # unparseable as a table - keep the content as text
            result.extra_metadata["parser"] = "text-fallback"
            _parse_text(path, filename, result)
            return
    else:
        sheets = _load_excel(path, filename, extension, result, pd)

    if not sheets:
        return

    settings = get_settings()
    sheet_meta: List[Dict[str, Any]] = []
    total_rows = 0

    for sheet in sheets:
        summary_text, summary_meta = _summarise_sheet(sheet, filename)
        result.chunks.append(
            ParsedChunk(
                text=summary_text,
                locator=SourceLocator(
                    filename=filename,
                    source_type=SourceType.TABLE_SUMMARY.value,
                    sheet_name=sheet.name or None,
                    row_start=sheet.row_numbers[0] if sheet.row_numbers else None,
                    row_end=sheet.row_numbers[-1] if sheet.row_numbers else None,
                    column_names=[str(c) for c in sheet.frame.columns],
                    section="Population summary",
                ),
                extra_metadata=summary_meta,
            )
        )

        rows = sheet.frame.values.tolist()
        columns = [str(c) for c in sheet.frame.columns]
        per_chunk = max(1, int(settings.table_rows_per_chunk))
        for start in range(0, len(rows), per_chunk):
            window_rows = rows[start : start + per_chunk]
            window_numbers = sheet.row_numbers[start : start + per_chunk]
            header_line = (
                "TABLE ROWS - {0}{1} | header on spreadsheet row {2} | this chunk covers "
                "spreadsheet rows {3}-{4} of {5} data row(s)".format(
                    filename,
                    " [sheet: {0}]".format(sheet.name) if sheet.name else "",
                    sheet.header_row,
                    window_numbers[0],
                    window_numbers[-1],
                    len(rows),
                )
            )
            body = render_table_rows(columns, window_rows, window_numbers, row_label="row")
            result.chunks.append(
                ParsedChunk(
                    text=header_line + "\n" + body,
                    locator=SourceLocator(
                        filename=filename,
                        source_type=SourceType.TABLE_ROWS.value,
                        sheet_name=sheet.name or None,
                        row_start=window_numbers[0],
                        row_end=window_numbers[-1],
                        row_numbers=[int(n) for n in window_numbers],
                        column_names=columns,
                    ),
                    extra_metadata={
                        "header_row": sheet.header_row,
                        "sheet_data_rows": len(rows),
                    },
                )
            )

        total_rows += int(sheet.frame.shape[0])
        sheet_meta.append(
            {
                "sheet": sheet.name,
                "header_row": sheet.header_row,
                "data_rows": int(sheet.frame.shape[0]),
                "columns": [str(c) for c in sheet.frame.columns],
                "first_data_row": sheet.row_numbers[0] if sheet.row_numbers else None,
                "last_data_row": sheet.row_numbers[-1] if sheet.row_numbers else None,
                "blank_rows_skipped": sheet.blank_rows,
            }
        )

    result.row_count = total_rows
    result.extra_metadata["sheets"] = sheet_meta
    result.extra_metadata["sheet_names"] = [s.name for s in sheets]


def _load_csv(path: str, filename: str, result: ParseResult, pd: Any) -> Optional[List[_Sheet]]:
    """Read a CSV while keeping every data record aligned to its spreadsheet row.

    ``skip_blank_lines=False`` matters: pandas would otherwise silently drop blank lines
    and shift every subsequent row number by one, which is exactly the kind of quiet
    off-by-one that makes a cited row point at the wrong account. Blank records are
    removed *after* their true row numbers have been recorded.

    ``keep_default_na=False`` with an explicit empty-string NA also matters for audit
    data: a literal "N/A" in an MFA column is a recorded value, not a missing one, and
    it must survive into the chunk text rather than being rendered as a blank cell.
    """
    raw = _read_bytes(path)
    if _looks_binary(raw):
        result.warn("File has a .csv extension but contains binary data; it was not parsed.")
        return []

    text, encoding, warning = _decode_text(raw)
    result.extra_metadata["encoding"] = encoding
    if warning:
        result.warn(warning)

    lines = text.splitlines()
    lead_blank = 0
    while lead_blank < len(lines) and not lines[lead_blank].strip():
        lead_blank += 1
    if lead_blank >= len(lines):
        result.warn("CSV contains no non-blank lines.")
        return []
    header_row = lead_blank + 1
    body = "\n".join(lines[lead_blank:])

    read_kwargs = {"skip_blank_lines": False, "keep_default_na": False, "na_values": [""]}
    frame = None
    try:
        frame = pd.read_csv(io.StringIO(body), **read_kwargs)
    except pd.errors.EmptyDataError:
        result.warn("CSV contains no parsable columns.")
        return []
    except pd.errors.ParserError as exc:
        frame = _repair_csv(body, result, pd, read_kwargs, str(exc))
    except Exception as exc:  # noqa: BLE001
        result.warn("CSV could not be read ({0}: {1}); falling back to plain-text parsing.".format(type(exc).__name__, exc))
        return None

    if frame is None:
        return None

    sheet = _finalise_sheet("", frame, header_row, result)
    if sheet is not None and len(sheet.frame.columns) == 1:
        # pandas will read free text as a one-column table with a sentence for a column
        # name. The content is still captured faithfully, but the auditor should be told
        # that what was indexed may not be the table they expected.
        header_cell = str(sheet.frame.columns[0])
        if len(header_cell.split()) >= 3:
            result.warn(
                "The CSV parsed as a single column named '{0}'; it may be free text rather than "
                "delimited data. The content was still captured, but check the delimiter.".format(
                    _clip(header_cell, 60)
                )
            )
    return [sheet] if sheet is not None else []


def _repair_csv(body: str, result: ParseResult, pd: Any, read_kwargs: Dict[str, Any], error: str) -> Optional[Any]:
    """Re-read a malformed CSV, repairing bad lines *in place* rather than skipping them.

    ``on_bad_lines="skip"`` would be the obvious fix and is the wrong one here: dropping
    a line shifts every row number after it, so a cited exception would silently point at
    its neighbour. Padding short lines and folding surplus fields into the last column
    keeps every record on its own row number and keeps the anomaly visible in the text.
    """
    try:
        width = len(pd.read_csv(io.StringIO(body), nrows=0).columns)
    except Exception:  # noqa: BLE001
        width = 0

    repaired: List[int] = []

    def _fix(bad_line: Sequence[Any]) -> List[Any]:
        values = list(bad_line)
        repaired.append(len(values))
        if width and len(values) > width:
            head = values[: width - 1]
            head.append(",".join(str(v) for v in values[width - 1 :]))
            return head
        while width and len(values) < width:
            values.append(None)
        return values

    try:
        frame = pd.read_csv(io.StringIO(body), engine="python", on_bad_lines=_fix, **read_kwargs)
    except Exception as exc:  # noqa: BLE001
        result.warn(
            "CSV is malformed and could not be repaired ({0}: {1}); falling back to plain-text "
            "parsing.".format(type(exc).__name__, exc)
        )
        return None

    result.warn(
        "{0} malformed line(s) did not match the header width ({1} column(s)) and were padded or "
        "merged into the final column; spreadsheet row numbering was preserved. Original parser "
        "error: {2}".format(len(repaired), width, " ".join(str(error).split()))
    )
    result.extra_metadata["repaired_rows"] = len(repaired)
    return frame


def _load_excel(path: str, filename: str, extension: str, result: ParseResult, pd: Any) -> List[_Sheet]:
    """Read every sheet, locating each sheet's real header row.

    The workbook is read with ``header=None`` so the parsed grid keeps a 1:1
    correspondence with spreadsheet rows even when a sheet starts below row 1; the header
    is then located explicitly. Reading with ``header=0`` would quietly treat whatever
    landed first as column names and put every later row number out by the offset.
    """
    engine = "openpyxl" if extension == ".xlsx" else None
    try:
        grids = pd.read_excel(path, sheet_name=None, header=None, engine=engine)
    except ImportError as exc:
        result.warn(
            "Legacy '{0}' workbooks need the 'xlrd' package, which is not installed ({1}). "
            "Re-save the file as .xlsx.".format(extension, exc)
        )
        return []
    except Exception as exc:  # noqa: BLE001
        result.warn("Workbook could not be opened ({0}: {1}).".format(type(exc).__name__, exc))
        return []

    sheets: List[_Sheet] = []
    for sheet_name, grid in grids.items():
        name = str(sheet_name)
        if grid is None or grid.shape[0] == 0 or grid.shape[1] == 0:
            result.warn("Sheet '{0}' is empty; a summary chunk was still recorded for it.".format(name))
            sheets.append(_empty_sheet(name, pd))
            continue

        header_idx = None
        for position in range(grid.shape[0]):
            if grid.iloc[position].notna().any():
                header_idx = position
                break
        if header_idx is None:
            result.warn("Sheet '{0}' contains no data; a summary chunk was still recorded for it.".format(name))
            sheets.append(_empty_sheet(name, pd))
            continue

        header_values = grid.iloc[header_idx].tolist()
        columns = _clean_columns(header_values)
        data = grid.iloc[header_idx + 1 :].reset_index(drop=True)
        data.columns = columns
        data = _coerce_types(data, pd)
        header_row = header_idx + 1
        sheet = _finalise_sheet(name, data, header_row, result)
        if sheet is not None:
            sheets.append(sheet)

    if not sheets:
        result.warn("Workbook contains no readable sheets.")
    return sheets


def _empty_sheet(name: str, pd: Any) -> _Sheet:
    return _Sheet(name=name, frame=pd.DataFrame(), row_numbers=[], header_row=1, notes=["Sheet is empty."])


def _clean_columns(values: Sequence[Any]) -> List[str]:
    """Give every column a usable, unique name so citations can list real columns."""
    names: List[str] = []
    seen: Dict[str, int] = {}
    for position, value in enumerate(values):
        name = format_cell(value, 80).strip()
        if not name or name.lower().startswith("unnamed:"):
            name = "column_{0}".format(position + 1)
        if name in seen:
            seen[name] += 1
            name = "{0}.{1}".format(name, seen[name])
        else:
            seen[name] = 0
        names.append(name)
    return names


def _coerce_types(frame: Any, pd: Any) -> Any:
    """Restore per-column types lost by reading the grid without a header row."""
    for column in frame.columns:
        series = frame[column]
        if series.dtype != object:
            continue
        non_null = series.dropna()
        if non_null.empty:
            continue
        if all(isinstance(value, (datetime, date)) for value in non_null):
            try:
                frame[column] = pd.to_datetime(series, errors="coerce")
            except Exception:  # noqa: BLE001
                pass
            continue
        try:
            converted = pd.to_numeric(series, errors="coerce")
        except (TypeError, ValueError):
            continue
        if int(converted.notna().sum()) == int(non_null.shape[0]):
            frame[column] = converted
    return frame


def _finalise_sheet(name: str, frame: Any, header_row: int, result: ParseResult) -> Optional[_Sheet]:
    """Attach true spreadsheet row numbers, then drop wholly blank records."""
    frame = frame.reset_index(drop=True)
    frame.columns = _clean_columns(list(frame.columns))
    row_numbers = [header_row + 1 + position for position in range(int(frame.shape[0]))]

    if frame.shape[0]:
        blank_mask = frame.isna().all(axis=1).tolist()
    else:
        blank_mask = []
    blank_rows = [row_numbers[i] for i, is_blank in enumerate(blank_mask) if is_blank]
    if blank_rows:
        keep = [i for i, is_blank in enumerate(blank_mask) if not is_blank]
        frame = frame.iloc[keep].reset_index(drop=True)
        row_numbers = [row_numbers[i] for i in keep]

    if int(frame.shape[0]) == 0:
        label = "Sheet '{0}'".format(name) if name else "File"
        result.warn("{0} has a header but no data rows; a summary chunk was still recorded.".format(label))

    return _Sheet(name=name, frame=frame, row_numbers=row_numbers, header_row=header_row, blank_rows=blank_rows)


def _summarise_sheet(sheet: _Sheet, filename: str) -> Tuple[str, Dict[str, Any]]:
    """Build the one TABLE_SUMMARY chunk that describes a whole population.

    This is the chunk that makes population-level statements checkable. Retrieval will
    rarely surface all 100 rows of a user listing, so without a summary a model either
    guesses the totals or refuses to answer. With one, it can say "10 of 100 accounts
    show MFA_Status = Disabled" and name the exact rows to inspect - and a reviewer can
    disprove it in seconds.
    """
    frame = sheet.frame
    rows = int(frame.shape[0])
    columns = [str(c) for c in frame.columns]
    label = filename + (" [sheet: {0}]".format(sheet.name) if sheet.name else "")

    lines: List[str] = ["TABLE SUMMARY - {0}".format(label)]
    meta: Dict[str, Any] = {
        "summary": True,
        "sheet": sheet.name,
        "header_row": sheet.header_row,
        "data_rows": rows,
        "column_count": len(columns),
        "columns": columns,
    }

    if rows == 0 or not columns:
        lines.append("This sheet contains no data rows; there is no population to test.")
        if columns:
            lines.append("Declared columns: " + ", ".join(columns))
        meta["empty"] = True
        return "\n".join(lines), meta

    first_row, last_row = sheet.row_numbers[0], sheet.row_numbers[-1]
    lines.append(
        "Population: {0} data row(s) x {1} column(s). Header on spreadsheet row {2}; data on "
        "spreadsheet rows {3}-{4}.".format(rows, len(columns), sheet.header_row, first_row, last_row)
    )
    lines.append(
        "Row numbers below are 1-based spreadsheet rows exactly as the file opens in a spreadsheet "
        "application. Individual records appear in the TABLE_ROWS chunks of this file."
    )

    lines.append("")
    lines.append("COLUMNS (name | dtype | non-empty | empty | distinct)")
    dtypes: Dict[str, str] = {}
    null_counts: Dict[str, int] = {}
    distinct_counts: Dict[str, int] = {}
    for column in frame.columns:
        series = frame[column]
        non_null = int(series.notna().sum())
        nulls = rows - non_null
        distinct = int(series.nunique(dropna=True))
        dtypes[str(column)] = str(series.dtype)
        null_counts[str(column)] = nulls
        distinct_counts[str(column)] = distinct
        lines.append(
            "  {0} | {1} | {2} | {3} | {4}".format(str(column), str(series.dtype), non_null, nulls, distinct)
        )
    meta["dtypes"] = dtypes
    meta["null_counts"] = null_counts
    meta["distinct_counts"] = distinct_counts

    value_counts_meta: Dict[str, Dict[str, int]] = {}
    value_rows_meta: Dict[str, Dict[str, List[int]]] = {}
    profiled: List[str] = []
    high_cardinality: List[str] = []
    for column in frame.columns:
        series = frame[column]
        try:
            counts = series.value_counts(dropna=True)
        except TypeError:  # unhashable cell values cannot be profiled
            continue
        if 0 < len(counts) <= LOW_CARDINALITY_MAX:
            profiled.append(str(column))
            rendered: List[str] = []
            per_value: Dict[str, int] = {}
            row_lists: Dict[str, List[int]] = {}
            values = series.tolist()
            for value, count in counts.items():
                shown = format_cell(value, 60) or "(blank)"
                count = int(count)
                per_value[shown] = count
                rendered.append("{0} = {1} ({2:.1f}%)".format(shown, count, 100.0 * count / rows))
                if count <= ROW_LIST_MAX_OCCURRENCES and count * 2 <= rows:
                    hits = [sheet.row_numbers[i] for i, item in enumerate(values) if item == value]
                    if hits:
                        row_lists[shown] = [int(h) for h in hits]
            if rendered:
                if not value_counts_meta:
                    lines.append("")
                    lines.append(
                        "VALUE COUNTS (every column with {0} or fewer distinct values)".format(LOW_CARDINALITY_MAX)
                    )
                lines.append("  {0}: {1}".format(str(column), " | ".join(rendered)))
                for shown, hits in row_lists.items():
                    printed = ", ".join(str(h) for h in hits[:ROW_LIST_MAX_PRINTED])
                    suffix = " (+{0} more)".format(len(hits) - ROW_LIST_MAX_PRINTED) if len(hits) > ROW_LIST_MAX_PRINTED else ""
                    lines.append(
                        "    {0} = {1} occurs in {2} row(s), at spreadsheet rows: {3}{4}".format(
                            str(column), shown, len(hits), printed, suffix
                        )
                    )
            value_counts_meta[str(column)] = per_value
            if row_lists:
                value_rows_meta[str(column)] = row_lists
        elif len(counts) > LOW_CARDINALITY_MAX:
            high_cardinality.append(str(column))
    meta["value_counts"] = value_counts_meta
    meta["value_row_numbers"] = value_rows_meta

    if high_cardinality:
        lines.append("")
        lines.append("HIGH-CARDINALITY COLUMNS (examples only)")
        for column in high_cardinality:
            examples = [format_cell(v, 40) for v in frame[column].dropna().head(3).tolist()]
            lines.append(
                "  {0}: {1} distinct value(s), e.g. {2}".format(
                    column, distinct_counts[column], ", ".join(e for e in examples if e)
                )
            )

    numeric = frame.select_dtypes(include=["number"])
    if numeric.shape[1]:
        lines.append("")
        lines.append("NUMERIC COLUMNS (non-empty | min | median | mean | max)")
        numeric_meta: Dict[str, Dict[str, Any]] = {}
        for column in numeric.columns:
            series = numeric[column].dropna()
            if series.empty:
                continue
            stats = {
                "count": int(series.shape[0]),
                "min": _json_safe(series.min()),
                "median": _json_safe(series.median()),
                "mean": round(float(series.mean()), 4),
                "max": _json_safe(series.max()),
            }
            numeric_meta[str(column)] = stats
            lines.append(
                "  {0} | {1} | {2} | {3} | {4} | {5}".format(
                    str(column), stats["count"], stats["min"], stats["median"], stats["mean"], stats["max"]
                )
            )
        meta["numeric_summary"] = numeric_meta

    if sheet.blank_rows or sheet.notes:
        lines.append("")
        lines.append("NOTES")
        if sheet.blank_rows:
            preview = ", ".join(str(r) for r in sheet.blank_rows[:20])
            lines.append(
                "  {0} wholly blank row(s) were excluded from the counts above (spreadsheet rows {1}).".format(
                    len(sheet.blank_rows), preview
                )
            )
        for note in sheet.notes:
            lines.append("  " + note)
    meta["blank_rows_skipped"] = [int(r) for r in sheet.blank_rows]
    meta["profiled_columns"] = profiled

    return "\n".join(lines), meta


# ---- plain text and markdown
def _parse_text(path: str, filename: str, result: ParseResult) -> None:
    """Split on blank lines and headings, then pack to ``chunk_size`` with overlap."""
    settings = get_settings()
    raw = _read_bytes(path)
    if _looks_binary(raw):
        result.warn(
            "File has a text extension but contains binary data; it was not parsed. "
            "Re-upload it in its native format."
        )
        return

    text, encoding, warning = _decode_text(raw)
    result.extra_metadata["encoding"] = encoding
    if warning:
        result.warn(warning)

    normalised = normalise_whitespace(text)
    if not normalised:
        result.warn("File contains no readable text.")
        return

    sections_seen: List[str] = []
    blocks, _ = _blocks_from_lines(normalised.split("\n"), None, sections_seen)
    packed = pack_blocks(
        blocks,
        chunk_size=settings.chunk_size,
        overlap=settings.chunk_overlap,
        flush_on_section_change=True,
        min_section_chunk_chars=0,  # as in the DOCX parser: never mislabel a chunk's section
    )
    _append_text_chunks(packed, filename, result)
    result.extra_metadata["sections"] = sections_seen[:50]
    result.extra_metadata["line_count"] = len(normalised.split("\n"))


def _append_text_chunks(packed: Sequence[PackedChunk], filename: str, result: ParseResult) -> None:
    for part in packed:
        result.chunks.append(
            ParsedChunk(
                text=part.text,
                locator=SourceLocator(
                    filename=filename,
                    source_type=SourceType.TEXT_BLOCK.value,
                    section=part.section,
                    paragraph_index=part.first_index or None,
                ),
                extra_metadata={
                    "line_start": part.line_start,
                    "line_end": part.line_end,
                    "sections": part.sections,
                },
            )
        )


# ---- JSON
def _parse_json(path: str, filename: str, result: ParseResult) -> None:
    """Pretty-print, then chunk on line boundaries so every chunk has a real line range.

    The most recent top-level key is carried into ``section`` - for a configuration
    export that is usually the setting group an auditor would ask about by name.
    """
    settings = get_settings()
    raw = _read_bytes(path)
    if _looks_binary(raw):
        result.warn("File has a .json extension but contains binary data; it was not parsed.")
        return

    text, encoding, warning = _decode_text(raw)
    result.extra_metadata["encoding"] = encoding
    if warning:
        result.warn(warning)

    try:
        document = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        result.warn("File is not valid JSON ({0}); it was parsed as plain text instead.".format(exc))
        result.extra_metadata["parser"] = "text-fallback"
        _parse_text(path, filename, result)
        return

    pretty = json.dumps(document, indent=2, ensure_ascii=False, sort_keys=False, default=str)
    lines = pretty.split("\n")
    result.extra_metadata["json_top_level"] = type(document).__name__
    if isinstance(document, dict):
        result.extra_metadata["json_keys"] = [str(k) for k in list(document.keys())[:50]]
    elif isinstance(document, list):
        result.extra_metadata["json_items"] = len(document)

    size = max(200, int(settings.chunk_size))
    overlap_lines = 2 if settings.chunk_overlap else 0
    buffer: List[str] = []
    buffer_len = 0
    start_line = 1
    section: Optional[str] = None
    chunk_section: Optional[str] = None

    def flush(end_line: int) -> None:
        nonlocal buffer, buffer_len, start_line, chunk_section
        body = "\n".join(buffer).strip()
        if body:
            result.chunks.append(
                ParsedChunk(
                    text=body,
                    locator=SourceLocator(
                        filename=filename,
                        source_type=SourceType.TEXT_BLOCK.value,
                        section=chunk_section,
                    ),
                    extra_metadata={"line_start": start_line, "line_end": end_line, "format": "json"},
                )
            )
        carry = buffer[-overlap_lines:] if overlap_lines and len(buffer) > overlap_lines else []
        start_line = end_line - len(carry) + 1
        buffer = list(carry)
        buffer_len = sum(len(line) + 1 for line in buffer)
        chunk_section = section

    for number, line in enumerate(lines, start=1):
        match = _JSON_TOP_KEY.match(line)
        if match:
            section = match.group(1)
        if not buffer:
            start_line = number
            chunk_section = section
        buffer.append(line)
        buffer_len += len(line) + 1
        if buffer_len >= size:
            flush(number)
    if buffer:
        flush(len(lines))

    if not result.chunks:
        # A tiny JSON document (e.g. "{}") still deserves a chunk rather than silence.
        for piece in window_text(pretty, size, 0):
            result.chunks.append(
                ParsedChunk(
                    text=piece,
                    locator=SourceLocator(filename=filename, source_type=SourceType.TEXT_BLOCK.value),
                    extra_metadata={"format": "json"},
                )
            )
