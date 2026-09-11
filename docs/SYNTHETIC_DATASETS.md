# The Synthetic Evaluation Datasets

**Every accuracy figure this project reports rests on these six cases.** This document
states, for each one: how it is built, exactly what is in it, what the correct answer is,
*why* that is the correct answer, which strings retrieval must surface, and which specific
capability it probes.

Everything here is fabricated. No file describes a real organisation, system, person or
audit, and every generated file says so in its own visible text as well as in its document
metadata. Tabular exports carry a `Data_Origin = SYNTHETIC-RESEARCH-DATA` column so the
notice survives into the chunk text a model is actually shown — a disclaimer buried in a
file property that nobody reads is not a disclosure.

> **DATASET-005 is the discriminating case for the whole study.** It is the only case where
> the correct behaviour is to refuse to conclude, and it is the case on which the three
> experimental conditions actually separate. If the project's central claim is going to fail,
> it fails here. §6 and §8 explain why, and `EXAMPLE_RESULTS.md` §2 shows what each
> condition did with it.

---

## 1. What a "dataset" is, mechanically

| Layer | Module | Role |
|---|---|---|
| Declaration | `app.evaluation.datasets` | States the population, the planted condition, the exception rows, the retrieval markers, and the expected conclusion. This is the ground truth. |
| Generation | `app.evaluation.generator` | Writes real `.csv` / `.xlsx` / `.docx` / `.pdf` / `.txt` / `.json` files **from** the declaration. Owns only the surrounding narrative prose. |
| Verification | `generator.verify_dataset` | Re-parses the written bytes and checks the declaration is still true of them — counts, exception rows, every marker string. |

The dependency runs declaration → document, never the reverse. That is what makes it
impossible for a marker string to drift out of sync with the document that is supposed to
contain it: the document is built from the marker. `prepare_datasets(verify=True)` runs the
verification before every evaluation suite and **raises rather than evaluating** against a
dataset that no longer matches its own declaration.

`GENERATOR_SEED = 20240630` is a module constant, deliberately not read from settings, so
evaluation data cannot change because an unrelated runtime knob was adjusted. Files are
written to `settings.synthetic_dir` (default `data/synthetic/dataset-00N/`).

### 1.1 The design holds one variable fixed

DATASET-001, -005 and -006 all test **CONTROL-001** against the **same policy document, byte
for byte** (`Multi_Factor_Authentication_Policy_v3.docx`, SHA-256 prefix `0c5ad952c4abdbc0`,
37 789 bytes, 5 chunks). Only the operational evidence differs:

| Dataset | Operational evidence | Correct conclusion |
|---|---|---|
| DATASET-001 | Account listing; MFA reported; 10 of 100 not enrolled | POTENTIAL_DEFICIENCY |
| DATASET-005 | Account listing with **no MFA attribute at all** | INSUFFICIENT_EVIDENCE |
| DATASET-006 | Account listing; MFA reported; none disabled; plus enforcement config | EFFECTIVE |

Three different correct answers from one unchanged requirement. This is the cleanest
available demonstration that a system is reading the *evidence* rather than paraphrasing the
*control text*, and it is what makes the DATASET-005 result interpretable: the only thing
that changed is the presence of the attribute under test.

### 1.2 Suite at a glance

| ID | Control | Condition planted | Expected status | Expected risk | Files | Chunks | Mandated |
|---|---|---|---|---|---|---|---|
| DATASET-001 | CONTROL-001 MFA on privileged accounts | 10 / 100 not enrolled | POTENTIAL_DEFICIENCY | CRITICAL | 2 (docx, csv) | 10 | yes |
| DATASET-002 | CONTROL-003 Security patching | 5 / 100 endpoints non-compliant | POTENTIAL_DEFICIENCY | HIGH | 2 (pdf, xlsx) | 7 | yes |
| DATASET-003 | CONTROL-005 Password policy | configured 8 vs policy 14 | NOT_EFFECTIVE | CRITICAL | 3 (txt, csv, txt) | 9 | yes |
| DATASET-004 | CONTROL-004 Change approval | 5 / 100 changes unapproved | POTENTIAL_DEFICIENCY | HIGH | 2 (docx, xlsx) | 10 | yes |
| DATASET-005 | CONTROL-001 MFA on privileged accounts | attribute under test absent | INSUFFICIENT_EVIDENCE | HIGH | 2 (docx, csv) | 10 | yes |
| DATASET-006 | CONTROL-001 MFA on privileged accounts | fully compliant + enforcement | EFFECTIVE | LOW | 3 (docx, csv, json) | 12 | **no — added** |

