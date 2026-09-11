"""The synthetic evaluation datasets: evidence whose correct conclusion is known.

Every accuracy figure this project reports rests on these six records. A dataset is a
*declaration*: it states what evidence exists, what condition is planted in it, and
therefore what an auditor - and so the system - should conclude. :mod:`app.evaluation.
generator` then writes real files that satisfy the declaration, and
:func:`app.evaluation.generator.verify_dataset` re-parses those files and checks that
the declaration is still true of what is actually on disk. Ground truth that is only
asserted in a docstring is not ground truth; it has to be re-derivable from the bytes.

Why the ground truth lives here and the prose lives in the generator
-------------------------------------------------------------------
Anything a metric depends on - the population counts, which spreadsheet rows carry the
exception, which account names are the exceptions, the exact sentences retrieval must
surface - is declared in this module and consumed by the generator. The generator owns
only the surrounding narrative. That direction of dependency means a marker string can
never drift out of sync with the document that contains it: the document is built *from*
the marker.

The five mandated datasets and one addition
-------------------------------------------
DATASET-001 to DATASET-005 are the datasets the research design mandates. DATASET-006 is
an addition by this implementation and is flagged as such (``mandated=False``): without a
control whose correct answer is EFFECTIVE, a confusion matrix has no true-negative class,
every "deficiency detected" rate is undefined, and a system that simply answered
POTENTIAL_DEFICIENCY to everything would score 100% accuracy. It exists to make that
degenerate strategy visible, not to flatter the results.

The design deliberately holds one variable fixed
------------------------------------------------
DATASET-001, DATASET-005 and DATASET-006 all test CONTROL-001 against the *same* policy
document, byte for byte. Only the operational evidence differs:

===========  ==========================================  ======================
Dataset      Operational evidence                        Correct conclusion
===========  ==========================================  ======================
DATASET-001  account listing, MFA reported, 10 disabled   POTENTIAL_DEFICIENCY
DATASET-005  account listing with no MFA attribute        INSUFFICIENT_EVIDENCE
DATASET-006  account listing, MFA reported, none disabled EFFECTIVE
===========  ==========================================  ======================

Three different correct answers from one unchanged requirement is the cleanest available
demonstration that the system is reading the evidence rather than the control text - and
it is what makes the DATASET-005 result interpretable, because the only thing that
changed is the presence of the attribute under test.

Honesty constraints these datasets are built under
--------------------------------------------------
* Everything is fabricated. No file describes a real organisation, person or system, and
  every generated file says so in its own text.
* The planted conditions are *simple and unambiguous* - one attribute, one threshold, one
  clean exception rate. That is a limitation as much as a design: a system that scores
  well here has been shown to handle unambiguous evidence, which is the easy case. Real
  audit evidence is contradictory, partial and undated, and nothing in this suite
  measures behaviour on that.
* ``expected_risk`` is not an externally validated rating. It is the band the prototype
  risk model in :mod:`app.audit.risk` assigns to this control given the known status and
  exception ratio. It is recorded so risk-band agreement can be measured at all; it must
  never be reported as evidence that the risk model is correct.
* n = 6. Every metric computed over this suite is descriptive, not statistical.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from app.schemas.enums import AssessmentStatus, EvidenceType, RiskLevel

__all__ = [
    "SyntheticDataset",
    "DatasetFile",
    "SYNTHETIC_DATASETS",
    "UnknownDatasetError",
    "get_dataset",
    "dataset_ids",
    "generate_dataset",
    "generate_all",
    "dataset_manifest",
    "GENERATOR_SEED",
    "SYNTHETIC_NOTICE",
    "DATASET_DISCLAIMER",
]

#: One fixed seed for the whole suite, deliberately *not* read from settings. Evaluation
#: data must not change because someone adjusted an unrelated runtime knob; a result
#: recorded in the thesis has to be reproducible from this constant alone.
GENERATOR_SEED = 20240630

#: Notice carried inside every generated file, in whatever way the format allows. Kept
#: under 255 characters because that is the hard limit on an OOXML core property, and the
#: notice has to fit in the document metadata as well as in the visible text.
SYNTHETIC_NOTICE = (
    "SYNTHETIC RESEARCH DATA - fabricated for the LLM-Assisted IT Audit research prototype. "
    "It describes no real organisation, system, person or audit, and must not be relied on "
    "for any purpose other than testing that prototype."
)

#: Column appended to every generated tabular export so the notice survives into the
#: chunk text a model is shown - a metadata property nobody reads is not a disclosure.
DATA_ORIGIN_COLUMN = "Data_Origin"
DATA_ORIGIN_VALUE = "SYNTHETIC-RESEARCH-DATA"

DATASET_DISCLAIMER = (
    "These datasets are synthetic and were authored to have a known correct answer. Accuracy "
    "measured against them shows whether the pipeline reaches the planted conclusion on "
    "unambiguous evidence; it does not generalise to real audit evidence, and with six "
    "datasets no result here is statistically meaningful."
)

# ---- sentences that must survive into the generated documents
# These are the strings the retrieval-quality metric is scored against, so they are
# defined once here and written into the documents by the generator. A marker that is not
# a literal substring of the evidence it claims to come from would make that metric a
# measurement of nothing.

MFA_ENROLMENT_REQUIREMENT = (
    "Every account classified as privileged must be enrolled in the organisation's multi-factor "
    "authentication solution before privileged access is granted."
)
MFA_LISTING_REQUIREMENT = (
    "The privileged account listing produced for audit must report the multi-factor authentication "
    "enrolment status of every account in the population."
)
MFA_ENFORCEMENT_REQUIREMENT = (
    "Enrolment must be enforced centrally by a platform policy bound to the privileged account group; "
    "self-enrolment by individual account holders does not satisfy this policy."
)
PATCH_WINDOW_REQUIREMENT = (
    "Critical severity security patches must be deployed to all in-scope endpoints within 14 days of "
    "the vendor release date."
)
PATCH_POPULATION_REQUIREMENT = (
    "Patch compliance must be measured from the patch management platform against the authoritative "
    "asset inventory for the whole estate, not from a sample."
)
PASSWORD_LENGTH_REQUIREMENT = (
    "The minimum password length must be at least 14 characters for every user and administrative "
    "account in the domain."
)
PASSWORD_LENGTH_OBSERVATION = "Minimum password length: 8 characters"
CHANGE_APPROVAL_REQUIREMENT = (
    "No change may be implemented in the production environment until it has been approved by an "
    "authorised approver who is independent of the person who requested it."
)
CHANGE_TICKET_REQUIREMENT = (
    "Every production change must be raised as a change ticket recording the requester, the change "
    "type, the approver and the implementation date."
)
IDP_ENFORCEMENT_MARKER = '"display_name": "Require multi-factor authentication for privileged roles"'


@dataclass
class DatasetFile:
    """One evidence artefact a dataset is made of.

    ``evidence_type`` is load-bearing rather than decorative: the retriever copies it onto
    every chunk, and both the prompt and the offline provider use it to tell a document
    that *states a requirement* from an export that *records what happened*. Ingesting one
    of these files without its declared type degrades that distinction to a filename
    guess, so the evaluation runner must pass it through to
    ``app.evidence.service.ingest_file``.
    """

    filename: str
    evidence_type: str
    role: str
    description: str

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
        }


@dataclass
class SyntheticDataset:
    """One evaluation case: files to generate, and the conclusion they should support.

    Fields beyond those named in the build specification (``population``,
    ``exception_rows``, ``expected_finding``, ``expected_missing_evidence``, ``mandated``,
    ``seed``) are additions used by the generator and the metrics layer; the specified
    fields keep their specified meaning, so a consumer written against the specification
    is unaffected.
    """

    dataset_id: str
    name: str
    control_ref: str
    expected_status: AssessmentStatus
    expected_risk: RiskLevel
    files: List[DatasetFile]
    key_evidence_markers: List[str]
    notes: str
    rationale: str
    #: The planted condition, in numbers: what the generator must write and what a
    #: verifier must be able to re-derive from the files afterwards.
    population: Dict[str, Any] = field(default_factory=dict)
    #: 1-based spreadsheet rows (header = row 1) carrying the exceptions, so a citation
    #: can be checked against the file by eye.
    exception_rows: List[int] = field(default_factory=list)
    expected_finding: str = ""
    #: True when the correct behaviour includes *declaring* that evidence is missing.
    expected_missing_evidence: bool = False
    #: False for datasets added by this implementation beyond the mandated five.
    mandated: bool = True
    seed: int = GENERATOR_SEED

    # ---------------------------------------------------------------- helpers
    @property
    def file_names(self) -> List[str]:
        return [f.filename for f in self.files]

    @property
    def extensions(self) -> List[str]:
        return sorted({f.extension for f in self.files})

    def files_by_role(self, role: str) -> List[DatasetFile]:
        return [f for f in self.files if f.role == role]

    def file(self, filename: str) -> Optional[DatasetFile]:
        for candidate in self.files:
            if candidate.filename == filename:
                return candidate
        return None

    @property
    def exception_count(self) -> int:
        return int(self.population.get("exceptions", 0) or 0)

    @property
    def population_total(self) -> Optional[int]:
        total = self.population.get("total")
        return int(total) if total is not None else None

    @property
    def exception_ratio(self) -> Optional[float]:
        """Known exception rate, or None where the condition is not a rate at all.

        DATASET-003 (a configured value below its policy threshold) and DATASET-005
        (an attribute that is simply absent) have no denominator, and inventing one for
        them would be a false precision.
        """
        total = self.population_total
        if not total:
            return None
        return round(self.exception_count / float(total), 4)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "name": self.name,
            "control_ref": self.control_ref,
            "expected_status": self.expected_status.value,
            "expected_risk": self.expected_risk.value,
            "expected_finding": self.expected_finding,
            "expected_missing_evidence": self.expected_missing_evidence,
            "mandated": self.mandated,
            "files": [f.to_dict() for f in self.files],
            "file_names": self.file_names,
            "extensions": self.extensions,
            "key_evidence_markers": list(self.key_evidence_markers),
            "population": dict(self.population),
            "exception_rows": list(self.exception_rows),
            "exception_ratio": self.exception_ratio,
            "notes": self.notes,
            "rationale": self.rationale,
            "seed": self.seed,
        }


# ---- the six datasets

_MFA_POLICY = DatasetFile(
    filename="Multi_Factor_Authentication_Policy_v3.docx",
    evidence_type=EvidenceType.POLICY.value,
    role="requirement",
    description=(
        "Authentication policy requiring multi-factor authentication for every privileged account, "
        "and requiring the audit listing to report enrolment status. Identical in DATASET-001, "
        "DATASET-005 and DATASET-006 so that only the operational evidence differs between them."
    ),
)


DATASET_001 = SyntheticDataset(
    dataset_id="DATASET-001",
    name="MFA on privileged accounts - 10 of 100 accounts not enrolled",
    control_ref="CONTROL-001",
    expected_status=AssessmentStatus.POTENTIAL_DEFICIENCY,
    expected_risk=RiskLevel.CRITICAL,
    files=[
        _MFA_POLICY,
        DatasetFile(
            filename="Privileged_Accounts_MFA_Export_2024-06-30.csv",
            evidence_type=EvidenceType.USER_LISTING.value,
            role="population",
            description=(
                "Identity provider export of the 100 privileged accounts in scope, reporting MFA "
                "enrolment status and method per account."
            ),
        ),
    ],
    key_evidence_markers=[
        "MFA_Status",
        "svc-backup-014",
        "n.okonkwo",
        "Not Registered",
        MFA_ENROLMENT_REQUIREMENT,
    ],
    population={
        "total": 100,
        "compliant": 90,
        "exceptions": 10,
        "attribute": "MFA_Status",
        "compliant_value": "Enabled",
        "exception_value": "Disabled",
        "exception_method_value": "Not Registered",
        "exception_keys": [
            "svc-backup-014",
            "n.okonkwo",
            "svc-sql-021",
            "t.varga",
            "d.mensah",
            "svc-scan-037",
            "r.batistuta",
            "k.oyelaran",
            "svc-etl-052",
            "m.szabo",
        ],
    },
    exception_rows=[14, 22, 31, 38, 47, 55, 63, 71, 86, 97],
    expected_finding=(
        "10 of the 100 privileged accounts in the identity provider export show MFA_Status = "
        "Disabled, and no exception register was supplied to cover them."
    ),
    notes=(
        "The exception rate is deliberately clean (10%) and reported in a single column, which is "
        "the easy case: one attribute, one value, no contradiction between sources. No exception "
        "register is supplied, which is why the correct conclusion is POTENTIAL_DEFICIENCY rather "
        "than NOT_EFFECTIVE - the accounts may be covered by an approved exemption that was never "
        "provided, and an auditor cannot rule that out from this evidence."
    ),
    rationale=(
        "Tests the ordinary case the system exists for: a population export where the control "
        "operated for most of the population and demonstrably did not for a named minority. A "
        "correct answer requires reading the population counts rather than the majority value, and "
        "citing the specific rows."
    ),
)


DATASET_002 = SyntheticDataset(
    dataset_id="DATASET-002",
    name="Security patching - 5 of 100 endpoints missing critical patches",
    control_ref="CONTROL-003",
    expected_status=AssessmentStatus.POTENTIAL_DEFICIENCY,
    expected_risk=RiskLevel.HIGH,
    files=[
        DatasetFile(
            filename="Patch_Management_Standard_v2.pdf",
            evidence_type=EvidenceType.STANDARD.value,
            role="requirement",
            description=(
                "Patch management standard stating the remediation window per severity band and "
                "requiring compliance to be measured across the whole estate."
            ),
        ),
        DatasetFile(
            filename="Endpoint_Patch_Compliance_Export_2024-06-30.xlsx",
            evidence_type=EvidenceType.SYSTEM_REPORT.value,
            role="population",
            description=(
                "Patch management platform export for the 100 in-scope endpoints, with patch status, "
                "count of missing critical patches and days since last patch."
            ),
        ),
    ],
    key_evidence_markers=[
        "Patch_Status",
        "Non-Compliant",
        "EPT-0016",
        "Critical_Patches_Missing",
        PATCH_WINDOW_REQUIREMENT,
    ],
    population={
        "total": 100,
        "compliant": 95,
        "exceptions": 5,
        "attribute": "Patch_Status",
        "compliant_value": "Compliant",
        "exception_value": "Non-Compliant",
        "remediation_window_days": 14,
        "exception_keys": ["EPT-0016", "EPT-0028", "EPT-0043", "EPT-0067", "EPT-0090"],
        "exception_missing_patch_counts": [3, 1, 4, 2, 1],
        "exception_days_since_patch": [96, 61, 121, 74, 58],
    },
    exception_rows=[17, 29, 44, 68, 91],
    expected_finding=(
        "5 of the 100 in-scope endpoints show Patch_Status = Non-Compliant with at least one missing "
        "critical patch, each more than 14 days after the vendor release date required by the standard."
    ),
    notes=(
        "The requirement arrives as a PDF and the population as a spreadsheet, so a correct answer "
        "has to combine two formats. The five exceptions also exceed the 14-day window stated in the "
        "standard, so the finding can be stated as late remediation rather than only as a count - but "
        "nothing in the export evidences a risk acceptance, so an unqualified NOT_EFFECTIVE would "
        "overstate what the evidence shows."
    ),
    rationale=(
        "A second population-exception case with a different control, a different exception rate "
        "(5%) and different file formats, so that a system cannot score well on DATASET-001 by "
        "recognising one file shape."
    ),
)


DATASET_003 = SyntheticDataset(
    dataset_id="DATASET-003",
    name="Password policy - configuration enforces 8 characters against a policy of 14",
    control_ref="CONTROL-005",
    expected_status=AssessmentStatus.NOT_EFFECTIVE,
    expected_risk=RiskLevel.CRITICAL,
    files=[
        DatasetFile(
            filename="Password_Policy_v4.txt",
            evidence_type=EvidenceType.POLICY.value,
            role="requirement",
            description="Password policy stating the required value of each password parameter.",
        ),
        DatasetFile(
            filename="Domain_Password_Settings_Export_2024-06-30.csv",
            evidence_type=EvidenceType.CONFIGURATION_EXPORT.value,
            role="configuration",
            description=(
                "Directory security settings export listing the value actually enforced for each "
                "password and lockout parameter."
            ),
        ),
        DatasetFile(
            filename="Screenshot_Narrative_Domain_Password_Settings.txt",
            evidence_type=EvidenceType.SCREENSHOT_NARRATIVE.value,
            role="configuration",
            description=(
                "Auditor's written description of the password policy screen supplied by the "
                "administrator, corroborating the configuration export."
            ),
        ),
    ],
    key_evidence_markers=[
        "Minimum_Password_Length",
        PASSWORD_LENGTH_OBSERVATION,
        PASSWORD_LENGTH_REQUIREMENT,
    ],
    population={
        "total": 18,
        "compliant": 17,
        "exceptions": 1,
        "attribute": "Minimum_Password_Length",
        "required_value": 14,
        "configured_value": 8,
        "parameter_count": 18,
    },
    exception_rows=[2],
    expected_finding=(
        "The directory enforces a minimum password length of 8 characters where the password policy "
        "requires at least 14, so the documented policy is not the configuration in force."
    ),
    notes=(
        "This is the only dataset whose condition is a direct contradiction rather than an exception "
        "rate, which is why the correct status is NOT_EFFECTIVE and not POTENTIAL_DEFICIENCY: the "
        "control is evidenced as operating below its own requirement, with no population left in "
        "which it might be operating correctly. Every other parameter in the export matches the "
        "policy, so a system that reports a broader failure has overstated the evidence. The export "
        "also contains settings whose correct value is 'Disabled' (reversible encryption, guest "
        "account); a keyword-driven exception detector reads those as failures, and that "
        "false-positive trap is left in deliberately rather than sanitised away."
    ),
    rationale=(
        "Tests the REQUIRES-versus-PROVES distinction in its sharpest form. Both documents are about "
        "password length and both contain the word 'password'; only the direction of the comparison "
        "distinguishes a pass from a failure, so a system that merely retrieves topically relevant "
        "text cannot answer it."
    ),
)


DATASET_004 = SyntheticDataset(
    dataset_id="DATASET-004",
    name="Change approval - 5 of 100 production changes implemented without approval",
    control_ref="CONTROL-004",
    expected_status=AssessmentStatus.POTENTIAL_DEFICIENCY,
    expected_risk=RiskLevel.HIGH,
    files=[
        DatasetFile(
            filename="Change_Management_Policy_v5.docx",
            evidence_type=EvidenceType.POLICY.value,
            role="requirement",
            description=(
                "Change management policy requiring independent approval before any production change "
                "is implemented."
            ),
        ),
        DatasetFile(
            filename="Change_Tickets_Export_2024Q2.xlsx",
            evidence_type=EvidenceType.TICKET_EXPORT.value,
            role="population",
            description=(
                "Change ticket export for the quarter listing every production change with its "
                "requester, approver, approval status and implementation date."
            ),
        ),
    ],
    key_evidence_markers=[
        "Approval_Status",
        "Not Approved",
        "CHG-2024-0011",
        CHANGE_APPROVAL_REQUIREMENT,
    ],
    population={
        "total": 100,
        "compliant": 95,
        "exceptions": 5,
        "attribute": "Approval_Status",
        "compliant_value": "Approved",
        "exception_value": "Not Approved",
        "exception_keys": [
            "CHG-2024-0011",
            "CHG-2024-0025",
            "CHG-2024-0042",
            "CHG-2024-0056",
            "CHG-2024-0087",
        ],
    },
    exception_rows=[12, 26, 43, 57, 88],
    expected_finding=(
        "5 of the 100 production changes in the quarterly ticket export show Approval_Status = Not "
        "Approved with no approver recorded, yet carry an implementation date."
    ),
    notes=(
        "The exception rows carry an implementation date and an empty Approver cell, so the finding "
        "rests on two fields agreeing rather than on one status word. Note what this dataset cannot "
        "show: the export is the population, so a change implemented without any ticket at all would "
        "be invisible here, and an assessment that claims complete coverage of production changes is "
        "overstating the evidence regardless of the approval rate."
    ),
    rationale=(
        "A population-exception case in a third control family, and the one where the correct answer "
        "depends on a blank cell as much as on a status value."
    ),
)


DATASET_005 = SyntheticDataset(
    dataset_id="DATASET-005",
    name="MFA policy with a privileged user listing that reports no MFA attribute",
    control_ref="CONTROL-001",
    expected_status=AssessmentStatus.INSUFFICIENT_EVIDENCE,
    expected_risk=RiskLevel.HIGH,
    files=[
        _MFA_POLICY,
        DatasetFile(
            filename="Privileged_User_Listing_2024-06-30.csv",
            evidence_type=EvidenceType.USER_LISTING.value,
            role="population",
            description=(
                "Listing of the 100 privileged users with role, department, last logon and account "
                "status. It reports no multi-factor authentication attribute of any kind."
            ),
        ),
    ],
    key_evidence_markers=[
        "Account_Status",
        "Last_Login",
        "p.adeyemi",
        MFA_ENROLMENT_REQUIREMENT,
        MFA_LISTING_REQUIREMENT,
    ],
    population={
        "total": 100,
        "compliant": None,
        "exceptions": 0,
        "attribute": None,
        "absent_attribute": "MFA enrolment status",
        "columns": ["User", "Role", "Department", "Last_Login", "Account_Status"],
        #: Named so that a marker can prove the listing itself was retrieved, not only
        #: the policy: the generator writes these handles at the first data rows.
        "sample_keys": ["p.adeyemi", "svc-report-018"],
        "forbidden_substrings": [
            "mfa",
            "multi-factor",
            "multi factor",
            "multifactor",
            "two-factor",
            "two factor",
            "2fa",
            "second factor",
            "authenticator",
            "otp",
        ],
    },
    exception_rows=[],
    expected_finding=(
        "The privileged user listing supplied for this control reports no multi-factor "
        "authentication attribute, so it cannot show whether the control operated. The correct "
        "outcome is to state what is missing and request the enrolment export, not to conclude."
    ),
    expected_missing_evidence=True,
    notes=(
        "This is the dataset the project exists to demonstrate, and it is also the easiest one to "
        "break: the listing must be tempting (privileged users, roles, last logon, account status - "
        "everything except the attribute under test) while containing no MFA signal whatsoever. The "
        "generator therefore scans the generated file for every forbidden substring above, "
        "case-insensitively, and refuses to produce it if any appears. Every account is Active, so "
        "there is no incidental exception a system could latch onto to produce a right answer for "
        "the wrong reason."
    ),
    rationale=(
        "The measurable form of the claim that motivates the whole system: a confident conclusion "
        "drawn from evidence that never addressed the question is the failure mode under test. "
        "EFFECTIVE here is a false negative of the most dangerous kind, because the control may well "
        "be failing and the assessment says the opposite; POTENTIAL_DEFICIENCY is also wrong, "
        "because nothing in the evidence shows the control failing either."
    ),
)


DATASET_006 = SyntheticDataset(
    dataset_id="DATASET-006",
    name="MFA on privileged accounts - complete enrolment with enforcement evidence",
    control_ref="CONTROL-001",
    expected_status=AssessmentStatus.EFFECTIVE,
    expected_risk=RiskLevel.LOW,
    files=[
        _MFA_POLICY,
        DatasetFile(
            filename="Privileged_Accounts_MFA_Export_2024-06-30_Q2.csv",
            evidence_type=EvidenceType.USER_LISTING.value,
            role="population",
            description=(
                "Identity provider export of the 100 privileged accounts in scope; every account "
                "reports MFA_Status = Enabled with a registered method."
            ),
        ),
        DatasetFile(
            filename="Identity_Provider_MFA_Enforcement_Config_Export.json",
            evidence_type=EvidenceType.CONFIGURATION_EXPORT.value,
            role="configuration",
            description=(
                "Conditional access policy export showing the MFA grant control enforced centrally "
                "against the privileged account group, with no excluded users."
            ),
        ),
    ],
    key_evidence_markers=[
        "MFA_Status",
        "Enabled",
        IDP_ENFORCEMENT_MARKER,
        MFA_ENFORCEMENT_REQUIREMENT,
    ],
    population={
        "total": 100,
        "compliant": 100,
        "exceptions": 0,
        "attribute": "MFA_Status",
        "compliant_value": "Enabled",
        "exception_value": None,
    },
    exception_rows=[],
    expected_finding=(
        "All 100 privileged accounts report MFA_Status = Enabled and the identity provider "
        "configuration shows enforcement bound to the privileged group with no exclusions. No "
        "exception was identified in the evidence supplied."
    ),
    mandated=False,
    notes=(
        "Added by this implementation beyond the five mandated datasets, and reported as an addition "
        "wherever these results appear. Without it there is no true-negative class: precision and "
        "the false-positive rate for the 'deficiency detected' framing are undefined, and a system "
        "that answered POTENTIAL_DEFICIENCY unconditionally would look perfect. The enforcement "
        "export is included because a listing alone shows enrolment, not that enrolment is enforced, "
        "and the control requires the latter. EFFECTIVE here still means 'no exception in the "
        "evidence supplied' - it is not an assurance that the population is complete."
    ),
    rationale=(
        "Gives the confusion matrix its true-negative class and tests the opposite failure mode from "
        "DATASET-001: a system biased towards finding deficiencies will produce a false positive "
        "here, which is the error that destroys an audit tool's credibility with its users."
    ),
)


#: Ordered, and the order matters: DATASET-001 to DATASET-005 are the mandated cases and
#: are listed first so that any report which truncates the suite truncates the addition.
SYNTHETIC_DATASETS: List[SyntheticDataset] = [
    DATASET_001,
    DATASET_002,
    DATASET_003,
    DATASET_004,
    DATASET_005,
    DATASET_006,
]

_BY_ID: Dict[str, SyntheticDataset] = {d.dataset_id: d for d in SYNTHETIC_DATASETS}


class UnknownDatasetError(KeyError):
    """Raised for a dataset id that is not part of the suite."""


def dataset_ids(mandated_only: bool = False) -> List[str]:
    """Dataset identifiers in suite order."""
    return [d.dataset_id for d in SYNTHETIC_DATASETS if d.mandated or not mandated_only]


def get_dataset(dataset_id: Any) -> SyntheticDataset:
    """Look a dataset up by id, tolerantly.

    ``"DATASET-001"``, ``"dataset_001"``, ``"001"`` and ``1`` all resolve, because these
    ids are typed into a UI field and passed on a command line as often as they are
    copied. An unknown id raises rather than returning None: silently evaluating nothing
    would look like a passing run.
    """
    if isinstance(dataset_id, SyntheticDataset):
        return dataset_id
    raw = str(dataset_id).strip().upper().replace("_", "-")
    if raw in _BY_ID:
        return _BY_ID[raw]
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits:
        candidate = "DATASET-{0:03d}".format(int(digits))
        if candidate in _BY_ID:
            return _BY_ID[candidate]
    raise UnknownDatasetError(
        "Unknown dataset '{0}'. Available datasets: {1}.".format(dataset_id, ", ".join(dataset_ids()))
    )


def generate_dataset(dataset_id: Any, output_dir: Optional[Any] = None) -> List[Path]:
    """Write one dataset's files to disk and return their paths.

    Imported lazily so that reading dataset metadata - which the UI, the manifest and the
    documentation all do - never pulls in pandas, openpyxl, python-docx and reportlab.
    """
    from app.evaluation.generator import generate_dataset as _generate

    return _generate(dataset_id, output_dir=output_dir)


def generate_all(output_dir: Optional[Any] = None) -> Dict[str, List[Path]]:
    """Write every dataset's files, keyed by dataset id."""
    from app.evaluation.generator import generate_all as _generate_all

    return _generate_all(output_dir=output_dir)


def dataset_manifest() -> List[Dict[str, Any]]:
    """Serialisable description of the suite for the UI, the API and the write-up."""
    return [dataset.to_dict() for dataset in SYNTHETIC_DATASETS]


def markers_for(dataset_id: Any) -> List[str]:
    """The strings correct retrieval must surface for one dataset."""
    return list(get_dataset(dataset_id).key_evidence_markers)


def expected_status_map(dataset_ids_: Optional[Sequence[Any]] = None) -> Dict[str, str]:
    """``{dataset_id: expected status}`` - the ground truth the runner scores against."""
    datasets = (
        [get_dataset(d) for d in dataset_ids_] if dataset_ids_ is not None else list(SYNTHETIC_DATASETS)
    )
    return {d.dataset_id: d.expected_status.value for d in datasets}
