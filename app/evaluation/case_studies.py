"""Historical case studies: publicly documented failure patterns, reconstructed safely.

What a case study is here
-------------------------
A *case study* is a declarative JSON manifest under ``data/case_studies/`` describing a
publicly documented IT control failure pattern, the controls it exercises, the evidence
files that must be generated to make it testable, and the conclusion the case author
expects an auditor to reach from that evidence, with the reasoning. :func:`import_case_study`
turns a manifest into a real audit engagement: a project, its scoped controls, and the
generated files pushed through the same ingestion path as any upload.

Why the organisation is fictional and the failure is not
-------------------------------------------------------
The tempting version of this feature is to name a company whose breach was widely
reported and reconstruct "its" evidence. That would be indefensible, for two reasons
that have nothing to do with legal caution:

1. **The evidence is not public.** What is public about any real incident is a narrative:
   press coverage, an advisory, sometimes a regulatory finding. The account listings, the
   alert queues and the vendor registers an auditor would actually test have never been
   published. Anything written under a real company's name would therefore be invented
   detail attributed to a real organisation - which is fabrication, however well
   intentioned, and exactly the failure mode this whole project exists to detect.
2. **A file that reads as real evidence will eventually be quoted as real evidence.** The
   reconstruction is useful precisely because it is realistic. Realism plus a real name is
   how a synthetic account listing ends up in somebody's slide deck as fact.

So the organisation is invented and says so on every artefact, while the *failure pattern*
- the thing the case study actually teaches - is real, public, and documented in the
sources each manifest names. The pattern shipped as CASE-001 (a policy requiring
multi-factor authentication, enforcement that misses service, break-glass, legacy and
vendor accounts, a dormant unenrolled account, and alerts that fire but are never
reviewed) is described in national cyber-security advisories and annual industry breach
studies; it is not the property of any one incident. Naming no company costs the case
study nothing and removes every claim this project cannot source.

Sourcing discipline
-------------------
Each manifest carries a ``sourcing_policy`` and a list of ``public_sources``. A URL appears
only where the resource was retrieved and its title confirmed when the manifest was
written, and only for durable publications of standards bodies or established industry
reports. Sources for incident reporting are described generically with no URL rather than
guessed at. ``url_confirmed`` records which is which, on every source, so a reader never
has to infer it.

Provenance, and why it is a column
----------------------------------
Every file this module ingests is written with
``provenance = EvidenceProvenance.HISTORICAL_PUBLIC`` and carries the disclosure sentence
from :data:`~app.schemas.enums.PROVENANCE_LABELS` in its description, alongside a header
inside the file itself. The database column is the load-bearing part: a disclaimer in a
docstring protects nobody, and filenames get renamed. See
:class:`~app.schemas.enums.EvidenceProvenance`.

Not an evaluation dataset
-------------------------
``expected_outcomes`` is the case author's reasoned judgement, recorded so that a reader
can disagree with it. It is *not* an answer key: this module is deliberately not wired
into :mod:`app.evaluation.runner`, and no accuracy, precision or recall figure this
project reports is computed over a case study. The evaluation suite in
:mod:`app.evaluation.datasets` is where ground truth lives, because there the correct
answer is true by construction rather than by argument.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import service as audit_service
from app.config import BASE_DIR
from app.database.models import AuditProject, EvidenceFile
from app.evidence.service import ingest_file
from app.schemas.enums import (
    ActivityAction,
    AssessmentStatus,
    EvidenceProvenance,
    EvidenceType,
    ProjectStatus,
    RiskLevel,
    provenance_label,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CASE_STUDY_DIR",
    "CASE_STUDY_DISCLAIMER",
    "CASE_STUDY_PROVENANCE",
    "CASE_STUDY_SCOPE_TAG",
    "CaseStudy",
    "CaseStudyError",
    "CaseStudyFile",
    "CaseStudyImportError",
    "CaseStudyManifestError",
    "ExpectedOutcome",
    "GeneratedFile",
    "PublicSource",
    "UnknownCaseStudyError",
    "build_case_study_files",
    "case_study_ids",
    "case_study_project_case_id",
    "case_study_summaries",
    "file_description",
    "get_case_study",
    "import_case_study",
    "is_case_study_project",
    "list_case_studies",
    "project_description",
    "reload_case_studies",
    "set_evidence_provenance",
]

#: Where the manifests live. Source, not runtime data: these files are committed, which is
#: why they sit beside the control library rather than under a generated-output directory.
CASE_STUDY_DIR = Path(BASE_DIR) / "data" / "case_studies"

#: Every artefact a case study creates carries this provenance, without exception.
CASE_STUDY_PROVENANCE = EvidenceProvenance.HISTORICAL_PUBLIC

#: Written into the project description and into every evidence file's description. It is
#: the same sentence the enum publishes, so the wording an auditor reads on the Evidence
#: page, in the report appendix and in the database is literally one string.
CASE_STUDY_DISCLAIMER = provenance_label(CASE_STUDY_PROVENANCE)

#: Machine-readable marker written into ``AuditProject.scope_note``, mirroring the
#: convention ``app.evaluation.runner`` uses for its throwaway projects. It is what
#: :func:`is_case_study_project` tests, so a consumer never has to pattern-match a name.
CASE_STUDY_SCOPE_TAG = "CASE-STUDY"

#: Column prepended to every generated tabular file. A CSV cannot carry a comment header:
#: the parser reads the first non-blank line as the column row, so a ``#`` preamble would
#: corrupt the export rather than annotate it. Carrying the notice as the first *field of
#: every record* is strictly stronger anyway - it survives into every TABLE_ROWS chunk a
#: model is shown and every row an auditor reads, not just the top of the file. The name
#: matches the convention in ``app.evaluation.datasets`` deliberately.
NOTICE_COLUMN = "Data_Origin"
NOTICE_VALUE = "SYNTHETIC-HISTORICAL-RECONSTRUCTION"

#: Recognised values of a manifest file's ``format`` field.
FORMAT_TEXT = "text"
FORMAT_TABLE = "table"

_TEXT_EXTENSIONS = (".md", ".txt")
_TABLE_EXTENSIONS = (".csv",)

#: Column widths from ``app.database.models``; SQLite ignores them, PostgreSQL will not.
_MAX_PROJECT_NAME = 255
_MAX_AUDIT_AREA = 255


class CaseStudyError(RuntimeError):
    """Base class so a caller can catch every failure this module raises."""


class UnknownCaseStudyError(CaseStudyError):
    """No manifest with the requested ``case_id`` exists."""


class CaseStudyManifestError(CaseStudyError):
    """A manifest is present but malformed, so nothing was loaded from it."""


class CaseStudyImportError(CaseStudyError):
    """The import could not proceed (a clashing project, a missing control)."""


# --------------------------------------------------------------------------- declarations
@dataclass
class PublicSource:
    """One public reference for the failure pattern a case study reconstructs.

    ``url_confirmed`` is recorded rather than implied. A source described generically with
    an empty ``url`` is a deliberate choice - it is how this project refuses to invent a
    citation - and a reader must be able to tell that apart from an oversight.
    """

    title: str
    publisher: str = ""
    url: str = ""
    url_confirmed: bool = False
    relevance: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "publisher": self.publisher,
            "url": self.url,
            "url_confirmed": self.url_confirmed,
            "relevance": self.relevance,
        }

    @property
    def citation(self) -> str:
        """One-line rendering for a report or a UI list."""
        parts = [self.title]
        if self.publisher:
            parts.append(self.publisher)
        if self.url:
            parts.append(self.url)
        else:
            parts.append("described generically; no URL asserted")
        return " - ".join(parts)


@dataclass
class CaseStudyFile:
    """One reconstructed evidence artefact, declared rather than coded.

    The content lives in the manifest - ``body`` lines for a document, ``columns`` and
    ``rows`` for an export - so that what will be written is readable without running
    anything, and so that no random number generator sits between the declaration and the
    bytes. A case study whose evidence changes between runs would not be a case study.
    """

    filename: str
    evidence_type: str
    role: str = ""
    description: str = ""
    fmt: str = FORMAT_TEXT
    body: List[str] = field(default_factory=list)
    columns: List[str] = field(default_factory=list)
    rows: List[List[str]] = field(default_factory=list)

    @property
    def extension(self) -> str:
        return Path(self.filename).suffix.lower()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "filename": self.filename,
            "extension": self.extension,
            "evidence_type": self.evidence_type,
            "role": self.role,
            "description": self.description,
            "format": self.fmt,
            "row_count": len(self.rows),
            "column_count": len(self.columns),
        }


@dataclass
class ExpectedOutcome:
    """What the case author argues an auditor should conclude, and why.

    Documentation of intent, not ground truth. Nothing in this project scores a prediction
    against it; see the module docstring.
    """

    control_ref: str
    expected_status: AssessmentStatus
    expected_risk: RiskLevel
    justification: str = ""
    evidence_pointers: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "control_ref": self.control_ref,
            "expected_status": self.expected_status.value,
            "expected_risk": self.expected_risk.value,
            "justification": self.justification,
            "evidence_pointers": list(self.evidence_pointers),
        }


@dataclass
class CaseStudy:
    """One loaded manifest."""

    case_id: str
    title: str
    organisation_label: str
    audit_area: str
    incident: Dict[str, Any]
    public_sources: List[PublicSource]
    control_refs: List[str]
    files: List[CaseStudyFile]
    expected_outcomes: List[ExpectedOutcome]
    limitations: List[str] = field(default_factory=list)
    sourcing_policy: str = ""
    period_start: str = ""
    period_end: str = ""
    schema_version: str = "1.0"
    provenance: EvidenceProvenance = CASE_STUDY_PROVENANCE
    manifest_path: str = ""

    @property
    def default_project_name(self) -> str:
        """Name used when the caller does not supply one.

        The case id leads so that two case studies can never collide, and so that the
        engagement is identifiable as a reconstruction from the project picker alone.
        """
        return "{0} - {1}".format(self.case_id, self.title)[:_MAX_PROJECT_NAME]

    @property
    def file_names(self) -> List[str]:
        return [item.filename for item in self.files]

    def outcome_for(self, control_ref: str) -> Optional[ExpectedOutcome]:
        for outcome in self.expected_outcomes:
            if outcome.control_ref == control_ref:
                return outcome
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "case_id": self.case_id,
            "title": self.title,
            "organisation_label": self.organisation_label,
            "audit_area": self.audit_area,
            "provenance": self.provenance.value,
            "provenance_label": provenance_label(self.provenance),
            "period_start": self.period_start,
            "period_end": self.period_end,
            "incident": dict(self.incident),
            "sourcing_policy": self.sourcing_policy,
            "public_sources": [source.to_dict() for source in self.public_sources],
            "control_refs": list(self.control_refs),
            "files": [item.to_dict() for item in self.files],
            "expected_outcomes": [outcome.to_dict() for outcome in self.expected_outcomes],
            "limitations": list(self.limitations),
            "manifest_path": self.manifest_path,
        }


@dataclass
class GeneratedFile:
    """The bytes for one reconstructed artefact, ready to ingest."""

    filename: str
    content: bytes
    evidence_type: str
    description: str
    role: str = ""

    @property
    def size_bytes(self) -> int:
        return len(self.content)


# --------------------------------------------------------------------------- loading
#: Parsed manifests keyed by ``case_id``. Manifests are committed source that cannot
#: change under a running process, so one load per process is correct; a developer editing
#: one calls :func:`reload_case_studies`.
_CACHE: Dict[str, CaseStudy] = {}
_CACHE_DIR: Optional[str] = None


def reload_case_studies() -> int:
    """Drop the cache and return how many manifests reload from the default directory."""
    global _CACHE_DIR
    _CACHE.clear()
    _CACHE_DIR = None
    return len(list_case_studies())


def list_case_studies(directory: Optional[Union[str, Path]] = None) -> List[CaseStudy]:
    """Every manifest in ``directory`` (default :data:`CASE_STUDY_DIR`), by case id.

    A manifest that will not parse is reported as a warning and skipped rather than
    raising: one broken file must not make the feature unavailable, and the Streamlit
    console has no good way to recover from an exception raised while listing.
    """
    global _CACHE_DIR
    root = Path(directory) if directory is not None else CASE_STUDY_DIR
    key = str(root.resolve()) if root.exists() else str(root)
    if _CACHE and _CACHE_DIR == key:
        return sorted(_CACHE.values(), key=lambda case: case.case_id)

    loaded: Dict[str, CaseStudy] = {}
    if root.is_dir():
        for path in sorted(root.glob("*.json")):
            try:
                case = _load_manifest(path)
            except CaseStudyManifestError as exc:
                logger.warning("Skipping case study manifest %s: %s", path.name, exc)
                continue
            if case.case_id in loaded:
                logger.warning(
                    "Case study id %s is declared twice (%s and %s); the first was kept.",
                    case.case_id,
                    loaded[case.case_id].manifest_path,
                    path,
                )
                continue
            loaded[case.case_id] = case
    else:
        logger.warning("Case study directory %s does not exist; no case studies are available.", root)

    _CACHE.clear()
    _CACHE.update(loaded)
    _CACHE_DIR = key
    return sorted(loaded.values(), key=lambda case: case.case_id)


def case_study_ids(directory: Optional[Union[str, Path]] = None) -> List[str]:
    return [case.case_id for case in list_case_studies(directory)]


def get_case_study(case_id: str, directory: Optional[Union[str, Path]] = None) -> CaseStudy:
    """Look one case study up by id. Raises :class:`UnknownCaseStudyError` if absent."""
    wanted = str(case_id or "").strip().upper()
    for case in list_case_studies(directory):
        if case.case_id.upper() == wanted:
            return case
    available = ", ".join(case_study_ids(directory)) or "(none)"
    raise UnknownCaseStudyError(
        "No case study {0!r}. Available: {1}.".format(case_id, available)
    )


def _load_manifest(path: Path) -> CaseStudy:
    """Parse and validate one manifest. Every failure names the file and the field."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CaseStudyManifestError("{0}: {1}".format(path.name, exc)) from exc
    if not isinstance(raw, dict):
        raise CaseStudyManifestError("{0}: the manifest must be a JSON object.".format(path.name))

    case_id = _require_text(raw, "case_id", path)
    # A manifest claiming SYNTHETIC or ORGANISATIONAL provenance is a category error: the
    # whole point of this mechanism is material reconstructed from public history. A
    # *misspelled* provenance must fail the same way rather than fall through to the
    # default, so the declared value is coerced without one.
    declared_provenance = raw.get("provenance", CASE_STUDY_PROVENANCE.value)
    provenance = EvidenceProvenance.coerce(declared_provenance, None)
    if provenance is not CASE_STUDY_PROVENANCE:
        raise CaseStudyManifestError(
            "{0}: provenance must be {1}, not {2!r}.".format(
                path.name, CASE_STUDY_PROVENANCE.value, declared_provenance
            )
        )

    control_refs = [str(ref).strip() for ref in (raw.get("control_refs") or []) if str(ref).strip()]
    if not control_refs:
        raise CaseStudyManifestError("{0}: control_refs must name at least one control.".format(path.name))

    files = [_load_file(entry, path) for entry in (raw.get("files") or [])]
    if not files:
        raise CaseStudyManifestError("{0}: files must declare at least one artefact.".format(path.name))
    seen: Dict[str, int] = {}
    for declared in files:
        seen[declared.filename] = seen.get(declared.filename, 0) + 1
    duplicates = sorted(name for name, count in seen.items() if count > 1)
    if duplicates:
        raise CaseStudyManifestError(
            "{0}: duplicate filename(s) {1}.".format(path.name, ", ".join(duplicates))
        )

    return CaseStudy(
        case_id=case_id,
        title=_require_text(raw, "title", path),
        organisation_label=str(raw.get("organisation_label", "") or ""),
        audit_area=str(raw.get("audit_area", "") or "IT general controls"),
        incident=dict(raw.get("incident") or {}),
        public_sources=[_load_source(entry) for entry in (raw.get("public_sources") or [])],
        control_refs=control_refs,
        files=files,
        expected_outcomes=[_load_outcome(entry, path) for entry in (raw.get("expected_outcomes") or [])],
        limitations=[str(item) for item in (raw.get("limitations") or [])],
        sourcing_policy=str(raw.get("sourcing_policy", "") or ""),
        period_start=str(raw.get("period_start", "") or ""),
        period_end=str(raw.get("period_end", "") or ""),
        schema_version=str(raw.get("schema_version", "1.0") or "1.0"),
        provenance=provenance,
        manifest_path=str(path),
    )


