import ast
import json
import os
import shlex
import signal
import sys
import time
from pathlib import Path

import pytest

from lib import cli, skill_probe
from lib.doctor_live import last_reply, live_canary
from lib.harnesses import all_drivers
from lib.machines_yml import Device, MachinesConfig, load_machines_yml
from lib.orch_yml import load_orch_yml
from lib.pstack_models import rules_text
from lib.pstack_pin import (
    RULES_PATH,
    ROOT,
    canary_prompt_for,
    install_skills,
    installed_errors,
    load_pin,
    pinned_token,
    remove_claude_extras,
    score_reply,
)
from lib.skill_probe import launch_argv, plan_canary
from lib.waiter import WaiterError
from tests.test_pstack_install import snapshot
from tests.test_remote_start import Ssh, use_fake_ssh

PSTACK = ROOT / "skills" / "pstack"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    assert install_skills(ROOT, home, "local", rules=rules_text(ROOT)) == []
    assert installed_errors(ROOT, home) == []
    return home


def install(home: Path) -> str:
    return f"bin/orch install-skills --home {shlex.quote(str(home))}"


def run_fix(home: Path) -> str:
    return f"run {install(home)}"


def remove_then_install(home: Path) -> str:
    return f"run {install(home)} --remove-claude-extras, then {install(home)}"


def move_aside(home: Path) -> str:
    return f"move it aside, then {run_fix(home)}"


def move_blocker(home: Path, blocker: Path) -> str:
    return f"move {blocker} aside, then {run_fix(home)}"


def flagged(home: Path) -> list[str]:
    before = snapshot(home)
    errors = installed_errors(ROOT, home)
    assert snapshot(home) == before, "the doctor wrote to the home"
    return errors


def install_fixes(home: Path) -> None:
    assert install_skills(ROOT, home, "local", rules=rules_text(ROOT)) == []
    assert installed_errors(ROOT, home) == []


def test_a_stale_rules_file_is_flagged_with_the_install_command(home: Path) -> None:
    rules = home / RULES_PATH
    rules.write_text(rules_text(ROOT).splitlines(keepends=True)[0] + "feature, refactoring: sonnet low\n")
    assert flagged(home) == [f"claude: did not load the pinned version ({rules} is stale); {run_fix(home)}"]
    install_fixes(home)


def test_a_missing_rules_file_is_flagged(home: Path) -> None:
    rules = home / RULES_PATH
    rules.unlink()
    assert flagged(home) == [f"claude: did not load the pinned version ({rules} is missing); {run_fix(home)}"]
    install_fixes(home)


def test_a_symlinked_rules_file_must_be_moved_aside(home: Path, tmp_path: Path) -> None:
    rules = home / RULES_PATH
    elsewhere = tmp_path / "pstack-models.md"
    elsewhere.write_text(rules_text(ROOT), encoding="utf-8")
    rules.unlink()
    rules.symlink_to(elsewhere)
    assert flagged(home) == [f"claude: did not load the pinned version ({rules} is a symlink); {move_aside(home)}"]
    assert rules.is_symlink()


def test_a_rules_file_orch_did_not_write_must_be_moved_aside(home: Path) -> None:
    rules = home / RULES_PATH
    rules.write_text("my own model rule\n", encoding="utf-8")
    assert flagged(home) == [
        f"claude: did not load the pinned version ({rules} was not written by orch); {move_aside(home)}"
    ]
    assert rules.read_text(encoding="utf-8") == "my own model rule\n"


def test_a_removed_agent_link_is_flagged_as_missing(home: Path) -> None:
    agent = home / ".claude" / "agents" / "poteto-agent.md"
    agent.unlink()
    assert flagged(home) == [f"claude: did not load the pinned version ({agent} is missing); {run_fix(home)}"]
    install_fixes(home)


def test_a_dangling_docs_link_is_flagged(home: Path, tmp_path: Path) -> None:
    doc = home / ".claude" / "docs" / "harness" / "claude-code.md"
    doc.unlink()
    doc.symlink_to(tmp_path / "old-checkout" / "skills" / "pstack" / "docs" / "harness" / "claude-code.md")
    assert flagged(home) == [f"claude: did not load the pinned version ({doc} is a dangling link); {run_fix(home)}"]
    install_fixes(home)


