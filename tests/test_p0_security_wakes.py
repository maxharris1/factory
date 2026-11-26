"""PR 0.3 regression tests: security boundaries, env scrubbing, secret scan, dashboard HTTPS, wakes."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import pwd
import subprocess
import urllib.error
from pathlib import Path

import pytest

from lib import (
    codex_sandbox,
    grok_delivery,
    harness_env,
    orch_yml,
    pre_ready,
    secret_scan,
    signin_relay,
)
from lib.attention import DRIVER_WAKE_UNANSWERED
from lib.dispatcher import Dispatcher
from lib.job_state import JobStateConfig
from lib.orch_yml import Approvals
from lib.queue import JobRequest, SQLiteJobQueue
from tests.test_dispatcher import FakeReviewActions

ROOT = Path(__file__).resolve().parents[1]


def _load_dashboard_server():
    path = ROOT / "deploy" / "factory-dashboard-http.py"
    spec = importlib.util.spec_from_file_location("factory_dashboard_http_p03", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_secret_scan_detects_xai_linear_google_jwt_and_job_proof() -> None:
    diff = "\n".join(
        [
            "diff --git a/app.py b/app.py",
            "--- a/app.py",
            "+++ b/app.py",
            "@@ -1,0 +1,5 @@",
            "+XAI = 'xai-1234567890abcdefghijklmno'",
            "+LIN = 'lin_api_1234567890abcdefghijklmno'",
            "+GOOG = 'AIzaSyAbCdEfGhIjKlMnOpQrStUvWxYz1234567'",
            "+TOK = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c'",
            "+PROOF = 'ORCH_JOB_PROOF=abcdefghijklmnop1234'",
        ]
    ).encode("utf-8")
    result = secret_scan.scan_added(diff)
    assert result.status == "found"
    names = {hit.pattern for hit in result.hits}
    assert {"xai-api-key", "linear-api-key", "google-api-key", "jwt", "orch-job-proof"} <= names


def test_harness_env_isolates_claude_securestorage_and_pre_ready_scrubs_secrets() -> None:
    base = {
        "PATH": "/usr/bin",
        "CLAUDE_SECURESTORAGE_CONFIG_DIR": "/tmp/other-acct",
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "LINEAR_API_KEY": "lin_api_secret",
        "FACTORY_WEBHOOK_URL": "https://example.invalid/hook",
        "FACTORY_WEBHOOK_KEY": "secret",
        "FACTORY_CLAUDE_FIRE_TOKEN": "secret",
        "QA_S3_SECRET_ACCESS_KEY": "secret",
        "ORCH_DB": "/state/orch.db",
        "ORCH_JOB_PROOF": "proof123",
    }
    acct_env = harness_env.account_environ(base, "claude", "/home/claude-b")
    assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" not in acct_env
    assert acct_env["CLAUDE_CONFIG_DIR"] == "/home/claude-b"

    chk_env = pre_ready.check_environ(base, None)
    for leaked in (
        "LINEAR_API_KEY",
        "FACTORY_WEBHOOK_URL",
        "FACTORY_WEBHOOK_KEY",
        "FACTORY_CLAUDE_FIRE_TOKEN",
        "QA_S3_SECRET_ACCESS_KEY",
        "ORCH_DB",
        "ORCH_JOB_PROOF",
    ):
        assert leaked not in chk_env


def test_signin_relay_local_and_ssh_enforce_expected_owner(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    relay_dir = tmp_path / "relay"
    spool.mkdir()
    relay_dir.mkdir()
    me = pwd.getpwuid(os.getuid()).pw_name
    rid = "a" * 20

    # Matching owner succeeds.
    (relay_dir / f"{rid}.code").write_bytes(b"123456")
    ok_relay = signin_relay.LocalRelay(spool, relay_dir, expected_owner=me)
    assert ok_relay.take(rid, "code") == b"123456"

    # Mismatched owner is rejected and temp file unlinked.
    (relay_dir / f"{rid}.code").write_bytes(b"999999")
    bad_relay = signin_relay.LocalRelay(spool, relay_dir, expected_owner="definitely-not-me")
    with pytest.raises(signin_relay.SpoolError, match="unexpected owner|owned by"):
        bad_relay.take(rid, "code")
    assert not list(relay_dir.iterdir())

    # SshRelay injects owner check into _TAKE script when expected_owner is set.
    seen_scripts: list[str] = []

    class FakeTransport:
        def exchange(self, argv, **_kwargs):
            seen_scripts.append(argv[-1])
            return signin_relay.Exchanged(0, b"+ok", "")

    ssh_relay = signin_relay.SshRelay(
        "box", "factory", "/spool", "/relay",
        transport=FakeTransport(), expected_owner="dash-sync",
    )
    assert ssh_relay.take(rid, "code") == b"ok"
    assert "stat -c %U" in seen_scripts[0] and "dash-sync" in seen_scripts[0]


def test_grok_delivery_accepts_201_202_and_preserves_429_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # _post preserves headers from HTTPError (including Retry-After).
    class FakeOpener:
        def open(self, _req, timeout=None):
            raise urllib.error.HTTPError(
                "https://example.invalid/hook", 429, "Too Many Requests",
                {"Retry-After": "45"}, io.BytesIO(b""),
            )

    monkeypatch.setattr(grok_delivery, "_OPENER", FakeOpener())
    res = grok_delivery.send_webhook("https://example.invalid/hook", "Bearer tok", b"{}", 5.0)
    assert res.status == 429
    assert res.headers.get("Retry-After") == "45"

    # 201 and 202 succeed; 429 raises GrokDeliveryError with retry_after=45.
    job = SQLiteJobQueue(Path(":memory:"), clock=lambda: 100.0)
    del job
    req = JobRequest(
        repo="owner/repo", ticket_id="1", action="clarify", recipient="grok",
        source_comment_id="c1", timeout_seconds=600, generation=1,
    )
    from lib.queue import Job
    fake_job = Job(
        id="j1", dedupe_key="k1", request=req, state="running",
        created_at=100.0, last_activity_at=100.0, finished_at=None, handoff_comment_id=None,
    )
    for code in (200, 201, 202):
        delivery = grok_delivery.WebhookGrokDelivery(
            url_env="U", key_env="K",
            environ={"U": "https://example.invalid/hook", "K": "tok"},
            transport=lambda *_a, c=code: grok_delivery.WebhookResult(c, {"X-Request-Id": f"r-{c}"}),
        )
        assert delivery.deliver(fake_job, logical_key="lk") == f"r-{code}"

    rl_delivery = grok_delivery.WebhookGrokDelivery(
        url_env="U", key_env="K",
        environ={"U": "https://example.invalid/hook", "K": "tok"},
        transport=lambda *_a: grok_delivery.WebhookResult(429, {"Retry-After": "45"}),
    )
    with pytest.raises(grok_delivery.GrokDeliveryError) as exc_info:
        rl_delivery.deliver(fake_job, logical_key="lk")
    assert exc_info.value.status == 429
    assert exc_info.value.retry_after == 45.0


def test_dispatcher_pauses_remaining_wakes_on_429_and_backstops_undelivered_critical(
    tmp_path: Path,
) -> None:
    now = [1_000.0]
    db = tmp_path / "orch.db"
    queue = SQLiteJobQueue(db, clock=lambda: now[0])
    attempts: list[str] = []

    class RateLimitDelivery:
        def deliver(self, job, *, logical_key: str) -> str:
            attempts.append(job.id)
            raise grok_delivery.GrokDeliveryError(
                "HTTP 429", detail="HTTP-429", status=429, retry_after=120.0
            )

    dispatcher = Dispatcher(
        db, queue,
        trusted_logins={"github": {"grok-login": frozenset({"grok-bot"})}},
        review_actions=FakeReviewActions(),
        grok_delivery=RateLimitDelivery(),
        job_states=JobStateConfig(driver_wake_seconds=300.0),
    )
    for idx in (1, 2):
        queue.enqueue(
            JobRequest(
                repo="owner/repo", ticket_id=str(idx), action="stuck", recipient="grok",
                source_comment_id=f"c{idx}", timeout_seconds=3600, generation=1,
            ),
            dedupe_key=f"stuck-{idx}",
        )
    dispatcher._deliver_grok_jobs(set())
    # Must stop after the first 429 instead of hammering the second queued wake.
    assert len(attempts) == 1

    # Still backed off at +60s -> zero new delivery attempts.
    now[0] = 1_060.0
    dispatcher._deliver_grok_jobs(set())
    assert len(attempts) == 1

    # Once past driver_wake_seconds (300s) with critical wakes still queued, backstop flags needs_attention.
    now[0] = 1_400.0
    dispatcher._backstop_undelivered_critical_wakes()
    with queue.connect() as conn:
        rows = conn.execute(
            "SELECT ticket_id, reason, detail FROM needs_attention WHERE reason = ? ORDER BY ticket_id",
            (DRIVER_WAKE_UNANSWERED,),
        ).fetchall()
    assert len(rows) == 2
    assert all("stuck wake" in r["detail"] and "undelivered" in r["detail"] for r in rows)


def test_bot_approver_overlap_is_error_when_github_app_enabled(tmp_path: Path) -> None:
    from tests.test_github_apps_config import load, orch_text, two_worker_apps

    # Full app config with approver still carrying a harness bot -> error.
    cfg_full, _ = load(
        tmp_path,
        orch_text(enabled=True, trust={"maxharris1": "[claude-bot]"}),
    )
    assert cfg_full is not None
    findings_full = orch_yml.bot_approver_findings(cfg_full)
    assert findings_full and all(f.severity == "error" for f in findings_full)

    # Staged 2-app rollout where only an unmigrated bot shares approver -> warning.
    cfg_staged, _ = load(tmp_path, two_worker_apps(enabled=True))
    assert cfg_staged is not None
    findings_staged = orch_yml.bot_approver_findings(cfg_staged)
    assert findings_staged and all(f.severity == "warning" for f in findings_staged)

    # Switch off -> warning even when overlap exists.
    cfg_off, _ = load(
        tmp_path,
        orch_text(enabled=False, trust={"maxharris1": "[claude-bot]"}),
    )
    assert cfg_off is not None
    findings_off = orch_yml.bot_approver_findings(cfg_off)
    assert findings_off and all(f.severity == "warning" for f in findings_off)


def test_dashboard_cookie_secure_flag_and_approver_headers() -> None:
    server = _load_dashboard_server()
    assert "; Secure" not in server.cookie("factory_session", "abc", 60, secure=False)
    assert server.cookie("factory_session", "abc", 60, secure=True).endswith("; Secure")
    for header in (
        server.GO_HEADER,
        server.ANSWERS_HEADER,
        server.CLOSE_HEADER,
        server.RETRY_HEADER,
        server.HANDOFF_HEADER,
    ):
        assert "from=approver role=approver" in header
        assert "from=grok-bot" not in header


def test_self_heal_scopes_webhook_env_to_escalating_commands_only(tmp_path: Path) -> None:
    script = (ROOT / "deploy" / "self-heal.sh").read_text(encoding="utf-8")
    assert "with_webhook_env()" in script
    # Top-level once() must not unconditionally source WEBHOOK_ENV before app-token-service.sh.
    once_body = script.split("once() {", 1)[1].split("\n}\n", 1)[0]
    assert 'with_webhook_env "$ORCH" heal deploy' in once_body
    assert 'with_webhook_env "$ORCH" heal --health-json' in once_body
    assert '"${HERE}/app-token-service.sh" once' in once_body
    assert 'with_webhook_env "${HERE}/app-token-service.sh"' not in once_body


def test_codex_sandbox_is_sandboxed_exec_detection() -> None:
    assert codex_sandbox.is_sandboxed_exec(["codex", "exec", "--sandbox", "workspace-write", "-"])
    assert codex_sandbox.is_sandboxed_exec(["codex", "exec", "--full-auto", "-"])
    assert not codex_sandbox.is_sandboxed_exec(["codex", "exec", "--sandbox", "read-only", "-"])
    assert not codex_sandbox.is_sandboxed_exec(["claude", "-p"])


def test_local_sandboxed_codex_keeps_job_running_while_worker_lives(tmp_path: Path) -> None:
    """Codex PR #1537 f-1: local marker run must not fail while its worker PID is alive."""
    import subprocess
    from lib.waiter import HarnessCommand, Waiter
    from tests.test_waiter import FakeContext, FakeSpawn, MACHINES, enqueue, local_pool, make_queue, state

    db, queue = make_queue(tmp_path)
    job_id = enqueue(queue, "liveness", recipient="codex")
    spawn = FakeSpawn()
    waiter = Waiter(
        bot="codex-bot",
        queue=queue,
        pool=local_pool(queue),
        harness=HarnessCommand(bot="codex-bot", command=("codex", "exec", "-s", "workspace-write", "-")),
        context=FakeContext(),
        state_dir=tmp_path / "state",
        db=db,
        machines=MACHINES,
        orch=("python3", "/unused/orch"),
        spawn=spawn,
        environ={},
    )
    worker = subprocess.Popen(["sleep", "60"])
    try:
        assert waiter.step() == job_id
        child, proof = waiter._children[job_id]
        waiter.pool.attach_runner(
            job_id, owner="codex", pid=child.pid, start=None, alive=lambda *_: False, proof=proof
        )
        waiter.pool.attach_worker(job_id, owner="codex", pid=worker.pid, proof=proof)
        child.returncode = -9
        waiter._reap()
        assert state(db, job_id) == "running"
    finally:
        worker.terminate()
        worker.wait()
        spawn.end_runners()