def _require_text(raw: Dict[str, Any], key: str, path: Path) -> str:
    value = str(raw.get(key, "") or "").strip()
    if not value:
        raise CaseStudyManifestError("{0}: '{1}' is required.".format(path.name, key))
    return value


def _load_source(entry: Any) -> PublicSource:
    data = dict(entry or {})
    return PublicSource(
        title=str(data.get("title", "") or ""),
        publisher=str(data.get("publisher", "") or ""),
        url=str(data.get("url", "") or ""),
        url_confirmed=bool(data.get("url_confirmed", False)),
        relevance=str(data.get("relevance", "") or ""),
    )


def _load_file(entry: Any, path: Path) -> CaseStudyFile:
    data = dict(entry or {})
    filename = str(data.get("filename", "") or "").strip()
    if not filename:
        raise CaseStudyManifestError("{0}: every file entry needs a filename.".format(path.name))

    fmt = str(data.get("format", FORMAT_TEXT) or FORMAT_TEXT).strip().lower()
    if fmt not in (FORMAT_TEXT, FORMAT_TABLE):
        raise CaseStudyManifestError(
            "{0}: {1} declares unknown format {2!r}; use '{3}' or '{4}'.".format(
                path.name, filename, fmt, FORMAT_TEXT, FORMAT_TABLE
            )
        )

    extension = Path(filename).suffix.lower()
    expected = _TEXT_EXTENSIONS if fmt == FORMAT_TEXT else _TABLE_EXTENSIONS
    if extension not in expected:
        raise CaseStudyManifestError(
            "{0}: {1} is declared as '{2}' but has extension '{3}'; expected one of {4}.".format(
                path.name, filename, fmt, extension or "(none)", ", ".join(expected)
            )
        )

    # An unrecognised evidence type must fail loudly. Retrieval and the prompt both use it
    # to tell a document that states a requirement from an export that records what
    # happened, so quietly defaulting it to OTHER would change what the case study tests.
    declared_type = str(data.get("evidence_type", "") or "")
    evidence_type = EvidenceType.coerce(declared_type, None)
    if evidence_type is None:
        raise CaseStudyManifestError(
            "{0}: {1} declares unknown evidence_type {2!r}. Known: {3}.".format(
                path.name, filename, declared_type, ", ".join(EvidenceType.values())
            )
        )

    columns = [str(column) for column in (data.get("columns") or [])]
    rows = [[_cell(value) for value in row] for row in (data.get("rows") or [])]
    body = [str(line) for line in (data.get("body") or [])]

    if fmt == FORMAT_TABLE:
        if not columns or not rows:
            raise CaseStudyManifestError(
                "{0}: {1} is a table and needs both columns and rows.".format(path.name, filename)
            )
        # Checked before the row widths, because a manifest that declared the reserved
        # column would otherwise be reported as merely ragged and the author would fix the
        # wrong thing.
        if NOTICE_COLUMN in columns:
            raise CaseStudyManifestError(
                "{0}: {1} declares the reserved column '{2}'; it is added automatically.".format(
                    path.name, filename, NOTICE_COLUMN
                )
            )
        ragged = [index + 1 for index, row in enumerate(rows) if len(row) != len(columns)]
        if ragged:
            raise CaseStudyManifestError(
                "{0}: {1} has {2} column(s) but row(s) {3} do not match.".format(
                    path.name, filename, len(columns), ", ".join(str(i) for i in ragged[:10])
                )
            )
    elif not body:
        raise CaseStudyManifestError("{0}: {1} is a text file and needs a body.".format(path.name, filename))

    return CaseStudyFile(
        filename=filename,
        evidence_type=evidence_type.value,
        role=str(data.get("role", "") or ""),
        description=str(data.get("description", "") or ""),
        fmt=fmt,
        body=body,
        columns=columns,
        rows=rows,
    )


