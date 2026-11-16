"""T7: stops with no known answer wake Grok Bot once (docs/SPEC.md section 4.2).

Events:

- kind=stuck from any trusted bot. Its state= names the stop: blocked,
  capped or refused; anything else is stuck.
- A comment with a header-shaped line (one starting with <!-- orch:v)
  that does not parse, from a trusted login. Untrusted ones are recorded
  and ignored. Prose that only mentions orch:v is not a header.

blocked and capped wake once per ticket and head: the reported head, or
the PR head when the report names none. Short heads are not expanded.
stuck wakes once per ticket, refused once per ticket, and an unparsed
comment once per comment. Config refused after intake, capped from T3 and
a reviewer's verdict=BLOCKED (#188) call Dispatcher.wake_grok_once directly.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from lib.dispatcher import Comment, Dispatcher, TrackedTicket
from lib.handoff import HandoffError, parse_comment

STOP_STATES = ("blocked", "capped", "refused")
_HEADER_SHAPED = re.compile(r"^\s*(?:<!--\s*|`)orch:v", re.MULTILINE)


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
        if fields["kind"] == "stuck":
            sender = fields.get("from")
            trusted = dispatcher._trusted_bots(comment)
            if sender is None or sender not in trusted:
                return True, "rejected-stuck-author"
            state = fields.get("state")
            reason = state if state in STOP_STATES else "stuck"
            detail = f"kind=stuck state={reason} from {sender}"
        else:
            return None
        if dispatcher.is_stuck(ticket):
            dispatcher._ensure_stuck_wake(ticket.repo, ticket.ticket_id)
            return True, "ticket-stuck"
        head = fields.get("head") or ticket.current_head
        dispatcher.wake_grok_once(
            ticket,
            reason=reason,
            source_comment_id=comment.id,
            detail=dispatcher._project_wake_detail(
                ticket,
                detail if head is None else f"{detail} on {head}",
            ),
            head=head,
        )
        return True, f"woke-grok-{reason}"

    def _unparsed(
        self,
        dispatcher: Dispatcher,
        ticket: TrackedTicket,
        comment: Comment,
    ) -> tuple[bool, str] | None:
        if _HEADER_SHAPED.search(comment.body) is None:
            return None
        if not dispatcher._trusted_bots(comment):
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
            ticket,
            reason="unparsed",
            source_comment_id=comment.id,
            detail=dispatcher._project_wake_detail(
                ticket,
                f"unparsed orch header from {comment.author}: {error}",
            ),
        )
        return True, "woke-grok-unparsed"
