"""Each test starts with the signal dispositions and mask the session had (#1598)."""

from __future__ import annotations

import signal

import pytest

GUARDED = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):
    handlers = {signum: signal.getsignal(signum) for signum in GUARDED}
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    try:
        return (yield)
    finally:
        for signum, handler in handlers.items():
            if handler is not None:
                signal.signal(signum, handler)
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)
