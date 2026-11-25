"""Helpers every page uses, the base CSS and the script runtime (head and tail)."""

from __future__ import annotations

import html
import re
import urllib.parse

from lib.pr_title import label
from lib.dashboard_static import read_asset


CSS_BASE = read_asset("css/common_base.css")


CSS_PANEL = read_asset("css/common_panel.css")


CSS_PARTS = read_asset("css/common_parts.css")


CSS_MISC = read_asset("css/common_misc.css")


CSS_TAIL = read_asset("css/common_tail.css")


SCRIPT_HEAD = read_asset("js/common_head.js")


SCRIPT_GITHUB = read_asset("js/common_github.js")


SCRIPT_TELEMETRY = r"""
__TELEMETRY__
"""


SCRIPT_TAIL = read_asset("js/common_tail.js")


LIST_MAX = 12


_BOTS = ("claude", "codex", "cursor", "grok")


def _e(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return html.escape(str(value), quote=True)


def duration(seconds: object) -> str:
    """Like the script's dur(): 45s, 3m 05s, 2h 07m, 1d 4h."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return "—"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    if seconds < 86400:
        return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
    return f"{seconds // 86400}d {seconds % 86400 // 3600}h"


def _num(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    return _e(value)


def _short(repo: object) -> str:
    return str(repo).rsplit("/", 1)[-1]


def _ticket(row: dict, *, full: bool = False) -> str:
    repo = str(row["repo"]) if full else _short(row["repo"])
    return label(repo, str(row["ticket_id"]), row.get("source"), row.get("pr"))


_GH_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_GH_NUMBER = re.compile(r"[0-9]+")


def github_refs(row: dict) -> dict | None:
    """The row's GitHub repo, issue and PR numbers (ghRefs in the script), or None."""
    repo, source, pr = str(row.get("repo") or ""), row.get("source"), row.get("pr")
    if not _GH_REPO.fullmatch(repo):
        return None
    issue = source[1:] if isinstance(source, str) and source.startswith("#") and _GH_NUMBER.fullmatch(source[1:]) else None
    pr = str(pr) if pr is not None and _GH_NUMBER.fullmatch(str(pr)) else None
    return {"repo": repo, "issue": issue, "pr": pr} if issue or pr else None


def github_links(row: dict) -> str:
    """The issue and PR links on github.com (ghLinks in the script)."""
    refs = github_refs(row)
    if refs is None:
        return ""
    links = "".join(
        f'<a href="https://github.com/{_e(refs["repo"])}/{path}/{number}" rel="noopener noreferrer" '
        f'target="_blank">{text}</a>'
        for path, number, text in (("issues", refs["issue"], "issue ↗"), ("pull", refs["pr"], "PR ↗"))
        if number
    )
    return f'<span class="gh">{links}</span>'


def _job_label(row: dict) -> str:
    if row.get("tracker") == "linear":
        pr = row.get("pr")
        return f"{row['ticket_id']} (PR #{pr})" if pr else str(row["ticket_id"])
    return _ticket(row)


def _tile_label(row: dict) -> str:
    if row.get("tracker") == "linear":
        return str(row["ticket_id"])
    return label(_short(row["repo"]), str(row["ticket_id"]), row.get("source"), None)


def anchor(job: object) -> str:
    return "job-" + re.sub(r"[^A-Za-z0-9_-]", "-", str(job))


def _thread_key(row: dict) -> str:
    return f"{str(row['repo']).lower()}#{row['ticket_id']}"


def ticket_address(row: dict) -> str:
    return f"{row['repo']}#{row['ticket_id']}"


ENCODE_URI_COMPONENT_SAFE = "-_.!~*'()"


def ticket_page_href(row: dict, open_ci: bool = False) -> str:
    href = "ticket.html#t=" + urllib.parse.quote(ticket_address(row), safe=ENCODE_URI_COMPONENT_SAFE)
    return href + "&ci=1" if open_ci else href


def _b(value: object, cls: str = "") -> str:
    attr = f' class="{cls}"' if cls else ""
    return f"<b{attr}>{_num(value)}</b>"


def _span(cls: str | None, text: object) -> str:
    attr = f' class="{cls}"' if cls else ""
    return f"<span{attr}>{_e(text)}</span>"


def _bot(name: object) -> str:
    return _span(f"bot {name if name in _BOTS else 'other'}", name)


def _chip(text: object, cls: str = "muted") -> str:
    return _span(f"chip {cls}", text)


def checks_named(ci: dict) -> str:
    return ", ".join(ci["checks"]) or "checks pending"


def checks_link(ticket: dict) -> str:
    refs = github_refs(ticket)
    if refs is None or refs["pr"] is None:
        return ""
    return f'<a href="https://github.com/{_e(refs["repo"])}/pull/{refs["pr"]}/checks">checks</a>'


def _panel(
    ident: str, title: str, inner: str, *, count: object = None, count_html: str | None = None,
    extra: str = "", hidden: bool = False, heading_id: str | None = None, count_cls: str = "count",
    count_id: str | None = None,
) -> str:
    count_attr = f' id="{count_id}"' if count_id else ""
    head = _span(None, title) + ("" if count is None else f'<span class="{count_cls}"{count_attr}>{_e(count)}</span>')
    if count_html is not None:
        head += f'<span class="count">{count_html}</span>'
    classes = f"panel {extra}".strip()
    heading = f'<h2 id="{heading_id}">' if heading_id else "<h2>"
    return (
        f'<section class="{classes}" id="{ident}" aria-label="{_e(title)}"{" hidden" if hidden else ""}>'
        f"{heading}{head}</h2>{inner}</section>"
    )


def _feed(rows: list, empty: str, draw) -> str:
    if not rows:
        return f'<p class="empty">{_e(empty)}</p>'
    items = "".join(f"<li>{draw(row)}</li>" for row in rows[:LIST_MAX])
    more = (
        f'<p class="more">+{len(rows) - LIST_MAX} more</p>' if len(rows) > LIST_MAX else ""
    )
    return f'<ul class="feed">{items}</ul>{more}'


def _when(seconds: object, prefix: str = "", suffix: str = "") -> str:
    return _span("when", prefix + duration(seconds) + suffix)


# #75: lib/job_state.py's state of a running job, as a chip (JOB_STATE in the script).
_JOB_STATE = {
    "claimed": ("claimed, not started", "warn"),
    "stale": ("stale heartbeat", "warn"),
    "dead": ("runner dead", "bad"),
    "runner-gone": ("runner gone, worker alive", "warn"),
}


def _state_chip(row: dict) -> str:
    found = _JOB_STATE.get(row.get("stale_cause") or row.get("job_state"))  # type: ignore[arg-type]
    return _chip(found[0], found[1]) if found else ""


def _td(value: object, cls: str | None = None) -> str:
    attr = f' class="{cls}"' if cls else ""
    return f"<td{attr}>{_e(value)}</td>"
