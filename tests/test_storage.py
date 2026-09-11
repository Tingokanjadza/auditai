"""Evidence storage: integrity, content addressing and path safety.

Uploaded filenames are untrusted input. The tests below check that a hostile name can
only ever resolve to a leaf inside the project's upload directory, and that identical
bytes collapse onto one stored artefact instead of quietly becoming a second copy that
could drift from the first.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.evidence.storage import (
    StoredFile,
    delete_stored,
    guess_content_type,
    project_dir,
    read_stored,
    safe_filename,
    save_upload,
    sha256_bytes,
    verify_stored,
)

PAYLOAD = b"Account,MFA_Status\nsvc-account-001,Disabled\n"


def _inside(child, parent) -> bool:
    child_path = Path(str(child)).resolve()
    parent_path = Path(str(parent)).resolve()
    return parent_path in child_path.parents


# ---- hashing
def test_sha256_bytes_matches_hashlib():
    assert sha256_bytes(PAYLOAD) == hashlib.sha256(PAYLOAD).hexdigest()


def test_sha256_of_empty_input_is_defined():
    assert sha256_bytes(b"") == hashlib.sha256(b"").hexdigest()


# ---- saving
def test_save_upload_writes_inside_the_project_directory(settings):
    stored = save_upload(PAYLOAD, "listing.csv", 42)
    assert isinstance(stored, StoredFile)
    assert stored.exists
    assert _inside(stored.stored_path, Path(settings.upload_dir) / "project_42")
    assert stored.size_bytes == len(PAYLOAD)
    assert stored.sha256 == sha256_bytes(PAYLOAD)
    assert stored.extension == ".csv"
    assert stored.content_type == "text/csv"


def test_stored_name_is_content_addressed(settings):
    stored = save_upload(PAYLOAD, "listing.csv", 43)
    assert Path(stored.stored_path).name == "listing_{0}.csv".format(stored.sha256[:12])


def test_the_same_bytes_twice_collapse_onto_one_file(settings):
    first = save_upload(PAYLOAD, "listing.csv", 44)
    second = save_upload(PAYLOAD, "listing.csv", 44)
    assert first.stored_path == second.stored_path
    assert len(list(Path(first.stored_path).parent.glob("listing_*.csv"))) == 1


def test_different_bytes_under_one_name_never_overwrite_each_other(settings):
    """A silent overwrite would destroy evidence rather than just confuse a listing."""
    first = save_upload(b"version one", "export.csv", 45)
    second = save_upload(b"version two", "export.csv", 45)
    assert first.stored_path != second.stored_path
    assert read_stored(first.stored_path) == b"version one"
    assert read_stored(second.stored_path) == b"version two"


def test_a_traversing_upload_name_cannot_escape_the_upload_directory(settings):
    stored = save_upload(PAYLOAD, "../../../../etc/passwd", 46)
    assert _inside(stored.stored_path, Path(settings.upload_dir))
    assert "etc" not in Path(stored.stored_path).parts[-2:]


def test_projects_are_kept_in_separate_directories(settings):
    one = save_upload(PAYLOAD, "listing.csv", 47)
    two = save_upload(PAYLOAD, "listing.csv", 48)
    assert Path(one.stored_path).parent != Path(two.stored_path).parent
    assert Path(one.stored_path).parent.name == "project_47"


def test_project_dir_is_created_on_demand(settings):
    directory = project_dir(4711)
    assert directory.is_dir()
    assert _inside(directory, settings.upload_dir)


# ---- filename sanitisation
@pytest.mark.parametrize(
    "given,expected",
    [
        ("simple.csv", "simple.csv"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\admin\\evil.docx", "evil.docx"),
        # Each run of unsafe characters collapses to one underscore, including the
        # trailing ")" - hence the underscore before the extension.
        ("spaces and (brackets).xlsx", "spaces_and_brackets_.xlsx"),
        ("", "upload"),
        ("...", "upload"),
        ("UPPER.CSV", "UPPER.csv"),
    ],
)
def test_safe_filename(given, expected):
    assert safe_filename(given) == expected


def test_safe_filename_bounds_the_stem():
    assert len(Path(safe_filename("a" * 500 + ".csv")).stem) <= 80


@pytest.mark.parametrize(
    "extension,expected",
    [
        (".csv", "text/csv"),
        (".md", "text/markdown"),
        (".json", "application/json"),
        (".pdf", "application/pdf"),
        (".nonsense", "application/octet-stream"),
        ("", "application/octet-stream"),
    ],
)
def test_guess_content_type(extension, expected):
    assert guess_content_type(extension) == expected


# ---- reading, verifying, deleting
def test_read_stored_returns_the_exact_bytes():
    stored = save_upload(PAYLOAD, "listing.csv", 49)
    assert read_stored(stored.stored_path) == PAYLOAD


def test_verify_stored_reproves_integrity():
    stored = save_upload(PAYLOAD, "listing.csv", 50)
    assert verify_stored(stored.stored_path, stored.sha256) is True
    assert verify_stored(stored.stored_path, "0" * 64) is False
    assert verify_stored(stored.stored_path + ".missing", stored.sha256) is None


def test_verify_stored_detects_tampering():
    stored = save_upload(PAYLOAD, "listing.csv", 51)
    Path(stored.stored_path).write_bytes(PAYLOAD + b"appended\n")
    assert verify_stored(stored.stored_path, stored.sha256) is False


def test_delete_stored_is_idempotent_and_reports_honestly():
    stored = save_upload(PAYLOAD, "listing.csv", 52)
    assert delete_stored(stored.stored_path) is True
    assert delete_stored(stored.stored_path) is False


@pytest.mark.parametrize("path", ["", "/etc/hosts", "/tmp", "relative/thing.csv"])
def test_delete_stored_refuses_anything_outside_the_upload_directory(path):
    assert delete_stored(path) is False


def test_empty_upload_is_stored_rather_than_rejected():
    """A zero-byte artefact is still something the auditor handed over."""
    stored = save_upload(b"", "blank.csv", 53)
    assert stored.exists
    assert stored.size_bytes == 0
