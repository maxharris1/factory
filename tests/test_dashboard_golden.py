from __future__ import annotations

import copy
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from lib.dashboard import RECORDS
from lib.dashboard_pages import (
    activity, cockpit, counts, efficiency, evals, lanes, routing, settings, system, tickets,
)
from lib.dashboard_pages import ticket_detail as td
from lib.dashboard_pages.common import ticket_address
from lib.dashboard_telemetry import rollup_panel
from tests.golden.pages import pinned_version
from tests.test_dashboard_layout import _held_more_than_running, _snapshot
from tests.test_dashboard_ticket_detail import SHIPPED, _with_shipped

GOLDEN = Path(__file__).parent / "golden" / "dashboard"
SOURCE = "a2bb7247d6e85731543c23ceabbd0f72ee98e0fa"
EXTENDED = "e0183d64d82d37d7a87ac7b1b6754224f8d19db0"
RUNNING = "maxharris1/cleffy#13"
STOPPED = "aut0-code/aut0#AUT-7"
ASKING = "maxharris1/tracy-harris-coaching#5"
QUEUED = "maxharris1/cleffy#12"
QUESTION = {"text": "Sign in with OAuth or an API key?", "options": ["OAuth", "Key"]}
VISUAL_QA = {
    "state": "awaiting-ok",
    "wait": {"why": "visual-qa-review", "comment_id": "55", "detail": None},
    "visual_qa": {"run": ["0123456789abcdef", "0123456789abcdef"], "media": [
        {"lane": 1, "path_parts": ["qa-1/01-landing.png"], "scenario": "landing", "result": "verified"},
        {"lane": 1, "path_parts": ["qa-1/walkthrough.webm"], "kind": "video", "result": "failed"}]},
}
VARIANTS = {"snapshot": _snapshot, "held_more_than_running": _held_more_than_running}

SNAPSHOT_PANELS = (
    lanes._lanes, lanes._just_happened, lanes._intake, cockpit._needs_max, activity._attention, activity._relay,
    efficiency._headroom, efficiency._rounds, efficiency._savings, efficiency._balance, efficiency._jobs_lifetime,
    rollup_panel, routing._routing, evals._evals, system._broken, system._components, system._versions,
    system._health, system._log, system._rules, system._no_waiter, tickets._board, tickets._tickets,
    settings._settings, settings._accounts, counts._counts,
)


def on(kind: str, draw: Callable[[dict], str]) -> Callable[[dict], str]:
    return lambda c: draw(c[kind])


KINDS = {"running": RUNNING, "shipped": SHIPPED, "stopped": STOPPED, "queued": QUEUED, "idle": ASKING}
SECTIONS = {"_runlog": lambda t: td._runlog(t["key"], t["model"]),
            **{f"_{name}": draw for name, draw in td.SECTIONS.items()}}


def renders(kind: str, section: str) -> bool:
    spec = td.TICKET_KINDS[kind]
    return section == "_runlog" or section[1:] in (*spec.get("lead", ()), *spec["sections"])


def section_panels(section: str, draw: Callable[[dict], str]) -> dict[str, Callable[[dict], str]]:
    first, *rest = (kind for kind in KINDS if renders(kind, section))
    return {section: on(first, draw), **{f"{section}.{kind}": on(kind, draw) for kind in rest}}


PANELS: dict[str, Callable[[dict], str]] = {
    **{panel.__name__: lambda c, panel=panel: panel(c["snapshot"]) for panel in SNAPSHOT_PANELS},
    **{name: panel for section, draw in SECTIONS.items() for name, panel in section_panels(section, draw).items()},
    "ticket_cockpit_html": lambda c: cockpit.ticket_cockpit_html({**c["asking"], "questions": [QUESTION]}),
    "ticket_cockpit_html.visual-qa": lambda c: cockpit.ticket_cockpit_html({**c["asking"], **VISUAL_QA}),
}

NET_NEW = (
    "lib/dashboard_pages/counts.py", "lib/dashboard_static/css/counts.css", "lib/dashboard_static/js/counts.js",
)
COPIED = (*NET_NEW, "config/routing.yml", "lib/role_router.py", "lib/dashboard_pages/cockpit.py")

REPRODUCE = f"""\
    git worktree add --detach /tmp/golden-$sha $sha
    cp tests/test_dashboard_golden.py /tmp/golden-$sha/tests/
{"".join(f"    cp {path} /tmp/golden-$sha/{path}\n" for path in COPIED)}\
    (cd /tmp/golden-$sha && GOLDEN_UPDATE=1 python3 -m pytest -q -p no:cacheprovider tests/test_dashboard_golden.py)
    diff -r tests/golden/dashboard /tmp/golden-$sha/tests/golden/dashboard"""