def _load_outcome(entry: Any, path: Path) -> ExpectedOutcome:
    data = dict(entry or {})
    control_ref = str(data.get("control_ref", "") or "").strip()
    if not control_ref:
        raise CaseStudyManifestError("{0}: every expected outcome needs a control_ref.".format(path.name))
    status = AssessmentStatus.coerce(data.get("expected_status"), None)
    if status is None:
        raise CaseStudyManifestError(
            "{0}: {1} declares unknown expected_status {2!r}.".format(
                path.name, control_ref, data.get("expected_status")
            )
        )
    risk = RiskLevel.coerce(data.get("expected_risk"), RiskLevel.NOT_RATED)
    return ExpectedOutcome(
        control_ref=control_ref,
        expected_status=status,
        expected_risk=risk,
        justification=str(data.get("justification", "") or ""),
        evidence_pointers=[str(item) for item in (data.get("evidence_pointers") or [])],
    )


def _cell(value: Any) -> str:
    """Render a manifest cell as the literal text the export will contain.

    Everything becomes a string on purpose: an audit export is text, and letting JSON
    numbers through would make ``2024`` and ``"2024"`` produce different files.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# --------------------------------------------------------------------------- generation
def build_case_study_files(case: CaseStudy) -> List[GeneratedFile]:
    """Render every declared artefact to bytes, in declaration order.

    Deterministic by construction: the content comes from the manifest and nothing is
    sampled, so two imports of the same case study produce byte-identical files and the
    same SHA-256 on every row.
    """
    return [_render_file(case, declared) for declared in case.files]


def _render_file(case: CaseStudy, declared: CaseStudyFile) -> GeneratedFile:
    if declared.fmt == FORMAT_TABLE:
        content = _render_table(case, declared)
    else:
        content = _render_text(case, declared)
    return GeneratedFile(
        filename=declared.filename,
        content=content,
        evidence_type=declared.evidence_type,
        description=file_description(case, declared),
        role=declared.role,
    )


def _header_lines(case: CaseStudy) -> List[str]:
    """The notice every generated document opens with.

    Built from the case rather than hard-coded so a second case study cannot ship with
    the first one's organisation name in its header.
    """
    return [
        "SYNTHETIC HISTORICAL RECONSTRUCTION - NOT REAL ORGANISATIONAL EVIDENCE",
        "",
        "Case study {0} of the LLM-Assisted IT Audit research prototype. This file was "
        "generated from data/case_studies/; it is not the evidence of any real "
        "organisation and was never obtained from one.".format(case.case_id),
        "{0} is a fictional organisation. The account names, vendor names, dates, "
        "references and figures below are invented.".format(
            case.organisation_label or "The organisation named below"
        ),
        "What is not invented is the failure pattern this file illustrates, which is "
        "documented in public reporting cited in the case study manifest.",
        "Nothing here may be quoted as a finding about any real organisation.",
    ]


def _render_text(case: CaseStudy, declared: CaseStudyFile) -> bytes:
    """Prepend the notice to a declared document body.

    In Markdown the notice is a blockquote so it renders as a callout rather than as body
    text; in a plain ``.txt`` it is written as-is. Either way it is the first thing in the
    file and the first thing in the first parsed chunk.
    """
    header = _header_lines(case)
    if declared.extension == ".md":
        header = ["> " + line if line else ">" for line in header]
    lines = header + [""] + list(declared.body)
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def _render_table(case: CaseStudy, declared: CaseStudyFile) -> bytes:
    """Write a CSV whose every record leads with the reconstruction notice.

    See :data:`NOTICE_COLUMN` for why the notice is a column rather than a comment header.
    ``lineterminator`` is set explicitly because :mod:`csv` defaults to CRLF, and the
    stored bytes are hashed - a platform-dependent line ending would make the SHA-256 of
    the same case study differ between machines.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([NOTICE_COLUMN] + list(declared.columns))
    for row in declared.rows:
        writer.writerow([NOTICE_VALUE] + list(row))
    return buffer.getvalue().encode("utf-8")


