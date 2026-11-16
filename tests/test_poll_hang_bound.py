from __future__ import annotations

import re
import socket
import socketserver
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from lib.dispatch.cycle_clock import CycleClock
from lib.dispatcher import Dispatcher
from lib.feeder_gates import github_ref
from lib.github_actions import MARK_READY_QUERY
from lib.intake_live import GitHubIntakeClient
from lib.orch_yml import LinearConfig, OrchConfig, Project
from lib.poll_source import (
    HANG_STRIKES,
    HTTPResult,
    LivePollSource,
    PollCycleError,
    RetryablePollSourceError,
    post_json,
)
from lib.queue import SQLiteJobQueue
from tests.test_live_poll import FACTORY, FakeGh

DEADLINE = 0.3
ATTEMPT = 0.2
SLACK = 1.5
BOUND = HANG_STRIKES * DEADLINE + SLACK
GITHUB = tuple(str(number) for number in range(12, 18))
LINEAR = ("AUT-1", "AUT-2", "AUT-3", "AUT-4")


def mixed_config() -> OrchConfig:
    return OrchConfig(
        poll_seconds=60,
        projects=(
            Project("acme/widgets", factory_ref="main"),
            Project(
                "acme/linear",
                factory_ref="main",
                linear_project_ids=frozenset({"project-1"}),
            ),
        ),
        linear=LinearConfig("LINEAR_KEY"),
        trusted_logins={"grok-login": frozenset({"grok-bot"})},
    )


def github_config() -> OrchConfig:
    return OrchConfig(poll_seconds=60, projects=(Project("acme/widgets", factory_ref="main"),))


def tracked_dispatcher(tmp_path: Path, refs) -> Dispatcher:
    database = tmp_path / "orch.db"
    queue = SQLiteJobQueue(database, clock=lambda: 10)
    dispatcher = Dispatcher(
        database,
        queue,
        trusted_logins={"github": {"grok-login": {"grok-bot"}}, "linear": {}},
        review_actions=Mock(),
        grok_delivery=Mock(),
    )
    with queue.connect() as connection:
        connection.executemany(
            "INSERT INTO tracked_tickets (repo, ticket_id, tracker, tracked_at) VALUES (?, ?, ?, 0)",
            refs,
        )
    dispatcher.process = Mock()
    dispatcher.reconcile_jobs = Mock()
    return dispatcher


def hanging_gh(calls: list[float]):
    def runner(argv, timeout):
        calls.append(timeout)
        time.sleep(timeout)
        raise subprocess.TimeoutExpired(argv, timeout)

    return runner


def processed(dispatcher: Dispatcher) -> list[str]:
    return [call.args[0].ticket_id for call in dispatcher.process.call_args_list]


def test_a_hanging_gh_read_ends_the_cycle_within_the_bound_and_is_read_next_cycle(
    tmp_path: Path,
) -> None:
    dispatcher = tracked_dispatcher(
        tmp_path, [("acme/widgets", number, "github") for number in GITHUB]
    )
    calls: list[float] = []
    healthy = FakeGh(FACTORY.format(tickets="github", ship="merge"))
    hanging = [True]
    warnings: list[str] = []

    def runner(argv, timeout):
        if hanging[0]:
            return hanging_gh(calls)(argv, timeout)
        return healthy(argv, timeout)

    source = LivePollSource(
        github_config(),
        gh_runner=runner,
        error_sink=dispatcher.record_poll_error,
        gh_call_timeout_seconds=ATTEMPT,
        read_deadline_seconds=DEADLINE,
    )
    source.warning_sink = warnings.append

    began = time.monotonic()
    with pytest.raises(PollCycleError):
        dispatcher.poll_once(source)
    elapsed = time.monotonic() - began

    assert elapsed < BOUND, elapsed
    assert sum(calls) <= HANG_STRIKES * DEADLINE + 1e-6
    assert processed(dispatcher) == []
    assert warnings == [
        f"github skipped for the rest of this cycle after {HANG_STRIKES} "
        "timed-out reads; read again next cycle"
    ]
    with dispatcher._connect() as connection:
        errored = {row[0] for row in connection.execute("SELECT ticket_id FROM poll_errors")}
    assert set(GITHUB) <= errored

    hanging[0] = False
    dispatcher.poll_once(source)

    assert processed(dispatcher) == list(GITHUB)


