"""Linear webhook listener resource limits (issue #55, Codex r1 F1.4).

A public listener must bound how many requests it works on at once and
how long any one request may take to arrive, however slowly its bytes
trickle in. Everything here is deterministic: socketpairs instead of
network sockets, fake clocks instead of wall time, and events instead of
sleeps. The only timeouts are guards so a regression fails, not hangs.
"""

import hashlib
import hmac
import io
import json
import socket
import threading
from pathlib import Path

import pytest

from lib import linear_webhook
from lib.linear_webhook import WebhookApp
from lib.queue import SQLiteJobQueue

SECRET = b"fake-linear-signing-secret-0123456789abcdef"
NOW = 1_800_000_000.0
GUARD_SECONDS = 5


@pytest.fixture
def app(tmp_path: Path) -> WebhookApp:
    database = tmp_path / "orch.db"
    SQLiteJobQueue(database, clock=lambda: NOW)
    return WebhookApp(
        secret=SECRET,
        project_ids=frozenset({"project-1"}),
        db_path=database,
        clock=lambda: NOW,
        log=lambda _line: None,
    )


def signed_request() -> bytes:
    body = json.dumps(
        {
            "action": "update",
            "type": "Issue",
            "data": {"id": "issue-uuid-42", "identifier": "AUT-42", "projectId": "project-1"},
            "webhookTimestamp": int(NOW * 1000),
        }
    ).encode()
    signature = hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    return (
        b"POST /linear-webhook HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + f"Linear-Signature: {signature}\r\n".encode()
        + b"\r\n"
        + body
    )


def read_reply(client: socket.socket) -> bytes:
    client.settimeout(GUARD_SECONDS)
    chunks = []
    while True:
        try:
            chunk = client.recv(4096)
        except ConnectionResetError:
            # Closed with our request still unread: dropped, no reply.
            chunk = b""
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def test_connections_beyond_the_worker_limit_are_closed_without_a_worker(
    app: WebhookApp,
) -> None:
    server = linear_webhook.make_server(app, "127.0.0.1", 0, max_workers=2)
    started = threading.Semaphore(0)
    workers: list[int] = []
    finish_request = server.finish_request

    def counted(request, client_address):
        workers.append(1)
        started.release()
        finish_request(request, client_address)

    server.finish_request = counted
    pairs = [socket.socketpair() for _ in range(4)]
    try:
        # Two slow clients that have sent nothing yet hold both workers.
        for server_side, _client in pairs[:2]:
            server.process_request(server_side, ("127.0.0.1", 40000))
        for _ in range(2):
            assert started.acquire(timeout=GUARD_SECONDS)

        # A third is refused on the accepting thread: closed, no worker.
        server.process_request(pairs[2][0], ("127.0.0.1", 40001))
        assert read_reply(pairs[2][1]) == b""
        assert len(workers) == 2

        # Once a worker finishes, the next connection is served again.
        pairs[0][1].sendall(b"GET /elsewhere HTTP/1.0\r\n\r\n")
        assert read_reply(pairs[0][1]).startswith(b"HTTP/1.0 404")
        pairs[1][1].sendall(b"GET /elsewhere HTTP/1.0\r\n\r\n")
        assert read_reply(pairs[1][1]).startswith(b"HTTP/1.0 404")
        server.server_close()  # joins the finished workers, freeing both slots
        server.process_request(pairs[3][0], ("127.0.0.1", 40002))
        assert started.acquire(timeout=GUARD_SECONDS)
        pairs[3][1].sendall(signed_request())
        assert read_reply(pairs[3][1]).startswith(b"HTTP/1.0 200")
        assert len(workers) == 3
    finally:
        server.server_close()
        for server_side, client in pairs:
            server_side.close()
            client.close()


class TrickleSocket:
    """A client that always makes progress: one byte per read, forever."""

    def __init__(self, clock: list[float], step: float) -> None:
        self.clock = clock
        self.step = step
        self.timeouts: list[float] = []
        self.reads = 0

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def recv_into(self, buffer, nbytes: int = 0) -> int:
        self.reads += 1
        self.clock[0] += self.step  # each byte arrives just inside the idle timeout
        buffer[0:1] = b"X"
        return 1


@pytest.mark.parametrize("step", [1.0, 9.0])
def test_a_trickling_client_cannot_extend_a_request_past_its_deadline(
    step: float,
) -> None:
    clock = [100.0]
    trickle = TrickleSocket(clock, step)
    reader = io.BufferedReader(
        linear_webhook.DeadlineReader(
            trickle,
            deadline=clock[0] + linear_webhook.REQUEST_DEADLINE_SECONDS,
            clock=lambda: clock[0],
        ),
        buffer_size=1,
    )

    with pytest.raises(TimeoutError):
        reader.readline(65537)

    deadline = 100.0 + linear_webhook.REQUEST_DEADLINE_SECONDS
    # Every socket wait is capped at the time left, so the last one ends
    # at the deadline, not one idle timeout after it.
    assert all(timeout <= linear_webhook.REQUEST_DEADLINE_SECONDS for timeout in trickle.timeouts)
    assert trickle.timeouts == sorted(trickle.timeouts, reverse=True)
    assert trickle.reads <= linear_webhook.REQUEST_DEADLINE_SECONDS / step + 1
    assert clock[0] <= deadline + step


@pytest.mark.parametrize("late", [False, True], ids=["within-deadline", "past-deadline"])
def test_the_listener_reads_requests_through_the_deadline(
    app: WebhookApp,
    late: bool,
) -> None:
    calls = [0]

    def clock() -> float:
        # First call starts the request's deadline; later calls are reads.
        calls[0] += 1
        if calls[0] == 1 or not late:
            return 500.0
        return 500.0 + linear_webhook.REQUEST_DEADLINE_SECONDS + 1

    handled: list[str] = []
    real_handle = app.handle

    def spy(*args, **kwargs):
        handled.append(args[0])
        return real_handle(*args, **kwargs)

    app.handle = spy
    server = linear_webhook.make_server(app, "127.0.0.1", 0, clock=clock)
    server_side, client = socket.socketpair()
    try:
        client.sendall(signed_request())
        server.process_request(server_side, ("127.0.0.1", 40000))
        reply = read_reply(client)
    finally:
        server.server_close()
        server_side.close()
        client.close()

    if late:
        # Past the deadline the connection is dropped before the app runs.
        assert reply == b""
        assert handled == []
    else:
        assert reply.startswith(b"HTTP/1.0 200")
        assert handled == ["POST"]
