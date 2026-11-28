"""#785: unit and integration tests for the Claude Code cloud QA runner."""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Mapping
from pathlib import Path

import pytest

from lib import qa_runners, qa_smoke
from lib.factory_md import QA_RUNNERS, QaRunner, parse_factory_md
from lib.handoff import parse_comment
from lib.orch_yml import load_orch_yml
from lib.qa_runners import Lane, Launched, LaunchFailed, RunnerOutage, UsageLimit
from lib.qa_runners.claude_cloud import (
    BINARY_ENV,
    CONFIG_DIR_ENV,
    ENVIRONMENTS_ENV,
    LAUNCH_TIMEOUT_ENV,
    REPO_CACHE_ENV,
    REPO_URL_TEMPLATE_ENV,
    RUNNER_NAME,
    STOP_TIMEOUT_ENV,
    WORK_ROOT_ENV,
    ClaudeCloudConfig,
    ClaudeCloudRunner,
    RunCommand,
    _default_run,
    parse_launch_output,
)
from tests.test_qa_cloud import Lab

HEAD_SHA = "a" * 40
BASE_SHA = "b" * 40
GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "QA",
    "GIT_AUTHOR_EMAIL": "qa@example.invalid",
    "GIT_COMMITTER_NAME": "QA",
    "GIT_COMMITTER_EMAIL": "qa@example.invalid",
}


