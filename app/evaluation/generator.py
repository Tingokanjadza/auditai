"""Write the synthetic datasets to disk as real files, byte-identically, every time.

The evaluation harness ingests these files through exactly the same path as an auditor's
upload - parser, chunker, embedder, retriever - so they have to be genuine .csv, .xlsx,
.docx, .pdf, .txt and .json artefacts rather than strings handed to the assessment engine.
Anything less would evaluate the engine against evidence that never went through the
machinery under test.

Determinism is a research requirement, not tidiness
---------------------------------------------------
A number quoted in the write-up has to be reproducible from the code, which means the
same bytes must come out of every run. Three of these formats fight that by default:

* ``.docx`` and ``.xlsx`` are ZIP archives. ``zipfile`` stamps each member with the
  current clock, and openpyxl overwrites ``dcterms:modified`` at save time. Both are
  rewritten after saving (:func:`_normalise_zip`) with a fixed member timestamp and a
  fixed modification date.
* ``.pdf`` embeds a creation date and a document id; reportlab's ``invariant`` mode
  pins both.

Row content is drawn from ``random.Random(dataset.seed)``, never the global ``random``
module, so generating one dataset cannot shift another's data.

What is fabricated, and how obviously
-------------------------------------
Account handles follow the ``p.adeyemi`` / ``svc-backup-014`` shape the build
specification asks for: plausible enough that retrieval and chunking behave as they would
on a real export, but attached to nothing real. Every file states in its own text that it
is synthetic - text formats carry a notice line, tabular exports carry a ``Data_Origin``
column whose value repeats on every row, because a notice buried in file metadata is not
a disclosure to anyone reading a quoted chunk.

Verification lives here too
---------------------------
:func:`verify_dataset` re-reads what was written, parses it with the production parser
and checks that the declared ground truth is still true of the bytes: the counts, the
exception rows, every ``key_evidence_marker``, and - for DATASET-005 - that no forbidden
substring appears anywhere in the file. The generator refuses to leave a dataset on disk
that fails its own declaration, because a silently broken dataset would not fail an
evaluation run; it would quietly change what the run measured.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from random import Random
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from app.config import get_settings
from app.evaluation.datasets import (
    CHANGE_APPROVAL_REQUIREMENT,
    CHANGE_TICKET_REQUIREMENT,
    DATA_ORIGIN_COLUMN,
    DATA_ORIGIN_VALUE,
    IDP_ENFORCEMENT_MARKER,
    MFA_ENFORCEMENT_REQUIREMENT,
    MFA_ENROLMENT_REQUIREMENT,
    MFA_LISTING_REQUIREMENT,
    PASSWORD_LENGTH_OBSERVATION,
    PASSWORD_LENGTH_REQUIREMENT,
    PATCH_POPULATION_REQUIREMENT,
    PATCH_WINDOW_REQUIREMENT,
    SYNTHETIC_DATASETS,
    SYNTHETIC_NOTICE,
    SyntheticDataset,
    get_dataset,
)

__all__ = [
    "generate_dataset",
    "generate_all",
    "verify_dataset",
    "verify_all",
    "dataset_dir",
    "output_root",
    "DatasetGenerationError",
]


class DatasetGenerationError(RuntimeError):
    """A dataset could not be written in a state that matches its own declaration."""


#: Every timestamp written into a generated document. Fixed so the bytes are stable, and
#: chosen to sit at the end of the fictional audit period the evidence describes.
FIXED_TIMESTAMP = datetime(2024, 6, 30, 12, 0, 0)
_ZIP_TIMESTAMP = (2024, 6, 30, 12, 0, 0)
EXPORT_DATE = "2024-06-30"
_EXPORT_DAY = date(2024, 6, 30)

_GENERATOR_NAME = "LLM IT Auditor synthetic dataset generator"
_DOMAIN = "SYNTHCORP-RESEARCH"

# ---- synthetic identity pools
# Deliberately varied and deliberately fictitious. Handles are combined with an initial
# and de-duplicated, so a dataset of 100 accounts never repeats a name.

_SURNAMES = [
    "adeyemi", "okonkwo", "varga", "mensah", "batistuta", "oyelaran", "szabo", "nakamura",
    "delacroix", "haddad", "ferreira", "okafor", "lindqvist", "marchetti", "novikova",
    "abubakar", "tanaka", "brennan", "kowalski", "moreau", "petrova", "sanchez", "dlamini",
    "eriksen", "gallagher", "ibrahim", "jansen", "kovacs", "leclerc", "mbeki", "nguyen",
    "ortega", "pereira", "quintana", "rasmussen", "sorensen", "thorne", "ustinov",
    "vasquez", "whitfield", "yilmaz", "zubair",
]
_INITIALS = ["a", "d", "e", "f", "h", "j", "k", "l", "m", "n", "o", "p", "r", "s", "t", "v"]
_SERVICE_ROLES = [
    "backup", "sql", "scan", "etl", "report", "deploy", "monitor", "archive", "sync", "batch",
]

_DEPARTMENTS = [
    "Infrastructure",
    "Application Support",
    "Information Security",
    "Data Platform",
    "Service Desk",
    "Network Operations",
]
_ACCOUNT_TYPES = [
    "Domain Administrator",
    "Database Administrator",
    "Security Tool Administrator",
    "Application Administrator",
    "Service Account",
]
_OS_VERSIONS = [
    "Windows 11 23H2",
    "Windows 11 22H2",
    "Windows Server 2022",
    "Windows Server 2019",
    "Ubuntu 22.04 LTS",
]
_OWNER_TEAMS = [
    "Infrastructure",
    "Application Support",
    "Data Platform",
    "Network Operations",
    "End User Computing",
]
_CHANGE_SYSTEMS = [
    "Core Banking Application",
    "Identity Provider",
    "Data Warehouse",
    "Customer Portal",
    "Payment Gateway",
    "Network Firewall",
    "Reporting Service",
    "Document Management System",
]
_CHANGE_TYPES = ["Standard", "Normal", "Emergency"]


# ---- paths


def output_root(output_dir: Optional[Any] = None) -> Path:
    """Directory the suite is written under; ``settings.synthetic_dir`` by default."""
    root = Path(output_dir) if output_dir is not None else Path(get_settings().synthetic_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def dataset_dir(dataset_id: Any, output_dir: Optional[Any] = None) -> Path:
    """One directory per dataset, so an evaluation project ingests a clean set."""
    dataset = get_dataset(dataset_id)
    path = output_root(output_dir) / dataset.dataset_id.lower()
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---- low-level writers


def _write_text(path: Path, lines: Sequence[str]) -> Path:
    """Write UTF-8 text with LF endings regardless of platform."""
    body = "\n".join(lines).rstrip("\n") + "\n"
    path.write_bytes(body.encode("utf-8"))
    return path


def _write_json(path: Path, payload: Dict[str, Any]) -> Path:
    """Write JSON exactly as :mod:`app.evidence.parsers` will re-render it.

    The parser pretty-prints with ``indent=2, ensure_ascii=False`` and preserves key
    order, so writing the same way means a marker taken from this file is a literal
    substring of the chunk text as well as of the file.
    """
    body = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    path.write_bytes(body.encode("utf-8"))
    return path


def _write_csv(path: Path, columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> Path:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(list(columns))
    for row in rows:
        writer.writerow(["" if value is None else value for value in row])
    path.write_bytes(buffer.getvalue().encode("utf-8"))
    return path


_MODIFIED_RE = re.compile(rb"(<dcterms:modified[^>]*>)[^<]*(</dcterms:modified>)")


def _normalise_zip(path: Path) -> None:
    """Rewrite an OOXML package so two runs produce identical bytes.

    Member order and content are preserved; only the per-member timestamp and the
    package's ``dcterms:modified`` value change, both to fixed values. Without this,
    ``.docx`` and ``.xlsx`` outputs differ on every run purely because of the clock, and
    "regenerate and compare hashes" stops being a usable integrity check.
    """
    with zipfile.ZipFile(path, "r") as source:
        members: List[Tuple[zipfile.ZipInfo, bytes]] = [
            (info, source.read(info.filename)) for info in source.infolist()
        ]

    stamp = FIXED_TIMESTAMP.strftime("%Y-%m-%dT%H:%M:%SZ").encode("ascii")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for info, data in members:
            if info.filename.endswith("core.xml"):
                data = _MODIFIED_RE.sub(rb"\g<1>" + stamp + rb"\g<2>", data)
            entry = zipfile.ZipInfo(info.filename, date_time=_ZIP_TIMESTAMP)
            entry.compress_type = info.compress_type
            entry.external_attr = 0o600 << 16
            entry.create_system = 3
            target.writestr(entry, data)
    path.write_bytes(buffer.getvalue())


def _write_docx(path: Path, title: str, blocks: Sequence[Tuple[str, str]]) -> Path:
    """Write a Word document from ``(kind, text)`` blocks: ``heading`` or ``body``."""
    import docx

    document = docx.Document()
    document.add_heading(title, level=1)
    for kind, text in blocks:
        if kind == "heading":
            document.add_heading(text, level=2)
        else:
            document.add_paragraph(text)

    properties = document.core_properties
    properties.title = title
    properties.author = _GENERATOR_NAME
    properties.last_modified_by = _GENERATOR_NAME
    properties.comments = SYNTHETIC_NOTICE
    properties.category = "Synthetic research data"
    properties.created = FIXED_TIMESTAMP
    properties.modified = FIXED_TIMESTAMP
    properties.revision = 1
    document.save(str(path))
    _normalise_zip(path)
    return path


def _write_xlsx(
    path: Path,
    sheet_name: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    title: str = "",
) -> Path:
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = sheet_name
    sheet.append(list(columns))
    for row in rows:
        sheet.append(list(row))

    properties = workbook.properties
    properties.creator = _GENERATOR_NAME
    properties.lastModifiedBy = _GENERATOR_NAME
    properties.title = title or sheet_name
    properties.description = SYNTHETIC_NOTICE
    properties.category = "Synthetic research data"
    properties.created = FIXED_TIMESTAMP
    properties.modified = FIXED_TIMESTAMP
    workbook.save(str(path))
    _normalise_zip(path)
    return path


def _write_pdf(path: Path, title: str, blocks: Sequence[Tuple[str, str]]) -> Path:
    """Write a text-only PDF whose extracted text is stable and quotable.

    Long paragraphs are wrapped on word boundaries, which is what makes a declared marker
    sentence survive extraction: pypdf rejoins the wrapped lines of a paragraph with
    single spaces, so the sentence comes back as it was written.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfgen import canvas as rl_canvas

    width, height = A4
    left, right_margin, bottom = 56.0, 56.0, 64.0
    usable = width - left - right_margin

    pdf = rl_canvas.Canvas(str(path), pagesize=A4, invariant=1)
    pdf.setTitle(title)
    pdf.setAuthor(_GENERATOR_NAME)
    pdf.setSubject(SYNTHETIC_NOTICE)
    pdf.setCreator(_GENERATOR_NAME)

    cursor = height - 72.0

    def draw(text: str, font: str, size: int, gap: float) -> None:
        nonlocal cursor
        pdf.setFont(font, size)
        for line in simpleSplit(text, font, size, usable):
            if cursor < bottom:
                pdf.showPage()
                pdf.setFont(font, size)
                cursor = height - 72.0
            pdf.drawString(left, cursor, line)
            cursor -= size + 3.0
        cursor -= gap

    draw(title, "Helvetica-Bold", 15, 10.0)
    for kind, text in blocks:
        if kind == "heading":
            draw(text, "Helvetica-Bold", 11, 4.0)
        else:
            draw(text, "Helvetica", 10, 8.0)
    pdf.showPage()
    pdf.save()
    return path


