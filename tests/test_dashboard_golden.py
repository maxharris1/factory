from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from lib.dashboard import collect_snapshot
from lib.dashboard_pages import activity, cockpit, efficiency, evals, lanes, routing, settings, system, tickets
from lib.dashboard_pages import ticket_detail as td
from lib.dashboard_pages.common import ticket_address
from lib.dashboard_telemetry import rollup_panel
from tests.golden.pages import pinned_version
from tests.test_dashboard import NOW
from tests.test_dashboard_layout import MINI, _job_row, _snapshot
from tests.test_dashboard_ticket_records import CLEFFY, _shipped

GOLDEN = Path(__file__).resolve().parent / "golden" / "dashboard"


def _broken_chart(root: Path) -> dict:
    bad = root / "bad.mmd"
    bad.write_text("not a chart\n", encoding="utf-8")
    return {"chart_path": bad}


VARIANTS: dict[str, Callable[[Path], dict]] = {
    "default": lambda root: {},
    "live": lambda root: {"last_cycle_at": NOW - 4},
    "mini-chart": lambda root: {"chart_path": MINI},
    "broken-chart": _broken_chart,
}


@dataclass(frozen=True)
class Case:
    snapshot: dict
    parts: dict[str, dict]


def _part(snapshot: dict, address: str) -> dict:
    found = td.resolve_ticket(snapshot, address, td._records(snapshot))
    chart = snapshot.get("statechart")
    model = {**td._detail_model(found["row"], chart), "now": snapshot.get("generated_at_epoch")}
    return {"row": found["row"], "model": model, "chart": chart, "key": found["address"],
            "started": td.TICKET_KINDS[found["kind"]]["started"], "ticket": found.get("ticket"),
            "kind": found["kind"]}


def _case(root: Path, variant: Callable[[Path], dict]) -> Case:
    kwargs = variant(root)
    snapshot = _snapshot(root / "layout", **kwargs)
    shipped = collect_snapshot(*_shipped(root / "shipped"), now=NOW, **kwargs)
    return Case(snapshot, {
        "running": _part(snapshot, ticket_address(_job_row(snapshot, "h-review"))),
        "stopped": _part(snapshot, "aut0-code/aut0#AUT-7"),
        "shipped": _part(shipped, f"{CLEFFY}#80"),
    })


def _snap(render: Callable[[dict], str]) -> Callable[[Case], str | None]:
    return lambda case: render(case.snapshot)


def _ticket(kind: str, render: Callable[[dict], str]) -> Callable[[Case], str | None]:
    return lambda case: render(case.parts[kind]) if case.parts[kind]["kind"] == kind else None


LEAVES: dict[str, Callable[[Case], str | None]] = {
    "_lanes": _snap(lanes._lanes),
    "_just_happened": _snap(lanes._just_happened),
    "_intake": _snap(lanes._intake),
    "_needs_max": _snap(cockpit._needs_max),
    "_attention": _snap(activity._attention),
    "_relay": _snap(activity._relay),
    "_headroom": _snap(efficiency._headroom),
    "_rounds": _snap(efficiency._rounds),
    "_savings": _snap(efficiency._savings),
    "_balance": _snap(efficiency._balance),
    "_jobs_lifetime": _snap(efficiency._jobs_lifetime),
    "rollup_panel": _snap(rollup_panel),
    "_routing": _snap(routing._routing),
    "_evals": _snap(evals._evals),
    "_broken": _snap(system._broken),
    "_components": _snap(system._components),
    "_versions": _snap(system._versions),
    "_health": _snap(system._health),
    "_log": _snap(system._log),
    "_rules": _snap(system._rules),
    "_no_waiter": _snap(system._no_waiter),
    "_board": _snap(tickets._board),
    "_tickets": _snap(tickets._tickets),
    "_settings": _snap(settings._settings),
    "_accounts": _snap(settings._accounts),
    "_runlog": _ticket("running", lambda p: td._runlog(p["key"], p["model"])),
    "_journey": _ticket("running", lambda p: td._journey(p["row"], p["model"])),
    "_next": _ticket("running", lambda p: td._next(p["model"])),
    "_liverun": _ticket("running", lambda p: td._liverun(p["row"], p["started"])),
    "_chart": _ticket("running", lambda p: td._chart(p["key"], p["model"], p["chart"])),
    "_summary": _ticket("shipped", lambda p: td._summary(p["model"])),
    "_ended": _ticket("shipped", lambda p: td._ended(p["model"])),
    "_why": _ticket("stopped", lambda p: td._why(p["model"])),
    "_exposure": _ticket("running", lambda p: td._exposure(p["row"])),
    "ticket_cockpit_html": _ticket("running", lambda p: cockpit.ticket_cockpit_html(p["ticket"])),
}


@pytest.fixture(scope="module")
def cases(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Case]:
    with pinned_version():
        return {name: _case(tmp_path_factory.mktemp(name), variant) for name, variant in VARIANTS.items()}


@pytest.mark.parametrize("leaf", LEAVES, ids=list(LEAVES))
def test_leaf_panel_matches_its_golden(leaf: str, cases: dict[str, Case]) -> None:
    with pinned_version():
        markups = {name: LEAVES[leaf](case) for name, case in cases.items()}
    text = "".join(f"<!-- variant: {name} -->\n{markup}\n" for name, markup in markups.items() if markup is not None)
    path = GOLDEN / f"{leaf}.html"
    if os.environ.get("GOLDEN_UPDATE") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        return
    assert path.exists(), f"no golden for {leaf}; run GOLDEN_UPDATE=1 python3 -m pytest tests/test_dashboard_golden.py"
    with path.open(encoding="utf-8", newline="") as handle:
        assert handle.read() == text, f"{leaf} markup changed"


def test_golden_folder_holds_only_leaf_goldens() -> None:
    assert {path.name for path in GOLDEN.iterdir()} == {f"{name}.html" for name in LEAVES} | {"README.md"}
