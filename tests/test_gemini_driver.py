from __future__ import annotations

import io
import json
import re
import sqlite3
from pathlib import Path
from typing import get_args

import pytest

from lib import (
    account_doctor,
    account_home,
    factory_md,
    harness_env,
    harnesses,
    lane_limit,
    orch_yml,
    pstack_models,
    pstack_pin,
    queue,
    schema,
    telemetry,
    usage_readings,
    wait,
    waiter,
)
from lib.brief import render_brief
from lib.harnesses import AccountIsolation, HarnessDriver, StreamFlags
from lib.harnesses.gemini import AUTH_OVERRIDES, GeminiDriver
from lib.machines_yml import load_machines_yml
from tests.test_brief import job

NOW = 1_800_000_000.0


def test_gemini_is_first_class_in_schema_wait_queue_and_registry() -> None:
    assert "gemini" in schema.HARNESSES
    assert wait.OWNING_BOTS["gemini-bot"] == "gemini"
    assert "gemini" in queue.RECIPIENTS
    assert "gemini-bot" in orch_yml.APP_BOTS
    assert ".gemini/skills" in pstack_pin.SKILL_DIRS
    assert pstack_pin.DIR_HARNESS[".gemini/skills"] == "gemini"
    assert "gemini" in harnesses.registered_names()

    driver = harnesses.get("gemini")
    assert isinstance(driver, GeminiDriver)
    assert isinstance(driver, HarnessDriver)
    assert driver.bot_user == "gemini-bot"
    assert driver.skills_dir == ".gemini/skills"
    assert driver.family_for_model("gemini-3.1-pro") == "google"
    assert driver.family_for_model(None) == "google"
    assert harnesses.driver_for_bot("gemini-bot") is driver
    assert driver.account_isolation() == AccountIsolation("GEMINI_CLI_HOME", AUTH_OVERRIDES)
    assert driver.probe_usage({}, NOW) == []
    assert usage_readings.NOT_TRACKED["gemini"]


UPSTREAM_AUTH_OVERRIDES = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_CLOUD_ACCESS_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_GENAI_USE_GCA",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GEMINI_CLI_USE_COMPUTE_ADC",
    "CLOUD_SHELL",
)


def test_an_account_job_drops_every_gemini_auth_override_and_signs_in_from_its_folder() -> None:
    home = "/home/gemini-bot/.factory-accounts/gemini-pro"
    secrets = {name: f"SYNTHETIC-{index}-{name}" for index, name in enumerate(AUTH_OVERRIDES)}
    scrubbed = harness_env.account_environ({"PATH": "/usr/bin", "KEEP": "ok", **secrets}, "gemini", home)

    assert set(UPSTREAM_AUTH_OVERRIDES) <= set(AUTH_OVERRIDES)
    assert scrubbed == {"PATH": "/usr/bin", "KEEP": "ok", "GEMINI_CLI_HOME": home}
    assert not [value for value in secrets.values() if value in repr(scrubbed)]


@pytest.mark.parametrize("path", ["README.md", "docs/SPEC.md", "bots/gemini-bot/SKILL.md"])
def test_the_docs_name_every_gemini_auth_override_the_job_drops(path: str) -> None:
    text = (pstack_pin.ROOT / path).read_text(encoding="utf-8")
    assert [name for name in AUTH_OVERRIDES if f"`{name}`" not in text] == []


def _gemini_session(tmp_path: Path) -> tuple[telemetry.Session, io.BytesIO]:
    out = io.BytesIO()
    clock = iter(range(2000, 100000))
    session = telemetry.Session(
        connect=lambda: sqlite3.connect(tmp_path / "orch.db"),
        job_id="job-gem",
        bot="gemini-bot",
        repo="acme/widgets",
        harness="gemini",
        flags=harnesses.get("gemini").stream_flags(["gemini", "-p", "hi"]),
        raw_log=None,
        scrub=telemetry.Scrubber({}),
        log=lambda message: None,
        stdout=out,
        clock=lambda: float(next(clock)),
    )
    return session, out


def _assistant(text: str) -> dict[str, object]:
    return {"type": "message", "role": "assistant", "content": text, "delta": True}


