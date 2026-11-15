import ast
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from lib.eval_report import CHART_TRANSITIONS, EvalError, build_report, render
from lib.factory_md import load_factory_md
from lib.handoff import STATES, parse_comment
from lib.orch_yml import load_orch_yml

ROOT = Path(__file__).resolve().parents[1]
WEEK = ROOT / "tests" / "fixtures" / "eval" / "week"

# Section 4.1 stateDiagram names, mapped to lib.handoff.STATES.
SPEC_STATE = {
    "Intake": "intake",
    "Refused": "refused",
    "Questions": "questions",
    "WaitingOnMax": "waiting-max",
    "Planning": "planning",
    "PlanCheck": "plan-check",
    "Building": "building",
    "Review": "review",
    "Fixing": "fixing",
    "Capped": "capped",
    "Blocked": "blocked",
    "Ship": "ship",
    "Merged": "merged",
    "HandedOff": "handed-off",
    "MergeQueue": "merge-queue",
    "Updating": "updating",
    "WaitingExternal": "waiting-external",
    "AwaitingOk": "awaiting-ok",
    "PostMerge": "post-merge",
    "Done": "done",
    "Stuck": "stuck",
    "Abandoned": "abandoned",
}

EXPECTED = """\
weekly eval report
runs: 5

time in each state
intake 70
questions 70
building 340
review 130
ship 30
merged 35
post-merge 0
done 0
stuck 0
qa 0

open states
none

wasted agent wakes
count: 2
2026-11-03T15:04:30Z maxharris1/Cleffy 12 claude-bot fix
2026-11-03T15:08:00Z maxharris1/Cleffy 12 grok-bot nobody

review rounds per PR
maxharris1/Cleffy 9 0
maxharris1/Cleffy 12 1
maxharris1/Cleffy 15 0
maxharris1/tracy-harris-coaching 3 0
maxharris1/tracy-harris-coaching 4 2

stuck jobs
maxharris1/Cleffy 9

rule breaks the model finds
2026-11-03T15:08:00Z maxharris1/Cleffy 12 ship-unpinned approval head does not match covered head

chart mismatches
maxharris1/Cleffy 15 post-merge-in-phase-1
maxharris1/tracy-harris-coaching 3 illegal-transition review qa
maxharris1/tracy-harris-coaching 3 unknown-state qa
maxharris1/tracy-harris-coaching 4 round-over-cap low 2 1

bot-to-bot messages vs scripted handoffs
maxharris1/Cleffy 2026-W45 manual=0 scripted=6
maxharris1/tracy-harris-coaching 2026-W45 manual=0 scripted=1
token savings: not measured (no per-message token count in the input)
"""

WINDOW = """\
weekly eval report
runs: 1

time in each state
intake 10
questions 10
building 100
stuck 0

open states
none

wasted agent wakes
count: 0

review rounds per PR
maxharris1/Cleffy 9 0

stuck jobs
maxharris1/Cleffy 9

rule breaks the model finds
none

chart mismatches
none

bot-to-bot messages vs scripted handoffs
maxharris1/Cleffy 2026-W45 manual=0 scripted=1
token savings: not measured (no per-message token count in the input)
"""

MIN_FACTORY = """\
---
base_branch: main
tickets: github
builder:
  harness: claude
reviewer:
  harness: codex
review:
  rounds:
    low: 1
    high: 3
  verify: none
ship: merge
skills: []
---
"""

MIN_ORCH = """\
poll_seconds: 60
projects:
  - repo: maxharris1/Cleffy
"""


