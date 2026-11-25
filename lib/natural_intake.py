"""Natural D31 clarifying intake workflow (E3-2).

Allows rough, natural-language tickets to enter ``orch:state/intake`` and wake
the orchestrator via a single deduplicated ``intake-clarify`` item rather than
failing the strict ``#204`` feeder template.

D31 question discipline:
- ``observable`` and ``default-able`` technical questions are recorded with
  their decisions/defaults without pausing on Max.
- Only genuine ``product`` questions set ``state=waiting-max`` and pause the
  ticket until Max answers them (one product question at a time, or in one
  consolidated ``answers_for`` comment).
- Once every open ``product`` question is answered, ``promote()`` validates
  the structured brief (including ``Acceptance criteria``, ``Likely files``,
  optional ``Out of scope``, ``Risk``, and ``Done-evidence``), resolves the
  cross-family route via ``lib.orchestrate.choose_route``, and produces the
  ``orch:v1`` brief and ``go`` comments.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from lib.factory_md import FactoryConfig
from lib.handoff import Handoff, format_comment
from lib.orchestrate import (
    FROM,
    ROLE,
    ClassifiedQuestion,
    GoBlocked,
    OrchestrateError,
    RouteDecision,
    RouteRequest,
    TicketFlow,
    brief_comment,
    choose_route,
    next_product_question,
    product_questions_cleared,
)

NATURAL_INTAKE_ACTION = "intake-clarify"
INTAKE_STATE_LABEL = "orch:state/intake"

SessionState = Literal["intake", "waiting-max", "ready-to-promote", "promoted"]
StepState = Literal["waiting-max", "ready-to-promote"]


class NaturalIntakeError(OrchestrateError):
    """Natural clarifying intake step rejected."""


@dataclass(frozen=True)
class NaturalIntakeStep:
    """Result of posting D31 clarifying questions or recording Max's answers."""

    state: StepState
    comment: str
    open_product_questions: tuple[ClassifiedQuestion, ...]
    next_product_question: ClassifiedQuestion | None
    recorded_defaults: tuple[tuple[str, str], ...]
    observable_questions: tuple[str, ...]


@dataclass(frozen=True)
class PromotionArtifacts:
    """Structured brief and ``kind=go`` handoff produced after D31 clears."""

    brief_comment: str
    go_comment: str
    decision: RouteDecision