def test_an_agent_linked_elsewhere_is_flagged_as_the_wrong_target(home: Path, tmp_path: Path) -> None:
    agent = home / ".claude" / "agents" / "comment-sicko.md"
    mine = tmp_path / "comment-sicko.md"
    mine.write_text("my agent\n", encoding="utf-8")
    agent.unlink()
    agent.symlink_to(mine)
    assert flagged(home) == [
        f"claude: did not load the pinned version ({agent} is not the pinned file); {move_aside(home)}"
    ]
    assert agent.resolve() == mine


def test_a_leftover_owned_link_the_pin_no_longer_lists_is_flagged(home: Path) -> None:
    pinned = load_pin(ROOT).hashes()
    assert "agents/retired-agent.md" not in pinned
    leftover = home / ".claude" / "agents" / "retired-agent.md"
    leftover.symlink_to(PSTACK.resolve() / "agents" / "retired-agent.md")
    assert flagged(home) == [
        f"claude: did not load the pinned version ({leftover} is not in the pin); {remove_then_install(home)}"
    ]
    assert leftover.is_symlink()
    assert remove_claude_extras(home, rules_text(ROOT)) == []
    install_fixes(home)
    assert not leftover.is_symlink()


def test_the_repair_command_passes_a_home_with_spaces_as_one_argument(tmp_path: Path) -> None:
    home = tmp_path / "home with spaces"
    install_fixes(home)
    (home / ".claude" / "agents" / "poteto-agent.md").unlink()
    [error] = flagged(home)
    command = error.rpartition("; run ")[2]
    assert shlex.split(command) == ["bin/orch", "install-skills", "--home", str(home)]
    install_fixes(home)


def test_a_file_in_place_of_a_docs_dir_must_be_moved_aside(home: Path, tmp_path: Path) -> None:
    harness = home / ".claude" / "docs" / "harness"
    for link in harness.iterdir():
        link.unlink()
    harness.rmdir()
    harness.write_text("mine\n", encoding="utf-8")
    assert flagged(home) == [
        f"claude: did not load the pinned version ({harness} is not a directory); {move_blocker(home, harness)}"
    ]
    harness.rename(tmp_path / "harness-aside")
    install_fixes(home)


def test_a_symlinked_agents_dir_must_be_moved_aside(home: Path, tmp_path: Path) -> None:
    agents = home / ".claude" / "agents"
    for link in agents.iterdir():
        link.unlink()
    agents.rmdir()
    agents.symlink_to(PSTACK.resolve() / "agents")
    assert flagged(home) == [
        f"claude: did not load the pinned version ({agents} is a symlink); {move_blocker(home, agents)}"
    ]
    agents.rename(tmp_path / "agents-aside")
    install_fixes(home)


def test_a_file_in_place_of_the_rules_dir_must_be_moved_aside(home: Path, tmp_path: Path) -> None:
    rules_dir = home / RULES_PATH.parent
    (home / RULES_PATH).unlink()
    rules_dir.rmdir()
    rules_dir.write_text("mine\n", encoding="utf-8")
    assert flagged(home) == [
        f"claude: did not load the pinned version ({rules_dir} is not a directory); {move_blocker(home, rules_dir)}"
    ]
    rules_dir.rename(tmp_path / "rules-aside")
    install_fixes(home)


def test_a_users_own_agent_beside_the_pinned_ones_is_not_flagged(home: Path) -> None:
    (home / ".claude" / "agents" / "reviewer.md").write_text("keep\n", encoding="utf-8")
    assert installed_errors(ROOT, home) == []


def test_pstack_models_does_not_import_the_pin() -> None:
    tree = ast.parse((ROOT / "lib" / "pstack_models.py").read_text(encoding="utf-8"))
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imported |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert "lib.pstack_pin" not in imported


