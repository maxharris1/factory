from __future__ import annotations

import csv
import os
import pwd
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lib.state_backup import write_manifest
from tests.test_dispatcher import GO, comment, setup, ticket
from tests.test_orch_move import Host, _executable

ROOT = Path(__file__).resolve().parents[1]
TRAIL = ROOT / "docs" / "decisions" / "694-trail.tsv"
SEPARATOR = re.compile(r"^[;&|()<>]+$")


def _rows() -> dict[str, dict[str, str]]:
    with TRAIL.open(encoding="utf-8", newline="") as handle:
        return {row["id"]: row for row in csv.DictReader(handle, delimiter="\t", quoting=csv.QUOTE_NONE)}


def _spans(cell: str) -> list[str]:
    return re.findall(r"`([^`]*)`", cell)


def _span(row: str, needle: str) -> str:
    found = [span for span in _spans(_rows()[row]["check"]) if needle in span]
    assert found, f"{row}'s check names a command containing {needle!r}"
    return found[0]


def _run(script: str, env: dict[str, str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", "-c", script], env=env, cwd=cwd, text=True,
                          capture_output=True, timeout=60, check=False)


def _pc(tmp_path: Path, name: str) -> Host:
    pc = Host(tmp_path, name)
    _executable(pc.home / ".local/share/factory/linear-webhook-service.sh",
                '#!/bin/sh\necho "$* [${LINEAR_WEBHOOK_TUNNEL_HOST-unset}]" >>"$HOME/linear.log"\n')
    (pc.config / "webhook.env").write_text("LINEAR_WEBHOOK_TUNNEL_HOST=\n", encoding="utf-8")
    (pc.config / "orch-poll.env").write_text(f"ORCH_DB={pc.db}\n", encoding="utf-8")
    pc.retired.write_text("pc fenced\n", encoding="utf-8")
    return pc


def test_r8_import_starts_the_linear_service_listener_only(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    db = snapshot / "orch.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE jobs (id TEXT, detail TEXT, state TEXT)")
    conn.close()
    write_manifest(db, snapshot / "manifest.json", source_host="box", to="pc", retired="pc now")

    imports = [span for span in _spans(_rows()["R8"]["check"]) if "deploy/orch-move.sh import" in span]
    assert len(imports) >= 2, f"R8 names the reachable and the D4 import: {imports}"
    for index, span in enumerate(imports):
        pc = _pc(tmp_path, f"pc{index}")
        command = re.sub(r"(deploy/orch-move\.sh import) .*$", rf"\1 {snapshot}", span)
        result = _run(command, pc.env(), cwd=pc.checkout)
        assert result.returncode == 0, f"{span!r} imports: {result.stderr}"
        linear = (pc.home / "linear.log").read_text(encoding="utf-8")
        assert linear == "start []\n", f"{span!r} starts Linear with an empty tunnel host, saw {linear!r}"


def _cron_env_per_line(crontab: str) -> list[tuple[str, dict[str, str]]]:
    env: dict[str, str] = {}
    lines = []
    for line in crontab.splitlines():
        setting = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if setting:
            value = setting.group(2)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            env[setting.group(1)] = value
        else:
            lines.append((line, dict(env)))
    return lines


def test_r11_cron_runs_the_linear_ensure_listener_only(tmp_path: Path) -> None:
    seed = [span for span in _spans(_rows()["P8"]["expected"]) if span.startswith("LINEAR_WEBHOOK_TUNNEL_HOST=")]
    assert seed, "P8 expects a LINEAR_WEBHOOK_TUNNEL_HOST= crontab line"
    home = tmp_path / "home"
    share = home / ".local/share/factory"
    share.mkdir(parents=True)
    shutil.copy(ROOT / "deploy/linear-webhook-service.sh", share)
    shutil.copy(ROOT / "deploy/host-pidns.sh", share)
    table = tmp_path / "crontab.txt"
    table.write_text(f"{seed[0]}\n", encoding="utf-8")
    _executable(tmp_path / "bin/crontab", f"""#!/bin/sh
case "$1" in
    -l) [ -f "{table}" ] && exec cat "{table}"; echo "no crontab for x" >&2; exit 1 ;;
    -) exec cat >"{table}" ;;
esac
exit 2
""")

    span = _span("R11", "linear-webhook-service.sh install-autostart")
    result = _run(span, {"HOME": str(home), "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin"})
    assert result.returncode == 0, f"{span!r} installs autostart: {result.stderr}"

    tagged = [(line, env) for line, env in _cron_env_per_line(table.read_text(encoding="utf-8"))
              if "# factory-linear-webhook" in line]
    assert len(tagged) == 2, f"install-autostart writes the @reboot and 5-minute lines: {tagged}"
    for line, env in tagged:
        assert env.get("LINEAR_WEBHOOK_TUNNEL_HOST") == "", f"cron runs {line!r} with an empty tunnel host, env {env}"


def _sqlite3_shim(tmp_path: Path) -> Path:
    _executable(tmp_path / "shim/sqlite3", f"""#!{sys.executable}
import pathlib, sqlite3, sys
_, readonly, db, sql = sys.argv
assert readonly == "-readonly"
conn = sqlite3.connect(pathlib.Path(db).resolve().as_uri() + "?mode=ro", uri=True)
for statement in filter(str.strip, sql.split(";")):
    for row in conn.execute(statement):
        print("|".join("" if value is None else str(value) for value in row))
""")
    return tmp_path / "shim"


EVIDENCE = "from jobs where created_at"
COUNTS = "from jobs where finished_at"


def _r15_rows(query: str, db: Path, shim: Path, start: str, end: str) -> list[str]:
    window = _span("R15", "date -d").replace("<start ISO>", start).replace("<end ISO>", end)
    command = _span("R15", query)
    result = _run(f"{window}\n{command}", {"PATH": f"{shim}:/usr/bin:/bin", "DB": str(db)})
    assert result.returncode == 0, f"R15's window and query run: {result.stderr}"
    return result.stdout.splitlines()[1:]


def test_r15_query_finds_dispatch_and_merge_inside_the_window(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path, now=[1794916800.0])
    dispatcher.process(ticket(comment("go-1", GO.format(state="building"), author="grok-login")))
    job = queue.claim("claude")
    queue.finish(job.id, owner="claude", handoff_comment_id="ready-test")
    dispatcher.record_terminal("owner/repo", "42", "merged")
    shim = _sqlite3_shim(tmp_path)

    inside = _r15_rows(EVIDENCE, queue.path, shim, "2026-11-17T11:59:00Z", "2026-11-17T12:01:00Z")
    assert any(re.fullmatch(r"queued\|build\|[1-9]\d*", row) for row in inside), f"a dispatch inside the window: {inside}"
    assert any(re.fullmatch(r"merged\|[1-9]\d*", row) for row in inside), f"a merge inside the window: {inside}"
    later = _r15_rows(EVIDENCE, queue.path, shim, "2026-11-17T12:59:00Z", "2026-11-17T13:01:00Z")
    assert not any(re.fullmatch(r"(queued\|.*|merged\|[1-9]\d*)", row) for row in later), f"nothing an hour later: {later}"


def test_r15_evidence_ignores_a_cancel_of_a_job_queued_before_the_window(tmp_path: Path) -> None:
    clock = [1794913200.0]
    dispatcher, queue = setup(tmp_path, now=clock)
    dispatcher.process(ticket(comment("go-1", GO.format(state="building"), author="grok-login")))
    with sqlite3.connect(queue.path) as conn:
        (job_id,) = conn.execute("select id from jobs where state = 'queued'").fetchone()
    conn.close()
    clock[0] = 1794916800.0
    queue.cancel(job_id, reason="operator cancelled")
    shim = _sqlite3_shim(tmp_path)

    evidence = _r15_rows(EVIDENCE, queue.path, shim, "2026-11-17T11:59:00Z", "2026-11-17T12:01:00Z")
    assert evidence == ["merged|0"], f"a 12:00 cancel of a job queued at 11:00 is neither dispatch nor merge: {evidence}"
    counts = _r15_rows(COUNTS, queue.path, shim, "2026-11-17T11:59:00Z", "2026-11-17T12:01:00Z")
    assert "finished|cancelled|1" in counts and "job_event|cancelled|1" in counts, \
        f"the counts query still records the cancel: {counts}"


def _commands(script: str) -> list[tuple[str, list[str]]]:
    lexer = shlex.shlex(script, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    commands: list[tuple[str, list[str]]] = []
    joiner, words = "", []
    for token in lexer:
        if SEPARATOR.match(token):
            if words:
                commands.append((joiner, words))
            joiner, words = token, []
        else:
            words.append(token)
    if words:
        commands.append((joiner, words))
    nested = []
    for joiner, words in commands:
        if words[0] == "B" and len(words) == 2:
            nested += _commands(words[1])
        else:
            nested.append((joiner, words))
    return nested


def _grep_pattern(words: list[str]) -> str:
    args = iter(words[1:])
    for arg in args:
        if arg == "-e":
            return next(args)
        if not arg.startswith("-"):
            return arg
    raise AssertionError(f"grep without a pattern: {words}")


def _process_patterns(span: str) -> list[str]:
    commands = _commands(span)
    patterns = []
    for (_, before), (joiner, words) in zip([("", [""])] + commands, commands):
        if words[0] == "pgrep" and len(words) > 1:
            patterns.append(words[-1])
        elif words[0] == "grep" and joiner == "|" and before[0] == "ps":
            patterns.append(_grep_pattern(words))
    return patterns


def test_no_process_pattern_matches_the_command_line_that_runs_it() -> None:
    checked = {}
    for row_id, row in _rows().items():
        for span in _spans(row["check"]):
            for pattern in _process_patterns(span):
                checked[(row_id, pattern)] = re.search(pattern, span)
    matching = {key: match.group(0) for key, match in checked.items() if match}
    assert matching == {}, f"patterns that match their own command line: {matching}"
    assert {row_id for row_id, _ in checked} >= {"P11", "R5", "R10", "R12", "R13", "N1", "RB2"}, \
        f"every pgrep and ps | grep in the trail was checked: {sorted(checked)}"


def test_r5_prints_only_the_fence_and_the_stopped_status(tmp_path: Path) -> None:
    if subprocess.run(["unshare", "-rpf", "--mount-proc", "true"], capture_output=True, check=False).returncode:
        pytest.skip("needs unprivileged unshare for a process view without the host's own pollers")
    home = tmp_path / "home"
    (home / ".config/factory").mkdir(parents=True)
    (home / ".config/factory/orch-retired").write_text("pc 2026-11-17T12:00:00Z\n", encoding="utf-8")
    _executable(home / "factory/deploy/poller-service.sh", "#!/bin/sh\nexit 3\n")
    words = shlex.split(_span("R5", "orch-retired"))
    assert words[0] == "B" and len(words) == 2, f"R5 runs one command on the box: {words}"

    result = subprocess.run(["unshare", "-rpf", "--mount-proc", "bash", "-c", words[1]],
                            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
                            text=True, capture_output=True, timeout=60, check=False)
    assert result.stdout == f"{home}/.config/factory/orch-retired\n3\n", \
        f"R5 lists the fence, both pgreps print nothing, status exits 3: {result.stdout!r}"


def test_rb3_puts_the_export_where_rb4_imports_it(tmp_path: Path) -> None:
    sftp = Path("/usr/lib/openssh/sftp-server")
    if not shutil.which("scp") or not sftp.exists():
        pytest.skip("needs scp and the OpenSSH sftp-server")
    export2 = tmp_path / "pc-moves/20261117T120000Z"
    export2.mkdir(parents=True)
    (export2 / "orch.db").write_text("db\n", encoding="utf-8")
    (export2 / "manifest.json").write_text("{}\n", encoding="utf-8")
    box = tmp_path / "box-home"
    box.mkdir()
    _executable(tmp_path / "ssh-stub", f'#!/bin/sh\nexec {sftp} -d "{box}"\n')

    reachable = _spans(_rows()["RB3"]["check"])[0]
    prelude = (f'B() {{ HOME="{box}" bash -c "$1"; }}\n'
               f'scp() {{ command scp -S "{tmp_path / "ssh-stub"}" "$@"; }}\n')
    result = _run(prelude + reachable, {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "EXPORT2": str(export2)})
    assert result.returncode == 0, f"{reachable!r} copies the export: {result.stderr}"
    assert (box / "orch-move-rollback" / export2.name / "orch.db").is_file(), \
        f"the export lands in ~/orch-move-rollback/{export2.name} on the box"
    assert "~/orch-move-rollback/<dir>" in _rows()["RB4"]["check"], "RB4 imports from ~/orch-move-rollback/<dir>"


def test_r10_matches_the_launcher_that_execs_orch_poll(tmp_path: Path) -> None:
    home = tmp_path / "home"
    state = home / ".local/state/factory"
    state.mkdir(parents=True)
    (home / ".config/factory").mkdir(parents=True)
    for name in ("webhook.env", "orch-poll.env"):
        (home / ".config/factory" / name).write_text("", encoding="utf-8")
    _executable(home / ".local/bin/orch", f"""#!{sys.executable}
import os, sys, time
if sys.argv[1:3] == ["poll", "--pidfile"]:
    with open(sys.argv[3], "w") as pidfile:
        pidfile.write(f"{{os.getpid()}}\\n")
    time.sleep(600)
""")
    checkout = tmp_path / "checkout"
    (checkout / "deploy").mkdir(parents=True)
    for name in ("run-poller.sh", "poller-service.sh", "host-pidns.sh"):
        shutil.copy(ROOT / "deploy" / name, checkout / "deploy")
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    pidfile = state / "poller.pid"

    launcher = subprocess.Popen(["bash", str(checkout / "deploy/run-poller.sh"), "--pidfile", str(pidfile)],
                                env=env, start_new_session=True)
    try:
        deadline = time.monotonic() + 20
        while not pidfile.exists() or not pidfile.read_text(encoding="utf-8").endswith("\n"):
            assert time.monotonic() < deadline, "the poller wrote its pidfile"
            time.sleep(0.02)

        assert pidfile.read_text(encoding="utf-8").strip() == str(launcher.pid), "the poller keeps run-poller.sh's pid"

        user = pwd.getpwuid(os.getuid()).pw_name
        ps = _span("R10", "ps -o").replace("-u factory", f"-u {user}")
        listed = _run(f"echo $$; {ps}", env).stdout.splitlines()
        shell, lines = listed[0], [line.split(None, 3) for line in listed[1:]]
        ours = [fields for fields in lines if fields[1] == str(launcher.pid) or fields[2] == shell]
        assert any(fields[1] == str(launcher.pid) and "orch poll --pidfile" in fields[3] for fields in ours), \
            f"ps shows the poller as orch poll: {ours}"
        assert not any("run-poller" in fields[3] for fields in ours), f"no run-poller line: {ours}"

        expected = _rows()["R10"]["expected"]
        assert "whose ppid is `run-poller.sh`" not in expected, "R10 claims no run-poller.sh parent"
        assert "exec" in expected, "R10 says run-poller.sh execs orch poll"

        cat = _run(_span("R10", "poller.pid"), env)
        assert cat.stdout.strip() == str(launcher.pid), "R10's cat prints the poller's pid"
        status = _run(_span("R10", "poller-service.sh status"), env, cwd=checkout)
        assert status.stdout.splitlines() == [f"running (pid {launcher.pid})", "0"], \
            f"R10's status names the poller and exits 0: {status.stdout!r}"
    finally:
        os.killpg(launcher.pid, 9)
        launcher.wait(timeout=10)
