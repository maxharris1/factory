from __future__ import annotations

import ast
import dataclasses
import inspect
import json
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from lib.dispatch import review
from lib.dispatch.review import ReviewCoordinator, ReviewPort
from lib.dispatcher import Dispatcher
from tests.test_dispatcher import VERDICT
from tests.test_first_cap_grant import rows
from tests.test_round_grant import fix_keys
from tests.test_t3_convergence import ConvergingFlow

ROUNDS = pytest.mark.parametrize("auto_extra_rounds", [0, 2])
CAP_LINE = (
    "orch poll: engine owner/repo 42 site=cap subject=revise-3 rule=first-cap-grant"
    " outcome=escalated reason=convergence-owns-cap"
)


class EngineFlow(ConvergingFlow):
    def __init__(self, tmp_path: Path, auto_extra_rounds: int, cap: int = 3) -> None:
        super().__init__(tmp_path, "convergence")
        self.factory = dataclasses.replace(
            self.factory, rule_engine="on", auto_extra_rounds=auto_extra_rounds, rounds_high=cap
        )

    def revise_with(self, round_text: str, ids: Sequence[str], repeat: str | None = None) -> str:
        assert repeat is None
        body = VERDICT.format(round=round_text, head=self.head, verdict="REVISE", risk="high")
        body += "".join(f"- [blocking] {rule_id}: still open.\n" for rule_id in ids)
        return self.post(f"revise-{round_text.split('/')[0]}", body, "codex-login")


def new_low(tmp_path: Path, auto_extra_rounds: int) -> tuple[EngineFlow, str]:
    flow = EngineFlow(tmp_path, auto_extra_rounds)
    return flow, flow.falling_to_round_three()


def carried(tmp_path: Path, auto_extra_rounds: int) -> tuple[EngineFlow, str]:
    flow = EngineFlow(tmp_path, auto_extra_rounds)
    assert flow.go("go-1") == "queued-build"
    flow.finish("claude")
    flow.review(1)
    assert flow.revise_with("1/3", ["f-1", "f-2"]) == "queued-review-fix"
    flow.finish("claude")
    flow.review(2)
    return flow, flow.revise_with("2/3", ["f-1", "f-3"])


def stalled(tmp_path: Path, auto_extra_rounds: int) -> tuple[EngineFlow, str]:
    flow = EngineFlow(tmp_path, auto_extra_rounds, cap=4)
    assert flow.go("go-1") == "queued-build"
    flow.finish("claude")
    for number in (1, 2):
        flow.review(number, cap=4)
        assert flow.revise_with(f"{number}/4", flow.fresh(2)) == "queued-review-fix"
        flow.finish("claude")
    flow.review(3, cap=4)
    return flow, flow.revise_with("3/4", flow.fresh(2))


SCENARIOS: dict[str, Callable[[Path, int], tuple[EngineFlow, str]]] = {
    "new-low": new_low,
    "carried": carried,
    "stalled": stalled,
}


def grok_jobs(flow: EngineFlow) -> list[tuple[str, str, str]]:
    with flow.queue.connect() as connection:
        found = connection.execute(
            "SELECT action, source_comment_id, detail FROM jobs WHERE recipient = 'grok' ORDER BY created_at, id"
        ).fetchall()
    return [(row["action"], row["source_comment_id"], json.loads(row["detail"])["detail"]) for row in found]


def observed(flow: EngineFlow, result: str) -> tuple:
    state = flow.state()
    with flow.queue.connect() as connection:
        engine = connection.execute(
            "SELECT site, subject, outcome, reason FROM engine_decisions ORDER BY decided_at, subject"
        ).fetchall()
        bodies = connection.execute("SELECT body FROM auto_extra_comments ORDER BY created_at, comment_id").fetchall()
    return (
        result,
        state["workflow_state"],
        state["review_round"],
        state["cap_extra"],
        [tuple(row) for row in engine],
        [str(row["body"]) for row in bodies],
        list(flow.actions.posted),
        grok_jobs(flow),
    )


@ROUNDS
def test_a_new_low_at_the_cap_spends_a_round(tmp_path: Path, auto_extra_rounds: int) -> None:
    flow, result = new_low(tmp_path, auto_extra_rounds)
    result, state, round_number, cap_extra, engine, bodies, posted, grok = observed(flow, result)
    assert (result, state, round_number, cap_extra, engine, grok) == ("queued-review-fix", "fixing", 3, 1, [], [])
    [notice] = posted
    assert notice.startswith("Extra round 1: this round cut the open blocking findings to a new low")
    assert bodies == [notice]