`expected_risk` is **not** an externally validated rating. It is the band the prototype risk
model in `app.audit.risk` assigns given the known status and exception ratio. It is recorded
so risk-band agreement can be measured at all, and it must never be reported as evidence that
the risk model is correct.

### 1.3 Why DATASET-006 was added beyond the five mandated cases

Without a case whose correct answer is EFFECTIVE, the confusion matrix has **no
true-negative class**. Precision and the false-positive rate for the "deficiency detected"
framing are undefined, and a system that answered POTENTIAL_DEFICIENCY to everything would
score 5/5 and look perfect. DATASET-006 exists to make that degenerate strategy visible, not
to flatter the results, and it is flagged `mandated=False` and reported as an addition
wherever these results appear.

Its effect is real: in the reported run, mode A's deficiency false-positive rate is 0.500
**entirely because of DATASET-005** (a case it wrongly flagged) with DATASET-006 supplying the
only true negative it got right. Remove DATASET-006 and FPR becomes 1.000; remove both
negatives and it becomes undefined. One added case moves a headline number from "undefined"
to "0.5", which is exactly how fragile every proportion at n = 6 is.

---

## 2. DATASET-001 — MFA on privileged accounts: 10 of 100 accounts not enrolled

**Control:** CONTROL-001 Multi-Factor Authentication for Privileged Accounts
(inherent risk HIGH, privilege level 5/5, data sensitivity 4/5).

### Composition

| File | Type | Role | Size | Parsed as |
|---|---|---|---|---|
| `Multi_Factor_Authentication_Policy_v3.docx` | POLICY | requirement | 37 789 B | 5 `DOCX_PARAGRAPH` chunks, 2 080 chars |
| `Privileged_Accounts_MFA_Export_2024-06-30.csv` | USER_LISTING | population | 12 947 B | 1 `TABLE_SUMMARY` + 4 `TABLE_ROWS` chunks, 100 data rows |

Export columns: `Account_Name, Account_Type, Department, Privilege_Level, MFA_Status,
MFA_Method, Last_Login, Export_Date, Data_Origin`.

### The planted condition

`MFA_Status` is `Enabled` for 90 accounts and `Disabled` for 10; every exception also carries
`MFA_Method = Not Registered`, so the finding rests on two fields agreeing rather than on one
status word. Exceptions are at **1-based spreadsheet rows 14, 22, 31, 38, 47, 55, 63, 71, 86,
97** (header = row 1), so any citation can be checked against the file by eye:

| Row | Account | Type | Tier |
|---|---|---|---|
| 14 | `svc-backup-014` | Service Account | Tier 0 |
| 22 | `n.okonkwo` | Domain Administrator | Tier 1 |
| 31 | `svc-sql-021` | Service Account | Tier 1 |
| 38 | `t.varga` | Domain Administrator | Tier 0 |
| 47 | `d.mensah` | Database Administrator | Tier 0 |
| 55 | `svc-scan-037` | Service Account | Tier 1 |
| 63 | `r.batistuta` | Database Administrator | Tier 1 |
| 71 | `k.oyelaran` | Database Administrator | Tier 0 |
| 86 | `svc-etl-052` | Service Account | Tier 0 |
| 97 | `m.szabo` | Application Administrator | Tier 1 |

Exception ratio = 0.10.

