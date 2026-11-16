"""Durable appends to dispatcher.log for lib.dispatch.eval_log (#545)."""

from __future__ import annotations

import os
from pathlib import Path


def append_bytes(log: Path, data: bytes) -> None:
    with log.open("ab") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def blank_bytes(log: Path, start: int, length: int) -> None:
    with log.open("r+b") as handle:
        handle.seek(start)
        handle.write(b" " * length)
        handle.flush()
        os.fsync(handle.fileno())


def append_lines(log: Path, lines: list[bytes]) -> None:
    """Append newline-terminated lines, starting a new line if the log lacks one."""
    data = b"".join(lines)
    if log.exists() and log.stat().st_size > 0:
        with log.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                data = b"\n" + data
    append_bytes(log, data)
