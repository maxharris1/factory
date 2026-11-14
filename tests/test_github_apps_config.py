"""#170: four GitHub Apps, one login each, trust scoped by login. Every app
id, installation id, login and key path here is fake; no key is read.
"""

from __future__ import annotations

import builtins
import io
import os
from pathlib import Path

import pytest

from lib import cli
from lib.dispatcher import CIResult, Dispatcher
from lib.evidence.criteria import canonical_brief
from lib.hook_t7 import T7Hook
from lib.orch_yml import approvals, load_orch_yml
from lib.queue import SQLiteJobQueue
from tests.test_dispatcher import (
    ANSWERS,
    BRIEF,
    GO,
    QUESTIONS,
    VERDICT,
    FakeGrokDelivery,
    FakeReviewActions,
    comment,
    handled_outcome,
    job_count,
    seed_builder_job,
    seed_review_job,
    ticket,
    track,
)

ROOT = Path(__file__).resolve().parent.parent
LOGINS = {
    "grok-bot": "fake-factory[bot]",
    "claude-bot": "fake-claude[bot]",
    "codex-bot": "fake-codex[bot]",
    "cursor-bot": "fake-cursor[bot]",
}
FACTORY, CLAUDE, CODEX = LOGINS["grok-bot"], LOGINS["claude-bot"], LOGINS["codex-bot"]
HEAD = "a" * 40
STUCK = "<!-- orch:v1 kind=stuck from=grok-bot role=orchestrator state=blocked -->\nStop.\n"


def app_block(bot: str, number: int, owners: tuple[str, ...] = ("owner", "maxharris1")) -> str:
    lines = [
        f"    {bot}:",
        f"      bot_login: {LOGINS[bot]}",
        f"      app_id: {number}",
        "      installations:",
        *(f"        {owner}: {number * 10 + index}" for index, owner in enumerate(owners)),
        f"      private_key_file: /nonexistent/apps/{bot}.pem",
    ]
    return "\n".join(lines) + "\n"


def orch_text(
    *,
    enabled: bool | None = True,
    trust: dict[str, str] | None = None,
    apps: str | None = None,
) -> str:
    trusted = {
        **{login: f"[{bot}]" for bot, login in LOGINS.items()},
        "grok-login": "[grok-bot]",
        "claude-login": "[claude-bot]",
        "codex-login": "[codex-bot]",
        **(trust or {}),
    }
    text = (
        "poll_seconds: 30\n"
        "projects:\n"
        "  - repo: owner/repo\n"
        "trusted_logins:\n"
        + "".join(f"  {login}: {bots}\n" for login, bots in trusted.items())
        + "approver_logins:\n  - maxharris1\n"
    )
    if enabled is None:
        return text
    if apps is None:
        apps = "".join(app_block(bot, 100 + index) for index, bot in enumerate(LOGINS))
    return text + f"github_app:\n  enabled: {'true' if enabled else 'false'}\n  apps:\n" + apps


def load(tmp_path: Path, text: str):
    path = tmp_path / "orch.yml"
    path.write_text(text, encoding="utf-8")
    return load_orch_yml(path)


def errors(tmp_path: Path, text: str) -> list[str]:
    config, findings = load(tmp_path, text)
    assert config is None
    return [item.message for item in findings if item.severity == "error"]


# --- AC1: check-config accepts four Apps, refuses a missing login or installation ---