### Ground truth: POTENTIAL_DEFICIENCY, and why not NOT_EFFECTIVE

The control demonstrably operated for 90% of the population, and 10 named accounts
demonstrably fall outside it. **No exception register was supplied.** The control's own
assessment criteria allow an account to be compliant if it appears in an approved,
unexpired exemption — and an auditor cannot rule that out from this evidence. A deficiency
that *may* be covered by an exemption nobody produced is a potential deficiency requiring
auditor confirmation, not an established control failure. Concluding NOT_EFFECTIVE would
overstate what the evidence shows; concluding EFFECTIVE would ignore ten Tier-0 and Tier-1
administrators authenticating with a password alone.

**Expected finding:** *10 of the 100 privileged accounts in the identity provider export
show MFA_Status = Disabled, and no exception register was supplied to cover them.*

### Key evidence markers (5)

`MFA_Status`, `svc-backup-014`, `n.okonkwo`, `Not Registered`, and the policy sentence
*"Every account classified as privileged must be enrolled in the organisation's multi-factor
authentication solution before privileged access is granted."*

### What it probes

The ordinary case the system exists for: a population export where the control operated for
most of the population and demonstrably did not for a named minority. A correct answer
requires reading the **population counts** rather than the majority value, and citing the
specific rows. It is deliberately the easy version of that case — one attribute, one value,
no contradiction between sources — and a system that scores well on it has been shown to
handle unambiguous evidence only.

---

## 3. DATASET-002 — Security patching: 5 of 100 endpoints missing critical patches

**Control:** CONTROL-003 Security Patch Management (inherent HIGH, privilege 4, sensitivity 4).

### Composition

| File | Type | Role | Size | Parsed as |
|---|---|---|---|---|
| `Patch_Management_Standard_v2.pdf` | STANDARD | requirement | 2 821 B | 2 `PDF_PAGE` chunks, 1 page |
| `Endpoint_Patch_Compliance_Export_2024-06-30.xlsx` | SYSTEM_REPORT | population | 8 951 B | 1 `TABLE_SUMMARY` + 4 `TABLE_ROWS`, 100 rows |

Export columns: `Hostname, OS_Version, Owner_Team, Patch_Status, Critical_Patches_Missing,
Last_Patch_Date, Days_Since_Patch, Data_Origin`.

### The planted condition

95 endpoints `Compliant`, 5 `Non-Compliant`, at spreadsheet rows 17, 29, 44, 68, 91:

| Row | Hostname | Missing critical patches | Days since patch |
|---|---|---|---|
| 17 | `EPT-0016` | 3 | 96 |
| 29 | `EPT-0028` | 1 | 61 |
| 44 | `EPT-0043` | 4 | 121 |
| 68 | `EPT-0067` | 2 | 74 |
| 91 | `EPT-0090` | 1 | 58 |

Every one exceeds the **14-day** critical remediation window stated in the standard, so the
finding can be expressed as late remediation and not merely as a count. Exception ratio = 0.05.

### Ground truth: POTENTIAL_DEFICIENCY

Same reasoning as DATASET-001, one step stronger on the evidence (the delay is quantified in
the export) and one step weaker on coverage: the standard requires a documented risk
acceptance for an endpoint that cannot be patched in window, and **no risk acceptance is
supplied either way**. Their absence from this evidence is not proof they do not exist, so an
unqualified NOT_EFFECTIVE would overstate the evidence.

### Key evidence markers (5)

`Patch_Status`, `Non-Compliant`, `EPT-0016`, `Critical_Patches_Missing`, and *"Critical
severity security patches must be deployed to all in-scope endpoints within 14 days of the
vendor release date."*

### What it probes

A second population-exception case with a **different control, a different exception rate
(5% vs 10%), and different file formats — the requirement arrives as a PDF and the population
as a spreadsheet**. Its purpose is to stop a system scoring well on DATASET-001 by
recognising one file shape. A correct answer must combine a PDF-extracted threshold with a
spreadsheet-derived count.

---