CANARY_MACHINES = """\
devices:
  - name: box
    local: true
    slot_max: 3
    harnesses: {claude: claude-bot, codex: codex-bot, cursor: cursor-bot, gemini: gemini-bot}
  - name: mac
    tailscale_name: mac.example.ts.net
    slot_max: 2
    harnesses: {claude: claude-bot, codex: codex-bot, cursor: cursor-bot, gemini: gemini-bot}
    workdir_root: /Users/{bot}/factory-work
    ssh_alias: mac-{bot}
    factory_root: /Users/{bot}/factory
"""

WAITERS = {driver.name: (driver.name, "-p", f"--{driver.name}-waiter") for driver in all_drivers()}


def canary_rows(tmp_path: Path, refused: str = "[]"):
    machines_path = tmp_path / "machines.yml"
    machines_path.write_text(CANARY_MACHINES, encoding="utf-8")
    machines, findings = load_machines_yml(machines_path)
    assert machines is not None, findings
    orch_path = tmp_path / "orch.yml"
    orch_path.write_text(
        f"poll_seconds: 30\nprojects:\n  - repo: acme/widgets\nrefused_harnesses: {refused}\n",
        encoding="utf-8",
    )
    orch, findings = load_orch_yml(orch_path, allow_placeholders=True)
    assert orch is not None, findings
    return plan_canary(
        machines, orch.refused_harnesses, WAITERS, run_id="doctor-0000", local_root=str(tmp_path / "work")
    )


def test_plan_canary_has_a_row_per_harness_on_local_and_remote_devices(tmp_path: Path) -> None:
    rows = canary_rows(tmp_path)
    names = [driver.name for driver in all_drivers()]
    assert [(row.device, row.harness) for row in rows] == [
        (device, name) for device in ("box", "mac") for name in names
    ]
    bots = {driver.name: driver.bot_user for driver in all_drivers()}
    for row in rows:
        assert row.skip is None and row.launch is not None
        assert row.bot == bots[row.harness]
        argv = launch_argv(row.launch)
        if row.device == "box":
            assert argv == list(WAITERS[row.harness])
        else:
            assert argv[0] == "ssh"
            assert argv[argv.index("--") + 1] == f"mac-{row.bot}"


def test_plan_canary_skips_harnesses_refused_in_orch_yml(tmp_path: Path) -> None:
    rows = canary_rows(tmp_path, refused="[cursor]")
    refused = [row for row in rows if row.harness == "cursor"]
    assert [row.device for row in refused] == ["box", "mac"]
    assert all(row.skip == "refused in orch.yml" and row.launch is None for row in refused)
    others = [row for row in rows if row.harness != "cursor"]
    assert len(others) == 6
    assert all(row.skip is None and row.launch is not None for row in others)


def test_canary_prompt_for_names_each_drivers_skills_dir_and_bot() -> None:
    for driver in all_drivers():
        prompt = canary_prompt_for(driver)
        assert f"{driver.skills_dir}/factory-builder/SKILL.md" in prompt
        assert f"started-by: {driver.bot_user}" in prompt


def test_score_reply_needs_the_exact_pinned_passage() -> None:
    token = pinned_token(ROOT)
    changed = token[:-1] + ("x" if token[-1] != "x" else "y")
    for driver in all_drivers():
        name, bot = driver.name, driver.bot_user

        def reply(started_by: str = bot, passage: str = token) -> str:
            return f"started-by: {started_by}\nskill: factory-builder\ntoken: {passage}\n"

        assert score_reply(name, bot, reply(), token) is None
        mismatch = f"{name}: did not load the pinned version (token does not match the pin)"
        assert score_reply(name, bot, reply(passage=changed), token) == mismatch
        assert score_reply(name, bot, reply(passage=token + " and more"), token) == mismatch
        assert score_reply(name, bot, reply(started_by="someone-else"), token) == (
            f"{name}: did not load the pinned version (not started by {bot})"
        )


def canary_machines(tmp_path: Path, text: str = CANARY_MACHINES):
    path = tmp_path / "machines.yml"
    path.write_text(text, encoding="utf-8")
    machines, findings = load_machines_yml(path)
    assert machines is not None, findings
    return machines