def _stream(*events: dict[str, object]) -> io.BytesIO:
    return io.BytesIO(b"".join(json.dumps(event).encode() + b"\n" for event in events))


def test_a_handoff_marker_split_across_streamed_deltas_reaches_stdout(tmp_path: Path) -> None:
    session, out = _gemini_session(tmp_path)
    session.consume(_stream(
        {"type": "init", "model": "gemini-3.1-pro"},
        _assistant("ORCH-HANDOFF: 1"),
        {"type": "tool_use", "tool_name": "run_shell_command", "tool_id": "t1", "parameters": {"command": "ls"}},
        {"type": "tool_result", "tool_id": "t1", "status": "success", "output": "x"},
        _assistant("Done.\nORCH-"),
        _assistant("HANDOFF: "),
        _assistant("4242424242\n"),
        {"type": "result", "status": "success", "stats": {}},
    ))
    session.finish(0)

    assert out.getvalue() == b"ORCH-HANDOFF: 4242424242\n"
    assert waiter.handoff_marker(out.getvalue().decode()) == "4242424242"


@pytest.mark.parametrize(
    ("argv", "bot_harness", "expected"),
    [
        (
            ["gemini", "-p", "hello"],
            "gemini",
            StreamFlags(["gemini", "--output-format", "stream-json", "-p", "hello"], "gemini", True, "gemini"),
        ),
        (
            ["/usr/local/bin/gemini", "--prompt=hi"],
            None,
            StreamFlags(
                ["/usr/local/bin/gemini", "--output-format", "stream-json", "--prompt=hi"],
                "gemini",
                True,
                "gemini",
            ),
        ),
        (
            ["gemini", "-p", "--output-format", "stream-json", "hi"],
            "gemini",
            StreamFlags(
                ["gemini", "-p", "--output-format", "stream-json", "hi"],
                "gemini",
                False,
                "output format already set",
            ),
        ),
        (
            ["gemini", "models", "list"],
            "gemini",
            StreamFlags(["gemini", "models", "list"], "gemini", False, "not print mode"),
        ),
        (
            ["/srv/bin/wrap-gemini", "-p", ""],
            "gemini",
            StreamFlags(
                ["/srv/bin/wrap-gemini", "--output-format", "stream-json", "-p", ""],
                "gemini",
                True,
                "gemini wrapper",
            ),
        ),
    ],
)
def test_gemini_stream_flags(argv: list[str], bot_harness: str | None, expected: StreamFlags) -> None:
    assert telemetry.stream_flags(argv, bot_harness) == expected


def test_gemini_summary_parses_stream_events_and_handoff() -> None:
    summary = telemetry.Summary("gemini", lambda value, limit=40: str(value))
    summary.feed({"type": "init", "model": "gemini-3.1-pro"}, NOW)
    summary.feed({"type": "message", "role": "assistant", "content": "working"}, NOW + 1)
    summary.feed(
        {
            "type": "tool_use",
            "tool_id": "g1",
            "tool_name": "run_shell_command",
            "parameters": {"command": "pytest tests/test_a.py"},
        },
        NOW + 2,
    )
    summary.feed(
        {"type": "tool_result", "tool_id": "g1", "status": "success", "output": "5 passed in 0.4s"},
        NOW + 3,
    )
    summary.feed(
        {
            "type": "tool_use",
            "tool_id": "g2",
            "tool_name": "write_file",
            "parameters": {"file_path": "lib/harnesses/gemini.py"},
        },
        NOW + 4,
    )
    summary.feed({"type": "tool_result", "tool_id": "g2", "status": "success", "output": ""}, NOW + 5)
    result_event = {
        "type": "result",
        "status": "success",
        "stats": {"input_tokens": 500, "output_tokens": 120, "cached": 100},
        "response": 'Done.\nORCH-HANDOFF: {"kind": "ready"}',
    }
    summary.feed(result_event, NOW + 6)

    assert summary.model == "gemini-3.1-pro"
    assert summary.turns == 1
    assert summary.commands == 1
    assert summary.test_runs == 1
    assert (summary.tests_passed, summary.tests_failed) == (5, 0)
    assert summary.files_touched == 1
    assert (summary.tokens_in, summary.tokens_cached, summary.tokens_out) == (500, 100, 120)
    assert summary.cli_reason == "success"
    assert telemetry.final_message(result_event) == result_event["response"]


