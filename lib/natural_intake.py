"""Natural D31 clarifying intake workflow (E3-2).

Rough, natural-language tickets skip the strict ``#204`` feeder template. The
feeder opens a GitHub ticket's draft PR labelled ``orch:state/intake`` and wakes
the orchestrator with one deduplicated ``intake-clarify`` item; the session's
handoffs are posted on that PR (or the Linear ticket).

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
from lib.orchestrate import (
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
        brief = brief_comment(
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