def test_webhook_rate_limit_does_not_starve_inbox_only_project(tmp_path: Path) -> None:
    """Codex PR #1537 f-2: webhook backoff defers webhook wakes while still posting inbox-only wakes."""
    from lib.dispatcher import Dispatcher
    from lib.grok_delivery import GrokDeliveryError
    from lib.orch_yml import DriverInbox, Project
    from lib.queue import JobRequest, SQLiteJobQueue

    posts: list[str] = []

    class LimitedDelivery:
        def deliver(self, job, *, logical_key):
            raise GrokDeliveryError("limited", detail="HTTP-429", status=429, retry_after=3600)

    class Poster:
        def post_issue_comment(self, repo, number, body):
            posts.append(body)
            return "posted"

        def issue_comments(self, repo, number):
            return ()

    now = [1000.0]
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: now[0])
    dispatcher = Dispatcher(
        queue.path,
        queue,
        trusted_logins={"github": {"driver": frozenset({"grok-bot"})}},
        review_actions=Poster(),
        grok_delivery=LimitedDelivery(),
        projects=(Project(repo="org/inbox", driver_inbox=DriverInbox("org/inbox", 99), grok_webhook=False),),
    )
    for n, repo in [("1", "org/webhook"), ("2", "org/webhook"), ("3", "org/inbox")]:
        queue.enqueue(
            JobRequest(
                repo=repo,
                ticket_id=n,
                action="blocked",
                recipient="grok",
                source_comment_id=n,
                timeout_seconds=100000,
            ),
            dedupe_key=n,
        )
        now[0] += 1
    for _ in range(3):
        dispatcher.reconcile_jobs()
        now[0] += 30
    assert len(posts) == 1
    with queue.connect() as conn:
        states = dict(conn.execute("SELECT ticket_id, state FROM jobs").fetchall())
    assert states == {"1": "queued", "2": "queued", "3": "done"}


