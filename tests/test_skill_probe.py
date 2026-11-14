"""bin/skill-probe (#119): transcript parsers, launch plan, credential guard, render."""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from lib import skill_probe as sp
from lib.harness_env import harness_env
from lib.machines_yml import load_machines_yml

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "skill_probe"
PINNED = ROOT / "skills" / "pstack" / "skills" / "poteto-mode" / "SKILL.md"
CANARY = sp.skill_canary(PINNED.read_text(encoding="utf-8"))
CLAUDE_CMD = ("claude", "-p", "--dangerously-skip-permissions")
CODEX_CMD = (
    "/home/box/.local/bin/codex", "exec", "--sandbox", "workspace-write",
    "--add-dir", "/home/box/.local/state/factory", "-",
)

MACHINES = """\
devices:
  - name: box
    local: true
    slot_max: 3
    harnesses: {claude: claude-bot, codex: codex-bot}
  - name: mac
    tailscale_name: mac.example.ts.net
    slot_max: 2
    harnesses: {claude: claude-bot, codex: codex-bot}
    workdir_root: /Users/{bot}/factory-work
    ssh_alias: mac-{bot}
  - name: pc
    enabled: false
    tailscale_name: pc.example.ts.net
    slot_max: 3
    harnesses: {claude: claude-bot, codex: codex-bot}
    workdir_root: /home/{bot}/factory-work
    ssh_alias: pc-{bot}
  - name: spare
    tailscale_name: spare.example.ts.net
    slot_max: 1
    harnesses: {claude: claude-bot}
    workdir_root: REPLACE_WITH_SPARE_WORKDIR
    ssh_alias: REPLACE_WITH_SPARE_ALIAS
"""


def evidence(harness: str, fixture: str) -> sp.Evidence:
    text = (FIXTURES / fixture).read_text(encoding="utf-8")
    return sp.PARSERS[harness](sp.parse_jsonl(text), CANARY)


def machines(tmp_path: Path, text: str = MACHINES):
    path = tmp_path / "machines.yml"
    path.write_text(text, encoding="utf-8")
    config, findings = load_machines_yml(path, allow_placeholders=True)
    assert config is not None, findings
    return config


def plan(tmp_path: Path, bot: str = "claude-bot", command=CLAUDE_CMD, **kwargs):
    return sp.plan_launches(
        machines(tmp_path), bot, command, run_id="skill-probe-0000",
        local_root=str(tmp_path / "work"), homes={"box": "/home/" + bot}, **kwargs,
    )


# Transcript parsers (criterion 4)


def test_canary_is_body_not_frontmatter():
    text = PINNED.read_text(encoding="utf-8")
    frontmatter = text.split("---")[1]
    assert CANARY not in frontmatter
    assert not CANARY.startswith("#")
    assert CANARY in text.split("---", 2)[2]
    assert sp.skill_canary("---\nname: x\ndescription: d\n---\n\n# X\n\nBody line.\n") == "Body line."
    with pytest.raises(sp.ProbeError):
        sp.skill_canary("---\nname: x\n---\n# Only a title\n")


def test_claude_read_tool_counts_as_read():
    found = evidence("claude", "claude_read_tool.jsonl")
    assert (found.read, found.read_via) == ("yes", "Read")
    assert found.harness_version == "2.1.322"


def test_claude_slash_expansion_counts_as_read():
    found = evidence("claude", "claude_slash.jsonl")
    assert (found.read, found.read_via) == ("yes", "injected")
    assert found.helper == "yes"  # a Task call with no type is general-purpose


def test_claude_claim_without_read_is_no():
    found = evidence("claude", "claude_claim_only.jsonl")
    assert (found.read, found.read_via) == ("no", None)
    assert (found.helper, found.poteto_agent) == ("no", "no")


def test_claude_refused_skill_tool_is_no():
    found = evidence("claude", "claude_skill_refused.jsonl")
    assert found.read == "no"  # an error result never counts, even holding the canary
    assert found.helper == "no"


def test_claude_general_purpose_helper_yes_poteto_agent_missing():
    found = evidence("claude", "claude_read_tool.jsonl")
    assert (found.helper, found.poteto_agent) == ("yes", "missing")