## 4. DATASET-003 — Password policy: configuration enforces 8 characters against a policy of 14

**Control:** CONTROL-005 Password Policy Enforcement (inherent HIGH, privilege 4, sensitivity 3).

### Composition

| File | Type | Role | Size | Parsed as |
|---|---|---|---|---|
| `Password_Policy_v4.txt` | POLICY | requirement | 1 369 B | 6 `TEXT_BLOCK` chunks |
| `Domain_Password_Settings_Export_2024-06-30.csv` | CONFIGURATION_EXPORT | configuration | 2 233 B | 1 `TABLE_SUMMARY` + 1 `TABLE_ROWS`, 18 rows |
| `Screenshot_Narrative_Domain_Password_Settings.txt` | SCREENSHOT_NARRATIVE | configuration | 1 183 B | 1 `TEXT_BLOCK` chunk |

### The planted condition

The policy states: *"The minimum password length must be at least 14 characters for every
user and administrative account in the domain."* The configuration export's very first data
row (spreadsheet row 2) states `Minimum_Password_Length = 8`. The screenshot narrative
independently corroborates: *"Minimum password length: 8 characters"*.

**All 17 other parameters match the policy.** History 24, max age 90, min age 1, complexity
Enabled, lockout threshold 5 — every one is as required.

This is the only dataset whose condition is a **contradiction** rather than an exception
rate. There is no denominator, so `exception_ratio` is `None`; inventing one would be false
precision.

### Ground truth: NOT_EFFECTIVE, and why not POTENTIAL_DEFICIENCY

The control is evidenced as operating **below its own requirement**, with no residual
population in which it might be operating correctly. Every account in the domain
authenticates under an 8-character minimum. There is no exemption that could rescue it and
nothing an exception register could add: the configuration in force *is* the finding. This is
the one case in the suite where a definite negative conclusion is warranted.

Conversely, a system that reports a **broader** failure has also overstated the evidence —
seventeen parameters are compliant and saying otherwise is as wrong as missing the one that
is not.

### The false-positive trap, left in deliberately

The export contains settings whose **correct** value is `Disabled`:
`Store_Passwords_Using_Reversible_Encryption = Disabled`, `Guest_Account_Status = Disabled`,
`Anonymous_SID_Enumeration = Disabled`, and `Fine_Grained_Password_Policy_Applied = No`. A
keyword-driven exception detector that treats "Disabled"/"No" as failure reads three or four
findings here instead of one. The trap was not sanitised away, because a dataset that has had
its hard parts removed measures nothing.

### Key evidence markers (3)

`Minimum_Password_Length`, `Minimum password length: 8 characters`, and the policy sentence
above.

### What it probes

**The REQUIRES-versus-PROVES distinction in its sharpest form.** Both documents are about
password length; both contain the word "password"; both are topically identical to any
retrieval system. Only the *direction of the comparison* — 8 < 14 — distinguishes a pass from
a failure. A system that merely retrieves topically relevant text cannot answer this, and a
system that confuses the policy statement with an observation will report the requirement as
the finding.

It also probes triangulation across three formats and a corroborating narrative, and it is
the only case where the *same fact* is present in two independent artefacts.

---

## 5. DATASET-004 — Change approval: 5 of 100 production changes implemented without approval

**Control:** CONTROL-004 Change Management Approval (inherent HIGH, privilege 4, sensitivity 3).

### Composition

| File | Type | Role | Size | Parsed as |
|---|---|---|---|---|
| `Change_Management_Policy_v5.docx` | POLICY | requirement | 37 643 B | 5 `DOCX_PARAGRAPH` chunks |
| `Change_Tickets_Export_2024Q2.xlsx` | TICKET_EXPORT | population | 9 365 B | 1 `TABLE_SUMMARY` + 4 `TABLE_ROWS`, 100 rows |

Export columns: `Change_ID, Change_Type, System, Requested_By, Approver, Approval_Status,
Implementation_Date, Data_Origin`.

