"""Mission Control daily cockpit: Needs Max / Product Inbox & Visual QA Gate (E4-1).

Server-side rendering companion to `lib/dashboard_static/js/cockpit.js` and
`lib/dashboard_static/css/cockpit.css` for `index.html` and `ticket.html`.
"""

from __future__ import annotations

import re

from lib.dashboard_pages.activity import _WHY, _comment_href
from lib.dashboard_pages.common import (
    _chip,
    _e,
    _panel,
    _span,
    _ticket,
    _when,
    ticket_address,
    ticket_page_href,
)
from lib.dashboard_static import read_asset

CSS = read_asset("css/cockpit.css")
SCRIPT = read_asset("js/cockpit.js")

_QA_PATH = re.compile(r"^/qa/runs/[A-Za-z0-9._-]{1,64}(?:/lane-[1-9][0-9]*/[^\s\"'<>]+)?$")


def _safe_qa_url(url: object) -> str | None:
    if not isinstance(url, str) or ".." in url or "//" in url:
        return None
    return url if _QA_PATH.fullmatch(url) else None


def _run_url_of(vqa: dict) -> str | None:
    direct = _safe_qa_url(vqa.get("run_url"))
    if direct is not None:
        return direct
    run_parts = vqa.get("run")
    if isinstance(run_parts, list) and all(isinstance(p, str) for p in run_parts):
        return _safe_qa_url("/qa/runs/" + "".join(run_parts))
    return None


def _media_url_of(vqa: dict, item: dict) -> str | None:
    direct = _safe_qa_url(item.get("url"))
    if direct is not None:
        return direct
    run_url = _run_url_of(vqa)
    lane = item.get("lane")
    path_parts = item.get("path_parts")
    if (
        run_url is None
        or not isinstance(lane, int)
        or lane < 1
        or not isinstance(path_parts, list)
        or not all(isinstance(p, str) for p in path_parts)
    ):
        return None
    return _safe_qa_url(f"{run_url}/lane-{lane}/{''.join(path_parts)}")


def inbox_tickets(snapshot: dict) -> list[dict]:
    """Tracked tickets currently waiting on Max (`waiting-max` or `awaiting-ok`)."""
    items = [
        row
        for row in snapshot.get("tickets") or ()
        if isinstance(row, dict) and row.get("state") in ("waiting-max", "awaiting-ok")
    ]

    def rank(row: dict) -> tuple[int, str, str]:
        wait = row.get("wait") if isinstance(row.get("wait"), dict) else {}
        why = wait.get("why") or ""
        order = (
            0 if why == "visual-qa-review"
            else 1 if why == "question"
            else 2 if row.get("state") == "awaiting-ok"
            else 3
        )
        return order, str(row.get("repo") or "").casefold(), str(row.get("ticket_id") or "")

    return sorted(items, key=rank)


def _media_gallery_html(vqa: dict) -> str:
    media = vqa.get("media") if isinstance(vqa.get("media"), list) else []
    if not media:
        run_url = _run_url_of(vqa)
        link = (
            f'<a href="{_e(run_url)}" target="_blank" rel="noopener noreferrer">open QA run</a>'
            if run_url
            else "pending upload"
        )
        return f'<p class="empty">Visual QA evidence {link}</p>'
    figures: list[str] = []
    for item in media:
        if not isinstance(item, dict):
            continue
        url = _media_url_of(vqa, item)
        if url is None:
            continue
        path_parts = item.get("path_parts")
        joined_path = (
            "".join(path_parts)
            if isinstance(path_parts, list) and all(isinstance(p, str) for p in path_parts)
            else item.get("path")
        )
        label = str(item.get("scenario") or joined_path or "evidence")
        if item.get("kind") == "video":
            body = f'<video controls preload="metadata" src="{_e(url)}"></video>'
        else:
            body = f'<img src="{_e(url)}" alt="{_e(label)}" loading="lazy">'
        result = item.get("result")
        badge = ""
        if isinstance(result, str) and result:
            tone = "ok" if result == "verified" else "bad" if result == "failed" else "muted"
            badge = _chip(result, tone)
        figures.append(
            f'<figure class="ck-media">{body}<figcaption>'
            f'<a href="{_e(url)}" target="_blank" rel="noopener noreferrer">{_e(label)}</a>'
            f"{badge}</figcaption></figure>"
        )
    return f'<div class="ck-gallery">{"".join(figures)}</div>' if figures else ""


def _questions_html(row: dict) -> str:
    questions = row.get("questions") if isinstance(row.get("questions"), list) else []
    if not questions:
        return ""
    items: list[str] = []
    for entry in questions:
        if not isinstance(entry, dict) or not entry.get("text"):
            continue
        opts = entry.get("options") if isinstance(entry.get("options"), list) else []
        opt_html = (
            f'<ul class="ck-qopts">{"".join(f"<li>{_e(opt)}</li>" for opt in opts)}</ul>'
            if opts
            else ""
        )
        items.append(
            f'<li class="ck-qitem"><div class="ck-qtext">{_e(entry["text"])}</div>{opt_html}</li>'
        )
    return f'<ul class="ck-qlist">{"".join(items)}</ul>' if items else ""