# ---- deterministic synthetic values


def _handles(rng: Random, count: int, exclude: Sequence[str] = ()) -> List[str]:
    """``count`` unique ``initial.surname`` handles, deterministic for a given ``rng``."""
    taken = {str(name) for name in exclude}
    pool: List[str] = []
    for surname in _SURNAMES:
        for initial in _INITIALS:
            pool.append("{0}.{1}".format(initial, surname))
    rng.shuffle(pool)
    chosen: List[str] = []
    for handle in pool:
        if handle in taken:
            continue
        taken.add(handle)
        chosen.append(handle)
        if len(chosen) == count:
            return chosen
    raise DatasetGenerationError(
        "Only {0} unique handles available; {1} requested.".format(len(chosen), count)
    )


def _service_accounts(rng: Random, count: int, exclude: Sequence[str] = ()) -> List[str]:
    taken = {str(name) for name in exclude}
    pool = ["svc-{0}-{1:03d}".format(role, number) for role in _SERVICE_ROLES for number in range(1, 60)]
    rng.shuffle(pool)
    chosen: List[str] = []
    for name in pool:
        if name in taken:
            continue
        taken.add(name)
        chosen.append(name)
        if len(chosen) == count:
            return chosen
    raise DatasetGenerationError("Ran out of unique service account names.")