README = f"""# Dashboard leaf panel goldens

Server-rendered HTML of every leaf panel, one block per fixture variant. The 35 unsuffixed goldens were rendered
from main {SOURCE}; the `.<kind>` and `.visual-qa` goldens from main {EXTENDED}. Run with this harness and the
checked-in `config/routing.yml`, both commits render all 52 goldens byte for byte.

Each ticket page section is pinned for every ticket kind whose page shows it: running `{RUNNING}`, shipped
`{SHIPPED}` (added by `_with_shipped`), stopped `{STOPPED}`, queued `{QUEUED}` and idle `{ASKING}`.
`<section>.html` holds the first of those kinds that shows the section and `<section>.<kind>.html` each other one.
`ticket_cockpit_html` renders waiting-max `{ASKING}` with one open question added, and
`ticket_cockpit_html.visual-qa` the same ticket waiting on a visual QA review.

Regenerate from the checked-out code:

    GOLDEN_UPDATE=1 python3 -m pytest -q -p no:cacheprovider tests/test_dashboard_golden.py

Update mode deletes every `.html` here that the harness does not render. {SOURCE} has no harness and the one at
{EXTENDED} renders only the 35 unsuffixed goldens, so copy this harness into a checkout of either commit before
regenerating.
Neither commit has the panels added since, so copy their files too: {", ".join(f"`{path}`" for path in NET_NEW)}.
`_routing.html` renders the checked-in `config/routing.yml`, so copy it too, with `lib/role_router.py` to read its `shares` block.
`_needs_max.html` draws the inbox cards added since, so copy `lib/dashboard_pages/cockpit.py` too.
From the repo root, with `sha` set to one of them:

{REPRODUCE}
"""


def ticket(snapshot: dict, kind: str) -> dict:
    found = td.resolve_ticket(snapshot, KINDS[kind], snapshot.get(RECORDS))
    assert found and (found["address"], found["kind"]) == (KINDS[kind], kind), kind
    chart = snapshot.get("statechart")
    return {
        "row": found["row"],
        "ticket": found.get("ticket"),
        "chart": chart,
        "model": {**td._detail_model(found["row"], chart), "now": snapshot.get("generated_at_epoch")},
        "started": td.TICKET_KINDS[kind]["started"],
        "key": found["address"],
    }


def context(snapshot: dict) -> dict:
    shipped = _with_shipped(copy.deepcopy(snapshot))
    return {
        "snapshot": snapshot,
        **{kind: ticket(shipped if kind == "shipped" else snapshot, kind) for kind in KINDS},
        "asking": next(row for row in snapshot["tickets"] if ticket_address(row) == ASKING),
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
    assert len(PANELS) == 52
    assert {path.stem for path in GOLDEN.glob("*.html")} == set(PANELS)


@pytest.mark.parametrize("panel", PANELS)
def test_the_panel_renders_byte_identical(rendered: dict[str, str], panel: str) -> None:
    assert rendered[panel] == (GOLDEN / f"{panel}.html").read_bytes().decode("utf-8")


def test_the_committed_readme_is_the_one_update_mode_writes(rendered: dict[str, str]) -> None:
    assert (GOLDEN / "README.md").read_text(encoding="utf-8") == README


def reproduce_steps(scratch: Path, python: Path) -> str:
    inserted = {"/tmp/": shlex.quote(f"{scratch}/"), "python3 ": f"{shlex.quote(str(python))} "}
    return re.sub("|".join(map(re.escape, inserted)), lambda match: inserted[match.group(0)], REPRODUCE)


def test_the_reproduce_steps_keep_inserted_paths_intact_when_they_contain_the_replaced_text() -> None:
    scratch = Path("/tmp/python3 tests/golden steps")
    python = Path("/tmp/a venv/tmp/bin/python3")
    lines = [shlex.split(line) for line in reproduce_steps(scratch, python).splitlines()]
    assert lines[0][-2:] == [f"{scratch}/golden-$sha", "$sha"]
    assert lines[1][-1] == f"{scratch}/golden-$sha/tests/"
    assert [line[-1] for line in lines[2:-2]] == [f"{scratch}/golden-$sha/{path}" for path in COPIED]
    assert lines[-2][1] == f"{scratch}/golden-$sha"
    assert lines[-2][4] == str(python)
    assert lines[-1][-1] == f"{scratch}/golden-$sha/tests/golden/dashboard"


@pytest.mark.parametrize("sha", [SOURCE, EXTENDED])
def test_the_documented_steps_reproduce_the_goldens_at_each_cited_commit(tmp_path: Path, sha: str) -> None:
    if os.environ.get("GOLDEN_UPDATE") == "1":
        pytest.skip("the documented steps run this harness in update mode")
    root = Path(__file__).parent.parent
    if subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=root, capture_output=True).returncode:
        pytest.skip(f"{sha} is not in this clone")
    scratch = tmp_path / "golden steps"
    scratch.mkdir()
    python = tmp_path / "python bin" / "python3"
    python.parent.mkdir()
    python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    python.chmod(0o755)
    script = reproduce_steps(scratch, python)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PYTEST", "GOLDEN"))}
    try:
        steps = subprocess.run(["bash", "-euc", script], cwd=root, env={**env, "sha": sha},
                               capture_output=True, text=True, timeout=100)
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", scratch / f"golden-{sha}"], cwd=root,
                       capture_output=True)
    assert steps.returncode == 0, steps.stdout + steps.stderr
