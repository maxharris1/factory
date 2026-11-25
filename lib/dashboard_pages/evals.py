from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from lib.dashboard_pages.common import _chip, _e, _panel, _span

TITLE = "Weekly evals"
NO_DIR = '<p class="empty">No evals dir on this host.</p>'
NO_SNAPSHOTS = '<p class="empty">No weekly evals yet. The Monday report writes the first.</p>'


@dataclass(frozen=True)
class Kind:
    scale: float
    digits: int
    prefix: str = ""
    suffix: str = ""
    delta_suffix: str = ""


KINDS = {
    "count": Kind(1, 0),
    "rate": Kind(100, 0, suffix="%", delta_suffix=" pts"),
    "hours": Kind(1, 1, suffix=" h", delta_suffix=" h"),
    "usd": Kind(1, 2, prefix="$"),
}


def _number(value: object) -> float | None:
    return None if isinstance(value, bool) or not isinstance(value, (int, float)) else value


def _metric(key: str) -> Callable[[list[dict], int], float | None]:
    return lambda weeks, i: _number(weeks[i]["metrics"].get(key)) if i < len(weeks) else None


_SUCCEEDED = _metric("tickets-succeeded")


def _shipped(weeks: list[dict], i: int) -> float | None:
    now, before = _SUCCEEDED(weeks, i), _SUCCEEDED(weeks, i + 1)
    return None if now is None or before is None else now - before


@dataclass(frozen=True)
class EvalRow:
    label: str
    kind: str
    better: Literal["up", "down"]
    value: Callable[[list[dict], int], float | None]


EVAL_ROWS = (
    EvalRow("Throughput (tickets shipped)", "count", "up", _shipped),
    EvalRow("First-pass approve rate (lifetime)", "rate", "up", _metric("first-pass-approve-rate")),
    EvalRow("Cap rate (lifetime)", "rate", "down", _metric("cap-rate")),
    EvalRow("Poll errors", "count", "down", _metric("poll-errors")),
    EvalRow("Stuck jobs", "count", "down", _metric("stuck-jobs")),
    EvalRow("Failed jobs", "count", "down", _metric("failed-jobs")),
    EvalRow("Cycle time p50", "hours", "down", _metric("cycle-time-p50-hours")),
    EvalRow("Cycle time p90", "hours", "down", _metric("cycle-time-p90-hours")),
    EvalRow("Max interventions", "count", "down", _metric("max-interventions")),
    EvalRow("Wasted wakes", "count", "down", _metric("wasted-wakes")),
    EvalRow("Productive spend", "usd", "up", _metric("productive-cost-usd")),
    EvalRow("Wasted spend", "usd", "down", _metric("wasted-cost-usd")),
)


def _format(kind: Kind, number: float, suffix: str, signed: bool) -> str:
    sign = "-" if number < 0 else "+" if signed and number > 0 else ""
    return f"{sign}{kind.prefix}{abs(number):,.{kind.digits}f}{suffix}"


def _cell(row: EvalRow, weeks: list[dict], i: int) -> str:
    kind = KINDS[row.kind]
    now, before = row.value(weeks, i), row.value(weeks, i + 1)
    text = "n/a" if now is None else _format(kind, round(now * kind.scale, kind.digits), kind.suffix, False)
    if now is None or before is None:
        return f'<td>{_span("ev-v", text)} {_chip("n/a")}</td>'
    delta = round((now - before) * kind.scale, kind.digits)
    tone = "muted" if not delta else "ok" if (delta > 0) == (row.better == "up") else "bad"
    return f'<td>{_span("ev-v", text)} {_chip(_format(kind, delta, kind.delta_suffix, True), tone)}</td>'


def _evals(snapshot: dict) -> str:
    bundle = snapshot.get("evals") or {"state": "no dir", "weeks": [], "skipped": 0}
    weeks, skipped = bundle["weeks"], bundle["skipped"]
    if bundle["state"] == "no dir":
        inner = NO_DIR
    elif bundle["state"] != "ok" or not weeks:
        inner = NO_SNAPSHOTS
    else:
        head = "".join(f'<th scope="col">{_e(week["week"])}</th>' for week in weeks)
        rows = "".join(
            f'<tr><th scope="row">{_e(row.label)}</th>{"".join(_cell(row, weeks, i) for i in range(len(weeks)))}</tr>'
            for row in EVAL_ROWS
        )
        inner = (f'<div class="ev-scroll"><table class="ev-table"><thead><tr><th>metric</th>{head}</tr></thead>'
                 f"<tbody>{rows}</tbody></table></div>")
    if skipped:
        inner += f'<p class="ev-skipped">{skipped} report{"" if skipped == 1 else "s"} skipped.</p>'
    count = f'{len(weeks)} week{"" if len(weeks) == 1 else "s"}' if weeks else None
    return _panel("evals", TITLE, inner, count=count)