def _iso(day: date) -> str:
    return day.isoformat()


def _recent_dates(rng: Random, count: int, earliest_days: int, latest_days: int) -> List[str]:
    """Dates between ``latest_days`` and ``earliest_days`` before the export date."""
    return [
        _iso(_EXPORT_DAY - timedelta(days=rng.randint(latest_days, earliest_days)))
        for _ in range(count)
    ]


def _row_index(dataset: SyntheticDataset, spreadsheet_row: int) -> int:
    """Convert a 1-based spreadsheet row (header = row 1) into a 0-based data index."""
    index = spreadsheet_row - 2
    if index < 0:
        raise DatasetGenerationError(
            "{0}: row {1} is the header row, not a data row.".format(dataset.dataset_id, spreadsheet_row)
        )
    return index


# ---- shared documents


def _notice_blocks(reference: str, version: str, owner: str) -> List[Tuple[str, str]]:
    return [
        ("body", SYNTHETIC_NOTICE),
        (
            "body",
            "Document reference {0}. Version {1}. Owner: {2} (fabricated). Effective date "
            "1 February 2024.".format(reference, version, owner),
        ),
    ]


def _mfa_policy_blocks() -> List[Tuple[str, str]]:
    """The MFA policy used unchanged by DATASET-001, DATASET-005 and DATASET-006."""
    blocks = _notice_blocks("SYN-POL-014", "3.0", "Information Security")
    blocks.extend(
        [
            ("heading", "1. Purpose and scope"),
            (
                "body",
                "This policy applies to every system in scope of the annual IT general controls audit "
                "and to every account that holds privileged access to those systems.",
            ),
            (
                "body",
                "Privileged accounts include domain administrators, database administrators, security "
                "tool administrators, application administrators, and service accounts that are "
                "permitted interactive logon.",
            ),
            ("heading", "2. Enrolment requirement"),
            ("body", MFA_ENROLMENT_REQUIREMENT),
            ("body", MFA_ENFORCEMENT_REQUIREMENT),
            (
                "body",
                "A single knowledge factor is never sufficient to authenticate a privileged session, "
                "whether the session originates inside or outside the corporate network.",
            ),
            ("heading", "3. Exceptions"),
            (
                "body",
                "An account that cannot be enrolled must be recorded in the multi-factor authentication "
                "exception register with a named approver, a documented compensating control and an "
                "expiry date no more than 90 days after the date of approval.",
            ),
            (
                "body",
                "An exception that has expired is treated as a control failure until the account is "
                "enrolled or the exception is formally renewed.",
            ),
            ("heading", "4. Evidence of operation"),
            ("body", MFA_LISTING_REQUIREMENT),
            (
                "body",
                "The listing must state the date on which it was extracted from the identity provider, "
                "and that date must fall inside the audit period.",
            ),
            (
                "body",
                "Where enrolment is enforced by a platform policy, a configuration export of that policy "
                "must be supplied in addition to the account listing.",
            ),
        ]
    )
    return blocks


def _write_mfa_policy(path: Path) -> Path:
    return _write_docx(path, "Multi-Factor Authentication Policy", _mfa_policy_blocks())


