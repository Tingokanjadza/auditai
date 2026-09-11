"""The linguistic contract that turns a language model into an audit assistant.

Everything downstream of this module - the JSON schema, the citation validator, the
safety rails, the human-review gate - can only check what the model was *asked* to
produce. This module is where the research principle is actually stated in words:

    a statement about what a control REQUIRES, a statement about what the evidence
    PROVES, a statement the assistant INFERS, and a statement that a HUMAN must
    VERIFY are four different kinds of claim, and blurring them is the failure mode
    this system exists to prevent.

Two design consequences worth stating up front:

* ``SYSTEM_PROMPT`` carries an explicit worked example of the case that fails most
  often in practice - a privileged-account listing that simply has no MFA column.
  The tempting wrong answer ("no MFA shown, therefore MFA is missing") converts a gap
  in the *export* into a finding about the *control*. The example is a few-shot anchor
  precisely because rules alone do not reliably prevent it.
* ``build_raw_prompt`` is deliberately weak. It is the Experiment A control condition,
  and it must stay naive for the comparison to mean anything (see its docstring).

``PROMPT_VERSION`` and :func:`prompt_metadata` exist so a result set in the write-up
can be pinned to the exact wording that produced it. A prompt change is a change to
the instrument, and an unversioned instrument is not reproducible.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from app.config import get_settings
from app.rag.base import RetrievalResult, RetrievedChunk
from app.schemas.assessment import (
    ASSESSMENT_JSON_TEMPLATE,
    SELF_CRITIQUE_JSON_TEMPLATE,
    SUFFICIENCY_JSON_TEMPLATE,
    AssessmentOutput,
)
from app.schemas.enums import ExperimentMode, SourceType

#: Bump on ANY change to the wording below. Results produced by different prompt
#: versions are not directly comparable and must not be pooled in the write-up.
PROMPT_VERSION = "1.0.0"

#: Chunk types that are never truncated: they are small and each one carries
#: population-level facts (row counts, column names, per-column value tallies) that a
#: model cannot reconstruct from the row chunks it can actually see.
PROTECTED_SOURCE_TYPES = (SourceType.TABLE_SUMMARY.value,)

#: Below this many characters a truncated chunk is more misleading than useful, so the
#: renderer would rather drop a low-ranked chunk (and say so) than keep a stub.
MIN_CHUNK_TEXT_CHARS = 240

#: Characters held back from the evidence budget for the trailing truncation note, so
#: that the note itself cannot push the block over ``max_chars``.
_NOTE_RESERVE_CHARS = 760

#: Floor on the evidence budget. Below this a block cannot hold one citable chunk *and*
#: the statement of which chunk IDs exist, and truncating that statement is worse than
#: overspending: a half-printed ID list ("...ids are: 7, 1") is an invitation to cite a
#: chunk that does not exist. A smaller ``max_chars`` is clamped up to this and the
#: clamp is recorded in :attr:`EvidenceBlock.notes`.
MIN_EVIDENCE_BUDGET_CHARS = 800

#: Below this budget the preamble switches to a terse form; the ID list survives either way.
_COMPACT_PREAMBLE_BUDGET = 1500


# ---- the system prompt: the four-way separation rule and the hard rules

SYSTEM_PROMPT = """You are an IT audit assistant working under the supervision of a qualified human IT auditor.

You do not issue audit opinions. You prepare grounded, checkable working-paper input that a human
auditor will review, correct, and sign. Everything you write is mechanically re-checked against the
evidence you were given: cited chunk IDs are resolved, quotations are matched back to the source text,
and numbers are looked for in the evidence. Write accordingly.

## THE FOUR KINDS OF STATEMENT

There are exactly four kinds of statement in an audit working paper. Keep them strictly apart. Never
let one drift into another, and never write a sentence that mixes two of them.

1. WHAT THE CONTROL / POLICY REQUIRES.
   The obligation itself. Goes in `control_requirement`. It comes from the control definition you were
   given and from any policy document in the evidence. A requirement is not an observation: the fact
   that a policy demands something is not the fact that it happened.

2. WHAT THE EVIDENCE PROVES.
   Only what the supplied evidence literally states. Goes in `assessment`, and in `evidence[]` as
   verbatim quotations tagged with the chunk_id they were copied from. If you cannot point at a chunk
   and quote it, it is not proven, and it does not belong in `assessment`.

3. WHAT YOU INFER.
   Every conclusion that travels even one step beyond the literal words of the evidence. Goes in
   `inferences`. "The export lists 10 accounts with MFA_ENABLED = FALSE, therefore 10 privileged
   accounts are not covered by MFA" is a small inference from a quotation and must still be labelled.
   "The organisation does not enforce its access policy" is a very large inference and must be
   labelled as one, or not made at all.

4. WHAT A HUMAN MUST VERIFY.
   Everything you could not establish, plus every check a reviewer must perform before this work can
   be relied on. Goes in `human_verification_required` and `missing_evidence`. This field is not a
   formality: it is where you record the limits of what you actually saw.

## HARD RULES

- NEVER INVENT EVIDENCE. Not a document, not a system name, not a row, not a date, not a percentage,
  not a count, not a column that would have been convenient.
- NEVER CITE A chunk_id THAT WAS NOT SUPPLIED. The EVIDENCE section lists every chunk ID that exists
  for this assessment. Any other ID is a fabrication and will be recorded as one.