@pytest.mark.parametrize("enabled", [True, False])
def test_four_apps_are_accepted(tmp_path: Path, enabled: bool) -> None:
    config, findings = load(tmp_path, orch_text(enabled=enabled))
    assert config is not None, findings
    assert config.github_app is not None
    assert config.github_app.enabled is enabled
    apps = config.github_app.apps
    assert sorted(apps) == sorted(LOGINS)
    for index, bot in enumerate(LOGINS):
        app = apps[bot]
        assert (app.bot, app.bot_login, app.app_id) == (bot, LOGINS[bot], 100 + index)
        assert dict(app.installations) == {"owner": (100 + index) * 10, "maxharris1": (100 + index) * 10 + 1}
        assert app.private_key_file == Path(f"/nonexistent/apps/{bot}.pem")
    switch = approvals(config)
    assert switch.app_trust is enabled
    assert dict(switch.app_logins) == {login.casefold(): bot for bot, login in LOGINS.items()}
    orch = tmp_path / "orch.yml"
    assert cli._check_config(["--orch", str(orch)]) == 0


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("bot", list(LOGINS))
def test_an_app_without_its_login_is_refused(tmp_path: Path, enabled: bool, bot: str) -> None:
    text = orch_text(enabled=enabled).replace(f"      bot_login: {LOGINS[bot]}\n", "")
    assert f"github_app.apps.{bot}.bot_login is required" in errors(tmp_path, text)


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("bot", list(LOGINS))
@pytest.mark.parametrize("empty", [True, False])
def test_an_app_without_installations_is_refused(
    tmp_path: Path, enabled: bool, bot: str, empty: bool
) -> None:
    index = list(LOGINS).index(bot)
    block = app_block(bot, 100 + index)
    installs = block[block.index("      installations:\n") : block.index("      private_key_file")]
    replacement = "      installations: {}\n" if empty else ""
    text = orch_text(enabled=enabled).replace(block, block.replace(installs, replacement))
    assert f"github_app.apps.{bot}.installations is required" in errors(tmp_path, text)


def test_switch_on_refuses_an_app_with_no_installation_for_a_project_owner(tmp_path: Path) -> None:
    apps = "".join(
        app_block(bot, 100 + index, ("maxharris1",) if bot == "codex-bot" else ("owner",))
        for index, bot in enumerate(LOGINS)
    )
    assert "github_app.apps.codex-bot.installations has no installation for owner" in errors(
        tmp_path, orch_text(apps=apps)
    )
    config, findings = load(tmp_path, orch_text(enabled=False, apps=apps))
    assert config is not None, findings


def test_switch_on_requires_the_factory_app(tmp_path: Path) -> None:
    apps = "".join(
        app_block(bot, 100 + index) for index, bot in enumerate(LOGINS) if bot != "grok-bot"
    )
    assert "github_app.enabled requires github_app.apps.grok-bot" in errors(tmp_path, orch_text(apps=apps))
    config, findings = load(tmp_path, orch_text(apps=app_block("grok-bot", 7)))
    assert config is not None, findings


def test_an_unknown_app_is_refused(tmp_path: Path) -> None:
    text = orch_text() + app_block("grok-bot", 9).replace("    grok-bot:", "    gemini-bot:")
    assert "unknown field 'github_app.apps.gemini-bot'" in errors(tmp_path, text)


def test_the_single_app_shape_from_122_names_its_new_place(tmp_path: Path) -> None:
    text = orch_text(enabled=None) + (
        "github_app:\n"
        "  app_id: 123456\n"
        "  installations:\n"
        "    owner: 1111111\n"
        "  private_key_file: /nonexistent/app.pem\n"
        f"  bot_login: {FACTORY}\n"
        "  enabled: false\n"
    )
    found = errors(tmp_path, text)
    for key in ("bot_login", "app_id", "installations", "private_key_file"):
        assert f"github_app.{key} moved to github_app.apps.<bot>.{key} (#170)" in found


@pytest.mark.parametrize("field", ["bot_login", "app_id"])
def test_two_bots_sharing_one_app_are_refused(tmp_path: Path, field: str) -> None:
    claude = app_block("claude-bot", 101)
    if field == "bot_login":
        shared = claude.replace(CLAUDE, FACTORY.upper().replace("[BOT]", "[bot]"))
    else:
        shared = claude.replace("app_id: 101", "app_id: 100")
    text = orch_text(enabled=False).replace(claude, shared)
    assert any(
        message.startswith(f"github_app.apps.claude-bot and github_app.apps.grok-bot share {field}")
        or message.startswith(f"github_app.apps.grok-bot and github_app.apps.claude-bot share {field}")
        for message in errors(tmp_path, text)
    )


