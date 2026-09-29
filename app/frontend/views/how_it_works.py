"""How it works - the plain-language explanation of the audit assistant.

What this page is for
---------------------
A first-time auditor should be able to read one screen and understand what the tool
does, what it will not do, what the five steps are, how an assessment is put together,
how to read the assessment screen and what their own decision records. No jargon, no
metrics, no code: the research detail lives in the Research section and in the
"Details for researchers" expander on the assessment screen.

Everything written here describes what the code actually does - the wording of the four
sections follows :mod:`app.frontend.assessment_view`, the decisions follow
:mod:`app.frontend.review_form`, and the limits follow the checks in
:mod:`app.audit.validators` and the README's "What this is not". If one of those
changes, this page should change with it.

Nothing on this page runs anything or touches a project. It is text and links.
"""

from __future__ import annotations

import sys
from pathlib import Path

# A page module is exec'd by ``st.Page`` in the entry script's process, where the
# repository root is already on ``sys.path``. It is repeated here so the module also
# imports cleanly when it is loaded directly (a test, ``streamlit run`` on this file).
_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from typing import Sequence, Tuple  # noqa: E402

import streamlit as st  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.frontend import components  # noqa: E402

_esc = components.escape

#: The five steps, in the order an auditor walks them. Labels match
#: ``data_access.project_stage`` so the words here are the words on the workflow strip.
_STEPS: Tuple[Tuple[str, str, str, str], ...] = (
    (
        "Choose the controls",
        "Pick the controls this audit will test. The requirement wording comes from the "
        "control library, so the AI is always judged against a fixed standard.",
        "views/audit_projects.py",
        components.WORKFLOW_ICONS["scope"],
    ),
    (
        "Add evidence",
        "Upload the policies, exports, reports and notes you have collected. Each file is "
        "fingerprinted on upload so a report can name exactly which file it quoted.",
        "views/evidence.py",
        components.WORKFLOW_ICONS["evidence"],
    ),
    (
        "Run the AI assessment",
        "Press the button and the assistant writes a proposal for each control, quoting "
        "only the evidence you uploaded. Nothing runs until you press it.",
        "views/assessments.py",
        components.WORKFLOW_ICONS["assess"],
    ),
    (
        "Record your decisions",
        "Read each proposal and accept, modify or reject it, or ask for more evidence. Your "
        "decision is the audit conclusion; the proposal never is.",
        "views/human_review.py",
        components.WORKFLOW_ICONS["review"],
    ),
    (
        "Generate the report",
        "Produce a working paper that prints the AI proposal and your decision side by "
        "side for every control, and says plainly which controls still await a decision.",
        "views/reports.py",
        components.WORKFLOW_ICONS["report"],
    ),
)


# ---- small plain-language primitives (all escaped)
def _para(text: str, muted: bool = False) -> None:
    value = str(text or "").strip()
    if not value:
        return
    st.markdown(
        '<div class="ia-text{0}">{1}</div>'.format(" ia-text-muted" if muted else "", _esc(value)),
        unsafe_allow_html=True,
    )


def _bullets(items: Sequence[str], muted: bool = False) -> None:
    entries = [str(item).strip() for item in (items or []) if str(item or "").strip()]
    if not entries:
        return
    st.markdown(
        '<ul class="ia-list{0}">{1}</ul>'.format(
            " ia-list-muted" if muted else "",
            "".join("<li>{0}</li>".format(_esc(item)) for item in entries),
        ),
        unsafe_allow_html=True,
    )


def _numbered(items: Sequence[Tuple[str, str]]) -> None:
    """``[(lead, sentence), ...]`` as an ordered list with the lead in bold."""
    entries = [(str(lead).strip(), str(body).strip()) for lead, body in items if str(lead or "").strip()]
    if not entries:
        return
    st.markdown(
        '<ol class="ia-list">{0}</ol>'.format(
            "".join(
                "<li><strong>{0}</strong>{1}</li>".format(
                    _esc(lead), " - " + _esc(body) if body else ""
                )
                for lead, body in entries
            )
        ),
        unsafe_allow_html=True,
    )


def _safe_page_link(page: str, label_text: str, icon: str = "") -> None:
    """``st.page_link`` that degrades to a caption when the target is not registered."""
    try:
        st.page_link(page, label=label_text, icon=icon or None)
    except Exception:  # noqa: BLE001 - navigation is a convenience, never a dependency
        st.caption(label_text)


