"""Mechanical grounding checks on model output - the system's anti-hallucination layer.

Why this module exists
----------------------
"The AI cannot invent evidence" is a promise until something checks it. This module is
that check. It takes an :class:`~app.schemas.assessment.AssessmentOutput` and the
:class:`~app.rag.base.RetrievalResult` that was actually shown to the model, and it
re-derives, without asking the model anything, three things:

1. **Does every citation resolve?** A ``chunk_id`` that was not in the retrieved set is
   FABRICATED, no matter how plausible the accompanying quote reads. Plausibility is
   precisely the failure mode being guarded against, so it is given no weight at all.
2. **Is every quote actually in the chunk it claims to come from?** Quotes are matched
   against the real chunk text after normalisation, using two independent similarity
   measures, and the more generous of the two is used.
3. **Does every number in the prose appear in the evidence?** A grounded citation does
   not stop a model from writing "97% of accounts" next to it.

Nothing here is a language-model judgement. Every verdict is reproducible from the
stored assessment and the stored chunks, which is what makes the hallucination rate a
measurable property of the pipeline rather than a claim about it.

What the checks are *not*
-------------------------
A VERIFIED citation means the quoted characters exist in the chunk the model pointed
at. It does not mean the quote was read in context, that it supports the conclusion
drawn from it, or that the underlying evidence is authentic or complete. Those remain
human judgements, which is why :func:`enforce_safety_rails` forces human review on
every assessment regardless of how clean the report looks.

One limit deserves stating plainly, because it bounds what the hallucination metric can
claim. Taking the *maximum* of the two similarity measures means the order-insensitive
one sets the floor, and a sentence assembled out of the cited chunk's own vocabulary
scores highly even when it says something the chunk does not. Measured on this
project's own fixtures: a verbatim quote scores 1.00; a quote with one word substituted
scores 1.00; a word-salad rearrangement of the chunk's vocabulary scores 0.92; a
sentence that reuses the chunk's words to assert the *opposite* of the chunk scores
0.90 - all three of which land at VERIFIED against a 0.60 threshold - while wholly
invented text scores 0.36. So these checks reliably catch text that is not in the
evidence, and do not catch text that is in the evidence but rearranged. Semantic
faithfulness is not tested here and is not claimed anywhere.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from app.config import get_settings
from app.rag.base import RetrievalResult
from app.schemas.assessment import NO_EVIDENCE_SENTINEL, AssessmentOutput
from app.schemas.enums import (
    AssessmentStatus,
    CitationVerdict,
    ConfidenceLevel,
    EvidenceSufficiency,
)

#: Statuses that assert something definite about how the control operates. These are
#: the conclusions that must not stand on unverifiable evidence.
_DEFINITE_STATUSES = (AssessmentStatus.EFFECTIVE, AssessmentStatus.NOT_EFFECTIVE)

#: Guard rails on the quote/chunk comparison so a pathological input cannot make the
#: quadratic longest-common-substring search dominate an audit run.
_MAX_QUOTE_CHARS = 4000
_MAX_CHUNK_CHARS = 20000

#: Above this many distinct evidence counts the pairwise percentage search is skipped.
_MAX_PERCENT_PAIR_TERMS = 400
#: Smallest population accepted as a percentage denominator.
_MIN_PERCENT_DENOMINATOR = 10
#: How close a derived share must be to the claimed percentage, in percentage points.
_PERCENT_TOLERANCE = 0.05

#: Collapses every run of punctuation/whitespace/underscore into a single space. Using
#: ``\W`` (rather than an ASCII class) keeps accented letters and non-Latin scripts
#: intact instead of silently deleting them from both sides of the comparison.
_PUNCT_RE = re.compile(r"[\W_]+", re.UNICODE)

_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_ANY_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")

#: Numeric claim + optional percent marker. The lookbehind stops the scanner from
#: re-entering the middle of a number it has already consumed.
_CLAIM_NUMBER_RE = re.compile(
    r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(%|percent\b|per cent\b)?",
    re.IGNORECASE,
)

_MONTHS = r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"

#: Date shapes are masked as a unit and judged on their year component, because the
#: same date is routinely written three different ways across a policy PDF, a ticket
#: export and a model's prose.
_DATE_PATTERNS: List[re.Pattern] = [
    re.compile(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b"),
    re.compile(r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b"),
    re.compile(r"\b\d{1,2}\s+(?:" + _MONTHS + r")[a-z]*\.?,?\s+(\d{4})\b", re.IGNORECASE),
    re.compile(r"\b(?:" + _MONTHS + r")[a-z]*\.?\s+\d{1,2},?\s+(\d{4})\b", re.IGNORECASE),
]

#: Spans whose digits are never evidential claims: identifiers, cross-references,
#: ordinals and list markers. Masked out before the numeric scan so that "CONTROL-001",
#: "section 4.2" and a numbered list do not each become a fabricated statistic.
_IGNORE_PATTERNS: List[re.Pattern] = [
    re.compile(r"\b[A-Za-z]{2,}[-_]\d+(?:[-_]\d+)*\b"),
    re.compile(r"\bchunk[_ ]?id\s*[:#=]?\s*\d+", re.IGNORECASE),
    re.compile(r"#\s?\d+"),
    re.compile(r"\b\d{1,3}(?:st|nd|rd|th)\b", re.IGNORECASE),
    re.compile(r"(?m)^\s*\(?\d{1,2}[.)]\s"),
    re.compile(
        r"\b(?:section|clause|step|item|paragraph|para|page|pages|sheet|figure|table|appendix|"
        r"control|criterion|criteria|requirement|version|no\.?|number)\s+\d+(?:\.\d+)*",
        re.IGNORECASE,
    ),
    # Locator references: "rows 14, 27 and 38", "row 15", "lines 4-9".
    re.compile(
        r"\b(?:rows?|lines?|entries)\s+\d+(?:\s*(?:,|-|--|to|and|&)\s*\d+)*",
        re.IGNORECASE,
    ),
    re.compile(r"\bv\d+(?:\.\d+)+\b", re.IGNORECASE),
]

#: Fields whose prose is scanned for numeric claims. ``control_requirement`` is
#: deliberately excluded: it restates policy rather than reporting an observation.
_CLAIM_FIELDS = ("assessment", "finding", "reasoning")


# ---- text normalisation and similarity


def normalise_text(text: str) -> str:
    """Casefold, strip punctuation and collapse whitespace.

    Both sides of every comparison go through this, so a model that re-types a quote
    with different quote marks, hyphenation or line wrapping is not punished for it -
    while a model that invented the sentence still cannot match.
    """
    if not text:
        return ""
    # NFKC first: without it "cafe\u0301" and "caf\u00e9" normalise differently, because
    # stripping punctuation deletes a combining accent but leaves a precomposed one.
    folded = unicodedata.normalize("NFKC", text).casefold()
    return _PUNCT_RE.sub(" ", folded).strip()


def longest_common_substring_ratio(quote_norm: str, chunk_norm: str) -> float:
    """Longest shared run of characters, as a fraction of the quote's length.

    This is the measure that catches a model quoting a real sentence and then adding a
    clause of its own: the shared run stops where the invention starts.
    """
    if not quote_norm or not chunk_norm:
        return 0.0
    a = quote_norm[:_MAX_QUOTE_CHARS]
    b = chunk_norm[:_MAX_CHUNK_CHARS]
    # autojunk would treat common characters as noise once b is long, which for
    # character-level matching destroys the comparison entirely.
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    match = matcher.find_longest_match(0, len(a), 0, len(b))
    return float(match.size) / float(len(a))


def token_overlap_ratio(quote_norm: str, chunk_norm: str) -> float:
    """Jaccard overlap restricted to the quote's token set (i.e. containment).

    A symmetric Jaccard index would be near zero by construction here, because a chunk
    is one to two orders of magnitude longer than a quote; the union term would swamp
    the intersection and every honest citation would look fabricated. Dividing by the
    quote's own tokens asks the question that actually matters: how much of what the
    model claims to have read is present in what it was given?

    This measure is order-insensitive, so it rescues a citation that reorders or
    reflows a table row, at the cost of being fooled by a bag of the right words in the
    wrong arrangement - which is why it is combined with, not used instead of, the
    substring measure.
    """
    quote_tokens = set(quote_norm.split())
    if not quote_tokens:
        return 0.0
    chunk_tokens = set(chunk_norm.split())
    return len(quote_tokens & chunk_tokens) / float(len(quote_tokens))


def grounding_score(quoted_text: str, chunk_text: str) -> float:
    """Best of the two similarity measures, in ``0.0-1.0``."""
    quote_norm = normalise_text(quoted_text)
    chunk_norm = normalise_text(chunk_text)
    if not quote_norm or not chunk_norm:
        return 0.0
    overlap = token_overlap_ratio(quote_norm, chunk_norm)
    if overlap >= 1.0:
        # Nothing can beat a perfect score, and the substring search is the expensive
        # half - this short-circuits the common case of a genuinely verbatim quote.
        return 1.0
    return max(longest_common_substring_ratio(quote_norm, chunk_norm), overlap)


# ---- citation checking


@dataclass
class CitationCheck:
    """The verdict on one model citation, carrying everything a citation row needs."""

    order_index: int = 0
    chunk_id: Optional[int] = None
    evidence_file_id: Optional[int] = None
    filename: str = ""
    locator_text: str = ""
    quoted_text: str = ""
    relevance: str = ""
    supports: str = ""
    verdict: CitationVerdict = CitationVerdict.UNVERIFIED
    match_score: float = 0.0
    note: str = ""

    @property
    def is_fabricated(self) -> bool:
        return self.verdict is CitationVerdict.FABRICATED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order_index": self.order_index,
            "chunk_id": self.chunk_id,
            "evidence_file_id": self.evidence_file_id,
            "filename": self.filename,
            "locator_text": self.locator_text,
            "quoted_text": self.quoted_text,
            "relevance": self.relevance,
            "supports": self.supports,
            "verdict": self.verdict.value,
            "match_score": round(self.match_score, 4),
            "note": self.note,
        }


#: Credit a PARTIAL citation earns in :attr:`ValidationReport.grounding_rate`. Named
#: rather than inlined so the figure a dissertation quotes has one definition in one
#: place, and so a reader can see it is a chosen constant and not a derived quantity.
PARTIAL_CITATION_CREDIT = 0.5


@dataclass
class ValidationReport:
    """Aggregate grounding result for one assessment. Persisted verbatim.

    Counting rules
    --------------
    Every citation the model made falls into exactly one verdict, so
    ``verified + partial + unverified + fabricated == total`` always holds. ``total`` is
    the number of citations the model emitted, **not** the number of chunks retrieved: a
    model that cites nothing has ``total == 0`` and every rate below is ``0.0`` by the
    empty-denominator convention, which is why a rate of zero must never be read on its
    own as "the model was caught inventing things". Read it with ``total``.

    Three rates, and which one to quote
    -----------------------------------
    ``grounding_rate = (verified + 0.5 x partial) / total``
        The working figure shown in the UI and printed in the audit report. The half
        credit is a *choice*, not a measurement: a PARTIAL verdict means the model was
        demonstrably reading real retrieved text but did not reproduce it faithfully,
        which is neither a clean citation nor an invention, and collapsing it into
        either neighbour loses the distinction the verdict exists to record. The
        constant lives in :data:`PARTIAL_CITATION_CREDIT`.
    ``grounding_rate_strict = verified / total``
        The conservative floor: only a quote matched at or above
        ``settings.citation_match_threshold`` counts. **This is the figure to quote in
        a write-up**, because it rests on the threshold alone and on no weighting
        choice. :func:`app.evaluation.metrics.evidence_metrics` reports the pooled
        equivalent of this rate across a run.
    ``partial_credit_rate = 0.5 x partial / total``
        The difference between the two, exposed separately so the reader can see how
        much of the headline figure is credit rather than verified quotation.

    All three, and the four counts they are computed from, are in :meth:`to_dict`, so a
    stored assessment can be re-scored under any other weighting without re-running
    anything. That is the point of exposing the components: the observation
    "0 verified, 0 fabricated, grounding 0.50" is uninterpretable on its own and fully
    interpretable once ``partial`` and ``total`` are printed beside it - it means every
    citation was a partial match.

    What none of the three measure
    ------------------------------
    They measure whether quoted characters exist in the chunk the model pointed at.
    They say nothing about whether the quote supports the conclusion drawn from it (see
    the module docstring's account of the order-insensitive similarity floor).
    """

    citations: List[CitationCheck] = field(default_factory=list)
    total: int = 0
    verified: int = 0
    partial: int = 0
    fabricated: int = 0
    unverified: int = 0
    grounding_rate: float = 0.0
    unsupported_claims: List[str] = field(default_factory=list)
    rails_applied: List[str] = field(default_factory=list)
    retrieved_chunk_ids: List[int] = field(default_factory=list)
    threshold: float = 0.0
    status_downgraded: bool = False
    original_status: str = ""

    @property
    def has_hallucination(self) -> bool:
        """True when *any* mechanical grounding failure was found.

        Used as the binary hallucination indicator in the evaluation harness. It is
        deliberately inclusive of unsupported numeric claims, because an invented
        statistic misleads an auditor exactly as much as an invented quotation.
        """
        return self.fabricated > 0 or bool(self.unsupported_claims)

    @property
    def grounding_rate_strict(self) -> float:
        """``verified / total``: the rate that credits nothing but a verified quote.

        Derived rather than stored so it can never disagree with the counts it is
        computed from, including on a report assembled by hand or loaded from an older
        stored row.
        """
        return (float(self.verified) / float(self.total)) if self.total else 0.0

    @property
    def partial_credit_rate(self) -> float:
        """The share of :attr:`grounding_rate` contributed by half-credited PARTIALs."""
        return (PARTIAL_CITATION_CREDIT * float(self.partial) / float(self.total)) if self.total else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "verified": self.verified,
            "partial": self.partial,
            "fabricated": self.fabricated,
            "unverified": self.unverified,
            "grounding_rate": round(self.grounding_rate, 4),
            "grounding_rate_strict": round(self.grounding_rate_strict, 4),
            "partial_credit_rate": round(self.partial_credit_rate, 4),
            "partial_citation_credit": PARTIAL_CITATION_CREDIT,
            "threshold": self.threshold,
            "has_hallucination": self.has_hallucination,
            "unsupported_claims": list(self.unsupported_claims),
            "rails_applied": list(self.rails_applied),
            "retrieved_chunk_ids": list(self.retrieved_chunk_ids),
            "status_downgraded": self.status_downgraded,
            "original_status": self.original_status,
            "citations": [c.to_dict() for c in self.citations],
            "grounding_rate_definition": (
                "(verified + {0} x partial) / total citations, where total counts every "
                "citation the model emitted and is 0 when it cited nothing. Quote "
                "grounding_rate_strict (verified / total) in a write-up: it depends only on "
                "the {1:.0%} match threshold and on no weighting choice.".format(
                    PARTIAL_CITATION_CREDIT, self.threshold
                )
            ),
            "grounding_rate_strict_definition": "verified citations / total citations; PARTIAL counts as 0",
        }


def validate_citations(output: AssessmentOutput, retrieval: RetrievalResult) -> ValidationReport:
    """Check every citation against the chunks the model was actually shown.

    Verdicts:

    ``FABRICATED``
        The ``chunk_id`` is not in the retrieved set, or it is and the quote has no
        textual support in that chunk. An unresolvable pointer is fabricated on the
        strength of the pointer alone - the quote is not even scored, because a quote
        that happens to read well is the symptom, not the defence.
    ``VERIFIED``
        Quote matched its own chunk at or above ``settings.citation_match_threshold``.
    ``PARTIAL``
        Matched at or above half the threshold: the model was looking at real text but
        did not reproduce it faithfully. Also used when a quote is verifiable but the
        model supplied no ``chunk_id``, since an untraceable citation can never be
        fully verified even when its text is genuine.
    ``UNVERIFIED``
        A resolvable ``chunk_id`` with no quotation. This is a legitimate, honest form
        of citation - the model pointed at real evidence without excerpting it - so it
        is not counted as fabrication, but it earns no grounding credit either.
    """
    settings = get_settings()
    threshold = float(settings.citation_match_threshold)
    partial_threshold = threshold / 2.0

    retrieved_ids = list(retrieval.chunk_ids) if retrieval is not None else []
    id_preview = ", ".join(str(i) for i in retrieved_ids[:12]) or "(none)"
    if len(retrieved_ids) > 12:
        id_preview += ", ..."

    checks: List[CitationCheck] = []
    for index, citation in enumerate(output.evidence):
        check = CitationCheck(
            order_index=index,
            chunk_id=citation.chunk_id,
            filename=citation.filename,
            locator_text=citation.locator,
            quoted_text=citation.quoted_text,
            relevance=citation.relevance,
            supports=citation.supports,
        )

        chunk = retrieval.by_id(citation.chunk_id) if (retrieval is not None and citation.chunk_id is not None) else None

        if citation.chunk_id is not None and chunk is None:
            check.verdict = CitationVerdict.FABRICATED
            check.match_score = 0.0
            check.note = (
                "chunk_id {0} was never retrieved for this control. Retrieved ids: {1}. "
                "The quoted text was not scored - an unresolvable citation is fabricated "
                "regardless of how plausible its wording is.".format(citation.chunk_id, id_preview)
            )
            checks.append(check)
            continue

        if chunk is not None:
            # Prefer the stored provenance over whatever the model typed.
            check.evidence_file_id = chunk.evidence_file_id
            check.filename = chunk.filename or citation.filename
            check.locator_text = chunk.locator.render() if chunk.locator is not None else citation.locator

            if not normalise_text(citation.quoted_text):
                check.verdict = CitationVerdict.UNVERIFIED
                check.match_score = 0.0
                check.note = (
                    "Citation resolves to retrieved chunk {0} but carries no quotation, so there "
                    "is nothing to match. The pointer is real; the excerpt is missing.".format(chunk.chunk_id)
                )
            else:
                score = grounding_score(citation.quoted_text, chunk.text)
                check.match_score = score
                if score >= threshold:
                    check.verdict = CitationVerdict.VERIFIED
                    check.note = "Quote matched chunk {0} at {1:.0%} (threshold {2:.0%}).".format(
                        chunk.chunk_id, score, threshold
                    )
                elif score >= partial_threshold:
                    check.verdict = CitationVerdict.PARTIAL
                    check.note = (
                        "Quote only partially matches chunk {0} ({1:.0%}, below the {2:.0%} threshold). "
                        "The model appears to have paraphrased or extended real text.".format(
                            chunk.chunk_id, score, threshold
                        )
                    )
                else:
                    check.verdict = CitationVerdict.FABRICATED
                    check.note = (
                        "Quote does not appear in chunk {0} ({1:.0%} similarity, below the {2:.0%} "
                        "partial floor). The chunk is real but this excerpt is not in it.".format(
                            chunk.chunk_id, score, partial_threshold
                        )
                    )
            checks.append(check)
            continue

        # No chunk_id supplied at all.
        if not normalise_text(citation.quoted_text):
            check.verdict = CitationVerdict.UNVERIFIED
            check.match_score = 0.0
            check.note = "Citation carries neither a chunk id nor a quotation; there is nothing to verify."
            checks.append(check)
            continue

        best_score, best_chunk_id = _best_match(citation.quoted_text, retrieval)
        check.match_score = best_score
        if best_score >= partial_threshold:
            check.verdict = CitationVerdict.PARTIAL
            check.note = (
                "No chunk_id was supplied. The quoted text matches retrieved chunk {0} at {1:.0%}, so it "
                "is not invented, but an untraceable citation cannot be recorded as verified.".format(
                    best_chunk_id, best_score
                )
            )
        else:
            check.verdict = CitationVerdict.FABRICATED
            check.note = (
                "No chunk_id was supplied and the quoted text matches no retrieved chunk "
                "(best {0:.0%}).".format(best_score)
            )
        checks.append(check)

    verified = sum(1 for c in checks if c.verdict is CitationVerdict.VERIFIED)
    partial = sum(1 for c in checks if c.verdict is CitationVerdict.PARTIAL)
    fabricated = sum(1 for c in checks if c.verdict is CitationVerdict.FABRICATED)
    unverified = sum(1 for c in checks if c.verdict is CitationVerdict.UNVERIFIED)
    total = len(checks)
    # Half credit for PARTIAL, per PARTIAL_CITATION_CREDIT. See the ValidationReport
    # docstring for why, and for the strict verified-only rate exposed alongside it.
    # Note that the safety rails below do *not* accept partial credit as grounding: a
    # conclusion supported only by partial matches is still withdrawn.
    grounding_rate = ((verified + PARTIAL_CITATION_CREDIT * partial) / total) if total else 0.0

    return ValidationReport(
        citations=checks,
        total=total,
        verified=verified,
        partial=partial,
        fabricated=fabricated,
        unverified=unverified,
        grounding_rate=grounding_rate,
        retrieved_chunk_ids=retrieved_ids,
        threshold=threshold,
    )


def _best_match(quoted_text: str, retrieval: Optional[RetrievalResult]) -> Tuple[float, Optional[int]]:
    best_score = 0.0
    best_id: Optional[int] = None
    if retrieval is None:
        return best_score, best_id
    for chunk in retrieval.chunks:
        score = grounding_score(quoted_text, chunk.text)
        if score > best_score:
            best_score = score
            best_id = chunk.chunk_id
        if best_score >= 1.0:
            break
    return best_score, best_id


# ---- numeric claim checking


def detect_unsupported_claims(
    output: AssessmentOutput,
    retrieval: RetrievalResult,
    control: Any = None,
) -> List[str]:
    """Flag figures in the prose that appear nowhere in the retrieved evidence.

    A citation check cannot catch "97% of privileged accounts have MFA" written beside
    a perfectly genuine quotation. This scans ``assessment``, ``finding`` and
    ``reasoning`` for numeric claims and reports any whose value cannot be found in the
    evidence the model was shown.

    The supported vocabulary is every number appearing in the retrieved chunk text,
    plus the chunks' own provenance (filenames, page numbers, sheet names, row numbers,
    rendered locators) so that "rows 14, 27 and 38 show..." is not flagged, plus the
    numbers in the control definition when ``control`` is supplied - a policy threshold
    such as "minimum length 14" is a requirement being restated, not an observation
    being invented. ``control_requirement`` is *not* treated as supporting evidence:
    letting the model's own restatement license its own statistics would defeat the
    check.

    False-positive suppression, in order of application:

    * identifier-shaped spans (``CONTROL-001``, ``SHA-256``), ``chunk_id`` references,
      ``#12``, ordinals (``1st``), markdown/numbered list markers, cross-references
      (``section 4.2``, ``step 3``, ``page 7``) and locator references (``rows 14, 27
      and 38``) are masked out before scanning;
    * date-shaped spans are judged on their year alone, so ``2024-03-15`` is accepted
      when the evidence writes ``15 March 2024``;
    * a percentage is accepted when the counts behind it are present - ``10%`` is
      supported by a population of ``100`` containing ``10`` exceptions - and the same
      pair is also tested in complement, since ``90 of 100 compliant`` and ``10%
      non-compliant`` are one observation stated two ways. Only integer counts drawn
      from evidence *text*, with dates removed, can play this role, and only
      populations of ten or more can be denominators; without those restrictions a
      scattering of small integers makes every percentage look derivable;
    * thousands separators are stripped, so ``1,250`` and ``1250`` are the same number.

    Honest limits - a thesis examiner should read these as the heuristic's known
    failure modes, not as edge cases:

    * **False positives.** A count the model derived correctly but the evidence never
      states literally will be flagged. If a listing contains five non-compliant rows
      but no total anywhere, "5 accounts failed" is correct arithmetic and will still
      be reported as unsupported. Arithmetic over evidence is exactly what an auditor
      wants a tool to do, so this is the heuristic's most common and most annoying
      error. It is accepted deliberately: the check is a *flag for human attention*,
      never an automatic verdict, and under-flagging would defeat its purpose.
    * **False negatives.** Any number that happens to occur somewhere in a large
      evidence set passes, whatever it was used to claim. With a hundred-row export in
      context, small integers are effectively unfalsifiable, and a fabricated figure
      that coincides with an unrelated row number, page number or timestamp passes
      silently. This was observed directly while building the module: an invented "3
      accounts" survived because a cited chunk happened to sit on page 3. The check is
      therefore a lower bound on unsupported numeric claims, never a clean bill.
    * **Out of scope.** Non-numeric fabrication - an invented policy name, an invented
      approver, a wrong causal claim - is not detected here at all.

    No rate is claimed for either error, because the only populations it has been run
    against are this project's own synthetic datasets, where the figures were chosen by
    the same author who wrote the heuristic.
    """
    vocabulary = _evidence_number_vocabulary(retrieval, control)
    claims: List[str] = []
    seen: Set[Tuple[str, str]] = set()

    for field_name in _CLAIM_FIELDS:
        text = getattr(output, field_name, "") or ""
        if not text.strip():
            continue
        for raw, kind, context in _iter_numeric_claims(text, vocabulary):
            key = (field_name, raw)
            if key in seen:
                continue
            seen.add(key)
            if kind == "percentage":
                explanation = (
                    "no matching figure in the evidence and no pair of evidence counts yields it"
                )
            elif kind == "date":
                explanation = "no evidence carries this date, in any format"
            else:
                explanation = "this figure does not appear anywhere in the retrieved evidence"
            claims.append(
                "{field}: '{raw}' is unsupported - {explanation}. Context: \"{context}\"".format(
                    field=field_name, raw=raw, explanation=explanation, context=context
                )
            )
    return claims


def _iter_numeric_claims(
    text: str, vocabulary: _EvidenceNumbers
) -> Iterable[Tuple[str, str, str]]:
    """Yield ``(raw, kind, context)`` for each *unsupported* figure in ``text``."""
    masked = _mask_ignorable(text)
    masked, date_claims = _resolve_dates(masked, text, vocabulary.present)
    for raw, context in date_claims:
        yield raw, "date", context

    for match in _CLAIM_NUMBER_RE.finditer(masked):
        raw_number = match.group(1)
        marker = match.group(2) or ""
        is_percent = bool(marker)
        value = _to_float(raw_number)
        if value is None:
            continue
        if _number_supported(value, is_percent, vocabulary):
            continue
        raw = raw_number + ("%" if is_percent else "")
        kind = "percentage" if is_percent else "figure"
        yield raw, kind, _context_of(text, match.start(1), match.end(1))


def _mask_ignorable(text: str) -> str:
    """Blank out spans whose digits are never evidential, preserving offsets."""
    masked = text
    for pattern in _IGNORE_PATTERNS:
        masked = pattern.sub(lambda m: " " * (m.end() - m.start()), masked)
    return masked


def _resolve_dates(
    masked: str, original: str, evidence_numbers: Set[float]
) -> Tuple[str, List[Tuple[str, str]]]:
    """Mask date spans; return the ones whose year is absent from the evidence."""
    unsupported: List[Tuple[str, str]] = []
    for pattern in _DATE_PATTERNS:
        out_parts: List[str] = []
        cursor = 0
        for match in pattern.finditer(masked):
            years = [g for g in match.groups() if g and len(g) == 4]
            year = _to_float(years[0]) if years else None
            if year is not None and year not in evidence_numbers:
                unsupported.append(
                    (
                        original[match.start() : match.end()].strip(),
                        _context_of(original, match.start(), match.end()),
                    )
                )
            out_parts.append(masked[cursor : match.start()])
            out_parts.append(" " * (match.end() - match.start()))
            cursor = match.end()
        out_parts.append(masked[cursor:])
        masked = "".join(out_parts)
    return masked, unsupported


def _number_supported(value: float, is_percent: bool, vocabulary: _EvidenceNumbers) -> bool:
    if _in_set(value, vocabulary.present):
        return True
    if not is_percent:
        return False
    # A percentage is a claim about two counts. If the counts are in the evidence the
    # model has done arithmetic, not invention.
    return _percentage_derivable(value, vocabulary.counts)


def _in_set(value: float, numbers: Set[float]) -> bool:
    return any(abs(value - candidate) < 1e-9 for candidate in numbers)


def _percentage_derivable(value: float, counts: Set[int]) -> bool:
    """True when some ``a / b`` of real evidence counts lands on ``value``.

    Both ``a / b`` and its complement ``(b - a) / b`` are accepted from the *same*
    pair, because "10 of 100 failed" and "90% complied" are one observation stated two
    ways. Testing the complement against unrelated numbers, by contrast, would make
    almost any percentage derivable, so it is not done.

    Populations below :data:`_MIN_PERCENT_DENOMINATOR` are rejected as denominators:
    a handful of small integers can hit any target percentage by coincidence. The
    tolerance is near-exact, which means a percentage the model rounded off a real
    division may still be flagged - a deliberate trade in favour of catching invented
    figures.
    """
    if value < 0.0 or value > 100.0:
        return False
    ordered = sorted(counts)
    if len(ordered) > _MAX_PERCENT_PAIR_TERMS:
        # Pairwise search is quadratic; on an evidence set this large the check would
        # accept almost anything anyway, so it is skipped rather than approximated.
        return False
    for denominator in ordered:
        if denominator < _MIN_PERCENT_DENOMINATOR:
            continue
        for numerator in ordered:
            if numerator > denominator:
                break
            share = (float(numerator) / float(denominator)) * 100.0
            if abs(share - value) <= _PERCENT_TOLERANCE or abs((100.0 - share) - value) <= _PERCENT_TOLERANCE:
                return True
    return False


@dataclass
class _EvidenceNumbers:
    """Two views of the numbers in the evidence, used for two different questions.

    ``present`` answers "could the model have read this figure anywhere?" and is
    deliberately permissive - it includes provenance (filenames, rendered locators,
    row numbers) so that a reference like "rows 14, 27 and 38" is never mistaken for an
    invented statistic.

    ``counts`` answers "is this percentage arithmetic over real counts?" and is
    deliberately strict: integers only, drawn from evidence *text* with date spans
    removed. A month number, a page number or a retrieval chunk id is not a population
    count, and letting one act as a denominator makes almost any percentage look
    derivable.
    """

    present: Set[float] = field(default_factory=set)
    counts: Set[int] = field(default_factory=set)


def _evidence_number_vocabulary(retrieval: Optional[RetrievalResult], control: Any = None) -> _EvidenceNumbers:
    """Every number the model could legitimately have read, from any retrieved source."""
    vocabulary = _EvidenceNumbers()
    body_texts: List[str] = []

    if retrieval is not None:
        for chunk in retrieval.chunks:
            body_texts.append(chunk.text)
            _absorb_numbers(chunk.text, vocabulary.present)
            _absorb_numbers(chunk.filename, vocabulary.present)
            locator = chunk.locator
            if locator is not None:
                # render() already spells out page, sheet, rows and columns.
                _absorb_numbers(locator.render(), vocabulary.present)
                for row in locator.row_numbers or []:
                    _absorb_value(row, vocabulary.present)

    if control is not None:
        for key in (
            "name",
            "objective",
            "description",
            "risk_addressed",
            "expected_evidence",
            "assessment_criteria",
            "retrieval_keywords",
            "framework_refs",
        ):
            value = control.get(key) if isinstance(control, dict) else getattr(control, key, None)
            if value is None:
                continue
            items = value if isinstance(value, (list, tuple)) else [value]
            for item in items:
                body_texts.append(str(item))
                _absorb_numbers(str(item), vocabulary.present)

    dateless: Set[float] = set()
    for text in body_texts:
        _absorb_numbers(_mask_dates(text), dateless)
    vocabulary.counts = {int(value) for value in dateless if float(value).is_integer() and value >= 0}
    return vocabulary


def _mask_dates(text: str) -> str:
    """Blank out date spans so their components never become population counts."""
    masked = text
    for pattern in _DATE_PATTERNS:
        masked = pattern.sub(lambda m: " " * (m.end() - m.start()), masked)
    return masked


def _absorb_numbers(text: str, numbers: Set[float]) -> None:
    if not text:
        return
    cleaned = _THOUSANDS_RE.sub("", text)
    for token in _ANY_NUMBER_RE.findall(cleaned):
        value = _to_float(token)
        if value is not None:
            numbers.add(value)


def _absorb_value(value: Any, numbers: Set[float]) -> None:
    if value is None:
        return
    try:
        numbers.add(float(value))
    except (TypeError, ValueError):
        return


def _to_float(token: str) -> Optional[float]:
    try:
        return float(token.replace(",", ""))
    except (TypeError, ValueError, AttributeError):
        return None


def _context_of(text: str, start: int, end: int, window: int = 45) -> str:
    left = max(0, start - window)
    right = min(len(text), end + window)
    snippet = " ".join(text[left:right].split())
    if left > 0:
        snippet = "..." + snippet
    if right < len(text):
        snippet = snippet + "..."
    return snippet


# ---- safety rails


def enforce_safety_rails(output: AssessmentOutput, report: ValidationReport) -> AssessmentOutput:
    """Return a corrected copy of ``output``; never mutate the caller's object.

    The original model output is the audit trail - what the system claims about
    hallucination rates is only checkable if the unedited answer survives - so this
    works on ``model_copy(deep=True)`` and leaves the caller's instance untouched.
    ``report.rails_applied`` accumulates a plain-language reason for every intervention
    so the UI and the report can show what was changed and why.

    Rails, in order:

    1. An ``EFFECTIVE`` or ``NOT_EFFECTIVE`` conclusion with no grounding is downgraded
       to ``INSUFFICIENT_EVIDENCE``. Both directions are covered on purpose: an
       unsupported clean opinion and an unsupported accusation are equally unsafe.
       ``POTENTIAL_DEFICIENCY`` is deliberately left alone - it already asserts only a
       possibility, and downgrading a hedged flag would suppress exactly the signal an
       auditor most needs to see.
    2. Fabricated citations and unsupported numeric claims are recorded and pushed into
       ``human_verification_required``. They are never edited out: a fabricated citation
       that quietly disappeared would be worse than one left visible and labelled.
    3. If nothing at all was cited, :data:`NO_EVIDENCE_SENTINEL` is written into the
       assessment and finding text so no downstream reader can mistake silence for a
       clean result.
    4. ``human_review_required`` is forced true, unconditionally and always.
    """
    guarded = output.model_copy(deep=True)
    rails: List[str] = []
    report.original_status = output.status.value

    # Deliberately the *strict* test: a conclusion resting entirely on partial matches
    # is ungrounded for the purpose of this rail, even though such citations earn half
    # credit in the headline grounding_rate. The two figures answer different questions
    # ("how well quoted was this?" versus "may this conclusion stand?") and the rail
    # takes the conservative one.
    ungrounded = report.verified == 0

    if guarded.status in _DEFINITE_STATUSES and ungrounded:
        reason = (
            "no citations at all"
            if report.total == 0
            else "{0} citation(s), none of which could be verified against the retrieved evidence".format(
                report.total
            )
        )
        rails.append(
            "status_downgraded: '{0}' asserted a definite conclusion on {1}; downgraded to "
            "INSUFFICIENT_EVIDENCE because this system may not conclude on ungrounded reasoning.".format(
                guarded.status.value, reason
            )
        )
        report.status_downgraded = True
        guarded.status = AssessmentStatus.INSUFFICIENT_EVIDENCE
        guarded.confidence = ConfidenceLevel.LOW
        guarded.evidence_sufficiency = (
            EvidenceSufficiency.NONE if report.total == 0 else EvidenceSufficiency.INSUFFICIENT
        )
        _add_verification_item(
            guarded,
            "Re-test this control against source evidence: the AI conclusion was withdrawn because "
            "it could not be grounded in the supplied evidence.",
        )

    if report.fabricated:
        indices = ", ".join(str(c.order_index + 1) for c in report.citations if c.is_fabricated)
        rails.append(
            "fabricated_citations: {0} citation(s) could not be resolved to the retrieved evidence "
            "(position {1}). They are retained and flagged, not removed.".format(report.fabricated, indices)
        )
        _add_verification_item(
            guarded,
            "Disregard citation(s) {0} - they do not resolve to supplied evidence - and re-check any "
            "conclusion that relied on them.".format(indices),
        )

    if report.partial:
        rails.append(
            "partial_citations: {0} citation(s) only partially matched their chunk; the quoted wording "
            "is not verbatim.".format(report.partial)
        )

    if report.unsupported_claims:
        rails.append(
            "unsupported_numeric_claims: {0} figure(s) in the narrative do not appear in the retrieved "
            "evidence.".format(len(report.unsupported_claims))
        )
        _add_verification_item(
            guarded,
            "Recompute the figures flagged as unsupported ({0}) directly from the source evidence.".format(
                "; ".join(claim.split(" - ")[0] for claim in report.unsupported_claims[:5])
            ),
        )

    if not guarded.has_evidence:
        rails.append(
            "no_evidence_sentinel: nothing was cited, so the evidence statement was replaced with the "
            "no-evidence sentinel."
        )
        guarded.evidence_sufficiency = EvidenceSufficiency.NONE
        guarded.assessment = _with_sentinel(guarded.assessment)
        guarded.finding = _with_sentinel(guarded.finding)
        _add_verification_item(
            guarded,
            "Obtain and supply evidence for this control; no citable evidence was available to the AI.",
        )

    # Unconditional. This system never issues a final compliance decision, so this rail
    # is recorded on every assessment, clean or not.
    guarded.human_review_required = True
    rails.append(
        "human_review_enforced: every assessment produced by this system requires auditor review "
        "before it can be relied upon."
    )

    if len(rails) > 1:
        guarded.limitations = _append_line(
            guarded.limitations,
            "Automated validation altered or annotated this assessment; see the validation report for "
            "the rails applied.",
        )

    for rail in rails:
        if rail not in report.rails_applied:
            report.rails_applied.append(rail)
    return guarded


def validate_and_enforce(
    output: AssessmentOutput,
    retrieval: RetrievalResult,
    control: Any = None,
) -> Tuple[AssessmentOutput, ValidationReport]:
    """Run the whole grounding pass: citations, numeric claims, then the rails.

    Convenience for the assessment engine, which always needs all three together. The
    returned ``AssessmentOutput`` is the guarded copy; ``output`` is left as the
    unedited record of what the model actually said.
    """
    report = validate_citations(output, retrieval)
    report.unsupported_claims = detect_unsupported_claims(output, retrieval, control)
    guarded = enforce_safety_rails(output, report)
    return guarded, report


def _add_verification_item(output: AssessmentOutput, item: str) -> None:
    if item not in output.human_verification_required:
        output.human_verification_required.append(item)


def _with_sentinel(text: str) -> str:
    current = (text or "").strip()
    if NO_EVIDENCE_SENTINEL in current:
        return current
    if not current:
        return NO_EVIDENCE_SENTINEL
    return "{0} {1}".format(NO_EVIDENCE_SENTINEL, current)


def _append_line(text: str, line: str) -> str:
    current = (text or "").strip()
    if line in current:
        return current
    return line if not current else "{0} {1}".format(current, line)


__all__ = [
    "PARTIAL_CITATION_CREDIT",
    "CitationCheck",
    "ValidationReport",
    "detect_unsupported_claims",
    "enforce_safety_rails",
    "grounding_score",
    "longest_common_substring_ratio",
    "normalise_text",
    "token_overlap_ratio",
    "validate_and_enforce",
    "validate_citations",
]