- QUOTE VERBATIM OR NOT AT ALL. `quoted_text` must be characters copied exactly out of the cited
  chunk. Paraphrase belongs in `assessment` or `reasoning`, never inside a quotation.
- EVERY NUMBER you state must be readable in, or directly countable from, the quoted evidence. If you
  are estimating or extrapolating, put the claim in `inferences` and say what it rests on.
- IF THE SUPPLIED EVIDENCE DOES NOT CONTAIN THE ATTRIBUTE UNDER TEST, THE ANSWER IS
  INSUFFICIENT_EVIDENCE AND NOT A COMPLIANCE VERDICT. "I cannot tell from this evidence" is a
  complete, correct, professional answer. Reaching for a verdict anyway is the worst thing you can do.
- ABSENCE OF EVIDENCE IS NOT EVIDENCE OF A DEFICIENCY. Evidence that does not report an attribute is
  silent about it. Silence is not failure. Name what is missing and ask for it.
- CONFIGURATION IS NOT OPERATION. Evidence that a setting is configured today does not establish that
  the control operated throughout the audit period. Say which of the two you have.
- A SAMPLE IS NOT A POPULATION. You cannot establish that the evidence is complete, authentic, or
  representative of the whole population. Assume none of these; record them as limitations.
- YOU ARE NOT AUTHORISED TO STATE THAT AN ORGANISATION IS COMPLIANT, OR NON-COMPLIANT, WITH ANY LAW,
  REGULATION, STANDARD OR FRAMEWORK. You may describe what the evidence shows against the control's
  own stated criteria. Nothing beyond that. Framework references, where given, are informative
  cross-references only, never a basis for a compliance statement.
- A HUMAN AUDITOR MAKES EVERY FINAL DECISION. `human_review_required` is always true. You are drafting
  input to a review, not concluding an audit.

## STATUS VOCABULARY

- EFFECTIVE - the cited evidence shows the control operating as required, with no exceptions visible
  in that evidence, for the population and period the evidence actually covers.
- POTENTIAL_DEFICIENCY - the cited evidence contains specific, quotable exceptions to the requirement.
- NOT_EFFECTIVE - the cited evidence shows the control as configured or operated does not meet the
  requirement at all (for example, a configured value below the required threshold).
- INSUFFICIENT_EVIDENCE - the evidence does not contain the attribute under test, or does not cover
  the population or period well enough to conclude either way. This is the correct answer whenever you
  are reaching. Prefer it when in doubt.
- NOT_APPLICABLE - the control does not apply to the environment the evidence describes.

## WORKED EXAMPLE - the case that is most often got wrong

EVIDENCE SUPPLIED (abbreviated):

[chunk_id: 7] SOURCE: Access_Control_Policy.pdf - page 4 - section '4.2 Privileged Access'
    All privileged and administrative accounts must be protected by multi-factor authentication.

[chunk_id: 12] SOURCE: Privileged_Accounts_Q3.csv - sheet 'Accounts' - rows 2-26 - columns: username,
    full_name, role, department, account_created
    svc-account-014, Service Account 14, Domain Administrator, IT Infrastructure, 2023-02-11
    p.adeyemi, [synthetic name], Database Administrator, IT Operations, 2022-08-30
    ... 23 further rows with the same five columns ...

CORRECT ANSWER: status = "INSUFFICIENT_EVIDENCE"

Correct reasoning:
  1. REQUIRES: chunk 7 establishes the requirement - MFA on all privileged accounts.
  2. PROVES: chunk 12 establishes that a population of privileged accounts exists and lists it. That
     is all it establishes.
  3. The attribute under test is whether MFA is enabled on each of those accounts. Chunk 12 has no
     MFA column, and no other supplied chunk reports MFA status. The attribute is simply absent.
  4. Therefore no conclusion about the operation of the control is available.
  5. missing_evidence must name what would be needed: an MFA enrolment or authentication-method
     export per privileged account, covering the audit period, reconcilable to this account listing.
  6. human_verification_required must ask the auditor to obtain that export and to confirm that the
     account listing is the complete privileged population.

WRONG ANSWERS, AND WHY THEY ARE WRONG:
  - "POTENTIAL_DEFICIENCY - the listing does not show MFA, so MFA is probably not enabled."
    This converts a gap in the export into a finding about the control. Absence of evidence is not
    evidence of a deficiency.
  - "EFFECTIVE - the policy requires MFA and a privileged account listing was produced."
    This confuses the requirement with the observation. Producing a listing proves nothing about MFA.
  - Any answer quoting an "mfa_enabled" or "MFA_Status" column from chunk 12. That column does not
    exist in the evidence. Writing it is a fabricated citation, and it is the single most damaging
    thing you can do in this system.

## OUTPUT DISCIPLINE

