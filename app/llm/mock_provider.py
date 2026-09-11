"""Deterministic, offline stand-in for a language model.

WHAT THIS IS - AND WHAT IT IS NOT
---------------------------------
``MockLLMProvider`` contains **no language model**. It is an ordinary rule-based
program: it reads the retrieved evidence text it is handed, applies a fixed set of
auditing heuristics, and emits the same JSON shape a real model would be asked to
produce. It never touches the network.

The consequence matters for the research write-up and must be stated wherever these
numbers appear: **results produced with this provider measure the surrounding pipeline
- retrieval, citation validation, the multi-step workflow, the human-review gate - and
say nothing whatsoever about the capability of any language model.** Accuracy figures
obtained offline are a property of the rules below, not of an LLM. To measure model
capability the same harness must be re-run against a real provider
(``app.llm.openai_provider``).

Why it still has to be good
---------------------------
The system must be demonstrable and evaluable with zero API keys. That means the mock
has to do real work: parse a table summary, count exceptions, compare a configured
value against a policy threshold, notice when the attribute under test is simply not
present in the evidence, and cite chunks that actually exist with text copied verbatim
out of them. A stub that returned canned strings would make every downstream metric
meaningless.

Design decisions worth knowing
------------------------------
* **Citations are constructed by slicing, never by writing.** ``quoted_text`` is always
  ``chunk_text[i:j]`` (optionally whitespace-stripped), so it is a genuine substring of
  the chunk it cites and the validator in :mod:`app.audit.validators` can confirm it.
  Nothing is ever paraphrased into a quotation.
* **Derived numbers go in ``inferences``, not in the narrative.** A percentage computed
  from two counts is not something the evidence states, so it is reported as an
  inference. Narrative fields only ever quote integers that literally occur in the
  retrieved text. This is both intellectually correct and what keeps the honest path
  clean under :func:`app.audit.validators.detect_unsupported_claims`.
* **INSUFFICIENT_EVIDENCE is a first-class outcome.** If the attribute under test does
  not appear in the *operational* evidence at all - a privileged-user listing with no
  MFA column - the provider refuses to conclude either way. This is the single
  behaviour the project exists to demonstrate, so it is checked before any other rule.
* **An expected artefact is judged on what makes it that artefact, not on wording.** A
  control's ``expected_evidence`` entries are sentences, and no supplied file contains
  the sentence describing it. So an entry naming a field ("... MFA enrolment status
  (MFA_Status)") is decided by whether the evidence reports that field, and an entry
  naming a policy is decided by whether a policy document about the attribute was
  retrieved. See :func:`_match_expected_artefacts`; the same rule is what keeps a
  listing *with* an MFA column from being reported missing and a listing *without* one
  from being reported present.
* **Sufficiency reports testability, not wish-list completeness.** ``SUFFICIENT`` means
  the control could be tested over the whole population the evidence reports. Expected
  artefacts that were never supplied are listed under ``missing_evidence`` for the
  auditor to request; they do not by themselves reduce the rating. See
  :func:`_sufficiency_rating`.

Experiment A (no retrieval context) is deliberately weaker
----------------------------------------------------------
The Experiment A baseline path is taken when
:class:`~app.llm.base.LLMCallContext` carries no chunks *and* its extras do not say
retrieval was performed - the engine states that explicitly, because "this mode
retrieves nothing" and "retrieval ran and found nothing" look identical from an empty
chunk list and warrant opposite behaviour. On that path the provider sees one
undifferentiated, possibly truncated blob of raw text with no chunk IDs, no source types
and no evidence-type labels. Two rules that modes B and C rely on therefore cannot fire:

1. It cannot separate a *policy* document from a *system export*, so it cannot ask
   "is the attribute under test present in the operational evidence?" - it can only ask
   "does this word appear anywhere in the blob?".
2. It has no table summary and so no population denominator; it counts only the
   exception rows that are actually visible inside the truncated text.

Where a precondition cannot be evaluated for lack of information, mode A treats it as
satisfied and modes B/C treat it as unmet. That asymmetry is stated here rather than
buried: it is a modelling choice, and it is the choice that reproduces the failure mode
this research is about - a confident conclusion drawn from evidence that never
addressed the question. Nothing in the code special-cases a dataset or forces mode A to
be wrong; A is given exactly the information the baseline prompt contains and the same
rules are applied to it.

The hallucination lever
-----------------------
``settings.mock_hallucination_rate`` injects, at that rate and seeded deterministically
from ``settings.mock_seed`` plus the control reference, either a citation pointing at a
chunk ID that was never supplied, a quotation that does not occur in the chunk it
claims, or an unsupported numeric claim. It exists so the validation layer can be
exercised and measured offline. Note when reporting: the self-critique step of
Experiment C detects these by *exact substring checking*, which is a mechanical
advantage of the workflow, not evidence that a model would notice its own invention.
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from app.config import Settings, get_settings
from app.llm.base import (
    LLMCallContext,
    LLMProvider,
    LLMResponse,
    estimate_tokens,
    extract_json,
)
from app.schemas.assessment import NO_EVIDENCE_SENTINEL
from app.schemas.enums import (
    AssessmentStatus,
    ConfidenceLevel,
    EvidenceSufficiency,
    RiskLevel,
)

#: Bumped whenever the rules below change, so a stored assessment can be traced back to
#: the exact rule set that produced it. 1.1: expected-evidence matching now resolves a
#: named field against the fields the evidence actually reports, and a policy artefact
#: against the retrieved requirement documents, instead of matching a whole sentence;
#: the population column is chosen by whether its values can be read as compliant or
#: exceptional; row-level exceptions are read from the attribute column rather than from
#: anywhere in the row; the sufficiency rating reports testability rather than wish-list
#: completeness; and an empty chunk list is no longer read as "the evidence is in the
#: prompt" when the caller says retrieval was performed.
MOCK_RULES_VERSION = "mock-rules-1.1"
MOCK_MODEL_NAME = "mock-deterministic-rules"

MOCK_DISCLAIMER = (
    "Produced by the offline deterministic rule-based provider, not by a language model. "
    "Results demonstrate the pipeline (retrieval, citation validation, workflow), not LLM capability."
)

_STANDING_LIMITATION = (
    "This assessment is based solely on the documents supplied. It cannot establish that the "
    "population is complete, that the extracts are authentic or unaltered, or that the control "
    "operated throughout the audit period. It is not a compliance opinion."
)


# ---- text and number helpers


def _has_word(haystack: str, needle: str) -> bool:
    """Whole-word containment. Keeps 'approved' from matching inside 'unapproved'."""
    if not needle:
        return False
    return re.search(r"(?<![A-Za-z0-9])" + re.escape(needle) + r"(?![A-Za-z0-9])", haystack, re.IGNORECASE) is not None


def _has_word_start(haystack: str, needle: str) -> bool:
    """Containment anchored to a word *start*: 'vulnerab' matches 'vulnerabilities'.

    Several attribute profiles carry deliberately truncated stems ('vulnerab',
    'encrypt', 'recertif') so that one term covers a family of inflections. A bare
    substring test would also let a three-letter stem such as 'otp' match the middle of
    an unrelated word and report the attribute under test as present when it is not -
    the precise error this provider exists to avoid - so the match is anchored at the
    left boundary and left open at the right.
    """
    if not needle:
        return False
    return re.search(r"(?<![A-Za-z0-9])" + re.escape(needle), haystack, re.IGNORECASE) is not None


_WORD_NUMBERS: Dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "hundred": 100,
}


def _parse_number_token(token: str) -> Optional[int]:
    """Turn '14', 'fourteen' or 'twenty-one' into an int; anything else into None.

    Policies are written in words at least as often as in digits ('at least fourteen
    characters'), and a threshold comparison that only understands digits silently
    fails to notice the requirement.
    """
    if not token:
        return None
    cleaned = token.strip().strip(".,;:()[]%").lower()
    if not cleaned:
        return None
    if cleaned.isdigit():
        try:
            return int(cleaned)
        except ValueError:
            return None
    if cleaned in _WORD_NUMBERS:
        return _WORD_NUMBERS[cleaned]
    if "-" in cleaned:
        parts = cleaned.split("-")
        if len(parts) == 2 and parts[0] in _WORD_NUMBERS and parts[1] in _WORD_NUMBERS:
            tens, units = _WORD_NUMBERS[parts[0]], _WORD_NUMBERS[parts[1]]
            if tens >= 20 and units < 10:
                return tens + units
    return None


_FILLER_BEFORE_NUMBER = {
    "is", "are", "of", "to", "at", "be", "must", "shall", "should", "set", "least",
    "no", "less", "more", "than", "minimum", "maximum", "min", "max", "configured",
    "value", "equal", "a", "an", "the", "characters", "days", "attempts", "=", "or",
    "greater", "fewer", "length", "requires", "required", "enforced", "policy",
}


def _first_number_after(text: str, pos: int, window: int = 110) -> Optional[Tuple[int, int, int]]:
    """First number-like token within ``window`` characters after ``pos``.

    Returns ``(value, start, end)`` in the coordinates of ``text`` so the caller can
    still quote verbatim. Scanning stops at a line break: a value on the next line
    belongs to the next setting, not this one.
    """
    segment = text[pos : pos + window]
    newline = segment.find("\n")
    if newline != -1:
        segment = segment[:newline]
    for match in re.finditer(r"[A-Za-z0-9][A-Za-z0-9\-\.]*", segment):
        token = match.group(0)
        value = _parse_number_token(token)
        if value is not None:
            return value, pos + match.start(), pos + match.end()
        if token.lower() not in _FILLER_BEFORE_NUMBER:
            # A content word that is not a number means the value did not follow the
            # key; stop rather than grabbing an unrelated figure further along.
            return None
    return None


_SENTENCE_ENDS = (". ", "! ", "? ")


def _quote_span(text: str, start: int, end: int, max_chars: int = 300) -> str:
    """Return a VERBATIM substring of ``text`` around ``[start, end)``.

    The result is produced only by slicing and whitespace-stripping, so it is always a
    genuine substring of the chunk. Nothing is ever appended - an ellipsis would make
    the quotation unverifiable, which is exactly the failure the validator hunts for.
    """
    if not text:
        return ""
    start = max(0, min(start, len(text)))
    end = max(start, min(end, len(text)))
    low = text.rfind("\n", 0, start) + 1
    high = text.find("\n", end)
    if high == -1:
        high = len(text)
    # Tighten to the sentence containing the match, so a policy paragraph is quoted at
    # the requirement itself rather than at everything that shares its line.
    sentence_low = low
    for terminator in _SENTENCE_ENDS:
        index = text.rfind(terminator, low, start)
        if index != -1:
            sentence_low = max(sentence_low, index + len(terminator))
    sentence_high = high
    for terminator in _SENTENCE_ENDS:
        index = text.find(terminator, end, high)
        if index != -1:
            sentence_high = min(sentence_high, index + 1)
    if sentence_low <= start and sentence_high >= end and sentence_high > sentence_low:
        low, high = sentence_low, sentence_high
    if high - low > max_chars:
        low2 = max(low, start - max_chars // 3)
        sentence = text.rfind(". ", low2, start)
        if sentence != -1 and sentence + 2 < start:
            low2 = sentence + 2
        high2 = min(high, low2 + max_chars)
        stop = text.find(". ", end, high2)
        if stop != -1:
            high2 = stop + 1
        else:
            space = text.rfind(" ", end, high2)
            high2 = space if space > end else min(high, end + 60)
        low, high = low2, max(high2, end)
    return text[low:high].strip()


def _digit_runs(text: str) -> Set[str]:
    return set(re.findall(r"\d+", text or ""))


# ---- normalised view of one supplied chunk


_REQUIREMENT_EVIDENCE_TYPES = {"POLICY", "STANDARD"}
_OBSERVATION_EVIDENCE_TYPES = {
    "CONFIGURATION_EXPORT", "SYSTEM_REPORT", "USER_LISTING", "TICKET_EXPORT", "LOG_EXTRACT",
}
_REQUIREMENT_FILENAME_HINTS = ("policy", "standard", "procedure", "guideline", "charter", "directive")
_OBSERVATION_FILENAME_HINTS = (
    "export", "extract", "report", "listing", "list", "users", "config", "settings", "gpo",
    "ticket", "log", "inventory", "register", "status", "scan", "compliance",
)
_NORMATIVE_MARKERS = (
    "must", "shall", "is required", "are required", "required to", "minimum of", "at least",
    "no later than", "policy requires", "mandatory", "may not", "must not", "no less than",
    "no more than", "is prohibited",
)


@dataclass
class _ChunkView:
    """Everything the rules need from one retrieved chunk, however it was handed over."""

    chunk_id: Optional[int]
    text: str
    filename: str = ""
    citation: str = ""
    source_type: str = "TEXT_BLOCK"
    evidence_type: str = ""
    column_names: List[str] = field(default_factory=list)
    row_start: Optional[int] = None

    @property
    def is_tabular(self) -> bool:
        return self.source_type in ("TABLE_ROWS", "TABLE_SUMMARY")

    @property
    def normative_score(self) -> float:
        lowered = self.text.lower()
        hits = sum(lowered.count(marker) for marker in _NORMATIVE_MARKERS)
        return 1000.0 * hits / max(1, len(lowered))

    @property
    def keyvalue_score(self) -> float:
        lines = [ln for ln in self.text.splitlines() if ln.strip()]
        if not lines:
            return 0.0
        pairs = sum(1 for ln in lines if re.match(r"^\s*[A-Za-z][A-Za-z0-9_\.\- ]{2,60}\s*[:=]\s*\S", ln))
        return pairs / float(len(lines))

    def classify(self) -> str:
        """'requirement' (states what must be true) or 'observation' (records what is).

        The distinction is what makes "the evidence never addressed the question"
        detectable: a policy that mentions MFA does not demonstrate MFA.
        """
        etype = (self.evidence_type or "").upper()
        if etype in _OBSERVATION_EVIDENCE_TYPES:
            return "observation"
        if etype in _REQUIREMENT_EVIDENCE_TYPES:
            return "requirement"
        name = (self.filename or "").lower()
        obs_score = 0.0
        req_score = 0.0
        if self.is_tabular:
            obs_score += 2.0
        if any(hint in name for hint in _OBSERVATION_FILENAME_HINTS):
            obs_score += 1.0
        if any(hint in name for hint in _REQUIREMENT_FILENAME_HINTS):
            req_score += 1.0
        obs_score += min(1.5, self.keyvalue_score * 1.5)
        req_score += min(2.0, self.normative_score)
        if obs_score == req_score:
            return "observation" if self.is_tabular else "requirement"
        return "observation" if obs_score > req_score else "requirement"


def _attr(source: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a dict or an object. Callers hand us either, and guessing
    wrong at runtime would take an audit run down for no good reason."""
    if isinstance(source, dict):
        return source.get(key, default)
    return getattr(source, key, default)


def _as_chunk_views(raw_chunks: Sequence[Any]) -> List[_ChunkView]:
    views: List[_ChunkView] = []
    for raw in raw_chunks or []:
        text = str(_attr(raw, "text", "") or "")
        if not text.strip():
            continue
        locator = _attr(raw, "locator", None)
        citation = str(_attr(raw, "citation", "") or "")
        columns: List[str] = []
        row_start: Optional[int] = None
        if isinstance(locator, dict):
            citation = citation or str(locator.get("rendered", "") or "")
            columns = [str(c) for c in (locator.get("column_names") or [])]
            row_start = locator.get("row_start")
        elif locator is not None and not isinstance(locator, str):
            render = getattr(locator, "render", None)
            if callable(render):
                citation = citation or str(render())
            columns = [str(c) for c in (getattr(locator, "column_names", None) or [])]
            row_start = getattr(locator, "row_start", None)
        elif isinstance(locator, str):
            citation = citation or locator
        chunk_id_raw = _attr(raw, "chunk_id", None)
        try:
            chunk_id = int(chunk_id_raw) if chunk_id_raw is not None else None
        except (TypeError, ValueError):
            chunk_id = None
        filename = str(_attr(raw, "filename", "") or "")
        views.append(
            _ChunkView(
                chunk_id=chunk_id,
                text=text,
                filename=filename,
                citation=citation or filename,
                source_type=str(_attr(raw, "source_type", "TEXT_BLOCK") or "TEXT_BLOCK").upper(),
                evidence_type=str(_attr(raw, "evidence_type", "") or ""),
                column_names=columns,
                row_start=row_start if isinstance(row_start, int) else None,
            )
        )
    return views


# ---- what is actually being tested


_GENERIC_POSITIVE = [
    "enabled", "yes", "true", "compliant", "approved", "current", "up to date", "patched",
    "active", "configured", "complete", "completed", "success", "successful", "passed",
    "registered", "enrolled", "enforced", "reviewed", "encrypted", "installed", "applied",
    "in place", "ok", "pass", "1",
]
_GENERIC_NEGATIVE = [
    "disabled", "no", "false", "non compliant", "noncompliant", "not compliant", "missing",
    "failed", "failure", "fail", "overdue", "outdated", "expired", "unapproved",
    "not approved", "rejected", "pending", "unauthorised", "unauthorized", "absent",
    "inactive", "none", "not enrolled", "not registered", "not configured", "not enabled",
    "incomplete", "never", "0",
]


@dataclass
class _AttributeProfile:
    """The attribute a control is actually tested against, and how to recognise it.

    A control's own wording is too generic to search evidence with: an MFA control
    mentions 'privileged accounts', and so does a user listing that says nothing about
    MFA. Each profile therefore carries the *distinguishing* terms that must appear in
    operational evidence before the control can be concluded on at all.
    """

    key: str
    label: str
    artefact: str
    control_terms: List[str]
    evidence_terms: List[str]
    column_terms: List[str]
    positive: List[str] = field(default_factory=list)
    negative: List[str] = field(default_factory=list)


_PROFILES: List[_AttributeProfile] = [
    _AttributeProfile(
        key="mfa",
        label="multi-factor authentication status",
        artefact=(
            "A per-account multi-factor authentication status or enrolment export covering the "
            "in-scope account population"
        ),
        control_terms=["mfa", "multi-factor", "multi factor", "multifactor", "two-factor", "two factor", "2fa", "second factor"],
        evidence_terms=["mfa", "multi-factor", "multi factor", "multifactor", "two-factor", "2fa", "authenticator", "second factor", "otp"],
        column_terms=["mfa", "2fa", "multifactor", "multi_factor", "authenticator", "token", "second_factor"],
        positive=["enabled", "enrolled", "registered"],
        negative=["disabled", "not enrolled", "not registered"],
    ),
    _AttributeProfile(
        key="patching",
        label="patch compliance status",
        artefact="A patch or vulnerability compliance report covering every in-scope endpoint",
        control_terms=["patch", "patching", "vulnerability", "security update", "hotfix", "remediation"],
        evidence_terms=["patch", "hotfix", "kb", "vulnerab", "update"],
        column_terms=["patch", "vulnerab", "update", "compliance", "compliant", "kb", "remediation"],
        positive=["compliant", "patched", "up to date", "current", "installed"],
        negative=["missing", "missing critical patch", "non compliant", "overdue", "outdated", "not installed"],
    ),
    _AttributeProfile(
        key="password_policy",
        label="password policy configuration",
        artefact="A password policy configuration export taken from the authentication system in scope",
        control_terms=["password", "passphrase", "credential", "complexity", "lockout", "password length"],
        evidence_terms=["password", "passphrase", "lockout", "complexity", "length", "expiry", "expiration"],
        column_terms=["password", "complexity", "length", "lockout", "expiry", "age"],
        positive=["enforced", "compliant", "enabled"],
        negative=["not enforced", "non compliant", "disabled"],
    ),
    _AttributeProfile(
        key="change_management",
        label="change approval status",
        artefact=(
            "A change ticket export showing the documented approval (approver and date) for every "
            "production change in the audit period"
        ),
        control_terms=["change", "cab", "release", "deployment", "rfc", "change management", "authoris", "authoriz"],
        evidence_terms=["change", "ticket", "approval", "approver", "cab", "rfc", "release"],
        column_terms=["approval", "approved", "approver", "authoris", "authoriz", "cab", "sign_off", "signoff", "sign off"],
        positive=["approved", "authorised", "authorized"],
        negative=["unapproved", "not approved", "rejected", "no approval", "missing approval"],
    ),
    _AttributeProfile(
        key="access_review",
        label="periodic access review completion",
        artefact="Evidence of the completed periodic access review: reviewer, date and outcome per account",
        control_terms=["access review", "recertification", "recertify", "entitlement review", "attestation", "user access review"],
        evidence_terms=["review", "recertif", "attest", "sign-off", "signoff", "reviewer"],
        column_terms=["review", "reviewed", "recertif", "attest", "reviewer", "certification"],
        positive=["reviewed", "completed", "attested", "certified"],
        negative=["not reviewed", "outstanding", "overdue", "pending"],
    ),
    _AttributeProfile(
        key="logging",
        label="audit logging configuration",
        artefact="A logging or SIEM forwarding configuration export for every in-scope system",
        control_terms=["logging", "audit log", "siem", "event log", "monitoring", "log retention"],
        evidence_terms=["log", "siem", "event", "monitor", "forward"],
        column_terms=["log", "logging", "siem", "monitor", "forwarding", "audit"],
        positive=["enabled", "forwarding", "active"],
        negative=["disabled", "not forwarding", "not enabled"],
    ),
    _AttributeProfile(
        key="backup",
        label="backup and recovery status",
        artefact="A backup job report covering the audit period, with evidence of a restore test",
        control_terms=["backup", "restore", "recovery", "rpo", "rto", "resilience"],
        evidence_terms=["backup", "restore", "recovery", "job"],
        column_terms=["backup", "restore", "recovery", "job", "result"],
        positive=["success", "successful", "completed", "restored"],
        negative=["failed", "failure", "missed", "incomplete", "not run"],
    ),
    _AttributeProfile(
        key="encryption",
        label="encryption status",
        artefact="An encryption status export for every in-scope device or data store",
        control_terms=["encrypt", "encryption", "tls", "cipher", "at rest", "in transit", "bitlocker"],
        evidence_terms=["encrypt", "tls", "cipher", "bitlocker", "aes"],
        column_terms=["encrypt", "encryption", "tls", "cipher", "protection"],
        positive=["encrypted", "enabled", "aes", "protected"],
        negative=["not encrypted", "unencrypted", "disabled", "plaintext"],
    ),
    _AttributeProfile(
        key="termination",
        label="leaver account disablement",
        artefact="An HR leaver listing reconciled to the account disablement date for each leaver",
        control_terms=["termination", "terminated", "leaver", "offboard", "deprovision", "joiner"],
        evidence_terms=["termination", "leaver", "offboard", "deprovision", "disabled", "revoked"],
        column_terms=["disabled", "termination", "revoked", "deprovision", "offboard", "status"],
        positive=["disabled", "revoked", "removed"],
        negative=["active", "still active", "enabled", "not disabled"],
    ),
]

#: Words too common in control text to distinguish one control's evidence from another's.
_TERM_STOPWORDS = {
    "control", "controls", "system", "systems", "user", "users", "account", "accounts",
    "access", "review", "reviewed", "management", "policy", "policies", "process", "procedure",
    "ensure", "ensures", "must", "all", "the", "and", "for", "with", "that", "this", "are",
    "is", "of", "to", "in", "on", "by", "from", "data", "information", "security", "audit",
    "evidence", "record", "records", "report", "reports", "status", "list", "listing",
    "privileged", "production", "annual", "periodic", "quarterly", "approved", "appropriate",
}


def _control_text(control: Any) -> str:
    if control is None:
        return ""
    parts: List[str] = []
    for key in ("control_id", "name", "objective", "description", "risk_addressed", "category"):
        value = _attr(control, key, "")
        if value:
            parts.append(str(value))
    for key in ("assessment_criteria", "expected_evidence", "retrieval_keywords", "framework_refs"):
        value = _attr(control, key, None) or []
        if isinstance(value, (list, tuple)):
            parts.extend(str(v) for v in value)
        elif value:
            parts.append(str(value))
    return "\n".join(parts)


def _generic_profile(control_text: str) -> _AttributeProfile:
    """Fallback profile for a control outside the built-in registry.

    Distinguishing terms are the control's own uncommon words. This is weaker than a
    hand-written profile and the assessment says so in its limitations.
    """
    tokens = [t for t in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", control_text.lower()) if t not in _TERM_STOPWORDS]
    seen: List[str] = []
    for token in tokens:
        if token not in seen:
            seen.append(token)
    terms = seen[:8] or ["control"]
    return _AttributeProfile(
        key="generic",
        label="the attribute this control is tested against",
        artefact="A system-generated record showing, for the complete population, whether the control operated",
        control_terms=terms,
        evidence_terms=terms,
        column_terms=terms,
    )


def _select_profile(control_text: str) -> _AttributeProfile:
    lowered = control_text.lower()
    best: Optional[_AttributeProfile] = None
    best_score = 0
    for profile in _PROFILES:
        score = sum(1 for term in profile.control_terms if term in lowered)
        if score > best_score:
            best, best_score = profile, score
    if best is None or best_score == 0:
        return _generic_profile(control_text)
    return best


def _classify_value(value: str, profile: _AttributeProfile) -> str:
    """'positive', 'negative' or 'unknown' for one cell value of the attribute column."""
    text = re.sub(r"[_\-/]+", " ", str(value)).strip().lower()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return "unknown"
    negative = list(profile.negative) + _GENERIC_NEGATIVE
    positive = list(profile.positive) + _GENERIC_POSITIVE
    if text in negative:
        return "negative"
    if text in positive:
        return "positive"
    negated = re.match(r"^(?:not|non|un|no)[ ]?(.+)$", text)
    if negated and any(negated.group(1) == p or _has_word(negated.group(1), p) for p in positive):
        return "negative"
    for term in sorted(negative, key=len, reverse=True):
        if len(term) >= 3 and _has_word(text, term):
            return "negative"
    for term in sorted(positive, key=len, reverse=True):
        if len(term) >= 3 and _has_word(text, term):
            return "positive"
    return "unknown"


def _column_tokens(name: str) -> List[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(name))
    return [t for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t]


def _column_matches(name: str, profile: _AttributeProfile) -> int:
    tokens = _column_tokens(name)
    if not tokens:
        return 0
    score = 0
    for term in profile.column_terms:
        term_norm = term.lower().replace(" ", "_")
        for part in term_norm.split("_"):
            if not part:
                continue
            if part in tokens:
                score = max(score, 2)
            elif any(part in token or token in part for token in tokens if len(token) > 3):
                score = max(score, 1)
    return score


# ---- table summary parsing


#: Column names that report *how many* defects a record carries rather than whether it
#: complies. For these the numeric reading inverts: 0 is the compliant value and any
#: positive count is an exception - the opposite of the boolean 0/1 reading used for a
#: flag column. Without this, a 'Critical_Patches_Missing' summary reading '0 = 95 |
#: 3 = 5' would be counted as 95 exceptions, because '0' is a generic negative marker.
_DEFECT_COUNT_COLUMN_TOKENS = {
    "missing", "outstanding", "overdue", "failed", "failures", "exception", "exceptions",
    "violation", "violations", "breach", "breaches", "unpatched", "late", "open", "gaps",
    "defects", "findings", "errors",
}


def _is_defect_count_column(name: str) -> bool:
    return any(token in _DEFECT_COUNT_COLUMN_TOKENS for token in _column_tokens(name))


@dataclass
class _Population:
    """A counted population read out of a TABLE_SUMMARY chunk."""

    chunk: _ChunkView
    column: str
    counts: Dict[str, int]
    total: Optional[int]
    counts_span: Tuple[int, int]
    columns_present: List[str] = field(default_factory=list)

    def split(self, profile: _AttributeProfile) -> Tuple[int, int, int, List[str]]:
        """Partition the value counts into compliant / exception / unclassifiable.

        The labels are the *values* the column takes; the counts are how many records
        take each. A defect-count column is read numerically (0 compliant, >0 an
        exception) because its values are quantities, not verdicts.
        """
        compliant = exceptions = unknown = 0
        exception_labels: List[str] = []
        numeric_column = _is_defect_count_column(self.column)
        for label, count in self.counts.items():
            verdict = _classify_value(label, profile)
            if numeric_column:
                quantity = _parse_number_token(label)
                if quantity is not None:
                    verdict = "negative" if quantity > 0 else "positive"
            if verdict == "negative":
                exceptions += count
                exception_labels.append(label)
            elif verdict == "positive":
                compliant += count
            else:
                unknown += count
        return compliant, exceptions, unknown, exception_labels


_ROW_COUNT_PATTERNS = (
    r"(\d+)\s+(?:data\s+)?rows?\b",
    r"\brows?\s*[:=]\s*(\d+)",
    r"\brow[_\s]?count\s*[:=]\s*(\d+)",
    r"\brecords?\s*[:=]\s*(\d+)",
    r"\bpopulation\s*[:=]\s*(\d+)",
    r"\bshape\s*[:=]\s*\(?\s*(\d+)",
    r"\btotal\s+(?:rows|records|accounts|entries)\s*[:=]?\s*(\d+)",
)


def _parse_row_total(text: str) -> Optional[int]:
    for pattern in _ROW_COUNT_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                continue
    return None


def _parse_value_counts(line: str) -> Dict[str, int]:
    """Read 'Enabled=90; Disabled=10', 'Enabled: 90, Disabled: 10' or 'Enabled 90, Disabled 10'.

    The exact rendering is owned by the evidence parser, so this accepts the three
    obvious forms rather than depending on one of them.
    """
    counts: Dict[str, int] = {}
    body = line.strip().strip("{}")
    pairs = re.findall(r"[\"']?([A-Za-z0-9][A-Za-z0-9 _\-/\.\+&']*?)[\"']?\s*[:=]\s*\(?(\d+)\)?", body)
    if not pairs:
        pairs = re.findall(r"([A-Za-z][A-Za-z0-9 _\-/\.\+&']*?)\s+\((\d+)\)", body)
    if not pairs:
        pairs = re.findall(r"([A-Za-z][A-Za-z0-9 _\-/\.\+&']*?)\s+(\d+)\s*(?=[;,]|$)", body)
    for label, count in pairs:
        label_clean = label.strip().strip("-*• ")
        if not label_clean:
            continue
        try:
            counts[label_clean] = int(count)
        except ValueError:
            continue
    return counts


#: A rendered column list inside chunk text, e.g. "COLUMNS: a | b | c" or
#: "columns (6): a, b, c". Shared by the population parser and the column-list citation
#: so the two can never disagree about what counts as a column line.
_COLUMNS_LINE_RE = re.compile(
    r"^[ \t\-\*•]*columns?\s*(?:\(\d+\))?\s*[:=]\s*(.+)$", re.IGNORECASE | re.MULTILINE
)

#: Column lists are written with either separator depending on the emitting parser.
_COLUMN_SEPARATOR_RE = re.compile(r"[,|]")


def _split_column_list(text: str) -> List[str]:
    return [part.strip().strip("'\"") for part in _COLUMN_SEPARATOR_RE.split(text) if part.strip()]


def _parse_populations(chunk: _ChunkView, profile: _AttributeProfile) -> List[_Population]:
    """Pull per-column value counts out of a table-summary chunk."""
    populations: List[_Population] = []
    total = _parse_row_total(chunk.text)
    columns_present = list(chunk.column_names)
    for match in _COLUMNS_LINE_RE.finditer(chunk.text):
        listed = _split_column_list(match.group(1))
        if len(listed) > len(columns_present):
            columns_present = listed
    for line_match in re.finditer(r"^(.*)$", chunk.text, re.MULTILINE):
        line = line_match.group(1)
        if not line.strip() or ":" not in line and "=" not in line:
            continue
        header = re.match(r"^[ \t\-\*•]*([A-Za-z][A-Za-z0-9 _\.\-]{0,60}?)\s*[:=]\s*(.+)$", line)
        if not header:
            continue
        column, remainder = header.group(1).strip(), header.group(2)
        if column.lower() in {"columns", "column", "rows", "row", "shape", "sheet", "file", "records", "population", "total"}:
            continue
        counts = _parse_value_counts(remainder)
        if len(counts) < 2 and not (len(counts) == 1 and _column_matches(column, profile)):
            continue
        offset = line_match.start(1) + header.start(2)
        populations.append(
            _Population(
                chunk=chunk,
                column=column,
                counts=counts,
                total=total,
                counts_span=(line_match.start(1), line_match.start(1) + len(line)),
                columns_present=columns_present,
            )
        )
    if not populations and columns_present:
        # A summary that lists its columns but no value counts still tells us which
        # attributes the export does and does not report - that is the DATASET-005 signal.
        populations.append(
            _Population(
                chunk=chunk,
                column="",
                counts={},
                total=total,
                counts_span=(0, min(len(chunk.text), 200)),
                columns_present=columns_present,
            )
        )
    return populations


# ---- configuration value vs policy threshold


@dataclass
class _SettingFamily:
    name: str
    pattern: str
    direction: str  # "min": configured value must be >= requirement; "max": <=
    unit: str


_SETTING_FAMILIES: List[_SettingFamily] = [
    _SettingFamily(
        "minimum password length",
        r"min(?:imum)?[\s_\-]*(?:password|passphrase|pwd)[\s_\-]*(?:length|len)"
        r"|(?:password|passphrase)[\s_\-]*(?:min(?:imum)?[\s_\-]*)?length"
        r"|length[\s_\-]*of[\s_\-]*(?:the[\s_\-]*)?(?:password|passphrase)",
        "min",
        "characters",
    ),
    _SettingFamily("maximum password age", r"max(?:imum)?[\s_\-]*(?:password|pwd)[\s_\-]*age|password[\s_\-]*expir\w*", "max", "days"),
    _SettingFamily("minimum password age", r"min(?:imum)?[\s_\-]*(?:password|pwd)[\s_\-]*age", "min", "days"),
    _SettingFamily("password history", r"(?:enforce[\s_\-]*)?password[\s_\-]*history|passwords?[\s_\-]*remembered", "min", "passwords"),
    _SettingFamily(
        "account lockout threshold",
        r"(?:account[\s_\-]*)?lockout[\s_\-]*threshold|invalid[\s_\-]*log(?:on|in)[\s_\-]*attempts|failed[\s_\-]*log(?:on|in)[\s_\-]*attempts",
        "max",
        "attempts",
    ),
    _SettingFamily("lockout duration", r"lockout[\s_\-]*duration", "min", "minutes"),
    _SettingFamily("session inactivity timeout", r"(?:session|screen|idle|inactivity)[\s_\-]*(?:lock|timeout)", "max", "minutes"),
    _SettingFamily(
        "critical patch remediation window",
        r"critical[\s\w]{0,24}patch\w*[\s\w]{0,24}within|patch\w*[\s\w]{0,18}within|remediation[\s_\-]*window|remediated[\s_\-]*within",
        "max",
        "days",
    ),
]


@dataclass
class _SettingReading:
    family: _SettingFamily
    value: int
    chunk: _ChunkView
    key_span: Tuple[int, int]
    value_span: Tuple[int, int]
    classification: str

    @property
    def quote(self) -> str:
        return _quote_span(self.chunk.text, self.key_span[0], self.value_span[1])


def _read_settings(chunks: Sequence[_ChunkView]) -> List[_SettingReading]:
    readings: List[_SettingReading] = []
    for chunk in chunks:
        classification = chunk.classify()
        for family in _SETTING_FAMILIES:
            for match in re.finditer(family.pattern, chunk.text, re.IGNORECASE):
                found = _first_number_after(chunk.text, match.end())
                if found is None:
                    continue
                value, vstart, vend = found
                readings.append(
                    _SettingReading(
                        family=family,
                        value=value,
                        chunk=chunk,
                        key_span=(match.start(), match.end()),
                        value_span=(vstart, vend),
                        classification=classification,
                    )
                )
    return readings


@dataclass
class _SettingConflict:
    family: _SettingFamily
    required: int
    observed: int
    required_reading: Optional[_SettingReading]
    observed_reading: _SettingReading
    requirement_source: str


def _requirement_value_from_control(control_text: str, family: _SettingFamily) -> Optional[Tuple[int, int, int]]:
    for match in re.finditer(family.pattern, control_text, re.IGNORECASE):
        found = _first_number_after(control_text, match.end())
        if found is not None:
            return found
    return None


def _find_setting_conflicts(
    readings: Sequence[_SettingReading],
    control_text: str,
) -> List[_SettingConflict]:
    """A configured value that directly contradicts a stated threshold.

    The requirement side is taken from a chunk classified as a requirement where one
    exists; otherwise, from the control definition itself, in which case the assessment
    says so rather than pretending a document was cited.
    """
    conflicts: List[_SettingConflict] = []
    by_family: Dict[str, List[_SettingReading]] = {}
    for reading in readings:
        by_family.setdefault(reading.family.name, []).append(reading)

    for family_name, group in sorted(by_family.items()):
        family = group[0].family
        requirements = [r for r in group if r.classification == "requirement"]
        observations = [r for r in group if r.classification == "observation"]
        if not observations:
            continue
        if requirements:
            required_reading: Optional[_SettingReading] = max(
                requirements, key=lambda r: (r.chunk.normative_score, -(r.chunk.chunk_id or 0))
            )
            required_value = required_reading.value
            source = "policy document"
        else:
            from_control = _requirement_value_from_control(control_text, family)
            if from_control is None:
                continue
            required_reading, required_value, source = None, from_control[0], "control definition"
        for observation in observations:
            if required_reading is not None and observation is required_reading:
                continue
            violates = (
                observation.value < required_value if family.direction == "min" else observation.value > required_value
            )
            if violates:
                conflicts.append(
                    _SettingConflict(
                        family=family,
                        required=required_value,
                        observed=observation.value,
                        required_reading=required_reading,
                        observed_reading=observation,
                        requirement_source=source,
                    )
                )
                break
    return conflicts


# ---- row-level exceptions


@dataclass
class _RowException:
    chunk: _ChunkView
    line: str
    span: Tuple[int, int]
    row_number: Optional[int]


def _distinctive_negatives(profile: _AttributeProfile) -> List[str]:
    """Negative markers safe to search for in free-running row text.

    Bare 'no' and '0' are excluded: they appear inside 'Account_No' and every date, and
    a false exception is worse than a missed one when the output is an audit finding.
    """
    terms = list(profile.negative) + _GENERIC_NEGATIVE
    return sorted({t for t in terms if len(t) >= 5 or " " in t}, key=len, reverse=True)


#: One rendered record inside a TABLE_ROWS chunk: "row 11: a | b | c".
_TABLE_ROW_RE = re.compile(r"^\s*row\s+(\d+)\s*:\s*(.+)$", re.IGNORECASE)

#: Cell separator used by the tabular parser when it renders a record.
_CELL_SEPARATOR = "|"


def _row_columns(chunk: _ChunkView) -> List[str]:
    """The column list this chunk's rendered rows are laid out against."""
    if chunk.column_names:
        return list(chunk.column_names)
    match = _COLUMNS_LINE_RE.search(chunk.text)
    return _split_column_list(match.group(1)) if match else []


def _find_row_exceptions(
    chunks: Sequence[_ChunkView],
    profile: _AttributeProfile,
    attribute_column: str = "",
    limit: int = 12,
) -> List[_RowException]:
    """Individual records that fail the requirement, for citation as exceptions.

    Where the attribute under test has been located in a named column - which is the
    normal case once a table summary has been read - only that column's cell is
    classified. Scanning the whole record instead, as this did before, cites the wrong
    rows whenever a *different* column happens to carry a negative-sounding value: a
    privileged-account listing with both ``MFA_Status = Enabled`` and ``Last_Login =
    Never`` on the same row would otherwise be cited as an MFA exception, which is a
    fabricated finding about a compliant account.

    The whole-record scan remains the fallback for chunks that are not rendered rows, or
    where the column could not be located, because for free-text evidence it is the only
    thing available. The quoted span is the whole record either way: an auditor reading
    a cited exception needs the identifier next to the failing value.
    """
    markers = _distinctive_negatives(profile)
    found: List[_RowException] = []
    for chunk in chunks:
        if chunk.source_type == "TABLE_SUMMARY":
            continue
        columns = _row_columns(chunk)
        target = -1
        if attribute_column:
            wanted = _column_tokens(attribute_column)
            for index, name in enumerate(columns):
                if _column_tokens(name) == wanted:
                    target = index
                    break
        offset = 0
        line_index = 0
        for line in chunk.text.splitlines(True):
            stripped = line.rstrip("\n")
            if stripped.strip():
                if _is_exception_line(stripped, markers, target):
                    row_number = _row_number_for(stripped, chunk, line_index)
                    found.append(
                        _RowException(
                            chunk=chunk,
                            line=stripped.strip(),
                            span=(offset, offset + len(stripped)),
                            row_number=row_number,
                        )
                    )
                line_index += 1
            offset += len(line)
            if len(found) >= limit:
                return found
    return found


def _is_exception_line(line: str, markers: Sequence[str], target: int) -> bool:
    """True when this rendered line records a failure of the requirement."""
    if target >= 0:
        match = _TABLE_ROW_RE.match(line)
        if match is None:
            # In a rendered-rows chunk with a located attribute column, anything that is
            # not a record is chunk metadata (the file header, the column list) and
            # cannot be an exception.
            return False
        cells = [cell.strip() for cell in match.group(2).split(_CELL_SEPARATOR)]
        if target < len(cells):
            # The attribute's own cell is decisive: a value that is not a recognised
            # failure means this record is not an exception, whatever else the row says.
            return any(_has_word(cells[target], marker) for marker in markers)
    return any(_has_word(line, marker) for marker in markers)


def _row_number_for(line: str, chunk: _ChunkView, line_index: int) -> Optional[int]:
    match = re.match(r"^\s*(?:row\s*)?(\d{1,6})\s*[\|:,\t]", line, re.IGNORECASE)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    if chunk.row_start is not None:
        return chunk.row_start + line_index
    return None


# ---- the analysis that drives every payload


@dataclass
class _Analysis:
    profile: _AttributeProfile
    chunks: List[_ChunkView]
    requirement_chunks: List[_ChunkView]
    observation_chunks: List[_ChunkView]
    populations: List[_Population]
    population: Optional[_Population]
    compliant: int
    exceptions: int
    unknown: int
    exception_labels: List[str]
    total: Optional[int]
    row_exceptions: List[_RowException]
    conflicts: List[_SettingConflict]
    attribute_in_observation: bool
    attribute_anywhere: bool
    columns_present: List[str]
    missing_artefacts: List[str]
    matched_artefacts: List[str]
    evidence_numbers: Set[str]
    raw_mode: bool
    status: AssessmentStatus = AssessmentStatus.INSUFFICIENT_EVIDENCE
    reason_code: str = "no_evidence"

    def can_state(self, number: Optional[int]) -> bool:
        """True when a figure literally occurs in the supplied evidence.

        Anything else is a derived quantity and belongs in ``inferences``, not in a
        sentence that reads like an observation.
        """
        return number is not None and str(number) in self.evidence_numbers


#: Words carried by almost every artefact description; they cannot distinguish one
#: expected artefact from another and are dropped before matching.
_ARTEFACT_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "each", "every", "all", "any", "per",
    "audit", "period", "evidence", "showing", "covering", "during", "within", "supplied",
    "provided", "relevant", "scope", "applicable", "such", "its", "their",
}

#: An artefact whose description names no specific field counts as supplied when this
#: share of its distinguishing words appears in one file's name, text or column list.
#: A share rather than a whole-phrase match, because expected-evidence entries are
#: written as sentences and a file cannot be expected to contain the sentence.
_ARTEFACT_MATCH_THRESHOLD = 0.6

#: A field the control names explicitly, written as an identifier: ``MFA_Status``,
#: ``Last_Login``, ``Critical_Patches_Missing``. Control authors write these when the
#: artefact is only useful if it carries that specific column, so where one is present
#: it is the decisive test - see :func:`_match_expected_artefacts`.
_NAMED_FIELD_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+)(?![A-Za-z0-9])")

