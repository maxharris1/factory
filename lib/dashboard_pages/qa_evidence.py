from __future__ import annotations

import html
import json
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote

Kind = Literal["image", "video", "file", "missing"]

MISSING = "evidence recycled or missing"
_LANE = re.compile(r"lane-[1-9][0-9]*")
_KINDS: dict[str, Kind] = {
    **dict.fromkeys((".png", ".jpg", ".jpeg", ".webp"), "image"),
    **dict.fromkeys((".webm", ".mp4"), "video"),
}
_RENDER: dict[Kind, Callable[[str, str], str]] = {
    "image": lambda href, name: f'<img src="{href}" alt="{name}">',
    "video": lambda href, name: f'<video controls src="{href}"></video>',
    "file": lambda href, name: f'<a href="{href}">{name}</a>',
    "missing": lambda _href, name: f'<p class="missing">{name}: {MISSING}</p>',
}
_CSS = (
    "body{font:14px/1.5 system-ui,sans-serif;margin:2em;background:#1a1b26;color:#c0caf5}"
    "a{color:#7dcfff}img,video{display:block;max-width:100%;margin:.5em 0}"
    ".missing{color:#ff9e64}.result{color:#a9b1d6;font-weight:normal}"
)


@dataclass(frozen=True)
class Evidence:
    path: str
    kind: Kind


@dataclass(frozen=True)
class Scenario:
    id: str
    result: str
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class Lane:
    n: int
    scenarios: tuple[Scenario, ...] | None


def run_page(run_folder: Path, run_id: str) -> str:
    title = html.escape(f"QA run {run_id}")
    lanes = "".join(_lane_html(lane, run_id) for lane in _lanes(run_folder))
    body = lanes or f'<p class="missing">{MISSING}</p>'
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{title}</title>'
        f"<style>{_CSS}</style></head><body><h1>{title}</h1>{body}</body></html>\n"
    )


def _lanes(run_folder: Path) -> list[Lane]:
    try:
        with os.scandir(run_folder) as entries:
            numbers = sorted(
                int(entry.name[len("lane-"):])
                for entry in entries
                if _LANE.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False)
            )
    except OSError:
        return []
    return [_read_lane(run_folder / f"lane-{n}", n) for n in numbers]


def _read_lane(folder: Path, n: int) -> Lane:
    try:
        result = json.loads((folder / "result.json").read_bytes())
        manifest = json.loads((folder / "manifest.json").read_bytes())
        scenarios = [(item["id"], item["result"]) for item in result["scenarios"]]
        paths = [entry["path"] for entry in manifest["files"]]
    except (OSError, ValueError, RecursionError, TypeError, KeyError):
        return Lane(n, None)
    if not all(isinstance(text, str) for text in (*paths, *(text for pair in scenarios for text in pair))):
        return Lane(n, None)
    evidence = [_evidence(folder, path) for path in paths]
    return Lane(n, tuple(
        Scenario(id, result, tuple(item for item in evidence if item.path.startswith(f"{id}/")))
        for id, result in scenarios
    ))


def _evidence(lane_folder: Path, path: str) -> Evidence:
    parts = path.split("/")
    if "\x00" in path or any(not part or part.startswith(".") for part in parts):
        return Evidence(path, "missing")
    try:
        on_disk = stat.S_ISREG(os.lstat(lane_folder.joinpath(*parts)).st_mode)
    except OSError:
        on_disk = False
    return Evidence(path, _KINDS.get(os.path.splitext(path)[1].lower(), "file") if on_disk else "missing")


def _lane_html(lane: Lane, run_id: str) -> str:
    heading = f"<h2>Lane {lane.n}</h2>"
    if lane.scenarios is None:
        return f'<section>{heading}<p class="missing">{MISSING}</p></section>'
    base = f"{quote(run_id, safe='')}/lane-{lane.n}"
    return f"<section>{heading}{''.join(_scenario_html(scenario, base) for scenario in lane.scenarios)}</section>"


def _scenario_html(scenario: Scenario, base: str) -> str:
    items = "".join(
        _RENDER[item.kind](
            html.escape("/".join((base, *(quote(part, safe="") for part in item.path.split("/"))))),
            html.escape(item.path),
        )
        for item in scenario.evidence
    )
    return (
        f"<article><h3>{html.escape(scenario.id)} "
        f'<span class="result">{html.escape(scenario.result)}</span></h3>{items}</article>'
    )