def run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "bin" / "orch"), *args],
        cwd=cwd or ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def write_bundle(
    root: Path,
    log: str,
    comments: dict[str, str] | None = None,
    *,
    factory: str = MIN_FACTORY,
    orch: str = MIN_ORCH,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "orch.yml").write_text(orch, encoding="utf-8")
    factory_path = root / "projects" / "maxharris1" / "Cleffy" / "factory.md"
    factory_path.parent.mkdir(parents=True)
    factory_path.write_text(factory, encoding="utf-8")
    (root / "dispatcher.log").write_text(log, encoding="utf-8")
    for relative, text in (comments or {}).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_fixture_report_covers_every_f1_14_measure() -> None:
    orch, orch_findings = load_orch_yml(WEEK / "orch.yml")
    assert orch is not None
    assert not any(item.severity == "error" for item in orch_findings)
    tracy, tracy_findings = load_factory_md(
        WEEK / "projects" / "maxharris1" / "tracy-harris-coaching" / "factory.md"
    )
    assert tracy is not None and tracy.rounds_low == 1
    assert not any(item.severity == "error" for item in tracy_findings)
    ready = sorted((WEEK / "comments" / "cleffy-12").glob("ready*.txt"))
    assert len(ready) == 2
    verdict = parse_comment(
        (WEEK / "comments" / "cleffy-12" / "verdict.txt").read_text(encoding="utf-8")
    ).as_dict()
    assert verdict["kind"] == "verdict"
    assert verdict["round"] == "1/1"
    stuck = parse_comment(
        (WEEK / "comments" / "cleffy-9" / "stuck.txt").read_text(encoding="utf-8")
    ).as_dict()
    assert stuck["kind"] == "stuck"
    text = render(build_report(WEEK).report)
    assert text == EXPECTED
    for heading in (
        "time in each state",
        "wasted agent wakes",
        "review rounds per PR",
        "stuck jobs",
        "rule breaks the model finds",
    ):
        assert heading in text


SPEC_EDGE = re.compile(r"  (\[\*\]|[A-Za-z]+) --> (\[\*\]|[A-Za-z]+)(?:: .+)?")
MMD_EDGE = re.compile(r"  s(\d+) --> s(\d+): .+")


def spec_edges(spec: str) -> set[tuple[str, str]]:
    start = spec.index("stateDiagram-v2")
    block = spec[start : spec.index("```", start)]
    found: set[tuple[str, str]] = set()
    for line in block.splitlines():
        if "-->" not in line:
            continue
        match = SPEC_EDGE.fullmatch(line)
        assert match is not None, f"unparsed section 4.1 edge: {line!r}"
        src, dst = match.groups()
        if src == "[*]" or dst == "[*]":
            continue
        found.add((SPEC_STATE[src], SPEC_STATE[dst]))
    return found


def mmd_edges(text: str) -> set[tuple[str, str]]:
    names: dict[str, str] = {}
    edges: set[tuple[str, str]] = set()
    for line in text.splitlines():
        state = re.fullmatch(r"  s(\d+): (.+)", line)
        if state:
            names[state.group(1)] = state.group(2)
            continue
        if "-->" not in line or line.startswith("  [*] -->"):
            continue
        edge = MMD_EDGE.fullmatch(line)
        assert edge is not None, f"unparsed chart.mmd edge: {line!r}"
        pair = (names[edge.group(1)], names[edge.group(2)])
        assert pair not in edges, f"chart.mmd draws {pair} twice"
        edges.add(pair)
    return edges