#: Words that appear in half the column names in any export and so identify nothing on
#: their own. ``MFA_Status`` is recognised by ``mfa``; ``Status`` alone would match an
#: ``Account_Status`` column in a listing that says nothing about MFA.
_FIELD_GENERIC_TOKENS = {
    "status", "date", "name", "id", "type", "count", "flag", "value", "code", "number",
    "last", "first", "result", "level", "info", "detail", "details", "field", "column",
}


def _stem(token: str) -> str:
    if len(token) > 4 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def _artefact_tokens(text: str) -> Set[str]:
    return {
        _stem(t)
        for t in re.split(r"[^A-Za-z0-9]+", str(text).lower())
        if len(t) > 2 and t not in _ARTEFACT_STOPWORDS
    }


#: The nouns that say what *kind* of document an expected artefact is. Stemmed, because
#: an artefact description is as likely to say "records" as "record".
_REQUIREMENT_ARTEFACT_KINDS = {
    _stem(word)
    for word in ("policy", "policies", "standard", "standards", "procedure", "procedures",
                 "guideline", "guidelines", "charter", "directive")
}
_OBSERVATION_ARTEFACT_KINDS = {
    _stem(word)
    for word in ("export", "exports", "extract", "extracts", "listing", "listings", "list",
                 "report", "reports", "register", "log", "logs", "inventory", "ticket",
                 "tickets", "record", "records", "screenshot", "narrative", "dump",
                 "snapshot", "minutes", "cmdb", "configuration")
}