def _git(*args: object, cwd: Path | None = None) -> str:
    proc = subprocess.run(["git", *map(str, args)], cwd=cwd, env=GIT_ENV, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _source_repo(root: Path, branch: str = "main") -> tuple[Path, str]:
    source = root / "source"
    if not source.exists():
        _git("init", "-b", branch, source)
        (source / "README.md").write_text("base\n", encoding="utf-8")
        _git("add", ".", cwd=source)
        _git("commit", "-m", "base", cwd=source)
    return source, _git("rev-parse", branch, cwd=source)


def _write_fake_claude(bin_dir: Path, state_dir: Path, script_body: str | None = None) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    exe = bin_dir / "claude"
    if script_body is None:
        script_body = f"""#!{sys.executable}
import json, os, pathlib, subprocess, sys, time

state = pathlib.Path({str(state_dir)!r})
calls_file = state / "calls.jsonl"
mode_file = state / "mode.json"
mode = json.loads(mode_file.read_text(encoding="utf-8")) if mode_file.exists() else {{}}

argv = sys.argv[1:]
if argv == ["--version"]:
    if mode.get("version_fail"):
        sys.stderr.write(mode["version_fail"] + "\\n")
        sys.exit(1)
    print(mode.get("version", "2.1.323 (Claude Code)"))
    sys.exit(0)

cwd = os.getcwd()
try:
    branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd, text=True).strip()
    remote = subprocess.check_output(["git", "remote", "get-url", "origin"], cwd=cwd, text=True).strip()
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=cwd, text=True).strip()
except Exception:
    branch = remote = head = None

record = {{
    "argv": argv,
    "cwd": cwd,
    "branch": branch,
    "remote": remote,
    "head": head,
    "isatty": [sys.stdin.isatty(), sys.stdout.isatty(), sys.stderr.isatty()],
    "claude_config_dir": os.environ.get("CLAUDE_CONFIG_DIR"),
    "anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY"),
    "oauth_token": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"),
    "linear_api_key": os.environ.get("LINEAR_API_KEY"),
    "gh_token": os.environ.get("GH_TOKEN"),
    "aws_secret": os.environ.get("AWS_SECRET_ACCESS_KEY"),
}}
with calls_file.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(record) + "\\n")

if "-p" in argv and "--cloud" in argv:
    idx = argv.index("--cloud")
    sid = argv[idx + 1]
    if mode.get("stop_delay"):
        time.sleep(float(mode["stop_delay"]))
    if mode.get("stop_error"):
        err = mode["stop_error"]
        sys.stderr.write(err["text"] + "\\n")
        sys.exit(err.get("code", 1))
    owner = mode.get("session_config_dir")
    if owner and os.environ.get("CLAUDE_CONFIG_DIR") != owner:
        sys.stderr.write(f"Error: session {{sid}} not found\\n")
        sys.exit(1)
    if mode.get("stop_json_error"):
        print(json.dumps({{"ok": False, "error": mode["stop_json_error"]}}))
        sys.exit(0)
    print(json.dumps({{
        "ok": True,
        "session_id": sid,
        "url": f"https://claude.ai/code/{{sid}}?from=cli&m=0",
    }}))
    sys.exit(0)

if "--cloud" in argv:
    if not sys.stdout.isatty():
        sys.stderr.write(
            "Error: --cloud requires an interactive terminal.\\n"
            "Non-interactive invocations (piped stdout, --init-only, --sdk-url) run locally "
            "and would silently ignore --cloud. Drop --cloud, or run from a TTY.\\n"
        )
        sys.exit(1)
    if "--environment" in argv:
        env_val = argv[argv.index("--environment") + 1]
        if not env_val.startswith("ccpool_"):
            sys.stderr.write(f'--environment expects a self-hosted environment id (ccpool_...), got "{{env_val}}"\\n')
            sys.exit(1)
    if mode.get("launch_delay"):
        time.sleep(float(mode["launch_delay"]))
    if mode.get("trust_prompt"):
        sys.stdout.write(
            "\\x1b[1mAccessing workspace\\x1b[0m\\n"
            "Quick safety check: Is this a project you created or one you trust?\\n"
            "> 1. No, exit\\n"
            "  2. Yes, I trust this folder\\n"
        )
        sys.stdout.flush()
        time.sleep(30)
        sys.exit(124)
    if mode.get("launch_error"):
        err = mode["launch_error"]
        sys.stderr.write(err["text"] + "\\n")
        sys.exit(err.get("code", 1))
    counter_file = state / "counter"
    n = int(counter_file.read_text(encoding="utf-8")) + 1 if counter_file.exists() else 1
    counter_file.write_text(str(n), encoding="utf-8")
    sid = f"session_01TestClaude{{n:04d}}"
    url = f"https://claude.ai/code/{{sid}}?from=cli&m=0"
    if mode.get("ansi"):
        print(f"\\x1b[32mCreated cloud session:\\x1b[0m QA run {{n}}")
        print(f"\\x1b[36mView:\\x1b[0m {{url}}")
        print(f"Resume with: \\x1b[1mclaude --teleport {{sid}}\\x1b[0m")
    else:
        print("Created cloud session: " + mode.get("title", f"QA run {{n}}"))
        if mode.get("machine"):
            print(f"Machine: {{mode['machine']}}")
        print(f"View: {{url}}")
        print(f"Resume with: claude --teleport {{sid}}")
    sys.exit(0)

sys.stderr.write("unexpected args: " + " ".join(argv) + "\\n")
sys.exit(2)
"""
    exe.write_text(script_body, encoding="utf-8")
    exe.chmod(0o755)
    return exe


def _runner(
    tmp_path: Path,
    *,
    source: Path | None = None,
    bin_dir: Path | None = None,
    config_dir: Path | None = None,
    extra_env: dict[str, str] | None = None,
    environments: dict[str, str] | None = None,
    repo_url_template: str | None = None,
    launch_timeout: int = 30,
    run: RunCommand = _default_run,
) -> tuple[ClaudeCloudRunner, Path]:
    if source is None and repo_url_template is None:
        source, _ = _source_repo(tmp_path)
    state_dir = tmp_path / "cli-state"
    if bin_dir is None:
        bin_dir = tmp_path / "bin"
        _write_fake_claude(bin_dir, state_dir)
    if config_dir is None:
        config_dir = tmp_path / "claude-qa-config"
        config_dir.mkdir(parents=True, exist_ok=True)
    env = {
        **GIT_ENV,
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        **(extra_env or {}),
    }
    cfg = ClaudeCloudConfig(
        config_dir=config_dir,
        work_root=tmp_path / "work",
        repo_cache_root=tmp_path / "repos",
        claude_binary="claude",
        launch_timeout_seconds=launch_timeout,
        repo_url_template=repo_url_template or str(source),
        environments={"qa-prod": "ccpool_qa_prod_01", "qa-cleffy": "ccpool_qa_cleffy_01"}
        if environments is None
        else environments,
    )
    return ClaudeCloudRunner(cfg, home=tmp_path / "home", environ=env, run=run), state_dir


def _lane(
    lane_id: str = "lane-1",
    *,
    brief: str = "# QA lane 1 brief\nUpload to https://bucket.invalid/result.json?up=SECRET_SIG_TOKEN\n",
    repo: str = "acme/widgets",
    base_ref: str = "main",
    environment: str = "qa-prod",
    model: str | None = "claude-sonnet-4-6",
) -> Lane:
    return Lane(
        lane_id=lane_id,
        brief=brief,
        repo=repo,
        base_ref=base_ref,
        environment_name=environment,
        model=model,
    )


def _calls(state_dir: Path) -> list[dict]:
    calls_file = state_dir / "calls.jsonl"
    if not calls_file.exists():
        return []
    return [json.loads(line) for line in calls_file.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_claude_cloud_is_registered_and_accepted_by_factory_md_and_handoff() -> None:
    assert RUNNER_NAME in QA_RUNNERS
    assert RUNNER_NAME in qa_runners.registered()
    instance = qa_runners.create(RUNNER_NAME)
    assert isinstance(instance, ClaudeCloudRunner)

    text = (
        "---\n"
        "base_branch: main\n"
        "tickets: github\n"
        "builder: {harness: claude}\n"
        "reviewer: {harness: codex}\n"
        "review: {rounds: {low: 1, high: 3}, verify: none}\n"
        "ship: merge\n"
        "skills: [poteto-mode]\n"
        "qa:\n"
        "  enabled: true\n"
        "  runners: [claude-cloud]\n"
        "  claude_cloud:\n"
        "    environment: qa-prod\n"
        "    model: sonnet\n"
        "  profile: full-stack\n"
        "  lane: {boot: 'npm run dev', base_url: 'http://127.0.0.1:3000'}\n"
        "---\n"
    )
    cfg, findings = parse_factory_md(text, strict=True)
    assert findings == []
    assert cfg is not None and cfg.qa is not None
    assert cfg.qa.runners == (QaRunner("claude-cloud", "qa-prod", "sonnet"),)

    parsed = parse_comment(
        f"<!-- orch:v1 kind=qa from=grok-bot role=qa head={HEAD_SHA} result=PASS scope=full "
        "runner=claude-cloud arm=primary attempt=1 -->\n## QA result: PASS\n"
    )
    assert dict(parsed.fields)["runner"] == "claude-cloud"


def test_launch_runs_in_base_branch_checkout_with_pty_env_model_and_config_dir(tmp_path: Path) -> None:
    source, base_sha = _source_repo(tmp_path, branch="dev")
    config_dir = tmp_path / "qa-claude-home"
    config_dir.mkdir()
    secret_file = config_dir / "credentials.json"
    secret_file.write_text('{"token":"sk-ant-do-not-read-me"}\n', encoding="utf-8")
    secret_file.chmod(0o000)
    try:
        runner, state_dir = _runner(
            tmp_path,
            source=source,
            config_dir=config_dir,
            extra_env={
                "ANTHROPIC_API_KEY": "sk-ant-ambient-must-be-dropped",
                "CLAUDE_CODE_OAUTH_TOKEN": "oauth-ambient-must-be-dropped",
                "LINEAR_API_KEY": "lin_api_must_be_dropped",
                "GH_TOKEN": "ghp_must_be_dropped",
                "AWS_SECRET_ACCESS_KEY": "aws_secret_must_be_dropped",
            },
        )
        (state_dir / "mode.json").write_text(json.dumps({"ansi": True}), encoding="utf-8")
        lane = _lane(base_ref="dev", environment="qa-cleffy", model="claude-opus-4-7")

        launched = runner.launch(lane)
        assert launched.agent_id == "session_01TestClaude0001"
        assert launched.url == "https://claude.ai/code/session_01TestClaude0001?from=cli&m=0"
        assert runner.status(launched.agent_id) == "running"

        # Idempotent across in-memory call and a fresh restarted runner.
        assert runner.launch(lane) == launched
        fresh, _ = _runner(tmp_path, source=source, bin_dir=tmp_path / "bin", config_dir=config_dir)
        assert fresh.launch(lane) == launched

        calls = _calls(state_dir)
        assert len(calls) == 1
        call = calls[0]
        assert call["branch"] == "dev"
        assert call["head"] == base_sha
        assert call["remote"] == str(source)
        assert Path(call["cwd"]) == runner.checkout_path_for("acme/widgets", "dev")
        assert call["isatty"] == [True, True, True]
        assert call["claude_config_dir"] == str(config_dir)
        assert call["anthropic_api_key"] is None
        assert call["oauth_token"] is None
        assert call["linear_api_key"] is None
        assert call["gh_token"] is None
        assert call["aws_secret"] is None
        assert call["argv"] == [
            "--environment", "ccpool_qa_cleffy_01",
            "--model", "claude-opus-4-7",
            "--cloud", lane.brief,
        ]

        # Persisted markers must never contain brief text or signed upload links.
        for marker in (tmp_path / "work").rglob("*.json"):
            content = marker.read_text(encoding="utf-8")
            assert "SECRET_SIG_TOKEN" not in content
            assert "sk-ant-do-not-read-me" not in content
    finally:
        secret_file.chmod(0o600)


def test_environment_resolution_honors_claude_cli_ccpool_contract(tmp_path: Path) -> None:
    source, _ = _source_repo(tmp_path)
    dedicated_env_dir = tmp_path / "claude-hosted-env"
    dedicated_env_dir.mkdir()
    runner, state_dir = _runner(
        tmp_path,
        source=source,
        environments={"hosted-qa": str(dedicated_env_dir)},
    )

    # 1. Direct ccpool_... id passes straight through to --environment.
    direct = runner.launch(_lane("lane-direct", environment="ccpool_direct_99", model=None))
    assert direct.agent_id == "session_01TestClaude0001"

    # 2. Dedicated /remote-env config directory launches without --environment and stops with the same config dir.
    hosted = runner.launch(_lane("lane-hosted", environment="hosted-qa", model=None))
    runner.stop(hosted.agent_id)

    # 3. Unmapped non-ccpool_ logical name fails closed instead of sending an invalid flag to claude.
    with pytest.raises(LaunchFailed, match="ccpool_"):
        runner.launch(_lane("lane-unmapped", environment="qa-unmapped", model=None))

    calls = _calls(state_dir)
    assert len(calls) == 3
    assert calls[0]["argv"][:2] == ["--environment", "ccpool_direct_99"]
    assert "--environment" not in calls[1]["argv"]
    assert calls[1]["claude_config_dir"] == str(dedicated_env_dir)
    assert calls[2]["argv"] == ["-p", "stop", "--cloud", hosted.agent_id, "--output-format", "json"]
    assert calls[2]["claude_config_dir"] == str(dedicated_env_dir)


def test_launch_omits_model_when_none_and_recovers_dirty_checkout_when_base_advances(tmp_path: Path) -> None:
    source, _ = _source_repo(tmp_path, branch="main")
    runner, state_dir = _runner(tmp_path, source=source)

    first = runner.launch(_lane("lane-1", model=None))
    checkout = runner.checkout_path_for("acme/widgets", "main")
    # Introduce local drift on the same tracked file that advances upstream (f-6).
    (checkout / "README.md").write_text("dirty local drift\n", encoding="utf-8")
    (checkout / "untracked.tmp").write_text("junk\n", encoding="utf-8")

    (source / "README.md").write_text("updated upstream\n", encoding="utf-8")
    _git("add", ".", cwd=source)
    _git("commit", "-m", "advance base", cwd=source)
    new_sha = _git("rev-parse", "main", cwd=source)

    second = runner.launch(_lane("lane-2", model=None))
    assert second.agent_id != first.agent_id
    calls = _calls(state_dir)
    assert len(calls) == 2
    assert "--model" not in calls[0]["argv"]
    assert calls[1]["head"] == new_sha
    assert calls[0]["cwd"] == calls[1]["cwd"]
    assert (checkout / "README.md").read_text(encoding="utf-8") == "updated upstream\n"
    assert not (checkout / "untracked.tmp").exists()


def test_stop_queues_json_stop_message_and_second_stop_is_noop(tmp_path: Path) -> None:
    runner, state_dir = _runner(tmp_path)
    launched = runner.launch(_lane())

    runner.stop(launched.agent_id)
    assert runner.status(launched.agent_id) == "ended"
    # Second stop on an ended session raises nothing and does not invoke CLI again.
    runner.stop(launched.agent_id)

    calls = _calls(state_dir)
    assert len(calls) == 2
    stop_call = calls[1]
    assert stop_call["argv"] == ["-p", "stop", "--cloud", launched.agent_id, "--output-format", "json"]
    assert stop_call["claude_config_dir"] == str(tmp_path / "claude-qa-config")


def test_stop_treats_remote_already_ended_session_as_not_an_error(tmp_path: Path) -> None:
    runner, state_dir = _runner(tmp_path)
    (state_dir / "mode.json").write_text(
        json.dumps({"stop_error": {"code": 1, "text": "Error: session session_01Ended has already ended"}}),
        encoding="utf-8",
    )
    runner.stop("session_01Ended")
    assert runner.status("session_01Ended") == "ended"


def test_stop_tls_certificate_expired_remains_retryable_until_delivery_succeeds(tmp_path: Path) -> None:
    runner, state_dir = _runner(tmp_path)
    launched = runner.launch(_lane())

    (state_dir / "mode.json").write_text(
        json.dumps({"stop_error": {"code": 1, "text": "TLS connection failed: certificate has expired"}}),
        encoding="utf-8",
    )
    with pytest.raises(RunnerOutage):
        runner.stop(launched.agent_id)
    assert runner.status(launched.agent_id) == "running"

    # Restore healthy CLI and retry stop: it must deliver the stop message and mark ended.
    (state_dir / "mode.json").write_text("{}", encoding="utf-8")
    runner.stop(launched.agent_id)
    assert runner.status(launched.agent_id) == "ended"

    stop_calls = [c for c in _calls(state_dir) if "-p" in c["argv"]]
    assert len(stop_calls) == 2


def test_silent_remote_exit_reclaims_lane_at_timeout_when_stop_sees_already_ended(
    tmp_path: Path,
) -> None:
    """Claude Code 2.1.322 has no non-interactive cloud status CLI command (#780):
    a lane whose remote session exits silently stays running on the local marker
    until qa.timeout_minutes, when stop() observes already-ended and releases the slot."""
    source, _ = _source_repo(tmp_path)
    runner, state_dir = _runner(
        tmp_path / "runner",
        source=source,
        environments={"full-stack": "ccpool_full_stack"},
    )
    lab = Lab(tmp_path / "lab", lanes=1, runners=("claude-cloud",))
    lab.fakes["claude-cloud"] = runner
    lab.worker = lab.new_worker()
    lab.enqueue(ids=1)
    lab.silent.add((0, 1))

    # Launch the lane; the remote session then exits silently without uploading results.
    lab.tick(2)
    [(agent_id,)] = lab.run.rows("SELECT agent_id FROM qa_lanes")
    assert agent_id == "session_01TestClaude0001"
    assert runner.status(agent_id) == "running"
    (state_dir / "mode.json").write_text(
        json.dumps({"stop_error": {"code": 1, "text": f"Error: session {agent_id} has already ended"}}),
        encoding="utf-8",
    )

    # Before qa.timeout_minutes, status() stays running without polling a non-existent CLI command.
    lab.tick(7)
    assert lab.posted == {}
    assert runner.status(agent_id) == "running"
    assert [c for c in _calls(state_dir) if "-p" in c["argv"]] == []

    # Past qa.timeout_minutes, stop() treats already-ended as clean and releases the lane.
    lab.tick(4)
    assert [row[3:5] for row in lab.lanes()] == [("timed-out", "no-result-in-time")]
    assert runner.status(agent_id) == "ended"
    assert lab.header()["result"] == "BLOCKED"
    assert len([c for c in _calls(state_dir) if "-p" in c["argv"]]) == 1


@pytest.mark.parametrize(
    ("recorded_output", "expected_error"),
    [
        (
            "Error: You've hit your limit · resets 11pm (America/New_York)",
            UsageLimit,
        ),
        (
            "API Error: 429 {\"type\":\"error\",\"error\":{\"type\":\"rate_limit_error\",\"message\":\"Number of request tokens has exceeded your rate limit\"}}",
            UsageLimit,
        ),
        (
            "Error: Out of extra usage. Your usage limit has been reached.",
            UsageLimit,
        ),
        (
            "Unable to connect to API. Check your internet connection",
            RunnerOutage,
        ),
        (
            "Error: fetch failed: getaddrinfo ENOTFOUND api.anthropic.com",
            RunnerOutage,
        ),
        (
            "API Error: 529 {\"type\":\"error\",\"error\":{\"type\":\"overloaded_error\",\"message\":\"Overloaded\"}}",
            RunnerOutage,
        ),
        (
            "Error: 503 Service Unavailable - upstream connect error or disconnect/reset before headers",
            RunnerOutage,
        ),
        (
            "Error: ECONNRESET connection reset by peer",
            RunnerOutage,
        ),
        (
            "Error: request to https://api.anthropic.com/response403/v1/messages failed: ECONNREFUSED",
            RunnerOutage,
        ),
        (
            "Error: --cloud requires an interactive terminal.\nNon-interactive invocations run locally.",
            LaunchFailed,
        ),
        (
            "Error: Not logged in · Please run /login (https://secret.invalid/upload?up=LEAK_ME)",
            LaunchFailed,
        ),
    ],
)
def test_recorded_cli_failures_map_to_runner_errors_without_leaking_urls(
    tmp_path: Path,
    recorded_output: str,
    expected_error: type[Exception],
) -> None:
    runner, state_dir = _runner(tmp_path)
    (state_dir / "mode.json").write_text(
        json.dumps({"launch_error": {"code": 1, "text": recorded_output}}),
        encoding="utf-8",
    )
    with pytest.raises(expected_error) as exc_info:
        runner.launch(_lane())
    assert "SECRET_SIG_TOKEN" not in str(exc_info.value)
    assert "LEAK_ME" not in str(exc_info.value)


def test_git_http_503_raises_runner_outage_and_triggers_coordinator_fallback(tmp_path: Path) -> None:
    class _Always503(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(503)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Always503)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        runner, _ = _runner(
            tmp_path / "runner",
            repo_url_template=f"http://127.0.0.1:{port}/{{repo}}.git",
            environments={"full-stack": "ccpool_full_stack"},
        )
        with pytest.raises(RunnerOutage):
            runner.launch(_lane(environment="full-stack"))

        lab = Lab(tmp_path / "lab", lanes=1, runners=("claude-cloud", "cursor-cloud"))
        lab.fakes["claude-cloud"] = runner
        lab.worker = lab.new_worker()
        lab.enqueue(ids=1)
        lab.tick(3)
        assert lab.header()["runner"] == "cursor-cloud"
        assert lab.header()["result"] == "PASS"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("repo_path", ["example", "response403", "HTTP404", "rate_limit_error"])
def test_git_empty_reply_raises_runner_outage_and_triggers_coordinator_fallback(tmp_path: Path, repo_path: str) -> None:
    class _EmptyReply(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.close_connection = True

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _EmptyReply)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        runner, _ = _runner(
            tmp_path / "runner",
            repo_url_template=f"http://127.0.0.1:{server.server_address[1]}/{repo_path}/{{repo}}.git",
            environments={"full-stack": "ccpool_full_stack"},
        )
        with pytest.raises(RunnerOutage):
            runner.launch(_lane(environment="full-stack"))

        lab = Lab(tmp_path / "lab", lanes=1, runners=("claude-cloud", "cursor-cloud"))
        lab.fakes["claude-cloud"] = runner
        lab.worker = lab.new_worker()
        lab.enqueue(ids=1)
        lab.tick(3)
        assert lab.header()["runner"] == "cursor-cloud"
        assert lab.header()["result"] == "PASS"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("fatal: unable to access 'https://repo.invalid/o/r.git/': Empty reply from server", RunnerOutage),
        ("fatal: unable to access 'https://repo.invalid/o/r.git/': Recv failure: Connection reset by peer", RunnerOutage),
        ("error: RPC failed; curl 18 transfer closed with outstanding read data remaining", RunnerOutage),
        ("fatal: unable to access 'https://repo.invalid/o/r.git/': The requested URL returned error: 429", RunnerOutage),
        ("fatal: unable to access 'https://repo.invalid/o/r.git/': The requested URL returned error: 403", LaunchFailed),
        ("fatal: unable to access 'https://repo.invalid/o/r.git/': The requested URL returned error: 400", LaunchFailed),
        ("fatal: unable to access 'https://repo.invalid/o/r.git/': The requested URL returned error: 408", RunnerOutage),
        ("fatal: unable to access 'https://qa-review.invalid/o/r.git/': CONNECT tunnel failed, response 403", LaunchFailed),
        ("fatal: unable to access 'https://qa-review.invalid/o/r.git/': CONNECT tunnel failed, response 407", LaunchFailed),
        ("fatal: unable to access 'https://qa-review.invalid/o/r.git/': CONNECT tunnel failed, response 429", RunnerOutage),
        ("fatal: unable to access 'https://qa-review.invalid/o/r.git/': CONNECT tunnel failed, response 502", RunnerOutage),
        (
            "fatal: unable to access 'https://qa-review.invalid/o/r.git/': Received HTTP code 403 from proxy after CONNECT",
            LaunchFailed,
        ),
        ("error: RPC failed; HTTP 403 curl 22 The requested URL returned error: 403", LaunchFailed),
        ("client_loop: send disconnect: Broken pipe", RunnerOutage),
        ("fatal: unable to access 'http://127.0.0.1:9/example/response403.git/': Empty reply from server", RunnerOutage),
        ("fatal: unable to access 'https://repo.invalid/HTTP404/r.git/': Connection reset by peer", RunnerOutage),
        ("fatal: repository 'https://repo.invalid/econnreset/r.git/' not found", LaunchFailed),
        ("fatal: 'acme/getaddrinfo.git' does not appear to be a git repository", LaunchFailed),
        ("fatal: unable to access 'https://repo.invalid/response502/r.git/': The requested URL returned error: 403", LaunchFailed),
    ],
)
def test_git_failure_classification(tmp_path: Path, stderr: str, expected: type[Exception]) -> None:
    def _fail(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 128, stdout="", stderr=stderr)

    runner = ClaudeCloudRunner(
        ClaudeCloudConfig(config_dir=None, work_root=tmp_path / "work", repo_cache_root=tmp_path / "repos"),
        home=tmp_path / "home",
        environ=GIT_ENV,
        run=_fail,
    )
    with pytest.raises(expected) as raised:
        runner._git_ok("fetch", "origin")
    assert type(raised.value) is expected


