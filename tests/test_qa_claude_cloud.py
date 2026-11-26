"""#785: unit and integration tests for the Claude Code cloud QA runner."""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from lib import qa_runners, qa_smoke
from lib.factory_md import QA_RUNNERS, QaRunner, parse_factory_md
from lib.handoff import parse_comment
from lib.orch_yml import load_orch_yml
from lib.qa_runners import Lane, LaunchFailed, RunnerOutage, UsageLimit
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
    if mode.get("stop_error"):
        err = mode["stop_error"]
        sys.stderr.write(err["text"] + "\\n")
        sys.exit(err.get("code", 1))
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
        print(f"Created cloud session: QA run {{n}}")
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
    return ClaudeCloudRunner(cfg, home=tmp_path / "home", environ=env), state_dir


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