### The planted condition

95 `Approved`, 5 `Not Approved` at spreadsheet rows 12, 26, 43, 57, 88. Each exception has an
**empty `Approver` cell** *and* a populated `Implementation_Date` — it went into production
without an approver:

| Row | Change | Type | System | Implemented |
|---|---|---|---|---|
| 12 | `CHG-2024-0011` | Normal | Data Warehouse | 2024-04-09 |
| 26 | `CHG-2024-0025` | Standard | Core Banking Application | 2024-04-22 |
| 43 | `CHG-2024-0042` | Emergency | Identity Provider | 2024-05-07 |
| 57 | `CHG-2024-0056` | Normal | Document Management System | 2024-05-20 |
| 88 | `CHG-2024-0087` | Emergency | Reporting Service | 2024-06-17 |

Exception ratio = 0.05.

### Ground truth: POTENTIAL_DEFICIENCY

Five production changes with no recorded approval and a recorded implementation date. As with
DATASET-001 and -002 the qualified status is correct because the finding rests on a ticket
export whose completeness cannot be confirmed from the export itself.

**Note what this dataset cannot show, and what an assessment of it must not claim:** the
export *is* the population under test, so **a change implemented with no ticket at all would
be invisible here**. An assessment that claims complete coverage of production changes is
overstating the evidence regardless of the approval rate. This is a deliberate teaching case
about population completeness, and the correct output should push it into
`human_verification_required`.

### Key evidence markers (4)

`Approval_Status`, `Not Approved`, `CHG-2024-0011`, and *"No change may be implemented in the
production environment until it has been approved by an authorised approver who is
independent of the person who requested it."*

### What it probes

A population-exception case in a third control family, and the only one where the correct
answer depends on **a blank cell** as much as on a status value. A system reading only
`Approval_Status` gets the right count; a system that also reads the empty `Approver` against
a populated `Implementation_Date` can state *why* it is a finding.

---

## 6. DATASET-005 — MFA policy with a privileged user listing that reports no MFA attribute

> **This is the case the project exists to demonstrate.**

**Control:** CONTROL-001 — the same control and the same policy document as DATASET-001 and
DATASET-006.

### Composition

| File | Type | Role | Size | Parsed as |
|---|---|---|---|---|
| `Multi_Factor_Authentication_Policy_v3.docx` | POLICY | requirement | 37 789 B | 5 `DOCX_PARAGRAPH` chunks — **identical bytes to DATASET-001 and -006** |
| `Privileged_User_Listing_2024-06-30.csv` | USER_LISTING | population | 9 269 B | 1 `TABLE_SUMMARY` + 4 `TABLE_ROWS`, 100 rows |

Listing columns: `User, Role, Department, Last_Login, Account_Status, Data_Origin`.
**There is no MFA column, and no column that stands in for one.**

### The planted condition

100 privileged users — Domain Administrators, Database Administrators, Security Tool
Administrators, Service Accounts — with roles, departments, last-logon dates and account
status. Every account is `Active`, so there is no incidental exception a system could latch
onto to produce a right answer for the wrong reason. The listing is deliberately *tempting*:
it looks exactly like the artefact this control is normally tested with, and it contains
everything except the attribute under test.

The generator enforces the absence mechanically. It scans the produced file,
case-insensitively, for every one of: `mfa`, `multi-factor`, `multi factor`, `multifactor`,
`two-factor`, `two factor`, `2fa`, `second factor`, `authenticator`, `otp` — and **refuses to
write the file** if any appears. The claim "there is no MFA signal in this evidence" is
therefore checked against the bytes, not asserted in prose.

The policy document, meanwhile, is present and explicitly requires enrolment *and* requires
the listing to report enrolment status. So the evidence establishes what is **required** and
says nothing about what **happened**.

### Ground truth: INSUFFICIENT_EVIDENCE

The supplied evidence cannot show whether the control operated. The correct outcome is to
**state what is missing and request the enrolment export**, not to conclude.

