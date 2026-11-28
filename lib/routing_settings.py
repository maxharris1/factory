from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, get_args

from lib.role_router import (
    KNOWN_HARNESSES,
    ROLE_SLOTS,
    BuildShare,
    Difficulty,
    DrawRole,
    ReviewShare,
    RoutingConfig,
    ShareTable,
    central_pool,
    family_for,
)

ROLES: tuple[DrawRole, ...] = get_args(DrawRole)
DIFFICULTIES: tuple[Difficulty, ...] = get_args(Difficulty)
Reason = Literal[
    "bad-shape", "unknown-role", "unknown-difficulty", "bad-share", "unknown-harness", "build-total", "review-total"
]
REASONS: tuple[Reason, ...] = get_args(Reason)
_TOTALS: dict[DrawRole, Reason] = {"build": "build-total", "review": "review-total"}
_BLOCK_KEYS = frozenset({"difficulty", *ROLES})
_ROLE_POOLS = {role: tuple(slot for slot in ROLE_SLOTS if slot.split(":")[0] == role) for role in ROLES}


@dataclass(frozen=True)
class Seat:
    model: str | None
    family: str | None


@dataclass(frozen=True)
class HarnessRow:
    harness: str
    build: Seat | None
    review: Seat | None


def _seat(config: RoutingConfig, role: DrawRole, harness: str) -> Seat | None:
    for slot in _ROLE_POOLS[role]:
        for candidate in central_pool(config, slot):
            if candidate.harness == harness:
                return Seat(candidate.model, family_for(harness, candidate.model)[0])
    return None


def editable_harnesses(config: RoutingConfig) -> tuple[HarnessRow, ...]:
    rows = (HarnessRow(h, _seat(config, "build", h), _seat(config, "review", h)) for h in KNOWN_HARNESSES)
    return tuple(row for row in rows if row.build or row.review)


class OverrideRefused(ValueError):
    def __init__(self, reason: Reason) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class HarnessSetting:
    difficulty: Difficulty
    build: int | None
    review: int | None


@dataclass(frozen=True)
class Override:
    settings: Mapping[str, HarnessSetting]
    build_off: frozenset[str]
    review_off: frozenset[str]

    def table(self, *, version: int) -> ShareTable:
        settings = self.settings.items()
        return ShareTable(
            build=tuple(BuildShare(h, s.build / 100, s.difficulty) for h, s in settings if s.build is not None),
            review=tuple(ReviewShare(h, s.review / 100) for h, s in settings if s.review is not None),
            build_off=self.build_off,
            review_off=self.review_off,
            version=version,
        )


def parse_override(value: object, config: RoutingConfig) -> Override:
    try:
        return _parse(value, config)
    except OverrideRefused as refused:
        reason = refused.reason
    except Exception:
        reason = "bad-shape"
    # Raised outside the except blocks so the refusal chains no exception that could carry the value.
    raise OverrideRefused(reason)


def _parse(value: object, config: RoutingConfig) -> Override:
    if not isinstance(value, dict):
        raise OverrideRefused("bad-shape")
    pairs = list(value.items())
    if not all(isinstance(h, str) and isinstance(b, dict) for h, b in pairs):
        raise OverrideRefused("bad-shape")
    blocks: dict[str, dict[str, object]] = {h: dict(b.items()) for h, b in pairs}
    if any("difficulty" not in b or not all(isinstance(k, str) for k in b) for b in blocks.values()):
        raise OverrideRefused("bad-shape")
    if any(k not in _BLOCK_KEYS for b in blocks.values() for k in b):
        raise OverrideRefused("unknown-role")
    if not all(isinstance(b["difficulty"], str) and b["difficulty"] in DIFFICULTIES for b in blocks.values()):
        raise OverrideRefused("unknown-difficulty")
    if any(type(b[r]) is not int or not 0 <= b[r] <= 100 for b in blocks.values() for r in ROLES if r in b):
        raise OverrideRefused("bad-share")
    rows = editable_harnesses(config)
    seats = {role: frozenset(row.harness for row in rows if getattr(row, role)) for role in ROLES}
    if any(h not in seats[r] for h, b in blocks.items() for r in ROLES if r in b):
        raise OverrideRefused("unknown-harness")
    for role in ROLES:
        if seats[role] and sum(b[role] for b in blocks.values() if role in b) != 100:
            raise OverrideRefused(_TOTALS[role])
    rank = {h: i for i, h in enumerate(KNOWN_HARNESSES)}
    ordered = sorted(blocks.items(), key=lambda item: rank.get(item[0], len(rank)))
    settings = {h: HarnessSetting(b["difficulty"], b.get("build"), b.get("review")) for h, b in ordered}
    return Override(
        MappingProxyType(settings),
        build_off=seats["build"] - {h for h, b in blocks.items() if "build" in b},
        review_off=seats["review"] - {h for h, b in blocks.items() if "review" in b},
    )