class _WorkerExit(BaseException):
    pass


def test_worker_exit_right_after_checkout_init_leaves_base_branch_usable(tmp_path: Path) -> None:
    source, _ = _source_repo(tmp_path)
    exited = False

    def _exit_after_init(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal exited
        result = _default_run(argv, **kwargs)
        if "init" in argv and not exited:
            exited = True
            raise _WorkerExit
        return result

    runner, state_dir = _runner(tmp_path, source=source, run=_exit_after_init)
    with pytest.raises(_WorkerExit):
        runner.launch(_lane("lane-retried"))

    restarted, _ = _runner(tmp_path, source=source, bin_dir=tmp_path / "bin")
    restarted.launch(_lane("lane-retried"))
    restarted.launch(_lane("lane-next"))

    assert len(_calls(state_dir)) == 2
    assert _calls(state_dir)[-1]["head"] == _git("rev-parse", "main", cwd=source)


def test_checkout_without_origin_remote_heals_on_launch(tmp_path: Path) -> None:
    source, _ = _source_repo(tmp_path)
    runner, state_dir = _runner(tmp_path, source=source)
    checkout = runner.checkout_path_for("acme/widgets", "main")
    checkout.parent.mkdir(parents=True)
    _git("init", "--quiet", str(checkout))

    runner.launch(_lane("lane-first"))
    runner.launch(_lane("lane-second"))

    assert len(_calls(state_dir)) == 2
    assert _calls(state_dir)[-1]["head"] == _git("rev-parse", "main", cwd=source)


def test_restart_adopts_session_when_only_session_marker_survived_crash(tmp_path: Path) -> None:
    source, _ = _source_repo(tmp_path)
    runner, state_dir = _runner(tmp_path, source=source)
    lane = _lane("lane-crash-between-markers")

    # Simulate a crash right after the session marker was written and before the lane marker was written.
    first = runner.launch(lane)
    lane_marker = runner._lane_marker(lane.lane_id)
    assert lane_marker.exists()
    lane_marker.unlink()

    restarted, _ = _runner(tmp_path, source=source, bin_dir=tmp_path / "bin")
    adopted = restarted.launch(lane)
    assert adopted == first
    assert len(_calls(state_dir)) == 1
    assert restarted._lane_marker(lane.lane_id).exists()


def test_concurrent_same_lane_launches_return_single_session(tmp_path: Path) -> None:
    source, _ = _source_repo(tmp_path)
    runner, state_dir = _runner(tmp_path, source=source)
    (state_dir / "mode.json").write_text(json.dumps({"launch_delay": 0.25}), encoding="utf-8")
    lane = _lane("lane-concurrent")

    results: list[object] = []
    barrier = threading.Barrier(2)

    def _call() -> None:
        barrier.wait()
        results.append(runner.launch(lane))

    threads = [threading.Thread(target=_call), threading.Thread(target=_call)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert len(results) == 2
    assert results[0] == results[1]
    assert len(_calls(state_dir)) == 1


def test_unaccepted_workspace_trust_prompt_fails_fast_as_launch_failed(tmp_path: Path) -> None:
    runner, state_dir = _runner(tmp_path, launch_timeout=15)
    (state_dir / "mode.json").write_text(json.dumps({"trust_prompt": True}), encoding="utf-8")

    with pytest.raises(LaunchFailed, match="workspace trust is not accepted"):
        runner.launch(_lane())


def test_missing_config_dir_and_missing_binary_raise_expected_errors(tmp_path: Path) -> None:
    source, _ = _source_repo(tmp_path)
    no_cfg = ClaudeCloudRunner(
        ClaudeCloudConfig(
            config_dir=None,
            work_root=tmp_path / "work",
            repo_cache_root=tmp_path / "repos",
            repo_url_template=str(source),
            environments={"qa-prod": "ccpool_qa_prod_01"},
        ),
        home=tmp_path / "home",
        environ=GIT_ENV,
    )
    with pytest.raises(LaunchFailed, match=CONFIG_DIR_ENV):
        no_cfg.launch(_lane())
    with pytest.raises(LaunchFailed, match=CONFIG_DIR_ENV):
        no_cfg.ping()

    missing_bin = ClaudeCloudRunner(
        ClaudeCloudConfig(
            config_dir=tmp_path / "cfg",
            work_root=tmp_path / "work2",
            repo_cache_root=tmp_path / "repos2",
            claude_binary=str(tmp_path / "does-not-exist-claude"),
            repo_url_template=str(source),
            environments={"qa-prod": "ccpool_qa_prod_01"},
        ),
        home=tmp_path / "home",
        environ=GIT_ENV,
    )
    with pytest.raises(RunnerOutage, match="not installed"):
        missing_bin.launch(_lane("lane-missing-bin"))
    with pytest.raises(RunnerOutage, match="not installed"):
        missing_bin.ping()


def test_describe_reports_machine_or_unknown_and_ping_reports_version(tmp_path: Path) -> None:
    runner, state_dir = _runner(tmp_path)
    assert runner.ping() == "2.1.323"

    first = runner.launch(_lane("lane-default-machine"))
    info = runner.describe(first.agent_id)
    assert info.machine == "unknown"
    assert info.version == "claude-2.1.323"

    (state_dir / "mode.json").write_text(json.dumps({"machine": "cloud-8cpu-32gb"}), encoding="utf-8")
    second = runner.launch(_lane("lane-named-machine"))
    assert runner.describe(second.agent_id).machine == "cloud-8cpu-32gb"


def test_parse_launch_output_matches_measured_780_transcript() -> None:
    measured = (
        "\x1b[2K\rCreated cloud session: Repository top-level files\r\n"
        "View: https://claude.ai/code/session_01Fy3jwSBohiKjUVSCVbDbid?from=cli&m=0\r\n"
        "Resume with: claude --teleport session_01Fy3jwSBohiKjUVSCVbDbid\r\n"
    )
    launched, machine = parse_launch_output(measured)
    assert launched.agent_id == "session_01Fy3jwSBohiKjUVSCVbDbid"
    assert launched.url == "https://claude.ai/code/session_01Fy3jwSBohiKjUVSCVbDbid?from=cli&m=0"
    assert machine == "unknown"


def test_refuses_protected_host_paths_and_credentialed_repo_urls(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    runner = ClaudeCloudRunner(
        ClaudeCloudConfig(
            config_dir=tmp_path / "cfg",
            work_root=home / ".ssh" / "qa",
            repo_cache_root=tmp_path / "repos",
            environments={"qa-prod": "ccpool_qa_prod_01"},
        ),
        home=home,
        environ=GIT_ENV,
    )
    with pytest.raises(LaunchFailed, match="protected directory"):
        runner.launch(_lane())

    cred_runner = ClaudeCloudRunner(
        ClaudeCloudConfig(
            config_dir=tmp_path / "cfg",
            work_root=tmp_path / "work",
            repo_cache_root=tmp_path / "repos",
            repo_url_template="https://token:secret@github.com/{repo}.git",
            environments={"qa-prod": "ccpool_qa_prod_01"},
        ),
        home=home,
        environ=GIT_ENV,
    )
    with pytest.raises(LaunchFailed, match="credentials"):
        cred_runner.launch(_lane())


def test_config_from_env_reads_documented_host_settings(tmp_path: Path) -> None:
    cfg = ClaudeCloudConfig.from_env(
        {
            CONFIG_DIR_ENV: str(tmp_path / "claude-dir"),
            ENVIRONMENTS_ENV: json.dumps({"qa-prod": "ccpool_01", "qa-hosted": "/srv/claude-hosted"}),
            WORK_ROOT_ENV: str(tmp_path / "work"),
            REPO_CACHE_ENV: str(tmp_path / "cache"),
            BINARY_ENV: "/opt/bin/claude",
            LAUNCH_TIMEOUT_ENV: "240",
            STOP_TIMEOUT_ENV: "45",
            REPO_URL_TEMPLATE_ENV: "git@github.com:{repo}.git",
        }
    )
    assert cfg.config_dir == tmp_path / "claude-dir"
    assert dict(cfg.environments) == {"qa-prod": "ccpool_01", "qa-hosted": "/srv/claude-hosted"}
    assert cfg.work_root == tmp_path / "work"
    assert cfg.repo_cache_root == tmp_path / "cache"
    assert cfg.claude_binary == "/opt/bin/claude"
    assert cfg.launch_timeout_seconds == 240
    assert cfg.stop_timeout_seconds == 45
    assert cfg.repo_url_template == "git@github.com:{repo}.git"

    kv_cfg = ClaudeCloudConfig.from_env({ENVIRONMENTS_ENV: "qa-a=ccpool_a,qa-b=/srv/b"})
    assert dict(kv_cfg.environments) == {"qa-a": "ccpool_a", "qa-b": "/srv/b"}

    spec = (Path(__file__).resolve().parents[1] / "docs" / "SPEC.md").read_text(encoding="utf-8")
    for env_name in (
        CONFIG_DIR_ENV,
        ENVIRONMENTS_ENV,
        WORK_ROOT_ENV,
        REPO_CACHE_ENV,
        BINARY_ENV,
        LAUNCH_TIMEOUT_ENV,
        STOP_TIMEOUT_ENV,
        REPO_URL_TEMPLATE_ENV,
    ):
        assert env_name in spec


def test_qa_smoke_checks_claude_cloud_runner_and_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    orch = tmp_path / "orch.yml"
    orch.write_text(
        "poll_seconds: 30\n"
        "trusted_logins:\n  maxharris1: [grok-bot]\n"
        "projects:\n"
        "  - repo: acme/widgets\n"
        "    qa_storage:\n"
        "      bucket: factory-qa\n      region: us-east-1\n"
        "      access_key_env: QA_KEY\n      secret_key_env: QA_SECRET\n"
        f"      evidence_root: {evidence_root}\n",
        encoding="utf-8",
    )
    config, findings = load_orch_yml(orch)
    assert findings == []

    bin_dir = tmp_path / "bin"
    _write_fake_claude(bin_dir, tmp_path / "state")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}")
    monkeypatch.delenv(CONFIG_DIR_ENV, raising=False)
    failed = {check.name: check for check in qa_smoke.run(config, "acme/widgets", "claude-cloud")}
    assert not failed["runner"].ok
    assert CONFIG_DIR_ENV in failed["runner"].detail

    cfg_dir = tmp_path / "claude-config"
    cfg_dir.mkdir()
    monkeypatch.setenv(CONFIG_DIR_ENV, str(cfg_dir))
    passed = {check.name: check for check in qa_smoke.run(config, "acme/widgets", "claude-cloud")}
    assert all(check.ok for check in passed.values()), passed
    assert passed["runner"].detail == "claude-cloud 2.1.323"


class _CrashAfterRename(BaseException):
    pass


def _project_account(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    project_config = tmp_path / "project-claude-config"
    project_config.mkdir(exist_ok=True)
    return project_config, {"project": str(project_config)}


def _stop_accounts(state_dir: Path) -> list[str | None]:
    return [call["claude_config_dir"] for call in _calls(state_dir) if "-p" in call["argv"]]


def test_stop_after_crash_between_marker_writes_targets_the_launch_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = _source_repo(tmp_path)
    project_config, environments = _project_account(tmp_path)
    runner, state_dir = _runner(tmp_path, source=source, environments=environments)
    lane = _lane("lane-crash-after-first-rename", environment="project")
    real_replace = os.replace

    def _crash_after_first_rename(src: str, dst: str) -> None:
        real_replace(src, dst)
        raise _CrashAfterRename

    monkeypatch.setattr(os, "replace", _crash_after_first_rename)
    with pytest.raises(_CrashAfterRename):
        runner.launch(lane)
    monkeypatch.setattr(os, "replace", real_replace)

    (state_dir / "mode.json").write_text(json.dumps({"session_config_dir": str(project_config)}), encoding="utf-8")
    restarted, _ = _runner(tmp_path, source=source, bin_dir=tmp_path / "bin", environments=environments)
    adopted = restarted.launch(lane)
    restarted.stop(adopted.agent_id)

    assert adopted.agent_id == "session_01TestClaude0001"
    assert _stop_accounts(state_dir) == [str(project_config)]
    assert restarted.status(adopted.agent_id) == "ended"


@pytest.mark.parametrize("lost", ["lane", "session"])
def test_adopted_lane_stops_under_the_launch_account_whichever_marker_survived(tmp_path: Path, lost: str) -> None:
    source, _ = _source_repo(tmp_path)
    project_config, environments = _project_account(tmp_path)
    runner, state_dir = _runner(tmp_path, source=source, environments=environments)
    lane = _lane(f"lane-lost-{lost}-marker", environment="project")
    first = runner.launch(lane)
    lost_marker = runner._lane_marker(lane.lane_id) if lost == "lane" else runner._session_marker(first.agent_id)
    lost_marker.unlink()

    (state_dir / "mode.json").write_text(json.dumps({"session_config_dir": str(project_config)}), encoding="utf-8")
    restarted, _ = _runner(tmp_path, source=source, bin_dir=tmp_path / "bin", environments=environments)
    adopted = restarted.launch(lane)
    restarted.stop(adopted.agent_id)

    assert adopted == first
    assert len([call for call in _calls(state_dir) if "-p" not in call["argv"]]) == 1
    assert _stop_accounts(state_dir) == [str(project_config)]
    assert restarted.status(adopted.agent_id) == "ended"


def test_stop_with_no_recorded_account_keeps_not_found_retryable(tmp_path: Path) -> None:
    runner, state_dir = _runner(tmp_path)
    (state_dir / "mode.json").write_text(
        json.dumps({"session_config_dir": str(tmp_path / "some-other-account")}), encoding="utf-8"
    )

    with pytest.raises(LaunchFailed, match="not found"):
        runner.stop("session_01UnknownAccount")
    assert runner.status("session_01UnknownAccount") == "running"


def test_launch_takes_the_session_id_from_link_and_resume_line_not_the_title(tmp_path: Path) -> None:
    runner, state_dir = _runner(tmp_path)
    (state_dir / "mode.json").write_text(
        json.dumps({"title": "Fix claude --teleport retry behavior"}), encoding="utf-8"
    )

    launched = runner.launch(_lane())
    runner.stop(launched.agent_id)

    assert launched.agent_id == "session_01TestClaude0001"
    assert launched.url == "https://claude.ai/code/session_01TestClaude0001?from=cli&m=0"
    assert [call["argv"][3] for call in _calls(state_dir) if "-p" in call["argv"]] == ["session_01TestClaude0001"]


@pytest.mark.parametrize(
    "output",
    [
        "Created cloud session: see View: https://claude.ai/code/session_title\n"
        "View: https://claude.ai/code/session_real?from=cli&m=0\n"
        "Resume with: claude --teleport session_real\n",
        "Created cloud session: try Resume with: claude --teleport session_title\n"
        "View: https://claude.ai/code/session_real?from=cli&m=0\n"
        "Resume with: claude --teleport session_real\n",
    ],
)
def test_parse_launch_output_ignores_session_lookalikes_in_the_title(output: str) -> None:
    launched, _ = parse_launch_output(output)
    assert launched == qa_runners.Launched(
        agent_id="session_real", url="https://claude.ai/code/session_real?from=cli&m=0"
    )


@pytest.mark.parametrize(
    "output",
    [
        "Created cloud session: QA\n"
        "View: https://claude.ai/code/session_one?from=cli&m=0\n"
        "Resume with: claude --teleport session_two\n",
        json.dumps({"session_id": "session_two", "url": "https://claude.ai/code/session_one?from=cli&m=0"}),
    ],
)
def test_parse_launch_output_refuses_a_link_and_id_that_disagree(output: str) -> None:
    with pytest.raises(LaunchFailed, match="disagree"):
        parse_launch_output(output)


@pytest.mark.parametrize(
    "ssh_error",
    [
        "ssh: Could not resolve hostname repo.invalid: Name or service not known",
        "ssh: Could not resolve hostname repo.invalid: nodename nor servname provided, or not known",
        "ssh: connect to host repo.invalid port 22: Operation timed out",
        "ssh: connect to host repo.invalid port 22: No route to host",
        "kex_exchange_identification: Connection closed by remote host",
    ],
)
def test_git_ssh_network_failure_raises_runner_outage_and_falls_back(tmp_path: Path, ssh_error: str) -> None:
    fake_ssh = tmp_path / "fake-ssh"
    fake_ssh.write_text(f"#!/bin/sh\necho {json.dumps(ssh_error)} >&2\necho 'fatal: Could not read from remote repository.' >&2\nexit 255\n")
    fake_ssh.chmod(0o755)
    runner, _ = _runner(
        tmp_path / "runner",
        repo_url_template="ssh://git@repo.invalid/{repo}.git",
        environments={"full-stack": "ccpool_full_stack"},
        extra_env={"GIT_SSH_COMMAND": str(fake_ssh)},
    )
    with pytest.raises(RunnerOutage):
        runner.launch(_lane(environment="full-stack"))

    lab = Lab(tmp_path / "lab", lanes=1, runners=("claude-cloud", "cursor-cloud"))
    lab.fakes["claude-cloud"] = runner
    lab.worker = lab.new_worker()
    lab.enqueue(ids=1)
    lab.tick(3)
    assert lab.header()["runner"] == "cursor-cloud"
    assert lab.header()["result"] == "PASS"


@pytest.mark.parametrize("status", [401, 403, 404])
def test_git_http_access_denial_is_launch_failed_without_runner_backoff(tmp_path: Path, status: int) -> None:
    class _Deny(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(status)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Deny)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        runner, _ = _runner(
            tmp_path / "runner",
            repo_url_template=f"http://127.0.0.1:{server.server_address[1]}/{{repo}}.git",
            environments={"full-stack": "ccpool_full_stack"},
            extra_env={"GIT_TERMINAL_PROMPT": "0"},
        )
        with pytest.raises(LaunchFailed):
            runner.launch(_lane(environment="full-stack"))

        lab = Lab(tmp_path / "lab", lanes=1, runners=("claude-cloud",))
        lab.fakes["claude-cloud"] = runner
        lab.worker = lab.new_worker()
        lab.enqueue(ids=1)
        lab.tick(3)
        assert lab.run.rows("SELECT runner FROM qa_runner_backoff") == []
        assert [row[3:5] for row in lab.lanes()] == [("gap", "launch-failed:LaunchFailed")]
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("status", [403, 407])
def test_git_proxy_connect_denial_is_launch_failed_without_runner_backoff(tmp_path: Path, status: int) -> None:
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(0.05)
    port = listener.getsockname()[1]
    requests: list[bytes] = []
    stopping = threading.Event()

    def _serve() -> None:
        while not stopping.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(1.0)
                data = b""
                while b"\r\n\r\n" not in data:
                    try:
                        chunk = conn.recv(4096)
                    except (TimeoutError, OSError):
                        break
                    if not chunk:
                        break
                    data += chunk
                if data:
                    requests.append(data)
                    with contextlib.suppress(OSError):
                        conn.sendall(f"HTTP/1.1 {status} Denied\r\nContent-Length: 0\r\n\r\n".encode())

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        runner, _ = _runner(
            tmp_path / "runner",
            repo_url_template="https://qa-review.invalid/{repo}.git",
            environments={"full-stack": "ccpool_full_stack"},
            extra_env={"https_proxy": f"http://127.0.0.1:{port}", "GIT_TERMINAL_PROMPT": "0"},
        )
        with pytest.raises(LaunchFailed):
            runner.launch(_lane(environment="full-stack"))
        assert any(r.startswith(b"CONNECT qa-review.invalid:443 ") for r in requests), "git tunnelled through the proxy"

        lab = Lab(tmp_path / "lab", lanes=1, runners=("claude-cloud",))
        lab.fakes["claude-cloud"] = runner
        lab.worker = lab.new_worker()
        lab.enqueue(ids=1)
        lab.tick(3)
        assert lab.run.rows("SELECT runner FROM qa_runner_backoff") == []
        assert [row[3:5] for row in lab.lanes()] == [("gap", "launch-failed:LaunchFailed")]
    finally:
        stopping.set()
        listener.close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_git_ssh_permission_denied_is_launch_failed(tmp_path: Path) -> None:
    fake_ssh = tmp_path / "fake-ssh"
    fake_ssh.write_text(
        "#!/bin/sh\necho 'git@repo.invalid: Permission denied (publickey).' >&2\n"
        "echo 'fatal: Could not read from remote repository.' >&2\nexit 255\n"
    )
    fake_ssh.chmod(0o755)
    runner, _ = _runner(
        tmp_path / "runner",
        repo_url_template="ssh://git@repo.invalid/{repo}.git",
        environments={"full-stack": "ccpool_full_stack"},
        extra_env={"GIT_SSH_COMMAND": str(fake_ssh)},
    )
    with pytest.raises(LaunchFailed):
        runner.launch(_lane(environment="full-stack"))


@pytest.mark.parametrize(("old", "new"), [("release", "release/current"), ("release/current", "release")])
def test_launch_works_on_a_base_branch_renamed_upstream(tmp_path: Path, old: str, new: str) -> None:
    source, _ = _source_repo(tmp_path, branch=old)
    runner, state_dir = _runner(tmp_path, source=source)
    runner.launch(_lane("lane-before-rename", base_ref=old))
    _git("branch", "-m", old, new, cwd=source)

    runner.launch(_lane("lane-after-rename", base_ref=new))

    assert _calls(state_dir)[-1]["branch"] == new
    assert _calls(state_dir)[-1]["head"] == _git("rev-parse", new, cwd=source)


# --- Issue #1701 follow-ups from #1669 ---------------------------------------


def test_reused_checkout_removes_nested_git_repository_before_next_launch(tmp_path: Path) -> None:
    """f-6: git clean -ffdx removes untracked nested git repositories inside disposable checkouts."""
    source, _ = _source_repo(tmp_path)
    runner, state_dir = _runner(tmp_path, source=source)
    lane1 = _lane("lane-nested-1")
    runner.launch(lane1)
    checkout = runner.checkout_path_for(lane1.repo, lane1.base_ref)

    # Clone a nested git repo inside the disposable checkout between launches.
    nested = checkout / "old-project"
    _git("clone", str(source), str(nested))
    assert (nested / ".git").is_dir()

    runner.launch(_lane("lane-nested-2"))
    assert not nested.exists()
    assert _git("status", "--porcelain", cwd=checkout) == ""
    assert len([c for c in _calls(state_dir) if "--cloud" in c["argv"] and "-p" not in c["argv"]]) == 2


def test_module_docstring_has_no_unresolvable_local_doc_path() -> None:
    """f-7: module docstring does not name docs/plans/qa-cloud-lane-measurements.md absent on HEAD."""
    from lib.qa_runners import claude_cloud

    doc = claude_cloud.__doc__ or ""
    assert "docs/plans/qa-cloud-lane-measurements.md" not in doc
    assert "#780" in doc and "PR #1664" in doc


def test_tag_only_base_ref_fails_as_missing_branch_without_launching(tmp_path: Path) -> None:
    """f-11: a remote tag named `release` without a `refs/heads/release` branch fails before launch."""
    source, _ = _source_repo(tmp_path, branch="main")
    _git("tag", "release", cwd=source)
    runner, state_dir = _runner(tmp_path, source=source)

    with pytest.raises(LaunchFailed):
        runner.launch(_lane("lane-tag-only", base_ref="release"))
    assert _calls(state_dir) == []


def test_concurrent_stop_across_two_runner_instances_queues_one_message(tmp_path: Path) -> None:
    """f-12: two runner instances stopping the same session concurrently send only one stop CLI call."""
    source, _ = _source_repo(tmp_path)
    runner_a, state_dir = _runner(tmp_path, source=source)
    runner_b = ClaudeCloudRunner(runner_a.config, home=tmp_path / "home", environ=runner_a._environ)
    launched = runner_a.launch(_lane("lane-concurrent-stop"))

    (state_dir / "mode.json").write_text(json.dumps({"stop_delay": 0.4}), encoding="utf-8")
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def _call_stop(r: ClaudeCloudRunner) -> None:
        try:
            barrier.wait(timeout=5)
            r.stop(launched.agent_id)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    t1 = threading.Thread(target=_call_stop, args=(runner_a,))
    t2 = threading.Thread(target=_call_stop, args=(runner_b,))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors
    assert runner_a.status(launched.agent_id) == "ended"
    assert runner_b.status(launched.agent_id) == "ended"
    stop_calls = [c for c in _calls(state_dir) if "-p" in c["argv"]]
    assert len(stop_calls) == 1


def test_empty_reply_from_url_containing_response403_raises_runner_outage_and_falls_back(
    tmp_path: Path,
) -> None:
    """f-3 (r4 variant): a repo path containing `response403` must not mask an empty-reply outage."""
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(0.05)
    port = listener.getsockname()[1]
    stopping = threading.Event()

    def _serve() -> None:
        while not stopping.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(1.0)
                with contextlib.suppress(OSError, TimeoutError):
                    conn.recv(4096)
                # Close immediately without headers -> curl/git reports Empty reply from server.

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        runner, _ = _runner(
            tmp_path / "runner",
            repo_url_template=f"http://127.0.0.1:{port}/example/response403.git",
            environments={"full-stack": "ccpool_full_stack"},
        )
        lane = _lane("lane-response403", repo="example/response403", environment="full-stack")
        with pytest.raises(RunnerOutage):
            runner.launch(lane)

        lab = Lab(tmp_path / "lab", lanes=1, runners=("claude-cloud", "cursor-cloud"))
        lab.fakes["claude-cloud"] = runner
        lab.worker = lab.new_worker()
        lab.enqueue(ids=1)
        lab.tick(3)
        assert lab.header()["runner"] == "cursor-cloud"
        assert lab.header()["result"] == "PASS"
    finally:
        stopping.set()
        listener.close()
        thread.join(timeout=2)
        assert not thread.is_alive()


# --- Issue #1731 follow-ups from #1730 ---------------------------------------


def test_sha_named_remote_branch_launches_at_branch_tip(tmp_path: Path) -> None:
    """f-1: a configured remote branch whose name is a 40-char SHA launches at that branch's tip."""
    source, commit1 = _source_repo(tmp_path, branch="main")
    (source / "second.txt").write_text("second\n", encoding="utf-8")
    _git("add", "second.txt", cwd=source)
    _git("commit", "-m", "second", cwd=source)
    commit2 = _git("rev-parse", "HEAD", cwd=source)
    assert commit1 != commit2

    # Name a branch after commit1's full lowercase SHA and point it at commit2.
    _git("branch", commit1, commit2, cwd=source)
    runner, state_dir = _runner(tmp_path, source=source)

    runner.launch(_lane("lane-sha-branch", base_ref=commit1))
    call = _calls(state_dir)[-1]
    assert call["head"] == commit2
    assert call["branch"] == commit1

    # Once that branch is deleted, passing commit1 still falls back to detached HEAD at commit1.
    _git("branch", "-D", commit1, cwd=source)
    runner.launch(_lane("lane-raw-sha", base_ref=commit1))
    detached_call = _calls(state_dir)[-1]
    assert detached_call["head"] == commit1
    assert detached_call["branch"] == "HEAD"


def test_missing_session_marker_recovery_preserves_stop_acknowledged_before_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """f-2: stopping a session while missing-marker recovery is paused keeps `ended` and sends one stop."""
    source, _ = _source_repo(tmp_path)
    runner_a, state_dir = _runner(tmp_path, source=source)
    lane = _lane("lane-recover-race-1")
    launched = runner_a.launch(lane)

    session_marker = runner_a._session_marker(launched.agent_id)
    assert session_marker.exists()
    session_marker.unlink()

    runner_b = ClaudeCloudRunner(runner_a.config, home=tmp_path / "home", environ=runner_a._environ)
    runner_c = ClaudeCloudRunner(runner_a.config, home=tmp_path / "home", environ=runner_a._environ)

    entered = threading.Event()
    proceed = threading.Event()
    orig_session_lock = runner_b._session_lock

    def paused_session_lock(agent_id: str) -> Path:
        entered.set()
        assert proceed.wait(timeout=5)
        return orig_session_lock(agent_id)

    monkeypatch.setattr(runner_b, "_session_lock", paused_session_lock)

    recovered: list[Launched] = []
    errors: list[BaseException] = []

    def _recover() -> None:
        try:
            recovered.append(runner_b.launch(lane))
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    t = threading.Thread(target=_recover)
    t.start()
    assert entered.wait(timeout=5)

    # Stop the session by ID while runner_b is paused right before acquiring the session lock.
    runner_c.stop(launched.agent_id)
    assert runner_c.status(launched.agent_id) == "ended"

    proceed.set()
    t.join(timeout=10)
    assert not errors
    assert recovered == [launched]
    assert runner_b.status(launched.agent_id) == "ended"
    assert runner_c.status(launched.agent_id) == "ended"

    # Calling stop again on the recovered ID must be a no-op (still 1 stop CLI call total).
    runner_b.stop(recovered[0].agent_id)
    stop_calls = [c for c in _calls(state_dir) if "-p" in c["argv"]]
    assert len(stop_calls) == 1


def test_missing_session_marker_recovery_serializes_against_concurrent_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """f-2: holding the session lock during missing-marker write serializes concurrent stop."""
    from lib.qa_runners import claude_cloud

    source, _ = _source_repo(tmp_path)
    runner_a, state_dir = _runner(tmp_path, source=source)
    lane = _lane("lane-recover-race-2")
    launched = runner_a.launch(lane)

    session_marker = runner_a._session_marker(launched.agent_id)
    session_marker.unlink()

    runner_b = ClaudeCloudRunner(runner_a.config, home=tmp_path / "home", environ=runner_a._environ)
    runner_c = ClaudeCloudRunner(runner_a.config, home=tmp_path / "home", environ=runner_a._environ)

    entered = threading.Event()
    proceed = threading.Event()
    orig_write = claude_cloud._write_json_if_absent

    def paused_write(path: Path, data: Mapping[str, object]) -> bool:
        if path == session_marker:
            entered.set()
            assert proceed.wait(timeout=5)
        return orig_write(path, data)

    monkeypatch.setattr(claude_cloud, "_write_json_if_absent", paused_write)

    errors: list[BaseException] = []
    stop_done = threading.Event()

    def _recover() -> None:
        try:
            runner_b.launch(lane)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    def _stop() -> None:
        try:
            runner_c.stop(launched.agent_id)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)
        finally:
            stop_done.set()

    t_rec = threading.Thread(target=_recover)
    t_rec.start()
    assert entered.wait(timeout=5)

    t_stop = threading.Thread(target=_stop)
    t_stop.start()
    # stop() must wait for recovery's session lock rather than racing ahead of it.
    assert not stop_done.wait(timeout=0.2)

    proceed.set()
    t_rec.join(timeout=10)
    t_stop.join(timeout=10)
    assert not errors
    assert runner_b.status(launched.agent_id) == "ended"
    assert runner_c.status(launched.agent_id) == "ended"

    runner_b.stop(launched.agent_id)
    stop_calls = [c for c in _calls(state_dir) if "-p" in c["argv"]]
    assert len(stop_calls) == 1
