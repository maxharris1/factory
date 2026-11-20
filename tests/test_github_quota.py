import base64
import json

import pytest

from lib.github_actions import GhResult
from lib.poll_source import (
    BACKOFF_SECONDS,
    LivePollSource,
    PollSourceError,
    QuotaDeferred,
    RATE_RESET_GRACE_SECONDS,
)
from tests.test_live_poll import FACTORY, config

WALL = 1_800_000_000.0
FACTORY_BODY = json.dumps(
    {
        "encoding": "base64",
        "content": base64.b64encode(
            FACTORY.format(tickets="github", ship="merge").encode()
        ).decode(),
    }
)


def response(status: int, body: str = "{}", **headers: object) -> GhResult:
    reason = {200: "OK", 403: "Forbidden", 429: "Too Many Requests"}[status]
    lines = [f"HTTP/2.0 {status} {reason}"]
    lines += [f"{name.replace('_', '-')}: {value}" for name, value in headers.items()]
    stdout = "\r\n".join(lines) + "\r\n\r\n" + body
    if status == 200:
        return GhResult(0, stdout, "")
    return GhResult(1, stdout, f"gh: API rate limit exceeded (HTTP {status})")


def limited(status: int = 403, **headers: object) -> GhResult:
    return response(
        status,
        '{"message": "API rate limit exceeded"}',
        **{"X-RateLimit-Limit": 5000, "X-RateLimit-Remaining": 0, **headers},
    )


class Quota:
    def __init__(self) -> None:
        self.mono = 1_000.0
        self.wall = WALL
        self.factory: list[GhResult] = []
        self.probe: GhResult = GhResult(1, "", "gh: HTTP 500")
        self.calls: list[list[str]] = []
        self.lines: list[str] = []
        self.source = LivePollSource(
            config(),
            gh_runner=self.run,
            clock=lambda: self.mono,
            wall=lambda: self.wall,
            sleep=lambda seconds: None,
        )
        self.source.warning_sink = self.lines.append

    def run(self, argv, timeout):
        del timeout
        call = list(argv)
        self.calls.append(call)
        if "rate_limit" in call:
            return self.probe
        if any("/contents/factory.md" in part for part in call):
            return self.factory.pop(0)
        return GhResult(0, "[]", "")

    def read_factory(self) -> None:
        self.source.factory_context(config().projects[0])

    def comments(self) -> None:
        self.source.github.list_comments("acme/widgets", "7")

    def reads(self) -> int:
        return sum("rate_limit" not in call for call in self.calls)

    def hit_limit(self, result: GhResult) -> None:
        self.factory.append(result)
        with pytest.raises(PollSourceError):
            self.read_factory()


@pytest.mark.parametrize("status", [403, 429])
def test_rate_limit_pauses_every_read_until_the_reset_not_300s(status) -> None:
    quota = Quota()
    quota.hit_limit(limited(status, X_RateLimit_Reset=int(WALL + 900)))
    reads = quota.reads()

    quota.mono += BACKOFF_SECONDS + 1
    with pytest.raises(QuotaDeferred):
        quota.comments()
    quota.mono = 1_000.0 + 900 + RATE_RESET_GRACE_SECONDS - 0.1
    with pytest.raises(QuotaDeferred):
        quota.comments()
    assert quota.reads() == reads

    quota.mono = 1_000.0 + 900 + RATE_RESET_GRACE_SECONDS
    quota.comments()
    assert quota.reads() == reads + 1


def test_wall_clock_jump_does_not_lift_the_pause() -> None:
    quota = Quota()
    quota.hit_limit(limited(X_RateLimit_Reset=int(WALL + 900)))

    quota.wall += 3600
    quota.mono += 10
    with pytest.raises(QuotaDeferred):
        quota.comments()


def test_retry_after_wins_over_the_reset_header() -> None:
    quota = Quota()
    quota.hit_limit(limited(X_RateLimit_Reset=int(WALL + 900), Retry_After=120))

    quota.mono += 119
    with pytest.raises(QuotaDeferred):
        quota.comments()
    quota.mono += 1
    quota.comments()


def test_no_reset_header_and_no_probe_falls_back_to_300s() -> None:
    quota = Quota()
    quota.hit_limit(response(429, '{"message": "rate limited"}'))

    assert any("rate_limit" in call for call in quota.calls)
    quota.mono += BACKOFF_SECONDS - 1
    with pytest.raises(QuotaDeferred):
        quota.comments()
    quota.mono += 1
    quota.comments()


