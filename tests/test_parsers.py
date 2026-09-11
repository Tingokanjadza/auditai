"""Document parsing, and the one convention the whole citation trail rests on.

If a locator is wrong, every downstream guarantee is theatre: a citation that names row
14 has to point at the row an auditor sees when they open the file at row 14. The row
numbering tests below are therefore the heart of this module, not boilerplate.
"""

from __future__ import annotations

import json

import pytest

from app.evidence.parsers import ParseResult, parse_file, supported_extensions
from app.schemas.enums import SourceType
from tests.conftest import (
    FIXTURE_CONFIG_FIRST_ROW,
    FIXTURE_CSV_EXCEPTION_ROWS,
    FIXTURE_CSV_EXCEPTIONS,
    FIXTURE_CSV_ROWS,
    FIXTURE_POLICY_SENTENCE,
)


def _chunks_of(result, source_type):
    return [c for c in result.chunks if c.locator.source_type == source_type.value]


# --------------------------------------------------------------------- row numbering
def test_csv_row_numbers_are_the_real_one_based_spreadsheet_rows(sample_files):
    """The fixture plants five exceptions at rows 7, 14, 23, 38 and 45.

    Data row *n* must appear as spreadsheet row *n + 1*, because the header occupies
    row 1. The assertion is made against the rendered chunk text, which is what a model
    is actually shown and what it quotes.
    """
    result = parse_file(str(sample_files["csv"]), sample_files["csv"].name)
    assert result.row_count == FIXTURE_CSV_ROWS
    assert not result.warnings

    row_chunks = _chunks_of(result, SourceType.TABLE_ROWS)
    exception_rows = []
    for chunk in row_chunks:
        for line in chunk.text.splitlines():
            if "Disabled" in line and line.strip().lower().startswith("row "):
                exception_rows.append(int(line.split()[1].rstrip(":")))
    assert exception_rows == FIXTURE_CSV_EXCEPTION_ROWS


def test_the_named_account_sits_on_the_row_the_locator_claims(sample_files):
    """Row 7 must carry ``svc-account-006``: the sixth data record, header on row 1."""
    result = parse_file(str(sample_files["csv"]), sample_files["csv"].name)
    lines = [
        line
        for chunk in _chunks_of(result, SourceType.TABLE_ROWS)
        for line in chunk.text.splitlines()
        if line.startswith("row 7:")
    ]
    assert len(lines) == 1
    assert "svc-account-006" in lines[0]
    assert "Disabled" in lines[0]


def test_row_numbers_survive_a_blank_line_in_the_middle_of_a_csv(tmp_path):
    """pandas would silently drop the blank record and shift every row after it."""
    path = tmp_path / "gappy.csv"
    path.write_text("Account,MFA_Status\na,Enabled\n\nb,Disabled\n", encoding="utf-8")
    result = parse_file(str(path), "gappy.csv")
    rows = _chunks_of(result, SourceType.TABLE_ROWS)[0]
    assert rows.locator.row_numbers == [2, 4]
    assert "row 4: b | Disabled" in rows.text


def test_table_row_chunks_carry_their_row_numbers_in_the_locator(sample_files):
    result = parse_file(str(sample_files["csv"]), sample_files["csv"].name)
    for chunk in _chunks_of(result, SourceType.TABLE_ROWS):
        assert chunk.locator.row_numbers
        assert chunk.locator.row_start == chunk.locator.row_numbers[0]
        assert chunk.locator.row_end == chunk.locator.row_numbers[-1]
        assert min(chunk.locator.row_numbers) >= 2  # never the header


# ---------------------------------------------------------------- population summary
def test_exactly_one_table_summary_chunk_per_sheet(sample_files):
    csv_result = parse_file(str(sample_files["csv"]), sample_files["csv"].name)
    assert len(_chunks_of(csv_result, SourceType.TABLE_SUMMARY)) == 1

    xlsx_result = parse_file(str(sample_files["xlsx"]), sample_files["xlsx"].name)
    summaries = _chunks_of(xlsx_result, SourceType.TABLE_SUMMARY)
    assert len(summaries) == 2
    assert {c.locator.sheet_name for c in summaries} == {"PolicyRules", "Scope"}