def test_google_family_cross_family_review_and_contradiction_rules(tmp_path: Path) -> None:
    def check_md(builder: str, reviewer: str, b_model: str = "", r_model: str = "") -> list[str]:
        bm = f"\n  model: {b_model}" if b_model else ""
        rm = f"\n  model: {r_model}" if r_model else ""
        p = tmp_path / "factory.md"
        p.write_text(
            f"""---
base_branch: main
tickets: github
builder:
  harness: {builder}{bm}
reviewer:
  harness: {reviewer}{rm}
review:
  rounds:
    low: 1
    high: 3
  verify: none
ship: merge
skills: []
---
""",
            encoding="utf-8",
        )
        _, findings = factory_md.load_factory_md(p)
        return [f.message for f in findings if f.severity == "error"]

    assert any("same-family reviewer" in m for m in check_md("gemini", "gemini"))
    assert any("same-family reviewer" in m for m in check_md("cursor", "gemini", b_model="gemini-3.1-pro"))
    assert check_md("gemini", "claude") == []
    assert check_md("gemini", "codex") == []
    assert check_md("claude", "gemini") == []
    assert check_md("codex", "gemini") == []
    assert any("contradicts gemini harness" in m for m in check_md("gemini", "codex", b_model="claude-opus-4-6"))
    assert "gemini" in pstack_models.load_map()


def test_gemini_not_tracked_reason_surfaces_in_efficiency_and_settings(tmp_path: Path) -> None:
    import sqlite3
    from lib import dashboard_accounts, dashboard_usage
    from lib.dashboard_pages import efficiency, settings
    from lib.machines_yml import Account, Device, MachinesConfig

    config = MachinesConfig(
        (Device("pc", 1, {"gemini": "gemini-bot"}, home="/home/gemini-bot"),),
        accounts=(Account("gemini-ultra", "gemini", plan="ultra", signed_in_on=frozenset({"pc"})),),
    )
    with sqlite3.connect(":memory:") as db:
        db.execute(usage_readings.DDL)
        usage = dashboard_usage.read_usage(db, NOW, config)
        entry = next(e for e in usage["accounts"] if e["account"] == "gemini-ultra")
        assert entry["state"] == "not tracked"
        assert entry["not_tracked"] == usage_readings.NOT_TRACKED["gemini"]
        html = efficiency._headroom({"usage": usage, "generated_at_epoch": NOW})
        assert usage_readings.NOT_TRACKED["gemini"] in html

        accounts = dashboard_accounts.read_accounts(db, NOW, config)
        acct = next(a for a in accounts if a["id"] == "gemini-ultra")
        assert acct["plan"] == "ultra"
        assert acct["not_tracked"] == usage_readings.NOT_TRACKED["gemini"]
        facts = dict((k, v) for k, v, _ in settings.account_facts(acct))
        assert facts["plan"] == "ultra"
        assert usage_readings.NOT_TRACKED["gemini"] in facts["usage"]


GEMINI_STATUS = Path(__file__).parent / "fixtures" / "gemini_status"
GEMINI_REPLIES = {"gemini-default": "signed-in", "gemini-b": "needs-login", "gemini-c": "no-auth-method"}
FAKE_GEMINI = """\
#!/bin/sh
printf '%s\\n' "$(env | cut -d= -f1 | tr '\\n' ' ')" >> {log}
folder=${{GEMINI_CLI_HOME:-{home}/.factory-accounts/gemini-default}}
reply={replies}/$(basename "$folder")
if [ -n "{token_test}" ]; then reply={replies}/token; fi
cat "$reply.out" >&2
exit "$(cat "$reply.code")"
"""


def gemini_status(name: str) -> tuple[int, bytes]:
    return int((GEMINI_STATUS / f"{name}.code").read_text()), (GEMINI_STATUS / f"{name}.out").read_bytes()