def test_claude_live_transcript_from_pc():
    found = evidence("claude", "claude_live_read_path.jsonl")
    assert found == sp.Evidence("yes", "Read", "yes", "missing", "2.1.322")


def test_codex_live_transcript_from_box():
    found = evidence("codex", "codex_live_run_as.jsonl")
    assert found == sp.Evidence("yes", "exec", "yes", "n/a", "0.162.1")


def test_codex_shell_read_counts_as_read():
    found = evidence("codex", "codex_shell_read.jsonl")
    assert (found.read, found.read_via) == ("yes", "shell")
    assert found.harness_version == "0.130.0"
    assert found.poteto_agent == "n/a"


def test_codex_injected_skill_counts_as_read():
    found = evidence("codex", "codex_injected.jsonl")
    assert (found.read, found.read_via, found.helper) == ("yes", "injected", "no")


def test_codex_claim_and_reasoning_are_not_reads():
    found = evidence("codex", "codex_claim_only.jsonl")
    assert (found.read, found.helper) == ("no", "no")


def test_codex_subagent_spawn_counts_as_helper():
    assert evidence("codex", "codex_shell_read.jsonl").helper == "yes"


def codex_spawn(output) -> list:
    meta = {"type": "session_meta", "payload": {"cwd": "/r", "cli_version": "0.130.0"}}
    call = {"type": "function_call", "name": "spawn_agent", "arguments": "{}", "call_id": "c"}
    result = {"type": "function_call_output", "call_id": "c", "output": output}
    return [meta, {"type": "response_item", "payload": call},
            {"type": "response_item", "payload": result}]


FAILED_SPAWN = {"content": "unsupported call: spawn_agent", "success": False}


@pytest.mark.parametrize("output", [
    FAILED_SPAWN,
    json.dumps(FAILED_SPAWN),
    "unsupported call: spawn_agent",
    json.dumps({"agent_id": "019a", "success": False}),
    json.dumps({"task_name": "/root/probe", "success": False}),
    json.dumps({"task_name": ""}),
    json.dumps({"status": "error"}),
    "",
    None,
], ids=["mapping", "serialized", "plain", "serialized-failed-id", "failed-task-name",
        "empty-task-name", "no-id", "empty", "none"])
def test_codex_spawn_that_names_no_agent_is_not_a_helper(output):
    assert sp.codex_evidence(codex_spawn(output), CANARY).helper == "no"


@pytest.mark.parametrize("output", [
    {"agent_id": "019a"},
    json.dumps({"agent_id": "019a"}),
    json.dumps({"task_name": "/root/probe"}),
], ids=["mapping", "serialized", "task-name"])
def test_codex_spawn_that_names_its_agent_is_a_helper(output):
    assert sp.codex_evidence(codex_spawn(output), CANARY).helper == "yes"


def test_codex_serialized_failure_scores_like_its_mapping_form():
    records = sp.parse_jsonl((FIXTURES / "codex_claim_only.jsonl").read_text(encoding="utf-8"))
    for record in records:
        payload = record.get("payload", {})
        if payload.get("type") == "function_call_output":
            payload["output"] = json.dumps(payload["output"])
    assert sp.codex_evidence(records, CANARY).helper == "no"


@pytest.mark.parametrize("encode", [lambda o: o, json.dumps], ids=["mapping", "serialized"])
def test_codex_failed_output_with_the_canary_is_not_a_read(encode):
    records = codex_spawn(encode({"content": CANARY, "success": False}))
    assert sp.codex_evidence(records, CANARY).read == "no"


def test_missing_transcript_is_unknown():
    for parser in sp.PARSERS.values():
        assert parser([], CANARY) == sp.UNKNOWN
    assert sp.parse_jsonl("not json\n\n[1]\n") == []


def test_fixtures_hold_no_secret_shapes():
    secret = re.compile(r"ghp_|gho_|sk-[A-Za-z0-9]|Bearer |BEGIN .* PRIVATE KEY")
    for path in FIXTURES.iterdir():
        text = path.read_text(encoding="utf-8")
        assert not secret.search(text), path.name
        for line in text.splitlines():
            json.loads(line)


# Prompt


