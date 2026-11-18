"""#839 ac-3: a dashboard snapshot push starts at least once every push
interval while a cycle runs long, whenever the last push has finished."""

from __future__ import annotations

from lib.dashboard_push import PollPusher, push_timer_tick
from tests.test_health_slow_poller import Clock, PushChild

EVERY = 60.0
CYCLE_END = 1.0
WINDOW = 600.0


def test_a_cycle_end_push_just_after_a_timer_push_leaves_no_gap_past_the_interval() -> None:
    clock = Clock(0.0)
    started: list[float] = []

    def spawn(argv, **_kwargs) -> PushChild:
        started.append(clock.now)
        return PushChild()

    pusher = PollPusher(["orch"], {}, 0, spawn=spawn, clock=clock, log=lambda line: None)
    tick = push_timer_tick(EVERY)
    ticks = [n * tick for n in range(int(WINDOW / tick) + 1)]
    events = sorted([(at, "timer") for at in ticks] + [(CYCLE_END, "cycle")])
    for at, kind in events:
        clock.now = at
        if kind == "timer":
            pusher.on_timer(EVERY)
        else:
            pusher()

    assert started[:2] == [0.0, CYCLE_END]
    gaps = [later - earlier for earlier, later in zip(started, [*started[1:], WINDOW])]
    assert max(gaps) <= EVERY, started