def test_a_hanging_linear_read_ends_the_cycle_within_the_bound_and_github_goes_on(
    tmp_path: Path,
) -> None:
    dispatcher = tracked_dispatcher(
        tmp_path,
        [("acme/widgets", "12", "github")]
        + [("acme/linear", key, "linear") for key in LINEAR],
    )
    never = threading.Event()
    linear_calls: list[float] = []

    def transport(url, headers, body, timeout):
        linear_calls.append(timeout)
        never.wait()

    source = LivePollSource(
        mixed_config(),
        environ={"LINEAR_KEY": "key"},
        gh_runner=FakeGh(FACTORY.format(tickets="github", ship="merge")),
        linear_transport=transport,
        error_sink=dispatcher.record_poll_error,
        read_deadline_seconds=DEADLINE,
    )

    began = time.monotonic()
    try:
        with pytest.raises(PollCycleError, match="acme/linear x4"):
            dispatcher.poll_once(source)
    finally:
        never.set()
    elapsed = time.monotonic() - began

    assert elapsed < BOUND, elapsed
    assert len(linear_calls) == HANG_STRIKES
    assert all(0 < timeout < DEADLINE for timeout in linear_calls), linear_calls
    assert processed(dispatcher) == ["12"]


def test_a_hanging_intake_read_skips_github_after_the_strikes() -> None:
    calls: list[float] = []
    source = LivePollSource(
        github_config(),
        gh_runner=hanging_gh(calls),
        gh_call_timeout_seconds=ATTEMPT,
        read_deadline_seconds=DEADLINE,
    )
    source.begin_cycle()
    intake = GitHubIntakeClient(runner=source._adapter_runner)

    began = time.monotonic()
    for repo in ("acme/widgets", "acme/gadgets", "acme/tools", "acme/more"):
        with pytest.raises(Exception, match="timed out|skipped"):
            intake.github_intake(repo, "factory:ready")
    elapsed = time.monotonic() - began

    assert elapsed < BOUND, elapsed
    assert sum(calls) <= HANG_STRIKES * DEADLINE + 1e-6


def test_the_feeder_reads_github_through_the_bounded_poll_runner(tmp_path: Path) -> None:
    from lib.cli import _feeder

    source = LivePollSource(mixed_config())
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 10)

    feeder = _feeder(mixed_config(), queue, None, source, [])

    assert feeder.github.runner == source._adapter_runner


def test_the_cycle_line_names_the_reads_inside_feed_and_tracked_read(
    tmp_path: Path,
) -> None:
    dispatcher = tracked_dispatcher(
        tmp_path, [("acme/widgets", number, "github") for number in GITHUB[:3]]
    )
    gh = FakeGh(FACTORY.format(tickets="github", ship="merge"))
    now = [0.0]

    def slow(argv, timeout):
        now[0] += 1.0
        return gh(argv, timeout)

    source = LivePollSource(github_config(), gh_runner=slow)
    project = source.config.projects[0]
    dispatcher.feeder = SimpleNamespace(
        feed_once=lambda: (source.factory_context(project), SimpleNamespace(fed=()))[1]
    )
    clock = CycleClock(wall=lambda: now[0])
    dispatcher.cycle_clock = clock
    clock.start()
    dispatcher.poll_once(source)
    line = clock.end()

    for phase in ("feed", "tracked-read"):
        match = re.search(rf"{phase} ([\d.]+)s \[([^\]]*)\]", line)
        assert match, line
        named = sum(float(seconds) for seconds in re.findall(r" ([\d.]+)s", match.group(2)))
        assert "acme/widgets" in match.group(2)
        assert float(match.group(1)) > 0
        assert named >= 0.9 * float(match.group(1)), line


def test_blocker_status_queries_take_the_bounded_read_path() -> None:
    calls: list[tuple[str, float]] = []

    def runner(argv, timeout):
        calls.append(("graphql" if "graphql" in argv else "rest", timeout))
        time.sleep(timeout)
        raise subprocess.TimeoutExpired(argv, timeout)

    source = LivePollSource(
        github_config(),
        gh_runner=runner,
        gh_call_timeout_seconds=ATTEMPT,
        read_deadline_seconds=DEADLINE,
    )
    intake = GitHubIntakeClient(runner=source._adapter_runner)
    blocker = {github_ref("acme/widgets", 5)}

    source.begin_cycle()
    began = time.monotonic()
    for repo in ("acme/widgets", "acme/gadgets"):
        with pytest.raises(Exception, match="timed out"):
            intake.github_intake(repo, "factory:ready")
    with pytest.raises(Exception, match="skipped"):
        intake.ref_statuses(blocker)
    elapsed = time.monotonic() - began

    assert elapsed < BOUND, elapsed
    assert [kind for kind, _ in calls] == ["rest"] * HANG_STRIKES

    calls.clear()
    source.begin_cycle()
    with pytest.raises(Exception, match="timed out"):
        intake.ref_statuses(blocker)
    assert calls and all(kind == "graphql" and timeout <= ATTEMPT for kind, timeout in calls)
    assert sum(timeout for _, timeout in calls) <= DEADLINE + 1e-6