#: How many words of an artefact description are read when deciding what kind of
#: document it is. Expected-evidence entries are written head-noun first ("Exception
#: register listing approved MFA exemptions..."), and a kind word further along is
#: almost always a reference to another document rather than a statement about this one
#: - "Exception documentation ... configured below the policy value" is not a request
#: for a policy. Reading only the head is what keeps the two apart.
_ARTEFACT_KIND_WINDOW = 6


def _artefact_class(artefact: str) -> str:
    """'requirement', 'observation' or '' for an artefact description.

    An artefact that says it is a policy cannot be satisfied by a spreadsheet, and an
    artefact that says it is an export cannot be satisfied by a policy - however many
    words the two happen to share. Without this gate the change-management policy, which
    naturally discusses approvals, retrospective approvals and deployments, matched
    three separate operational artefacts that nobody had supplied.
    """
    head = {
        _stem(word)
        for word in re.split(r"[^A-Za-z0-9]+", str(artefact).lower())[:_ARTEFACT_KIND_WINDOW]
        if word
    }
    if head & _OBSERVATION_ARTEFACT_KINDS:
        # An entry naming both ("configuration export or screenshot narrative of the
        # policy rule") is asking for the operational record of the policy, not the
        # policy, so the operational reading wins.
        return "observation"
    if head & _REQUIREMENT_ARTEFACT_KINDS:
        return "requirement"
    return ""