def _cockpit_card(row: dict, *, show_ticket_link: bool) -> str:
    wait = row.get("wait") if isinstance(row.get("wait"), dict) else {}
    why = str(wait.get("why") or row.get("state") or "")
    is_visual = why == "visual-qa-review"
    is_question = why == "question"
    cls = "ck-card" + (" ck-visual" if is_visual else " ck-question" if is_question else "")
    badge = _chip("visual-qa-review" if is_visual else str(row.get("state") or ""),
                  "accent" if is_visual else "warn")
    who = (
        f'<a class="ck-tk" href="{_e(ticket_page_href(row))}">{_e(_ticket(row))}</a>'
        if show_ticket_link
        else _span("ck-tk", _ticket(row))
    )
    meta = _span("ck-meta", _WHY.get(why, why))
    links: list[str] = []
    if wait:
        chref = _comment_href(row)
        if chref:
            links.append(
                f'<a href="{_e(chref)}" rel="noopener noreferrer" target="_blank">comment ↗</a>'
            )
    vqa = row.get("visual_qa") if isinstance(row.get("visual_qa"), dict) else None
    if vqa:
        run_url = _run_url_of(vqa)
        if run_url:
            links.append(
                f'<a href="{_e(run_url)}" rel="noopener noreferrer" target="_blank">full QA report ↗</a>'
            )
    link_bar = f'<span class="ck-links">{"".join(links)}</span>'
    gallery = _media_gallery_html(vqa) if vqa else ""
    questions = _questions_html(row)
    return (
        f'<article class="{cls}" data-cockpit-ticket="{_e(ticket_address(row))}">'
        f'<div class="ck-head">{badge}{who}{meta}{link_bar}</div>'
        f"{gallery}{questions}</article>"
    )


def _inbox_card(row: dict) -> str:
    wait = row.get("wait") if isinstance(row.get("wait"), dict) else {}
    why = str(wait.get("why") or row.get("state") or "")
    is_visual = why == "visual-qa-review"
    is_question = why == "question"
    cls = "ck-card ck-inbox" + (" ck-visual" if is_visual else " ck-question" if is_question else "")
    badge = _chip("visual-qa-review" if is_visual else str(row.get("state") or ""),
                  "accent" if is_visual else "warn")
    who = f'<a class="ck-tk" href="{_e(ticket_page_href(row))}">{_e(_ticket(row))}</a>'
    links: list[str] = []
    if wait:
        chref = _comment_href(row)
        if chref:
            links.append(
                f'<a href="{_e(chref)}" rel="noopener noreferrer" target="_blank">comment ↗</a>'
            )
    vqa = row.get("visual_qa") if isinstance(row.get("visual_qa"), dict) else None
    if vqa:
        run_url = _run_url_of(vqa)
        if run_url:
            links.append(
                f'<a href="{_e(run_url)}" rel="noopener noreferrer" target="_blank">full QA report ↗</a>'
            )
    age = row.get("state_age_seconds")
    waited = _when(age, "waited ") if isinstance(age, (int, float)) and not isinstance(age, bool) else ""
    title = row.get("title")
    title_html = f'<div class="ck-title">{_e(title)}</div>' if title else ""
    gallery = _media_gallery_html(vqa) if vqa else ""
    return (
        f'<article class="{cls}" data-cockpit-ticket="{_e(ticket_address(row))}"><div class="ck-main">'
        f'<div class="ck-head">{badge}{who}<span class="ck-links">{"".join(links)}</span></div>'
        f'<div class="ck-why">{_span(None, _WHY.get(why, why))}{waited}</div>'
        f"{title_html}{gallery}{_questions_html(row)}</div></article>"
    )


def _needs_max(snapshot: dict) -> str:
    """Needs Max / Product Inbox panel for `index.html`."""
    items = inbox_tickets(snapshot)
    count = f"{len(items)} waiting on you" if items else "all clear"
    inner = (
        f'<div class="ck-grid">{"".join(_inbox_card(row) for row in items)}</div>'
        if items
        else '<p class="empty">No open product questions or visual QA holds waiting on you.</p>'
    )
    return _panel("needs-max", "Needs Max · Product Inbox", inner, count=count)


def has_ticket_cockpit(ticket: dict | None) -> bool:
    """Whether `ticket.html` should render the inline Needs Max / Visual QA cockpit panel."""
    if not isinstance(ticket, dict) or ticket.get("state") not in ("waiting-max", "awaiting-ok"):
        return False
    wait = ticket.get("wait") if isinstance(ticket.get("wait"), dict) else {}
    return bool(
        ticket.get("visual_qa")
        or ticket.get("questions")
        or wait.get("why") == "visual-qa-review"
    )


def ticket_cockpit_html(ticket: dict | None) -> str:
    """Inline Needs Max / Visual QA Gate panel for `ticket.html`."""
    if not has_ticket_cockpit(ticket):
        return ""
    assert ticket is not None
    wait = ticket.get("wait") if isinstance(ticket.get("wait"), dict) else {}
    sub = str(wait.get("why") or ticket.get("state") or "")
    return (
        '<section class="jd-panel ck-ticket" id="ticket-cockpit">'
        f'<h2>{_span("t", "Needs Max · Product & Visual QA Gate")}{_span("count", sub)}</h2>'
        f"{_cockpit_card(ticket, show_ticket_link=False)}</section>"
    )
