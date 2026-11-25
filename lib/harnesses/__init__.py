"""Pluggable harness driver protocol and registry (E2-1).

Centralizes per-harness metadata and hooks (CLI stream flags, stream-JSON event
dispatch, handoff marker extraction, account-home isolation, usage probing,
skills directory, and default model family) so adding Cursor, Gemini, or future
harnesses does not scatter ``if harness == ...`` branches across the codebase.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple, Protocol, runtime_checkable


class AccountIsolation(NamedTuple):
    """Per-harness isolated account home env var and token vars to strip."""

    home: str
    tokens: tuple[str, ...]


class StreamFlags(NamedTuple):
    """Result of augmenting a harness CLI argv with structured stream flags."""

    argv: list[str]
    harness: str | None
    added: bool
    reason: str


@runtime_checkable
class HarnessDriver(Protocol):
    """Contract implemented by every supported CLI harness driver."""

    name: str
    bot_user: str
    skills_dir: str
    end_events: frozenset[str]

    def family_for_model(self, model: str | None) -> str | None:
        """Model family owned by this harness (e.g. 'anthropic', 'openai', 'google')."""
        ...

    def account_isolation(self) -> AccountIsolation | None:
        """Isolated account home variable and token env vars to scrub, or None."""
        ...

    def extra_env(self) -> Mapping[str, str]:
        """Additional static env vars required for unattended runs."""
        ...

    def dropped_account_env(self) -> tuple[str, ...]:
        """Extra env vars dropped when running from an isolated account home."""
        ...

    def matches_binary(self, basename: str) -> bool:
        """True when argv[0]'s basename is this harness's own CLI binary."""
        ...

    def stream_flags(self, argv: Sequence[str], bot_harness: str | None = None) -> StreamFlags:
        """Return ``StreamFlags`` with structured JSON stream flags injected if applicable."""
        ...

    def handles_event(self, kind: object) -> bool:
        """True when a parsed stream-JSON event ``type`` belongs to this driver."""
        ...

    def parse_event(self, summary: Any, kind: str, event: dict[str, Any], now: float) -> None:
        """Update a telemetry ``Summary`` with one parsed stream-JSON event."""
        ...

    def probe_usage(self, payload: object, now: float) -> list[Any]:
        """Parse usage readings from a harness-native probe payload or stream event."""
        ...

    def handoff_marker(self, event: Mapping[str, Any]) -> str | None:
        """Extract final assistant message text carrying ``ORCH-HANDOFF:``, or None."""
        ...


_REGISTRY: dict[str, HarnessDriver] = {}


def register(driver: HarnessDriver) -> None:
    """Register (or replace) a harness driver under ``driver.name``."""
    if not isinstance(driver.name, str) or not driver.name.strip():
        raise ValueError("driver.name must be a non-empty string")
    _REGISTRY[driver.name] = driver


def get(name: str) -> HarnessDriver:
    """Return the registered driver for ``name``, raising ``KeyError`` if unknown."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"no harness driver registered for {name!r}") from None


def find(name: str | None) -> HarnessDriver | None:
    """Return the registered driver for ``name``, or None if not registered."""
    if not name:
        return None
    return _REGISTRY.get(name)


def all_drivers() -> tuple[HarnessDriver, ...]:
    """Return all registered drivers in deterministic name order."""
    return tuple(_REGISTRY[name] for name in sorted(_REGISTRY))


def registered_names() -> tuple[str, ...]:
    """Return all registered harness names sorted alphabetically."""
    return tuple(sorted(_REGISTRY))


def driver_for_bot(bot: str) -> HarnessDriver | None:
    """Return the registered driver owning ``bot`` (e.g. ``claude-bot``), or None."""
    for driver in _REGISTRY.values():
        if driver.bot_user == bot:
            return driver
    return None


def driver_for_command(basename: str, bot_harness: str | None = None) -> HarnessDriver | None:
    """Resolve the driver for an invocation by binary basename first, then bot harness."""
    for driver in all_drivers():
        if driver.matches_binary(basename):
            return driver
    return find(bot_harness)


# Register built-in drivers when the package is imported.
from lib.harnesses import claude as _claude  # noqa: E402, F401
from lib.harnesses import codex as _codex  # noqa: E402, F401
from lib.harnesses import cursor as _cursor  # noqa: E402, F401
