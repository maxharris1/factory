"""Append dispatcher.log, the eval report's input, once per poll (#545).

The lines come from rows already committed: ticket_transitions (enter,
then end for a terminal state), Grok jobs (wake) and handled_times
(comment, when this poll read the comment and it parses as a handoff).
eval_log_cursor holds the last rowid exported per table. The log is
appended and fsynced before the cursor moves, so a crash repeats lines
and never drops them. The grammar is lib.eval_report's.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from lib.eval_log import append_lines
from lib.eval_report import _PR, _REPO, _STATE, _TICKET, _TOKEN, _format_time
from lib.handoff import HandoffError, parse_comment

TERMINAL_STATES = frozenset({"merged", "done", "refused", "abandoned"})


class _Line(NamedTuple):
    at: float
    text: str


class EvalLogMixin:
    def _export_eval_log(self) -> None:
        try:
            self._append_eval_log()
        except Exception as exc:
            self.record_poll_exception("-", "-", exc, prefix="eval-log:")

    def _append_eval_log(self) -> None:
        log = self.state_path.with_name("dispatcher.log")
        with self._connect() as connection:
            connection.execute("BEGIN")
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
            connection.commit()
        lines: list[_Line] = []
        for row in transitions:
            lines.extend(self._transition_lines(row))
        for row in jobs:
            lines.extend(self._wake_lines(row))
        for row in handled:
            lines.extend(self._comment_lines(log.parent, row))
        if lines:
            append_lines(log, [line.text for line in sorted(lines, key=lambda line: line.at)])
        advanced = {
            "ticket_transitions": transitions,
            "jobs": jobs,
            "handled_times": handled,
        }
        with self._connect() as connection:
            connection.executemany(
                "UPDATE eval_log_cursor SET last = ? WHERE source = ?",
                [(int(rows[-1]["seq"]), source) for source, rows in advanced.items() if rows],
            )

    def _run_prefix(self, at: float, repo: str, ticket_id: str) -> str | None:
        ticket = self._polled.get((repo.lower(), ticket_id))
        spelled = ticket.repo if ticket is not None else repo
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
        handled = str(row["source_comment_id"])
        handled_field = f" handled={handled}" if _TOKEN.fullmatch(handled) else ""
        return [_Line(at, f"{prefix} wake role=orchestrator reason={reason}{handled_field}")]

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