def _named_fields(artefact: str) -> List[str]:
    """Identifier-shaped field names the artefact description requires by name."""
    return _dedupe(_NAMED_FIELD_RE.findall(str(artefact)))


def _field_present(field_name: str, reported_names: Sequence[str]) -> bool:
    """Does the evidence report a field called ``field_name``?

    ``reported_names`` are the field names one file actually reports - its column names,
    plus the identifier-shaped names appearing in its own text, which is how a
    configuration export written as ``Setting_Name, Configured_Value`` rows names its
    parameters.

    Matching is on the field's *distinguishing* tokens, so it is deliberately forgiving
    about wording and unforgiving about subject. A column called ``MFA_Enrolled`` or
    ``MFA`` satisfies a field written ``MFA_Status``, because the export reports the
    attribute even though it names it differently. A listing whose fields never mention
    the subject at all does not satisfy it, however plausibly the file is titled - which
    is the whole of DATASET-005.
    """
    wanted = [t for t in _column_tokens(field_name) if t not in _FIELD_GENERIC_TOKENS]
    if not wanted:
        # A field built only from generic words ('Last_Date') identifies nothing on its
        # own, so require the full token set rather than any one of them.
        required = set(_column_tokens(field_name))
        return any(required and required <= set(_column_tokens(name)) for name in reported_names)
    stems = {_stem(token) for token in wanted}
    for name in reported_names:
        if stems & {_stem(token) for token in _column_tokens(name)}:
            return True
    return False


def _file_columns(chunks: Sequence[_ChunkView]) -> Dict[str, List[str]]:
    """Column names per file, from the stored locators and from rendered column lists."""
    per_file: Dict[str, List[str]] = {}
    for chunk in chunks:
        key = chunk.filename or "(unnamed)"
        bucket = per_file.setdefault(key, [])
        for column in chunk.column_names:
            if column not in bucket:
                bucket.append(column)
        for match in _COLUMNS_LINE_RE.finditer(chunk.text):
            for column in _split_column_list(match.group(1)):
                if column not in bucket:
                    bucket.append(column)
    return per_file


#: How much of one chunk is scanned for identifier-shaped field names. A settings export
#: names its parameters in the first rows; scanning the whole of every chunk buys
#: nothing and makes a large evidence set quadratic in practice.
_FIELD_NAME_SCAN_CHARS = 8000


def _reported_field_names(chunks: Sequence[_ChunkView]) -> Dict[str, List[str]]:
    """Identifier-shaped names each *operational* file reports, per file.

    Restricted to observation chunks on purpose. A policy that happens to name the
    column an export ought to carry is stating a requirement, not reporting data, and
    letting it satisfy an expected artefact would reinstate exactly the confusion this
    provider exists to avoid: a policy that mentions MFA is not evidence of MFA.
    """
    per_file: Dict[str, List[str]] = {}
    for chunk in chunks:
        if chunk.classify() != "observation":
            continue
        key = chunk.filename or "(unnamed)"
        bucket = per_file.setdefault(key, [])
        for name in _NAMED_FIELD_RE.findall(chunk.text[:_FIELD_NAME_SCAN_CHARS]):
            if name not in bucket:
                bucket.append(name)
    return per_file