def test_probe_supplies_the_reset_when_the_response_has_none() -> None:
    quota = Quota()
    quota.probe = GhResult(
        0,
        json.dumps(
            {"resources": {"core": {"limit": 5000, "remaining": 0, "reset": int(WALL + 600)}}}
        ),
        "",
    )
    quota.hit_limit(response(429, '{"message": "rate limited"}'))

    quota.mono += 600 + RATE_RESET_GRACE_SECONDS - 1
    with pytest.raises(QuotaDeferred):
        quota.comments()
    quota.mono += 1
    quota.comments()
    assert "core 0/5000 left" in quota.lines[0]


def test_paused_adapter_read_never_reaches_the_runner() -> None:
    quota = Quota()
    quota.hit_limit(limited(X_RateLimit_Reset=int(WALL + 900)))
    calls = len(quota.calls)

    for _ in range(3):
        with pytest.raises(QuotaDeferred):
            quota.comments()

    assert len(quota.calls) == calls


def test_one_throttle_line_per_episode() -> None:
    quota = Quota()
    quota.hit_limit(limited(X_RateLimit_Reset=int(WALL + 900)))
    for _ in range(10):
        with pytest.raises(QuotaDeferred):
            quota.comments()
    quota.mono += 905
    quota.wall += 905
    quota.hit_limit(limited(X_RateLimit_Reset=int(quota.wall + 60)))

    assert len(quota.lines) == 1
    line = quota.lines[0]
    assert line.startswith("github throttle: HTTP 403 rate limit on GET contents/factory.md")
    assert "core 0/5000 left" in line
    assert "paused 15m05s" in line

    quota.mono += 65
    quota.wall += 65
    quota.comments()
    quota.hit_limit(limited(X_RateLimit_Reset=int(quota.wall + 900)))
    assert len(quota.lines) == 2


def test_replayed_headers_do_not_overwrite_a_newer_window() -> None:
    quota = Quota()
    reset = int(WALL + 600)
    quota.factory.append(
        response(
            200,
            FACTORY_BODY,
            X_RateLimit_Limit=5000,
            X_RateLimit_Remaining=7,
            X_RateLimit_Reset=reset,
        )
    )
    quota.read_factory()
    quota.source.begin_cycle()
    quota.factory.append(
        response(
            200,
            FACTORY_BODY,
            X_RateLimit_Limit=5000,
            X_RateLimit_Remaining=4900,
            X_RateLimit_Reset=reset,
        )
    )
    quota.read_factory()
    quota.source.begin_cycle()
    quota.factory.append(
        response(
            200,
            FACTORY_BODY,
            X_RateLimit_Limit=5000,
            X_RateLimit_Remaining=4999,
            X_RateLimit_Reset=reset - 3600,
        )
    )
    quota.read_factory()
    quota.source.begin_cycle()

    quota.hit_limit(response(403, '{"message": "API rate limit exceeded"}'))

    assert "core 7/5000 left" in quota.lines[0]
    quota.mono += 600 + RATE_RESET_GRACE_SECONDS - 1
    with pytest.raises(QuotaDeferred):
        quota.comments()


def test_replayed_rate_limit_response_cannot_shorten_the_pause() -> None:
    quota = Quota()
    reset = int(WALL + 900)
    quota.factory.append(
        response(
            200,
            FACTORY_BODY,
            X_RateLimit_Limit=5000,
            X_RateLimit_Remaining=0,
            X_RateLimit_Reset=reset,
        )
    )
    quota.read_factory()
    quota.source.begin_cycle()

    quota.hit_limit(limited(429, X_RateLimit_Remaining=1, X_RateLimit_Reset=reset))

    quota.mono += BACKOFF_SECONDS
    with pytest.raises(QuotaDeferred):
        quota.comments()
    quota.mono = 1_000.0 + 900 + RATE_RESET_GRACE_SECONDS - 0.1
    with pytest.raises(QuotaDeferred):
        quota.comments()
    quota.mono = 1_000.0 + 900 + RATE_RESET_GRACE_SECONDS
    quota.comments()


def test_quota_pause_is_a_deferral_not_a_cycle_error() -> None:
    quota = Quota()
    deferrals: list[str] = []
    errors: list[tuple[str, str, str]] = []
    quota.source.deferral_sink = deferrals.append
    quota.source.error_sink = lambda *error: errors.append(error)
    quota.hit_limit(limited(X_RateLimit_Reset=int(WALL + 900)))
    quota.source.begin_cycle()
    errors.clear()

    assert quota.source.github_candidates("orch:state/") == ()
    quota.source.end_cycle()

    assert errors == []
    assert any("github quota" in line for line in deferrals)