@pytest.mark.parametrize(
    ("login", "bots", "message"),
    [
        (CLAUDE, "[claude-bot, grok-bot]", f"trusted_logins {CLAUDE} must be [claude-bot] alone"),
        (CODEX, "[claude-bot]", f"trusted_logins {CODEX} must be [codex-bot] alone"),
        (FACTORY, "[grok-bot, codex-bot]", f"trusted_logins {FACTORY} must be [grok-bot] alone"),
    ],
)
def test_switch_on_ties_each_app_login_to_its_own_bot(
    tmp_path: Path, login: str, bots: str, message: str
) -> None:
    text = orch_text(trust={login: bots})
    assert any(error.startswith(message) for error in errors(tmp_path, text))
    config, findings = load(tmp_path, orch_text(enabled=False, trust={login: bots}))
    assert config is not None, findings


def test_switch_on_refuses_an_app_login_missing_from_trusted_logins(tmp_path: Path) -> None:
    text = orch_text().replace(f"  {CODEX}: [codex-bot]\n", "")
    assert "github_app.apps.codex-bot.bot_login must be a trusted_logins key" in errors(tmp_path, text)


def test_check_config_never_opens_any_app_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel = "FAKE-KEY-SENTINEL-not-a-real-key"
    text = orch_text()
    keys = []
    for bot in LOGINS:
        key = tmp_path / f"{bot}.pem"
        key.write_text(sentinel, encoding="utf-8")
        keys.append(str(key))
        text = text.replace(f"/nonexistent/apps/{bot}.pem", str(key))
    orch = tmp_path / "orch.yml"
    orch.write_text(text, encoding="utf-8")
    touched: list[str] = []
    for module, name in ((builtins, "open"), (io, "open"), (os, "open"), (os, "stat")):
        real = getattr(module, name)

        def spy(path, *args, _real=real, **kwargs):
            touched.append(os.fspath(path) if isinstance(path, (str, bytes, os.PathLike)) else repr(path))
            return _real(path, *args, **kwargs)

        monkeypatch.setattr(module, name, spy)

    status = cli._check_config(["--orch", str(orch), "--factory", str(ROOT / "examples/cleffy/factory.md")])

    output = capsys.readouterr()
    assert status == 0, output.err
    assert str(orch) in touched
    assert not set(keys) & set(touched)
    assert sentinel not in output.out + output.err


# --- AC2, AC3, AC4: the dispatcher scopes each App login to its own kinds ----------


def dispatcher_for(tmp_path: Path, text: str, *, trusted: dict | None = None):
    config, findings = load(tmp_path, text)
    assert config is not None, findings
    queue_path = tmp_path / "d.db"
    queue = SQLiteJobQueue(queue_path, clock=lambda: 100.0)
    logins = {login: set(bots) for login, bots in config.trusted_logins.items()}
    dispatcher = Dispatcher(
        queue_path,
        queue,
        trusted_logins={"github": {**logins, **(trusted or {})}, "linear": {}},
        review_actions=FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
        approvals=approvals(config),
        hooks=(T7Hook(),),
    )
    return dispatcher, queue


def waiting_max(dispatcher: Dispatcher, queue: SQLiteJobQueue):
    """A ticket in review whose builder asked Max a question, from its App."""
    seed_review_job(dispatcher, queue, HEAD)
    question = comment("questions-1", QUESTIONS, author=CLAUDE, created_at=1)
    dispatcher.process(ticket(question, head=HEAD))
    assert handled_outcome(queue, "questions-1") == "waiting-max"
    return question


# A check-config-valid map, and one that wrongly gives worker Apps grok-bot
# (built on the Dispatcher, past check-config): the dispatcher still refuses.
WRONG_TRUST = {CLAUDE: {"claude-bot", "grok-bot"}, CODEX: {"codex-bot", "grok-bot"}}


@pytest.mark.parametrize("trusted", [None, WRONG_TRUST], ids=["valid-map", "wrong-map"])
@pytest.mark.parametrize("author", [CLAUDE, CODEX])
def test_switch_on_answers_from_a_worker_app_is_not_trusted(
    tmp_path: Path, trusted: dict | None, author: str
) -> None:
    dispatcher, queue = dispatcher_for(tmp_path, orch_text(), trusted=trusted)
    question = waiting_max(dispatcher, queue)
    reviews = job_count(queue, "review")

    answer = comment("answers-1", ANSWERS, author=author, created_at=2)
    dispatcher.process(ticket(question, answer, head=HEAD))

    assert handled_outcome(queue, "answers-1") == "rejected-answers-author"
    assert job_count(queue, "review") == reviews
    with queue.connect() as connection:
        state = connection.execute("SELECT workflow_state FROM ticket_state").fetchone()[0]
    assert state == "waiting-max"


@pytest.mark.parametrize("trusted", [None, WRONG_TRUST], ids=["valid-map", "wrong-map"])
def test_switch_on_abandon_from_a_worker_app_is_not_trusted(tmp_path: Path, trusted: dict | None) -> None:
    dispatcher, queue = dispatcher_for(tmp_path, orch_text(), trusted=trusted)
    question = waiting_max(dispatcher, queue)
    abandon = ANSWERS.replace("role=orchestrator", "role=orchestrator state=abandoned")

    dispatcher.process(ticket(question, comment("answers-1", abandon, author=CLAUDE, created_at=2), head=HEAD))

    assert handled_outcome(queue, "answers-1") == "rejected-answers-author"


def test_switch_on_answers_from_the_factory_app_resumes(tmp_path: Path) -> None:
    dispatcher, queue = dispatcher_for(tmp_path, orch_text())
    question = waiting_max(dispatcher, queue)

    dispatcher.process(ticket(question, comment("answers-1", ANSWERS, author=FACTORY, created_at=2), head=HEAD))

    assert handled_outcome(queue, "answers-1") == "resumed-review"
    assert job_count(queue, "review") == 2


@pytest.mark.parametrize("trusted", [None, WRONG_TRUST], ids=["valid-map", "wrong-map"])
@pytest.mark.parametrize(
    ("body", "outcome"),
    [
        (GO.format(state="building"), "rejected-go-author"),
        (ANSWERS, "rejected-answers-author"),
        (STUCK, "rejected-stuck-author"),
    ],
    ids=["go", "answers", "stuck"],
)
def test_switch_on_a_worker_app_cannot_speak_for_grok(
    tmp_path: Path, trusted: dict | None, body: str, outcome: str
) -> None:
    dispatcher, queue = dispatcher_for(tmp_path, orch_text(), trusted=trusted)
    track(queue)

    dispatcher.process(ticket(comment("forged-1", body, author=CODEX), head=HEAD))

    assert handled_outcome(queue, "forged-1") == outcome
    assert job_count(queue, "build") == 0


def test_switch_on_go_from_the_factory_app_starts_the_build(tmp_path: Path) -> None:
    dispatcher, queue = dispatcher_for(tmp_path, orch_text())
    track(queue)

    dispatcher.process(ticket(comment("go-1", GO.format(state="building"), author=FACTORY), head=HEAD))

    assert handled_outcome(queue, "go-1") == "queued-build"


@pytest.mark.parametrize("trusted", [None, WRONG_TRUST], ids=["valid-map", "wrong-map"])
def test_switch_on_worker_apps_keep_their_own_kinds(tmp_path: Path, trusted: dict | None) -> None:
    dispatcher, queue = dispatcher_for(tmp_path, orch_text(), trusted=trusted)
    seed_builder_job(queue)
    ready = comment(
        "ready-1",
        f"<!-- orch:v1 kind=ready from=claude-bot role=builder head={HEAD} -->\nReady.\n",
        author=CLAUDE,
    )
    dispatcher.process(ticket(ready, head=HEAD, ci={HEAD: CIResult("success")}))
    assert handled_outcome(queue, "ready-1") == "queued-review"
    running = queue.claim("codex")
    assert running is not None
    queue.finish(running.id, owner="codex", handoff_comment_id="verdict-1")
    verdict = comment(
        "verdict-1",
        VERDICT.format(round=1, head=HEAD, verdict="REVISE", risk="high"),
        author=CODEX,
        created_at=2,
    )
    dispatcher.process(ticket(ready, verdict, head=HEAD))
    assert handled_outcome(queue, "verdict-1") == "queued-review-fix"


