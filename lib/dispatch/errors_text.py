"""Redaction and text for poll and ship errors stored or posted by the dispatcher."""

from __future__ import annotations

import re
import traceback

from lib.dispatch.constants import (
    POLL_ERROR_DETAIL_LIMIT,
    POLL_ERROR_FRAMES,
    POLL_ERROR_LIMIT,
    SHIP_ERROR_LIMIT,
)


_SECRET_URL = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://\S*")
_SECRET_HOST_PATH = re.compile(
    r"\b(?:[A-Za-z0-9-]+\.)+(?:com|io|net|org|dev|app)(?:[:/]\S*)?",
    re.IGNORECASE,
)
_SECRET_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,}"
    r"|lin_(?:api|oauth)_[A-Za-z0-9_]{8,}|xox[abprs]-[A-Za-z0-9-]{8,}"
    r"|sk-[A-Za-z0-9_-]{8,}|eyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]+)*)"
)
_SECRET_NAME = (
    r"(?:bearer|basic|token|authorization|password|passwd|secret"
    r"|api[_-]?key|[A-Za-z_]*(?:_TOKEN|_SECRET|_KEY|_AUTH))"
)
_SECRET_ASSIGNMENT = re.compile(
    rf"(?i)(?<!<)(?:\b{_SECRET_NAME}\b\s*[:=]?\s*[\"']?)+\S+"
)
_PLAIN_MESSAGE = re.compile(r"[A-Za-z0-9_ .,:;()'/=+-]*")
_REDACTED_MARKS = {"\ue000": "<url>", "\ue001": "<host>", "\ue002": "<secret>"}
_PLAIN_REDACTED = re.compile(r"[A-Za-z0-9_ .,:;()'/=+\ue000-\ue002-]*")
_CREDENTIAL_WORD = re.compile(
    r"(?i)[A-Za-z0-9_-]*(?:token|secret|pass|pwd|key|auth|credential|session"
    r"|cookie|signature|bearer|basic)"
)


_POLL_ERROR_CAUSE = "\nThe above exception was the direct cause of the following exception:\n\n"
_POLL_ERROR_CONTEXT = "\nDuring handling of the above exception, another exception occurred:\n\n"


def _redact_spans(
    text: str, url: str = "<url>", host: str = "<host>", secret: str = "<secret>"
) -> str:
    text = _SECRET_URL.sub(url, text)
    text = _SECRET_HOST_PATH.sub(host, text)
    return _SECRET_TOKEN.sub(secret, text)


def _redact(text: str) -> str:
    return _SECRET_ASSIGNMENT.sub("<secret>", _redact_spans(text))


def _without_body(exc: BaseException) -> str:
    name, separator, message = "".join(
        traceback.format_exception_only(type(exc), exc)
    ).partition(": ")
    safe = getattr(exc, "safe_detail", None)
    if isinstance(safe, str) and safe:
        separator, message = ": ", safe
    return name.rstrip() + separator + _plain_prefix(message.rstrip(), _PLAIN_MESSAGE) + "\n"


def _plain_prefix(message: str, plain_pattern: re.Pattern[str]) -> str:
    plain = plain_pattern.match(message).group(0)
    credential = _CREDENTIAL_WORD.search(plain)
    if credential is not None:
        return plain[: credential.start()] + "<credential omitted>"
    if plain != message:
        return plain + " <body omitted>"
    return plain


def plain_fragment(text: str) -> str:
    plain = _PLAIN_MESSAGE.match(text).group(0)
    credential = _CREDENTIAL_WORD.search(plain)
    if credential is not None:
        plain = plain[: credential.start()]
    return plain.rstrip()


def poll_error_detail(exc: BaseException) -> str:
    """The exception chain's messages and newest frames, redacted and capped.

    A ShipError's safe_detail stands in for its message when set. Each
    message is cut at its first character outside letters, digits, spaces
    and _.,:;()'/=+-, so no structured or escaped body reaches the
    redactors, and at its first credential-like word, so no redactor can
    consume a key and leave its value. Stored in poll_errors.detail
    beside the bare class name; the tail is kept when the cap cuts, so the
    raising frame and message survive.
    """
    from lib.dashboard import redact

    blocks: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None:
        seen.add(id(current))
        frames = "".join(traceback.format_tb(current.__traceback__, limit=-POLL_ERROR_FRAMES))
        if frames:
            blocks.append(f"Traceback (most recent call last):\n{frames}{_without_body(current)}")
        else:
            blocks.append(_without_body(current))
        if current.__cause__ is not None:
            link, current = _POLL_ERROR_CAUSE, current.__cause__
        elif not current.__suppress_context__:
            link, current = _POLL_ERROR_CONTEXT, current.__context__
        else:
            current = None
        if current is None or id(current) in seen:
            break
        blocks.append(link)
    text = _redact(redact("".join(reversed(blocks)).rstrip()))
    if len(text) > POLL_ERROR_DETAIL_LIMIT:
        text = "..." + text[-(POLL_ERROR_DETAIL_LIMIT - 3):]
    return text


def safe_ship_error(exc: BaseException) -> str:
    """The exception class plus a detail with no URL, token or body.

    A ShipError's safe_detail is used when present; otherwise the message.
    Either way, URLs, host paths, token shapes, credential assignments and
    everything from the first {, [ or < on are cut, and the text is capped.
    """
    detail = getattr(exc, "safe_detail", None)
    if not isinstance(detail, str):
        detail = str(exc)
    return _cap(_named(type(exc).__name__, scrub_detail(detail)), SHIP_ERROR_LIMIT)


def poll_error_text(error: str | BaseException) -> str:
    """A poll_errors row's text (#107), capped at POLL_ERROR_LIMIT: an
    exception's class and its message, scrubbed as safe_ship_error scrubs,
    or a text the caller built from safe parts."""
    if isinstance(error, BaseException):
        error = _named(type(error).__name__, scrub_detail(str(error)))
    return _cap(error, POLL_ERROR_LIMIT)


def scrub_detail(detail: str) -> str:
    """`detail` with no URL, host path, token, credential value, body or
    non-printable character.

    URLs, host paths and token shapes are replaced; then the text is cut
    as poll_error_detail cuts a message: at its first character outside
    the plain set, so no encoded or escaped body survives, and at its
    first credential-like word. Credential assignments are not redacted
    before the cut, which would drop the key and keep a quoted value's
    later words."""
    detail = "".join(char if char.isprintable() else " " for char in detail)
    detail = _redact_spans(detail, *_REDACTED_MARKS)
    detail = _plain_prefix(detail, _PLAIN_REDACTED)
    for mark, placeholder in _REDACTED_MARKS.items():
        detail = detail.replace(mark, placeholder)
    return " ".join(detail.split())


def _named(name: str, detail: str) -> str:
    return f"{name}: {detail}" if detail else name


def _cap(text: str, limit: int) -> str:
    return text[: limit - 3].rstrip() + "..." if len(text) > limit else text
