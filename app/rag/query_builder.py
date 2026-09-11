"""Turning a control definition into the questions retrieval should actually ask.

A control is not one question. "Privileged accounts are protected by multi-factor
authentication" asks at least three separable things: what does the *policy* require,
what does the *configuration* show, and which *accounts* are exceptions. A single
concatenated query answers none of them well - it retrieves whatever is longest and
most keyword-dense, which in audit evidence is almost always the user listing rather
than the policy that states the requirement.

So each control is expanded into several targeted queries and the retriever combines a
chunk's scores across them (see :mod:`app.rag.retriever` for how, and why the two halves
of hybrid retrieval combine them differently). The expansion is deliberately mechanical
- no LLM call: query generation must stay cheap, offline and reproducible, because it
sits between the auditor and the evidence in every single assessment.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Set

#: Upper bound on one query. Long queries drift off-topic and, under BM25, dilute the
#: terms that actually discriminate.
MAX_QUERY_CHARS = 280

#: The contract with the retrieval layer: never fewer than 4 angles on a control,
#: never so many that a single control dominates a retrieval budget.
MIN_QUERIES = 4
MAX_QUERIES = 10

#: Always asked, for every control. A control's own wording describes the compliant
#: state ("all privileged accounts have MFA enabled"), so every query derived from it
#: is a better match for a compliant row than for a failing one - and the failing rows
#: are the entire point of the test. This angle deliberately searches the vocabulary of
#: failure so exception rows compete for a place in the evidence budget.
_EXCEPTION_ANGLE = "{name} exceptions non-compliant disabled failed missing not approved"

#: Used only when a control is too sparsely defined to yield four real queries -
#: generic audit phrasings that at least push retrieval towards requirement,
#: configuration and testing evidence respectively.
_GENERIC_ANGLES = (
    "{name} policy requirement standard",
    "{name} configuration setting evidence",
    "{name} control operating effectiveness testing",
    "{name} population listing records reviewed",
)

_WHITESPACE_RE = re.compile(r"\s+")
_KEY_NOISE_RE = re.compile(r"[^a-z0-9 ]+")


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _first_str(source: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value not in (None, "", [], {}) and not isinstance(value, (list, tuple, dict, set)):
            return str(value).strip()
    return ""


def _control_fields(control: Any) -> Dict[str, Any]:
    """Normalise a ``Control`` ORM row or a plain dict into the fields we use.

    Dicts come from the evaluation harness and from API payloads, ORM rows from the
    application itself; accepting both keeps this callable in tests without a database.
    """
    if isinstance(control, dict):
        source: Dict[str, Any] = dict(control)
    else:
        source = {}
        for attr in (
            "control_id",
            "name",
            "objective",
            "description",
            "risk_addressed",
            "expected_evidence",
            "assessment_criteria",
            "retrieval_keywords",
            "framework_refs",
            "category",
            "control_type",
        ):
            source[attr] = getattr(control, attr, None)

    return {
        "control_id": _first_str(source, "control_id", "control_ref", "ref", "id"),
        "name": _first_str(source, "name", "title", "control_name"),
        "objective": _first_str(source, "objective", "control_objective"),
        "description": _first_str(source, "description", "detail"),
        "risk_addressed": _first_str(source, "risk_addressed", "risk"),
        "category": _first_str(source, "category"),
        "expected_evidence": _as_list(source.get("expected_evidence") or source.get("evidence")),
        "assessment_criteria": _as_list(source.get("assessment_criteria") or source.get("criteria")),
        "retrieval_keywords": _as_list(source.get("retrieval_keywords") or source.get("keywords")),
    }


def _clean(text: str) -> str:
    """Collapse whitespace and trim to ``MAX_QUERY_CHARS`` on a word boundary."""
    cleaned = _WHITESPACE_RE.sub(" ", str(text or "")).strip()
    if len(cleaned) <= MAX_QUERY_CHARS:
        return cleaned
    cut = cleaned[:MAX_QUERY_CHARS]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.strip()


def _dedupe_key(query: str) -> str:
    return _WHITESPACE_RE.sub(" ", _KEY_NOISE_RE.sub(" ", query.lower())).strip()


def _append(queries: List[str], seen: Set[str], candidate: str) -> None:
    cleaned = _clean(candidate)
    # Three characters is the floor, not four: "MFA" is a legitimate query.
    if len(cleaned) < 3:
        return
    key = _dedupe_key(cleaned)
    if not key or key in seen:
        return
    seen.add(key)
    queries.append(cleaned)


def build_queries(control: Any, max_queries: int = MAX_QUERIES) -> List[str]:
    """Expand a control into 4-10 distinct retrieval queries.

    The queries are ordered by expected usefulness, because the cap truncates the
    tail: the control's stated objective first, then its keywords, then each
    assessment criterion (the most literal statement of what must be shown), the
    expected-evidence artefacts, an explicit search for exceptions, the control
    reference itself - audit artefacts such as change tickets often quote the control
    ID verbatim - and finally the risk addressed and the description, which usually
    restate the objective in other words.
    """
    fields = _control_fields(control)
    name = fields["name"] or fields["control_id"] or "control"
    keywords = fields["retrieval_keywords"]

    queries: List[str] = []
    seen: Set[str] = set()

    _append(queries, seen, "{} {}".format(name, fields["objective"]).strip())

    if keywords:
        _append(queries, seen, "{} {}".format(name, " ".join(keywords[:8])))

    for criterion in fields["assessment_criteria"][:4]:
        _append(queries, seen, criterion)

    if fields["expected_evidence"]:
        _append(
            queries,
            seen,
            "{} {}".format(name, " ".join(fields["expected_evidence"][:5])),
        )

    _append(queries, seen, _EXCEPTION_ANGLE.format(name=name))

    if fields["control_id"]:
        _append(queries, seen, "{} {}".format(fields["control_id"], name))

    _append(queries, seen, fields["risk_addressed"])
    _append(queries, seen, fields["description"])

    # Sparse control definitions (a name and little else) still need enough angles for
    # retrieval to see requirement evidence and exception evidence separately.
    for template in _GENERIC_ANGLES:
        if len(queries) >= MIN_QUERIES:
            break
        _append(queries, seen, template.format(name=name))

    limit = max(MIN_QUERIES, min(int(max_queries or MAX_QUERIES), MAX_QUERIES))
    return queries[:limit]


def build_query_text(control: Any) -> str:
    """A single flattened query, for callers that cannot use a query list."""
    return _clean(" ".join(build_queries(control)))[:MAX_QUERY_CHARS]


__all__ = ["MAX_QUERIES", "MAX_QUERY_CHARS", "MIN_QUERIES", "build_queries", "build_query_text"]
