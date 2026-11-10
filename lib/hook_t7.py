"""T7: stops with no known answer wake Grok Bot once (docs/SPEC.md section 4.2).

Events:

- kind=verdict verdict=BLOCKED from the ticket's reviewer bot.
- kind=stuck from any trusted bot. Its state= names the stop: blocked,
  capped or refused; anything else is stuck.
- A comment whose first line looks like an orch: header but does not
  parse, from a trusted login. Untrusted ones are recorded and ignored.

blocked, capped and stuck wake once per ticket and head, so repeated
reports of the same stop do not wake again. An unparsed comment wakes
once per comment. Config refused after intake, and capped from T3, call
Dispatcher.wake_grok_once directly.
"""

from __future__ import annotations

from collections.abc import Mapping

from lib.dispatcher import Comment, Dispatcher, TrackedTicket, _matching_head
from lib.handoff import HandoffError, parse_comment

STOP_STATES = ("blocked", "capped", "refused")


class T7Hook:
    def initialize(self, dispatcher: Dispatcher) -> None:
        return None

    def handle(
        self,
        dispatcher: Dispatcher,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str] | None,
    ) -> tuple[bool, str] | None:
        if fields is None:
            return self._unparsed(dispatcher, ticket, comment)
        if fields["kind"] == "verdict" and fields.get("verdict") == "BLOCKED":
            reviewer = f"{ticket.factory.reviewer_harness}-bot"
            if not dispatcher._trusted(comment, fields, reviewer, role="reviewer"):
                return True, "rejected-blocked-author"
            reason = "blocked"
            detail = f"verdict=BLOCKED from {reviewer}"
        elif fields["kind"] == "stuck":
            sender = dispatcher.trusted_logins.get(comment.author)
            if sender is None or fields.get("from") != sender:
                return True, "rejected-stuck-author"
            state = fields.get("state")
            reason = state if state in STOP_STATES else "stuck"
            detail = f"kind=stuck state={reason} from {sender}"
        else:
            return None
        if dispatcher.is_stuck(ticket):
            dispatcher._ensure_stuck_wake(ticket.repo, ticket.ticket_id)
            return True, "ticket-stuck"
        head = fields.get("head")
        if head is not None:
            head = _matching_head(head, ticket.current_head) or head
        dispatcher.wake_grok_once(
            repo=ticket.repo,
            ticket_id=ticket.ticket_id,
            reason=reason,
            source_comment_id=comment.id,
            timeout_seconds=ticket.job_timeout_minutes * 60,
            detail=detail if head is None else f"{detail} on {head}",
            once_key=head or "-",
            head=head,
        )
        return True, f"woke-grok-{reason}"

    def _unparsed(
        self,
        dispatcher: Dispatcher,
        ticket: TrackedTicket,
        comment: Comment,
    ) -> tuple[bool, str] | None:
        first = comment.body.lstrip().split("\n", 1)[0]
        if not (first.startswith("<!--") and "orch:" in first):
            return None
        if comment.author not in dispatcher.trusted_logins:
            return True, "ignored-unparsed-untrusted"
        if dispatcher.is_stuck(ticket):
            dispatcher._ensure_stuck_wake(ticket.repo, ticket.ticket_id)
            return True, "ticket-stuck"
        try:
            parse_comment(comment.body)
        except HandoffError as exc:
            error = str(exc)
        else:
            error = "unparsed"
        dispatcher.wake_grok_once(
            repo=ticket.repo,
            ticket_id=ticket.ticket_id,
            reason="unparsed",
            source_comment_id=comment.id,
            timeout_seconds=ticket.job_timeout_minutes * 60,
            detail=f"unparsed orch header from {comment.author}: {error}",
            once_key=comment.id,
        )
        return True, "woke-grok-unparsed"
