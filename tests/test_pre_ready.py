"""The review.pre_ready runner (#503): head check, timeout, environment, cut."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lib.factory_md import CheckCommand
from lib.pre_ready import TAIL_CHARS, TAIL_LINES, check_environ, cut, run


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def checkout(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "work"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "one")
    return repo, git(repo, "rev-parse", "HEAD")


def test_a_passing_command_passes(tmp_path: Path) -> None:
    repo, head = checkout(tmp_path)

    result = run(CheckCommand("echo fine", 10), repo, head, dict(os.environ))

    assert (result.passed, result.reason, result.tail) == (True, "exit 0", "fine")


def test_a_failing_command_fails_with_its_exit_and_output(tmp_path: Path) -> None:
    repo, head = checkout(tmp_path)

    result = run(CheckCommand("echo broke; exit 3", 10), repo, head, dict(os.environ))

    assert (result.passed, result.reason, result.tail) == (False, "exit 3", "broke")


def test_a_timeout_fails_and_kills_the_whole_group(tmp_path: Path) -> None:
    repo, head = checkout(tmp_path)
    marker = tmp_path / "child-lived"

    started = time.monotonic()
    result = run(
        CheckCommand(f"(sleep 3; touch {marker}) & sleep 30", 1), repo, head, dict(os.environ)
    )

    assert (result.passed, result.reason) == (False, "timeout after 1s")
    assert time.monotonic() - started < 10
    time.sleep(3.5)
    assert not marker.exists()


def test_a_timeout_does_not_wait_on_a_descendant_that_left_the_group(tmp_path: Path) -> None:
    repo, head = checkout(tmp_path)
    pid_file = tmp_path / "escaped.pid"

    started = time.monotonic()
    try:
        result = run(
            CheckCommand(f"echo before; setsid sh -c 'echo $$ > {pid_file}; exec sleep 30' & wait", 1),
            repo,
            head,
            dict(os.environ),
        )
        elapsed = time.monotonic() - started
    finally:
        os.kill(int(pid_file.read_text()), signal.SIGKILL)

    assert (result.passed, result.reason, result.tail) == (False, "timeout after 1s", "before")
    assert elapsed < 5


def test_another_head_or_a_dirty_tree_runs_nothing(tmp_path: Path) -> None:
    repo, head = checkout(tmp_path)
    marker = tmp_path / "ran"
    spec = CheckCommand(f"touch {marker}", 10)

    other = run(spec, repo, "b" * 40, dict(os.environ))
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    dirty = run(spec, repo, head, dict(os.environ))

    assert (other.passed, other.reason) == (False, f"not at head {'b' * 40}")
    assert (dirty.passed, dirty.reason) == (False, f"not at head {head}")
    assert not marker.exists()


def test_the_command_sees_no_token_proof_or_queue_variables(tmp_path: Path) -> None:
    repo, head = checkout(tmp_path)
    caller = {
        "PATH": os.environ["PATH"],
        "GH_TOKEN": "x",
        "GITHUB_TOKEN": "x",
        "ORCH_JOB_PROOF": "x",
        "ORCH_DB": "x",
        "KEPT_VARIABLE": "kept",
    }

    result = run(CheckCommand("env", 10), repo, head, check_environ(caller, None))

    names = {line.partition("=")[0] for line in result.tail.splitlines()}
    assert names.isdisjoint({"GH_TOKEN", "GITHUB_TOKEN", "ORCH_JOB_PROOF", "ORCH_DB"})
    assert "KEPT_VARIABLE" in names


def test_orch_python_names_the_interpreter_orch_runs_under(tmp_path: Path) -> None:
    repo, head = checkout(tmp_path)
    caller = {"PATH": "/usr/bin:/bin", "ORCH_PYTHON": "/elsewhere/python3"}

    same = "import os, sys; print(os.path.abspath(sys.executable) == os.path.abspath(sys.argv[1]))"

    result = run(
        CheckCommand(f'"$ORCH_PYTHON" -c {shlex.quote(same)} {shlex.quote(sys.executable)}', 10),
        repo,
        head,
        check_environ(caller, None),
    )

    assert (result.passed, result.tail) == (True, "True")


def test_long_output_is_cut_to_its_last_lines(tmp_path: Path) -> None:
    repo, head = checkout(tmp_path)

    result = run(CheckCommand("seq 1 10000", 10), repo, head, dict(os.environ))
    body = result.tail.splitlines()

    assert body[0] == f"[pre_ready output cut to its last {TAIL_LINES} lines]"
    assert len(body) == TAIL_LINES + 1
    assert body[-1] == "10000"


def test_wide_lines_are_cut_to_the_character_limit() -> None:
    tail = cut("\n".join("x" * 500 for _ in range(30)) + "\nlast")

    assert len(tail.partition("\n")[2]) <= TAIL_CHARS
    assert tail.startswith("[pre_ready output cut to its last ")
    assert tail.endswith("\nlast")


def test_short_output_is_not_cut() -> None:
    assert cut("one\ntwo\n") == "one\ntwo"


def test_a_secret_in_the_output_is_redacted(tmp_path: Path) -> None:
    repo, head = checkout(tmp_path)
    command = "echo GITHUB_TOKEN=ghp_abcdefghijklmnop; echo https://user:pw@example.test/x"

    result = run(CheckCommand(command, 10), repo, head, dict(os.environ))

    assert "ghp_abcdefghijklmnop" not in result.tail
    assert "user:pw" not in result.tail
    assert result.tail == "<secret>\n[redacted-url]"


def test_a_failing_check_redacts_its_tail(tmp_path: Path) -> None:
    repo, head = checkout(tmp_path)

    result = run(CheckCommand("echo password: hunter22; exit 4", 10), repo, head, dict(os.environ))

    assert (result.passed, result.reason) == (False, "exit 4")
    assert "hunter22" not in result.tail and "<secret>" in result.tail


def test_a_secret_split_by_the_character_cut_is_redacted_whole() -> None:
    token = "ghp_" + "a" * 60
    secret_line = "token " + token
    filler = "y" * (TAIL_CHARS - 30)

    tail = cut(f"{secret_line}\n{filler}")

    assert "a" * 8 not in tail and "<secret>" in tail


def test_a_secret_assignment_split_across_lines_is_redacted() -> None:
    tail = cut("API_KEY=\nsupersecretvalue\n" + "\n".join(str(n) for n in range(TAIL_LINES - 1)))

    assert "supersecretvalue" not in tail


QUOTED_SECRETS = (
    'password="first SYNTHETIC_VALUE"',
    '{"api_key": "SYNTHETIC_VALUE"}',
    "{'password': 'a b SYNTHETIC_VALUE'}",
    '{"client_secret":"first SYNTHETIC_VALUE"}',
    "secret: 'it\\'s SYNTHETIC_VALUE'",
    'GH_TOKEN="x y SYNTHETIC_VALUE" next',
    "password:\n  SYNTHETIC_VALUE",
    'password="first\nSYNTHETIC_VALUE"',
    "token='first\n\nSYNTHETIC_VALUE' after",
    'export DB_PASSWORD="first\\"\nSYNTHETIC_VALUE"',
    '{"api_key": "first\n  SYNTHETIC_VALUE"}',
    'secret="first\nSYNTHETIC_VALUE',
)


UNTERMINATED_AT_END_OF_OUTPUT = tuple(
    f"{name}{quote}first\nSYNTHETIC_VALUE{escape}"
    for name in ("password=", "export API_KEY=", '{"token": ')
    for quote in ('"', "'", '$"', "$'")
    for escape in ("", "\\", "\\\\", "\\\\\\")
)


@pytest.mark.parametrize("text", UNTERMINATED_AT_END_OF_OUTPUT)
def test_an_unterminated_quoted_secret_at_the_end_of_output_is_redacted_whole(text: str) -> None:
    assert "SYNTHETIC_VALUE" not in cut(text)


@pytest.mark.parametrize("ending", ["exit 1", "sleep 3"])
@pytest.mark.parametrize("continuations", [1, TAIL_LINES + 5])
def test_a_check_ending_mid_escape_in_a_quoted_secret_redacts_it_whole(
    tmp_path: Path, ending: str, continuations: int
) -> None:
    repo, head = checkout(tmp_path)
    value = "\n".join(f"part{n}" for n in range(continuations))
    text = f'password="{value}\nSYNTHETIC_VALUE' + chr(92)
    command = "printf %s " + shlex.quote(text) + "; " + ending

    result = run(CheckCommand(command, 1), repo, head, {"PATH": "/usr/bin:/bin"})

    assert not result.passed
    assert "SYNTHETIC_VALUE" not in result.tail and "part0" not in result.tail


def test_a_bare_secret_continued_by_a_backslash_newline_is_redacted_whole() -> None:
    assert cut("PASSWORD=first\\\nSYNTHETIC_VALUE next") == "<secret> next"


@pytest.mark.parametrize("line", QUOTED_SECRETS)
def test_a_quoted_or_structured_secret_is_redacted_whole(line: str) -> None:
    assert "SYNTHETIC_VALUE" not in cut(line)


@pytest.mark.parametrize("line", QUOTED_SECRETS)
def test_a_quoted_secret_cut_by_the_character_limit_is_redacted_whole(line: str) -> None:
    padded = line.replace("SYNTHETIC_VALUE", "w " * TAIL_CHARS + "SYNTHETIC_VALUE")

    assert "SYNTHETIC_VALUE" not in cut(padded)


@pytest.mark.parametrize("ending", ["exit 5", "sleep 30"])
def test_a_failed_or_timed_out_check_redacts_quoted_secrets(tmp_path: Path, ending: str) -> None:
    repo, head = checkout(tmp_path)
    script = tmp_path / "secrets.txt"
    script.write_text("\n".join(QUOTED_SECRETS) + "\n", encoding="utf-8")

    result = run(CheckCommand(f"cat {script}; {ending}", 1), repo, head, dict(os.environ))

    assert not result.passed
    assert "first" not in result.tail and "SYNTHETIC_VALUE" not in result.tail


@pytest.mark.parametrize("ending", ["exit 1", "sleep 30"])
def test_a_quoted_secret_longer_than_the_line_cut_is_redacted_whole(
    tmp_path: Path, ending: str
) -> None:
    repo, head = checkout(tmp_path)
    value = "\n".join(f"part{n}" for n in range(TAIL_LINES + 5)) + "\nSYNTHETIC_VALUE"
    script = tmp_path / "secret.txt"
    script.write_text(f'password="{value}"\ndone\n', encoding="utf-8")

    result = run(CheckCommand(f"cat {script}; {ending}", 1), repo, head, dict(os.environ))

    assert not result.passed
    assert "SYNTHETIC_VALUE" not in result.tail and "part4" not in result.tail
    assert result.tail.endswith("done")