@pytest.mark.parametrize("style", list(sp.Style))
def test_prompt_never_holds_the_canary(style):
    for harness in ("claude", "codex"):
        prompt = sp.prompt_for(style, harness, "/x/poteto-mode/SKILL.md", CANARY)
        assert CANARY not in prompt
        assert prompt.startswith(sp.style_spec(style).opening("/x/poteto-mode/SKILL.md"))
    with pytest.raises(sp.ProbeError):
        sp.prompt_for(sp.Style.READ_PATH, "claude", CANARY, CANARY)


def test_styles_apply_per_harness():
    assert [s.style for s in sp.STYLES if s.harnesses >= {"claude"}] == [
        sp.Style.RUN_AS, sp.Style.READ_PATH, sp.Style.SLASH,
    ]
    assert [s.style for s in sp.STYLES if s.harnesses >= {"codex"}] == [
        sp.Style.RUN_AS, sp.Style.READ_PATH, sp.Style.CODEX_MENTION,
    ]


# Launch plan (criteria 1 and 3)


def test_plan_launches_covers_local_and_remote_devices(tmp_path):
    claude = plan(tmp_path)
    assert [(l.device, l.style.value) for l in claude] == [
        ("box", "run-as"), ("box", "read-path"), ("box", "slash"),
        ("mac", "run-as"), ("mac", "read-path"), ("mac", "slash"),
    ]
    codex = plan(tmp_path, "codex-bot", CODEX_CMD)
    assert [(l.device, l.style.value) for l in codex] == [
        ("box", "run-as"), ("box", "read-path"), ("box", "codex-skill"),
        ("mac", "run-as"), ("mac", "read-path"), ("mac", "codex-skill"),
    ]
    assert len({l.run_dir for l in claude + codex}) == 12
    assert claude[1].skill_md == "/home/claude-bot/.claude/skills/poteto-mode/SKILL.md"
    assert codex[1].skill_md == "/home/codex-bot/.agents/skills/poteto-mode/SKILL.md"
    only = plan(tmp_path, only_device="mac", skill_md="/opt/pstack/poteto-mode/SKILL.md")
    assert {l.device for l in only} == {"mac"}
    assert {l.skill_md for l in only} == {"/opt/pstack/poteto-mode/SKILL.md"}


def test_disabled_or_placeholder_device_is_skipped(tmp_path):
    devices = {l.device for l in plan(tmp_path)}
    assert "pc" not in devices and "spare" not in devices
    shipped, _ = load_machines_yml(ROOT / "machines.yml", allow_placeholders=True)
    launches = sp.plan_launches(
        shipped, "claude-bot", CLAUDE_CMD, run_id="skill-probe-0000",
        local_root=str(tmp_path), homes={},
    )
    assert {l.device for l in launches} == {"box"}


def test_local_launch_argv_is_the_waiter_command_unchanged(tmp_path):
    for bot, command in (("claude-bot", CLAUDE_CMD), ("codex-bot", CODEX_CMD)):
        for launch in plan(tmp_path, bot, command):
            if launch.remote is None:
                assert sp.launch_argv(launch) == list(command)
                assert dict(launch.env) == harness_env(launch.harness)


def test_remote_launch_uses_start_script_and_bot_alias(tmp_path):
    launch = next(l for l in plan(tmp_path, "codex-bot", CODEX_CMD) if l.remote)
    argv = sp.launch_argv(launch)
    assert argv[0] == "ssh"
    assert argv[argv.index("-l") + 1] == "codex-bot"
    assert argv[argv.index("--") + 1] == "mac-codex-bot"
    script = argv[-1]
    assert "ORCH_WORKER_TAG=" in script and "bash -lc" in script
    assert "/home/box/.local/bin/codex" not in script  # for_device: bare name on the device
    assert launch.argv[0] == "codex"
    assert launch.guarded_dirs == ("/home/box/.local/state/factory",)
    assert launch.run_dir == "/Users/codex-bot/factory-work/jobs/skill-probe-0000-codex-run-as"


