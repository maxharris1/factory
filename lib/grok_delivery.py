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


class GrokDeliveryError(RuntimeError):
    pass


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
    key: str,
    body: bytes,
    timeout: float,
) -> WebhookResult:
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("webhook URL must use https")
        request = Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "X-Automation-Key": key,
            },
        )
        with _OPENER.open(request, timeout=timeout) as response:
            return WebhookResult(response.status, dict(response.headers.items()))
    except HTTPError as exc:
        return WebhookResult(exc.code, {})
    except (OSError, URLError, ValueError) as exc:
        raise GrokDeliveryError(
            f"Grok webhook request failed: {type(exc).__name__}"
        ) from None


class WebhookGrokDelivery:
    def __init__(
        self,
        *,
        url_env: str,
        key_env: str,
        environ: Mapping[str, str] = os.environ,
        transport: WebhookTransport = send_webhook,
        timeout_seconds: float = TIMEOUT_SECONDS,
    ):
        self.url_env = url_env
        self.key_env = key_env
        self.environ = environ
        self.transport = transport
        self.timeout_seconds = timeout_seconds

    def deliver(self, job: Job, *, logical_key: str) -> str:
        url = self._required(self.url_env)
        key = self._required(self.key_env)
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise GrokDeliveryError(
                f"{self.url_env} must contain an https URL"
            )
        request = job.request
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
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        try:
            result = self.transport(url, key, body, self.timeout_seconds)
        except GrokDeliveryError:
            raise
        except Exception as exc:
            raise GrokDeliveryError(
                f"Grok webhook request failed: {type(exc).__name__}"
            ) from None
        if result.status != 200:
            raise GrokDeliveryError(
                f"Grok webhook returned HTTP {result.status}"
            )
        receipt = result.headers.get("X-Request-Id")
        return receipt if receipt else logical_key

    def _required(self, name: str) -> str:
        value = self.environ.get(name)
        if not value:
            raise GrokDeliveryError(f"set {name} for Grok webhook delivery")
        return value
