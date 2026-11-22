"""Secrets on the lines a unified diff adds (#1310).

A hit names the file and the pattern row, never the matched text. A diff
over MAX_DIFF_BYTES is not read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

ScanStatus = Literal["clean", "found", "too-large"]

MAX_DIFF_BYTES = 1 << 20

PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("private-key", re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "github-token",
        re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b"),
    ),
    ("aws-access-key", re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(rb"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("api-key", re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}")),
)


@dataclass(frozen=True)
class SecretHit:
    path: str
    pattern: str


@dataclass(frozen=True)
class ScanResult:
    status: ScanStatus
    hits: tuple[SecretHit, ...]


def scan_added(diff: bytes) -> ScanResult:
    if len(diff) > MAX_DIFF_BYTES:
        return ScanResult("too-large", ())
    hits: dict[SecretHit, None] = {}
    path = ""
    in_hunk = False
    for line in diff.split(b"\n"):
        if line.startswith(b"diff --git "):
            in_hunk = False
        elif line.startswith(b"@@"):
            in_hunk = True
        elif not in_hunk:
            if line.startswith(b"+++ "):
                path = _header_path(line[4:])
        elif line.startswith(b"+"):
            added = line[1:]
            for name, pattern in PATTERNS:
                if pattern.search(added):
                    hits.setdefault(SecretHit(path, name))
    return ScanResult("found" if hits else "clean", tuple(hits))


def _header_path(name: bytes) -> str:
    # git ends a header path that holds a space with a tab.
    text = name.decode("utf-8", "replace").rstrip("\t")
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1]
    return text.removeprefix("b/")
