from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from lib.queue import Job

TIMEOUT_SECONDS = 8.0
BEARER = "Bearer "
GROK_LABEL = "Grok webhook"
CLAUDE_LABEL = "Claude fire"
CLAUDE_ROUTINE_BETA = "experimental-cc-routine-2026-04-01"
ANTHROPIC_VERSION = "2023-06-01"


class GrokDeliveryError(RuntimeError):
    """detail is safe to record: a status, an exception type, or env names."""

    def __init__(self, message: str, *, detail: str):
        super().__init__(message)
        self.detail = detail


@dataclass(frozen=True)
class WebhookResult:
    status: int
    headers: Mapping[str, str]


WebhookTransport = Callable[[str, str, bytes, float], WebhookResult]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = build_opener(_NoRedirect)


def send_webhook(
    url: str,
    authorization: str,
    body: bytes,
    timeout: float,
) -> WebhookResult:
    headers = {
        "Authorization": authorization,
        "Content-Type": "application/json",
    }
    if authorization[: len(BEARER)].lower() == BEARER.lower():
        headers["X-Automation-Key"] = authorization[len(BEARER) :].strip()
    return _post(url, headers, body, timeout, label=GROK_LABEL)


def send_fire(
    url: str,
    authorization: str,
    body: bytes,
    timeout: float,
) -> WebhookResult:
    headers = {
        "Authorization": authorization,
        "anthropic-beta": CLAUDE_ROUTINE_BETA,
        "anthropic-version": ANTHROPIC_VERSION,
        "Content-Type": "application/json",
    }
    return _post(url, headers, body, timeout, label=CLAUDE_LABEL)


def _post(
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout: float,
    *,
    label: str,
) -> WebhookResult:
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError(f"{label} URL must use https")
        request = Request(url, data=body, method="POST", headers=dict(headers))
        with _OPENER.open(request, timeout=timeout) as response:
            return WebhookResult(response.status, dict(response.headers.items()))
    except HTTPError as exc:
        return WebhookResult(exc.code, {})
    except (OSError, URLError, ValueError) as exc:
        detail = _exception_detail(exc)
        raise GrokDeliveryError(
            f"{label} request failed: {detail}",
            detail=detail,
        ) from None


class NoGrokWebhook:
    def deliver(self, job: Job, *, logical_key: str) -> str:
        raise GrokDeliveryError(
            "orch.yml has no grok block for Grok webhook delivery",
            detail="no-grok-block",
        )


class WebhookGrokDelivery:
    def __init__(
        self,
        *,
        url_env: str,
        key_env: str,
        auth_env: str | None = None,
        environ: Mapping[str, str] = os.environ,
        transport: WebhookTransport = send_webhook,
        timeout_seconds: float = TIMEOUT_SECONDS,
    ):
        self.url_env = url_env
        self.key_env = key_env
        self.auth_env = auth_env
        self.environ = environ
        self.transport = transport
        self.timeout_seconds = timeout_seconds

    def deliver(self, job: Job, *, logical_key: str) -> str:
        result = _send(
            GROK_LABEL,
            environ=self.environ,
            url_env=self.url_env,
            authorize=self._authorization,
            body=wake_body(job, logical_key),
            transport=self.transport,
            timeout=self.timeout_seconds,
        )
        receipt = result.headers.get("X-Request-Id")
        return receipt if receipt else logical_key

    def _authorization(self) -> str:
        return authorization(self.environ, auth_env=self.auth_env, key_env=self.key_env)


class ClaudeDriverDelivery:
    def __init__(
        self,
        *,
        url_env: str,
        token_env: str,
        environ: Mapping[str, str] = os.environ,
        transport: WebhookTransport = send_fire,
        timeout_seconds: float = TIMEOUT_SECONDS,
    ):
        self.url_env = url_env
        self.token_env = token_env
        self.environ = environ
        self.transport = transport
        self.timeout_seconds = timeout_seconds

    def deliver(self, job: Job, *, logical_key: str) -> str:
        text = wake_body(job, logical_key).decode("utf-8")
        result = _send(
            CLAUDE_LABEL,
            environ=self.environ,
            url_env=self.url_env,
            authorize=self._authorization,
            body=json.dumps({"text": text}).encode("utf-8"),
            transport=self.transport,
            timeout=self.timeout_seconds,
        )
        receipt = next(
            (value for name, value in result.headers.items() if name.lower() == "request-id"),
            None,
        )
        return receipt if receipt else logical_key

    def _authorization(self) -> str:
        return authorization(
            self.environ, auth_env=None, key_env=self.token_env, label=CLAUDE_LABEL
        )


def wake_body(job: Job, logical_key: str) -> bytes:
    request = job.request
    target_bot = None
    if request.detail is not None:
        try:
            detail = json.loads(request.detail)
        except json.JSONDecodeError:
            detail = None
        if isinstance(detail, dict) and isinstance(detail.get("project_bot"), str):
            target_bot = detail["project_bot"]
    return json.dumps(
        {
            "action": request.action,
            "attempt": request.attempt,
            "detail": request.detail,
            "generation": request.generation,
            "head": request.head,
            "job_id": job.id,
            "logical_key": logical_key,
            "repo": request.repo,
            "source_comment_id": request.source_comment_id,
            "ticket_id": request.ticket_id,
            "target_bot": target_bot,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _send(
    label: str,
    *,
    environ: Mapping[str, str],
    url_env: str,
    authorize: Callable[[], str],
    body: bytes,
    transport: WebhookTransport,
    timeout: float,
) -> WebhookResult:
    url = environ.get(url_env)
    if not url:
        raise GrokDeliveryError(
            f"set {url_env} for {label} delivery",
            detail=f"missing-env:{url_env}",
        )
    auth = authorize()
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise GrokDeliveryError(
            f"{url_env} must contain an https URL",
            detail=f"bad-env:{url_env}",
        )
    try:
        result = transport(url, auth, body, timeout)
    except GrokDeliveryError:
        raise
    except Exception as exc:
        detail = _exception_detail(exc)
        raise GrokDeliveryError(
            f"{label} request failed: {detail}",
            detail=detail,
        ) from None
    if result.status != 200:
        raise GrokDeliveryError(
            f"{label} returned HTTP {result.status}",
            detail=f"HTTP-{result.status}",
        )
    return result


def authorization(
    environ: Mapping[str, str],
    *,
    auth_env: str | None,
    key_env: str,
    label: str = GROK_LABEL,
) -> str:
    """The Authorization value from the environment: auth_env holds a full
    value and wins over key_env, which is sent as a Bearer token. Errors
    name the variables, never a value."""
    names = [name for name in (auth_env, key_env) if name]
    for name in names:
        value = environ.get(name)
        if not value:
            continue
        if "\r" in value or "\n" in value:
            raise GrokDeliveryError(
                f"{name} must be one line",
                detail=f"bad-env:{name}",
            )
        return value if name == auth_env else BEARER + value
    joined = " or ".join(names)
    raise GrokDeliveryError(
        f"set {joined} for {label} delivery",
        detail="missing-env:" + "|".join(names),
    )


def _exception_detail(exc: BaseException) -> str:
    name = type(exc).__name__
    reason = getattr(exc, "reason", None)
    if isinstance(exc, URLError) and isinstance(reason, BaseException):
        return f"{name}:{type(reason).__name__}"
    return name