def test_prompt_goes_on_stdin_only(tmp_path):
    seen = {}

    def run(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        return 0

    launch = plan(tmp_path)[0]
    prompt = sp.prompt_for(launch.style, launch.harness, launch.skill_md, CANARY)
    text, note = sp.run_launch(
        launch, prompt, timeout=5, log_path=tmp_path / "log", run=run,
        claude_dir=tmp_path / "claude",
    )
    assert seen["stdin"] == prompt.encode()
    assert not any("probe" in part for part in seen["argv"])
    assert seen["cwd"] == launch.run_dir
    assert seen["env"][sp.harness_env("claude").popitem()[0]] == "0"
    assert (text, note) == ("", "no transcript")


def test_remote_run_fetches_the_transcript_over_the_bot_alias(tmp_path):
    launch = next(l for l in plan(tmp_path) if l.remote)
    fetched = []

    def fetch(argv):
        fetched.append(argv)
        return subprocess.CompletedProcess(argv, 0, b'{"type":"user"}\n', b"")

    text, note = sp.run_launch(
        launch, "p", timeout=5, log_path=tmp_path / "log", run=lambda argv, **_: None,
        fetch=fetch,
    )
    assert text == '{"type":"user"}\n'
    assert note == "timed out after 5s"
    (argv,) = fetched
    assert argv[argv.index("-l") + 1] == "claude-bot"
    assert argv[argv.index("--") + 1] == "mac-claude-bot"
    assert sp.claude_slug(launch.run_dir) in argv[-1]


def test_spawn_stops_the_whole_group_on_timeout(tmp_path):
    marker = tmp_path / "child-alive"
    script = f"sleep 30 & echo $! > {shlex.quote(str(marker))}; wait"
    code = sp.spawn(
        ["sh", "-c", script], stdin=b"", log=tmp_path / "log", cwd=str(tmp_path),
        env=os.environ, timeout=1,
    )
    assert code is None
    pid = int(marker.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        subprocess.run(["sleep", "0.1"], check=False)
    else:
        pytest.fail("the harness's background child survived the timeout")


# Transcript locators (criterion 3)


def test_transcript_locator_refuses_paths_outside_the_transcript_dirs(tmp_path):
    outside = tmp_path / "outside.jsonl"
    outside.write_text('{"type":"session_meta","payload":{"cwd":"/r"}}\n')
    claude = tmp_path / "claude"
    folder = claude / "projects" / sp.claude_slug("/r")
    folder.mkdir(parents=True)
    (folder / "evil.jsonl").symlink_to(outside)
    assert sp.claude_transcript(claude, "/r") is None
    (folder / "good.jsonl").write_text("{}\n")
    assert sp.claude_transcript(claude, "/r") == folder / "good.jsonl"
    other = tmp_path / "elsewhere"
    other.mkdir()
    (claude / "projects" / sp.claude_slug("/s")).symlink_to(other)
    (other / "x.jsonl").write_text("{}\n")
    assert sp.claude_transcript(claude, "/s") is None

    codex = tmp_path / "codex"
    day = codex / "sessions" / "2026" / "11" / "13"
    day.mkdir(parents=True)
    (day / "rollout-evil.jsonl").symlink_to(outside)
    assert sp.codex_transcript(codex, "/r", since=0) is None
    (day / "rollout-a.jsonl").write_text('{"type":"session_meta","payload":{"cwd":"/q"}}\n')
    (day / "rollout-b.jsonl").write_text('{"type":"session_meta","payload":{"cwd":"/r"}}\n')
    assert sp.codex_transcript(codex, "/r", since=0) == day / "rollout-b.jsonl"
    assert sp.codex_transcript(codex, "/r", since=4e9) is None


def write_parent_and_newer_subagent_rollout(day: Path, run_dir: str) -> str:
    parent = json.dumps({"type": "session_meta", "payload": {
        "id": "p", "cwd": run_dir, "source": "exec", "thread_source": "user"}}) + "\n"
    child = json.dumps({"type": "session_meta", "payload": {
        "id": "c", "parent_thread_id": "p", "cwd": run_dir, "thread_source": "subagent",
        "source": {"subagent": {"thread_spawn": {"parent_thread_id": "p"}}}}}) + "\n"
    day.mkdir(parents=True)
    (day / "rollout-parent.jsonl").write_text(parent)
    (day / "rollout-sub.jsonl").write_text(child)
    os.utime(day / "rollout-parent.jsonl", (1000, 1000))
    os.utime(day / "rollout-sub.jsonl", (2000, 2000))
    os.utime(run_dir, (1, 1))
    return parent


def test_codex_locators_take_the_parent_rollout_not_a_subagents(tmp_path):
    home = tmp_path / "home"
    run_dir = str(tmp_path / "jobs" / "skill-probe-0000-codex-run-as")
    Path(run_dir).mkdir(parents=True)
    parent = write_parent_and_newer_subagent_rollout(
        home / ".codex" / "sessions" / "2026" / "11" / "14", run_dir)
    assert local_fetch("codex", home, run_dir) == parent
    assert remote_fetch("codex", home, run_dir).stdout == parent


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_fetch_script_cats_only_the_runs_transcript(tmp_path, harness):
    home = tmp_path / "home"
    run_dir = str(tmp_path / "jobs" / "skill-probe-0000-run-as")
    Path(run_dir).mkdir(parents=True)
    if harness == "claude":
        folder = home / ".claude" / "projects" / sp.claude_slug(run_dir)
        folder.mkdir(parents=True)
        (folder / "s.jsonl").write_text('{"type":"user"}\n')
        want = '{"type":"user"}\n'
    else:
        day = home / ".codex" / "sessions" / "2026" / "11" / "13"
        day.mkdir(parents=True)
        (day / "rollout-other.jsonl").write_text(
            json.dumps({"type": "session_meta", "payload": {"cwd": "/elsewhere"}}) + "\n"
        )
        want = json.dumps({"type": "session_meta", "payload": {"cwd": run_dir}}) + "\n"
        (day / "rollout-mine.jsonl").write_text(want)
        os.utime(run_dir, (1, 1))
    env = {"HOME": str(home), "PATH": os.environ["PATH"]}
    done = subprocess.run(
        ["sh", "-c", sp.fetch_script(harness, run_dir)], env=env, capture_output=True,
        text=True, check=False,
    )
    assert (done.returncode, done.stdout) == (0, want)
    empty = subprocess.run(
        ["sh", "-c", sp.fetch_script(harness, run_dir + "-x")], env=env, capture_output=True,
        check=False,
    )
    assert empty.returncode == 3 and empty.stdout == b""


def remote_fetch(harness: str, home: Path, run_dir: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", "-c", sp.fetch_script(harness, run_dir)],
        env={"HOME": str(home), "PATH": os.environ["PATH"]},
        capture_output=True, text=True, check=False,
    )


def local_fetch(harness: str, home: Path, run_dir: str) -> str | None:
    if harness == "claude":
        path = sp.claude_transcript(home / ".claude", run_dir)
    else:
        path = sp.codex_transcript(home / ".codex", run_dir, since=0)
    return None if path is None else path.read_text()


def escape_file_link(harness, home, run_dir, outside):
    if harness == "claude":
        folder = home / ".claude" / "projects" / sp.claude_slug(run_dir)
        folder.mkdir(parents=True)
        (folder / "session.jsonl").symlink_to(outside)
    else:
        day = home / ".codex" / "sessions" / "2026" / "11" / "13"
        day.mkdir(parents=True)
        (day / "rollout-evil.jsonl").symlink_to(outside)


def escape_dir_link(harness, home, run_dir, outside):
    elsewhere = outside.parent / "elsewhere"
    elsewhere.mkdir()
    if harness == "claude":
        (home / ".claude" / "projects").mkdir(parents=True)
        (home / ".claude" / "projects" / sp.claude_slug(run_dir)).symlink_to(elsewhere)
        (elsewhere / "session.jsonl").symlink_to(outside)
        (elsewhere / "plain.jsonl").write_text(outside.read_text())
    else:
        (home / ".codex" / "sessions").mkdir(parents=True)
        (home / ".codex" / "sessions" / "2026").symlink_to(elsewhere)
        (elsewhere / "rollout-plain.jsonl").write_text(outside.read_text())


def escape_root_link(harness, home, run_dir, outside):
    elsewhere = outside.parent / "elsewhere"
    elsewhere.mkdir()
    if harness == "claude":
        (elsewhere / sp.claude_slug(run_dir)).mkdir()
        (elsewhere / sp.claude_slug(run_dir) / "session.jsonl").write_text(outside.read_text())
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "projects").symlink_to(elsewhere)
    else:
        (elsewhere / "rollout-plain.jsonl").write_text(outside.read_text())
        (home / ".codex").mkdir(parents=True)
        (home / ".codex" / "sessions").symlink_to(elsewhere)


@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize("escape", [escape_file_link, escape_dir_link],
                         ids=["file-link", "dir-link"])
def test_remote_fetch_refuses_links_out_of_the_transcript_dir(tmp_path, harness, escape):
    home = tmp_path / "home"
    run_dir = str(tmp_path / "jobs" / "run")
    Path(run_dir).mkdir(parents=True)
    os.utime(run_dir, (1, 1))
    outside = tmp_path / "marker.txt"
    outside.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": run_dir}})
                       + "\nOUTSIDE_TRANSCRIPT_ROOT\n")
    escape(harness, home, run_dir, outside)
    assert local_fetch(harness, home, run_dir) is None
    done = remote_fetch(harness, home, run_dir)
    assert (done.returncode, done.stdout) == (3, "")


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_remote_fetch_matches_the_local_locator_through_a_linked_root(tmp_path, harness):
    home = tmp_path / "home"
    run_dir = str(tmp_path / "jobs" / "run")
    Path(run_dir).mkdir(parents=True)
    os.utime(run_dir, (1, 1))
    outside = tmp_path / "transcript.jsonl"
    outside.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": run_dir}}) + "\n")
    escape_root_link(harness, home, run_dir, outside)
    want = local_fetch(harness, home, run_dir)
    assert want == outside.read_text()
    assert remote_fetch(harness, home, run_dir).stdout == want