def test_chart_edges_match_section_4_1() -> None:
    spec = (ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8")
    assert spec_edges(spec) == CHART_TRANSITIONS
    assert set(SPEC_STATE.values()) == set(STATES)
    for src, dst in CHART_TRANSITIONS:
        assert src in STATES
        assert dst in STATES


def test_chart_mmd_edges_match_transitions() -> None:
    text = (ROOT / "model" / "chart.mmd").read_text(encoding="utf-8")
    assert mmd_edges(text) == CHART_TRANSITIONS


def test_t2_self_loops_are_charted() -> None:
    tla = (ROOT / "model" / "Orchestrator.tla").read_text(encoding="utf-8")
    start = tla.index("T2CiRed ==")
    action = tla[start : tla.index("\n\n", start)]
    assert "state' = state" in action
    looped = re.search(r"state \\in \{([^}]*)\}", action)
    assert looped is not None
    states = re.findall(r'"([a-z-]+)"', looped.group(1))
    assert states == ["building", "fixing"]
    spec = spec_edges((ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8"))
    chart = mmd_edges((ROOT / "model" / "chart.mmd").read_text(encoding="utf-8"))
    for state in states:
        assert (state, state) in CHART_TRANSITIONS
        assert (state, state) in spec
        assert (state, state) in chart


@pytest.mark.parametrize(
    ("source", "bad", "message"),
    [
        ("spec", "  Fixing -->Review: CI green", "unparsed section 4.1 edge"),
        ("chart", "  s15 --> s9", "unparsed chart.mmd edge"),
        ("chart", "  s3 --> s9: ready again", "draws .* twice"),
    ],
    ids=["spec-unparsed", "chart-unlabelled", "chart-duplicate"],
)
def test_chart_edges_fail_on_an_unparsed_line(
    source: str, bad: str, message: str
) -> None:
    if source == "spec":
        spec = (ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8")
        spec = spec.replace("  Done --> [*]", f"{bad}\n  Done --> [*]", 1)
        with pytest.raises(AssertionError, match=message):
            spec_edges(spec)
    else:
        text = (ROOT / "model" / "chart.mmd").read_text(encoding="utf-8")
        text = text.replace("  class s0 ", f"{bad}\n  class s0 ", 1)
        with pytest.raises(AssertionError, match=message):
            mmd_edges(text)


def test_week_window_uses_the_same_directory_shape() -> None:
    text = render(
        build_report(
            WEEK,
            since="2026-11-03T16:00:00Z",
            until="2026-11-03T16:02:00Z",
        ).report
    )
    assert text == WINDOW


def test_any_directory_can_be_the_weekly_input(tmp_path: Path) -> None:
    copied = tmp_path / "collected"
    shutil.copytree(WEEK, copied)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert render(build_report(copied).report) == EXPECTED
    result = run(
        ["eval-report", "--input", str(copied)],
        cwd=elsewhere,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == EXPECTED
    assert result.stderr == ""


def test_cli_week_window_flags() -> None:
    result = run(
        [
            "eval-report",
            "--input",
            str(WEEK),
            "--since",
            "2026-11-03T16:00:00Z",
            "--until",
            "2026-11-03T16:02:00Z",
        ]
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == WINDOW


def test_open_state_is_listed_or_measured_to_until(tmp_path: Path) -> None:
    write_bundle(
        tmp_path,
        "\n".join(
            (
                "2026-11-03T15:00:00Z repo=maxharris1/Cleffy ticket=1 enter state=intake",
                "2026-11-03T15:01:00Z repo=maxharris1/Cleffy ticket=1 enter state=questions",
                "",
            )
        ),
    )
    text = render(build_report(tmp_path).report)
    assert "intake 60\nquestions 0\n" in text
    assert "open states\nmaxharris1/Cleffy 1 questions\n" in text
    text = render(build_report(tmp_path, until="2026-11-03T15:03:00Z").report)
    assert "intake 60\nquestions 120\n" in text
    assert "open states\nnone\n" in text


def verdict(round_: str | None = None, risk: str | None = None) -> str:
    keys = "kind=verdict from=codex-bot role=reviewer"
    if round_ is not None:
        keys += f" round={round_}"
    if risk is not None:
        keys += f" risk={risk}"
    return f"<!-- orch:v1 {keys} verdict=REVISE -->\nRevise.\n"


def go(risk: str) -> str:
    return f"<!-- orch:v1 kind=go from=grok-bot role=orchestrator risk={risk} -->\nGo.\n"


def log(*lines: str) -> str:
    return "".join(f"{line}\n" for line in lines)


def row(at: str, rest: str, ticket: str = "4") -> str:
    return f"2026-11-03T{at}Z repo=maxharris1/Cleffy ticket={ticket} {rest}"


def test_verdict_without_risk_is_cap_unverified(tmp_path: Path) -> None:
    write_bundle(
        tmp_path,
        log(
            row("18:00:00", "enter state=review"),
            row("18:00:10", "comment path=comments/verdict.txt"),
            row("18:00:10", "end"),
        ),
        {"comments/verdict.txt": verdict(round_="5/5")},
    )
    text = render(build_report(tmp_path).report)
    assert "maxharris1/Cleffy 4 5\n" in text
    assert "maxharris1/Cleffy 4 cap-unverified 5\n" in text
    assert "round-over-cap" not in text


def test_unnumbered_verdicts_take_risk_from_go(tmp_path: Path) -> None:
    write_bundle(
        tmp_path,
        log(
            row("18:00:00", "comment path=comments/go.txt"),
            row("18:00:00", "enter state=review"),
            row("18:00:10", "comment path=comments/v1.txt"),
            row("18:00:10", "enter state=fixing"),
            row("18:00:20", "enter state=review"),
            row("18:00:30", "comment path=comments/v2.txt"),
            row("18:00:30", "end"),
        ),
        {
            "comments/go.txt": go("low"),
            "comments/v1.txt": verdict(),
            "comments/v2.txt": verdict(risk="high"),
        },
    )
    text = render(build_report(tmp_path).report)
    assert "maxharris1/Cleffy 4 2\n" in text
    assert "maxharris1/Cleffy 4 round-over-cap low 2 1\n" in text
    assert "maxharris1/Cleffy 4 risk-mismatch low high\n" in text
    assert "cap-unverified" not in text


def test_extra_round_after_capped_is_authorized(tmp_path: Path) -> None:
    write_bundle(
        tmp_path,
        log(
            row("18:00:00", "enter state=review"),
            row("18:00:10", "comment path=comments/v1.txt"),
            row("18:00:10", "enter state=capped"),
            row("18:01:00", "enter state=fixing"),
            row("18:02:00", "enter state=review"),
            row("18:02:10", "comment path=comments/v2.txt"),
            row("18:02:10", "end"),
        ),
        {
            "comments/v1.txt": verdict(round_="1/1", risk="low"),
            "comments/v2.txt": verdict(round_="2/1", risk="low"),
        },
    )
    text = render(build_report(tmp_path).report)
    assert "maxharris1/Cleffy 4 round-extra-authorized low 2 1\n" in text
    assert "round-over-cap" not in text


def test_round_two_without_capped_is_over_cap(tmp_path: Path) -> None:
    write_bundle(
        tmp_path,
        log(
            row("18:00:00", "enter state=review"),
            row("18:00:10", "comment path=comments/v1.txt"),
            row("18:00:10", "enter state=fixing"),
            row("18:01:00", "enter state=review"),
            row("18:01:10", "comment path=comments/v2.txt"),
            row("18:01:10", "end"),
        ),
        {
            "comments/v1.txt": verdict(round_="1/1", risk="low"),
            "comments/v2.txt": verdict(round_="2/2", risk="low"),
        },
    )
    text = render(build_report(tmp_path).report)
    assert "maxharris1/Cleffy 4 round-over-cap low 2 1\n" in text
    assert "round-extra-authorized" not in text


def test_high_risk_round_over_cap(tmp_path: Path) -> None:
    write_bundle(
        tmp_path,
        log(
            row("18:00:00", "enter state=review"),
            row("18:00:10", "comment path=comments/v3.txt"),
            row("18:00:20", "comment path=comments/v4.txt"),
            row("18:00:20", "end"),
        ),
        {
            "comments/v3.txt": verdict(round_="3/3", risk="high"),
            "comments/v4.txt": verdict(round_="4/3", risk="high"),
        },
    )
    text = render(build_report(tmp_path).report)
    assert "chart mismatches\nmaxharris1/Cleffy 4 round-over-cap high 4 3\n" in text
    assert text.count("round-over-cap") == 1


def test_rounds_fall_back_to_verdict_count(tmp_path: Path) -> None:
    write_bundle(
        tmp_path,
        log(
            row("18:00:00", "enter state=review"),
            row("18:00:10", "comment path=comments/v1.txt"),
            row("18:00:20", "comment path=comments/v2.txt"),
            row("18:00:20", "end"),
        ),
        {
            "comments/v1.txt": verdict(risk="high"),
            "comments/v2.txt": verdict(risk="high"),
        },
    )
    text = render(build_report(tmp_path).report)
    assert "review rounds per PR\nmaxharris1/Cleffy 4 2\n" in text


@pytest.mark.parametrize(
    "wake",
    ["wake role=claude-bot reason=fix wasted=yes", "wake role=grok-bot reason=nobody"],
    ids=["wasted-flag-alone", "reason-nobody-alone"],
)
def test_one_signal_alone_makes_a_wasted_wake(tmp_path: Path, wake: str) -> None:
    write_bundle(tmp_path, log(row("18:00:00", wake), row("18:00:00", "end")))
    text = render(build_report(tmp_path).report)
    assert "wasted agent wakes\ncount: 1\n" in text


def test_ticket_without_pr_is_reported_under_ticket_id(tmp_path: Path) -> None:
    write_bundle(
        tmp_path,
        log(
            row("18:00:00", "enter state=intake", ticket="ENG-42"),
            row("18:01:00", "enter state=questions", ticket="ENG-42"),
            row("18:02:00", "enter state=planning", ticket="ENG-42"),
            row("18:03:00", "wake role=grok-bot reason=nobody", ticket="ENG-42"),
            row("18:03:00", "end", ticket="ENG-42"),
            row("18:04:00", "enter state=review pr=7", ticket="9"),
            row("18:05:00", "end pr=7", ticket="9"),
        ),
    )
    text = render(build_report(tmp_path).report)
    assert "runs: 2\n" in text
    assert "2026-11-03T18:03:00Z maxharris1/Cleffy ENG-42 grok-bot nobody\n" in text
    assert "review rounds per PR\nmaxharris1/Cleffy 9 0\nmaxharris1/Cleffy ENG-42 0\n" in text


def test_row_without_ticket_is_refused(tmp_path: Path) -> None:
    write_bundle(
        tmp_path,
        "2026-11-03T15:00:00Z repo=maxharris1/Cleffy pr=1 enter state=intake\n",
    )
    with pytest.raises(EvalError, match="missing repo or ticket"):
        build_report(tmp_path)


def test_window_clips_state_straddling_both_ends(tmp_path: Path) -> None:
    write_bundle(
        tmp_path,
        log(row("10:00:00", "enter state=building"), row("12:00:00", "end")),
    )
    text = render(
        build_report(
            tmp_path, since="2026-11-03T11:00:00Z", until="2026-11-03T11:00:30Z"
        ).report
    )
    assert "runs: 1\n" in text
    assert "time in each state\nbuilding 30\n" in text


def test_window_clips_state_straddling_since(tmp_path: Path) -> None:
    write_bundle(
        tmp_path,
        log(
            row("10:00:00", "enter state=building"),
            row("11:00:20", "enter state=review"),
            row("11:00:30", "end"),
        ),
    )
    text = render(build_report(tmp_path, since="2026-11-03T11:00:00Z").report)
    assert "time in each state\nbuilding 20\nreview 10\n" in text


def test_repeat_handled_across_since_is_wasted(tmp_path: Path) -> None:
    write_bundle(
        tmp_path,
        log(
            row("10:00:00", "wake role=claude-bot reason=fix handled=c1"),
            row("11:00:10", "wake role=claude-bot reason=fix handled=c1"),
            row("11:00:20", "end"),
        ),
    )
    text = render(build_report(tmp_path, since="2026-11-03T11:00:00Z").report)
    assert (
        "wasted agent wakes\ncount: 1\n"
        "2026-11-03T11:00:10Z maxharris1/Cleffy 4 claude-bot fix\n"
    ) in text


def test_transition_across_since_is_checked(tmp_path: Path) -> None:
    write_bundle(
        tmp_path,
        log(
            row("10:00:00", "enter state=intake"),
            row("11:00:10", "enter state=ship"),
            row("11:00:20", "end"),
        ),
    )
    text = render(build_report(tmp_path, since="2026-11-03T11:00:00Z").report)
    assert "maxharris1/Cleffy 4 illegal-transition intake ship\n" in text


@pytest.mark.parametrize("name", ["orch.yml", "dispatcher.log", "model-findings.txt"])
def test_top_level_input_symlinked_outside_is_refused(tmp_path: Path, name: str) -> None:
    root = tmp_path / "week"
    shutil.copytree(WEEK, root)
    outside = tmp_path / "outside.txt"
    outside.write_text(
        (root / name).read_text(encoding="utf-8") + "credential-marker\n",
        encoding="utf-8",
    )
    (root / name).unlink()
    (root / name).symlink_to(outside)
    with pytest.raises(EvalError, match="leaves the input directory") as raised:
        build_report(root)
    assert "credential-marker" not in str(raised.value)


@pytest.mark.parametrize(
    ("where", "args"),
    [
        ("log", []),
        ("since", ["--since", "2026-02-30T00:00:00Z"]),
        ("until", ["--until", "2026-02-30T00:00:00Z"]),
        ("findings", []),
    ],
)
def test_impossible_date_is_bad_timestamp(
    tmp_path: Path, where: str, args: list[str]
) -> None:
    root = tmp_path / "week"
    shutil.copytree(WEEK, root)
    if where == "log":
        path = root / "dispatcher.log"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "2026-11-03T15:00:00Z", "2026-02-30T15:00:00Z", 1
            ),
            encoding="utf-8",
        )
    if where == "findings":
        path = root / "model-findings.txt"
        path.write_text(
            path.read_text(encoding="utf-8").replace("2026-11-03", "2026-02-30"),
            encoding="utf-8",
        )
    result = run(["eval-report", "--input", str(root), *args])
    assert result.returncode == 1
    assert "bad timestamp '2026-02-30T" in result.stderr
    assert result.stdout == ""


def test_missing_model_findings_is_none_supplied(tmp_path: Path) -> None:
    root = tmp_path / "week"
    shutil.copytree(WEEK, root)
    (root / "model-findings.txt").write_text("", encoding="utf-8")
    text = render(build_report(root).report)
    assert "rule breaks the model finds\nnone\n" in text
    (root / "model-findings.txt").unlink()
    text = render(build_report(root).report)
    assert "rule breaks the model finds\nnone supplied\n" in text


def test_stuck_comment_without_the_state_is_still_a_stuck_job(tmp_path: Path) -> None:
    write_bundle(
        tmp_path,
        "\n".join(
            (
                "2026-11-03T16:00:00Z repo=maxharris1/Cleffy ticket=8 comment path=comments/stuck.txt",
                "2026-11-03T16:00:00Z repo=maxharris1/Cleffy ticket=8 end",
                "",
            )
        ),
        {
            "comments/stuck.txt": (
                "<!-- orch:v1 kind=stuck from=grok-bot role=orchestrator state=stuck -->\n"
                "Stuck.\n"
            )
        },
    )
    text = render(build_report(tmp_path).report)
    assert "stuck jobs\nmaxharris1/Cleffy 8\n" in text
    assert "maxharris1/Cleffy 8 stuck-comment-without-state\n" in text


def test_invalid_comment_is_refused(tmp_path: Path) -> None:
    write_bundle(
        tmp_path,
        "2026-11-03T15:00:00Z repo=maxharris1/Cleffy ticket=1 comment path=comments/bad.txt\n",
        {"comments/bad.txt": "not a handoff\n"},
    )
    with pytest.raises(EvalError, match="not an orch:v1 header"):
        build_report(tmp_path)


def test_comment_path_cannot_leave_the_input_directory(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("credential-marker", encoding="utf-8")
    root = tmp_path / "week"
    write_bundle(
        root,
        "2026-11-03T15:00:00Z repo=maxharris1/Cleffy ticket=1 comment path=comments/../../secret.txt\n",
    )
    with pytest.raises(EvalError, match="leaves the input directory") as raised:
        build_report(root)
    assert "credential-marker" not in str(raised.value)


def test_repo_missing_from_orch_yml_is_refused(tmp_path: Path) -> None:
    write_bundle(
        tmp_path,
        "2026-11-03T15:00:00Z repo=maxharris1/other ticket=1 enter state=intake\n",
    )
    with pytest.raises(EvalError, match="is not a project in orch.yml"):
        build_report(tmp_path)


def test_missing_factory_md_is_refused(tmp_path: Path) -> None:
    write_bundle(
        tmp_path,
        "2026-11-03T15:00:00Z repo=maxharris1/Cleffy ticket=1 enter state=intake\n",
        orch="poll_seconds: 60\nprojects:\n  - repo: maxharris1/Cleffy\n  - repo: maxharris1/missing\n",
    )
    log = tmp_path / "dispatcher.log"
    log.write_text(
        "2026-11-03T15:00:00Z repo=maxharris1/missing ticket=1 enter state=intake\n",
        encoding="utf-8",
    )
    with pytest.raises(EvalError, match="file not found"):
        build_report(tmp_path)


def test_since_after_until_is_refused(tmp_path: Path) -> None:
    write_bundle(
        tmp_path,
        "2026-11-03T15:00:00Z repo=maxharris1/Cleffy ticket=1 enter state=intake\n",
    )
    with pytest.raises(EvalError, match="since is after until"):
        build_report(
            tmp_path,
            since="2026-11-03T16:00:00Z",
            until="2026-11-03T15:00:00Z",
        )


def test_invalid_orch_yml_is_refused(tmp_path: Path) -> None:
    write_bundle(
        tmp_path,
        "2026-11-03T15:00:00Z repo=maxharris1/Cleffy ticket=1 enter state=intake\n",
        orch="poll_seconds: 60\nprojects: []\nextra: true\n",
    )
    with pytest.raises(EvalError, match="unknown field"):
        build_report(tmp_path)


def test_cli_refuses_a_bad_comment(tmp_path: Path) -> None:
    write_bundle(
        tmp_path,
        "2026-11-03T15:00:00Z repo=maxharris1/Cleffy ticket=1 comment path=comments/bad.txt\n",
        {"comments/bad.txt": "nope\n"},
    )
    result = run(["eval-report", "--input", str(tmp_path)])
    assert result.returncode == 1
    assert "not an orch:v1 header" in result.stderr
    assert result.stdout == ""


def test_unknown_model_warns_and_still_reports(tmp_path: Path) -> None:
    factory = MIN_FACTORY.replace(
        "reviewer:\n  harness: codex\n",
        "reviewer:\n  harness: codex\n  model: not-a-real-model\n",
    )
    write_bundle(
        tmp_path,
        "2026-11-03T15:00:00Z repo=maxharris1/Cleffy ticket=1 enter state=intake\n"
        "2026-11-03T15:00:00Z repo=maxharris1/Cleffy ticket=1 end\n",
        factory=factory,
    )
    result = run(["eval-report", "--input", str(tmp_path)])
    assert result.returncode == 0, result.stderr
    assert "warning: unknown model name 'not-a-real-model'" in result.stderr
    assert "runs: 1\n" in result.stdout


def test_report_does_not_write_or_read_a_dotenv(tmp_path: Path) -> None:
    copied = tmp_path / "week"
    shutil.copytree(WEEK, copied)
    secret = copied / ".env"
    secret.write_text("TOKEN=should-not-appear\n", encoding="utf-8")
    before = {
        path: path.read_bytes()
        for path in copied.rglob("*")
        if path.is_file() and path.name != ".env"
    }
    text = render(build_report(copied).report)
    after = {
        path: path.read_bytes()
        for path in copied.rglob("*")
        if path.is_file() and path.name != ".env"
    }
    assert before == after
    assert secret.read_text(encoding="utf-8") == "TOKEN=should-not-appear\n"
    assert "should-not-appear" not in text
    assert "TOKEN" not in text


@pytest.mark.parametrize("command", ["correct", "reflect", "maintain", "blinded-eval"])
def test_retired_self_improvement_commands_are_rejected(command: str) -> None:
    result = run([command])
    assert result.returncode == 2
    assert f"unknown command: {command}" in result.stderr
    text = render(build_report(WEEK).report)
    assert "weekly correct" not in text
    assert "blinded" not in text
    assert "daily maintain" not in text


def test_eval_report_imports_and_does_not_touch_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (ROOT / "lib" / "eval_report.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules: set[str] = set()
    banned_defs = {"correct", "reflect", "maintain", "blinded_eval"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                modules.add(node.module)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.name not in banned_defs
    assert modules <= {
        "__future__",
        "json",
        "re",
        "sqlite3",
        "collections",
        "dataclasses",
        "datetime",
        "pathlib",
        "typing",
        "lib.factory_md",
        "lib.handoff",
        "lib.load",
        "lib.orch_yml",
        "lib.schema",
    }

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("process or network call")

    monkeypatch.setattr(os, "system", boom)
    monkeypatch.setattr(os, "popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "call", boom)
    monkeypatch.setattr(subprocess, "check_call", boom)
    monkeypatch.setattr(subprocess, "check_output", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    assert render(build_report(WEEK).report) == EXPECTED
