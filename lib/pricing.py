"""Pure token-to-USD fallback pricing for harness telemetry and weekly evals.

Zero I/O, zero network, zero subprocess imports so read-only reporting modules
(like lib.eval_report) can compute effective job spend without pulling in runtime
dependencies.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

# Per-1M-token fallback pricing (input, cached_read, cache_write, output) in USD
# when a harness CLI reports token counts without a reported total_cost_usd.
# Defaults reflect standard frontier-model API list rates.
DEFAULT_PRICING: dict[str, tuple[float, float, float, float]] = {
    "claude": (3.00, 0.30, 3.75, 15.00),
    "codex": (2.50, 0.25, 2.50, 10.00),
    "cursor": (3.00, 0.30, 3.75, 15.00),
    "gemini": (1.25, 0.3125, 1.25, 10.00),
}


def estimate_cost_usd(
    harness: str,
    *,
    tokens_in: int,
    tokens_cached: int,
    tokens_out: int,
    tokens_cache_write: int = 0,
    pricing: Mapping[str, tuple[float, float, float, float]] | None = None,
) -> float | None:
    """Estimate USD cost from token counts when a harness does not report cost_usd.

    `tokens_in` includes `tokens_cached` and `tokens_cache_write` (matching Summary's
    convention); uncached input is clamped at >= 0. Returns None if no tokens were
    recorded or the harness has no pricing entry.
    """
    rates = (pricing or DEFAULT_PRICING).get(harness)
    if rates is None:
        return None
    if tokens_in <= 0 and tokens_cached <= 0 and tokens_out <= 0 and tokens_cache_write <= 0:
        return None
    in_rate, cached_rate, write_rate, out_rate = rates
    cached = max(0, tokens_cached)
    cache_write = max(0, tokens_cache_write)
    uncached = max(0, tokens_in - cached - cache_write)
    out = max(0, tokens_out)
    cost = (
        uncached * in_rate
        + cached * cached_rate
        + cache_write * write_rate
        + out * out_rate
    ) / 1_000_000.0
    return round(cost, 6)


def effective_cost_usd(
    harness: str,
    cost_usd: float | None,
    *,
    tokens_in: int,
    tokens_cached: int,
    tokens_out: int,
    tokens_cache_write: int = 0,
    pricing: Mapping[str, tuple[float, float, float, float]] | None = None,
) -> float | None:
    """Reported CLI cost when present and valid, else estimated cost from token pricing."""
    if (
        isinstance(cost_usd, (int, float))
        and not isinstance(cost_usd, bool)
        and math.isfinite(cost_usd)
        and cost_usd >= 0
    ):
        return float(cost_usd)
    return estimate_cost_usd(
        harness,
        tokens_in=tokens_in,
        tokens_cached=tokens_cached,
        tokens_out=tokens_out,
        tokens_cache_write=tokens_cache_write,
        pricing=pricing,
    )
