"""Tests for E1-2: visual QA auto-hold gate (`visual-qa-review`)."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatcher import Comment
from lib.factory_md import HoldConfig, parse_factory_md, trust_front_matter_changed
from lib.hold import VISUAL_QA_REASON, LabelEvent, decide
from lib.qa_evidence import (
    VISUAL_SUFFIXES,
    has_visual_evidence,
    record_evidence_dir,
    recycle,
    run_dir,
    visual_evidence_files,
)
from lib.update_check import HeadDiff, merge_update
from tests.test_cold_spill import spill_while_abandoned
from tests.qa_support import (
    HEAD_A,
    HEAD_B,
    REPO,
    TICKET,
    Harness,
    qa_config,
    qa_report,
    ready,
    scenarios,
    verdict,
)

pytestmark = pytest.mark.usefixtures("every_wake_reaches_grok")

TABLE = "| qa-1 | Verified | ok |\n"
EVIDENCE_URL = "https://qa.example/runs/visual-1"
EVIDENCED = TABLE + f"Evidence: {EVIDENCE_URL}\n"


def _factory_md_text(extra_qa: str = "") -> str:
    return (
        "---\n"
        "base_branch: main\n"
        "tickets: github\n"
        "builder:\n"
        "  harness: claude\n"
        "reviewer:\n"
        "  harness: codex\n"
        "review:\n"
        "  rounds:\n"
        "    low: 1\n"
        "    high: 3\n"
        "  verify: none\n"
        "ship: merge\n"
        "skills: []\n"
        "qa:\n"
        "  enabled: true\n"
        "  runners:\n"
        "    - local-docker\n"
        "  local_docker:\n"
        "    environment: full-stack\n"
        "  profile: full-stack\n"
        "  lane:\n"
        "    boot: make boot\n"
        "    base_url: http://localhost:3000\n"
        f"{extra_qa}"
        "---\n"
    )


def _record_primary_evidence(run: Harness, head: str, folder: Path) -> str:
    [(run_id,)] = run.rows(
        "SELECT run_id FROM qa_runs WHERE head = ? AND arm = 'primary' ORDER BY rowid DESC LIMIT 1",
        head,
    )
    with run.queue.connect() as connection:
        record_evidence_dir(connection, run_id, folder)
        connection.commit()
    return run_id


def _make_evidence(base: Path, *rel_paths: str) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    for rel in rel_paths:
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"artifact-bytes")
    return base


def _wakes(run: Harness) -> list[dict]:
    return [
        json.loads(detail)
        for (detail,) in run.rows(
            "SELECT detail FROM jobs WHERE action = 'awaiting-ok' ORDER BY rowid"
        )
    ]


def _setup_approved(
    tmp_path: Path,
    *,
    ship: str = "merge",
    gate: str = "block",
    visual_hold: bool = True,
    hold: HoldConfig | None = None,
    qa_diff=None,
    patch_ids: dict[str, str] | None = None,
) -> Harness:
    qa = replace(qa_config(), gate=gate, visual_hold=visual_hold)
    run = Harness(
        tmp_path,
        qa,
        ship=ship,
        qa_diff=qa_diff,
    )
    if patch_ids is not None:
        run.actions.patch_ids.update(patch_ids)
    base = run.thread.base
    run.thread.base = replace(
        base,
        factory=replace(base.factory, hold=hold),
        changed_files=("src/page.tsx",),
        labels=(),
        label_events=(),
        ticket_url="https://github.com/maxharris1/factory/pull/900",
    )
    run.post("r1", ready(HEAD_A, body=scenarios("qa-1")), head=HEAD_A)
    run.poll()
    run.finish("codex", "v1")
    run.post("v1", verdict(HEAD_A, "APPROVE"))
    return run


# ---------------------------------------------------------------------------
# factory.md schema tests
# ---------------------------------------------------------------------------


def test_qa_visual_hold_defaults_true_and_accepts_false() -> None:
    cfg_default, findings_default = parse_factory_md(_factory_md_text())
    assert cfg_default is not None
    assert not findings_default
    assert cfg_default.qa is not None
    assert cfg_default.qa.visual_hold is True
    assert cfg_default.hold is None
    assert cfg_default.configured_hold_applies is False
    assert cfg_default.visual_hold_applies is True
    assert cfg_default.hold_applies is True

    cfg_off, findings_off = parse_factory_md(_factory_md_text("  visual_hold: false\n"))
    assert cfg_off is not None
    assert not findings_off
    assert cfg_off.qa is not None
    assert cfg_off.qa.visual_hold is False
    assert cfg_off.visual_hold_applies is False
    assert cfg_off.hold_applies is False


def test_qa_visual_hold_rejects_non_boolean() -> None:
    cfg, findings = parse_factory_md(_factory_md_text("  visual_hold: yes-please\n"))
    assert cfg is None
    assert any(
        f.severity == "error" and "qa.visual_hold must be true or false" in f.message
        for f in findings
    )


def test_qa_visual_hold_change_trips_factory_md_trust_guard() -> None:
    base = _factory_md_text()
    head = _factory_md_text("  visual_hold: false\n")
    assert trust_front_matter_changed(base, head) is True
    assert trust_front_matter_changed(base, base) is False


# ---------------------------------------------------------------------------
# qa_evidence visual detection & symlink refusal tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("suffix", sorted(VISUAL_SUFFIXES))
def test_visual_evidence_detects_every_visual_extension(tmp_path: Path, suffix: str) -> None:
    folder = _make_evidence(tmp_path / "run", f"lane-1/qa-1/capture{suffix}")
    assert visual_evidence_files(folder) == (f"lane-1/qa-1/capture{suffix}",)
    assert has_visual_evidence(folder) is True


def test_visual_evidence_ignores_non_visual_files_and_non_lane_dirs(tmp_path: Path) -> None:
    folder = _make_evidence(
        tmp_path / "run",
        "lane-1/result.json",
        "lane-1/manifest.json",
        "lane-1/qa-1/network.json",
        "lane-1/qa-1/app.log",
        "lane-1/qa-1/trace.zip",
        "outside/shot.png",
    )
    assert visual_evidence_files(folder) == ()
    assert has_visual_evidence(folder) is False


def test_visual_evidence_refuses_symlinks(tmp_path: Path) -> None:
    real_img = tmp_path / "outside.png"
    real_img.write_bytes(b"png")

    # 1. Symlinked visual file inside lane-1
    run_file_link = _make_evidence(tmp_path / "run_file_link", "lane-1/result.json")
    os.symlink(real_img, run_file_link / "lane-1" / "shot.png")
    assert has_visual_evidence(run_file_link) is False

    # 2. Symlinked lane-1 directory
    outside_lane = _make_evidence(tmp_path / "outside_lane", "shot.png")
    run_lane_link = tmp_path / "run_lane_link"
    run_lane_link.mkdir()
    os.symlink(outside_lane, run_lane_link / "lane-1")
    assert has_visual_evidence(run_lane_link) is False

    # 3. Symlinked run folder
    real_run = _make_evidence(tmp_path / "real_run", "lane-1/shot.png")
    linked_run = tmp_path / "linked_run"
    os.symlink(real_run, linked_run)
    assert has_visual_evidence(linked_run) is False


# ---------------------------------------------------------------------------
# End-to-end visual QA hold gate tests
# ---------------------------------------------------------------------------


def test_visual_evidence_auto_holds_pr_without_explicit_hold_block(tmp_path: Path) -> None:
    run = _setup_approved(tmp_path, ship="merge", gate="block", hold=None)
    folder = _make_evidence(tmp_path / "ev1", "lane-1/qa-1/home.png", "lane-1/qa-1/app.log")
    _record_primary_evidence(run, HEAD_A, folder)
    run.post("q1", qa_report(HEAD_A, "PASS", body=EVIDENCED))
    run.poll(4)

    assert run.actions.merges == []
    assert run.rows("SELECT workflow_state FROM ticket_state") == [("awaiting-ok",)]
    assert run.rows("SELECT head, approver, reasons FROM ship_holds") == [
        (HEAD_A, "maxharris1", VISUAL_QA_REASON)
    ]
    [wake] = _wakes(run)
    assert wake["reasons"] == [VISUAL_QA_REASON]
    assert wake["qa"]["result"] == "PASS"
    assert wake["qa"]["evidence_link"] == EVIDENCE_URL


def test_non_visual_qa_evidence_ships_without_hold(tmp_path: Path) -> None:
    run = _setup_approved(tmp_path, ship="merge", gate="block", hold=None)
    folder = _make_evidence(
        tmp_path / "ev_backend",
        "lane-1/result.json",
        "lane-1/manifest.json",
        "lane-1/qa-1/network.json",
        "lane-1/qa-1/app.log",
        "lane-1/qa-1/trace.zip",
    )
    _record_primary_evidence(run, HEAD_A, folder)
    run.post("q1", qa_report(HEAD_A, "PASS", body=EVIDENCED))
    run.poll(4)

    assert run.rows("SELECT workflow_state FROM ticket_state") == [("merged",)]
    assert run.actions.merges == [(True, HEAD_A)]
    assert _wakes(run) == []


def test_visual_hold_false_opts_out_of_auto_hold(tmp_path: Path) -> None:
    run = _setup_approved(tmp_path, ship="merge", gate="block", visual_hold=False, hold=None)
    folder = _make_evidence(tmp_path / "ev_optout", "lane-1/qa-1/home.webp")
    _record_primary_evidence(run, HEAD_A, folder)
    run.post("q1", qa_report(HEAD_A, "PASS", body=EVIDENCED))
    run.poll(4)

    assert run.rows("SELECT workflow_state FROM ticket_state") == [("merged",)]
    assert run.actions.merges == [(True, HEAD_A)]
    assert _wakes(run) == []


def test_slash_ok_releases_visual_qa_hold_and_ships(tmp_path: Path) -> None:
    run = _setup_approved(tmp_path, ship="merge", gate="block", hold=None)
    folder = _make_evidence(tmp_path / "ev_video", "lane-1/qa-1/flow.webm")
    _record_primary_evidence(run, HEAD_A, folder)
    run.post("q1", qa_report(HEAD_A, "PASS", body=EVIDENCED))
    run.poll(4)
    assert run.rows("SELECT workflow_state FROM ticket_state") == [("awaiting-ok",)]

    # Untrusted author cannot clear the visual hold.
    run.post("bad-ok", f"/ok {HEAD_A}", author="stranger")
    run.poll(2)
    assert run.rows("SELECT workflow_state FROM ticket_state") == [("awaiting-ok",)]

    # Approver /ok <sha> clears the visual hold and ships.
    run.post("ok1", f"/ok {HEAD_A}", author="maxharris1")
    run.poll(4)
    assert run.rows("SELECT workflow_state FROM ticket_state") == [("merged",)]
    assert run.actions.merges == [(True, HEAD_A)]


def test_removing_hold_label_alone_does_not_clear_visual_qa_review() -> None:
    hold = HoldConfig(label="factory:hold", approver="maxharris1", paths=())
    removed = LabelEvent(
        id="10",
        action="unlabeled",
        label="factory:hold",
        actor="maxharris1",
        created_at=100.0,
    )
    decision = decide(
        hold,
        labels=(),
        changed_files=("src/app.tsx",),
        label_events=(removed,),
        comments=(),
        approver_allowed=True,
        covers=lambda sha: sha == HEAD_A,
        factory_md_trust_changed=False,
        check_configured_hold=True,
        visual_qa_hold=True,
    )
    assert decision.blocks is True
    assert decision.reasons == (VISUAL_QA_REASON,)
    assert decision.cleared is None

    ok_comment = Comment(id="ok1", author="maxharris1", body=f"/ok {HEAD_A}", created_at=200.0)
    cleared = decide(
        hold,
        labels=(),
        changed_files=("src/app.tsx",),
        label_events=(removed,),
        comments=(ok_comment,),
        approver_allowed=True,
        covers=lambda sha: sha == HEAD_A,
        factory_md_trust_changed=False,
        check_configured_hold=True,
        visual_qa_hold=True,
    )
    assert cleared.blocks is False
    assert cleared.cleared == "ok"


def test_ok_carries_across_rebase_with_same_stable_patch_id_and_blocks_new_patch(
    tmp_path: Path,
) -> None:
    # Two heads with identical 40-char hex stable patch-id: HEAD_A -> HEAD_B.
    patch_ids = {HEAD_A: "1" * 40, HEAD_B: "1" * 40}

    def diff(repo, base, since, head):
        return HeadDiff(patch_ids[head], (("src/page.tsx", 5),))

    run = _setup_approved(
        tmp_path,
        ship="merge",
        gate="block",
        hold=None,
        qa_diff=diff,
        patch_ids=patch_ids,
    )
    folder = _make_evidence(tmp_path / "ev_carry", "lane-1/qa-1/screen.png")
    _record_primary_evidence(run, HEAD_A, folder)
    run.post("q1", qa_report(HEAD_A, "PASS", body=EVIDENCED))
    run.poll(4)
    assert run.rows("SELECT workflow_state FROM ticket_state") == [("awaiting-ok",)]

    # Rebase to HEAD_B with same patch_id BEFORE /ok -> carried QA PASS still holds!
    run.thread.head = HEAD_B
    run.poll(4)
    assert run.rows("SELECT workflow_state FROM ticket_state") == [("awaiting-ok",)]
    assert run.rows("SELECT head, reasons FROM ship_holds") == [(HEAD_B, VISUAL_QA_REASON)]
    # Wake for carried head still carries the visual QA evidence link.
    wakes = _wakes(run)
    assert any(w["head"] == HEAD_B and w["qa"]["evidence_link"] == EVIDENCE_URL for w in wakes)

    # Now Max posts /ok HEAD_A; because patch_id(HEAD_A) == patch_id(HEAD_B), it carries and ships!
    run.post("ok-a", f"<!-- orch:v1 kind=ok head={HEAD_A} -->", author="maxharris1")
    run.poll(4)
    assert run.rows("SELECT workflow_state FROM ticket_state") == [("merged",)]
    assert run.actions.merges == [(True, HEAD_B)]


def test_new_patch_does_not_inherit_prior_ok_for_visual_qa_hold(tmp_path: Path) -> None:
    patch_ids = {HEAD_A: "1" * 40, HEAD_B: "2" * 40}

    def diff(repo, base, since, head):
        return HeadDiff(patch_ids[head], (("src/page.tsx", 12),))

    run = _setup_approved(
        tmp_path,
        ship="merge",
        gate="block",
        hold=None,
        qa_diff=diff,
        patch_ids=patch_ids,
    )
    folder_a = _make_evidence(tmp_path / "ev_a", "lane-1/qa-1/screen.png")
    _record_primary_evidence(run, HEAD_A, folder_a)
    run.post("q1", qa_report(HEAD_A, "PASS", body=EVIDENCED))
    run.poll(4)
    assert run.rows("SELECT workflow_state FROM ticket_state") == [("awaiting-ok",)]

    # Prior /ok for HEAD_A does not carry to HEAD_B with a different patch_id.
    run.post("ok-a", f"/ok {HEAD_A}", author="maxharris1")
    run.thread.head = HEAD_B
    run.poll(2)
    run.finish("codex", "v2")
    run.post("v2", verdict(HEAD_B, "APPROVE", "1/3"))
    run.poll(2)
    folder_b = _make_evidence(tmp_path / "ev_b", "lane-1/qa-1/screen2.png")
    _record_primary_evidence(run, HEAD_B, folder_b)
    run.post("q2", qa_report(HEAD_B, "PASS", body=EVIDENCED))
    run.poll(4)

    assert run.rows("SELECT workflow_state FROM ticket_state") == [("awaiting-ok",)]
    assert run.rows("SELECT head, reasons FROM ship_holds") == [(HEAD_B, VISUAL_QA_REASON)]

    # Fresh /ok for HEAD_B clears the hold.
    run.post("ok-b", f"/ok {HEAD_B}", author="maxharris1")
    run.poll(4)
    assert run.rows("SELECT workflow_state FROM ticket_state") == [("merged",)]
    assert run.actions.merges == [(True, HEAD_B)]


def test_aut0_handoff_repo_holds_on_visual_qa_and_hands_off_never_merges(tmp_path: Path) -> None:
    run = _setup_approved(tmp_path, ship="handoff", gate="block", hold=None)
    folder = _make_evidence(tmp_path / "ev_aut0", "lane-1/qa-1/ui.jpg")
    _record_primary_evidence(run, HEAD_A, folder)
    run.post("q1", qa_report(HEAD_A, "PASS", body=EVIDENCED))
    run.poll(4)

    assert run.actions.merges == []
    assert run.actions.handoffs == []
    assert run.rows("SELECT workflow_state FROM ticket_state") == [("awaiting-ok",)]

    run.post("ok1", f"/ok {HEAD_A}", author="maxharris1")
    run.poll(4)

    assert run.actions.merges == []
    assert run.actions.handoffs == [HEAD_A]
    assert run.rows("SELECT workflow_state FROM ticket_state") == [("handed-off",)]


def _same_patch_rebase_held(
    tmp_path: Path, gate: str, ship: str = "merge"
) -> tuple[Harness, Path]:
    patch_ids = {HEAD_A: "1" * 40, HEAD_B: "1" * 40}
    run = _setup_approved(
        tmp_path,
        ship=ship,
        gate=gate,
        qa_diff=lambda *_args: HeadDiff("1" * 40, (("src/page.tsx", 5),)),
        patch_ids=patch_ids,
    )
    [(run_id,)] = run.rows("SELECT run_id FROM qa_runs")
    folder = run_dir(tmp_path / "evidence", REPO, TICKET, run_id)
    lane = folder / "lane-1"
    _make_evidence(lane, "qa-1/screen.png")
    (lane / "result.json").write_text(json.dumps({"scenarios": [{"id": "qa-1", "result": "verified"}]}))
    (lane / "manifest.json").write_text("{}")
    _record_primary_evidence(run, HEAD_A, folder)
    run.post("q1", qa_report(HEAD_A, "PASS", body=EVIDENCED))
    run.poll(3)
    assert run.rows("SELECT workflow_state FROM ticket_state") == [("awaiting-ok",)]
    run.thread.head = HEAD_B
    run.poll()
    return run, lane / "qa-1" / "screen.png"


@pytest.mark.parametrize("gate", ["report", "block"])
def test_same_patch_rebase_stays_held_under_either_qa_gate(tmp_path: Path, gate: str) -> None:
    run, _screen = _same_patch_rebase_held(tmp_path, gate)
    run.poll(3)

    assert run.rows("SELECT workflow_state FROM ticket_state") == [("awaiting-ok",)]
    assert run.actions.merges == []


@pytest.mark.parametrize("ship", ["merge", "handoff"])
@pytest.mark.parametrize("gate", ["report", "block"])
def test_rebased_visual_hold_wake_links_the_source_evidence(
    tmp_path: Path, gate: str, ship: str
) -> None:
    run, _screen = _same_patch_rebase_held(tmp_path, gate, ship)
    run.poll(3)

    [wake] = [w for w in _wakes(run) if w["head"] == HEAD_B]
    assert VISUAL_QA_REASON in wake["reasons"]
    assert wake["qa"]["result"] == "PASS"
    assert wake["qa"]["evidence_link"] == EVIDENCE_URL


@pytest.mark.parametrize("gate", ["report", "block"])
def test_visual_hold_wake_links_the_visual_run_over_a_later_non_visual_pass(
    tmp_path: Path, gate: str
) -> None:
    patch_ids = {HEAD_A: "1" * 40, HEAD_B: "2" * 40}
    run = _setup_approved(
        tmp_path,
        gate=gate,
        qa_diff=lambda _repo, _base, _since, head: HeadDiff(patch_ids[head], (("src/page.tsx", 12),)),
        patch_ids=patch_ids,
    )
    _record_primary_evidence(run, HEAD_A, _make_evidence(tmp_path / "ev_a", "lane-1/qa-1/screen.png"))
    run.post("q1", qa_report(HEAD_A, "PASS", body=EVIDENCED))
    run.poll(4)
    run.thread.head = HEAD_B
    run.poll(2)
    run.finish("codex", "v2")
    run.post("v2", verdict(HEAD_B, "APPROVE", "1/3"))
    run.poll(2)
    _record_primary_evidence(run, HEAD_B, _make_evidence(tmp_path / "ev_b", "lane-1/qa-1/log.txt"))
    run.post("q2", qa_report(HEAD_B, "PASS", body=TABLE + "Evidence: https://qa.example/runs/logs-2\n"))
    run.poll(4)

    assert run.rows("SELECT head, reasons FROM ship_holds") == [(HEAD_B, VISUAL_QA_REASON)]
    [wake] = [w for w in _wakes(run) if w["head"] == HEAD_B]
    assert wake["qa"]["evidence_link"] == EVIDENCE_URL


def test_recycling_keeps_the_carried_source_visuals_and_the_hold(tmp_path: Path) -> None:
    run, screen = _same_patch_rebase_held(tmp_path, "block")

    with run.queue.connect() as connection:
        recycled = recycle(connection, root=tmp_path / "evidence", repo=REPO, now=run.now[0])
    run.poll(3)

    assert recycled.deleted == ()
    assert screen.exists()
    assert run.rows("SELECT workflow_state FROM ticket_state") == [("awaiting-ok",)]
    assert run.actions.merges == []


def test_visual_hold_outlives_its_deleted_evidence(tmp_path: Path) -> None:
    run, screen = _same_patch_rebase_held(tmp_path, "block")

    screen.unlink()
    run.poll(3)

    assert run.rows("SELECT workflow_state FROM ticket_state") == [("awaiting-ok",)]
    assert run.actions.merges == []
    run.post("ok-b", f"/ok {HEAD_B}", author="maxharris1")
    run.poll(3)
    assert run.actions.merges == [(True, HEAD_B)]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _hand_edited_base_merge(root: Path) -> tuple[Path, str, str, dict[str, str]]:
    repo = root / "history"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "src").mkdir()
    (repo / "src/page.tsx").write_text("original UI\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "src/page.tsx").write_text("approved UI\n")
    _git(repo, "commit", "-q", "-am", "approved visual")
    approved = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    (repo / "README.md").write_text("upstream\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "upstream")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "merge", "-q", "--no-ff", "--no-commit", "main")
    (repo / "src/page.tsx").write_text("new unapproved UI\n")
    _git(repo, "commit", "-q", "-am", "merge main plus new UI")
    head = _git(repo, "rev-parse", "HEAD")
    patch_ids = {
        sha: subprocess.run(
            ["git", "patch-id", "--stable"],
            input=_git(repo, "diff", f"main...{sha}") + "\n",
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()[0]
        for sha in (approved, head)
    }
    return repo, approved, head, patch_ids


@pytest.mark.parametrize("ship", ["merge", "handoff"])
def test_hand_edited_base_merge_needs_a_fresh_visual_ok(tmp_path: Path, ship: str) -> None:
    repo, approved, head, patch_ids = _hand_edited_base_merge(tmp_path)
    assert merge_update(repo / ".git", approved, head, "main").steps[0].hand_merged == {"src/page.tsx"}
    run = Harness(
        tmp_path,
        replace(qa_config(), gate="block"),
        ship=ship,
        qa_diff=lambda _repo, _base, _since, sha: HeadDiff(patch_ids[sha], (("src/page.tsx", 500),)),
    )
    run.actions.patch_ids.update(patch_ids)
    run.dispatcher.hold_carry = lambda _repo, _base, old, new: merge_update(repo / ".git", old, new, "main")
    run.post("r1", ready(approved, body=scenarios("qa-1")), head=approved)
    run.poll()
    run.finish("codex", "v1")
    run.post("v1", verdict(approved, "APPROVE"))
    _record_primary_evidence(run, approved, _make_evidence(tmp_path / "ev_a", "lane-1/shot.png"))
    run.post("q1", qa_report(approved, "PASS", body=TABLE))
    run.poll(3)
    assert run.rows("SELECT workflow_state FROM ticket_state") == [("awaiting-ok",)]

    run.post("ok-a", f"/ok {approved}", author="maxharris1")
    run.thread.head = head
    run.poll(2)
    run.finish("codex", "v2")
    run.post("v2", verdict(head, "APPROVE", "1/3"))
    run.poll(2)
    _record_primary_evidence(run, head, _make_evidence(tmp_path / "ev_b", "lane-1/shot.png"))
    run.post("q2", qa_report(head, "PASS", body=TABLE))
    run.poll(3)

    assert run.actions.merges == []
    assert run.actions.handoffs == []
    assert run.rows("SELECT workflow_state FROM ticket_state") == [("awaiting-ok",)]


def test_rerun_qa_keeps_visual_hold_and_never_merges_without_ok(tmp_path: Path) -> None:
    from tests.test_dashboard_cockpit import _body

    run = _setup_approved(tmp_path, ship="merge", gate="observe", hold=None)
    folder = _make_evidence(tmp_path / "ev_rerun", "lane-1/qa-1/home.png")
    _record_primary_evidence(run, HEAD_A, folder)
    run.post("q1", qa_report(HEAD_A, "PASS", body=EVIDENCED))
    run.poll(4)
    assert run.rows("SELECT workflow_state FROM ticket_state") == [("awaiting-ok",)]

    run.post("rr-1", _body("rerun-qa", "Re-check the drawer.", head=HEAD_A))
    run.poll()
    assert run.rows("SELECT outcome FROM handled_comments WHERE comment_id = 'rr-1'") == [
        ("queued-rerun-qa",)
    ]
    run.poll(2)

    assert run.actions.merges == []
    assert run.rows("SELECT workflow_state FROM ticket_state") == [("awaiting-ok",)]
    assert run.rows("SELECT reasons FROM ship_holds") == [(VISUAL_QA_REASON,)]


def test_rerun_qa_carries_the_live_scenarios_of_a_spilled_run(tmp_path: Path) -> None:
    from tests.test_dashboard_cockpit import _body

    run = _setup_approved(tmp_path, ship="merge", gate="observe", hold=None)
    _record_primary_evidence(run, HEAD_A, _make_evidence(tmp_path / "ev_rerun", "lane-1/qa-1/home.png"))
    run.post("q1", qa_report(HEAD_A, "PASS", body=EVIDENCED))
    run.poll(4)
    [(job_id,)] = run.rows("SELECT job_id FROM qa_runs")
    with run.queue.connect() as connection:
        [(detail,)] = connection.execute("SELECT detail FROM jobs WHERE id = ?", (job_id,)).fetchall()
        detail = json.loads(detail)
        detail["live"][0]["steps"] = [*detail["live"][0]["steps"], "x" * 1100]
        connection.execute("UPDATE jobs SET detail = ? WHERE id = ?", (json.dumps(detail), job_id))
    spill_while_abandoned(run.queue, job_id)

    run.post("rr-1", _body("rerun-qa", "Re-check the drawer.", head=HEAD_A))
    run.poll()

    [rerun] = [job for job in run.queue.outstanding() if job.request.action == "qa"]
    assert json.loads(rerun.request.detail)["live"] == detail["live"]