def _match_expected_artefacts(
    control: Any,
    chunks: Sequence[_ChunkView],
    profile: _AttributeProfile,
) -> Tuple[List[str], List[str]]:
    """Decide which of the control's expected artefacts were actually supplied.

    Three tests, and the order between them is the point of this function. Each asks
    what actually makes a document *that* artefact, rather than how many of the words in
    its description happen to occur somewhere.

    **1. The named-field test, where the control names a field.** An expected-evidence
    entry such as "Privileged account listing ... showing ... MFA enrolment status
    (MFA_Status)" is not a request for a listing; it is a request for a listing that
    carries that field. So where the description names identifier-shaped fields, the
    artefact is supplied when some operational file reports one of them, and missing
    when none does - and no other signal may overturn that. This is decisive in *both*
    directions on purpose:

    * a privileged-account listing that has an ``MFA_Status`` column satisfies the
      entry, even though the file will never contain the words "exported from the
      identity provider";
    * the same listing with no MFA column does not satisfy it, even though the file is
      obviously a privileged account listing.

    The second case is the behaviour this project exists to demonstrate; the first is
    the false "missing artefact" that a whole-sentence match produced instead.

    **2. The requirement-document test, for artefacts that are policies.** An entry
    asking for "an authentication or MFA policy stating which classes of account are
    required to use a second factor" is satisfied by a retrieved policy document about
    that attribute. Matching the sentence instead fails on wording alone: the supplied
    policy says "must be enrolled in the multi-factor authentication solution" and
    shares under half its words with the request, while asserting exactly what was
    asked for.

    **3. Token coverage within the right class of document, for everything else.** An
    entry that names neither a field nor a policy ("Exception register listing approved
    MFA exemptions with the approver...") is matched on the share of its distinguishing
    words appearing in one file's name, text or field names - but only against files of
    the class it asks for, and never pooled across files. Both restrictions matter: an
    artefact is one document, and without the class gate a change-management policy,
    which naturally discusses approvals and deployments, reads as a CAB minute book, a
    retrospective approval record and a deployment log that nobody supplied.

    Limits worth stating. A named field the evidence reports under a semantically
    different name (``Second_Factor_Registered`` for ``MFA_Status``) is not recognised.
    The requirement test asks only whether *a* policy about the attribute was retrieved,
    so a control expecting two distinct standards has both satisfied by one document.
    Test 3 remains a bag-of-words measure, and where an operational artefact is
    misclassified as a requirement document it is reported missing even when supplied.
    Every one of these errs toward reporting an artefact missing, which asks an auditor
    for something they may already hold; the opposite error silently narrows what the
    assessment was tested on, and is the worse of the two here.
    """
    expected = _attr(control, "expected_evidence", None) or []
    if not isinstance(expected, (list, tuple)) or not expected:
        return [], []

    corpora: Dict[str, List[str]] = {}
    classes: Dict[str, Set[str]] = {}
    for chunk in chunks:
        key = chunk.filename or "(unnamed)"
        corpora.setdefault(key, []).append(chunk.text[:4000])
        classes.setdefault(key, set()).add(chunk.classify())
    if not corpora:
        return [], [str(item) for item in expected]

    columns_by_file = _file_columns(chunks)
    reported_by_file = _reported_field_names(chunks)
    fields_by_file = {
        name: list(columns_by_file.get(name, [])) + reported_by_file.get(name, [])
        for name in corpora
    }
    file_tokens = {
        name: _artefact_tokens(name + " " + " ".join(texts) + " " + " ".join(fields_by_file.get(name, [])))
        for name, texts in corpora.items()
    }
    requirement_about_attribute = any(
        chunk.classify() == "requirement"
        and any(
            _has_word_start(chunk.text + "\n" + chunk.filename, term)
            for term in profile.evidence_terms
        )
        for chunk in chunks
    )

    matched: List[str] = []
    missing: List[str] = []
    for artefact in expected:
        fields = _named_fields(artefact)
        if fields:
            supplied = any(
                _field_present(field_name, fields_by_file.get(name, []))
                for name in corpora
                # Only an operational file can *report* a field. A policy that names the
                # column an export ought to carry is stating the requirement, not
                # satisfying it.
                if "observation" in classes.get(name, set())
                for field_name in fields
            )
            (matched if supplied else missing).append(str(artefact))
            continue

        tokens = _artefact_tokens(artefact)
        if not tokens:
            continue
        wanted_class = _artefact_class(artefact)
        if wanted_class == "requirement":
            (matched if requirement_about_attribute else missing).append(str(artefact))
            continue

        best = 0.0
        for name, present in file_tokens.items():
            if wanted_class and wanted_class not in classes.get(name, set()):
                continue
            best = max(best, len(tokens & present) / float(len(tokens)))
        if best >= _ARTEFACT_MATCH_THRESHOLD:
            matched.append(str(artefact))
        else:
            missing.append(str(artefact))
    return matched, missing


def _analyse(control: Any, chunks: List[_ChunkView], raw_mode: bool) -> _Analysis:
    control_text = _control_text(control)
    profile_text = control_text
    if raw_mode and chunks:
        # The baseline prompt may carry no structured control record at all, but it does
        # name the subject in prose. Reading the subject out of the prompt is what a
        # model would do; it is not extra information the baseline was not given.
        profile_text = "{0}\n{1}".format(control_text, chunks[0].text[:4000])
    profile = _select_profile(profile_text)
    evidence_numbers: Set[str] = set()
    for chunk in chunks:
        evidence_numbers |= _digit_runs(chunk.text)

    if raw_mode:
        # No source metadata: the baseline cannot tell a policy from an export, so it
        # treats the whole blob as one body of text (see the module docstring).
        requirement_chunks: List[_ChunkView] = []
        observation_chunks: List[_ChunkView] = list(chunks)
    else:
        requirement_chunks = [c for c in chunks if c.classify() == "requirement"]
        observation_chunks = [c for c in chunks if c.classify() == "observation"]

    populations: List[_Population] = []
    for chunk in chunks:
        if chunk.source_type == "TABLE_SUMMARY" or re.search(r"value counts|distribution", chunk.text, re.IGNORECASE):
            populations.extend(_parse_populations(chunk, profile))

    best: Optional[_Population] = None
    best_split: Tuple[int, int, int, List[str]] = (0, 0, 0, [])
    best_score: Tuple[int, int, int] = (0, 0, 0)
    for population in populations:
        if not population.column or not population.counts:
            continue
        match = _column_matches(population.column, profile)
        if match == 0:
            # A column that does not report the attribute under test is not the
            # population this control is measured on, however well counted it is.
            continue
        split = population.split(profile)
        readable = split[0] + split[1]
        # Where an export reports the attribute in more than one column - a real
        # privileged-account listing carries both MFA_Status and MFA_Method - the column
        # to test against is the one whose values actually say whether the control
        # operated. MFA_Method's values name a device; they cannot be read as compliant
        # or exceptional, and testing against it would report a fully compliant
        # population as unclassifiable. Ranking on readability rather than on document
        # order also discards the narration line a summary writes underneath a
        # distribution ("MFA_Status = Disabled occurs in 10 row(s), at spreadsheet rows:
        # ..."), which parses as a single mislabelled count.
        score = (match, 1 if split[2] == 0 else 0, readable)
        if score > best_score:
            best, best_score, best_split = population, score, split

    compliant = exceptions = unknown = 0
    exception_labels: List[str] = []
    total: Optional[int] = None
    if best is not None:
        compliant, exceptions, unknown, exception_labels = best_split
        total = best.total if best.total is not None else (compliant + exceptions + unknown)
    else:
        for population in populations:
            if population.total is not None:
                total = population.total
                break

    columns_present: List[str] = []
    for population in populations:
        for column in population.columns_present:
            if column not in columns_present:
                columns_present.append(column)
    if not columns_present:
        for chunk in chunks:
            for column in chunk.column_names:
                if column not in columns_present:
                    columns_present.append(column)

    row_exceptions = _find_row_exceptions(
        observation_chunks or chunks, profile, attribute_column=best.column if best is not None else ""
    )
    conflicts = _find_setting_conflicts(_read_settings(chunks), control_text)

    def mentions(views: Sequence[_ChunkView]) -> bool:
        for chunk in views:
            haystack = chunk.text + "\n" + " ".join(chunk.column_names) + "\n" + chunk.filename
            if any(_has_word_start(haystack, term) for term in profile.evidence_terms):
                return True
        return False

    attribute_in_observation = mentions(observation_chunks)
    attribute_anywhere = attribute_in_observation or mentions(requirement_chunks)

    if raw_mode:
        matched_artefacts: List[str] = []
        missing_artefacts: List[str] = []
    else:
        matched_artefacts, missing_artefacts = _match_expected_artefacts(control, chunks, profile)

    analysis = _Analysis(
        profile=profile,
        chunks=chunks,
        requirement_chunks=requirement_chunks,
        observation_chunks=observation_chunks,
        populations=populations,
        population=best,
        compliant=compliant,
        exceptions=exceptions,
        unknown=unknown,
        exception_labels=exception_labels,
        total=total,
        row_exceptions=row_exceptions,
        conflicts=conflicts,
        attribute_in_observation=attribute_in_observation,
        attribute_anywhere=attribute_anywhere,
        columns_present=columns_present,
        missing_artefacts=missing_artefacts,
        matched_artefacts=matched_artefacts,
        evidence_numbers=evidence_numbers,
        raw_mode=raw_mode,
    )
    analysis.status, analysis.reason_code = _decide_status(analysis)
    return analysis


def _sufficiency_rating(
    analysis: _Analysis,
    exceptions_cited: bool = True,
) -> Tuple[EvidenceSufficiency, List[str]]:
    """Rate how far the evidence supports the conclusion, and say what limits it.

    The rating answers one question - *could this control be tested, and over how much
    of its population?* - and nothing else:

    ``NONE``
        Nothing was retrieved.
    ``INSUFFICIENT``
        The attribute under test is absent from the operational evidence, or there is no
        operational evidence at all. No conclusion is available in either direction.
    ``PARTIAL``
        The control could be tested, but not over the whole population or not at the
        detail the conclusion needs: no population summary to establish completeness,
        counts that do not add up to the stated total, values that could not be read as
        compliant or exceptional, no individual exception record available to inspect,
        or no document stating the requirement the observation is being judged against.
    ``SUFFICIENT``
        The attribute was recorded for the whole of the stated population and the
        conclusion drawn rests on evidence that is present and citable.

    What it deliberately does **not** do is treat every unsupplied artefact on the
    control's expected-evidence list as a reduction. An exception register or an
    authentication log would corroborate an MFA finding and an auditor should still
    obtain them, but their absence does not stop a listing with an MFA column from
    establishing that ten of a hundred accounts are not enrolled. Those artefacts are
    reported in ``missing_evidence`` - the auditor's request list - and the hedging they
    call for is carried by the status itself: POTENTIAL_DEFICIENCY already says "this
    needs auditor confirmation", which is exactly what an unseen exception register
    leaves open. Folding them into the rating as well conflates "the test could not be
    done" with "more documents exist", and it was that conflation which made a fully
    evidenced finding report as PARTIAL.
    """
    if not analysis.chunks:
        return EvidenceSufficiency.NONE, ["No evidence at all was retrieved for this control."]

    limits: List[str] = []
    if not analysis.raw_mode:
        if not analysis.observation_chunks:
            return EvidenceSufficiency.INSUFFICIENT, [
                "No system-generated record of the control operating: only requirement "
                "documentation was retrieved."
            ]
        if not analysis.attribute_in_observation:
            return EvidenceSufficiency.INSUFFICIENT, [analysis.profile.artefact]

    if analysis.status is AssessmentStatus.INSUFFICIENT_EVIDENCE:
        return EvidenceSufficiency.INSUFFICIENT, [analysis.profile.artefact]

    tabular_observation = any(chunk.is_tabular for chunk in analysis.observation_chunks)
    if analysis.population is None:
        # A configuration setting has no population to summarise, so only a control
        # tested by sampling a population is penalised for the absence of one.
        if tabular_observation or analysis.row_exceptions:
            limits.append(
                "A population-level summary establishing how many records the evidence covers; "
                "without it, completeness cannot be judged."
            )
    else:
        if analysis.unknown > 0:
            limits.append(
                "A reading for the {0} record(s) whose value for '{1}' could not be classified as "
                "compliant or exceptional.".format(analysis.unknown, analysis.population.column)
            )
        if analysis.total is not None and (analysis.compliant + analysis.exceptions) < analysis.total:
            limits.append(
                "Values covering the whole population: the counts read for '{0}' account for fewer "
                "records than the {1} the evidence reports.".format(
                    analysis.population.column, analysis.total
                )
            )
        if analysis.exceptions > 0 and not exceptions_cited:
            limits.append(
                "The individual records behind the summarised exception count were not available in "
                "the retrieved evidence, so no exception can be inspected."
            )

    if not analysis.requirement_chunks and not analysis.raw_mode:
        limits.append(
            "The policy or standard stating the requirement was not among the evidence retrieved for "
            "this control; the requirement was taken from the control definition instead."
        )

    return (EvidenceSufficiency.PARTIAL if limits else EvidenceSufficiency.SUFFICIENT), limits