def asked_bot(stdin: bytes) -> str:
    [line] = [line for line in stdin.decode().splitlines() if line.startswith("started-by: ")]
    return line.removeprefix("started-by: ")


def plain_reply(bot: str, token: str) -> str:
    return f"started-by: {bot}\nskill: factory-builder\ntoken: {token}\n"


def echoed_prompt_then_reply(prompt: str, bot: str, token: str) -> str:
    return f"user\n{prompt}\ncodex\n{plain_reply(bot, token)}tokens used\n1234\n{plain_reply(bot, token)}"


def stream_json_reply(bot: str, token: str) -> str:
    text = plain_reply(bot, token)
    events = [
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
        {"type": "result", "subtype": "success", "result": text, "session_id": "s1"},
    ]
    return "".join(json.dumps(event) + "\n" for event in events)


def gemini_delta_reply(bot: str, token: str) -> str:
    chunks = [f"started-by: {bot}\nskill: factory-builder\ntoken: ", token[:20], token[20:], "\n"]
    events = [{"type": "init", "model": "gemini-3.1-pro"}]
    events += [{"type": "message", "role": "assistant", "content": chunk, "delta": True} for chunk in chunks]
    events += [{"type": "result", "status": "success", "stats": {}}]
    return "".join(json.dumps(event) + "\n" for event in events)


TIMEOUT = object()


class FakeSpawn:
    def __init__(self, token: str, replies=None) -> None:
        self.token = token
        self.replies = replies or {}
        self.calls: list[tuple[list[str], str]] = []

    def __call__(self, argv, *, stdin, log, cwd, env, timeout):
        bot = asked_bot(stdin)
        harness = next(d.name for d in all_drivers() if d.bot_user == bot)
        device = "mac" if argv[0] == "ssh" else "box"
        self.calls.append((list(argv), f"{harness}@{device}"))
        reply = self.replies.get(f"{harness}@{device}")
        if reply is TIMEOUT:
            return None
        if reply is None:
            reply = {
                "claude": stream_json_reply,
                "codex": lambda b, t: echoed_prompt_then_reply(stdin.decode(), b, t),
                "gemini": gemini_delta_reply,
            }.get(harness, plain_reply)(bot, self.token)
        log.write_text(reply, encoding="utf-8")
        return 0


def waiter_for(bot: str):
    return WAITERS[next(d.name for d in all_drivers() if d.bot_user == bot)]


def live(tmp_path: Path, run, *, refused=(), harnesses=None, load=waiter_for):
    lines: list[str] = []
    errors = live_canary(
        canary_machines(tmp_path), refused, pinned_token(ROOT), harnesses=harnesses, timeout=5,
        work=tmp_path / "work", run_id="doctor-0000", load=load, run=run, out=lines.append,
    )
    return lines, errors


EVERY_TARGET = [f"{d.name}@{device}" for device in ("box", "mac") for d in all_drivers()]


def test_live_doctor_passes_for_every_enabled_harness(tmp_path: Path) -> None:
    spawn = FakeSpawn(pinned_token(ROOT))
    lines, errors = live(tmp_path, spawn)
    assert errors == []
    assert lines == [f"{target}: ok" for target in EVERY_TARGET]
    assert [target for _argv, target in spawn.calls] == EVERY_TARGET
    for argv, target in spawn.calls:
        if target.endswith("@mac"):
            assert argv[argv.index("--") + 1].startswith("mac-")


def test_live_doctor_names_the_harness_with_a_wrong_token(tmp_path: Path) -> None:
    token = pinned_token(ROOT)
    spawn = FakeSpawn(token, {"codex@mac": plain_reply("codex-bot", token[:-1])})
    lines, errors = live(tmp_path, spawn)
    assert errors == ["codex@mac: did not load the pinned version (token does not match the pin)"]
    assert "codex@mac: ok" not in lines
    assert len(lines) == len(EVERY_TARGET) - 1