def file_description(case: CaseStudy, declared: CaseStudyFile) -> str:
    """The description stored on the evidence row: disclosure first, then the artefact.

    Disclosure leads because the description is truncated in table views, and a warning
    that only survives when the column is wide is not a warning.
    """
    parts = [CASE_STUDY_DISCLAIMER, "Case study {0}".format(case.case_id)]
    if case.organisation_label:
        parts.append("{0} does not exist".format(case.organisation_label))
    if declared.description:
        parts.append(declared.description)
    return " | ".join(parts)


def project_description(case: CaseStudy) -> str:
    """The engagement description: disclosure, the pattern, the sources, the limits."""
    lines: List[str] = [CASE_STUDY_DISCLAIMER, ""]
    lines.append("Case study {0}: {1}".format(case.case_id, case.title))
    if case.organisation_label:
        lines.append(
            "The organisation, accounts, vendors and dates in this engagement are "
            "invented. {0} does not exist.".format(case.organisation_label)
        )
    summary = str(case.incident.get("summary", "") or "")
    if summary:
        lines.extend(["", "DOCUMENTED FAILURE PATTERN", summary])
    if case.public_sources:
        lines.extend(["", "PUBLIC SOURCES FOR THE PATTERN"])
        lines.extend("- " + source.citation for source in case.public_sources)
    if case.sourcing_policy:
        lines.extend(["", "SOURCING POLICY", case.sourcing_policy])
    if case.limitations:
        lines.extend(["", "LIMITATIONS"])
        lines.extend("- " + item for item in case.limitations)
    return "\n".join(lines)