def test_the_summary_states_the_population_and_lists_the_exception_rows(sample_files):
    """This chunk is what lets a model reason about 50 rows without seeing all 50."""
    result = parse_file(str(sample_files["csv"]), sample_files["csv"].name)
    summary = _chunks_of(result, SourceType.TABLE_SUMMARY)[0].text

    assert "{0} data row(s)".format(FIXTURE_CSV_ROWS) in summary
    assert "Disabled = {0}".format(FIXTURE_CSV_EXCEPTIONS) in summary
    assert "Enabled = {0}".format(FIXTURE_CSV_ROWS - FIXTURE_CSV_EXCEPTIONS) in summary
    for row in FIXTURE_CSV_EXCEPTION_ROWS:
        assert str(row) in summary.split("spreadsheet rows:")[-1]


def test_high_cardinality_columns_are_summarised_by_example_not_enumerated(sample_files):
    result = parse_file(str(sample_files["csv"]), sample_files["csv"].name)
    summary = _chunks_of(result, SourceType.TABLE_SUMMARY)[0].text
    assert "HIGH-CARDINALITY COLUMNS" in summary
    assert "{0} distinct value(s)".format(FIXTURE_CSV_ROWS) in summary


# ------------------------------------------------------------------------ formats
def test_xlsx_keeps_each_sheet_separate_and_numbers_its_rows(sample_files):
    result = parse_file(str(sample_files["xlsx"]), sample_files["xlsx"].name)
    assert result.extra_metadata["sheet_names"] == ["PolicyRules", "Scope"]

    rule_rows = [
        c
        for c in _chunks_of(result, SourceType.TABLE_ROWS)
        if c.locator.sheet_name == "PolicyRules"
    ]
    assert len(rule_rows) == 1
    assert FIXTURE_CONFIG_FIRST_ROW in rule_rows[0].text
    assert rule_rows[0].locator.row_numbers == [2, 3, 4]


def test_docx_tracks_headings_and_emits_its_tables(sample_files):
    result = parse_file(str(sample_files["docx"]), sample_files["docx"].name)
    assert result.extra_metadata["parser"] == "python-docx"
    assert result.extra_metadata["table_count"] == 1

    paragraphs = _chunks_of(result, SourceType.DOCX_PARAGRAPH)
    assert paragraphs
    assert any("Section 2 - Privileged access" == c.locator.section for c in paragraphs)
    assert any("second factor" in c.text for c in paragraphs)

    tables = _chunks_of(result, SourceType.TABLE_ROWS)
    assert len(tables) == 1
    assert "MFA enrolment" in tables[0].text
    assert "Table 1" in tables[0].locator.section


def test_txt_is_sectioned_by_its_own_headings(sample_files):
    result = parse_file(str(sample_files["policy_txt"]), sample_files["policy_txt"].name)
    assert result.chunks
    text = "\n".join(c.text for c in result.chunks)
    assert FIXTURE_POLICY_SENTENCE in text
    assert all(c.locator.source_type == SourceType.TEXT_BLOCK.value for c in result.chunks)


def test_markdown_headings_become_locator_sections(tmp_path):
    path = tmp_path / "policy.md"
    path.write_text("# Title\n\nBody one.\n\n## Section 2\n\nBody two.\n", encoding="utf-8")
    result = parse_file(str(path), "policy.md")
    assert [c.locator.section for c in result.chunks] == ["Title", "Section 2"]


def test_json_is_parsed_as_text_without_losing_its_values(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"policy": {"min_length": 14}, "enforced": True}, indent=2), encoding="utf-8")
    result = parse_file(str(path), "config.json")
    assert result.chunks
    assert "min_length" in result.chunks[0].text
    assert "14" in result.chunks[0].text


def test_pdf_pages_are_numbered(generated_datasets):
    """DATASET-002 ships a real PDF; page numbers must reach the locator."""
    pdf = [p for p in generated_datasets["DATASET-002"] if p.suffix == ".pdf"]
    assert pdf, "DATASET-002 no longer ships a PDF"
    result = parse_file(str(pdf[0]), pdf[0].name)
    pages = _chunks_of(result, SourceType.PDF_PAGE)
    assert pages
    assert result.page_count >= 1
    assert all(c.locator.page_number is not None and c.locator.page_number >= 1 for c in pages)