def _decide_status(analysis: _Analysis) -> Tuple[AssessmentStatus, str]:
    """The decision rule, in priority order, with the reason recorded alongside it."""
    if not analysis.chunks:
        return AssessmentStatus.INSUFFICIENT_EVIDENCE, "no_evidence"

    if not analysis.raw_mode:
        # The behaviour this project exists to demonstrate: evidence that never
        # addresses the attribute under test cannot support a conclusion either way.
        if not analysis.observation_chunks:
            return AssessmentStatus.INSUFFICIENT_EVIDENCE, "no_operational_evidence"
        if not analysis.attribute_in_observation:
            return AssessmentStatus.INSUFFICIENT_EVIDENCE, "attribute_absent"

    if analysis.conflicts:
        return AssessmentStatus.NOT_EFFECTIVE, "configuration_contradicts_requirement"

    if analysis.population is not None and analysis.exceptions > 0:
        if analysis.total is not None and analysis.exceptions >= analysis.total:
            return AssessmentStatus.NOT_EFFECTIVE, "whole_population_fails"
        return AssessmentStatus.POTENTIAL_DEFICIENCY, "population_exceptions"

    if analysis.population is None and analysis.row_exceptions:
        return AssessmentStatus.POTENTIAL_DEFICIENCY, "row_exceptions"

    if analysis.population is not None and analysis.exceptions == 0:
        if analysis.unknown > 0:
            return AssessmentStatus.INSUFFICIENT_EVIDENCE, "unclassifiable_values"
        if analysis.total is not None and analysis.compliant >= analysis.total:
            return AssessmentStatus.EFFECTIVE, "complete_population_no_exceptions"
        return AssessmentStatus.INSUFFICIENT_EVIDENCE, "population_incomplete"

    if analysis.raw_mode:
        # Baseline behaviour: with no population data and no visible exception, the
        # naive reading is that the control operated. This is the false negative the
        # retrieval + validation pipeline is being measured against.
        if analysis.attribute_anywhere:
            return AssessmentStatus.EFFECTIVE, "raw_no_visible_exception"
        return AssessmentStatus.INSUFFICIENT_EVIDENCE, "attribute_absent"

    return AssessmentStatus.INSUFFICIENT_EVIDENCE, "attribute_not_measurable"


# ---- citation construction


def _citation(
    chunk: _ChunkView,
    quote: str,
    relevance: str,
    supports: str,
    locator_suffix: str = "",
) -> Dict[str, Any]:
    locator = chunk.citation or chunk.filename
    if locator_suffix:
        locator = f"{locator} ({locator_suffix})" if locator else locator_suffix
    return {
        "chunk_id": chunk.chunk_id,
        "filename": chunk.filename,
        "locator": locator,
        "quoted_text": quote,
        "relevance": relevance,
        "supports": supports,
    }


def _requirement_citation(analysis: _Analysis) -> Optional[Dict[str, Any]]:
    """Quote the sentence in a policy chunk that states the requirement.

    Candidate spans are ranked, not taken first-come: the first mention of 'patch' in a
    standard is usually its title page, and a citation that quotes the title proves
    nothing. A span that reads like an obligation, and that is long enough to be a
    sentence rather than a heading, wins.
    """
    candidates = analysis.requirement_chunks or [c for c in analysis.chunks if c.classify() == "requirement"]
    best_quote = ""
    best_chunk: Optional[_ChunkView] = None
    best_score = -1.0
    for chunk in candidates:
        for term in analysis.profile.evidence_terms:
            for index, match in enumerate(re.finditer(re.escape(term), chunk.text, re.IGNORECASE)):
                if index >= 20:
                    break
                quote = _quote_span(chunk.text, match.start(), match.end())
                if not quote:
                    continue
                lowered = quote.lower()
                # Length only separates a heading from a sentence; beyond that, the
                # first statement of the requirement is the one worth quoting, so ties
                # keep the earliest candidate.
                score = min(1.0, len(quote) / 70.0)
                if any(marker in lowered for marker in _NORMATIVE_MARKERS):
                    score += 2.0
                if score > best_score:
                    best_score, best_quote, best_chunk = score, quote, chunk
    if best_chunk is not None and best_quote:
        return _citation(
            best_chunk,
            best_quote,
            "States the requirement the control is tested against.",
            "requirement",
        )
    for chunk in candidates:
        quote = _quote_span(chunk.text, 0, min(len(chunk.text), 120))
        if quote:
            return _citation(chunk, quote, "Policy context for the control.", "requirement")
    return None


def _population_citation(analysis: _Analysis) -> Optional[Dict[str, Any]]:
    population = analysis.population
    if population is None:
        return None
    quote = _quote_span(population.chunk.text, population.counts_span[0], population.counts_span[1])
    if not quote:
        return None
    return _citation(
        population.chunk,
        quote,
        "Population-level counts for the attribute under test, read from the table summary.",
        "observation",
    )


def _exception_citations(analysis: _Analysis, limit: int = 3) -> List[Dict[str, Any]]:
    citations: List[Dict[str, Any]] = []
    for exception in analysis.row_exceptions[:limit]:
        quote = _quote_span(exception.chunk.text, exception.span[0], exception.span[1])
        if not quote:
            continue
        suffix = "exception row {0}".format(exception.row_number) if exception.row_number else "exception row"
        citations.append(
            _citation(
                exception.chunk,
                quote,
                "An individual record that does not meet the control requirement.",
                "exception",
                locator_suffix=suffix,
            )
        )
    return citations


def _columns_citation(analysis: _Analysis) -> Optional[Dict[str, Any]]:
    """Quote the column list of the operational export.

    For an INSUFFICIENT_EVIDENCE conclusion this is the load-bearing citation: it shows
    the reader exactly which fields the export does report, and therefore why the
    attribute under test could not be evaluated.
    """
    for population in analysis.populations:
        chunk = population.chunk
        match = _COLUMNS_LINE_RE.search(chunk.text)
        if match:
            quote = _quote_span(chunk.text, match.start(), match.end())
            if quote:
                return _citation(
                    chunk,
                    quote,
                    "The fields this export actually reports; the attribute under test is not among them.",
                    "context",
                )
    for chunk in analysis.observation_chunks:
        quote = _quote_span(chunk.text, 0, min(len(chunk.text), 160))
        if quote:
            return _citation(
                chunk,
                quote,
                "The operational evidence supplied; it does not record the attribute under test.",
                "context",
            )
    return None


# ---- narrative assembly


def _control_ref(control: Any, user_prompt: str) -> str:
    ref = _attr(control, "control_id", "") or ""
    if ref:
        return str(ref)
    match = re.search(r"\b([A-Z][A-Z0-9]{1,15}-\d{1,5})\b", user_prompt or "")
    return match.group(1) if match else ""


def _requirement_sentence(control: Any, analysis: _Analysis) -> str:
    objective = str(_attr(control, "objective", "") or "").strip()
    criteria = _attr(control, "assessment_criteria", None) or []
    description = str(_attr(control, "description", "") or "").strip()
    name = str(_attr(control, "name", "") or "").strip()
    parts: List[str] = []
    if objective:
        parts.append(objective if objective.endswith(".") else objective + ".")
    elif description:
        parts.append(description if description.endswith(".") else description + ".")
    elif name:
        parts.append("The control requires: {0}.".format(name))
    if isinstance(criteria, (list, tuple)) and criteria:
        parts.append("Tested against: {0}".format("; ".join(str(c).rstrip(".") for c in criteria[:3])) + ".")
    if not parts:
        parts.append(
            "The control requires {0} to be in place for the population in scope.".format(analysis.profile.label)
        )
    return " ".join(parts)


def _population_phrase(analysis: _Analysis) -> str:
    """Describe the population using only figures that occur in the evidence."""
    if analysis.population is None:
        return ""
    column = analysis.population.column
    if analysis.can_state(analysis.total):
        head = "a population of {0} records".format(analysis.total)
    else:
        head = "the population summarised in the evidence"
    labels: List[str] = []
    for label, count in analysis.population.counts.items():
        if analysis.can_state(count):
            labels.append("{0} = {1}".format(label, count))
        else:
            labels.append(str(label))
    if labels:
        return "{0}, in which '{1}' is recorded as {2}".format(head, column, ", ".join(labels))
    return head


def _risk_level_for(control: Any, analysis: _Analysis) -> RiskLevel:
    """A suggestion only. The authoritative band is produced by :mod:`app.audit.risk`."""
    inherent = RiskLevel.coerce(_attr(control, "inherent_risk", None), RiskLevel.MEDIUM)
    if analysis.status is AssessmentStatus.EFFECTIVE:
        return RiskLevel.LOW
    if analysis.status is AssessmentStatus.NOT_EFFECTIVE:
        if inherent in (RiskLevel.CRITICAL,):
            return RiskLevel.CRITICAL
        return RiskLevel.HIGH
    if analysis.status is AssessmentStatus.POTENTIAL_DEFICIENCY:
        return inherent if inherent is not RiskLevel.NOT_RATED else RiskLevel.MEDIUM
    return inherent if inherent is not RiskLevel.NOT_RATED else RiskLevel.MEDIUM


def _risk_sentence(control: Any, analysis: _Analysis) -> str:
    stated = str(_attr(control, "risk_addressed", "") or "").strip()
    if stated:
        return stated if stated.endswith(".") else stated + "."
    return (
        "If {0} does not operate as required, the exposure the control was designed to reduce "
        "remains unmitigated.".format(analysis.profile.label)
    )


def _standard_verifications(analysis: _Analysis) -> List[str]:
    items = [
        "Confirm the supplied extract is the complete population for the audit period and was generated "
        "directly from the source system, not edited after export.",
        "Confirm the extract date falls inside the audit period and re-perform the test on a "
        "system-generated report if it does not.",
    ]
    if analysis.unknown > 0:
        items.append(
            "Resolve the {0} record(s) whose value for '{1}' could not be classified as compliant or "
            "exceptional.".format(analysis.unknown, analysis.population.column if analysis.population else "the attribute")
        )
    return items


def _limitations(analysis: _Analysis, extra: str = "") -> str:
    parts = [_STANDING_LIMITATION]
    if analysis.profile.key == "generic":
        parts.append(
            "This control is outside the built-in attribute registry, so the evidence was searched using "
            "the control's own wording, which is a weaker test than a purpose-written one."
        )
    if analysis.raw_mode:
        parts.append(
            "No retrieval was used: the text was read as one undifferentiated block, possibly truncated, "
            "with no source references, so no statement here is traceable to a specific document location."
        )
    if extra:
        parts.append(extra)
    return " ".join(parts)


# ---- payload builders