Both wrong answers are wrong for distinct and important reasons:

* **EFFECTIVE** is a false negative of the most dangerous kind. The control may well be
  failing and the assessment would say the opposite, on evidence that never addressed it.
* **POTENTIAL_DEFICIENCY** is also wrong, and is the *seductive* error: it feels
  appropriately cautious. But nothing in this evidence shows the control failing. Flagging a
  deficiency because an export lacks a column converts a gap in the **export** into a finding
  about the **control** — it sends an auditor to challenge a control owner who may be
  entirely compliant, and it is the error that destroys a tool's credibility with its users.

Correct behaviour additionally requires **naming the gap**: the missing-evidence detection
metric only credits a row that concludes INSUFFICIENT_EVIDENCE *and* names at least one
missing artefact, because a bare "insufficient evidence" tells an auditor nothing about what
to obtain.

### Key evidence markers (5)

`Account_Status`, `Last_Login`, `p.adeyemi` — chosen so a marker can prove the **listing
itself** was retrieved, not only the policy — plus two policy sentences: the enrolment
requirement and *"The privileged account listing produced for audit must report the
multi-factor authentication enrolment status of every account in the population."*

That second marker is the pivot of the case. It is the sentence that lets a correct system
say not merely "I cannot tell" but "the artefact you supplied does not meet the policy's own
evidence requirement".

### What it probes

The measurable form of the claim that motivates the entire system: **a confident conclusion
drawn from evidence that never addressed the question is the failure mode under test.**

It is also the hardest case to keep honest, because the temptation is symmetrical — a
retrieval system will happily return the MFA policy (it is full of the right words) and a
generative model will happily write about MFA on the strength of it. Getting this right
requires distinguishing a document that *states a requirement* from an export that *records
what happened*, and then noticing that the second one is silent on the attribute in the first.

**In the reported run, this is the only dataset on which the three conditions disagree**
(`EXAMPLE_RESULTS.md` §2). Remove it and A, B and C all score 4/5 on status and the study has
no result.

---

## 7. DATASET-006 — MFA on privileged accounts: complete enrolment with enforcement evidence

**Control:** CONTROL-001 — same control, same policy document. Added beyond the mandated five
(`mandated=False`).

### Composition

| File | Type | Role | Size | Parsed as |
|---|---|---|---|---|
| `Multi_Factor_Authentication_Policy_v3.docx` | POLICY | requirement | 37 789 B | 5 `DOCX_PARAGRAPH` chunks |
| `Privileged_Accounts_MFA_Export_2024-06-30_Q2.csv` | USER_LISTING | population | 12 955 B | 1 `TABLE_SUMMARY` + 4 `TABLE_ROWS`, 100 rows |
| `Identity_Provider_MFA_Enforcement_Config_Export.json` | CONFIGURATION_EXPORT | configuration | 1 760 B | 2 `TEXT_BLOCK` chunks |

### The planted condition

All 100 accounts report `MFA_Status = Enabled` with a registered method (Authenticator App
34, FIDO2 Security Key 33, Hardware Token 33). Exception ratio = 0.00.

The JSON export shows conditional access policy `CA-POL-014`, *"Require multi-factor
authentication for privileged roles"*, `state: enabled`, `enforcement_mode: enforced`, bound
to `grp-privileged-administrators` and four directory roles, with `excluded_users: []`,
`excluded_groups: []` and `self_enrolment_permitted: "no"`.

### Ground truth: EFFECTIVE — with a precise meaning