@ROUNDS
def test_a_carried_blocker_repeats_and_the_engine_grants_the_first_repeat(
    tmp_path: Path, auto_extra_rounds: int
) -> None:
    result, state, _, cap_extra, engine, bodies, posted, grok = observed(*carried(tmp_path, auto_extra_rounds))
    assert (result, state, cap_extra, engine, bodies, grok) == (
        "waiting-max-repeat",
        "fixing",
        0,
        [("repeat", "revise-2", "granted", None)],
        [],
        [],
    )
    [notice] = posted
    assert "<!-- orch-engine site=repeat subject=revise-2 -->" in notice.splitlines()


@ROUNDS
def test_two_flat_rounds_stall_below_the_cap(tmp_path: Path, auto_extra_rounds: int) -> None:
    result, state, round_number, cap_extra, _, bodies, posted, grok = observed(*stalled(tmp_path, auto_extra_rounds))
    assert (result, state, round_number, cap_extra, bodies, posted) == ("capped-review", "capped", 3, 0, [], [])
    [(action, subject, detail)] = grok
    assert (action, subject) == ("capped", "revise-3")
    assert detail.startswith("review not converging")


@ROUNDS
def test_a_stalled_cap_logs_convergence_owns_cap_and_grants_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], auto_extra_rounds: int
) -> None:
    flow, _ = stalled(tmp_path, auto_extra_rounds)
    [row] = [row for row in rows(flow) if row["site"] == "cap"]
    assert (row["subject"], row["outcome"], row["reason"], row["header"], row["notice_status"]) == (
        "revise-3",
        "escalated",
        "convergence-owns-cap",
        None,
        None,
    )
    assert CAP_LINE in capsys.readouterr().out.splitlines()
    assert [notice for notice in flow.actions.posted if "orch-engine site=cap" in notice] == []
    assert [key for _, key in fix_keys(flow) if ":fix:engine:" in key] == []
    assert [action for action, _, _ in grok_jobs(flow)] == ["capped"]


def test_auto_extra_rounds_does_not_change_a_convergence_stop(tmp_path: Path) -> None:
    for name, scenario in SCENARIOS.items():
        runs = []
        for auto_extra_rounds in (0, 2):
            path = tmp_path / f"{name}-{auto_extra_rounds}"
            path.mkdir()
            runs.append(observed(*scenario(path, auto_extra_rounds)))
        assert runs[0] == runs[1], name


def self_attributes(node: ast.AST, owner: str = "self") -> set[str]:
    return {
        found.attr
        for found in ast.walk(node)
        if isinstance(found, ast.Attribute) and ast.unparse(found.value) == owner
    }


def test_the_review_coordinator_is_a_collaborator_that_reaches_the_dispatcher_only_through_its_port(
    tmp_path: Path,
) -> None:
    dispatcher = EngineFlow(tmp_path, 0).dispatcher
    assert type(dispatcher.review) is ReviewCoordinator
    assert dispatcher.review.port is dispatcher
    assert ReviewCoordinator not in Dispatcher.__mro__
    assert [base for base in Dispatcher.__mro__ if base.__module__ == review.__name__] == []
    assert len(Dispatcher.__bases__) == 22
    assert isinstance(dispatcher, ReviewPort)

    signature = inspect.signature(ReviewCoordinator.__init__, eval_str=True)
    fields = list(signature.parameters.values())[1:]
    assert [field.name for field in fields] == ["queue", "connect", "port"]
    assert [field.kind for field in fields] == [inspect.Parameter.KEYWORD_ONLY] * 3
    assert all(field.annotation is not inspect.Parameter.empty for field in fields)
    assert signature.parameters["port"].annotation is ReviewPort

    tree = ast.parse(Path(review.__file__).read_text())
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    assert set(classes) == {"ReviewPort", "ReviewCoordinator"}
    coordinator = classes["ReviewCoordinator"]
    methods = {node.name for node in coordinator.body if isinstance(node, ast.FunctionDef)}
    declared = {node.name for node in classes["ReviewPort"].body if isinstance(node, ast.FunctionDef)}
    assert self_attributes(coordinator) - {field.name for field in fields} - methods == set()
    assert self_attributes(coordinator, "self.port") == declared
    outside = [node for node in tree.body if node not in classes.values()]
    assert [name for node in outside for name in ast.walk(node) if isinstance(name, ast.Name) and name.id == "self"] == []