def test_a_graphql_mutation_keeps_the_write_timeout_and_runs_once() -> None:
    timeouts: list[float] = []

    def runner(argv, timeout):
        timeouts.append(timeout)
        raise subprocess.TimeoutExpired(argv, timeout)

    source = LivePollSource(
        github_config(),
        gh_runner=runner,
        gh_call_timeout_seconds=ATTEMPT,
        read_deadline_seconds=DEADLINE,
    )
    source.begin_cycle()

    with pytest.raises(subprocess.TimeoutExpired):
        source._adapter_runner(
            ["gh", "api", "-X", "POST", "graphql", "-f", f"query={MARK_READY_QUERY}"],
            ATTEMPT,
        )

    assert timeouts == [source.timeout_seconds]


DOCUMENT = {"query": "query { viewer { id } }"}
HEALTHY = b'{"data": {"viewer": {"id": "me"}}}'


@pytest.fixture
def trickling_linear():
    state = SimpleNamespace(trickle=True, open=0, lock=threading.Lock())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            if not state.trickle:
                self.send_response(200)
                self.send_header("Content-Length", str(len(HEALTHY)))
                self.end_headers()
                self.wfile.write(HEALTHY)
                return
            self.send_response(200)
            self.send_header("Content-Length", "1000000")
            self.end_headers()
            with state.lock:
                state.open += 1
            try:
                while state.trickle:
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.02)
            except OSError:
                pass
            finally:
                with state.lock:
                    state.open -= 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state.url = f"http://127.0.0.1:{server.server_address[1]}/graphql"
    try:
        yield state
    finally:
        state.trickle = False
        server.shutdown()
        server.server_close()


def linear_source(transport) -> LivePollSource:
    return LivePollSource(
        mixed_config(),
        environ={"LINEAR_KEY": "key"},
        linear_transport=transport,
        read_deadline_seconds=DEADLINE,
    )


def eventually(predicate, seconds: float = 2.0) -> bool:
    stop = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > stop:
            return False
        time.sleep(0.02)
    return True


def test_timed_out_linear_reads_close_their_connections_and_a_later_cycle_reads(
    trickling_linear,
) -> None:
    source = linear_source(
        lambda url, headers, body, timeout: post_json(trickling_linear.url, headers, body, timeout)
    )

    for _ in range(4):
        source.begin_cycle()
        began = time.monotonic()
        for _ in range(HANG_STRIKES + 1):
            with pytest.raises(RetryablePollSourceError, match="timed out|skipped"):
                source._linear_request(DOCUMENT)
        assert time.monotonic() - began < BOUND

    assert eventually(lambda: trickling_linear.open == 0), trickling_linear.open

    trickling_linear.trickle = False
    source.begin_cycle()
    assert source._linear_request(DOCUMENT) == {"data": {"viewer": {"id": "me"}}}


def test_a_linear_transport_that_never_returns_cannot_pile_up_calls_across_cycles() -> None:
    released = threading.Event()
    calls: list[float] = []

    def transport(url, headers, body, timeout):
        calls.append(timeout)
        released.wait()
        return HTTPResult(200, HEALTHY.decode())

    source = linear_source(transport)
    try:
        for _ in range(5):
            source.begin_cycle()
            for _ in range(HANG_STRIKES + 1):
                with pytest.raises(RetryablePollSourceError):
                    source._linear_request(DOCUMENT)
        assert len(calls) == HANG_STRIKES
    finally:
        released.set()

    source.begin_cycle()
    assert eventually(lambda: _try_linear(source) is not None)


def _try_linear(source: LivePollSource):
    try:
        return source._linear_request(DOCUMENT)
    except RetryablePollSourceError:
        return None