# --------------------------------------------------------------------------- import
def import_case_study(
    session: Session,
    case_id: str,
    project_name: Optional[str] = None,
    auditor_name: str = "",
    directory: Optional[Union[str, Path]] = None,
) -> AuditProject:
    """Create an engagement from a case study: project, scoped controls, ingested evidence.

    The files go through :func:`app.evidence.service.ingest_file` - the same path an
    upload takes - rather than being written straight to the database, so a case study
    exercises parsing, chunking, locator construction and indexing exactly as real
    evidence does. A case study that bypassed ingestion would demonstrate nothing about
    the pipeline it is meant to exercise.

    Every ingested row is left with ``provenance = HISTORICAL_PUBLIC`` and the disclosure
    sentence in its description, and the same sentence opens the project description.

    Raises :class:`CaseStudyImportError` when a project of the target name already exists.
    Silently creating a second copy would leave two indistinguishable engagements in the
    picker and double every dashboard figure drawn over all projects; a caller who wants a
    second copy passes ``project_name``.
    """
    case = get_case_study(case_id, directory)
    name = str(project_name or case.default_project_name).strip()[:_MAX_PROJECT_NAME]
    if not name:
        raise CaseStudyImportError("project_name cannot be blank.")

    clash = session.execute(select(AuditProject).where(AuditProject.name == name)).scalars().first()
    if clash is not None:
        raise CaseStudyImportError(
            "An audit project named {0!r} already exists (id {1}). Pass project_name to "
            "import a second copy, or delete the existing project first.".format(name, clash.id)
        )

    missing = [ref for ref in case.control_refs if audit_service.get_control(session, ref) is None]
    if missing:
        raise CaseStudyImportError(
            "Case study {0} needs control(s) {1}, which are not in the control library. "
            "Seed the library first.".format(case.case_id, ", ".join(missing))
        )

    actor = (auditor_name or "Case study importer").strip()
    project = audit_service.create_project(
        session,
        name=name,
        audit_area=str(case.audit_area)[:_MAX_AUDIT_AREA],
        description=project_description(case),
        period_start=case.period_start or None,
        period_end=case.period_end or None,
        auditor_name=actor,
        status=ProjectStatus.FIELDWORK,
        scope_note="{tag} | case_id={case} | provenance={prov}".format(
            tag=CASE_STUDY_SCOPE_TAG, case=case.case_id, prov=case.provenance.value
        ),
        # Not a real engagement, and the report renderer keys its synthetic-data banner
        # off this flag as well as off the evidence rows.
        is_demo=True,
        control_refs=case.control_refs,
        actor=actor,
    )

    ingested: List[Dict[str, Any]] = []
    for generated in build_case_study_files(case):
        record = ingest_file(
            session,
            project.id,
            generated.content,
            generated.filename,
            evidence_type=generated.evidence_type,
            description=generated.description,
            uploaded_by=actor,
            is_synthetic=True,
        )
        set_evidence_provenance(session, record.id, case.provenance)
        ingested.append(
            {
                "filename": record.filename,
                "evidence_type": record.evidence_type,
                "provenance": record.provenance,
                "role": generated.role,
                "sha256": record.sha256,
                "parse_status": record.parse_status,
                "chunks": record.chunk_count,
                "rows": record.row_count,
                "parse_error": record.parse_error or "",
            }
        )

    audit_service.log_activity(
        session,
        entity_type="case_study",
        entity_id=project.id,
        action=ActivityAction.CASE_STUDY_IMPORTED,
        actor=actor,
        details={
            "case_id": case.case_id,
            "title": case.title,
            "provenance": case.provenance.value,
            "organisation_label": case.organisation_label,
            "control_refs": list(case.control_refs),
            "files": ingested,
            "disclaimer": CASE_STUDY_DISCLAIMER,
        },
        project_id=project.id,
    )

    failed = [item["filename"] for item in ingested if not item["chunks"]]
    if failed:
        # Not fatal: the engagement and its other evidence are real rows and are better
        # kept than rolled back. It is logged loudly because a file with no chunk is
        # invisible to retrieval, so the case study would silently under-test its control.
        logger.warning(
            "Case study %s: %s produced no chunks and cannot be retrieved or cited.",
            case.case_id,
            ", ".join(failed),
        )

    session.refresh(project)
    return project