@pytest.mark.parametrize("name, logged_in, detail", [
    ("signed-in", True, ""),
    ("needs-login", False, account_doctor.NOT_LOGGED_IN),
    ("no-auth-method", False, account_doctor.NOT_LOGGED_IN),
])
def test_the_gemini_status_probe_reads_each_recorded_reply(name: str, logged_in: bool, detail: str) -> None:
    probe = account_doctor.parse_gemini("gemini-b", "pc", *gemini_status(name))
    assert (probe.reached, probe.logged_in, probe.detail) == (True, logged_in, detail)


@pytest.mark.parametrize("code, reply", [
    (0, b"Hello! How can I help you today?\n"),
    (42, b"Unknown argument: auth\n"),
    (1, gemini_status("signed-in")[1]),
])
def test_a_gemini_model_reply_or_other_failure_never_reads_signed_in(code: int, reply: bytes) -> None:
    probe = account_doctor.parse_gemini("gemini-b", "pc", code, reply)
    assert (probe.logged_in, probe.detail) == (False, account_doctor.NOT_UNDERSTOOD)


def test_the_doctor_probes_gemini_accounts_from_their_folders_with_every_override_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lib import cli
    from tests.test_remote_start import Ssh

    ssh = Ssh(tmp_path)
    for device in ("pc", "lap"):
        (ssh.hosts / f"{device}-gemini-bot").write_text("gemini-bot", encoding="utf-8")
    ssh.offline("lap-gemini-bot").touch()
    home = tmp_path / "device-home"
    home.mkdir()
    replies = tmp_path / "replies"
    replies.mkdir()
    for account, name in {**GEMINI_REPLIES, "token": "signed-in"}.items():
        code, reply = gemini_status(name)
        (replies / f"{account}.out").write_bytes(reply)
        (replies / f"{account}.code").write_text(str(code), encoding="utf-8")
    bin_dir = tmp_path / "device-bin"
    bin_dir.mkdir()
    env_log = tmp_path / "gemini-env.log"
    fake = bin_dir / "gemini"
    fake.write_text(FAKE_GEMINI.format(
        log=env_log, home=home, replies=replies, token_test="".join(f"${name}" for name in AUTH_OVERRIDES),
    ), encoding="utf-8")
    fake.chmod(0o755)
    planted = {name: f"SYNTHETIC-{index}" for index, name in enumerate(AUTH_OVERRIDES)}
    (home / ".bash_profile").write_text(
        f"export PATH={bin_dir}:$PATH\nexport GEMINI_CLI_HOME={home}/elsewhere\n"
        + "".join(f"export {name}={value}\n" for name, value in planted.items()),
        encoding="utf-8",
    )
    machines = tmp_path / "machines.yml"
    machines.write_text(f"""\
orch_host: pc
accounts:
  - {{id: gemini-b, harness: gemini, signed_in_on: [pc]}}
  - {{id: gemini-c, harness: gemini, signed_in_on: [pc]}}
devices:
  - name: pc
    tailscale_name: pc.example.ts.net
    slot_max: 2
    harnesses: {{gemini: gemini-bot}}
    workdir_root: /home/{{bot}}/work
    ssh_alias: pc-{{bot}}
    factory_root: /srv/factory
    home: {home}
  - name: lap
    tailscale_name: lap.example.ts.net
    slot_max: 1
    harnesses: {{gemini: gemini-bot}}
    workdir_root: /home/{{bot}}/work
    ssh_alias: lap-{{bot}}
    factory_root: /srv/factory
    home: {home}
""", encoding="utf-8")
    for name, value in ssh.env().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("FAKE_SSH_HOME", str(home))
    monkeypatch.setenv("ORCH_SSH_MUX", "off")
    db = tmp_path / "orch.db"

    assert cli.main(["accounts", "doctor", "--db", str(db), "--machines", str(machines)]) == 0

    with sqlite3.connect(db) as connection:
        rows = {(row[0], row[1]): row for row in connection.execute(
            f"SELECT account, device, status, auth_method, detail FROM {account_doctor.TABLE}")}
    assert {key: (row[2], row[4]) for key, row in rows.items()} == {
        ("gemini-default", "pc"): ("ok", ""),
        ("gemini-b", "pc"): ("signed-out", account_doctor.NOT_LOGGED_IN),
        ("gemini-c", "pc"): ("signed-out", account_doctor.NOT_LOGGED_IN),
        ("gemini-default", "lap"): ("unreachable", account_doctor.SSH_DOWN),
    }
    envs = [set(line.split()) for line in env_log.read_text(encoding="utf-8").splitlines()]
    assert len(envs) == 3
    for names in envs:
        assert not names & set(AUTH_OVERRIDES)
        assert "NO_BROWSER" in names
    assert not [value for value in planted.values() if value in repr(rows)]