@pytest.fixture
def stalling_servers():
    state = SimpleNamespace(open=0, posts=0, lock=threading.Lock(), stop=threading.Event())

    def trickle(write) -> None:
        with state.lock:
            state.open += 1
        try:
            while not state.stop.wait(0.02):
                write(b".")
        except OSError:
            pass
        finally:
            with state.lock:
                state.open -= 1

    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_CONNECT(self):
            self.wfile.write(b"HTTP/1.1 200 Connection established\r\nX-Still-Loading: ")
            trickle(lambda data: (self.wfile.write(data), self.wfile.flush()))

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            with state.lock:
                state.posts += 1
            self.send_response(200)
            self.send_header("Content-Length", str(len(HEALTHY)))
            self.end_headers()
            self.wfile.write(HEALTHY)

    class Handshake(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.recv(4096)
            self.request.sendall(b"\x16\x03\x03\x40\x00")
            trickle(self.request.sendall)

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    tls = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handshake)
    for server in (proxy, tls):
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
    state.proxy = f"http://127.0.0.1:{proxy.server_address[1]}"
    state.tls = f"https://127.0.0.1:{tls.server_address[1]}/graphql"
    try:
        yield state
    finally:
        state.stop.set()
        for server in (proxy, tls):
            server.shutdown()
            server.server_close()


@pytest.mark.parametrize("stall", ["proxy CONNECT", "TLS handshake"])
def test_a_linear_read_stalled_before_its_request_is_cut_off_and_the_next_cycle_reads(
    stalling_servers, stall
) -> None:
    servers = stalling_servers
    url = ["https://linear-test.invalid/graphql" if stall == "proxy CONNECT" else servers.tls]
    proxies = {"https": servers.proxy} if stall == "proxy CONNECT" else {}
    source = linear_source(lambda _url, headers, body, timeout: post_json(url[0], headers, body, timeout))

    with patch("urllib.request.getproxies", return_value=proxies), patch(
        "urllib.request.proxy_bypass", return_value=False
    ):
        source.begin_cycle()
        began = time.monotonic()
        for _ in range(HANG_STRIKES):
            with pytest.raises(RetryablePollSourceError, match="timed out"):
                source._linear_request(DOCUMENT)
        assert time.monotonic() - began < BOUND

        url[0] = servers.proxy + "/graphql"
        source.begin_cycle()
        assert source._linear_request(DOCUMENT) == {"data": {"viewer": {"id": "me"}}}

    assert servers.posts == 1
    assert eventually(lambda: servers.open == 0), servers.open


@pytest.fixture
def unanswered_port():
    listener = socket.socket()
    listener.bind(("0.0.0.0", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    fillers = [socket.create_connection(("127.0.0.1", port), timeout=0.1) for _ in range(2)]
    probe = socket.socket()
    probe.settimeout(0.05)
    try:
        with pytest.raises(TimeoutError):
            probe.connect(("127.0.0.1", port))
        yield port
    finally:
        probe.close()
        for sock in fillers:
            sock.close()
        listener.close()


def test_a_linear_read_stalled_in_tcp_connects_frees_its_slot_for_the_next_cycle(
    stalling_servers, unanswered_port
) -> None:
    real_getaddrinfo = socket.getaddrinfo

    def eight_unanswered(host, port, *args, **kwargs):
        if host == "unresponsive.invalid":
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (f"127.0.0.{n}", unanswered_port))
                for n in range(1, 9)
            ]
        return real_getaddrinfo(host, port, *args, **kwargs)

    url = ["https://unresponsive.invalid/graphql"]
    source = linear_source(lambda _url, headers, body, timeout: post_json(url[0], headers, body, timeout))

    with patch("urllib.request.getproxies", return_value={}), patch(
        "socket.getaddrinfo", side_effect=eight_unanswered
    ):
        source.begin_cycle()
        for _ in range(HANG_STRIKES):
            with pytest.raises(RetryablePollSourceError, match="timed out"):
                source._linear_request(DOCUMENT)

        url[0] = stalling_servers.proxy + "/graphql"
        source.begin_cycle()
        assert source._linear_request(DOCUMENT) == {"data": {"viewer": {"id": "me"}}}

    assert stalling_servers.posts == 1


def test_an_address_whose_socket_cannot_be_made_falls_through_to_the_next(
    stalling_servers,
) -> None:
    port = int(stalling_servers.proxy.rsplit(":", 1)[1])
    unsupported = 9999

    def unsupported_then_local(host, requested_port, *args, **kwargs):
        return [
            (unsupported, socket.SOCK_STREAM, 6, "", ("::1", port, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ]

    with patch("urllib.request.getproxies", return_value={}), patch(
        "socket.getaddrinfo", side_effect=unsupported_then_local
    ):
        result = post_json("http://dual-stack.invalid/graphql", {}, b"{}", DEADLINE)

    assert result.status == 200
