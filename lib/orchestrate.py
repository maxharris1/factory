"""Grok Bot `orchestrate-project` interface (docs/SPEC.md F1-07).

Stable names for F1-08, which owns best fit and the usage reserve:

- `RouteRequest` — input. Do not add a required field.
- `FitChoice` — harness pair built from a `route_job` result.
- `RouteDecision` — what `orch:go` posts. Families and `job` are set here,
  with the reserve and fallback flags `choose_route` read from the route.
- `choose_route(request)` — checks the factory.md pair with
  `assert_cross_family`, then calls `lib.usage.route_job(config, marked_urgent=urgent)` once.
- `route_from_fit(request, fit)` — cross-family check, then a decision.
- `assert_cross_family(...)` — the check-config family rule. Same family raises.

`choose_route` does not rank fits and does not read a usage source.
`lib.usage` must not import `FitChoice`. The import stays inside
`choose_route` so the two modules do not cycle.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from lib.factory_md import (
    FactoryConfig,
    family_findings,
    load_factory_md,
    resolved_family,
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
    "stuck": "Factory retries by cause and replans after two retries.",
    "blocked": "Read the evidence. Max decides the unblock.",
    "capped": "Factory splits or replans. No extra round is asked of Max.",
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
    built_by: tuple[str, str | None] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.likely_files, str):
            raise RouteError("likely_files must be a sequence of paths, not a string")
        object.__setattr__(self, "likely_files", tuple(self.likely_files))
        if self.built_by is not None:
            object.__setattr__(self, "built_by", tuple(self.built_by))


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
    builder_using_reserve: bool = False
    reviewer_using_reserve: bool = False
    builder_fallback: bool = False
    reviewer_fallback: bool = False
    built_by: tuple[str, str | None] | None = None


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
                _text(self.answer, "answer", QuestionError),
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
        answer = _text(answer, "answer", QuestionError)
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

    def post_answers(
        self,
        answers: Sequence[tuple[str, str]],
        *,
        visible: bool = False,
    ) -> str:
        """One answers comment for every open product question, in question order."""
        if not self._questions_posted:
            raise QuestionError("questions step has not run")
        given = _answer_pairs(answers)
        open_texts = [
            question.text
            for question in self._questions
            if question.classification == "product" and question.answer is None
        ]
        if not open_texts:
            raise QuestionError("no open product question")
        if set(given) != set(open_texts):
            raise QuestionError("answer every open product question, and only those")
        self._questions = tuple(
            replace(question, answer=given[question.text])
            if question.text in given and question.answer is None
            else question
            for question in self._questions
        )
        return answers_for(
            [(text, given[text]) for text in open_texts],
            visible=visible,
        )

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

    The factory.md pair must pass `assert_cross_family` first, so a config
    check-config would refuse is refused here rather than routed around.
    The import is lazy so this module does not cycle with `lib.usage`.
    `lib.usage` must not import `FitChoice`. A route that is not ok raises
    `RouteError`. `usage` and `request.built_by` are forwarded.
    """
    _check_request(request)
    config = request.config
    assert_cross_family(
        config.builder_harness,
        config.builder_model,
        config.reviewer_harness,
        config.reviewer_model,
    )
    usage_mod = importlib.import_module("lib.usage")
    try:
        route = usage_mod.route_job(
            config,
            usage=usage,
            marked_urgent=request.urgent,
            built_by=request.built_by,
        )
    except ValueError as exc:
        raise RouteError(str(exc)) from exc
    if not route.ok:
        reason = route.reason
        if not isinstance(reason, str) or reason.strip() == "":
            raise RouteError("route not ok")
        raise RouteError(reason)
    builder = route.builder
    reviewer = route.reviewer
    fit = FitChoice(
        builder_harness=builder.harness,
        builder_model=builder.model,
        reviewer_harness=reviewer.harness,
        reviewer_model=reviewer.model,
    )
    return replace(
        route_from_fit(request, fit),
        builder_using_reserve=builder.using_reserve,
        reviewer_using_reserve=reviewer.using_reserve,
        builder_fallback=(
            request.built_by is None and builder.harness != config.builder_harness
        ),
        reviewer_fallback=reviewer.harness != config.reviewer_harness,
    )


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
        built_by=request.built_by,
    )


