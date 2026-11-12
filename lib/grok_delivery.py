"""Deliver queued Grok work to the configured webhook routine."""

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
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("webhook URL must use https")
        headers = {
            "Authorization": authorization,
            "Content-Type": "application/json",
        }
        if authorization[: len(BEARER)].lower() == BEARER.lower():
            headers["X-Automation-Key"] = authorization[len(BEARER) :].strip()
        request = Request(url, data=body, method="POST", headers=headers)
        with _OPENER.open(request, timeout=timeout) as response:
            return WebhookResult(response.status, dict(response.headers.items()))
    except HTTPError as exc:
        return WebhookResult(exc.code, {})
    except (OSError, URLError, ValueError) as exc:
        detail = _exception_detail(exc)
        raise GrokDeliveryError(
            f"Grok webhook request failed: {detail}",
            detail=detail,
        ) from None


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
        url = self.environ.get(self.url_env)
        if not url:
            raise GrokDeliveryError(
                f"set {self.url_env} for Grok webhook delivery",
                detail=f"missing-env:{self.url_env}",
            )
        authorization = self._authorization()
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise GrokDeliveryError(
                f"{self.url_env} must contain an https URL",
                detail=f"bad-env:{self.url_env}",
            )
        request = job.request
        target_bot = None
        if request.detail is not None:
            try:
                detail = json.loads(request.detail)
            except json.JSONDecodeError:
                detail = None
            if isinstance(detail, dict) and isinstance(
                detail.get("project_bot"), str
            ):
                target_bot = detail["project_bot"]
        body = json.dumps(
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
        try:
            result = self.transport(url, authorization, body, self.timeout_seconds)
        except GrokDeliveryError:
            raise
        except Exception as exc:
            detail = _exception_detail(exc)
            raise GrokDeliveryError(
                f"Grok webhook request failed: {detail}",
                detail=detail,
            ) from None
        if result.status != 200:
            raise GrokDeliveryError(
                f"Grok webhook returned HTTP {result.status}",
                detail=f"HTTP-{result.status}",
            )
        receipt = result.headers.get("X-Request-Id")
        return receipt if receipt else logical_key

    def _authorization(self) -> str:
        """auth_env holds a full Authorization value and wins over key_env."""
        names = [name for name in (self.auth_env, self.key_env) if name]
        for name in names:
            value = self.environ.get(name)
            if not value:
                continue
            if "\r" in value or "\n" in value:
                raise GrokDeliveryError(
                    f"{name} must be one line",
                    detail=f"bad-env:{name}",
                )
            return value if name == self.auth_env else BEARER + value
        joined = " or ".join(names)
        raise GrokDeliveryError(
            f"set {joined} for Grok webhook delivery",
            detail="missing-env:" + "|".join(names),
        )


def _exception_detail(exc: BaseException) -> str:
    name = type(exc).__name__
    reason = getattr(exc, "reason", None)
    if isinstance(exc, URLError) and isinstance(reason, BaseException):
        return f"{name}:{type(reason).__name__}"
    return name
