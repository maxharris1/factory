"""Append dispatcher.log, the eval report's input, once per poll (#545).

The lines come from rows already committed: ticket_transitions (enter,
then end for a terminal state), Grok jobs (wake) and handled_times
(comment, when this poll read the comment and it parses as a handoff).
eval_log_cursor holds the last rowid exported per table. The grammar is
lib.eval_report's.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from lib.dispatch.constants import TERMINAL_STATES
from lib.eval_log import append_lines, erase_with_spaces
from lib.eval_report import _PR, _REPO, _STATE, _TICKET, _TOKEN, _format_time
from lib.handoff import HandoffError, parse_comment


class _Line(NamedTuple):
    at: float
    text: str


def remember_repo(connection: sqlite3.Connection, repo: str) -> None:
    connection.execute(
        "INSERT INTO eval_log_repos (repo_key, repo) VALUES (?, ?) "
        "ON CONFLICT (repo_key) DO UPDATE SET repo = excluded.repo",
        (repo.lower(), repo),
    )


def _finish_batch(log: Path, offset: int, batch: list[bytes]) -> None:
    tail = b""
    if log.exists():
        with log.open("rb") as handle:
            handle.seek(offset)
            tail = handle.read()
    done = 0
    position = offset
    for line in tail.splitlines(keepends=True):
        if done == len(batch):
            break
        if line == batch[done]:
            done += 1
        else:
            content = line.removesuffix(b"\n")
            body = content.lstrip(b" ")
            k = len(content) - len(body)
            if body and batch[done][k : len(content)] == body:
                erase_with_spaces(log, position + k, len(body))
        position += len(line)
    if done < len(batch):
        append_lines(log, batch[done:])


_NON_TOKEN = re.compile(r"[^a-z0-9]+")


def _handled_token(raw: str) -> str | None:
    """Return a valid eval_report token for source_comment_id, sanitizing synthetic IDs with ':'."""
    if not raw or raw == "-":
        return None
    if _TOKEN.fullmatch(raw) is not None:
        return raw
    cleaned = _NON_TOKEN.sub("-", raw.lower()).strip("-")
    return cleaned if cleaned and _TOKEN.fullmatch(cleaned) is not None else None


class EvalLogMixin:
    _eval_repos: dict[str, str]

    def _export_eval_log(self) -> None:
        try:
            self._append_eval_log()
        except Exception as exc:
            self.record_poll_exception("-", "-", exc, prefix="eval-log:")

    def _append_eval_log(self) -> None:
        log = self.state_path.with_name("dispatcher.log")
        self._flush_eval_log(log)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for ticket in self._polled.values():
                remember_repo(connection, ticket.repo)
            self._eval_repos = {
                str(row["repo_key"]): str(row["repo"])
                for row in connection.execute("SELECT repo_key, repo FROM eval_log_repos")
            }
            cursor = {
                str(row["source"]): int(row["last"])
                for row in connection.execute("SELECT source, last FROM eval_log_cursor")
            }
            transitions = connection.execute(
                "SELECT rowid AS seq, repo, ticket_id, to_state, at FROM ticket_transitions "
                "WHERE rowid > ? ORDER BY rowid",
                (cursor["ticket_transitions"],),
            ).fetchall()
            jobs = connection.execute(
                "SELECT rowid AS seq, repo, ticket_id, recipient, action, source_comment_id, "
                "created_at FROM jobs WHERE rowid > ? ORDER BY rowid",
                (cursor["jobs"],),
            ).fetchall()
            handled = connection.execute(
                "SELECT rowid AS seq, repo, ticket_id, comment_id, handled_at FROM handled_times "
                "WHERE rowid > ? ORDER BY rowid",
                (cursor["handled_times"],),
            ).fetchall()
            lines: list[_Line] = []
            for row in transitions:
                lines.extend(self._transition_lines(row))
            for row in jobs:
                lines.extend(self._wake_lines(row))
            for row in handled:
                lines.extend(self._comment_lines(log.parent, row))
            if lines:
                text = "".join(
                    f"{line.text}\n" for line in sorted(lines, key=lambda line: line.at)
                )
                connection.execute(
                    "INSERT INTO eval_log_pending (id, offset, text) VALUES (1, ?, ?)",
                    (log.stat().st_size if log.exists() else 0, text),
                )
            advanced = {
                "ticket_transitions": transitions,
                "jobs": jobs,
                "handled_times": handled,
            }
            connection.executemany(
                "UPDATE eval_log_cursor SET last = ? WHERE source = ?",
                [(int(rows[-1]["seq"]), source) for source, rows in advanced.items() if rows],
            )
            connection.commit()
        self._flush_eval_log(log)

    def _flush_eval_log(self, log: Path) -> None:
        with self._connect() as connection:
            pending = connection.execute(
                "SELECT offset, text FROM eval_log_pending WHERE id = 1"
            ).fetchone()
        if pending is None:
            return
        batch = str(pending["text"]).encode("utf-8").splitlines(keepends=True)
        _finish_batch(log, int(pending["offset"]), batch)
        with self._connect() as connection:
            connection.execute("DELETE FROM eval_log_pending WHERE id = 1")

    def _run_prefix(self, at: float, repo: str, ticket_id: str) -> str | None:
        ticket = self._polled.get((repo.lower(), ticket_id))
        spelled = ticket.repo if ticket is not None else self._eval_repos.get(repo.lower(), repo)
        if _REPO.fullmatch(spelled) is None or _TICKET.fullmatch(ticket_id) is None:
            return None
        when = _format_time(datetime.fromtimestamp(at, timezone.utc))
        pr = ticket.pr_number if ticket is not None else None
        pr_field = f" pr={pr}" if pr is not None and _PR.fullmatch(pr) else ""
        return f"{when} repo={spelled} ticket={ticket_id}{pr_field}"

    def _transition_lines(self, row: sqlite3.Row) -> list[_Line]:
        at, state = float(row["at"]), str(row["to_state"])
        prefix = self._run_prefix(at, row["repo"], row["ticket_id"])
        if prefix is None or _STATE.fullmatch(state) is None:
            return []
        lines = [_Line(at, f"{prefix} enter state={state}")]
        if state in TERMINAL_STATES:
            lines.append(_Line(at, f"{prefix} end"))
        return lines

    def _wake_lines(self, row: sqlite3.Row) -> list[_Line]:
        at, reason = float(row["created_at"]), str(row["action"])
        if row["recipient"] != "grok" or _TOKEN.fullmatch(reason) is None:
            return []
        prefix = self._run_prefix(at, row["repo"], row["ticket_id"])
        if prefix is None:
            return []
        handled = _handled_token(str(row["source_comment_id"] or ""))
        handled_field = f" handled={handled}" if handled is not None else ""
        wasted_field = " wasted=yes" if reason == "qa-head-moved" else ""
        return [
            _Line(at, f"{prefix} wake role=orchestrator reason={reason}{wasted_field}{handled_field}")
        ]

    def _comment_lines(self, root: Path, row: sqlite3.Row) -> list[_Line]:
        ticket = self._polled.get((str(row["repo"]).lower(), row["ticket_id"]))
        comment_id = str(row["comment_id"])
        if ticket is None or _TOKEN.fullmatch(comment_id) is None:
            return []
        body = next((item.body for item in ticket.comments if item.id == comment_id), None)
        if body is None:
            return []
        try:
            parse_comment(body)
        except HandoffError:
            return []
        at = float(row["handled_at"])
        prefix = self._run_prefix(at, row["repo"], row["ticket_id"])
        if prefix is None:
            return []
        relative = f"comments/{ticket.repo}/{ticket.ticket_id}/{comment_id}.md"
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return [_Line(at, f"{prefix} comment path={relative}")]

