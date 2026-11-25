"""The brief's Skills section names the router by absolute path (#180, T09)."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from lib.brief import render_brief
from lib.brief.skills import ACTION_FILES, PLAYBOOKS, REVIEWER_FILES, builder_files
from lib.ticket_type import ticket_type_of
from lib.pstack_models import brief_lines
from lib.pstack_pin import load_pin
from tests.test_brief import job

ROOT = Path(__file__).resolve().parents[1]
FACTORY_ROOT = "/home/claude-bot/factory"
HEAD_KINDS = ("kind=plan", "kind=ready", "kind=verdict")

ROUTER_JOBS = [
    ("plan", "claude", {}),
    ("build", "claude", {}),
    ("fix", "claude", {"instruction": "https://github.com/owner/repo/pull/42#issuecomment-7"}),
    ("build", "codex", {}),
    ("review", "codex", {"round": "2/3"}),
    ("review", "claude", {"round": "2/3"}),
]
ALL_JOBS = [*ROUTER_JOBS, ("fix", "claude", {"kind": "merge-only"})]


def _brief(action: str, recipient: str, extra: dict, **kwargs: object) -> str:
    kwargs.setdefault("factory_root", FACTORY_ROOT)
    return render_brief(job(action, recipient, **extra), bot=f"{recipient}-bot", context=None, **kwargs)


def _named(text: str) -> list[str]:
    return re.findall(r"^- `(/[^`]+)`$", text, flags=re.MULTILINE)


@pytest.mark.parametrize(("action", "recipient", "extra"), ROUTER_JOBS)
def test_every_named_path_is_absolute_inside_factory_root_and_pinned(
    action: str, recipient: str, extra: dict
) -> None:
    pin = load_pin(ROOT)
    pinned = (
        {f"skills/pstack/{rel}" for rel, _ in pin.files}
        | {f"skills/factory/{rel}" for rel, _ in pin.factory}
        | {f"skills/cursor-team-kit/{rel}" for rel, _ in pin.kit}
    )
    for remote in (None, "pc"):
        named = _named(_brief(action, recipient, extra, remote_device=remote))
        assert named, "no path named"
        for path in named:
            assert path.startswith(FACTORY_ROOT + "/")
            assert ".." not in path.split("/")
            assert path.removeprefix(FACTORY_ROOT + "/") in pinned
            assert (ROOT / path.removeprefix(FACTORY_ROOT + "/")).is_file()


def test_the_router_comes_first_and_the_adapter_follows_the_harness() -> None:
    claude = _named(_brief("build", "claude", {}))
    codex = _named(_brief("build", "codex", {}))
    review = _named(_brief("review", "codex", {"round": "2/3"}))
    assert claude[0] == f"{FACTORY_ROOT}/skills/factory/factory-builder/SKILL.md"
    assert review[0] == f"{FACTORY_ROOT}/skills/factory/factory-reviewer/SKILL.md"
    assert f"{FACTORY_ROOT}/skills/pstack/docs/harness/claude-code.md" in claude
    assert f"{FACTORY_ROOT}/skills/factory/harness/codex.md" in codex
    assert f"{FACTORY_ROOT}/skills/factory/harness/codex.md" not in claude
    assert len(claude) == len(builder_files("build", None)) + 1
    assert len(review) == len(REVIEWER_FILES) + 1


def _skills_prose(text: str) -> str:
    skills = text.split("## Skills\n", 1)[1].split("\n## ", 1)[0]
    return "\n".join(line for line in skills.splitlines() if not line.startswith("- `/"))


ADAPTER_PROSE = {
    "claude": "`skills/pstack/docs/harness/claude-code.md`",
    "codex": "`skills/factory/harness/codex.md`",
}


@pytest.mark.parametrize(
    ("action", "recipient", "extra"),
    [job_ for job_ in ROUTER_JOBS if job_[1] == "codex" or job_[0] in ("plan", "build")],
)
def test_the_read_with_line_names_the_adapter_by_its_full_skills_path(
    action: str, recipient: str, extra: dict
) -> None:
    text = _brief(action, recipient, extra)
    read_with = next(line for line in _skills_prose(text).splitlines() if "Read each file with" in line)
    assert ADAPTER_PROSE[recipient] in read_with
    assert not re.search(r"`(docs/)?harness/(codex|claude-code)\.md`", text)


@pytest.mark.parametrize(("action", "recipient", "extra"), ROUTER_JOBS)
def test_every_harness_path_in_the_skills_prose_exists_under_the_factory_checkout(
    action: str, recipient: str, extra: dict
) -> None:
    text = _brief(action, recipient, extra)
    paths = [path for path in re.findall(r"`([^`\s]+)`", _skills_prose(text)) if "harness/" in path]
    assert paths
    named = _named(text)
    for path in paths:
        assert path.startswith(("skills/factory/", "skills/pstack/"))
        assert (ROOT / path).is_file()
        assert f"{FACTORY_ROOT}/{path}" in named


@pytest.mark.parametrize(("action", "recipient", "extra"), ROUTER_JOBS)
def test_the_model_lines_and_the_helper_permission_are_there_with_no_cap(
    action: str, recipient: str, extra: dict
) -> None:
    text = _brief(action, recipient, extra)
    assert brief_lines(recipient) in text
    assert "You may start helper agents as pstack recommends." in text
    for gone in ("They win over any other model config", "They do research only; you write the verdict.",
                 "start a general helper", "They never post to GitHub or push."):
        assert gone not in text, gone
    assert not re.search(r"(at most|up to|no more than) \w+ (helper|agent)", text, re.IGNORECASE)
    assert not re.search(r"\b\d+ (helper|agent)s?\b", text, re.IGNORECASE)


def test_codex_reviewer_helpers_run_on_sol() -> None:
    text = _brief("review", "codex", {"round": "2/3"})
    assert "interrogate reviewers: gpt-6.1-sol high" in text


@pytest.mark.parametrize(("action", "recipient", "extra"), ALL_JOBS)
def test_no_brief_asks_for_a_skills_read_line(action: str, recipient: str, extra: dict) -> None:
    for remote in (None, "pc"):
        assert "Skills read" not in _brief(action, recipient, extra, remote_device=remote)


@pytest.mark.parametrize(("action", "recipient", "extra"), ALL_JOBS)
def test_the_old_harness_and_no_rebase_rules_are_gone(action: str, recipient: str, extra: dict) -> None:
    for remote in (None, "pc"):
        text = _brief(action, recipient, extra, remote_device=remote)
        assert "Never start another harness" not in text
        assert "No force-push and no rebase of a pushed branch" not in text
        assert "Never start another factory job." in text
        assert text.lower().count("message no other bot") == 1
        assert "Run as poteto-mode" not in text


def test_merge_only_has_no_router_section() -> None:
    text = _brief("fix", "claude", {"kind": "merge-only"})
    assert "## Skills" not in text
    assert "Skills read:" not in text
    assert "kind=ready" in text


def _assert_questions_only(text: str, named: str) -> None:
    assert "kind=questions" in text
    assert named in text
    for header in HEAD_KINDS:
        assert header not in text
    assert "## Skills" not in text
    for line in ("Build ticket ", "Plan ticket ", "Review PR #", "Fix the red CI", "Fix the `[blocking]`"):
        assert line not in text


def test_a_path_missing_from_the_pin_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "skills").mkdir()
    lock = (ROOT / "skills" / "skills.lock").read_text(encoding="utf-8")
    dropped = "  agents/comment-sicko.md:"
    assert dropped in lock
    kept = "\n".join(line for line in lock.splitlines() if not line.startswith(dropped))
    (tmp_path / "skills" / "skills.lock").write_text(kept + "\n", encoding="utf-8")
    shutil.copytree(ROOT / "config", tmp_path / "config")
    setup = Path("skills/pstack/skills/setup-pstack/SKILL.md")
    (tmp_path / setup).parent.mkdir(parents=True)
    shutil.copy(ROOT / setup, tmp_path / setup)
    for action, recipient, extra in [("build", "claude", {}), ("fix", "claude", ROUTER_JOBS[2][2])]:
        text = _brief(action, recipient, extra, pin_root=tmp_path)
        _assert_questions_only(text, f"{FACTORY_ROOT}/skills/pstack/agents/comment-sicko.md")
    review = _brief("review", "codex", {"round": "2/3"}, pin_root=tmp_path)
    assert "kind=verdict" in review


def test_an_unreadable_pin_fails_closed(tmp_path: Path) -> None:
    text = _brief("build", "claude", {}, pin_root=tmp_path)
    _assert_questions_only(text, "skills.lock")


@pytest.mark.parametrize("root", [None, "REPLACE_WITH_FACTORY_ROOT", "home/claude-bot/factory",
                                  "/home/{bot}/factory", "/srv/../etc", "/srv/REPLACE_WITH_X"])
def test_a_bad_factory_root_fails_closed(root: str | None) -> None:
    for action, recipient, extra in ROUTER_JOBS:
        text = _brief(action, recipient, extra, factory_root=root)
        _assert_questions_only(text, "factory_root")


def test_a_harness_with_no_adapter_fails_closed() -> None:
    _assert_questions_only(_brief("build", "cursor", {}), "cursor")


@pytest.mark.parametrize(("action", "recipient", "extra"), ROUTER_JOBS)
def test_the_device_check_runs_before_any_read(action: str, recipient: str, extra: dict) -> None:
    text = _brief(action, recipient, extra)
    section = text.split("## Skills\n", 1)[1].split("\n## ", 1)[0]
    check = section.index("`test -f`")
    assert check < section.index(f"`{FACTORY_ROOT}/")
    assert "post a `questions` handoff naming it, then stop" in section


@pytest.mark.parametrize("recipient", ["claude", "codex"])
def test_the_brief_lists_the_agent_files_and_leaves_the_fallback_to_the_adapter(recipient: str) -> None:
    text = _brief("build", recipient, {})
    for gone in ('`subagent_type: "general-purpose"`', '`agent_type: "default"`',
                 "tell it first to read and follow the agent file"):
        assert gone not in text, gone
    assert f"`{FACTORY_ROOT}/skills/pstack/agents/comment-sicko.md`" in text
    assert f"`{FACTORY_ROOT}/skills/pstack/agents/poteto-agent.md`" in text


def _started_brief(tmp_path: Path, device: str, box_root: str | None = None) -> str:
    from tests.test_waiter import FakeSpawn, enqueue, slots
    from tests.test_waiter_remote import make_waiter, remote_machines

    machines = remote_machines(tmp_path, box_enabled=device == "box", pc=device == "pc")
    if box_root is not None:
        text = machines.read_text(encoding="utf-8")
        machines.write_text(text.replace("    local: true\n", f"    local: true\n    factory_root: {box_root}\n"),
                            encoding="utf-8")
    spawn = FakeSpawn()
    waiter, queue, _ = make_waiter(tmp_path, machines, spawn)
    job_id = enqueue(queue, "a")
    assert waiter.step() == job_id
    assert slots(tmp_path / "orch.db") == {job_id: device}
    return spawn.stdin[0].decode("utf-8")


def test_a_remote_job_names_the_devices_factory_root_never_its_workdir(tmp_path: Path) -> None:
    text = _started_brief(tmp_path, "pc")
    named = _named(text)
    assert named and all(path.startswith("/srv/factory/skills/") for path in named)
    assert not any(str(tmp_path) in path for path in named)


def test_a_box_job_names_the_box_factory_root(tmp_path: Path) -> None:
    named = _named(_started_brief(tmp_path, "box", box_root="/opt/orch/factory"))
    assert named and all(path.startswith("/opt/orch/factory/skills/") for path in named)


def test_a_box_with_no_factory_root_names_its_own_checkout(tmp_path: Path) -> None:
    named = _named(_started_brief(tmp_path, "box"))
    assert named and all(path.startswith(f"{ROOT}/skills/") for path in named)


SOURCES = ("skills/skills.lock", "config/pstack-models.yml", "skills/pstack/skills/setup-pstack/SKILL.md")
CORRUPTIONS = [b"broken: [\n", b"a: 1\na: 2\n", b"key: *missing\n", b"\xff\xfe\n", b"- a list\n", b"",
               b"? [a, b]\n: value\n", b"a: !!int zz\n", b"[" * 20000,
               b"a: !!timestamp zz\n", b"a: !!bool zz\n", b"a: !!int +\n"]


def _pin_copy(root: Path, bad: str, content: bytes) -> Path:
    for source in SOURCES:
        target = root / source
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / source, target)
    (root / bad).write_bytes(content)
    return root


@pytest.mark.parametrize("bad", SOURCES)
@pytest.mark.parametrize("content", CORRUPTIONS)
def test_any_malformed_pin_input_fails_closed(tmp_path: Path, bad: str, content: bytes) -> None:
    pin = _pin_copy(tmp_path, bad, content)
    named = "skills.lock" if bad == SOURCES[0] else "helper model map"
    for action, recipient, extra in ROUTER_JOBS:
        _assert_questions_only(_brief(action, recipient, extra, pin_root=pin), named)


@pytest.mark.parametrize("bad", SOURCES)
@pytest.mark.parametrize("content", CORRUPTIONS)
def test_a_malformed_pin_input_still_starts_the_job_with_a_questions_brief(
    tmp_path: Path, bad: str, content: bytes
) -> None:
    from functools import partial
    from unittest.mock import patch

    pin = _pin_copy(tmp_path / "pin", bad, content)
    run = tmp_path / "run"
    run.mkdir()
    with patch("lib.waiter.render_brief", partial(render_brief, pin_root=pin)):
        text = _started_brief(run, "pc")
    _assert_questions_only(text, "skills.lock" if bad == SOURCES[0] else "helper model map")


ROOT_CANDIDATES = [
    "/srv/factory checkout",
    "/srv/factory+checkout",
    "/srv/fäctory",
    "/srv/o'brien/factory",
    "/srv/a$b/factory",
    "/srv/a,b@c~d/factory",
    "/srv/a`b/factory",
    "/srv/a\tb/factory",
    "/srv/factory/",
    "/srv//factory",
    "/srv/./factory",
    "/mnt/c/factory",
    "/",
    "srv/factory",
    "/srv/{bot}/factory",
    "/srv/{user}/factory",
]


@pytest.mark.parametrize("root", ROOT_CANDIDATES)
def test_the_brief_accepts_exactly_the_factory_roots_the_machines_schema_accepts(root: str) -> None:
    from lib.brief.skills import resolve
    from lib.machines_yml import workdir_problem

    schema_ok = workdir_problem(root) is None and "{bot}" not in root
    router = resolve("builder", "claude-bot", root)
    assert isinstance(router, str) != schema_ok, router
    if schema_ok:
        named = _named(_brief("build", "claude", {}, factory_root=root))
        assert named and all(path.startswith(root + "/skills/") for path in named)


PSTACK = f"{FACTORY_ROOT}/skills/pstack/skills"
PLAYBOOK_PATH = re.compile(r"/poteto-mode/playbooks/(feature|bug-fix|refactoring|perf-issue)\.md$")
BUILDER_JOBS = [job_ for job_ in ROUTER_JOBS if job_[0] != "review"]
HOW = f"{PSTACK}/how/SKILL.md"
ARCHITECT = f"{PSTACK}/architect/SKILL.md"
TDD = f"{PSTACK}/tdd/SKILL.md"


def _playbooks(text: str) -> list[str]:
    return [path for path in _named(text) if PLAYBOOK_PATH.search(path)]


@pytest.mark.parametrize("ticket_type", ["feature", "bug-fix", "refactoring", "perf-issue"])
@pytest.mark.parametrize(("action", "recipient", "extra"), BUILDER_JOBS)
def test_the_ticket_type_picks_exactly_one_pinned_playbook(
    ticket_type: str, action: str, recipient: str, extra: dict
) -> None:
    text = _brief(action, recipient, {**extra, "ticket_type": ticket_type})
    assert _playbooks(text) == [f"{PSTACK}/poteto-mode/playbooks/{ticket_type}.md"]


@pytest.mark.parametrize("ticket_type", [None, "bugfix", "../feature", 3])
@pytest.mark.parametrize(("action", "recipient", "extra"), BUILDER_JOBS)
def test_no_type_or_an_unknown_one_reads_the_feature_playbook(
    ticket_type: object, action: str, recipient: str, extra: dict
) -> None:
    typed = {} if ticket_type is None else {"ticket_type": ticket_type}
    text = _brief(action, recipient, {**extra, **typed})
    assert _playbooks(text) == [f"{PSTACK}/poteto-mode/playbooks/feature.md"]


def test_a_review_brief_names_no_playbook_whatever_the_type() -> None:
    assert _playbooks(_brief("review", "codex", {"round": "2/3", "ticket_type": "bug-fix"})) == []


@pytest.mark.parametrize(("action", "recipient", "extra"), BUILDER_JOBS)
def test_build_and_fix_briefs_name_tdd_and_plans_name_how_and_architect(
    action: str, recipient: str, extra: dict
) -> None:
    text = _brief(action, recipient, extra)
    named = _named(text)
    assert "Test first." not in text
    if action == "plan":
        assert HOW in named and ARCHITECT in named
        assert TDD not in named
    else:
        assert TDD in named
        assert HOW not in named and ARCHITECT not in named


def test_the_build_task_points_at_the_tdd_skill() -> None:
    assert "Write tests first, as the `tdd` skill says." in _brief("build", "claude", {})


def test_every_playbook_and_action_file_is_pinned() -> None:
    pinned = load_pin(ROOT).hashes()
    for item in (*PLAYBOOKS.values(), *(entry for row in ACTION_FILES.values() for entry in row)):
        assert item.section == "files"
        assert item.rel in pinned, item.rel
        assert (ROOT / "skills" / "pstack" / item.rel).is_file()


@pytest.mark.parametrize(("dropped", "path"), [("skills/how/SKILL.md", HOW), ("skills/architect/SKILL.md", ARCHITECT)])
def test_a_plan_whose_how_or_architect_is_unpinned_fails_closed(tmp_path: Path, dropped: str, path: str) -> None:
    (tmp_path / "skills").mkdir()
    lock = (ROOT / "skills" / "skills.lock").read_text(encoding="utf-8")
    assert f"  {dropped}:" in lock
    kept = "\n".join(line for line in lock.splitlines() if not line.startswith(f"  {dropped}:"))
    (tmp_path / "skills" / "skills.lock").write_text(kept + "\n", encoding="utf-8")
    shutil.copytree(ROOT / "config", tmp_path / "config")
    setup = Path("skills/pstack/skills/setup-pstack/SKILL.md")
    (tmp_path / setup).parent.mkdir(parents=True)
    shutil.copy(ROOT / setup, tmp_path / setup)
    _assert_questions_only(_brief("plan", "claude", {}, pin_root=tmp_path), path)
    assert "kind=ready" in _brief("build", "claude", {}, pin_root=tmp_path)


@pytest.mark.parametrize(
    ("brief_text", "expected"),
    [
        ("**Brief for #7: X** (factory feeder)\n\n- Goal: X\n- Type: bug-fix\n- Start: building\n", "bug-fix"),
        ("**Brief for #7: X** (factory feeder)\n\n- Goal: X\n- Start: building\n", None),
        ("**Brief**\n\n- Type: bugfix\n", None),
        ("**Brief**\n\n- Goal: X\n\n### Ticket\n```\n- Type: perf-issue\n```\n", None),
        ("**Brief**\n\n### Acceptance criteria\n- Type: perf-issue\n", None),
    ],
)
def test_the_type_comes_only_from_the_briefs_header_lines(brief_text: str, expected: str | None) -> None:
    assert ticket_type_of(brief_text) == expected