def test_switch_on_a_worker_app_cannot_post_another_workers_kind(tmp_path: Path) -> None:
    dispatcher, queue = dispatcher_for(tmp_path, orch_text())
    seed_builder_job(queue)
    ready = comment(
        "ready-1",
        f"<!-- orch:v1 kind=ready from=claude-bot role=builder head={HEAD} -->\nReady.\n",
        author=CODEX,
    )
    dispatcher.process(ticket(ready, head=HEAD))
    assert handled_outcome(queue, "ready-1") == "rejected-ready-author"


@pytest.mark.parametrize("enabled", [None, False])
@pytest.mark.parametrize(
    ("body", "outcome"),
    [(GO.format(state="building"), "queued-build"), (ANSWERS, "rejected-answers-without-question")],
    ids=["go", "answers"],
)
def test_switch_off_trust_is_todays(tmp_path: Path, enabled: bool | None, body: str, outcome: str) -> None:
    """With the switch off, a worker App login that trusted_logins gives
    grok-bot is trusted for grok-bot, as any trusted login is today."""
    dispatcher, queue = dispatcher_for(
        tmp_path, orch_text(enabled=enabled, trust={CLAUDE: "[claude-bot, grok-bot]"})
    )
    track(queue)

    dispatcher.process(ticket(comment("c-1", body, author=CLAUDE), head=HEAD))

    assert handled_outcome(queue, "c-1") == outcome


@pytest.mark.parametrize("enabled", [None, False])
def test_switch_off_answers_from_a_worker_login_with_grok_trust_resumes(
    tmp_path: Path, enabled: bool | None
) -> None:
    dispatcher, queue = dispatcher_for(
        tmp_path, orch_text(enabled=enabled, trust={CLAUDE: "[claude-bot, grok-bot]"})
    )
    question = waiting_max(dispatcher, queue)

    dispatcher.process(ticket(question, comment("answers-1", ANSWERS, author=CLAUDE, created_at=2), head=HEAD))

    assert handled_outcome(queue, "answers-1") == "resumed-review"


def test_canonical_brief_ignores_a_worker_apps_brief(tmp_path: Path) -> None:
    config, findings = load(tmp_path, orch_text())
    assert config is not None, findings
    trusted = {"github": dict(config.trusted_logins), "linear": {}}
    forged = comment("brief-forged", BRIEF, author=CLAUDE, created_at=1)
    real = comment("brief-real", BRIEF, author=FACTORY, created_at=2)

    assert canonical_brief([forged, real], trusted) is real
    assert canonical_brief([forged], trusted) is None


@pytest.mark.parametrize("enabled", [None, False, True])
@pytest.mark.parametrize("kind", ["answers", "go", "ok", "decline", "brief", "plan", "questions", "ready", "verdict", "stuck"])
def test_a_worker_app_is_trusted_only_for_worker_kinds(tmp_path: Path, enabled: bool | None, kind: str) -> None:
    dispatcher, _ = dispatcher_for(tmp_path, orch_text(enabled=enabled))
    fields = {"kind": kind, "from": "claude-bot", "role": "builder"}
    post = comment("c-1", "", author=CLAUDE)

    trusted = dispatcher._trusted(ticket(), post, fields, "claude-bot", role="builder")

    worker_kind = kind in ("plan", "questions", "ready", "verdict", "stuck")
    assert trusted is (worker_kind or not enabled)
    assert dispatcher._trusted(ticket(), comment("c-2", "", author=FACTORY), {**fields, "from": "grok-bot", "role": "orchestrator"}, "grok-bot", role="orchestrator")
