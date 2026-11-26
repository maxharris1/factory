from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from typing import Literal

from lib.dashboard_evals import KEEP, METRIC_KEYS, UNRECORDED
from lib.dashboard_pages.common import _chip, _e, _panel, _span
from lib.dashboard_static import read_asset

CSS = read_asset("css/evals.css")

TITLE = "Weekly evals"
NO_DIR = "No evals dir on this host."
NO_SNAPSHOTS = "No weekly evals yet. The Monday report writes the first."


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


@dataclass(frozen=True)
class EvalRow:
    label: str
    kind: str
    better: Literal["up", "down"]
    metric: str
    shipped: bool = False


EVAL_ROWS = (
    EvalRow("Throughput (tickets shipped)", "count", "up", "tickets-succeeded", shipped=True),
    EvalRow("First-pass approve rate (lifetime)", "rate", "up", "first-pass-approve-rate"),
    EvalRow("Cap rate (lifetime)", "rate", "down", "cap-rate"),
    EvalRow("Poll errors", "count", "down", "poll-errors"),
    EvalRow("Stuck jobs", "count", "down", "stuck-jobs"),
    EvalRow("Failed jobs", "count", "down", "failed-jobs"),
    EvalRow("Cycle time p50", "hours", "down", "cycle-time-p50-hours"),
    EvalRow("Cycle time p90", "hours", "down", "cycle-time-p90-hours"),
    EvalRow("Max interventions", "count", "down", "max-interventions"),
    EvalRow("Wasted wakes", "count", "down", "wasted-wakes"),
    EvalRow("Productive spend", "usd", "up", "productive-cost-usd"),
    EvalRow("Wasted spend", "usd", "down", "wasted-cost-usd"),
)


def _value(row: EvalRow, weeks: list[dict], i: int) -> float | None:
    def read(j: int) -> float | None:
        value = weeks[j]["metrics"].get(row.metric) if j < len(weeks) else None
        return None if isinstance(value, bool) or not isinstance(value, (int, float)) else float(value)

    now = read(i)
    if not row.shipped or now is None:
        return now
    before = read(i + 1)
    return None if before is None else now - before


def _half_up(value: float, digits: int) -> float | None:
    scaled = value * 10**digits
    if not math.isfinite(scaled):
        return None
    whole = math.floor(scaled)
    return (whole + 1 if scaled - whole >= 0.5 else whole) / 10**digits


def _format(kind: Kind, number: float, suffix: str, signed: bool) -> str:
    sign = "-" if number < 0 else "+" if signed and number > 0 else ""
    return f"{sign}{kind.prefix}{abs(number):,.{kind.digits}f}{suffix}"


def _cell(row: EvalRow, weeks: list[dict], i: int) -> str:
    kind = KINDS[row.kind]
    now, before = _value(row, weeks, i), _value(row, weeks, i + 1)
    shown = None if now is None else _half_up(now * kind.scale, kind.digits)
    text = "n/a" if shown is None else _format(kind, shown, kind.suffix, False)
    delta = None if now is None or before is None else _half_up((now - before) * kind.scale, kind.digits)
    if delta is None:
        return f'<td>{_span("ev-v", text)} {_chip("n/a")}</td>'
    tone = "muted" if not delta else "ok" if (delta > 0) == (row.better == "up") else "bad"
    return f'<td>{_span("ev-v", text)} {_chip(_format(kind, delta, kind.delta_suffix, True), tone)}</td>'


def _evals(snapshot: dict) -> str:
    bundle = snapshot.get("evals") or {"state": "no dir", "weeks": [], "skipped": 0}
    weeks, skipped = bundle["weeks"], bundle["skipped"]
    if bundle["state"] == "no dir":
        inner = f'<p class="empty">{NO_DIR}</p>'
    elif bundle["state"] != "ok" or not weeks:
        inner = f'<p class="empty">{NO_SNAPSHOTS}</p>'
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


SCRIPT = read_asset("js/evals.js").replace("__EVALS__", json.dumps(
    {"rows": [asdict(row) for row in EVAL_ROWS], "kinds": {name: asdict(kind) for name, kind in KINDS.items()},
     "texts": {"title": TITLE, "no_dir": NO_DIR, "no_snapshots": NO_SNAPSHOTS},
     "keep": KEEP, "metric_keys": METRIC_KEYS, "unrecorded": UNRECORDED},
    separators=(",", ":"),
))
