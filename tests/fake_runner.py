from __future__ import annotations

import os

held: list[int] = []


def hold(fds) -> list[int]:
    dups = [os.dup(fd) for fd in fds]
    held.extend(dups)
    return dups


def end(fds) -> None:
    for fd in fds:
        if fd in held:
            held.remove(fd)
            os.close(fd)


def end_all() -> None:
    end(list(held))