# ---- sections
def _render_what_it_does(app_name: str) -> None:
    components.section_header("What this tool does")
    _para(
        "{0} helps an IT auditor test a control against the evidence they have collected. "
        "You choose the control, you upload the evidence, and the assistant reads the "
        "evidence for you and writes a proposal: what it found, which passages it relied on, "
        "and what it could not settle.".format(app_name)
    )
    _para(
        "The proposal is a starting point, not an answer. You read it, check the quoted "
        "passages against the source, and record your own decision. Only your decision "
        "counts as the audit conclusion, and the report says so on every control."
    )


def _render_five_steps() -> None:
    components.section_header(
        "The five steps",
        subtitle="The same five steps for every audit project. The strip at the top of each audit page shows where you are.",
    )
    for number, (label_text, sentence, page, icon) in enumerate(_STEPS, start=1):
        st.markdown(
            '<div class="ia-text"><strong>{0}. {1}</strong> - {2}</div>'.format(
                number, _esc(label_text), _esc(sentence)
            ),
            unsafe_allow_html=True,
        )
        _safe_page_link(page, "Open: {0}".format(label_text), icon)
    components.note(
        "Nothing happens on its own. Each step waits for you to press its button, and you "
        "can go back to any earlier step at any time."
    )


def _render_how_built() -> None:
    components.section_header(
        "How an assessment is built",
        subtitle="What happens between pressing Run assessment and seeing the result.",
    )
    _numbered(
        [
            (
                "Your files are split into passages.",
                "Each document, spreadsheet or export is broken into short, numbered pieces "
                "that keep their page, row or section reference.",
            ),
            (
                "The passages most relevant to the control are found.",
                "The assistant looks for the passages that speak to the control's "
                "requirement and sets the rest aside. Only those passages are shown to it.",
            ),
            (
                "The assistant writes a proposal, quoting only those passages.",
                "It states what it found, quotes the evidence it relied on, and lists what "
                "it could not find.",
            ),
            (
                "Every quotation is re-checked against the stored passage.",
                "A separate, mechanical check confirms that each quoted sentence really "
                "appears where the assistant says it does. A quote that is not there is "
                "marked as fabricated and shown as a failure, never as proof.",
            ),
            (
                "A risk score is suggested.",
                "A simple prototype model turns the assistant's answers into a score and a "
                "band (Low, Medium, High, Critical). It is a prompt for your judgement, not "
                "a rating in its own right.",
            ),
            (
                "The proposal waits for you.",
                "Every assessment is marked as needing auditor review. That cannot be "
                "switched off. Until you record a decision, the control has no conclusion.",
            ),
        ]
    )
    _para(
        "If the evidence does not let the assistant reach a view either way, it says so: "
        "the result is 'Insufficient evidence', which is a request for more evidence and "
        "not a deficiency. The two are kept apart everywhere in the tool.",
        muted=True,
    )


def _render_reading() -> None:
    components.section_header(
        "Reading an assessment",
        subtitle="Every assessment screen has the same shape, so you always know where to look.",
    )
    _para(
        "At the top is a short finding in the assistant's own words, unedited, under a banner "
        "reminding you that it is AI-generated and needs your review. Below it are four "
        "numbered sections that keep four different kinds of statement apart:"
    )
    _numbered(
        [
            (
                "What the control requires.",
                "The requirement, criteria and expected evidence exactly as written in the "
                "control library. This comes from the library, never from the AI, so the "
                "standard cannot drift to fit the answer.",
            ),
            (
                "What the evidence shows.",
                "Only direct quotations from your files, each with its source and the result "
                "of the re-check. Press 'Show the source passage' to see the exact rows or "
                "paragraph a quote came from, with the quoted words highlighted. Nothing in "
                "this section is the assistant's opinion.",
            ),
            (
                "What the AI infers.",
                "The assistant's conclusions: its proposed status, risk band, confidence and "
                "reasoning. Everything here is unproven by construction - it is what the "
                "assistant concluded from the quotes above, and it is for you to test.",
            ),
            (
                "What still needs verification.",
                "The points the assistant says a person must check, the evidence it says it "
                "did not have, and any numbers in its text that the re-check could not find "
                "in the evidence. This is your to-do list before the control can have a "
                "conclusion.",
            ),
        ]
    )
    _para(
        "Your decision form sits directly under the four sections. Once you have recorded a "
        "decision, the screen shows the AI proposal and your conclusion side by side, with "
        "every field where you departed from the AI marked.",
        muted=True,
    )


def _render_decision() -> None:
    components.section_header(
        "Your decision",
        subtitle="Four choices, recorded with your name and your wording.",
    )
    _numbered(
        [
            (
                "Accept finding.",
                "You agree with the assistant's conclusion and risk level. It becomes the "
                "audit conclusion under your name.",
            ),
            (
                "Modify finding.",
                "You reach your own conclusion, informed by the proposal. You choose the "
                "status and risk and write the finding and recommendation in your words.",
            ),
            (
                "Reject finding.",
                "The assistant's conclusion is wrong. You record what the conclusion should "
                "be and why. Rejecting the reasoning but reaching the same status still "
                "counts as agreeing on the outcome.",
            ),
            (
                "Request more evidence.",
                "The control cannot be concluded on the evidence held. The conclusion is "
                "recorded as 'Insufficient evidence' and the risk as 'Not rated', whatever "
                "the assistant proposed.",
            ),
        ]
    )
    _para(
        "Whichever you choose, the assistant's text is never altered. Your decision is a "
        "separate record with your name on it, and the report prints the two records side "
        "by side, each labelled with where it came from, so a later reader can see exactly "
        "where the auditor agreed with the AI and where they did not."
    )
    _para(
        "You can also flag a proposal as containing a fabrication or an unsupported claim. "
        "That flag is kept on the record and shown to whoever reads it next.",
        muted=True,
    )


def _render_will_not_do() -> None:
    components.section_header(
        "What this tool will not do",
        subtitle="Limits that are built in, not settings.",
    )
    _bullets(
        [
            "It never reaches a final conclusion. Every assessment is marked as needing "
            "auditor review, and a control with no recorded decision appears in the report "
            "as awaiting review and is left out of every headline figure.",
            "It never states that anything is compliant. It does not give an opinion, a "
            "sign-off or an attestation, and it is not permitted to assert legal or "
            "regulatory compliance. Framework references in the control library are "
            "illustrative only.",
            "It cannot prove that evidence is authentic or complete. The fingerprint taken at "
            "upload shows a file has not changed inside this tool. It says nothing about who "
            "produced the file, whether it was edited beforehand, or whether a listing is the "
            "whole population.",
            "A verified quote is not proof the conclusion is right. The re-check confirms that "
            "the quoted words exist in the passage cited. It does not confirm that the quote "
            "was read in context or that it supports what the assistant concluded from it - "
            "that is your judgement.",
            "Risk scores are a prototype model. They come from a simple research scoring "
            "model with fixed weights, not from any industry risk framework, and they are "
            "labelled as such wherever they appear.",
            "It runs nothing on its own. No assessment, upload, deletion or report happens "
            "without you pressing the button for it.",
        ]
    )
    components.note(components.RISK_MODEL_NOTE)


def _render_demo_and_providers(app_name: str) -> None:
    components.section_header(
        "Demo mode and AI providers",
        subtitle="Which assistant is answering, and how to tell.",
    )
    _para(
        "Out of the box, {0} runs in Demo mode. In Demo mode the 'assistant' is a small set "
        "of offline rules, not an AI language model. It follows the same steps and produces "
        "the same shape of answer, so you can see how the workflow feels without an internet "
        "connection or an account - but its results tell you about this workflow, not about "
        "what a real model would say. Demo mode is always labelled as such in the sidebar and "
        "on every assessment it produces.".format(app_name)
    )
    _para(
        "To have Claude answer instead, open Settings and follow the AI provider tab: you set "
        "the provider to Claude and supply an API key in the configuration file on the "
        "machine running the tool. Keys are never typed into or shown on any page here. If "
        "Claude is selected but cannot answer, the tool falls back to the offline rules and "
        "says so in red - it will never present an offline result as a Claude result."
    )
    _para(
        "The demo audit uses synthetic evidence only. The files describe no real "
        "organisation, system or person, and are safe to explore, delete and reload.",
        muted=True,
    )
    _safe_page_link("views/settings.py", "Open Settings to choose the AI provider", ":material/tune:")


# ---- page
def render() -> None:
    app_name = str(getattr(get_settings(), "app_short_name", "") or "AuditAI")
    components.section_header(
        "How the audit assistant works",
        subtitle="A plain-language guide. No metrics, no configuration - just what the tool does and what it leaves to you.",
        eyebrow="Learn",
    )
    _render_what_it_does(app_name)
    st.markdown("")
    _render_five_steps()
    st.markdown("")
    _render_how_built()
    st.markdown("")
    _render_reading()
    st.markdown("")
    _render_decision()
    st.markdown("")
    _render_will_not_do()
    st.markdown("")
    _render_demo_and_providers(app_name)
    st.markdown("")
    components.section_header("Where to go next")
    components.next_steps(
        [
            ("Back to Home", "views/home.py", ":material/home:"),
            ("Open the Review queue", "views/human_review.py", components.WORKFLOW_ICONS["review"]),
        ]
    )


if __name__ == "__main__":
    render()