def _one_line(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise NaturalIntakeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise NaturalIntakeError(f"{name} is empty")
    if "\n" in cleaned or "\r" in cleaned:
        raise NaturalIntakeError(f"{name} must be one line")
    return cleaned


def _items(heading: str, entries: Sequence[str]) -> str:
    if not entries:
        return f"**{heading}**\nNone.\n\n"
    bullets = "\n".join(f"- {entry}" for entry in entries)
    return f"**{heading}**\n{bullets}\n\n"


def _paragraph(heading: str, text: str) -> str:
    return f"**{heading}**\n{text}\n\n"


def render_structured_brief(
    *,
    risk: str,
    goal: str,
    acceptance_criteria: Sequence[str],
    likely_files: Sequence[str],
    done_evidence: str,
    out_of_scope: Sequence[str] = (),
    visible: bool = True,
) -> str:
    """Render a validated ``orch:v1 kind=brief`` comment for promoted intake.

    When ``out_of_scope`` is empty, delegates directly to
    ``lib.orchestrate.brief_comment``. When ``out_of_scope`` is provided,
    validates every field with the same rules and inserts an ``**Out of scope**``
    section between ``Likely files`` and ``Risk``.
    """
    scope_items = [_one_line(item, "out of scope item") for item in out_of_scope]
    if not scope_items:
        return brief_comment(
            risk=risk,
            goal=goal,
            acceptance_criteria=acceptance_criteria,
            likely_files=likely_files,
            done_evidence=done_evidence,
            visible=visible,
        )
    # Validate core fields through brief_comment first so rules never drift.
    brief_comment(
        risk=risk,
        goal=goal,
        acceptance_criteria=acceptance_criteria,
        likely_files=likely_files,
        done_evidence=done_evidence,
        visible=visible,
    )
    criteria = [_one_line(item, "acceptance criterion") for item in acceptance_criteria]
    files = [_one_line(item, "likely file") for item in likely_files]
    body = "".join(
        (
            _paragraph("Goal", goal.strip()),
            _items("Acceptance criteria", [f"[ ] {item}" for item in criteria]),
            _items("Likely files", files),
            _items("Out of scope", scope_items),
            _paragraph("Risk", risk),
            _paragraph("Done-evidence", done_evidence.strip()),
        )
    )
    return format_comment(
        Handoff(
            fields=(
                ("kind", "brief"),
                ("from", FROM),
                ("role", ROLE),
                ("risk", risk),
            ),
            body=body,
        ),
        visible=visible,
    )


class NaturalIntakeSession:
    """Stateful D31 clarifying-intake driver wrapping ``lib.orchestrate.TicketFlow``."""

    def __init__(self, config: FactoryConfig) -> None:
        if not isinstance(config, FactoryConfig):
            raise NaturalIntakeError("config must be a FactoryConfig")
        self.config = config
        self._flow = TicketFlow()
        self._questions_posted = False
        self._promoted = False

    @property
    def questions(self) -> tuple[ClassifiedQuestion, ...]:
        return self._flow.questions

    @property
    def open_product_questions(self) -> tuple[ClassifiedQuestion, ...]:
        return tuple(
            question
            for question in self._flow.questions
            if question.classification == "product" and question.answer is None
        )

    @property
    def next_product_question(self) -> ClassifiedQuestion | None:
        return next_product_question(self._flow.questions)

    @property
    def state(self) -> SessionState:
        if self._promoted:
            return "promoted"
        if not self._questions_posted:
            return "intake"
        if not product_questions_cleared(self._flow.questions):
            return "waiting-max"
        return "ready-to-promote"

    def _step(self, comment: str) -> NaturalIntakeStep:
        open_product = self.open_product_questions
        defaults = tuple(
            (q.text, q.default)
            for q in self._flow.questions
            if q.classification == "default-able" and q.default is not None
        )
        observable = tuple(
            q.text for q in self._flow.questions if q.classification == "observable"
        )
        step_state: StepState = "waiting-max" if open_product else "ready-to-promote"
        return NaturalIntakeStep(
            state=step_state,
            comment=comment,
            open_product_questions=open_product,
            next_product_question=open_product[0] if open_product else None,
            recorded_defaults=defaults,
            observable_questions=observable,
        )

    def classify_and_post(
        self,
        restatement: str,
        questions: Sequence[ClassifiedQuestion],
        *,
        visible: bool = True,
    ) -> NaturalIntakeStep:
        """Record classified D31 questions.

        ``observable`` and ``default-able`` questions never pause on Max; only
        at least one ``product`` question transitions the ticket to
        ``waiting-max``.
        """
        comment = self._flow.post_questions(restatement, questions, visible=visible)
        self._questions_posted = True
        return self._step(comment)

    def answer_product_question(
        self,
        text: str,
        answer: str,
        *,
        visible: bool = True,
    ) -> NaturalIntakeStep:
        """Record Max's answer to the current open product question."""
        comment = self._flow.post_answer(text, answer, visible=visible)
        return self._step(comment)

    def answer_all_product_questions(
        self,
        answers: Sequence[tuple[str, str]],
        *,
        visible: bool = True,
    ) -> NaturalIntakeStep:
        """Record Max's answers to all open product questions in one comment."""
        comment = self._flow.post_answers(answers, visible=visible)
        return self._step(comment)

    def promote(
        self,
        *,
        goal: str,
        acceptance_criteria: Sequence[str],
        likely_files: Sequence[str],
        risk: str,
        done_evidence: str,
        out_of_scope: Sequence[str] = (),
        summary: str | None = None,
        urgent: bool = False,
        scope_changed: bool = False,
        visible: bool = True,
    ) -> PromotionArtifacts:
        """Emit structured brief + ``kind=go`` once D31 product questions clear."""
        if not self._questions_posted:
            raise GoBlocked("questions step has not run")
        if self._promoted:
            raise GoBlocked("orch:go already posted")
        if not product_questions_cleared(self._flow.questions):
            raise GoBlocked("product questions are open")
        request = RouteRequest(
            config=self.config,
            risk=risk,
            urgent=urgent,
            summary=summary if summary is not None else goal,
            likely_files=tuple(likely_files),
            scope_changed=scope_changed,
        )
        decision = choose_route(request)
        brief = render_structured_brief(
            risk=risk,
            goal=goal,
            acceptance_criteria=acceptance_criteria,
            likely_files=likely_files,
            done_evidence=done_evidence,
            out_of_scope=out_of_scope,
            visible=visible,
        )
        verify = self.config.verify or "none"
        go = self._flow.post_go(decision, verify, visible=visible)
        self._promoted = True
        return PromotionArtifacts(
            brief_comment=brief,
            go_comment=go,
            decision=decision,
        )