# ------------------------------------------------------------------- every locator
@pytest.mark.parametrize("key", ["csv", "policy_txt", "docx", "xlsx"])
def test_every_chunk_can_say_where_it_came_from(sample_files, key):
    """A fragment that cannot name its source is not evidence an auditor can rely on."""
    path = sample_files[key]
    result = parse_file(str(path), path.name)
    assert result.chunks
    for chunk in result.chunks:
        assert chunk.locator.filename == path.name
        assert chunk.locator.source_type in SourceType.values()
        rendered = chunk.locator.render()
        assert rendered.startswith(path.name)
        assert len(rendered) > len(path.name)
        assert chunk.text.strip()
        assert chunk.char_count == len(chunk.text)


def test_chunk_indexes_are_contiguous_from_zero(sample_files):
    result = parse_file(str(sample_files["xlsx"]), sample_files["xlsx"].name)
    assert [c.chunk_index for c in result.chunks] == list(range(len(result.chunks)))


# ------------------------------------------------------------------- robustness
def test_unsupported_extension_is_recorded_not_raised(tmp_path):
    path = tmp_path / "evidence.exe"
    path.write_bytes(b"MZ\x90\x00")
    result = parse_file(str(path), "evidence.exe")
    assert result.chunks == []
    assert result.ok is False
    assert any("Unsupported file extension" in w for w in result.warnings)


def test_empty_file_yields_a_warning_and_no_chunks(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")
    result = parse_file(str(path), "empty.csv")
    assert result.chunks == []
    assert any("empty" in w.lower() for w in result.warnings)


def test_missing_file_is_a_warning_not_an_exception(tmp_path):
    result = parse_file(str(tmp_path / "absent.csv"), "absent.csv")
    assert result.chunks == []
    assert any("not found" in w.lower() for w in result.warnings)


def test_binary_content_behind_a_text_extension_is_refused_cleanly(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_bytes(b"\x00\x01\x02\xff" * 400)
    result = parse_file(str(path), "notes.txt")
    assert result.chunks == []
    assert any("binary" in w.lower() for w in result.warnings)


def test_csv_that_is_not_a_table_falls_back_to_text_rather_than_failing(tmp_path):
    path = tmp_path / "narrative.csv"
    path.write_text("This is prose that happens to have a .csv extension.\n", encoding="utf-8")
    result = parse_file(str(path), "narrative.csv")
    assert result.chunks, "content must never be lost because it did not tabulate"


@pytest.mark.parametrize(
    "name,payload",
    [
        ("weird.csv", b"\xff\xfe\x00o\x00k\x00"),
        ("ragged.csv", b"a,b,c\n1,2\n3,4,5,6\n"),
        ("headerless.csv", b"\n\n\n"),
        ("tiny.txt", b" "),
        ("broken.docx", b"not a zip file at all"),
        ("broken.xlsx", b"PK\x03\x04 truncated"),
        ("broken.pdf", b"%PDF-1.4 truncated"),
        ("bad.json", b"{not json"),
    ],
)
def test_parse_file_never_raises_on_malformed_input(tmp_path, name, payload):
    """Ingestion must record a bad file, not crash on it - a tool that crashes on a
    strange artefact teaches its user to work around it rather than record it."""
    path = tmp_path / name
    path.write_bytes(payload)
    result = parse_file(str(path), name)
    assert isinstance(result, ParseResult)
    assert isinstance(result.warnings, list)


def test_the_upload_name_is_what_appears_in_citations_not_the_stored_name(tmp_path):
    """Uploads are stored content-addressed; the auditor's name is the citable one."""
    path = tmp_path / "stored_abc123.csv"
    path.write_text("A,B\n1,2\n", encoding="utf-8")
    result = parse_file(str(path), "Original Name.csv")
    assert all(c.locator.filename == "Original Name.csv" for c in result.chunks)


def test_supported_extensions_matches_the_configured_list():
    from app.config import SUPPORTED_UPLOAD_EXTENSIONS

    assert supported_extensions() == list(SUPPORTED_UPLOAD_EXTENSIONS)


def test_parse_result_helpers():
    result = ParseResult()
    assert result.ok is False and result.chunk_count == 0
    result.warn("something")
    result.warn("something")
    assert result.warnings == ["something"]
    assert set(result.to_dict()) >= {"chunk_count", "row_count", "warnings"}