def test_require_auth_reads_protects_html_and_qa_routes_without_exposing_snapshot(tmp_path: Path) -> None:
    """Codex PR #1537 f-3: require_auth_reads must protect snapshot HTML & QA routes, not just JSON."""
    from types import SimpleNamespace
    from lib.dashboard import write_snapshot_pages
    from tests.test_dashboard import snapshot_for

    server = _load_dashboard_server()
    write_snapshot_pages(tmp_path / "site", snapshot_for(tmp_path))
    handler = object.__new__(server.SnapshotHandler)
    cfg = server.ActionsConfig("synthetic", "human", frozenset(), require_auth_reads=True)
    handler.server = SimpleNamespace(
        actions=SimpleNamespace(config=cfg, current_with_renewal=lambda *_: (None, False)),
        root_fd=server.open_root(tmp_path / "site"),
    )
    handler.client_address = ("127.0.0.1", 0)
    handler._cookie = lambda _: None
    out: list[tuple[int, bytes]] = []
    handler._send = lambda code, data, *a, **kw: out.append((code, data))
    handler._json = lambda code, data: out.append((code, json.dumps(data).encode()))
    try:
        for route in ("/status.json", "/health.html", "/qa/runs/r1"):
            handler.path = route
            handler._serve(body=True)
        assert [code for code, _ in out] == [401, 401, 401]
        assert b"cycle line 79" not in out[1][1]
        assert b"Sign in with GitHub" in out[1][1]
    finally:
        os.close(handler.server.root_fd)


def test_dashboard_page_script_bootstraps_session_on_401(tmp_path: Path) -> None:
    """Codex PR #1537 f-4: 401 from status.json must still bootstrap /api/session for sign-in."""
    import shutil
    import subprocess
    from lib.dashboard_page import page_script

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    script = r"""
const vm = require('vm');
const calls = [];
const ctx = {
  console,
  document: {
    body: {setAttribute:()=>{}, getAttribute:n=>({'data-page':'settings','data-refresh':'20','data-generated':'1'}[n])},
    getElementById:()=>null, querySelectorAll:()=>[], addEventListener:()=>{}, hidden:false
  },
  window:{addEventListener:()=>{}}, location:{hash:'',reload:()=>{}},
  setTimeout:()=>1, clearTimeout:()=>{}, setInterval:()=>1,
  fetch:url=>{calls.push(url); return Promise.resolve({ok:false,status:401});}
};
vm.runInNewContext(SOURCE, ctx);
setImmediate(()=>{
  if (!calls.includes('/api/session')) {
    console.error('fetch calls:', JSON.stringify(calls));
    process.exit(1);
  }
});
""".replace("SOURCE", json.dumps(page_script(None)))
    proc = subprocess.run([node], input=script, text=True, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr
