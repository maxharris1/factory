"""Grok Bot `orchestrate-project` interface (docs/SPEC.md F1-07).

Stable names for F1-08, which owns best fit and the usage reserve:

- `RouteRequest` — input. Do not add a required field.
- `FitChoice` — harness pair built from a `route_job` result.
- `RouteDecision` — what `orch:go` posts. Families and `job` are set here.
- `choose_route(request)` — calls `lib.usage.route_job(config, marked_urgent=urgent)`.
- `route_from_fit(request, fit)` — cross-family check, then a decision.
- `assert_cross_family(...)` — the check-config family rule. Same family raises.

`choose_route` does not rank fits and does not read a usage source.
`lib.usage` must not import `FitChoice`. The import stays inside
`choose_route` so the two modules do not cycle.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from lib.factory_md import (
    FactoryConfig,
    _family_findings,
    _resolved_family,
    load_factory_md,
)
from lib.handoff import Handoff, format_comment
from lib.load import Finding, split_front_matter
from lib.schema import HARNESSES

FROM = "grok-bot"
ROLE = "orchestrator"

QUESTION_CLASSES = ("observable", "default-able", "product")
JOBS = ("build", "plan")
STOPS = ("refused", "stuck", "blocked", "capped", "abandoned")

RECOMMENDED_NEXT = {
    "refused": (
        "Fix factory.md so orch check-config accepts it, then hand the ticket back."
    ),
    "stuck": "Read the evidence. Max decides whether to retry or abandon.",
    "blocked": "Read the evidence. Max decides the unblock.",
    "capped": "An extra round needs Max's own words.",
    "abandoned": "No further work unless Max reopens it.",
}


class OrchestrateError(ValueError):
    """The skill refused to take the next step."""


class RouteError(OrchestrateError):
    """The builder and reviewer pair cannot be used."""


class GoBlocked(OrchestrateError):
    """orch:go is not allowed yet."""


class QuestionError(OrchestrateError):
    """A question record is not valid for this step."""


class StopError(OrchestrateError):
    """A stop report cannot be sent."""


@dataclass(frozen=True)
class RouteRequest:
    config: FactoryConfig
    risk: str
    urgent: bool
    summary: str
    likely_files: tuple[str, ...]
    scope_changed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "likely_files", tuple(self.likely_files))


@dataclass(frozen=True)
class FitChoice:
    builder_harness: str
    builder_model: str | None
    reviewer_harness: str
    reviewer_model: str | None


@dataclass(frozen=True)
class RouteDecision:
    builder_harness: str
    builder_model: str | None
    reviewer_harness: str
    reviewer_model: str | None
    builder_family: str
    reviewer_family: str
    job: str
    urgent: bool
    risk: str
    file_count: int
    scope_changed: bool


@dataclass(frozen=True)
class ClassifiedQuestion:
    text: str
    classification: str
    default: str | None = None
    answer: str | None = None

    def __post_init__(self) -> None:
        if self.classification not in QUESTION_CLASSES:
            raise QuestionError(
                "question class must be observable, default-able, or product"
            )
        object.__setattr__(
            self,
            "text",
            _one_line(self.text, "question text", QuestionError),
        )
        if self.classification == "default-able":
            if not isinstance(self.default, str):
                raise QuestionError("default-able question has no recorded default")
            object.__setattr__(
                self,
                "default",
                _one_line(self.default, "default", QuestionError),
            )
        elif self.default is not None:
            raise QuestionError("default is only valid on default-able")
        if self.answer is not None:
            if self.classification != "product":
                raise QuestionError("answer is only valid on product")
            object.__setattr__(
                self,
                "answer",
                _one_line(self.answer, "answer", QuestionError),
            )


@dataclass(frozen=True)
class Project:
    config: FactoryConfig
    review: str
    ship_notes: str
    context: str
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ReadResult:
    project: Project | None
    stop: StopReport | None


@dataclass(frozen=True)
class StopReport:
    stop: str
    evidence: str
    next_step: str

    def __post_init__(self) -> None:
        if self.stop not in STOPS:
            raise StopError(f"unknown stop {self.stop!r}")
        if not isinstance(self.evidence, str) or self.evidence.strip() == "":
            raise StopError("stop report needs evidence")
        if not isinstance(self.next_step, str) or self.next_step.strip() == "":
            raise StopError("stop report needs a next step")


class StopLedger:
    """One report per ticket and stop. A later different stop can still be sent."""

    def __init__(self) -> None:
        self._seen: set[tuple[str, str]] = set()

    def report(self, ticket: str, stop: StopReport) -> str:
        if not isinstance(stop, StopReport):
            raise StopError("stop must be a StopReport")
        if not isinstance(ticket, str) or ticket.strip() == "":
            raise StopError("ticket is empty")
        key = (ticket, stop.stop)
        if key in self._seen:
            raise StopError(f"stop {stop.stop!r} already reported")
        self._seen.add(key)
        return format_stop_report(stop)


class TicketFlow:
    """Intake order: questions, then product answers one at a time, then go."""

    def __init__(self) -> None:
        self._questions_posted = False
        self._questions: tuple[ClassifiedQuestion, ...] = ()
        self._went = False

    @property
    def questions(self) -> tuple[ClassifiedQuestion, ...]:
        return self._questions

    def post_questions(
        self,
        restatement: str,
        questions: Sequence[ClassifiedQuestion],
        *,
        visible: bool = False,
    ) -> str:
        if self._questions_posted:
            raise QuestionError("questions step already ran")
        if not isinstance(restatement, str) or restatement.strip() == "":
            raise QuestionError("restatement is empty")
        checked: list[ClassifiedQuestion] = []
        for question in questions:
            if not isinstance(question, ClassifiedQuestion):
                raise QuestionError("question must be classified")
            if question.classification == "product" and question.answer is not None:
                raise QuestionError("record a product answer with post_answer")
            checked.append(question)
        self._questions = tuple(checked)
        self._questions_posted = True
        return questions_comment(restatement, self._questions, visible=visible)

    def post_answer(
        self,
        text: str,
        answer: str,
        *,
        visible: bool = False,
    ) -> str:
        if not self._questions_posted:
            raise QuestionError("questions step has not run")
        text = _one_line(text, "question text", QuestionError)
        answer = _one_line(answer, "answer", QuestionError)
        current = next_product_question(self._questions)
        if current is None:
            raise QuestionError("no open product question")
        if current.text != text:
            raise QuestionError("answer the open product question first")
        updated: list[ClassifiedQuestion] = []
        replaced = False
        for question in self._questions:
            if not replaced and question == current:
                updated.append(
                    ClassifiedQuestion(
                        text=question.text,
                        classification="product",
                        answer=answer,
                    )
                )
                replaced = True
            else:
                updated.append(question)
        self._questions = tuple(updated)
        return answers_comment(text, answer, visible=visible)

    def post_go(
        self,
        decision: RouteDecision,
        verify: str,
        *,
        visible: bool = False,
    ) -> str:
        if not self._questions_posted:
            raise GoBlocked("questions step has not run")
        if self._went:
            raise GoBlocked("orch:go already posted")
        if not product_questions_cleared(self._questions):
            raise GoBlocked("product questions are open")
        self._went = True
        return go_comment(decision, verify, visible=visible)


def read_factory(path: Path) -> ReadResult:
    """Read one factory.md. Any other filename is refused before the file is opened."""
    if path.name != "factory.md":
        raise OrchestrateError("path must be factory.md")
    config, findings = load_factory_md(path)
    if config is None:
        return ReadResult(project=None, stop=refused_stop(findings))
    _raw, body = split_front_matter(path.read_text(encoding="utf-8"))
    sections = _sections(body)
    warnings = tuple(
        finding.message for finding in findings if finding.severity == "warning"
    )
    project = Project(
        config=config,
        review=sections.get("Review", ""),
        ship_notes=sections.get("Ship", ""),
        context=sections.get("Context", ""),
        warnings=warnings,
    )
    return ReadResult(project=project, stop=None)


def refused_stop(findings: Sequence[Finding]) -> StopReport:
    errors = [finding.message for finding in findings if finding.severity == "error"]
    evidence = "\n".join(errors) if errors else "factory.md refused"
    return StopReport(
        stop="refused",
        evidence=evidence,
        next_step=RECOMMENDED_NEXT["refused"],
    )


def choose_route(
    request: RouteRequest,
    usage: object | None = None,
) -> RouteDecision:
    """Adapt `lib.usage.route_job(config, marked_urgent=urgent)` into a decision.

    The import is lazy so this module does not cycle with `lib.usage`.
    `lib.usage` must not import `FitChoice`. A route that is not ok raises
    `RouteError`. `usage`, when given, is forwarded and is the only extra
    argument.
    """
    _check_request(request)
    usage_mod = importlib.import_module("lib.usage")
    if usage is None:
        route = usage_mod.route_job(request.config, marked_urgent=request.urgent)
    else:
        route = usage_mod.route_job(
            request.config,
            usage=usage,
            marked_urgent=request.urgent,
        )
    if not route.ok:
        reason = route.reason
        if not isinstance(reason, str) or reason.strip() == "":
            raise RouteError("route not ok")
        raise RouteError(reason)
    builder = route.builder
    reviewer = route.reviewer
    if builder is None or reviewer is None:
        raise RouteError("route not ok")
    fit = FitChoice(
        builder_harness=builder.harness,
        builder_model=builder.model,
        reviewer_harness=reviewer.harness,
        reviewer_model=reviewer.model,
    )
    return route_from_fit(request, fit)


def route_from_fit(request: RouteRequest, fit: FitChoice) -> RouteDecision:
    _check_request(request)
    _check_fit(fit)
    builder_family, reviewer_family = assert_cross_family(
        fit.builder_harness,
        fit.builder_model,
        fit.reviewer_harness,
        fit.reviewer_model,
    )
    file_count = len(request.likely_files)
    job = job_for_file_count(file_count, scope_changed=request.scope_changed)
    return RouteDecision(
        builder_harness=fit.builder_harness,
        builder_model=fit.builder_model,
        reviewer_harness=fit.reviewer_harness,
        reviewer_model=fit.reviewer_model,
        builder_family=builder_family,
        reviewer_family=reviewer_family,
        job=job,
        urgent=request.urgent,
        risk=request.risk,
        file_count=file_count,
        scope_changed=request.scope_changed,
    )


def assert_cross_family(
    builder_harness: str,
    builder_model: str | None,
    reviewer_harness: str,
    reviewer_model: str | None,
) -> tuple[str, str]:
    """Return (builder family, reviewer family) using the check-config rule."""
    findings = _family_findings(
        builder_harness,
        builder_model,
        reviewer_harness,
        reviewer_model,
    )
    errors = [finding.message for finding in findings if finding.severity == "error"]
    if errors:
        raise RouteError(errors[0])
    builder_family, _builder_hint = _resolved_family(builder_harness, builder_model)
    reviewer_family, _reviewer_hint = _resolved_family(reviewer_harness, reviewer_model)
    if builder_family is None or reviewer_family is None:
        raise RouteError("same-family reviewer: family not established")
    if builder_family == reviewer_family:
        raise RouteError(
            "same-family reviewer: "
            f"builder family {builder_family!r} and "
            f"reviewer family {reviewer_family!r}"
        )
    return builder_family, reviewer_family


def job_for_file_count(count: int, *, scope_changed: bool = False) -> str:
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise RouteError("file count must be a non-negative integer")
    if not isinstance(scope_changed, bool):
        raise RouteError("scope_changed must be a boolean")
    if scope_changed or count > 3:
        return "plan"
    return "build"


def state_for_job(job: str) -> str:
    if job == "plan":
        return "planning"
    if job == "build":
        return "building"
    raise RouteError(f"unknown job {job!r}")


def product_questions_cleared(questions: Sequence[ClassifiedQuestion]) -> bool:
    return all(
        question.classification != "product" or question.answer is not None
        for question in questions
    )


def next_product_question(
    questions: Sequence[ClassifiedQuestion],
) -> ClassifiedQuestion | None:
    for question in questions:
        if question.classification == "product" and question.answer is None:
            return question
    return None


def brief_comment(
    *,
    risk: str,
    goal: str,
    acceptance_criteria: Sequence[str],
    likely_files: Sequence[str],
    done_evidence: str,
    visible: bool = False,
) -> str:
    if risk not in ("low", "high"):
        raise OrchestrateError("risk must be low or high")
    if not isinstance(goal, str) or goal.strip() == "":
        raise OrchestrateError("goal is empty")
    if not isinstance(done_evidence, str) or done_evidence.strip() == "":
        raise OrchestrateError("done-evidence is empty")
    criteria = [_one_line(item, "acceptance criterion") for item in acceptance_criteria]
    if not criteria:
        raise OrchestrateError("acceptance criteria are empty")
    files = [_one_line(item, "likely file") for item in likely_files]
    body = "".join(
        (
            _block("Goal", goal.strip()),
            _items("Acceptance criteria", criteria),
            _items("Likely files", files),
            _block("Risk", risk),
            _block("Done-evidence", done_evidence.strip()),
        )
    )
    return _comment(
        {"kind": "brief", "from": FROM, "role": ROLE, "risk": risk},
        body,
        visible=visible,
    )


def questions_comment(
    restatement: str,
    questions: Sequence[ClassifiedQuestion],
    *,
    visible: bool = False,
) -> str:
    if not isinstance(restatement, str) or restatement.strip() == "":
        raise QuestionError("restatement is empty")
    for question in questions:
        if not isinstance(question, ClassifiedQuestion):
            raise QuestionError("question must be classified")
    lines = ["**Restatement**", restatement.strip(), "", "**Questions**"]
    if not questions:
        lines.append("None.")
    else:
        for question in questions:
            lines.append(f"- [{question.classification}] {question.text}")
            if question.classification == "default-able":
                lines.append(f"  Default: {question.default}")
    fields: dict[str, str] = {"kind": "questions", "from": FROM, "role": ROLE}
    if next_product_question(questions) is not None:
        fields["state"] = "waiting-max"
    return _comment(fields, "\n".join(lines) + "\n", visible=visible)


def answers_comment(
    question: str,
    answer: str,
    *,
    visible: bool = False,
) -> str:
    _one_line(question, "question text", QuestionError)
    _one_line(answer, "answer", QuestionError)
    body = f"**Question**\n{question.strip()}\n\n**Answer**\n{answer.strip()}\n"
    return _comment(
        {"kind": "answers", "from": FROM, "role": ROLE},
        body,
        visible=visible,
    )


def go_comment(
    decision: RouteDecision,
    verify: str,
    *,
    visible: bool = False,
) -> str:
    if not isinstance(decision, RouteDecision):
        raise RouteError("decision must be a RouteDecision")
    if decision.risk not in ("low", "high"):
        raise RouteError("risk must be low or high")
    if decision.builder_harness not in HARNESSES:
        raise RouteError("builder harness must be claude, codex or cursor")
    _one_line(verify, "verify", RouteError)
    if decision.job not in JOBS:
        raise RouteError(f"unknown job {decision.job!r}")
    body = _go_body(decision, verify.strip())
    return _comment(
        {
            "kind": "go",
            "from": FROM,
            "role": ROLE,
            "risk": decision.risk,
            "state": state_for_job(decision.job),
            "builder": decision.builder_harness,
        },
        body,
        visible=visible,
    )


def format_stop_report(report: StopReport) -> str:
    return (
        f"**Stop**\n{report.stop}\n\n"
        f"**Evidence**\n{report.evidence.strip()}\n\n"
        f"**Next step**\n{report.next_step.strip()}\n"
    )


def stuck_comment(report: StopReport, *, visible: bool = False) -> str:
    if not isinstance(report, StopReport):
        raise StopError("stop must be a StopReport")
    if report.stop != "stuck":
        raise StopError("stuck comment is only for stop stuck")
    return _comment(
        {"kind": "stuck", "from": FROM, "role": ROLE, "state": "stuck"},
        format_stop_report(report),
        visible=visible,
    )


def _check_request(request: RouteRequest) -> None:
    if not isinstance(request, RouteRequest):
        raise RouteError("request must be a RouteRequest")
    if not isinstance(request.config, FactoryConfig):
        raise RouteError("config must be a FactoryConfig")
    if request.risk not in ("low", "high"):
        raise RouteError("risk must be low or high")
    if not isinstance(request.urgent, bool):
        raise RouteError("urgent must be a boolean")
    if not isinstance(request.scope_changed, bool):
        raise RouteError("scope_changed must be a boolean")
    if not isinstance(request.summary, str) or request.summary.strip() == "":
        raise RouteError("summary is empty")
    for path in request.likely_files:
        _one_line(path, "likely file", RouteError)


def _check_fit(fit: FitChoice) -> None:
    if not isinstance(fit, FitChoice):
        raise RouteError("fit must be a FitChoice")
    if fit.builder_harness not in HARNESSES:
        raise RouteError("builder harness must be claude, codex or cursor")
    if fit.reviewer_harness not in HARNESSES:
        raise RouteError("reviewer harness must be claude, codex or cursor")
    if fit.builder_model is not None:
        _one_line(fit.builder_model, "builder model", RouteError)
    if fit.reviewer_model is not None:
        _one_line(fit.reviewer_model, "reviewer model", RouteError)


def _go_body(decision: RouteDecision, verify: str) -> str:
    return (
        f"**Job**\n{decision.job}\n\n"
        f"**Builder**\n{decision.builder_harness}\n\n"
        f"**Builder model**\n{_or_none(decision.builder_model)}\n\n"
        f"**Reviewer**\n{decision.reviewer_harness}\n\n"
        f"**Reviewer model**\n{_or_none(decision.reviewer_model)}\n\n"
        f"**Verify**\n{verify}\n\n"
        f"**Files**\n{decision.file_count}\n"
    )


def _or_none(value: str | None) -> str:
    if value is None:
        return "none"
    return value


def _sections(body: str) -> dict[str, str]:
    current: str | None = None
    chunks: dict[str, list[str]] = {}
    for line in body.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            chunks.setdefault(current, [])
            continue
        if current is not None:
            chunks[current].append(line)
    return {name: "\n".join(lines).strip() for name, lines in chunks.items()}


def _items(title: str, items: Sequence[str]) -> str:
    if not items:
        rendered = "None."
    else:
        rendered = "\n".join(f"- {item}" for item in items)
    return f"**{title}**\n{rendered}\n\n"


def _block(title: str, text: str) -> str:
    return f"**{title}**\n{text}\n\n"


def _comment(
    fields: Mapping[str, str],
    body: str,
    *,
    visible: bool = False,
) -> str:
    text = body if body.endswith("\n") else body + "\n"
    return format_comment(
        Handoff(fields=tuple(fields.items()), body=text),
        visible=visible,
    )


def _one_line(
    value: object,
    label: str,
    error: type[OrchestrateError] = OrchestrateError,
) -> str:
    if not isinstance(value, str) or value.strip() == "" or "\n" in value:
        raise error(f"{label} must be one non-empty line")
    return value.strip()