def test_remote_claude_fetch_skips_a_newer_link_for_the_real_session(tmp_path):
    home = tmp_path / "home"
    run_dir = str(tmp_path / "jobs" / "run")
    folder = home / ".claude" / "projects" / sp.claude_slug(run_dir)
    folder.mkdir(parents=True)
    (folder / "real.jsonl").write_text('{"type":"user"}\n')
    os.utime(folder / "real.jsonl", (1, 1))
    outside = tmp_path / "marker.txt"
    outside.write_text("OUTSIDE_TRANSCRIPT_ROOT\n")
    (folder / "newer.jsonl").symlink_to(outside)
    assert local_fetch("claude", home, run_dir) == '{"type":"user"}\n'
    assert remote_fetch("claude", home, run_dir).stdout == '{"type":"user"}\n'


# End to end through the CLI, and the credential guard (criterion 3)

FAKE_HARNESS = """\
#!/bin/sh
cat > stdin.txt
slug=$(printf %s "$PWD" | sed 's/[^A-Za-z0-9]/-/g')
mkdir -p "$CLAUDE_CONFIG_DIR/projects/$slug"
cp "$FAKE_TRANSCRIPT" "$CLAUDE_CONFIG_DIR/projects/$slug/s.jsonl"
"""

_OPENED: list[str] | None = None


