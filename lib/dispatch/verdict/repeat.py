from __future__ import annotations

from dataclasses import dataclass

from lib.dispatch.helpers import _comment_link, _round_number, _scope
from lib.dispatch.transitions import write_workflow_state
from lib.dispatch.types import Comment, TrackedTicket
from lib.handoff import HandoffError, parse_comment, repeat_id
from lib.queue import JobRequest

_ACCEPTED_REVISE = ("queued-review-fix", "answered-fix", "capped-review")


@dataclass(frozen=True)
class RepeatBlock:
    rule_id: str
    earlier: Comment | None


class RepeatBlockMixin:
    def _repeat_trigger(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        reported_round: int | None,
    ) -> RepeatBlock | None:
        """The rule a REVISE blocks on again (#189), or None.

        The reviewer names it with repeat=, in the header or as the first body
        line. Without it, a blocking f-<n> or unmet ac-<n> id the REVISE T3
        accepted for the round before also blocked counts. Free text never does.
        """
        from lib.evidence.findings import blocking_ids

        handoff = parse_comment(comment.body)
        named = repeat_id(handoff)
        earlier = self._accepted_revises(ticket, reported_round)
        previous = next(
            (item for number, item in earlier if number == (reported_round or 0) - 1), None
        )
        if named is not None:
            holding = next(
                (item for _, item in earlier if named in blocking_ids(parse_comment(item.body).body)),
                previous,
            )
            return RepeatBlock(named, holding)
        if previous is None:
            return None
        shared = blocking_ids(handoff.body) & blocking_ids(parse_comment(previous.body).body)
        return RepeatBlock(min(shared), previous) if shared else None

    def _accepted_revises(
        self, ticket: TrackedTicket, reported_round: int | None
    ) -> list[tuple[int, Comment]]:
        """The REVISEs T3 accepted before reported_round, newest first."""
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
                number = _round_number(parse_comment(item.body).as_dict().get("round"))
            except HandoffError:
                continue
            if number is not None and number < (reported_round or 0):
                found.append((number, item))
        return sorted(found, key=lambda pair: (pair[0], pair[1].created_at), reverse=True)

    def _t3_repeat(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        block: RepeatBlock,
        reported_round: int | None,
    ) -> tuple[bool, str]:
        """Park the ticket at waiting-max with the fix paused and wake Grok once."""
        key = (ticket.repo.lower(), ticket.ticket_id)
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
            earlier = "no earlier verdict" if block.earlier is None else _comment_link(block.earlier)
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
                        detail=self._project_wake_detail(
                            ticket,
                            f"repeat-block: {block.rule_id}, {earlier}, {_comment_link(comment)}",
                        ),
                    )
                ),
                dedupe_key=f"t3-repeat:{_scope(ticket.repo, ticket.ticket_id)}:{comment.id}",
            )
            connection.commit()
        self._write_state_label(ticket, "waiting-max")
        return True, "waiting-max-repeat"