def assert_cross_family(
    builder_harness: str,
    builder_model: str | None,
    reviewer_harness: str,
    reviewer_model: str | None,
) -> tuple[str, str]:
    """Return (builder family, reviewer family) using the check-config rule."""
    findings = family_findings(
        builder_harness,
        builder_model,
        reviewer_harness,
        reviewer_model,
    )
    errors = [finding.message for finding in findings if finding.severity == "error"]
    if errors:
        raise RouteError(errors[0])
    builder_family, _builder_hint = resolved_family(builder_harness, builder_model)
    reviewer_family, _reviewer_hint = resolved_family(reviewer_harness, reviewer_model)
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
            _block("Goal", _contained(goal.strip())),
            _items("Acceptance criteria", [f"[ ] {item}" for item in criteria]),
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
    question = _one_line(question, "question text", QuestionError)
    answer = _text(answer, "answer", QuestionError)
    body = f"**Question**\n{question}\n\n**Answer**\n{answer}\n"
    return _comment(
        {"kind": "answers", "from": FROM, "role": ROLE},
        body,
        visible=visible,
    )


def answers_for(
    answers: Sequence[tuple[str, str]],
    *,
    visible: bool = False,
) -> str:
    """One answers comment holding every answer to one questions comment.

    The dispatcher resumes the paused step on the first trusted answers and
    rejects any later one, so all answers go in this single comment."""
    pairs = _answer_pairs(answers)
    body = "\n".join(
        f"**Question**\n{question}\n\n**Answer**\n{answer}\n"
        for question, answer in pairs.items()
    )
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
    if decision.reviewer_harness not in HARNESSES:
        raise RouteError("reviewer harness must be claude, codex or cursor")
    _one_line(verify, "verify", RouteError)
    if decision.job not in JOBS:
        raise RouteError(f"unknown job {decision.job!r}")
    families = assert_cross_family(
        decision.builder_harness,
        decision.builder_model,
        decision.reviewer_harness,
        decision.reviewer_model,
    )
    if families != (decision.builder_family, decision.reviewer_family):
        raise RouteError(
            f"builder family {decision.builder_family!r} and reviewer family "
            f"{decision.reviewer_family!r} do not match the harnesses"
        )
    expected = job_for_file_count(
        decision.file_count, scope_changed=decision.scope_changed
    )
    if decision.job != expected:
        raise RouteError(
            f"job {decision.job!r} does not match {decision.file_count} files"
        )
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


APPROVAL_TOKEN_VARIABLES = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
)


@dataclass(frozen=True)
class ApproverGo:
    body: str
    env: dict[str, str]


def approver_go_post(
    kind: str,
    max_words: str,
    environ: Mapping[str, str],
    *,
    app_trust: bool,
    gh_config_dir: Path | None,
    visible: bool = False,
) -> ApproverGo:
    """Short-term (#172, until T02f #173): the capped-ticket go Grok posts
    as maxharris1, quoting Max's exact words, with the gh environment from
    orch.yml's approvals (lib.orch_yml.approvals). Any other kind, ok above
    all, is refused: Max posts his own ok. Token variables are always
    dropped. With the app switch on, gh reads only Max's approver profile;
    with none configured this refuses."""
    if kind != "go":
        raise OrchestrateError(
            f"never post kind={kind} as Max: only a capped-ticket go, and Max posts his own ok"
        )
    body = _comment(
        {"kind": "go", "from": FROM, "role": ROLE, "state": "building"},
        _max_words_block(max_words),
        visible=visible,
    )
    return ApproverGo(body, _approver_env(environ, app_trust, gh_config_dir))


