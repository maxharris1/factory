"""Durable appends to dispatcher.log for lib.dispatch.eval_log (#545)."""

from __future__ import annotations

import os
from pathlib import Path


def append_lines(log: Path, lines: list[str]) -> None:
    """Append whole lines and fsync, starting a new line if the log lacks one."""
    text = "".join(f"{line}\n" for line in lines)
    with log.open("a+b") as handle:
        if handle.tell() > 0:
            handle.seek(handle.tell() - 1)
            if handle.read(1) != b"\n":
                text = "\n" + text
        handle.write(text.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
