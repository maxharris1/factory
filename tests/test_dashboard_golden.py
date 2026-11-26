from __future__ import annotations

import copy
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from lib.dashboard import RECORDS
from lib.dashboard_pages import activity, cockpit, efficiency, evals, lanes, routing, settings, system, tickets
from lib.dashboard_pages import ticket_detail as td
from lib.dashboard_pages.common import ticket_address
from lib.dashboard_telemetry import rollup_panel
from tests.golden.pages import pinned_version
from tests.test_dashboard_layout import _held_more_than_running, _snapshot
from tests.test_dashboard_ticket_detail import SHIPPED, _with_shipped

GOLDEN = Path(__file__).parent / "golden" / "dashboard"
SOURCE = "a2bb7247d6e85731543c23ceabbd0f72ee98e0fa"
RUNNING = "maxharris1/cleffy#13"
STOPPED = "aut0-code/aut0#AUT-7"
ASKING = "maxharris1/tracy-harris-coaching#5"
QUESTION = {"text": "Sign in with OAuth or an API key?", "options": ["OAuth", "Key"]}
VARIANTS = {"snapshot": _snapshot, "held_more_than_running": _held_more_than_running}

SNAPSHOT_PANELS = (
    lanes._lanes, lanes._just_happened, lanes._intake, cockpit._needs_max, activity._attention, activity._relay,
    efficiency._headroom, efficiency._rounds, efficiency._savings, efficiency._balance, efficiency._jobs_lifetime,
    rollup_panel, routing._routing, evals._evals, system._broken, system._components, system._versions,
    system._health, system._log, system._rules, system._no_waiter, tickets._board, tickets._tickets,
    settings._settings, settings._accounts,
)


def on(address: str, draw: Callable[[dict], str]) -> Callable[[dict], str]:
    return lambda c: draw(c[address])


PANELS: dict[str, Callable[[dict], str]] = {
    **{panel.__name__: lambda c, panel=panel: panel(c["snapshot"]) for panel in SNAPSHOT_PANELS},
    "_runlog": on(RUNNING, lambda t: td._runlog(t["key"], t["model"])),
    "_journey": on(RUNNING, lambda t: td._journey(t["row"], t["model"])),
    "_next": on(RUNNING, lambda t: td._next(t["model"])),
    "_liverun": on(RUNNING, lambda t: td._liverun(t["row"], t["started"])),
    "_chart": on(RUNNING, lambda t: td._chart(t["key"], t["model"], t["chart"])),
    "_summary": on(SHIPPED, lambda t: td._summary(t["model"])),
    "_ended": on(SHIPPED, lambda t: td._ended(t["model"])),
    "_why": on(STOPPED, lambda t: td._why(t["model"])),
    "_exposure": on(RUNNING, lambda t: td._exposure(t["row"])),
    "ticket_cockpit_html": lambda c: cockpit.ticket_cockpit_html(c[ASKING]),
}

README = f"""# Dashboard leaf panel goldens

Server-rendered HTML of every leaf panel, one block per fixture variant, rendered from main {SOURCE}.
The ticket panels render running `{RUNNING}`, stopped `{STOPPED}` (`_why`) and shipped `{SHIPPED}`
(`_summary`, `_ended`, added by `_with_shipped`). `ticket_cockpit_html` renders waiting-max `{ASKING}`
with one open question added.

Regenerate:

    GOLDEN_UPDATE=1 python3 -m pytest -q -p no:cacheprovider tests/test_dashboard_golden.py
"""


def ticket(snapshot: dict, address: str) -> dict:
    found = td.resolve_ticket(snapshot, address, snapshot.get(RECORDS))
    assert found and found["address"] == address, address
    chart = snapshot.get("statechart")
    return {
        "row": found["row"],
        "ticket": found.get("ticket"),
        "chart": chart,
        "model": {**td._detail_model(found["row"], chart), "now": snapshot.get("generated_at_epoch")},
        "started": td.TICKET_KINDS[found["kind"]]["started"],
        "key": found["address"],
    }


def context(snapshot: dict) -> dict:
    return {
        "snapshot": snapshot,
        RUNNING: ticket(snapshot, RUNNING),
        STOPPED: ticket(snapshot, STOPPED),
        SHIPPED: ticket(_with_shipped(copy.deepcopy(snapshot)), SHIPPED),
        ASKING: {**next(row for row in snapshot["tickets"] if ticket_address(row) == ASKING), "questions": [QUESTION]},
    }


def render(root: Path) -> dict[str, str]:
    with pinned_version():
        contexts = {name: context(build(root / name)) for name, build in VARIANTS.items()}
        return {
            panel: "".join(f"<!-- variant: {name} -->\n{draw(c)}\n" for name, c in contexts.items())
            for panel, draw in PANELS.items()
        }


@pytest.fixture(scope="module")
def rendered(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    found = render(tmp_path_factory.mktemp("dashboard_golden"))
    if os.environ.get("GOLDEN_UPDATE") == "1":
        GOLDEN.mkdir(parents=True, exist_ok=True)
        for stale in set(GOLDEN.glob("*.html")) - {GOLDEN / f"{panel}.html" for panel in found}:
            stale.unlink()
        for panel, html in found.items():
            (GOLDEN / f"{panel}.html").write_bytes(html.encode("utf-8"))
        (GOLDEN / "README.md").write_text(README, encoding="utf-8")
    return found


def test_the_golden_files_are_exactly_the_leaf_panels(rendered: dict[str, str]) -> None:
    assert len(PANELS) == 35
    assert {path.stem for path in GOLDEN.glob("*.html")} == set(PANELS)


@pytest.mark.parametrize("panel", PANELS)
def test_the_panel_renders_byte_identical(rendered: dict[str, str], panel: str) -> None:
    assert rendered[panel] == (GOLDEN / f"{panel}.html").read_bytes().decode("utf-8")