def extra_round_go(
    ticket: str,
    max_words: str,
    instruction: str,
    environ: Mapping[str, str],
    *,
    app_trust: bool,
    gh_config_dir: Path | None,
    visible: bool = False,
) -> ApproverGo:
    """The go for one extra round on a capped ticket (D16, short-term until
    T02f #173): the ticket, Max's exact words quoted unchanged, and the
    instruction for the round, posted as maxharris1 through the same gh
    environment as approver_go_post. It writes a go and nothing else."""
    ticket = _one_line(ticket, "ticket")
    instruction = _text(instruction, "instruction")
    body = (
        f"**Ticket**\n{ticket}\n\n"
        f"{_max_words_block(max_words)}\n"
        f"**Instruction**\n{instruction}\n"
    )
    comment = _comment(
        {"kind": "go", "from": FROM, "role": ROLE, "state": "building"},
        body,
        visible=visible,
    )
    return ApproverGo(comment, _approver_env(environ, app_trust, gh_config_dir))


def _max_words_block(max_words: str) -> str:
    words = _text(max_words, "Max's exact words")
    quoted = "\n".join(f"> {line}".rstrip() for line in words.splitlines())
    return f"**Max's words**\n{quoted}\n"


def _approver_env(
    environ: Mapping[str, str],
    app_trust: bool,
    gh_config_dir: Path | None,
) -> dict[str, str]:
    env = {key: value for key, value in environ.items() if key not in APPROVAL_TOKEN_VARIABLES}
    if not app_trust:
        return env
    if gh_config_dir is None:
        raise OrchestrateError(
            "no approver_gh_config_dir in orch.yml: post it from maxharris1 by hand"
        )
    env["GH_CONFIG_DIR"] = str(gh_config_dir)
    return env


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
    if request.built_by is not None:
        if len(request.built_by) != 2:
            raise RouteError("built_by must be (harness, model)")
        harness, model = request.built_by
        _one_line(harness, "built_by harness", RouteError)
        if model is not None:
            _one_line(model, "built_by model", RouteError)


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
        f"{_routing(decision)}"
    )


def _routing(decision: RouteDecision) -> str:
    flags = (
        (decision.builder_fallback, "Builder is a fallback from factory.md's builder."),
        (decision.builder_using_reserve, "Builder is using its usage reserve."),
        (decision.reviewer_fallback, "Reviewer is a fallback from factory.md's reviewer."),
        (decision.reviewer_using_reserve, "Reviewer is using its usage reserve."),
    )
    lines = [text for flag, text in flags if flag]
    if not lines:
        return ""
    lines.append("For information; the dispatcher runs factory.md's pair.")
    return "\n**Routing**\n" + "\n".join(lines) + "\n"


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


def _contained(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


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


def _answer_pairs(answers: Sequence[tuple[str, str]]) -> dict[str, str]:
    if isinstance(answers, str):
        raise QuestionError("each answer must be a question and answer pair")
    pairs: dict[str, str] = {}
    for pair in answers:
        if isinstance(pair, str) or len(pair) != 2:
            raise QuestionError("each answer must be a question and answer pair")
        question = _one_line(pair[0], "question text", QuestionError)
        if question in pairs:
            raise QuestionError(f"question {question!r} answered twice")
        pairs[question] = _text(pair[1], "answer", QuestionError)
    if not pairs:
        raise QuestionError("answers need at least one answer")
    return pairs


def _text(
    value: object,
    label: str,
    error: type[OrchestrateError] = OrchestrateError,
) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise error(f"{label} must not be empty")
    return value.strip()


def _one_line(
    value: object,
    label: str,
    error: type[OrchestrateError] = OrchestrateError,
) -> str:
    if not isinstance(value, str) or value.strip() == "" or "\n" in value:
        raise error(f"{label} must be one non-empty line")
    return value.strip()
