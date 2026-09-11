"""Format-agnostic chunking primitives shared by every parser.

A PDF page, a Word section and a block of spreadsheet rows are very different objects,
but retrieval has to treat them as comparable units. If each parser invented its own
splitting rules the same policy sentence would be retrievable from a ``.docx`` and
effectively invisible in a ``.pdf``, and any chunk-level metric would be comparing
nothing. So every decision about *how large* a chunk may be, *where* it may be cut and
*how* tabular rows are rendered for a model to read lives here, leaving each parser to
answer only the question it alone can answer: what the document's natural units are and
exactly where each one came from.

The other reason this module exists is locator fidelity. Splitting is expressed over
:class:`TextBlock` objects that already carry their own provenance (section heading,
ordinal, line range), so a chunk produced by :func:`pack_blocks` still knows precisely
which part of the source it covers. Splitting raw strings and reconstructing the
provenance afterwards is exactly how citations start pointing at the wrong page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence

from app.config import get_settings
from app.llm.base import estimate_tokens  # re-exported: one token heuristic for the whole app

__all__ = [
    "PackedChunk",
    "TextBlock",
    "detect_heading",
    "estimate_tokens",
    "format_cell",
    "normalise_whitespace",
    "pack_blocks",
    "render_table_rows",
    "window_text",
]


# ---- text hygiene
_WS_RUN = re.compile(r"[ \t\u00a0\u2007\u2009\u202f]+")
_MULTI_NEWLINE = re.compile(r"\n{3,}")
#: Control characters that PDF and DOCX extraction routinely leak into text.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def normalise_whitespace(text: str) -> str:
    """Collapse extraction noise without destroying paragraph structure.

    Blank lines survive (they are the strongest signal of a paragraph boundary), but
    runs of spaces, stray control characters and the soft hyphens pypdf emits mid-word
    are removed - a stray soft hyphen inside a word would otherwise stop the
    citation validator from matching a quote against its own source text.
    """
    if not text:
        return ""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = cleaned.replace("\u00ad", "")  # soft hyphen
    cleaned = _CONTROL.sub(" ", cleaned)
    cleaned = _WS_RUN.sub(" ", cleaned)
    cleaned = "\n".join(line.strip() for line in cleaned.split("\n"))
    cleaned = _MULTI_NEWLINE.sub("\n\n", cleaned)
    return cleaned.strip()


# ---- heading detection
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")
_SETEXT_RULE = re.compile(r"^(=+|-{3,})$")
_NUMBERED = re.compile(r"^\(?(\d+(?:\.\d+)*)[.)]?\s+(\S.*)$")
_SECTION_WORD = re.compile(
    r"^(SECTION|ARTICLE|APPENDIX|ANNEX|PART|CHAPTER|SCHEDULE|CLAUSE|POLICY|PROCEDURE)\b[\s:.-]",
    re.IGNORECASE,
)
_SENTENCE_END = (".", "?", "!", ",", ";")


def detect_heading(line: str, next_line: str = "", prev_blank: bool = True) -> Optional[str]:
    """Return the heading text if ``line`` looks like a section heading, else ``None``.

    Deliberately conservative. A false positive silently mislabels the ``section`` of
    every following chunk, so the loosest rule (a short title-case line) additionally
    requires the preceding line to be blank - which is what separates a real heading
    from a wrapped fragment of a sentence in extracted PDF text.
    """
    stripped = (line or "").strip()
    if not stripped or len(stripped) > 120:
        return None

    md = _MD_HEADING.match(stripped)
    if md:
        return md.group(2).strip() or None

    if next_line and _SETEXT_RULE.match(next_line.strip()) and not stripped.endswith(_SENTENCE_END):
        return stripped

    if _SECTION_WORD.match(stripped) and len(stripped) <= 100:
        return stripped.rstrip(":")

    numbered = _NUMBERED.match(stripped)
    if numbered:
        body = numbered.group(2).strip()
        if len(stripped) <= 100 and len(body.split()) <= 14 and not body.endswith(_SENTENCE_END):
            return stripped.rstrip(":")

    letters = [ch for ch in stripped if ch.isalpha()]
    if letters and len(letters) >= 3 and all(ch.isupper() for ch in letters):
        if len(stripped) <= 90 and len(stripped.split()) <= 12:
            return stripped.rstrip(":")

    if prev_blank and not stripped.endswith(_SENTENCE_END) and len(stripped) <= 80:
        words = stripped.split()
        alpha_words = [w for w in words if w[:1].isalpha()]
        if 1 < len(words) <= 10 and alpha_words:
            capitalised = sum(1 for w in alpha_words if w[:1].isupper())
            if capitalised / float(len(alpha_words)) >= 0.6:
                return stripped.rstrip(":")

    return None


# ---- windowing
def window_text(text: str, chunk_size: Optional[int] = None, overlap: Optional[int] = None) -> List[str]:
    """Split a long string into overlapping windows, cutting at the best nearby boundary.

    Preference order for a cut point is paragraph break, line break, sentence end, then
    plain whitespace; a hard character cut is the last resort. Overlap exists so a
    requirement stated across a boundary is still retrievable as a whole from one of
    the two windows.
    """
    settings = get_settings()
    size = int(chunk_size or settings.chunk_size)
    size = max(200, size)
    step_back = settings.chunk_overlap if overlap is None else int(overlap)
    step_back = max(0, min(step_back, size // 2))

    body = (text or "").strip()
    if not body:
        return []
    if len(body) <= size:
        return [body]

    windows: List[str] = []
    start = 0
    length = len(body)
    while start < length:
        end = min(start + size, length)
        if end < length:
            end = _best_break(body, start, end)
        piece = body[start:end].strip()
        if piece:
            windows.append(piece)
        if end >= length:
            break
        start = max(end - step_back, start + 1)
    return windows


def _best_break(text: str, start: int, end: int) -> int:
    """Find a cut point in ``text[start:end]``, never earlier than the window midpoint."""
    floor = start + (end - start) // 2
    for marker in ("\n\n", "\n", ". ", "! ", "? ", "; ", " "):
        cut = text.rfind(marker, floor, end)
        if cut > floor:
            return cut + len(marker)
    return end


# ---- block packing
@dataclass
class TextBlock:
    """A natural unit of a document (paragraph, page line group, heading) with provenance."""

    text: str
    section: Optional[str] = None
    #: 1-based ordinal of this block within its document or page.
    index: int = 0
    line_start: Optional[int] = None
    line_end: Optional[int] = None


@dataclass
class PackedChunk:
    """One or more :class:`TextBlock` objects merged into a chunk-sized unit."""

    text: str
    section: Optional[str] = None
    sections: List[str] = field(default_factory=list)
    first_index: int = 0
    last_index: int = 0
    line_start: Optional[int] = None
    line_end: Optional[int] = None


def pack_blocks(
    blocks: Sequence[TextBlock],
    chunk_size: Optional[int] = None,
    overlap: Optional[int] = None,
    flush_on_section_change: bool = False,
    min_section_chunk_chars: int = 0,
) -> List[PackedChunk]:
    """Merge blocks into chunks of at most ``chunk_size`` characters.

    ``overlap`` is honoured by repeating whole trailing *blocks* rather than a slice of
    characters: a chunk that begins mid-sentence with text it cannot locate is worse for
    an auditor than a slightly larger chunk, and repeating whole blocks keeps
    ``line_start`` and ``first_index`` truthful for the repeated text too.

    ``flush_on_section_change`` closes a chunk when the heading changes, but only once
    the open chunk is at least ``min_section_chunk_chars`` long, so a document with many
    short headings does not shatter into unretrievably small fragments.
    """
    settings = get_settings()
    size = max(200, int(chunk_size or settings.chunk_size))
    carry_budget = settings.chunk_overlap if overlap is None else int(overlap)
    carry_budget = max(0, min(carry_budget, size // 2))

    packed: List[PackedChunk] = []
    current: List[TextBlock] = []

    def current_len() -> int:
        return sum(len(b.text) + 2 for b in current)

    def flush(carry: bool) -> None:
        if not current:
            return
        chunk = _merge(current)
        if chunk is not None:
            packed.append(chunk)
        keep: List[TextBlock] = []
        if carry and carry_budget > 0 and len(current) > 1:
            total = 0
            # Never carry the whole chunk forward - that would not terminate.
            for block in reversed(current[1:]):
                if total + len(block.text) > carry_budget:
                    break
                keep.insert(0, block)
                total += len(block.text)
        del current[:]
        current.extend(keep)

    for block in blocks:
        text = (block.text or "").strip()
        if not text:
            continue
        if len(text) > size:
            flush(carry=False)
            for window in window_text(text, size, carry_budget):
                packed.append(
                    PackedChunk(
                        text=window,
                        section=block.section,
                        sections=[block.section] if block.section else [],
                        first_index=block.index,
                        last_index=block.index,
                        line_start=block.line_start,
                        line_end=block.line_end,
                    )
                )
            continue

        if current:
            section_changed = flush_on_section_change and block.section != current[-1].section
            if section_changed and current_len() >= min_section_chunk_chars:
                flush(carry=False)
            elif current_len() + len(text) > size:
                flush(carry=True)
        current.append(TextBlock(text, block.section, block.index, block.line_start, block.line_end))

    flush(carry=False)
    return packed


def _merge(blocks: Sequence[TextBlock]) -> Optional[PackedChunk]:
    text = "\n\n".join(b.text.strip() for b in blocks if b.text.strip()).strip()
    if not text:
        return None
    sections: List[str] = []
    for block in blocks:
        if block.section and block.section not in sections:
            sections.append(block.section)
    line_starts = [b.line_start for b in blocks if b.line_start is not None]
    line_ends = [b.line_end for b in blocks if b.line_end is not None]
    return PackedChunk(
        text=text,
        section=blocks[0].section,
        sections=sections,
        first_index=blocks[0].index,
        last_index=blocks[-1].index,
        line_start=min(line_starts) if line_starts else None,
        line_end=max(line_ends) if line_ends else None,
    )


# ---- tabular rendering
def format_cell(value: Any, max_chars: int = 240) -> str:
    """Render one cell as short, single-line, model-readable text.

    Missing values render as an empty string rather than ``nan``/``NaT``: an auditor
    reading a quoted chunk should see a blank cell, and a model should not be able to
    quote the literal token "nan" back as though it were recorded data.
    """
    if value is None:
        return ""
    if isinstance(value, float):
        if value != value:  # NaN, without importing numpy here
            return ""
        if value.is_integer():
            return str(int(value))
    if hasattr(value, "strftime") and hasattr(value, "year"):
        # Dates read from a spreadsheet arrive as datetimes; "2024-03-01 00:00:00"
        # invites a model to quote a time the source never recorded.
        try:
            if getattr(value, "hour", 0) or getattr(value, "minute", 0) or getattr(value, "second", 0):
                return value.strftime("%Y-%m-%d %H:%M:%S")
            return value.strftime("%Y-%m-%d")
        except (ValueError, AttributeError):
            pass
    text = str(value)
    if text in ("NaT", "nan", "None", "<NA>"):
        return ""
    text = _WS_RUN.sub(" ", text.replace("\n", " ").replace("\r", " ")).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 3] + "..."
    return text


def render_table_rows(
    columns: Sequence[Any],
    rows: Sequence[Sequence[Any]],
    row_numbers: Sequence[int],
    include_header: bool = True,
    row_label: str = "row",
    max_cell_chars: int = 240,
) -> str:
    """Render rows so that every line names the real source row it came from.

    The header is repeated in every chunk (a chunk that starts at row 60 is useless if
    the column names only exist in the chunk that starts at row 2), and each rendered
    line is prefixed with its true spreadsheet row number, so a model quoting a line
    verbatim is quoting a locator along with it.
    """
    lines: List[str] = []
    header = [format_cell(col, 80) for col in columns]
    if include_header and header:
        lines.append("COLUMNS: " + " | ".join(header))
    for number, row in zip(row_numbers, rows):
        cells = [format_cell(value, max_cell_chars) for value in row]
        lines.append("{} {}: {}".format(row_label, number, " | ".join(cells)))
    return "\n".join(lines)