def _assessment_payload(control: Any, analysis: _Analysis, control_ref: str) -> Dict[str, Any]:
    profile = analysis.profile
    citations: List[Dict[str, Any]] = []
    inferences: List[str] = []
    missing: List[str] = []
    verifications: List[str] = []
    status = analysis.status

    requirement_citation = _requirement_citation(analysis)
    population_citation = _population_citation(analysis)
    exception_citations = _exception_citations(analysis)

    if status is AssessmentStatus.INSUFFICIENT_EVIDENCE:
        columns_citation = _columns_citation(analysis)
        if requirement_citation:
            citations.append(requirement_citation)
        if columns_citation:
            citations.append(columns_citation)
        elif population_citation:
            citations.append(population_citation)

        if analysis.reason_code == "no_evidence":
            assessment = "No evidence was supplied for this control, so nothing could be tested."
            finding = "The control could not be tested: no evidence was available."
        elif analysis.reason_code == "no_operational_evidence":
            assessment = (
                "The supplied evidence states what is required but contains no system-generated record of "
                "how the control actually operated."
            )
            finding = (
                "The control could not be tested: only requirement documentation was available, with no "
                "operational evidence to test it against."
            )
        elif analysis.reason_code == "attribute_absent":
            columns_text = ", ".join(analysis.columns_present[:12]) if analysis.columns_present else ""
            assessment = (
                "The operational evidence supplied does not record {0}.".format(profile.label)
            )
            if columns_text:
                assessment += " The export reports the fields: {0}.".format(columns_text)
            if analysis.can_state(analysis.total):
                assessment += (
                    " It covers {0} records, but none of its fields state whether the control operated for "
                    "them.".format(analysis.total)
                )
            finding = (
                "The control could neither be confirmed nor challenged: {0} is absent from the evidence "
                "provided.".format(profile.label)
            )
            inferences.append(
                "The absence of {0} from this export is not evidence that the control failed; it is evidence "
                "that the export does not report it. No conclusion about the control's operation is drawn "
                "either way.".format(profile.label)
            )
        elif analysis.reason_code == "unclassifiable_values":
            assessment = (
                "The population summary reports values for the attribute under test that could not be read "
                "as either compliant or exceptional, so the exception count is unknown."
            )
            finding = "The control could not be tested reliably: the recorded values are ambiguous."
        elif analysis.reason_code == "population_incomplete":
            assessment = (
                "The counts recorded for the attribute under test do not account for the full population, so "
                "the untested remainder cannot be assumed compliant."
            )
            finding = "The control could not be tested over the complete population."
        else:
            assessment = (
                "The evidence mentions {0} but does not record it in a form that can be tested against the "
                "control requirement.".format(profile.label)
            )
            finding = "The control could not be tested on the evidence supplied."

        missing.append(profile.artefact)
        missing.extend(analysis.missing_artefacts)
        if not analysis.requirement_chunks and not analysis.raw_mode:
            missing.append(
                "The policy or standard stating the requirement was not among the evidence retrieved for this control."
            )
        verifications.extend(
            [
                "Request {0} and re-perform this test once it is available.".format(profile.artefact[0].lower() + profile.artefact[1:]),
                "Confirm with the system owner which system of record holds {0}.".format(profile.label),
            ]
        )
        verifications.extend(_standard_verifications(analysis))
        recommendation = (
            "Obtain {0} before concluding on this control. Do not treat the current evidence as either "
            "supporting or contradicting the requirement.".format(profile.artefact[0].lower() + profile.artefact[1:])
        )
        sufficiency, _ = _sufficiency_rating(analysis)
        confidence = ConfidenceLevel.MEDIUM
        reasoning = (
            "Requirement: {0} Evidence check: the supplied operational evidence was searched for {1} and it "
            "is not recorded there. Conclusion: the evidence does not establish how the control operated, so "
            "the outcome is INSUFFICIENT_EVIDENCE rather than a pass or a fail.".format(
                _requirement_sentence(control, analysis), profile.label
            )
        )

    elif status is AssessmentStatus.NOT_EFFECTIVE and analysis.conflicts:
        conflict = analysis.conflicts[0]
        if conflict.required_reading is not None:
            citations.append(
                _citation(
                    conflict.required_reading.chunk,
                    conflict.required_reading.quote,
                    "States the required {0}.".format(conflict.family.name),
                    "requirement",
                )
            )
        elif requirement_citation:
            citations.append(requirement_citation)
        citations.append(
            _citation(
                conflict.observed_reading.chunk,
                conflict.observed_reading.quote,
                "Records the configured {0} in the system under review.".format(conflict.family.name),
                "exception",
            )
        )
        comparator = "below" if conflict.family.direction == "min" else "above"
        # A threshold written in words ('at least fourteen characters') is understood but
        # cannot be restated as a digit without asserting something the text does not say.
        required_stated = analysis.can_state(conflict.required)
        observed_stated = analysis.can_state(conflict.observed)
        required_text = str(conflict.required) if required_stated else "the value the requirement states"
        observed_text = str(conflict.observed) if observed_stated else "a value"
        if observed_stated and required_stated:
            assessment = "The configured {0} is {1} {2}, {3} the required {4}.".format(
                conflict.family.name, observed_text, conflict.family.unit, comparator, required_text
            )
            finding = "Configured {0} of {1} is {2} the required {3}.".format(
                conflict.family.name, observed_text, comparator, required_text
            )
        else:
            assessment = "The configured {0} is {1} the value the requirement states.".format(
                conflict.family.name, comparator
            )
            finding = "Configured {0} does not meet the requirement stated in the evidence.".format(conflict.family.name)
        if conflict.requirement_source == "control definition":
            assessment += (
                " The required value is taken from the control definition; no policy document stating it was "
                "among the evidence retrieved."
            )
        inferences.append(
            "The configuration export is treated as representing the setting in force for the whole audit "
            "period; the evidence shows a single point in time."
        )
        verifications.extend(
            [
                "Confirm the configuration export was taken from the production system in scope.",
                "Confirm no separate or fine-grained policy overrides this setting for any part of the population.",
                "Establish how long the non-compliant value has been in force.",
            ]
        )
        verifications.extend(_standard_verifications(analysis))
        missing.extend(analysis.missing_artefacts)
        recommendation = (
            "Align the configured {0} with the requirement and retain evidence of the change, then re-test.".format(
                conflict.family.name
            )
        )
        sufficiency, _ = _sufficiency_rating(analysis)
        confidence = ConfidenceLevel.MEDIUM
        reasoning = (
            "Requirement: {0} Evidence: the {1} is recorded as {2} in the configuration evidence, against a "
            "required value of {3}. A configured value that directly contradicts the stated threshold is a "
            "control that is not operating as required, so the status is NOT_EFFECTIVE rather than a "
            "potential deficiency.".format(
                _requirement_sentence(control, analysis), conflict.family.name, observed_text, required_text
            )
        )

    elif status in (AssessmentStatus.POTENTIAL_DEFICIENCY, AssessmentStatus.NOT_EFFECTIVE):
        if requirement_citation:
            citations.append(requirement_citation)
        if population_citation:
            citations.append(population_citation)
        citations.extend(exception_citations)

        exception_count = analysis.exceptions if analysis.population is not None else len(analysis.row_exceptions)
        population_text = _population_phrase(analysis)
        if analysis.can_state(exception_count):
            finding = "{0} record(s) do not meet the requirement for {1}.".format(exception_count, profile.label)
        else:
            # The count was derived by reading rows rather than read off a stated total,
            # so it is not presented as though the evidence asserted it.
            finding = (
                "Records in the supplied evidence do not meet the requirement for {0}, and the evidence does "
                "not state how many.".format(profile.label)
            )
        assessment = (
            "The evidence summarises {0}. {1}".format(population_text, finding) if population_text else finding
        )
        if analysis.raw_mode:
            assessment += " Counted from the portion of the text supplied in the prompt."
        if analysis.exception_labels:
            finding += " Recorded as: {0}.".format(", ".join(analysis.exception_labels[:4]))

        if analysis.total and analysis.total > 0 and exception_count:
            ratio = 100.0 * exception_count / float(analysis.total)
            inferences.append(
                "Derived, not stated in the evidence: the exceptions represent approximately {0:.1f}% of the "
                "summarised population.".format(ratio)
            )
        inferences.append(
            "The records classified as compliant are assumed to be correctly recorded in the source system; "
            "the evidence shows the export's own values, not the underlying system state."
        )
        if analysis.population is not None and len(exception_citations) < exception_count:
            inferences.append(
                "Individual exception records are cited only where the underlying rows were retrieved; the "
                "remaining exceptions are known from the summary counts alone."
            )
        verifications.extend(
            [
                "Inspect each exception record in the source system and confirm it is a genuine exception "
                "rather than an export artefact.",
                "Establish whether any exception is covered by a documented and approved exemption.",
                "Determine how long each exception has persisted, which drives the severity of the finding.",
            ]
        )
        verifications.extend(_standard_verifications(analysis))
        missing.extend(analysis.missing_artefacts)
        if analysis.population is not None and not exception_citations:
            missing.append(
                "The individual records behind the summarised exception count were not available in the "
                "retrieved evidence."
            )
        recommendation = (
            "Remediate the exception records, and investigate why they were not prevented by the control "
            "before treating this as an isolated occurrence."
        )
        sufficiency, _ = _sufficiency_rating(analysis, exceptions_cited=bool(exception_citations))
        confidence = ConfidenceLevel.MEDIUM if analysis.population is not None else ConfidenceLevel.LOW
        reasoning = (
            "Requirement: {0} Evidence: the summarised population records exceptions for {1}. A non-zero "
            "minority of exceptions in an otherwise operating population is reported as a potential "
            "deficiency for auditor confirmation, not as a conclusion that the control does not "
            "operate.".format(_requirement_sentence(control, analysis), profile.label)
        )
        if status is AssessmentStatus.NOT_EFFECTIVE:
            reasoning += " Here every record in the population is an exception, so the control is reported as not operating."

    else:  # EFFECTIVE
        if requirement_citation:
            citations.append(requirement_citation)
        if population_citation:
            citations.append(population_citation)
        population_text = _population_phrase(analysis)
        if population_text:
            assessment = "The evidence summarises {0}, with no record failing the requirement.".format(
                population_text
            )
        else:
            assessment = "No record in the evidence supplied fails the requirement for {0}.".format(profile.label)
        if analysis.raw_mode:
            assessment += " Read from the text supplied in the prompt, which carries no population total."
        finding = "No finding identified."
        if analysis.total:
            inferences.append(
                "Derived, not stated in the evidence: every record in the summarised population is recorded "
                "as compliant, which is read as the control operating for that population."
            )
        if analysis.raw_mode:
            inferences.append(
                "No population total was available, so 'no exceptions found' means none were visible in the "
                "text supplied - not that none exist."
            )
        verifications.extend(
            [
                "Confirm the population tested is the complete population the control applies to.",
                "Confirm the control operated throughout the period, not only at the date of the export.",
            ]
        )
        verifications.extend(_standard_verifications(analysis))
        missing.extend(analysis.missing_artefacts)
        recommendation = "No remediation is indicated by this evidence. Confirm population completeness before relying on the result."
        sufficiency, _ = _sufficiency_rating(analysis)
        confidence = ConfidenceLevel.LOW if analysis.raw_mode else ConfidenceLevel.MEDIUM
        reasoning = (
            "Requirement: {0} Evidence: the summarised population records no exception for {1} and the counts "
            "account for the whole population. Conclusion: the evidence supports the control operating for "
            "that population at the date of the export.".format(
                _requirement_sentence(control, analysis), profile.label
            )
        )

    if analysis.raw_mode:
        # The baseline prompt asks for none of this scaffolding, so it is not invented here.
        missing = []
        verifications = verifications[:1]
        inferences = inferences[:1]

    limitations_extra = ""
    if not citations:
        limitations_extra = NO_EVIDENCE_SENTINEL

    payload: Dict[str, Any] = {
        "control_id": control_ref,
        "control_requirement": _requirement_sentence(control, analysis),
        "assessment": assessment,
        "status": status.value,
        "finding": finding,
        "risk": _risk_sentence(control, analysis),
        "risk_level": _risk_level_for(control, analysis).value,
        "evidence": citations,
        "evidence_sufficiency": sufficiency.value,
        "missing_evidence": _dedupe(missing),
        "reasoning": reasoning,
        "inferences": _dedupe(inferences),
        "human_verification_required": _dedupe(verifications),
        "recommendation": recommendation,
        "confidence": confidence.value,
        "human_review_required": True,
        "limitations": _limitations(analysis, limitations_extra),
    }
    return payload


def _sufficiency_payload(control: Any, analysis: _Analysis) -> Dict[str, Any]:
    """Step 1 of Experiment C: can this control be tested with this evidence at all?

    ``can_conclude`` and ``evidence_sufficiency`` answer that one question, on the rule
    documented in :func:`_sufficiency_rating`. ``missing_evidence`` is a wider list: it
    carries both the gaps that limited the rating *and* the expected artefacts that were
    never supplied, because the auditor reading it wants everything they should go and
    request, not only the things that stopped the test.
    """
    profile = analysis.profile
    present: List[str] = []
    for chunk in analysis.chunks:
        label = "{0} ({1})".format(chunk.filename or "supplied evidence", chunk.classify())
        if label not in present:
            present.append(label)
    present.extend("Expected artefact identified: {0}".format(item) for item in analysis.matched_artefacts)

    sufficiency, limits = _sufficiency_rating(analysis)
    can_conclude = sufficiency in (EvidenceSufficiency.SUFFICIENT, EvidenceSufficiency.PARTIAL)
    missing: List[str] = list(limits)
    missing.extend(analysis.missing_artefacts)

    if can_conclude:
        rationale = (
            "The retrieved evidence records {0}, so the control can be tested against it. "
            "{1} Artefacts listed as missing limit the scope of that conclusion, or would corroborate "
            "it; they do not prevent the test.".format(
                profile.label,
                "The evidence covers the whole of the population it reports."
                if sufficiency is EvidenceSufficiency.SUFFICIENT
                else "The test is only partial: {0}".format(limits[0][0].lower() + limits[0][1:]),
            )
        )
    else:
        rationale = (
            "The retrieved evidence does not record {0} for the population in scope. It can establish what is "
            "required, but not what happened, so no conclusion on the control's operation can be drawn from "
            "it.".format(profile.label)
        )

    return {
        "evidence_sufficiency": sufficiency.value,
        "present_evidence": _dedupe(present),
        "missing_evidence": _dedupe(missing),
        "can_conclude": can_conclude,
        "rationale": rationale,
    }