The enforcement export is present **because a listing alone shows enrolment, not that
enrolment is enforced**, and the control requires the latter (its assessment criteria say so
explicitly: *"MFA enforcement is evidenced as a platform-level policy bound to the privileged
group, not as per-user self-enrolment"*). A dataset with a clean listing and no enforcement
evidence would arguably not be an EFFECTIVE case at all.

**EFFECTIVE here means "no exception was identified in the evidence supplied."** It is not an
assurance that the population is complete, that the export was not edited, or that the
control operated throughout the period. Those remain human verifications, and an assessment
that words its conclusion more strongly than this has overstated the evidence even though its
status label is correct.

### Key evidence markers (4)

`MFA_Status`, `Enabled`, the JSON line
`"display_name": "Require multi-factor authentication for privileged roles"`, and the policy
sentence *"Enrolment must be enforced centrally by a platform policy bound to the privileged
account group; self-enrolment by individual account holders does not satisfy this policy."*

### What it probes

The **opposite failure mode** from DATASET-001: a system biased towards finding deficiencies
produces a false positive here. It supplies the confusion matrix's only true-negative class
and it is the only case that can catch a "flag everything" strategy.

It is also the most fragile case in the suite from an implementation standpoint. An
all-compliant population has no exception rows to anchor on, so a reader that picks the wrong
column — `MFA_Method`, whose values are `Authenticator App` / `FIDO2 Security Key` /
`Hardware Token`, none of which classify as compliant or exceptional — produces
INSUFFICIENT_EVIDENCE instead of EFFECTIVE. That exact regression occurred during
development and is recorded here as the case to re-run first after any change to the
evidence-reading rules.

---

## 8. What the suite covers, and what it does not

### Covered

| Capability | Cases |
|---|---|
| Population exception detection with a stated denominator | 001, 002, 004 |
| Threshold comparison against a policy value | 003 |
| Refusal to conclude when the attribute is absent | **005** |
| Correct clean opinion (true negative) | 006 |
| Combining a requirement document with an operational export | all six |
| Cross-format reading (docx, pdf, xlsx, csv, txt, json) | 001–006 |
| Corroboration across two artefacts | 003, 006 |
| Distinguishing a policy statement from an observation | 003, 005 |
| Resisting a keyword-driven false positive | 003 |
| Resisting a deficiency-bias false positive | 006 |
| Reasoning from a blank cell | 004 |

### Not covered — and these are the gaps that bound every claim

* **No contradictory evidence.** Nothing in the suite contains two artefacts that disagree,
  totals that do not reconcile, or a policy superseded by an undocumented practice.
  Reconciliation is a large part of real fieldwork and none of it is tested.
* **No stale or out-of-period evidence.** Every export is dated inside the audit period. The
  control criteria explicitly test for period coverage; the datasets never violate it, so
  that branch is never exercised.
* **No sampling.** Every population is the whole population. Nothing tests behaviour where a
  sample must be drawn, sized, or defended.
* **No adversarial or misleading evidence.** No file is designed to make a wrong answer look
  right. Cases where a control owner supplies favourable-but-irrelevant evidence are
  precisely where an audit tool is most needed and are entirely absent.
* **No multi-control dependency.** Each case is one control against one evidence set. Real
  ITGC conclusions depend on other controls (a change-approval finding is different if
  segregation of duties has also failed) and nothing here reasons across controls.
* **No ambiguity.** Every planted condition has exactly one defensible answer. Real evidence
  frequently supports two, and choosing between them is the professional judgement this
  system explicitly does not make.
* **Round numbers only.** Populations are exactly 100; rates are 5% and 10%. Nothing probes an
  arguably immaterial rate, a population too small to conclude from, or a boundary case.
* **n = 6.** Four distinct controls, three status classes with one instance each. Per-class
  precision and recall move by whole steps with a single row.

### The conflict of interest, stated plainly

The datasets and the system share an author. The datasets were written knowing how the
pipeline works, and that is a genuine threat to the validity of every accuracy figure derived
from them. The mitigations are partial and are listed so a reader can weigh them: ground truth
is declared *before* generation and mechanically re-derived from the written bytes; the
DATASET-003 false-positive trap was retained rather than removed; DATASET-006 was added
specifically to punish the failure mode the system is most likely to have; and the marker
strings are written into the documents by the generator so the retrieval metric cannot be
gamed by an approximate match. **None of that is equivalent to an independent test set**, and
no claim in this project should be read as though it were.