def _privileged_account_rows(
    dataset: SyntheticDataset,
    rng: Random,
    exception_rows: Sequence[int],
) -> Tuple[List[str], List[List[Any]]]:
    """The privileged account population shared in shape by DATASET-001 and DATASET-006."""
    total = int(dataset.population["total"])
    compliant_value = str(dataset.population["compliant_value"])
    exception_value = dataset.population.get("exception_value")
    exception_keys = list(dataset.population.get("exception_keys", []))
    exception_indexes = {_row_index(dataset, row): position for position, row in enumerate(exception_rows)}

    service_count = max(1, total // 4)
    people = _handles(rng, total - service_count, exclude=exception_keys)
    services = _service_accounts(rng, service_count, exclude=exception_keys)
    names: List[str] = []
    service_flags: List[bool] = []
    for index in range(total):
        if index % 4 == 3 and services:
            names.append(services.pop())
            service_flags.append(True)
        else:
            names.append(people.pop())
            service_flags.append(False)

    for index, position in exception_indexes.items():
        names[index] = exception_keys[position]
        service_flags[index] = names[index].startswith("svc-")

    if len(set(names)) != total:
        raise DatasetGenerationError("{0}: duplicate account names generated.".format(dataset.dataset_id))

    methods = ["Authenticator App", "FIDO2 Security Key", "Hardware Token"]
    logins = _recent_dates(rng, total, earliest_days=88, latest_days=1)

    columns = [
        "Account_Name",
        "Account_Type",
        "Department",
        "Privilege_Level",
        "MFA_Status",
        "MFA_Method",
        "Last_Login",
        "Export_Date",
        DATA_ORIGIN_COLUMN,
    ]
    rows: List[List[Any]] = []
    for index in range(total):
        is_exception = index in exception_indexes
        account_type = "Service Account" if service_flags[index] else _ACCOUNT_TYPES[index % 4]
        rows.append(
            [
                names[index],
                account_type,
                _DEPARTMENTS[index % len(_DEPARTMENTS)],
                "Tier 0" if index % 3 == 0 else "Tier 1",
                str(exception_value) if is_exception else compliant_value,
                str(dataset.population.get("exception_method_value", "Not Registered"))
                if is_exception
                else methods[index % len(methods)],
                logins[index],
                EXPORT_DATE,
                DATA_ORIGIN_VALUE,
            ]
        )
    return columns, rows


# ---- dataset builders


def _build_001(dataset: SyntheticDataset, directory: Path) -> List[Path]:
    rng = Random(dataset.seed)
    policy = _write_mfa_policy(directory / dataset.files[0].filename)
    columns, rows = _privileged_account_rows(dataset, rng, dataset.exception_rows)
    export = _write_csv(directory / dataset.files[1].filename, columns, rows)
    return [policy, export]


def _build_002(dataset: SyntheticDataset, directory: Path) -> List[Path]:
    rng = Random(dataset.seed)
    population = dataset.population
    total = int(population["total"])
    window = int(population["remediation_window_days"])

    standard_blocks = _notice_blocks("SYN-STD-021", "2.0", "IT Operations")
    standard_blocks.extend(
        [
            ("heading", "1. Scope"),
            (
                "body",
                "This standard applies to every server and endpoint recorded in the asset inventory as "
                "in scope for the IT general controls audit.",
            ),
            ("heading", "2. Remediation windows"),
            ("body", PATCH_WINDOW_REQUIREMENT),
            (
                "body",
                "High severity patches must be deployed within 30 days of the vendor release date, and "
                "moderate severity patches within 90 days of the vendor release date.",
            ),
            ("heading", "3. Measurement"),
            ("body", PATCH_POPULATION_REQUIREMENT),
            (
                "body",
                "An endpoint that cannot be patched inside its remediation window requires a documented "
                "risk acceptance naming an owner and an expiry date.",
            ),
            ("heading", "4. Reporting"),
            (
                "body",
                "Patch compliance is reported monthly to the IT risk forum. An endpoint that has been "
                "reported outside its remediation window for two consecutive cycles is escalated to the "
                "system owner named in the asset inventory.",
            ),
        ]
    )
    standard = _write_pdf(directory / dataset.files[0].filename, "Patch Management Standard", standard_blocks)

    exception_indexes = {
        _row_index(dataset, row): position for position, row in enumerate(dataset.exception_rows)
    }
    missing_counts = list(population["exception_missing_patch_counts"])
    overdue_days = list(population["exception_days_since_patch"])

    columns = [
        "Hostname",
        "OS_Version",
        "Owner_Team",
        "Patch_Status",
        "Critical_Patches_Missing",
        "Last_Patch_Date",
        "Days_Since_Patch",
        DATA_ORIGIN_COLUMN,
    ]
    rows: List[List[Any]] = []
    for index in range(total):
        hostname = "EPT-{0:04d}".format(index + 1)
        if index in exception_indexes:
            position = exception_indexes[index]
            days = int(overdue_days[position])
            status = str(population["exception_value"])
            missing = int(missing_counts[position])
        else:
            # Compliant hosts are inside the window by construction, so "5 of 100 late"
            # is a property of the data rather than of the reader's arithmetic.
            days = rng.randint(1, window - 1)
            status = str(population["compliant_value"])
            missing = 0
        rows.append(
            [
                hostname,
                _OS_VERSIONS[index % len(_OS_VERSIONS)],
                _OWNER_TEAMS[index % len(_OWNER_TEAMS)],
                status,
                missing,
                _iso(_EXPORT_DAY - timedelta(days=days)),
                days,
                DATA_ORIGIN_VALUE,
            ]
        )

    declared = list(population["exception_keys"])
    actual = [rows[index][0] for index in sorted(exception_indexes)]
    if declared != actual:
        raise DatasetGenerationError(
            "{0}: exception hosts {1} do not sit at the declared rows {2}.".format(
                dataset.dataset_id, declared, dataset.exception_rows
            )
        )

    export = _write_xlsx(
        directory / dataset.files[1].filename,
        "Patch_Compliance",
        columns,
        rows,
        title="Endpoint patch compliance export",
    )
    return [standard, export]


def _build_003(dataset: SyntheticDataset, directory: Path) -> List[Path]:
    population = dataset.population
    required = int(population["required_value"])
    configured = int(population["configured_value"])

    policy_lines = [
        SYNTHETIC_NOTICE,
        "",
        "Password Policy",
        "Document reference SYN-POL-008. Version 4.0. Owner: Information Security (fabricated).",
        "",
        "1. Scope",
        "",
        "This policy applies to every user account and administrative account in the {0} domain and "
        "to every application that authenticates against it.".format(_DOMAIN),
        "",
        "2. Password parameters",
        "",
        PASSWORD_LENGTH_REQUIREMENT,
        "Password complexity must be enabled so that a password contains characters drawn from at "
        "least three of the four available character categories.",
        "Password history must be at least 24 previous passwords.",
        "The minimum password age must be at least 1 day.",
        "The maximum password age must not exceed 90 days.",
        "",
        "3. Account lockout",
        "",
        "The account lockout threshold must be no more than five invalid logon attempts.",
        "Lockout duration must be at least 15 minutes.",
        "",
        "4. Enforcement",
        "",
        "A parameter written in this policy but not enforced by the directory configuration does not "
        "satisfy this policy. Evidence of enforcement is a configuration export taken from the domain "
        "inside the audit period; the written policy on its own evidences intention only.",
    ]
    policy = _write_text(directory / dataset.files[0].filename, policy_lines)

    settings_rows: List[Tuple[str, Any, str]] = [
        ("Minimum_Password_Length", configured, "Integer"),
        ("Password_Complexity_Enabled", "Enabled", "Boolean"),
        ("Enforce_Password_History", 24, "Integer"),
        ("Maximum_Password_Age_Days", 90, "Integer"),
        ("Minimum_Password_Age_Days", 1, "Integer"),
        ("Store_Passwords_Using_Reversible_Encryption", "Disabled", "Boolean"),
        ("Account_Lockout_Threshold", 5, "Integer"),
        ("Account_Lockout_Duration_Minutes", 15, "Integer"),
        ("Reset_Lockout_Counter_After_Minutes", 15, "Integer"),
        ("LDAP_Signing_Required", "Enabled", "Boolean"),
        ("NTLM_Authentication_Level", 5, "Integer"),
        ("Kerberos_Ticket_Lifetime_Hours", 10, "Integer"),
        ("Smartcard_Removal_Behaviour", "Lock Workstation", "String"),
        ("Guest_Account_Status", "Disabled", "Boolean"),
        ("Anonymous_SID_Enumeration", "Disabled", "Boolean"),
        ("Audit_Credential_Validation", "Enabled", "Boolean"),
        ("Domain_Functional_Level", "2016", "String"),
        ("Fine_Grained_Password_Policy_Applied", "No", "Boolean"),
    ]
    if len(settings_rows) != int(population["parameter_count"]):
        raise DatasetGenerationError(
            "{0}: declared {1} parameters but wrote {2}.".format(
                dataset.dataset_id, population["parameter_count"], len(settings_rows)
            )
        )

    columns = [
        "Setting_Name",
        "Configured_Value",
        "Value_Type",
        "Applies_To",
        "Extracted_On",
        DATA_ORIGIN_COLUMN,
    ]
    rows = [
        [
            name,
            value,
            value_type,
            "Default Domain Policy ({0})".format(_DOMAIN),
            EXPORT_DATE,
            DATA_ORIGIN_VALUE,
        ]
        for name, value, value_type in settings_rows
    ]
    export = _write_csv(directory / dataset.files[1].filename, columns, rows)

    narrative_lines = [
        SYNTHETIC_NOTICE,
        "",
        "Screenshot narrative - domain password settings",
        "",
        "Prepared by the audit team on 30 June 2024.",
        "Source: Group Policy Management Console, Default Domain Policy, Computer Configuration, "
        "Policies, Windows Settings, Security Settings, Account Policies, Password Policy.",
        "Supplied by the domain administrator as a screen capture taken on 30 June 2024.",
        "",
        "The following values were visible in the screen capture:",
        "",
        PASSWORD_LENGTH_OBSERVATION,
        "Password complexity requirements: Enabled",
        "Enforce password history: 24 passwords remembered",
        "Maximum password age: 90 days",
        "Minimum password age: 1 day",
        "Account lockout threshold: 5 invalid logon attempts",
        "Account lockout duration: 15 minutes",
        "",
        "The screen capture was legible and the domain shown in its title bar matched the domain named "
        "in the configuration export. The audit team did not observe these settings directly on the "
        "system, so this narrative evidences what was shown to it and not the state of the directory.",
    ]
    narrative = _write_text(directory / dataset.files[2].filename, narrative_lines)

    if configured >= required:
        raise DatasetGenerationError(
            "{0}: configured length {1} does not fall below the required {2}.".format(
                dataset.dataset_id, configured, required
            )
        )
    return [policy, export, narrative]


def _build_004(dataset: SyntheticDataset, directory: Path) -> List[Path]:
    rng = Random(dataset.seed)
    population = dataset.population
    total = int(population["total"])

    policy_blocks = _notice_blocks("SYN-POL-022", "5.0", "IT Service Management")
    policy_blocks.extend(
        [
            ("heading", "1. Scope"),
            (
                "body",
                "This policy applies to every change to a production application, infrastructure "
                "component or configuration item in scope of the IT general controls audit.",
            ),
            ("heading", "2. Approval"),
            ("body", CHANGE_APPROVAL_REQUIREMENT),
            ("body", CHANGE_TICKET_REQUIREMENT),
            (
                "body",
                "The approver must not be the person who requested the change or the person who built "
                "it, and the approval must be recorded on or before the implementation date.",
            ),
            ("heading", "3. Change types"),
            (
                "body",
                "Standard changes follow a pre-approved template and are authorised at the point the "
                "template itself is approved. Normal changes require approval by the change advisory "
                "board or by a delegated approver named in the ticket.",
            ),
            (
                "body",
                "An emergency change may be implemented before approval, but a retrospective approval "
                "must be recorded within five business days of implementation.",
            ),
            ("heading", "4. Evidence of operation"),
            (
                "body",
                "The change ticket export supplied for audit must cover the full audit period and must "
                "record the requester, the approver and the approval status of every change.",
            ),
            (
                "body",
                "The ticket population must be reconciled to the production deployment log for the same "
                "period; a deployment with no corresponding ticket is an unauthorised change regardless "
                "of the approval rate observed in the ticket export.",
            ),
        ]
    )
    policy = _write_docx(directory / dataset.files[0].filename, "Change Management Policy", policy_blocks)

    requesters = _handles(rng, 24)
    approvers = _handles(rng, 20, exclude=requesters)
    exception_indexes = {
        _row_index(dataset, row): position for position, row in enumerate(dataset.exception_rows)
    }

    columns = [
        "Change_ID",
        "Change_Type",
        "System",
        "Requested_By",
        "Approver",
        "Approval_Status",
        "Implementation_Date",
        DATA_ORIGIN_COLUMN,
    ]
    rows: List[List[Any]] = []
    for index in range(total):
        change_id = "CHG-2024-{0:04d}".format(index + 1)
        requester = requesters[index % len(requesters)]
        is_exception = index in exception_indexes
        approver = "" if is_exception else approvers[(index + 7) % len(approvers)]
        if approver == requester:  # approver independence holds for every approved change
            approver = approvers[(index + 8) % len(approvers)]
        rows.append(
            [
                change_id,
                _CHANGE_TYPES[index % len(_CHANGE_TYPES)],
                _CHANGE_SYSTEMS[index % len(_CHANGE_SYSTEMS)],
                requester,
                approver,
                str(population["exception_value"]) if is_exception else str(population["compliant_value"]),
                _iso(date(2024, 4, 1) + timedelta(days=(index * 89) // max(1, total - 1))),
                DATA_ORIGIN_VALUE,
            ]
        )

    declared = list(population["exception_keys"])
    actual = [rows[index][0] for index in sorted(exception_indexes)]
    if declared != actual:
        raise DatasetGenerationError(
            "{0}: unapproved changes {1} do not sit at the declared rows {2}.".format(
                dataset.dataset_id, declared, dataset.exception_rows
            )
        )

    export = _write_xlsx(
        directory / dataset.files[1].filename,
        "Change_Tickets",
        columns,
        rows,
        title="Change ticket export 2024 Q2",
    )
    return [policy, export]


def _build_005(dataset: SyntheticDataset, directory: Path) -> List[Path]:
    rng = Random(dataset.seed)
    population = dataset.population
    total = int(population["total"])
    policy = _write_mfa_policy(directory / dataset.files[0].filename)

    sample_keys = list(population.get("sample_keys", []))
    service_count = max(1, total // 4)
    people = _handles(rng, total - service_count, exclude=sample_keys)
    services = _service_accounts(rng, service_count, exclude=sample_keys)
    names: List[str] = []
    for index in range(total):
        if index % 4 == 3 and services:
            names.append(services.pop())
        else:
            names.append(people.pop())
    for position, key in enumerate(sample_keys):
        # Placed at the first data rows so a marker proves the listing was retrieved.
        names[position] = key
    if len(set(names)) != total:
        raise DatasetGenerationError("{0}: duplicate user names generated.".format(dataset.dataset_id))

    logins = _recent_dates(rng, total, earliest_days=60, latest_days=1)
    columns = list(population["columns"]) + [DATA_ORIGIN_COLUMN]
    rows: List[List[Any]] = []
    for index in range(total):
        role = "Service Account" if names[index].startswith("svc-") else _ACCOUNT_TYPES[index % 4]
        rows.append(
            [
                names[index],
                role,
                _DEPARTMENTS[index % len(_DEPARTMENTS)],
                logins[index],
                "Active",
                DATA_ORIGIN_VALUE,
            ]
        )
    listing = _write_csv(directory / dataset.files[1].filename, columns, rows)

    # The whole point of this dataset is the absence of one attribute, so absence is
    # checked here rather than trusted: filename included, because the retriever indexes
    # it alongside the text.
    haystack = (listing.name + "\n" + listing.read_text(encoding="utf-8")).lower()
    for forbidden in population.get("forbidden_substrings", []):
        if forbidden in haystack:
            raise DatasetGenerationError(
                "{0}: '{1}' appears in {2}; the dataset would no longer test missing evidence.".format(
                    dataset.dataset_id, forbidden, listing.name
                )
            )
    return [policy, listing]


def _build_006(dataset: SyntheticDataset, directory: Path) -> List[Path]:
    rng = Random(dataset.seed)
    policy = _write_mfa_policy(directory / dataset.files[0].filename)
    columns, rows = _privileged_account_rows(dataset, rng, dataset.exception_rows)
    export = _write_csv(directory / dataset.files[1].filename, columns, rows)

    config = {
        "_notice": SYNTHETIC_NOTICE,
        "export_metadata": {
            "source_system": "Synthetic Identity Provider (fabricated for research)",
            "tenant": "synthcorp-research",
            "exported_on": EXPORT_DATE,
            "exported_by": "svc-idp-export",
            "export_scope": "Conditional access policies applying to privileged directory roles",
        },
        "conditional_access_policy": {
            "policy_id": "CA-POL-014",
            "display_name": "Require multi-factor authentication for privileged roles",
            "state": "enabled",
            "enforcement_mode": "enforced",
            "created_on": "2023-11-02",
            "last_modified_on": "2024-02-18",
            "assignments": {
                "included_groups": ["grp-privileged-administrators"],
                "included_directory_roles": [
                    "Domain Administrator",
                    "Database Administrator",
                    "Security Tool Administrator",
                    "Application Administrator",
                ],
                "included_service_accounts": "all accounts in grp-privileged-administrators",
                "excluded_users": [],
                "excluded_groups": [],
            },
            "grant_controls": {
                "operator": "AND",
                "built_in_controls": ["require_multi_factor_authentication"],
                "authentication_strength": "phishing_resistant_or_app_based",
            },
            "accepted_methods": ["authenticator_app", "fido2_security_key", "hardware_token"],
        },
        "enforcement_summary": {
            "privileged_accounts_in_scope": int(dataset.population["total"]),
            "accounts_registered_for_mfa": int(dataset.population["compliant"]),
            "accounts_excluded_from_policy": 0,
            "self_enrolment_permitted": "no",
        },
    }
    config_path = _write_json(directory / dataset.files[2].filename, config)
    if IDP_ENFORCEMENT_MARKER not in config_path.read_text(encoding="utf-8"):
        raise DatasetGenerationError(
            "{0}: the enforcement marker is not present in {1}.".format(dataset.dataset_id, config_path.name)
        )
    return [policy, export, config_path]


_BUILDERS: Dict[str, Callable[[SyntheticDataset, Path], List[Path]]] = {
    "DATASET-001": _build_001,
    "DATASET-002": _build_002,
    "DATASET-003": _build_003,
    "DATASET-004": _build_004,
    "DATASET-005": _build_005,
    "DATASET-006": _build_006,
}


# ---- public generation API


def generate_dataset(dataset_id: Any, output_dir: Optional[Any] = None) -> List[Path]:
    """Write one dataset's files and return their paths in declaration order."""
    dataset = get_dataset(dataset_id)
    builder = _BUILDERS.get(dataset.dataset_id)
    if builder is None:  # pragma: no cover - unreachable while the table is complete
        raise DatasetGenerationError("No generator registered for {0}.".format(dataset.dataset_id))
    directory = dataset_dir(dataset, output_dir)
    paths = builder(dataset, directory)

    written = [path.name for path in paths]
    if written != dataset.file_names:
        raise DatasetGenerationError(
            "{0}: wrote {1} but the dataset declares {2}.".format(
                dataset.dataset_id, written, dataset.file_names
            )
        )
    return paths


def generate_all(output_dir: Optional[Any] = None) -> Dict[str, List[Path]]:
    """Write every dataset in the suite, keyed by dataset id."""
    return {
        dataset.dataset_id: generate_dataset(dataset, output_dir=output_dir)
        for dataset in SYNTHETIC_DATASETS
    }


# ---- verification


def _parse_all(paths: Sequence[Path]) -> Dict[str, Any]:
    """Parse generated files with the production parser, as ingestion will."""
    from app.evidence.parsers import parse_file

    parsed: Dict[str, Any] = {}
    for path in paths:
        parsed[path.name] = parse_file(str(path), path.name)
    return parsed


def _marker_locations(marker: str, parsed: Dict[str, Any]) -> List[str]:
    """Where a marker turns up in the parsed chunks, as ``filename#chunk_index``."""
    hits: List[str] = []
    for filename, result in parsed.items():
        for chunk in result.chunks:
            if marker in chunk.text:
                hits.append("{0}#{1}".format(filename, chunk.chunk_index))
    return hits


_PLAIN_TEXT_SUFFIXES = (".txt", ".csv", ".json", ".md")


def _raw_marker_files(marker: str, paths: Sequence[Path]) -> List[str]:
    """Plain-text files whose bytes literally contain the marker.

    Recorded separately from the chunk hits because it can only ever be a partial check:
    ``.docx``, ``.xlsx`` and ``.pdf`` are containers, so "is this string in the file" is
    only answerable of their extracted text. The chunk hit is the one that matters, since
    a chunk is what retrieval returns and what a citation points at.
    """
    found: List[str] = []
    for path in paths:
        if path.suffix.lower() not in _PLAIN_TEXT_SUFFIXES or not path.exists():
            continue
        if marker in path.read_text(encoding="utf-8"):
            found.append(path.name)
    return found


def _tabular_frame(path: Path) -> Any:
    """Re-read a generated export the way an auditor would open it."""
    import pandas as pd

    if path.suffix.lower() == ".csv":
        return pd.read_csv(str(path), keep_default_na=False, na_values=[""])
    return pd.read_excel(str(path), engine="openpyxl")


def _check_population(dataset: SyntheticDataset, paths: Sequence[Path]) -> List[Dict[str, Any]]:
    """Re-derive the declared condition from the files themselves."""
    checks: List[Dict[str, Any]] = []
    population = dataset.population
    attribute = population.get("attribute")
    tabular = [p for p in paths if p.suffix.lower() in (".csv", ".xlsx")]

    if attribute and population.get("compliant_value") is not None:
        for path in tabular:
            frame = _tabular_frame(path)
            if attribute not in list(frame.columns):
                continue
            values = [str(v) for v in frame[attribute].tolist()]
            total = len(values)
            exceptions = (
                values.count(str(population["exception_value"])) if population.get("exception_value") else 0
            )
            compliant = values.count(str(population["compliant_value"]))
            checks.append(
                {
                    "check": "population counts in {0}".format(path.name),
                    "expected": {
                        "total": population.get("total"),
                        "compliant": population.get("compliant"),
                        "exceptions": population.get("exceptions"),
                    },
                    "actual": {"total": total, "compliant": compliant, "exceptions": exceptions},
                    "ok": total == population.get("total")
                    and compliant == population.get("compliant")
                    and exceptions == population.get("exceptions"),
                }
            )
            if dataset.exception_rows:
                key_column = list(frame.columns)[0]
                rows_ok = True
                observed: List[Any] = []
                for row_number in dataset.exception_rows:
                    index = row_number - 2
                    observed.append(str(frame.iloc[index][key_column]))
                    if str(frame.iloc[index][attribute]) != str(population["exception_value"]):
                        rows_ok = False
                checks.append(
                    {
                        "check": "exceptions sit at the declared spreadsheet rows",
                        "expected": population.get("exception_keys"),
                        "actual": observed,
                        "ok": rows_ok and observed == list(population.get("exception_keys", observed)),
                    }
                )
            break

    if population.get("configured_value") is not None:
        for path in tabular:
            frame = _tabular_frame(path)
            if "Setting_Name" not in list(frame.columns):
                continue
            row = frame[frame["Setting_Name"] == str(attribute)]
            configured = int(str(row["Configured_Value"].tolist()[0]))
            required = int(population["required_value"])
            checks.append(
                {
                    "check": "{0} is configured below the value the policy requires".format(attribute),
                    "expected": {"configured": population["configured_value"], "required": required},
                    "actual": {"configured": configured, "required": required},
                    "ok": configured == int(population["configured_value"]) and configured < required,
                }
            )
            break

    forbidden = population.get("forbidden_substrings") or []
    if forbidden:
        for path in tabular:
            # Filename included: the retriever folds it into the indexed document, so a
            # filename that leaked the attribute would defeat the dataset just as surely.
            haystack = (path.name + "\n" + path.read_text(encoding="utf-8")).lower()
            found = [term for term in forbidden if term in haystack]
            checks.append(
                {
                    "check": "no signal for the withheld attribute in {0}".format(path.name),
                    "expected": [],
                    "actual": found,
                    "ok": not found,
                }
            )
    return checks


def verify_dataset(
    dataset_id: Any,
    output_dir: Optional[Any] = None,
    regenerate: bool = True,
) -> Dict[str, Any]:
    """Generate (by default) and then prove one dataset against its own declaration.

    Returns a report rather than raising, so a caller can print the whole suite's state
    in one pass. ``ok`` is true only when every file parsed, every key evidence marker was
    found in some chunk of the file it belongs to, and the planted condition was
    re-derived from the data.
    """
    dataset = get_dataset(dataset_id)
    directory = dataset_dir(dataset, output_dir)
    if regenerate:
        paths = generate_dataset(dataset, output_dir=output_dir)
    else:
        paths = [directory / name for name in dataset.file_names]

    missing = [str(p) for p in paths if not p.exists()]
    parsed = _parse_all([p for p in paths if p.exists()])

    file_reports: List[Dict[str, Any]] = []
    for path in paths:
        result = parsed.get(path.name)
        file_reports.append(
            {
                "filename": path.name,
                "bytes": path.stat().st_size if path.exists() else 0,
                "chunks": result.chunk_count if result else 0,
                "rows": result.row_count if result else 0,
                "pages": result.page_count if result else 0,
                "warnings": list(result.warnings) if result else ["file was not written"],
                "ok": bool(result and result.chunks),
            }
        )

    marker_reports: List[Dict[str, Any]] = []
    for marker in dataset.key_evidence_markers:
        hits = _marker_locations(marker, parsed)
        marker_reports.append(
            {
                "marker": marker,
                "chunks": hits,
                "raw_files": _raw_marker_files(marker, paths),
                "ok": bool(hits),
            }
        )

    population_checks = _check_population(dataset, [p for p in paths if p.exists()])

    ok = (
        not missing
        and all(f["ok"] for f in file_reports)
        and all(m["ok"] for m in marker_reports)
        and all(c["ok"] for c in population_checks)
    )
    return {
        "dataset_id": dataset.dataset_id,
        "name": dataset.name,
        "control_ref": dataset.control_ref,
        "expected_status": dataset.expected_status.value,
        "directory": str(directory),
        "missing_files": missing,
        "files": file_reports,
        "markers": marker_reports,
        "population_checks": population_checks,
        "total_chunks": sum(f["chunks"] for f in file_reports),
        "ok": ok,
    }


def verify_all(output_dir: Optional[Any] = None, regenerate: bool = True) -> List[Dict[str, Any]]:
    """Verify every dataset in the suite, in suite order."""
    return [
        verify_dataset(dataset, output_dir=output_dir, regenerate=regenerate)
        for dataset in SYNTHETIC_DATASETS
    ]