def _critique_payload(analysis: _Analysis, draft: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Check a draft assessment against the evidence that was actually supplied.

    The check is mechanical - chunk IDs are looked up, quotations are substring-matched -
    so a fabricated citation is caught with certainty. That is a property of the
    workflow step, not an indication that a language model would notice its own
    invention; the write-up must not conflate the two.
    """
    unsupported: List[str] = []
    fabricated: List[str] = []
    overstated = False
    recommended: Optional[str] = None
    if not draft:
        return {
            "unsupported_claims": [],
            "fabricated_citations": [],
            "overstated_conclusion": False,
            "recommended_status": None,
            "notes": "No draft assessment was supplied to critique, so nothing could be checked.",
        }

    by_id: Dict[int, _ChunkView] = {c.chunk_id: c for c in analysis.chunks if c.chunk_id is not None}
    citations = draft.get("evidence") or []
    verified = 0
    for index, citation in enumerate(citations):
        if not isinstance(citation, dict):
            continue
        chunk_id = citation.get("chunk_id")
        quote = str(citation.get("quoted_text") or "").strip()
        try:
            chunk_id_int = int(chunk_id) if chunk_id is not None else None
        except (TypeError, ValueError):
            chunk_id_int = None
        if chunk_id_int is None or chunk_id_int not in by_id:
            fabricated.append(
                "Citation {0} refers to chunk_id {1}, which was not among the evidence supplied.".format(
                    index + 1, chunk_id
                )
            )
            continue
        chunk_text = by_id[chunk_id_int].text
        if quote and _normalise_ws(quote) in _normalise_ws(chunk_text):
            verified += 1
        else:
            fabricated.append(
                "Citation {0} quotes text that does not occur in chunk {1}: \"{2}\".".format(
                    index + 1, chunk_id_int, quote[:120]
                )
            )

    narrative = " ".join(
        str(draft.get(key) or "") for key in ("assessment", "finding", "reasoning")
    )
    for number in sorted(_digit_runs(narrative)):
        if len(number) >= 2 and number not in analysis.evidence_numbers:
            unsupported.append(
                "The figure {0} appears in the narrative but occurs nowhere in the supplied evidence.".format(number)
            )

    draft_status = AssessmentStatus.coerce(draft.get("status"), None)
    if draft_status in (AssessmentStatus.EFFECTIVE, AssessmentStatus.NOT_EFFECTIVE):
        if verified == 0 or fabricated:
            overstated = True
            recommended = AssessmentStatus.INSUFFICIENT_EVIDENCE.value
    if (
        draft_status is not None
        and analysis.status is AssessmentStatus.INSUFFICIENT_EVIDENCE
        and draft_status is not AssessmentStatus.INSUFFICIENT_EVIDENCE
    ):
        overstated = True
        recommended = AssessmentStatus.INSUFFICIENT_EVIDENCE.value
        unsupported.append(
            "The draft concludes {0}, but the supplied evidence does not record {1} for the population in "
            "scope.".format(draft_status.value, analysis.profile.label)
        )

    if fabricated or unsupported or overstated:
        notes = (
            "{0} of {1} citation(s) were matched verbatim against the supplied chunks. Issues above were "
            "found by exact lookup and substring matching, not by judgement.".format(verified, len(citations))
        )
    else:
        notes = (
            "All {0} citation(s) resolve to supplied chunks and quote them verbatim, and every figure in the "
            "narrative occurs in the evidence.".format(len(citations))
        )
    return {
        "unsupported_claims": _dedupe(unsupported),
        "fabricated_citations": _dedupe(fabricated),
        "overstated_conclusion": overstated,
        "recommended_status": recommended,
        "notes": notes,
    }


def _normalise_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _dedupe(items: Sequence[str]) -> List[str]:
    out: List[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


# ---- the research hallucination lever


def _inject_hallucination(
    payload: Dict[str, Any],
    analysis: _Analysis,
    rng: random.Random,
) -> Dict[str, Any]:
    """Deliberately corrupt one element of an otherwise honest assessment.

    Used only when ``settings.mock_hallucination_rate`` is above zero. The corruption is
    recorded in the response's ``raw`` telemetry so a harness can measure detection
    recall against ground truth - it is never disclosed inside the assessment itself,
    which would defeat the purpose.
    """
    known_ids = [c.chunk_id for c in analysis.chunks if c.chunk_id is not None]
    kinds = ["fabricated_chunk_id", "misquotation", "unsupported_number"]
    if not payload.get("evidence"):
        kinds = ["unsupported_number"]
    elif not known_ids:
        kinds = ["misquotation", "unsupported_number"]
    kind = rng.choice(kinds)

    if kind == "fabricated_chunk_id":
        fake_id = (max(known_ids) + 7919) if known_ids else 900001
        payload["evidence"] = list(payload["evidence"]) + [
            {
                "chunk_id": fake_id,
                "filename": "Control_Owner_Confirmation.pdf",
                "locator": "page 2, section 3.1",
                "quoted_text": "The control owner confirmed that all exceptions were remediated before period end.",
                "relevance": "Corroborates that the exceptions were addressed.",
                "supports": "observation",
            }
        ]
        detail = "added a citation to chunk_id {0}, which was never supplied".format(fake_id)
    elif kind == "misquotation":
        index = rng.randrange(len(payload["evidence"]))
        citation = dict(payload["evidence"][index])
        citation["quoted_text"] = (
            "All privileged accounts were confirmed compliant with the requirement at the date of testing."
        )
        payload["evidence"] = list(payload["evidence"])
        payload["evidence"][index] = citation
        detail = "replaced the quotation in citation {0} with text that is not in that chunk".format(index + 1)
    else:
        invented = 100 + rng.randrange(1, 60)
        while str(invented) in analysis.evidence_numbers:
            invented += 1
        payload["assessment"] = (
            str(payload.get("assessment", "")).rstrip()
            + " A further {0} accounts were remediated during the period.".format(invented)
        )
        detail = "added an unsupported numeric claim ({0})".format(invented)
    return {"kind": kind, "detail": detail}


# ---- provider


class MockLLMProvider(LLMProvider):
    """Offline, deterministic, rule-based provider. See the module docstring."""

    name = "mock"

    def __init__(self, model: str = "", settings: Optional[Settings] = None, **kwargs: Any) -> None:
        super().__init__(model=model or MOCK_MODEL_NAME, **kwargs)
        self.settings = settings or get_settings()

    # -------------------------------------------------------------- interface
    def complete(
        self,
        system: str,
        user: str,
        json_schema: Optional[Dict[str, Any]] = None,
        temperature: float = 0.0,
        max_tokens: int = 2000,
        context: Optional[LLMCallContext] = None,
    ) -> LLMResponse:
        start = time.perf_counter()
        purpose = self._resolve_purpose(context, user, json_schema)
        control = context.control if context is not None else None
        extras = dict(context.extras) if context is not None and context.extras else {}
        raw_chunks = list(context.chunks) if context is not None and context.chunks else []
        chunk_views = _as_chunk_views(raw_chunks)

        # An empty chunk list means two opposite things, and the engine says which:
        # Experiment A supplies no chunks because it performs no retrieval, and the
        # evidence is in the prompt; a retrieving mode that supplies none means
        # retrieval ran and found nothing, in which case there is no evidence at all.
        # Reading the prompt as evidence in the second case makes the provider cite the
        # instructions it was given, so a control with no evidence produces citations
        # that are then recorded as fabricated - a hallucination figure manufactured by
        # the provider's own confusion rather than observed.
        retrieval_performed = bool(extras.get("retrieval_performed", False))
        raw_mode = not chunk_views and not retrieval_performed
        if raw_mode:
            chunk_views = self._raw_chunk_from_prompt(user)

        analysis = _analyse(control, chunk_views, raw_mode)
        control_ref = _control_ref(control, user)

        injection: Optional[Dict[str, Any]] = None
        if purpose == "sufficiency":
            payload: Dict[str, Any] = _sufficiency_payload(control, analysis)
        elif purpose == "critique":
            payload = _critique_payload(analysis, self._draft_from(extras, user))
        else:
            payload = _assessment_payload(control, analysis, control_ref)
            rate = float(getattr(self.settings, "mock_hallucination_rate", 0.0) or 0.0)
            if rate > 0.0:
                rng = random.Random("{0}|{1}|{2}".format(self.settings.mock_seed, control_ref, purpose))
                if rng.random() < rate:
                    injection = _inject_hallucination(payload, analysis, rng)

        text = json.dumps(payload, indent=2, ensure_ascii=False)
        return LLMResponse(
            text=text,
            provider=self.name,
            model=self.model,
            prompt_tokens=estimate_tokens("{0}\n{1}".format(system or "", user or "")),
            completion_tokens=estimate_tokens(text),
            latency_ms=self._timed(start),
            finish_reason="stop",
            raw={
                "provider": self.name,
                "rules_version": MOCK_RULES_VERSION,
                "purpose": purpose,
                "raw_mode": raw_mode,
                "attribute_profile": analysis.profile.key,
                "reason_code": analysis.reason_code,
                "chunk_ids_supplied": [c.chunk_id for c in analysis.chunks],
                # Reported as a matched pair or not at all. The engine turns these two
                # into the exception *rate* that feeds the risk model, and it can only
                # do that when the count and the total are counts of the same thing.
                # analysis.total is also set from an unrelated table when no attribute
                # column was found, so publishing it beside an exception count of zero
                # would tell the risk model "0% exceptions" about a population nobody
                # measured. Row-level exceptions are reported separately: they are a
                # floor, not a rate, since they are capped and cover only the rows that
                # were retrieved.
                "exception_count": analysis.exceptions if analysis.population is not None else None,
                "population_total": analysis.total if analysis.population is not None else None,
                "row_exception_count": len(analysis.row_exceptions),
                "exception_count_basis": (
                    "value counts for column '{0}'".format(analysis.population.column)
                    if analysis.population is not None
                    else "no attribute column was located; no exception rate is available"
                ),
                "mock_injection": injection,
                "disclaimer": MOCK_DISCLAIMER,
            },
        )

    def is_available(self) -> bool:
        """Always true: there is nothing to configure and nothing to reach."""
        return True

    def health(self) -> Dict[str, Any]:
        return {
            "provider": self.name,
            "model": self.model,
            "available": True,
            "offline": True,
            "rules_version": MOCK_RULES_VERSION,
            "hallucination_rate": float(getattr(self.settings, "mock_hallucination_rate", 0.0) or 0.0),
            "seed": int(getattr(self.settings, "mock_seed", 0) or 0),
            "note": MOCK_DISCLAIMER,
        }

    # ---------------------------------------------------------------- helpers
    def _resolve_purpose(
        self,
        context: Optional[LLMCallContext],
        user: str,
        json_schema: Optional[Dict[str, Any]],
    ) -> str:
        purpose = (context.purpose if context is not None else "") or ""
        purpose = purpose.strip().lower()
        if purpose in ("assessment", "sufficiency", "critique"):
            return purpose
        haystack = "{0} {1}".format(user or "", json.dumps(json_schema) if json_schema else "").lower()
        if "unsupported_claims" in haystack or "critique" in haystack:
            return "critique"
        if "can_conclude" in haystack or "sufficiency" in haystack:
            return "sufficiency"
        return "assessment"

    def _raw_chunk_from_prompt(self, user: str) -> List[_ChunkView]:
        """Experiment A: the prompt text itself is the only evidence available."""
        text = (user or "").strip()
        if not text:
            return []
        return [
            _ChunkView(
                chunk_id=None,
                text=text,
                filename="(evidence pasted into the prompt)",
                citation="raw prompt text - no source reference available",
                source_type="TEXT_BLOCK",
            )
        ]

    def _draft_from(self, extras: Dict[str, Any], user: str) -> Optional[Dict[str, Any]]:
        for key in ("draft", "draft_assessment", "assessment", "output", "draft_output"):
            candidate = extras.get(key)
            if isinstance(candidate, dict):
                return candidate
            if isinstance(candidate, str) and candidate.strip():
                try:
                    return extract_json(candidate)
                except Exception:  # noqa: BLE001 - a malformed draft is not fatal here
                    continue
            if candidate is not None and hasattr(candidate, "model_dump"):
                try:
                    return candidate.model_dump(mode="json")
                except Exception:  # noqa: BLE001
                    continue
        try:
            return extract_json(user or "")
        except Exception:  # noqa: BLE001 - no draft in the prompt either
            return None