def test_the_dashboard_and_doctor_give_the_gemini_sign_in_command(tmp_path: Path) -> None:
    from lib import dashboard_accounts
    from lib.machines_yml import Account, Device, MachinesConfig

    folder = "/home/gemini-bot/.factory-accounts/gemini-pro"
    command = account_doctor.login_command("gemini", folder)
    assert command == (
        "env " + " ".join(f"-u {name}" for name in AUTH_OVERRIDES)
        + f" GEMINI_CLI_HOME={folder} NO_BROWSER=true gemini"
    )
    assert account_doctor.login_command("gemini", None).split(" NO_BROWSER")[0].endswith("-u GEMINI_CLI_HOME")
    config = MachinesConfig(
        (Device("pc", 1, {"gemini": "gemini-bot"}, home="/home/gemini-bot"),),
        accounts=(Account("gemini-pro", "gemini", signed_in_on=frozenset({"pc"})),),
    )
    with sqlite3.connect(":memory:") as db:
        rows = dashboard_accounts.read_accounts(db, NOW, config)
    commands = {account["id"]: [row["command"] for row in account["signed_in_on"]] for account in rows}
    assert commands["gemini-pro"] == [command]


def test_a_gemini_account_folder_is_prepared(tmp_path: Path) -> None:
    err = {"type": "error", "message": "quota exhausted"}
    assert lane_limit.error_text("gemini", err) == err["message"]

    bot_home = tmp_path / "gemini-home"
    bot_home.mkdir()
    machines = tmp_path / "machines.yml"
    machines.write_text(
        f"""\
orch_host: pc
accounts:
  - {{id: gemini-pro, harness: gemini, signed_in_on: [pc]}}
devices:
  - name: pc
    tailscale_name: pc.example.ts.net
    slot_max: 2
    harnesses: {{gemini: gemini-bot}}
    workdir_root: /home/{{bot}}/factory-work
    ssh_alias: pc-{{bot}}
    factory_root: /home/{{bot}}/factory
    home: {bot_home.as_posix()}
""",
        encoding="utf-8",
    )
    config, findings = load_machines_yml(machines, overlay=False)
    assert config is not None
    assert not [f for f in findings if f.severity == "error"]
    assert config.accounts_on("pc", "gemini") == ("gemini-default", "gemini-pro")
    changes = account_home.prepare(config, "pc", "gemini-bot", bot_home)
    assert any(c.action == "made" and c.path.endswith("gemini-pro") for c in changes)


@pytest.mark.parametrize("path", ["docs/ACCOUNTS.md", "docs/RUNBOOK-HARDENING.md", "bots/gemini-bot/SKILL.md"])
def test_no_shipped_procedure_runs_a_gemini_auth_subcommand(path: str) -> None:
    text = (pstack_pin.ROOT / path).read_text(encoding="utf-8")
    blocks = re.findall(r"^\s*```[a-z]*\n(.*?)^\s*```", text, flags=re.MULTILINE | re.DOTALL)
    assert [block for block in blocks if re.search(r"\bgemini\s+auth\b", block)] == []


def test_gemini_pstack_adapter_and_bot_skill_exist_and_verify() -> None:
    assert pstack_pin.verify_pin(pstack_pin.ROOT) == []
    assert (pstack_pin.ROOT / "bots" / "gemini-bot" / "SKILL.md").is_file()
    assert (pstack_pin.ROOT / "config" / "waiter-gemini-bot.example.yml").is_file()
    brief = render_brief(
        job("build", "gemini"),
        bot="gemini-bot",
        context=None,
        factory_root="/home/gemini-bot/factory",
    )
    assert "## Skills" in brief
    assert "`/home/gemini-bot/factory/skills/factory/harness/gemini.md`" in brief
    assert "inherit-parent" in brief