Return exactly one JSON object matching the requested schema. No prose before it, no prose after it,
no markdown code fences. Every field must be present. Use empty strings and empty lists where you have
nothing to say - never omit a field, and never fill one with invented content."""


#: Experiment A must NOT see the system prompt above; that scaffolding is the very
#: thing the experiment is measuring. The engine pairs :func:`build_raw_prompt` with
#: this deliberately ordinary persona instead.
RAW_BASELINE_SYSTEM_PROMPT = (
    "You are an IT auditor. You review evidence and assess whether IT controls are working. "
    "Answer in JSON."
)


# ---- evidence rendering


@dataclass
class EvidenceBlock:
    """The rendered evidence section plus an account of what had to be cut.

    The engine and the UI need more than the string: for the research record they need
    to know which chunks the model could actually see in full, because a truncated
    chunk is a plausible explanation for a missed exception.
    """

    text: str
    included_chunk_ids: List[int] = field(default_factory=list)
    truncated_chunk_ids: List[int] = field(default_factory=list)
    dropped_chunk_ids: List[int] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    char_count: int = 0
    budget: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "char_count": self.char_count,
            "budget": self.budget,
            "included_chunk_ids": list(self.included_chunk_ids),
            "truncated_chunk_ids": list(self.truncated_chunk_ids),
            "dropped_chunk_ids": list(self.dropped_chunk_ids),
            "notes": list(self.notes),
        }


NO_EVIDENCE_BLOCK = (
    "EVIDENCE SUPPLIED: none.\n"
    "No evidence chunks were retrieved for this control. You therefore have nothing to quote and\n"
    "nothing to cite. The only defensible status is INSUFFICIENT_EVIDENCE. Leave `evidence` empty,\n"
    "and use `missing_evidence` to state precisely which artefacts would be needed to test this\n"
    "control. Do not describe evidence you were not given."
)


def render_evidence_block(retrieval: Optional[RetrievalResult], max_chars: Optional[int] = None) -> str:
    """Render retrieved chunks as a citable evidence block within ``max_chars``.

    Each chunk is printed as a delimited block headed
    ``[chunk_id: N] SOURCE: <locator.render()>`` followed by its text, because the
    chunk ID and the locator together are the only things that make a later citation
    checkable.

    Budgeting is max-min fair rather than rank-greedy: short chunks are kept whole and
    the *longest* chunks are truncated first, so a single 40 000-character table export
    cannot silently evict the two-paragraph policy chunk that states the requirement.
    ``TABLE_SUMMARY`` chunks are reserved whole before anything else is allocated - they
    are small, and they are the only place a population-level fact (row counts, column
    names, "this file has no MFA column") is available. They are truncated or dropped
    only in the pathological case where the budget cannot hold even them, which the
    returned :class:`EvidenceBlock` records.

    When anything is cut, the block ends with an explicit note naming what was cut. The
    note is inside the returned string on purpose: the model must not be able to read a
    truncated table as a complete population.

    The returned string never exceeds ``max(max_chars, MIN_EVIDENCE_BUDGET_CHARS)``. The
    floor exists because a block below it cannot both list the valid chunk IDs and show
    one usable extract, and a half-printed ID list would invite exactly the fabricated
    citation this block is designed to prevent.
    """
    return build_evidence_block(retrieval, max_chars).text


def build_evidence_block(
    retrieval: Optional[RetrievalResult], max_chars: Optional[int] = None
) -> EvidenceBlock:
    """:func:`render_evidence_block` with the bookkeeping kept, not thrown away."""
    settings = get_settings()
    requested = int(max_chars) if max_chars is not None else int(settings.max_evidence_chars)
    budget = max(requested, MIN_EVIDENCE_BUDGET_CHARS)
    meta_notes: List[str] = []
    if budget != requested:
        meta_notes.append(
            "Requested evidence budget of {0} characters was clamped up to {1}: below that a block "
            "cannot state which chunk IDs exist without truncating the ID list.".format(requested, budget)
        )

    chunks: List[RetrievedChunk] = list(getattr(retrieval, "chunks", []) or [])
    if not chunks:
        return EvidenceBlock(
            text=NO_EVIDENCE_BLOCK,
            notes=meta_notes,
            char_count=len(NO_EVIDENCE_BLOCK),
            budget=budget,
        )

    compact = budget < _COMPACT_PREAMBLE_BUDGET
    overheads = [len(_render_chunk(chunk, "")) + 1 for chunk in chunks]  # +1 for the joining newline
    lengths = [len(chunk.text or "") for chunk in chunks]

    full_size = len(_evidence_preamble(retrieval, chunks, compact)) + sum(overheads) + sum(lengths)
    reserve = 0 if full_size <= budget else min(_NOTE_RESERVE_CHARS, budget // 3)

    kept = list(range(len(chunks)))
    dropped: List[int] = []
    # Dropping is the last resort: budget is taken out of the longest chunks first
    # (see _allocate_text_budget), and only when even a minimally useful extract will
    # not fit does a chunk get removed - lowest-ranked first, TABLE_SUMMARY chunks last,
    # and never the final remaining chunk.
    while len(kept) > 1:
        overhead = sum(overheads[i] for i in kept)
        preamble_len = len(_evidence_preamble(retrieval, [chunks[i] for i in kept], compact))
        if budget - reserve - preamble_len - overhead >= MIN_CHUNK_TEXT_CHARS * len(kept):
            break
        # Preference order for removal: lowest-ranked ordinary chunk, then a
        # TABLE_SUMMARY, then (only if nothing else is left) the top-ranked chunk.
        droppable = [i for i in reversed(kept) if i != kept[0] and not _is_protected(chunks[i])]
        if not droppable:
            droppable = [i for i in reversed(kept) if i != kept[0]]
        if not droppable:
            break
        victim = droppable[0]
        kept.remove(victim)
        dropped.append(victim)
        reserve = min(_NOTE_RESERVE_CHARS, budget // 3)

    preamble = _evidence_preamble(retrieval, [chunks[i] for i in kept], compact)
    text_budget = max(0, budget - reserve - len(preamble) - sum(overheads[i] for i in kept))
    allocations = _allocate_text_budget(chunks, kept, lengths, text_budget)

    rendered: List[str] = [preamble]
    truncated: List[Tuple[int, int, int]] = []  # (chunk_id, shown, total)
    for index in kept:
        chunk = chunks[index]
        body, omitted = _truncate_text(chunk.text or "", allocations[index])
        if omitted:
            truncated.append((chunk.chunk_id, lengths[index] - omitted, lengths[index]))
        rendered.append(_render_chunk(chunk, body))

    note = _budget_notes(truncated, len(dropped), budget, reserve)
    if note:
        rendered.append(note)
        meta_notes.append(note)
    elif truncated or dropped:
        meta_notes.append(
            "Evidence was truncated or dropped but the budget left no room to say so in the prompt."
        )

    text = "\n".join(rendered)
    return EvidenceBlock(
        text=text,
        included_chunk_ids=[chunks[i].chunk_id for i in kept],
        truncated_chunk_ids=[cid for cid, _, _ in truncated],
        dropped_chunk_ids=[chunks[i].chunk_id for i in dropped],
        notes=meta_notes,
        char_count=len(text),
        budget=budget,
    )


def _is_protected(chunk: RetrievedChunk) -> bool:
    source_type = (chunk.source_type or chunk.locator.source_type or "").upper()
    return source_type in PROTECTED_SOURCE_TYPES


def _evidence_preamble(
    retrieval: Optional[RetrievalResult], chunks: Sequence[RetrievedChunk], compact: bool = False
) -> str:
    ids = ", ".join(str(c.chunk_id) for c in chunks)
    if compact:
        return (
            "EVIDENCE SUPPLIED ({count} chunk(s)). The ONLY chunk_id values that exist are: {ids}. "
            "Citing any other value is a fabrication. Chunks are extracts, not whole documents."
        ).format(count=len(chunks), ids=ids)
    strategy = getattr(retrieval, "strategy", "") or "unspecified"
    files = sorted({c.filename or c.locator.filename or "(unknown file)" for c in chunks})
    return (
        "EVIDENCE SUPPLIED ({count} chunk(s), retrieval strategy: {strategy}).\n"
        "Source files represented: {files}.\n"
        "These chunks are the complete set of evidence available to you for this control. The ONLY\n"
        "chunk_id values that exist are: {ids}. Citing any other value is a fabrication.\n"
        "Chunks are extracts, not whole documents: what is not shown here has not been shown to you."
    ).format(count=len(chunks), strategy=strategy, files=", ".join(files), ids=ids)


def _render_chunk(chunk: RetrievedChunk, body: str) -> str:
    """One delimited, citable evidence block. ``body=""`` measures the fixed overhead."""
    descriptors: List[str] = []
    source_type = chunk.source_type or chunk.locator.source_type
    if source_type:
        descriptors.append("content: {0}".format(source_type))
    if chunk.evidence_type:
        descriptors.append("evidence type: {0}".format(chunk.evidence_type))
    if chunk.rank:
        descriptors.append("retrieval rank: {0}".format(chunk.rank))
    meta = " | ".join(descriptors)
    lines = [
        "----- EVIDENCE CHUNK -----",
        "[chunk_id: {0}] SOURCE: {1}".format(chunk.chunk_id, chunk.locator.render()),
    ]
    if meta:
        lines.append("({0})".format(meta))
    lines.append("TEXT:")
    lines.append(body)
    lines.append("----- END OF CHUNK {0} -----".format(chunk.chunk_id))
    return "\n".join(lines)


def _allocate_text_budget(
    chunks: Sequence[RetrievedChunk],
    kept: Sequence[int],
    lengths: Sequence[int],
    text_budget: int,
) -> Dict[int, int]:
    """Max-min fair allocation with ``TABLE_SUMMARY`` chunks reserved whole.

    Water-filling: every chunk shorter than the fair share is kept intact and its
    surplus is redistributed, so only the genuinely long chunks are cut, and they are
    all cut back to roughly the same cap. This is the opposite of the obvious
    rank-greedy approach, which spends the whole budget on rank 1 and 2.
    """
    allocations: Dict[int, int] = {i: 0 for i in kept}
    protected = [i for i in kept if _is_protected(chunks[i])]
    protected_total = sum(lengths[i] for i in protected)

    remaining_budget = text_budget
    flexible = [i for i in kept if i not in protected]
    if protected and protected_total <= text_budget:
        for i in protected:
            allocations[i] = lengths[i]
        remaining_budget -= protected_total
    else:
        # Pathological budget: nothing can be protected, everything competes equally.
        flexible = list(kept)

    remaining_count = len(flexible)
    for i in sorted(flexible, key=lambda idx: lengths[idx]):
        share = remaining_budget // max(1, remaining_count)
        allocation = min(lengths[i], share)
        allocations[i] = allocation
        remaining_budget -= allocation
        remaining_count -= 1
    return allocations


def _truncate_text(text: str, budget: int) -> Tuple[str, int]:
    """Cut ``text`` to ``budget`` characters, keeping the head *and* the tail.

    Head-only truncation is dangerous for audit evidence: in a row export the exception
    rows are as likely to be at the end as at the start, and a model shown only the
    first 60% of a table will confidently report a clean population. Keeping both ends
    with a loud marker between them at least makes the gap visible.
    """
    total = len(text)
    if budget >= total:
        return text, 0
    if budget <= 0:
        return "", total

    long_marker = "\n[... {0} characters of this chunk omitted to fit the evidence budget; do not\n" \
                  "assume anything about the omitted text - request the full source if it matters ...]\n"
    # Reserve using the widest possible number so the result cannot exceed the budget.
    reserve = len(long_marker.format(total))
    content_budget = budget - reserve
    if content_budget < 80:
        short_marker = "\n[... truncated ...]\n"
        content_budget = budget - len(short_marker)
        if content_budget <= 0:
            return text[:budget], total - budget
        head = text[:content_budget]
        return head + short_marker, total - len(head)

    head_budget = int(content_budget * 0.65)
    tail_budget = content_budget - head_budget

    head = text[:head_budget]
    break_at = head.rfind("\n")
    if break_at > head_budget // 2:
        head = head[:break_at]

    tail = text[total - tail_budget:]
    break_at = tail.find("\n")
    if 0 <= break_at < tail_budget // 2:
        tail = tail[break_at + 1:]

    omitted = total - len(head) - len(tail)
    return head + long_marker.format(omitted) + tail, omitted


def _budget_notes(
    truncated: Sequence[Tuple[int, int, int]], dropped_count: int, budget: int, reserve: int
) -> str:
    """State what was cut, in the prompt, within the reserved character allowance.

    Dropped chunks are reported as a count, never as a list of IDs: an ID the model has
    seen but whose text it has not is exactly the material a fabricated citation is made
    of. The IDs are kept on the returned :class:`EvidenceBlock` for the research record.
    """
    if not truncated and not dropped_count:
        return ""
    lines = ["----- EVIDENCE BUDGET NOTE -----"]
    if truncated:
        shown = ", ".join(
            "chunk {0} ({1} of {2} chars shown)".format(cid, kept, total)
            for cid, kept, total in truncated[:6]
        )
        if len(truncated) > 6:
            shown += ", and {0} more".format(len(truncated) - 6)
        lines.append(
            "{0} chunk(s) were truncated to fit a {1}-character evidence budget: {2}.".format(
                len(truncated), budget, shown
            )
        )
    if dropped_count:
        lines.append(
            "{0} further retrieved chunk(s) did not fit and are not shown here at all. You have not "
            "seen them and must not cite them.".format(dropped_count)
        )
    lines.append(
        "Truncated or omitted text may contain exceptions you have not seen. Treat any population "
        "count drawn from a truncated chunk as an inference, not as proven, and record the gap under "
        "human_verification_required."
    )
    lines.append("----- END OF NOTE -----")
    note = "\n".join(lines)
    if len(note) <= reserve:
        return note
    compact = (
        "----- EVIDENCE BUDGET NOTE: evidence was truncated to fit the budget; omitted text may "
        "contain exceptions you have not seen and must not be assumed clean. -----"
    )
    return compact if len(compact) <= reserve else ""


# ---- control / project rendering


def _get(source: Any, name: str, default: Any = None) -> Any:
    """Read a field from either an ORM row or a plain dict (both are passed in)."""
    if source is None:
        return default
    if isinstance(source, dict):
        value = source.get(name, default)
    else:
        value = getattr(source, name, default)
    return default if value is None else value


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value)]


def _one_line(text: str, limit: int = 240) -> str:
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."


def render_control_requirement(control: Any) -> str:
    """The CONTROL REQUIREMENT section: what is being tested, and against what criteria."""
    control_ref = _get(control, "control_id", "") or "(unreferenced control)"
    lines = [
        "----- CONTROL REQUIREMENT -----",
        "Control reference: {0}".format(control_ref),
        "Control name: {0}".format(_get(control, "name", "") or "(unnamed)"),
    ]
    category = _get(control, "category", "")
    control_type = _get(control, "control_type", "")
    frequency = _get(control, "control_frequency", "")
    descriptors = [d for d in (category, control_type, frequency) if d]
    if descriptors:
        lines.append("Classification: {0}".format(" | ".join(str(d) for d in descriptors)))

    objective = _get(control, "objective", "")
    if objective:
        lines.append("")
        lines.append("Control objective (what the control is for):")
        lines.append(str(objective).strip())

    description = _get(control, "description", "")
    if description:
        lines.append("")
        lines.append("Control description (what the control requires):")
        lines.append(str(description).strip())

    risk_addressed = _get(control, "risk_addressed", "")
    if risk_addressed:
        lines.append("")
        lines.append("Risk this control addresses:")
        lines.append(str(risk_addressed).strip())

    expected = _as_list(_get(control, "expected_evidence", []))
    lines.append("")
    if expected:
        lines.append("Evidence an auditor would expect to see for this control:")
        for item in expected:
            lines.append("  - {0}".format(item))
        lines.append(
            "If an expected artefact is not in the EVIDENCE section, it was not supplied. Record it "
            "under missing_evidence rather than assuming what it would have said."
        )
    else:
        lines.append("Expected evidence: not specified in the control definition.")

    criteria = _as_list(_get(control, "assessment_criteria", []))
    lines.append("")
    if criteria:
        lines.append("Assessment criteria - judge the evidence against each of these, in order:")
        for number, item in enumerate(criteria, start=1):
            lines.append("  {0}. {1}".format(number, item))
        lines.append(
            "State explicitly, in `reasoning`, which criteria the evidence can address and which it "
            "cannot. A criterion the evidence is silent about cannot be marked as met."
        )
    else:
        lines.append(
            "Assessment criteria: none specified. Test against the control description alone and say "
            "so in `limitations`."
        )

    refs = _as_list(_get(control, "framework_refs", []))
    if refs:
        lines.append("")
        lines.append("Informative cross-references: {0}".format(", ".join(refs)))
        lines.append(
            "These are non-authoritative pointers recorded in a synthetic control library. They are "
            "NOT an official mapping and must never be used to state compliance with a framework."
        )
    lines.append("----- END OF CONTROL REQUIREMENT -----")
    return "\n".join(lines)


def render_audit_context(project: Any = None, mode: Any = None) -> str:
    """Scope, period and run configuration - the frame the conclusion is limited to."""
    lines = ["----- AUDIT CONTEXT -----"]
    if project is None:
        lines.append("No audit project context was supplied with this request.")
    else:
        lines.append("Audit project: {0}".format(_get(project, "name", "") or "(unnamed project)"))
        audit_area = _get(project, "audit_area", "")
        if audit_area:
            lines.append("Audit area: {0}".format(audit_area))
        period = _get(project, "period_label", "")
        if not period:
            start = _get(project, "period_start", "")
            end = _get(project, "period_end", "")
            if start or end:
                period = "{0} to {1}".format(start or "?", end or "?")
        if period:
            lines.append("Audit period: {0}".format(period))
        auditor = _get(project, "auditor_name", "")
        if auditor:
            lines.append("Responsible auditor (the human who will review your draft): {0}".format(auditor))
        scope_note = _get(project, "scope_note", "")
        if scope_note:
            lines.append("Scope note: {0}".format(_one_line(scope_note, 400)))

    resolved_mode = ExperimentMode.coerce(mode, None)
    if resolved_mode is not None:
        lines.append("Assessment configuration: {0}".format(resolved_mode.label))
    lines.append(
        "Your conclusion is limited to this evidence, this population and this period. If the evidence "
        "does not clearly cover the audit period, say so rather than assuming it does."
    )
    lines.append("----- END OF AUDIT CONTEXT -----")
    return "\n".join(lines)


# ---- prompt builders


def build_assessment_prompt(
    control: Any,
    retrieval: Optional[RetrievalResult],
    mode: Any = ExperimentMode.B_RAG,
    project: Any = None,
) -> str:
    """The Experiment B / C assessment prompt: requirement, context, evidence, contract.

    Modes B and C share this prompt deliberately. B is one call with this prompt; C
    wraps the same call in a sufficiency pre-check, a self-critique and the validator
    rails. Keeping the assessment wording identical is what allows a B-vs-C difference
    to be attributed to the *workflow* rather than to two differently worded prompts.
    """
    settings = get_settings()
    control_ref = _get(control, "control_id", "") or ""
    block = build_evidence_block(retrieval, settings.max_evidence_chars)

    sections = [
        "TASK: assess one IT control against the evidence supplied below, and return the structured "
        "JSON assessment described at the end of this message.",
        "",
        render_control_requirement(control),
        "",
        render_audit_context(project, mode),
        "",
        block.text,
        "",
        _assessment_output_contract(control_ref, block),
    ]
    return "\n".join(sections)


def _assessment_output_contract(control_ref: str, block: EvidenceBlock) -> str:
    valid_ids = ", ".join(str(c) for c in block.included_chunk_ids) or "(none - no evidence was supplied)"
    lines = [
        "----- HOW TO ANSWER -----",
        "Work in this order, and keep the four kinds of statement separate as instructed:",
        "  1. control_requirement - restate only what the control requires. No evidence, no inference.",
        "  2. evidence[] - copy the verbatim excerpts you are relying on, each with the chunk_id it "
        "came from and why it matters. Cite the requirement text as well as the observations.",
        "  3. assessment - state only what those quoted excerpts show.",
        "  4. inferences - list every step you took beyond the literal text of the quotations.",
        "  5. missing_evidence and human_verification_required - state what you did not have and what "
        "the auditor must check.",
        "  6. status, finding, risk, risk_level, recommendation, confidence, limitations.",
        "",
        "Constraints for this specific assessment:",
        "  - Set control_id to exactly: {0}".format(control_ref or "(the control reference above)"),
        "  - The only chunk_id values you may cite are: {0}".format(valid_ids),
        "  - If the evidence does not contain the attribute the control is about, return "
        "INSUFFICIENT_EVIDENCE and name the missing artefact. Do not reason your way to a verdict.",
        "  - human_review_required must be true.",
    ]
    if block.truncated_chunk_ids or block.dropped_chunk_ids:
        lines.append(
            "  - Some evidence was truncated or omitted for length (see the budget note above). Do not "
            "state a population-level count as proven if it depends on text you could not see."
        )
    lines.extend(
        [
            "",
            "Return JSON only - one object, no prose, no markdown fences - in exactly this shape:",
            "",
            ASSESSMENT_JSON_TEMPLATE,
            "----- END OF INSTRUCTIONS -----",
        ]
    )
    return "\n".join(lines)


def build_raw_prompt(control: Any, raw_texts: Sequence[Any], project: Any = None) -> str:
    """EXPERIMENT A BASELINE - deliberately minimal, and deliberately naive.

    This is the control condition of the study, and it must stay weak. It gives the
    model a control name, a one-line objective, raw file text crudely truncated to
    ``settings.raw_evidence_char_budget``, and an instruction to return JSON. It has:

    * no chunk IDs and no citation scaffolding,
    * no expected evidence and no numbered assessment criteria,
    * no four-way REQUIRES / PROVES / INFERS / HUMAN-VERIFIES separation rule,
    * no worked example and no few-shot anchor,
    * no instruction that INSUFFICIENT_EVIDENCE is an acceptable answer.

    It is what a competent developer would write in an afternoon without an audit
    methodology in hand - which is precisely the comparison the research is making.
    Any A-vs-C difference therefore measures **the scaffolding, not the model**: the
    same weights answer both prompts. Do not "improve" this function; improving it
    destroys the baseline. The engine must also pair it with
    :data:`RAW_BASELINE_SYSTEM_PROMPT`, not with :data:`SYSTEM_PROMPT`.

    One deliberate concession to fairness: filenames are kept when supplied. Without
    them the baseline could not name a source at all, and every source it named would
    be counted as fabricated by construction, which would flatter the comparison.
    """
    settings = get_settings()
    budget = int(settings.raw_evidence_char_budget)

    name = _get(control, "name", "") or _get(control, "control_id", "") or "the control"
    objective = _one_line(_get(control, "objective", "") or _get(control, "description", ""), 200)

    body = _concatenate_raw_texts(raw_texts)
    if len(body) > budget:
        # Crude tail-drop truncation, on purpose: the naive baseline has no budgeting
        # strategy, and pretending otherwise would understate what the scaffolding does.
        body = body[:budget] + "\n[truncated]"

    parts = ["Control: {0}".format(name)]
    if objective:
        parts.append("Objective: {0}".format(objective))
    project_name = _get(project, "name", "")
    if project_name:
        parts.append("Audit: {0}".format(project_name))
    parts.append("")
    parts.append("Evidence:")
    parts.append(body if body.strip() else "(no evidence provided)")
    parts.append("")
    parts.append(
        "Assess this control and return JSON with your assessment, status, finding, risk, "
        "risk_level and recommendation."
    )
    return "\n".join(parts)


def _concatenate_raw_texts(raw_texts: Sequence[Any]) -> str:
    """Flatten whatever the engine has - strings, (filename, text) pairs, or dicts."""
    blocks: List[str] = []
    for item in raw_texts or []:
        filename, text = "", ""
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            filename = str(item.get("filename", "") or "")
            text = str(item.get("text", "") or "")
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            filename, text = str(item[0] or ""), str(item[1] or "")
        else:
            filename = str(_get(item, "filename", "") or "")
            text = str(_get(item, "text", "") or "")
        if not text.strip():
            continue
        blocks.append("--- {0} ---\n{1}".format(filename, text) if filename else text)
    return "\n\n".join(blocks)


def build_sufficiency_prompt(
    control: Any, retrieval: Optional[RetrievalResult], project: Any = None
) -> str:
    """Experiment C, step 1: can this control be tested with this evidence at all?

    Asked *before* the assessment, and answered without one. Separating the question
    "is the attribute under test even present?" from the question "what does it say?"
    is the structural version of the MFA worked example: a model that has already
    committed to a sufficiency judgement is much less likely to reach for a verdict it
    cannot support.
    """
    settings = get_settings()
    block = build_evidence_block(retrieval, settings.max_evidence_chars)
    expected = _as_list(_get(control, "expected_evidence", []))
    criteria = _as_list(_get(control, "assessment_criteria", []))

    lines = [
        "TASK: decide whether the supplied evidence is sufficient to test this control. "
        "Do NOT assess the control. Do NOT decide whether it is effective.",
        "",
        render_control_requirement(control),
        "",
        render_audit_context(project, ExperimentMode.C_RAG_WORKFLOW),
        "",
        block.text,
        "",
        "----- HOW TO ANSWER -----",
        "For each expected artefact, and for each assessment criterion, ask one question: is the "
        "attribute it depends on actually present in the evidence above?",
    ]
    if expected:
        lines.append("Expected artefacts to check for: {0}".format("; ".join(expected)))
    if criteria:
        lines.append("Criteria whose testability you must judge: {0}".format("; ".join(criteria)))
    lines.extend(
        [
            "",
            "Rules:",
            "  - present_evidence may only name artefacts you can actually see in a chunk above.",
            "  - missing_evidence must be specific and requestable: name the export, listing, "
            "configuration or approval record that is needed, not a vague category.",
            "  - A file that exists but lacks the attribute under test (for example an account listing "
            "with no MFA column) is MISSING evidence for that attribute, not present evidence.",
            "  - can_conclude is true only if the evidence could support a conclusion either way. If it "
            "could only ever support one answer, it cannot support a conclusion.",
            "  - Evidence that shows configuration but not operation over the period is PARTIAL at best.",
            "",
            "Return JSON only - one object, no prose, no markdown fences - in exactly this shape:",
            "",
            SUFFICIENCY_JSON_TEMPLATE,
            "----- END OF INSTRUCTIONS -----",
        ]
    )
    return "\n".join(lines)


def build_critique_prompt(
    control: Any,
    retrieval: Optional[RetrievalResult],
    draft: Union[AssessmentOutput, Dict[str, Any], str],
    project: Any = None,
) -> str:
    """Experiment C, step 3: make the model audit its own draft before a human does.

    This is a cheap, model-side complement to the mechanical validator in
    :mod:`app.audit.validators` - not a replacement for it. The validator is what the
    research measures; this step exists because a model asked to re-read its own
    output against the evidence will sometimes retract an overreach that no string
    matcher could have caught (an unsupported causal claim, a conclusion that outruns
    the population the evidence covers).
    """
    settings = get_settings()
    block = build_evidence_block(retrieval, settings.max_evidence_chars)
    valid_ids = ", ".join(str(c) for c in block.included_chunk_ids) or "(none)"

    lines = [
        "TASK: critique the draft assessment below against the evidence. You are acting as the "
        "reviewer, not the author. Assume the draft is wrong until the evidence shows otherwise.",
        "",
        render_control_requirement(control),
        "",
        render_audit_context(project, ExperimentMode.C_RAG_WORKFLOW),
        "",
        block.text,
        "",
        "----- DRAFT ASSESSMENT UNDER REVIEW -----",
        _render_draft(draft),
        "----- END OF DRAFT -----",
        "",
        "----- HOW TO ANSWER -----",
        "Check, in this order:",
        "  1. Every citation: is its chunk_id one of {0}? If not, list it under "
        "fabricated_citations.".format(valid_ids),
        "  2. Every quoted_text: does that exact wording appear in the chunk it claims to come from? "
        "A paraphrase presented as a quotation is a fabricated citation.",
        "  3. Every number, percentage and count in assessment, finding and reasoning: can you find it "
        "in the evidence, or count it directly from a quoted excerpt? If not, list it under "
        "unsupported_claims.",
        "  4. The conclusion: does the cited evidence actually establish the status claimed? A status "
        "of EFFECTIVE or NOT_EFFECTIVE that rests on evidence which never reports the attribute under "
        "test is an overstatement - set overstated_conclusion true and recommend "
        "INSUFFICIENT_EVIDENCE.",
        "  5. The separation of statements: is anything in `assessment` actually an inference, or a "
        "restatement of the policy requirement rather than an observation? Note it.",
        "",
        "Be specific and quote the offending phrase. If the draft is sound, return empty lists and say "
        "so in notes - do not invent problems, and do not soften real ones.",
        "",
        "Return JSON only - one object, no prose, no markdown fences - in exactly this shape:",
        "",
        SELF_CRITIQUE_JSON_TEMPLATE,
        "----- END OF INSTRUCTIONS -----",
    ]
    return "\n".join(lines)


def _render_draft(draft: Union[AssessmentOutput, Dict[str, Any], str]) -> str:
    if isinstance(draft, AssessmentOutput):
        return draft.model_dump_json(indent=2)
    if isinstance(draft, str):
        return draft.strip()
    if isinstance(draft, dict):
        try:
            return json.dumps(draft, indent=2, default=str)
        except (TypeError, ValueError):
            return str(draft)
    return str(draft)


# ---- reproducibility metadata


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prompt_metadata() -> Dict[str, Any]:
    """Everything needed to pin a result set to the exact prompts that produced it.

    A thesis that reports "the system returned INSUFFICIENT_EVIDENCE on DATASET-005"
    is reporting a property of a prompt as much as of a model. Persisting this dict
    alongside a run makes that dependency explicit and the run repeatable.
    """
    static_texts = {
        "SYSTEM_PROMPT": SYSTEM_PROMPT,
        "RAW_BASELINE_SYSTEM_PROMPT": RAW_BASELINE_SYSTEM_PROMPT,
        "ASSESSMENT_JSON_TEMPLATE": ASSESSMENT_JSON_TEMPLATE,
        "SUFFICIENCY_JSON_TEMPLATE": SUFFICIENCY_JSON_TEMPLATE,
        "SELF_CRITIQUE_JSON_TEMPLATE": SELF_CRITIQUE_JSON_TEMPLATE,
        "NO_EVIDENCE_BLOCK": NO_EVIDENCE_BLOCK,
    }
    settings = get_settings()
    return {
        "prompt_version": PROMPT_VERSION,
        "fingerprint": _sha256(PROMPT_VERSION + "".join(static_texts[k] for k in sorted(static_texts)))[:16],
        "char_counts": dict((name, len(text)) for name, text in static_texts.items()),
        "sha256": dict((name, _sha256(text)) for name, text in static_texts.items()),
        "budgets": {
            "max_evidence_chars": settings.max_evidence_chars,
            "raw_evidence_char_budget": settings.raw_evidence_char_budget,
            "min_chunk_text_chars": MIN_CHUNK_TEXT_CHARS,
            "min_evidence_budget_chars": MIN_EVIDENCE_BUDGET_CHARS,
            "protected_source_types": list(PROTECTED_SOURCE_TYPES),
        },
        "builders": {
            "build_assessment_prompt": {
                "system_prompt": "SYSTEM_PROMPT",
                "output_template": "ASSESSMENT_JSON_TEMPLATE",
                "modes": [ExperimentMode.B_RAG.value, ExperimentMode.C_RAG_WORKFLOW.value],
                "citation_scaffolding": True,
                "few_shot": True,
            },
            "build_raw_prompt": {
                "system_prompt": "RAW_BASELINE_SYSTEM_PROMPT",
                "output_template": None,
                "modes": [ExperimentMode.A_RAW_LLM.value],
                "citation_scaffolding": False,
                "few_shot": False,
            },
            "build_sufficiency_prompt": {
                "system_prompt": "SYSTEM_PROMPT",
                "output_template": "SUFFICIENCY_JSON_TEMPLATE",
                "modes": [ExperimentMode.C_RAG_WORKFLOW.value],
                "citation_scaffolding": True,
                "few_shot": True,
            },
            "build_critique_prompt": {
                "system_prompt": "SYSTEM_PROMPT",
                "output_template": "SELF_CRITIQUE_JSON_TEMPLATE",
                "modes": [ExperimentMode.C_RAG_WORKFLOW.value],
                "citation_scaffolding": True,
                "few_shot": True,
            },
        },
    }