def test_live_doctor_reports_a_timeout(tmp_path: Path) -> None:
    spawn = FakeSpawn(pinned_token(ROOT), {"gemini@box": TIMEOUT})
    _lines, errors = live(tmp_path, spawn)
    assert errors == ["gemini@box: did not load the pinned version (timed out after 5s)"]


def test_live_doctor_skips_refused_harnesses(tmp_path: Path) -> None:
    spawn = FakeSpawn(pinned_token(ROOT))
    lines, errors = live(tmp_path, spawn, refused=("cursor",))
    assert errors == []
    assert "cursor@box: skipped (refused in orch.yml)" in lines
    assert "cursor@mac: skipped (refused in orch.yml)" in lines
    assert not any(target.startswith("cursor@") for _argv, target in spawn.calls)


def test_live_doctor_runs_only_the_named_harness(tmp_path: Path) -> None:
    spawn = FakeSpawn(pinned_token(ROOT))
    lines, errors = live(tmp_path, spawn, harnesses=("claude",))
    assert errors == []
    assert lines == ["claude@box: ok", "claude@mac: ok"]


def test_an_unreadable_waiter_config_names_the_per_bot_command(tmp_path: Path) -> None:
    def load(bot: str):
        if bot == "codex-bot":
            raise WaiterError("harness config /x/waiter-codex-bot.yml is not owned by this user")
        return waiter_for(bot)

    spawn = FakeSpawn(pinned_token(ROOT))
    _lines, errors = live(tmp_path, spawn, load=load)
    fix = "run bin/orch doctor --live --harness codex as codex-bot"
    assert errors == [
        f"codex@{device}: harness config /x/waiter-codex-bot.yml is not owned by this user; {fix}"
        for device in ("box", "mac")
    ]
    assert not any(target.startswith("codex@") for _argv, target in spawn.calls)


def test_last_reply_reads_plain_and_json_event_stdout() -> None:
    token = pinned_token(ROOT)
    for stdout in (
        plain_reply("claude-bot", token),
        stream_json_reply("claude-bot", token),
        gemini_delta_reply("claude-bot", token),
        echoed_prompt_then_reply("started-by: claude-bot\ntoken: <that passage>", "claude-bot", token),
    ):
        assert score_reply("claude", "claude-bot", last_reply(stdout), token) is None


def replying(bot: str) -> list[str]:
    reply = plain_reply(bot, pinned_token(ROOT))
    return [sys.executable, "-c", f"import sys; sys.stdin.read(); sys.stdout.write({reply!r})"]


def local_box(*harnesses: str) -> MachinesConfig:
    bots = {d.name: d.bot_user for d in all_drivers()}
    return MachinesConfig((Device("box", 2, {h: bots[h] for h in harnesses}, local=True),))


def test_a_harness_that_cannot_start_is_named_and_the_rest_still_run(tmp_path: Path) -> None:
    missing = tmp_path / "not-installed" / "claude"
    lines: list[str] = []
    errors = live_canary(
        local_box("claude", "codex"), (), pinned_token(ROOT), harnesses=None, timeout=10,
        work=tmp_path / "work", run_id="doctor-0000",
        load=lambda bot: [str(missing)] if bot == "claude-bot" else replying(bot),
        out=lines.append,
    )
    [error] = errors
    assert error.startswith("claude@box: did not load the pinned version (could not start: ")
    assert str(missing) in error
    assert lines == ["codex@box: ok"]


def gone(pid: int, within: float) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_a_harness_that_ignores_sigterm_is_killed_after_its_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(skill_probe, "STOP_GRACE", 0.5)
    marker = tmp_path / "pid"
    stubborn = (
        "import os, signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"open({str(marker)!r}, 'w').write(str(os.getpid())); sys.stdin.read(); time.sleep(60)"
    )
    lines: list[str] = []
    started = time.monotonic()
    errors = live_canary(
        local_box("claude", "codex"), (), pinned_token(ROOT), harnesses=None, timeout=1,
        work=tmp_path / "work", run_id="doctor-0000",
        load=lambda bot: [sys.executable, "-c", stubborn] if bot == "claude-bot" else replying(bot),
        out=lines.append,
    )
    assert time.monotonic() - started < 10
    assert errors == ["claude@box: did not load the pinned version (timed out after 1s)"]
    assert lines == ["codex@box: ok"]
    assert gone(int(marker.read_text()), 1)


