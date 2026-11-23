"""Redaction for a command's output, before any cut keeps only its end."""

from __future__ import annotations


def redact_output(text: str) -> str:
    from lib.dashboard import redact, redact_key_blocks
    from lib.dispatch.errors_text import _redact

    blocks = _scrub_credential_blocks(redact_key_blocks(text.splitlines()))
    return _redact(redact("\n".join(blocks)))


def _scrub_credential_blocks(lines: list[str]) -> list[str]:
    from lib.dispatch.errors_text import _CREDENTIAL_WORD, scrub_detail

    result: list[str] = []
    at = 0
    while at < len(lines):
        line = lines[at]
        indent = _indent(line)
        end = at + 1
        while end < len(lines) and (not lines[end].strip() or _indent(lines[end]) > indent):
            end += 1
        while end > at + 1 and not lines[end - 1].strip():
            end -= 1
        if end > at + 1 and _CREDENTIAL_WORD.search(line):
            result.append(line[:indent] + scrub_detail("\n".join(lines[at:end])))
            at = end
        else:
            result.append(line)
            at += 1
    return result


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())
