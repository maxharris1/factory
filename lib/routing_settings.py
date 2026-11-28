from __future__ import annotations

from dataclasses import dataclass
from typing import get_args

from lib.role_router import KNOWN_HARNESSES, ROLE_SLOTS, DrawRole, RoutingConfig, central_pool, family_for

ROLES: tuple[DrawRole, ...] = get_args(DrawRole)
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
