"""Where uploaded evidence physically lives.

Evidence has to stay byte-identical to what the auditor handed over, and it has to be
possible to prove that later. Two decisions follow from that:

* Every file is hashed with SHA-256 on the way in, and the hash is stored on the
  ``EvidenceFile`` row. Re-hashing the stored bytes at any point re-proves that the
  chunks quoted in a report came from the artefact that was actually submitted.
* Stored names are **content-addressed** (``<stem>_<first 12 hex of the hash><ext>``).
  Two uploads of the same bytes collapse onto one stored file, while two different files
  that happen to share a name cannot overwrite each other - a silent overwrite would
  destroy evidence rather than just confuse a listing.

The auditor's original filename is never used as a path component; it is kept on the
database row for display and citation. Uploaded names are untrusted input, and a name
like ``../../etc/passwd`` must resolve to a harmless file inside the upload directory.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.config import get_settings

__all__ = [
    "StoredFile",
    "delete_stored",
    "project_dir",
    "read_stored",
    "safe_filename",
    "save_upload",
    "sha256_bytes",
]

#: Content types mimetypes does not reliably know about across platforms.
_CONTENT_TYPES = {
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
}

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class StoredFile:
    """The result of persisting one upload to disk."""

    stored_path: str
    sha256: str
    size_bytes: int
    extension: str
    content_type: str

    @property
    def exists(self) -> bool:
        return os.path.isfile(self.stored_path)


def sha256_bytes(data: bytes) -> str:
    """SHA-256 of the raw upload - the integrity anchor for the whole evidence trail."""
    return hashlib.sha256(data).hexdigest()


def safe_filename(filename: str) -> str:
    """Reduce an uploaded name to something that can only ever be a leaf filename."""
    base = os.path.basename(str(filename or "").replace("\\", "/").strip()) or "upload"
    base = _UNSAFE.sub("_", base).strip("._") or "upload"
    stem, extension = os.path.splitext(base)
    stem = (stem or "upload")[:80]
    return stem + extension[:12].lower()


def project_dir(project_id: int) -> Path:
    """Per-project upload directory, created on demand."""
    settings = get_settings()
    directory = Path(settings.upload_dir) / "project_{0}".format(int(project_id))
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def save_upload(data: bytes, filename: str, project_id: int) -> StoredFile:
    """Write ``data`` under ``upload_dir/project_<id>/`` and describe what was written.

    Writing the same bytes twice is a no-op rather than an error: the stored name is
    derived from the content hash, so a re-upload is recognised as the same artefact
    instead of quietly becoming a second copy that could drift from the first.
    """
    payload = bytes(data or b"")
    digest = sha256_bytes(payload)
    safe = safe_filename(filename)
    stem, extension = os.path.splitext(safe)
    extension = extension.lower()

    directory = project_dir(project_id)
    target = directory / "{0}_{1}{2}".format(stem, digest[:12], extension)

    resolved = target.resolve()
    root = directory.resolve()
    if root not in resolved.parents:  # pragma: no cover - safe_filename already prevents this
        raise ValueError("Refusing to write evidence outside the project upload directory.")

    if not resolved.exists() or resolved.stat().st_size != len(payload):
        with open(resolved, "wb") as handle:
            handle.write(payload)

    return StoredFile(
        stored_path=str(resolved),
        sha256=digest,
        size_bytes=len(payload),
        extension=extension,
        content_type=guess_content_type(extension),
    )


def guess_content_type(extension: str) -> str:
    extension = (extension or "").lower()
    if extension in _CONTENT_TYPES:
        return _CONTENT_TYPES[extension]
    guessed, _ = mimetypes.guess_type("file" + extension)
    return guessed or "application/octet-stream"


def read_stored(stored_path: str) -> bytes:
    """Read stored bytes back, e.g. to re-verify a hash or to offer a download."""
    with open(str(stored_path), "rb") as handle:
        return handle.read()


def delete_stored(stored_path: str) -> bool:
    """Delete a stored file, refusing any path outside the configured upload directory.

    Returns ``True`` only when a file was actually removed, so a caller can record
    honestly whether the artefact is gone or was already missing.
    """
    if not stored_path:
        return False
    settings = get_settings()
    root = Path(settings.upload_dir).resolve()
    try:
        target = Path(str(stored_path)).resolve()
    except OSError:  # pragma: no cover - malformed path
        return False
    if root != target and root not in target.parents:
        return False
    if not target.is_file():
        return False
    try:
        target.unlink()
    except OSError:
        return False
    return True


def verify_stored(stored_path: str, expected_sha256: str) -> Optional[bool]:
    """Re-hash a stored file. ``None`` when the file is missing, else whether it matches.

    Nothing calls this in the normal ingestion path; it exists so the integrity claim
    made in a report can be demonstrated rather than merely asserted.
    """
    if not os.path.isfile(str(stored_path)):
        return None
    return sha256_bytes(read_stored(stored_path)) == str(expected_sha256)