def set_evidence_provenance(
    session: Session,
    evidence_file_id: int,
    provenance: Union[str, EvidenceProvenance],
) -> EvidenceFile:
    """Record where an already-ingested evidence file came from.

    This exists as a follow-up write because
    :func:`app.evidence.service.ingest_file` does not yet take a ``provenance`` argument -
    that module is owned elsewhere. The gap is narrow but real: between the ingest commit
    and this one, the row carries the column default ``SYNTHETIC``. That default is the
    safe direction to be wrong in (under-trusted rather than over-trusted), which is why
    the stopgap is acceptable in the meantime, but the right fix is one optional parameter
    on ``ingest_file``. When that lands, this function stays useful only for correcting a
    provenance an auditor mis-declared.
    """
    record = session.get(EvidenceFile, int(evidence_file_id))
    if record is None:
        raise CaseStudyImportError("Evidence file {0} does not exist.".format(evidence_file_id))
    member = EvidenceProvenance.coerce(provenance, None)
    if member is None:
        raise CaseStudyImportError(
            "Unknown provenance {0!r}. Known: {1}.".format(provenance, ", ".join(EvidenceProvenance.values()))
        )
    record.provenance = member.value
    session.commit()
    return record


def is_case_study_project(project: Any) -> bool:
    """True when a project was created by :func:`import_case_study`.

    Tests the marker in ``scope_note`` rather than the name, so renaming an engagement
    cannot make its evidence look organisational.
    """
    return CASE_STUDY_SCOPE_TAG in str(getattr(project, "scope_note", "") or "")


def case_study_project_case_id(project: Any) -> str:
    """The ``case_id`` recorded in a project's scope note, or "" if it is not a case study."""
    note = str(getattr(project, "scope_note", "") or "")
    for part in note.split("|"):
        token = part.strip()
        if token.startswith("case_id="):
            return token[len("case_id=") :].strip()
    return ""


def case_study_summaries(directory: Optional[Union[str, Path]] = None) -> List[Dict[str, Any]]:
    """Serialisable listing for the API and the console picker."""
    return [case.to_dict() for case in list_case_studies(directory)]
