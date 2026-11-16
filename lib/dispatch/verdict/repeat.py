from __future__ import annotations

from dataclasses import dataclass

from lib.dispatch.helpers import _comment_link, _comment_order, _round_number, _scope
from lib.dispatch.transitions import write_workflow_state
from lib.dispatch.types import Comment, TrackedTicket
from lib.handoff import HandoffError, parse_comment, repeat_id
from lib.queue import JobRequest

_ACCEPTED_REVISE = (
    "queued-review-fix",
    "answered-fix",
    "capped-review",
    "waiting-max-repeat",
    "waiting-max-unsat",
)


@dataclass(frozen=True)
class RepeatBlock:
    rule_id: str
    earlier: Comment | None


@dataclass(frozen=True)
class _EarlierRevise:
    round: int
    comment: Comment
    blocking: frozenset[str]


def _handoff_round(comment: Comment) -> int | None:
    try:
        return _round_number(parse_comment(comment.body).as_dict().get("round"))
    except HandoffError:
        return None


class RepeatBlockMixin:
    def _repeat_trigger(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        reported_round: int | None,
    ) -> RepeatBlock | None:
        from lib.evidence.findings import blocking_ids

        handoff = parse_comment(comment.body)
        named = repeat_id(handoff)
        earlier = self._accepted_revises(ticket, reported_round)
        previous = next((item for item in earlier if item.round == (reported_round or 0) - 1), None)
        if named is not None:
            holding = next((item for item in earlier if named in item.blocking), previous)
            return RepeatBlock(named, None if holding is None else holding.comment)
        if previous is None:
            return None
        shared = blocking_ids(handoff.body) & previous.blocking
        return RepeatBlock(min(shared), previous.comment) if shared else None

    def _accepted_revises(
        self, ticket: TrackedTicket, reported_round: int | None
    ) -> list[_EarlierRevise]:
        from lib.evidence.findings import blocking_ids

        with self._connect() as connection:
            accepted = {
                str(row["comment_id"])
                for row in connection.execute(
                    f"""
                    SELECT comment_id FROM review_events
                    WHERE repo = ? AND ticket_id = ?
                      AND outcome IN ({",".join("?" * len(_ACCEPTED_REVISE))})
                    """,
                    (ticket.repo.lower(), ticket.ticket_id, *_ACCEPTED_REVISE),
                )
            }
        found = []
        for item in ticket.comments:
            if item.id not in accepted:
                continue
            try:
                handoff = parse_comment(item.body)
            except HandoffError:
                continue
            number = _round_number(handoff.as_dict().get("round"))
            if number is not None and number < (reported_round or 0):
                named = repeat_id(handoff)
                blocking = blocking_ids(handoff.body) | ({named} if named else set())
                found.append(_EarlierRevise(number, item, blocking))
        return sorted(found, key=lambda entry: (entry.round, _comment_order(entry.comment)), reverse=True)

    def _repeat_stops(self, ticket: TrackedTicket, comment: Comment, rule_id: str) -> int:
        with self._connect() as connection:
            parked = {
                str(row["comment_id"])
                for row in connection.execute(
                    """
                    SELECT comment_id FROM review_events
                    WHERE repo = ? AND ticket_id = ? AND outcome = 'waiting-max-repeat'
                    """,
                    (ticket.repo.lower(), ticket.ticket_id),
                )
            }
        stops = 1
        for item in ticket.comments:
            number = _handoff_round(item) if item.id in parked and item.id != comment.id else None
            earlier = None if number is None else self._repeat_trigger(ticket, item, number)
            if earlier is not None and earlier.rule_id == rule_id:
                stops += 1
        return stops

    def _repeat_detail(self, ticket: TrackedTicket, comment: Comment, block: RepeatBlock) -> str:
        earlier = "no earlier verdict" if block.earlier is None else _comment_link(block.earlier)
        stops = self._repeat_stops(ticket, comment, block.rule_id)
        fields = [
            f"repeat-block: {block.rule_id}",
            earlier,
            _comment_link(comment),
            f"stops={stops}",
            f"for={'factory' if stops == 1 else 'max'}",
        ]
        if ticket.changed_lines is not None:
            fields.append(f"changed_lines={ticket.changed_lines}")
        return ", ".join(fields)

    def _t3_repeat(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        block: RepeatBlock,
        reported_round: int | None,
    ) -> tuple[bool, str]:
        key = (ticket.repo.lower(), ticket.ticket_id)
        detail = self._repeat_detail(ticket, comment, block)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                """
                SELECT outcome FROM review_events
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (*key, comment.id),
            ).fetchone()
            if prior is not None:
                connection.commit()
                self._write_state_label(ticket, "waiting-max")
                return True, str(prior["outcome"])
            state = connection.execute(
                """
                SELECT workflow_state, review_round, generation FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                key,
            ).fetchone()
            if (
                state is None
                or state["workflow_state"] not in ("review", "awaiting-ok")
                or reported_round != int(state["review_round"]) + 1
            ):
                connection.commit()
                self._wake_comment_problem(ticket, comment, "stale", "stale-revise")
                return True, "stale-revise"
            generation = int(state["generation"])
            write_workflow_state(
                connection,
                ticket.repo,
                ticket.ticket_id,
                "waiting-max",
                source="t3-repeat",
                at=self.queue.now(),
                also={
                    "review_round": reported_round,
                    "paused_action": "fix",
                    "paused_recipient": ticket.factory.builder_harness,
                    "paused_head": ticket.current_head,
                    "paused_generation": generation,
                    "paused_question_id": comment.id,
                },
            )
            connection.execute(
                """
                INSERT INTO review_events (repo, ticket_id, comment_id, outcome)
                VALUES (?, ?, ?, 'waiting-max-repeat')
                """,
                (*key, comment.id),
            )
            self.queue.enqueue_in_transaction(
                connection,
                self._grok_request(
                    JobRequest(
                        repo=ticket.repo,
                        ticket_id=ticket.ticket_id,
                        action="clarify",
                        recipient="grok",
                        source_comment_id=comment.id,
                        timeout_seconds=ticket.job_timeout_minutes * 60,
                        generation=generation,
                        head=ticket.current_head,
                        detail=self._project_wake_detail(ticket, detail),
                    )
                ),
                dedupe_key=f"t3-repeat:{_scope(ticket.repo, ticket.ticket_id)}:{comment.id}",
            )
            connection.commit()
        self._write_state_label(ticket, "waiting-max")
        return True, "waiting-max-repeat"