def _audit(event: str, args: tuple) -> None:
    if _OPENED is not None and event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        _OPENED.append(os.fsdecode(args[0]))


sys.addaudithook(_audit)

CREDENTIAL = re.compile(
    r"auth\.json|\.credentials|(^|/)\.env$|\.runner|(^|/)id_[a-z0-9]+|FACTORY_WEBHOOK|"
    r"(^|/)\.ssh/|(^|/)hosts\.yml$"
)


def _waiter_config(tmp_path: Path, command: list[str]) -> Path:
    folder = tmp_path / "cfg"
    folder.mkdir(mode=0o700)
    path = folder / "waiter-claude-bot.yml"
    path.write_text(json.dumps({"bot": "claude-bot", "command": command}))
    path.chmod(0o600)
    return path


def test_probe_run_opens_no_credential_path(tmp_path, monkeypatch, capsys):
    global _OPENED
    harness = tmp_path / "fake-claude"
    harness.write_text(FAKE_HARNESS)
    harness.chmod(harness.stat().st_mode | stat.S_IXUSR)
    machines_path = tmp_path / "machines.yml"
    machines_path.write_text(MACHINES.split("  - name: mac")[0])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    monkeypatch.setenv("FAKE_TRANSCRIPT", str(FIXTURES / "claude_read_tool.jsonl"))
    config = _waiter_config(tmp_path, [str(harness), "-p"])
    out = tmp_path / "out"
    base = ["run", "--bot", "claude-bot", "--machines", str(machines_path),
            "--config", str(config), "--work", str(tmp_path / "work")]
    _OPENED = []
    try:
        assert sp.main([*base, "--dry-run"]) == 0
        dry = capsys.readouterr().out
        assert sp.main([*base, "--out", str(out)]) == 0
    finally:
        opened, _OPENED = _OPENED, None
    assert opened
    assert not [path for path in opened if CREDENTIAL.search(path)]
    assert shlex.join([str(harness), "-p"]) in dry
    results = sorted(out.glob("*.json"))
    assert [p.name for p in results] == [
        "box-claude-read-path.json", "box-claude-run-as.json", "box-claude-slash.json",
    ]
    result = sp.ProbeResult.from_json(results[0].read_text())
    assert (result.evidence.read, result.evidence.helper) == ("yes", "yes")
    (stdin,) = (tmp_path / "work").glob("*-read-path/stdin.txt")
    assert stdin.read_text().startswith("Read ")
    assert (out / "box-claude-read-path.transcript.jsonl").exists()