def test_a_remote_harness_past_its_timeout_is_stopped_on_the_device(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(skill_probe, "REMOTE_STOP_PAUSE", 0.2)
    ssh = Ssh(tmp_path)
    use_fake_ssh(monkeypatch, ssh)
    marker = tmp_path / "pid"
    stubborn = (
        "import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"open({str(marker)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    )
    machines = MachinesConfig((Device(
        "remote", 1, {"claude": "claude-bot"}, workdir_root=str(tmp_path / "remote"),
        ssh_alias="fakepc-{bot}",
    ),))
    errors = live_canary(
        machines, (), pinned_token(ROOT), harnesses=("claude",), timeout=2,
        work=tmp_path / "work", run_id="doctor-0000",
        load=lambda bot: [sys.executable, "-c", stubborn], out=lambda line: None,
    )
    assert errors == ["claude@remote: did not load the pinned version (timed out after 2s)"]
    pid = int(marker.read_text())
    try:
        assert gone(pid, 1), "the remote harness still runs after the doctor returned"
    finally:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


LOCAL_CLAUDE = """\
devices:
  - name: box
    local: true
    slot_max: 1
    harnesses: {claude: claude-bot}
"""


def doctor_cli(tmp_path: Path, home: Path, monkeypatch, capsys, command: list[str], *extra: str):
    folder = home / ".config" / "factory"
    folder.mkdir(parents=True, mode=0o700)
    config = folder / "waiter-claude-bot.yml"
    config.write_text(json.dumps({"bot": "claude-bot", "command": command}), encoding="utf-8")
    config.chmod(0o600)
    machines = tmp_path / "machines.yml"
    machines.write_text(LOCAL_CLAUDE, encoding="utf-8")
    orch = tmp_path / "orch.yml"
    orch.write_text("poll_seconds: 30\nprojects:\n  - repo: acme/widgets\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    code = cli.main([
        "doctor", "--live", "--home", str(home), "--machines", str(machines), "--orch", str(orch),
        "--work", str(tmp_path / "work"), *extra,
    ])
    out, err = capsys.readouterr()
    return code, out, err


def test_orch_doctor_live_runs_the_waiter_command(home: Path, tmp_path: Path, monkeypatch, capsys) -> None:
    lines = plain_reply("claude-bot", pinned_token(ROOT)).splitlines()
    script = f"cat > /dev/null; printf '%s\\n' {shlex.join(lines)}"
    code, out, err = doctor_cli(tmp_path, home, monkeypatch, capsys, ["sh", "-c", script])
    assert (code, err) == (0, "")
    assert out == "claude@box: ok\nok\n"


def test_orch_doctor_live_stops_a_harness_past_its_timeout(
    home: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    marker = tmp_path / "child-alive"
    script = f"sleep 30 & echo $! > {shlex.quote(str(marker))}; wait"
    code, _out, err = doctor_cli(
        tmp_path, home, monkeypatch, capsys, ["sh", "-c", script], "--timeout", "1"
    )
    assert code == 1
    assert err == "claude@box: did not load the pinned version (timed out after 1s)\n"
    pid = int(marker.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the harness's background child survived the timeout")


@pytest.mark.parametrize("flag", (["--responses", "replies"], ["--print-canary"]))
def test_live_refuses_the_offline_flags(flag: list[str], capsys) -> None:
    assert cli.main(["doctor", "--live", *flag]) == 2
    assert "--live" in capsys.readouterr().err


@pytest.mark.parametrize("flag", (["--machines", "m.yml"], ["--harness", "claude"], ["--timeout", "5"]))
def test_live_flags_need_live(flag: list[str], capsys) -> None:
    assert cli.main(["doctor", *flag]) == 2
    assert "--live" in capsys.readouterr().err