def test_probe_run_requires_out_and_an_enabled_device(tmp_path, capsys):
    machines_path = tmp_path / "machines.yml"
    machines_path.write_text(MACHINES.split("  - name: mac")[0])
    config = _waiter_config(tmp_path, ["claude", "-p"])
    base = ["run", "--bot", "claude-bot", "--machines", str(machines_path), "--config", str(config)]
    assert sp.main(base) == 1
    assert "--out is required" in capsys.readouterr().err
    assert sp.main([*base, "--device", "mac", "--dry-run"]) == 1
    assert "no enabled device" in capsys.readouterr().err
    assert sp.main([*base, "--dry-run", "--skill-md", "relative/SKILL.md"]) == 1
    capsys.readouterr()
    shipped = ["run", "--bot", "claude-bot", "--machines", str(ROOT / "machines.yml"),
               "--config", str(config), "--dry-run"]
    assert sp.main(shipped) == 0
    headers = [line.split()[1] for line in capsys.readouterr().out.splitlines() if line.startswith("# ")]
    assert headers == ["box-claude-run-as", "box-claude-read-path", "box-claude-slash"]


# Render (criterion 2)


def _result(device, harness, style, read="yes", helper="yes", version="2.1.322"):
    return sp.ProbeResult(
        "2026-11-13", device, harness, style,
        sp.Evidence(read, "Read" if read == "yes" else None, helper,
                    "missing" if harness == "claude" else "n/a", version),
    )


def test_render_has_one_row_per_device_harness_style():
    results = [
        _result(device, harness, spec.style)
        for device in ("box", "mac")
        for harness in ("claude", "codex")
        for spec in sp.STYLES
        if harness in spec.harnesses
    ]
    table = sp.render(results).splitlines()
    rows = table[2:]
    assert len(rows) == 2 * 2 * len(sp.STYLES)
    assert sum("| n/a |" in row and "style does not apply" in row for row in rows) == 4
    assert table[0].startswith("| Date | Device | Harness | Version | Style | Read")
    assert "| Helper |" in table[0]


def test_render_shows_date_and_harness_version():
    table = sp.render([_result("box", "codex", sp.Style.RUN_AS, "no", "no", "0.130.0")])
    row = next(line for line in table.splitlines() if "run-as" in line)
    assert row.startswith("| 2026-11-13 | box | codex | 0.130.0 | run-as | no |")


def test_render_cli_reads_result_files(tmp_path, capsys):
    path = tmp_path / "r.json"
    path.write_text(_result("box", "claude", sp.Style.SLASH).to_json())
    assert sp.main(["render", str(path)]) == 0
    assert "| box | claude | 2.1.322 | slash | yes | Read | yes | missing |" in capsys.readouterr().out
