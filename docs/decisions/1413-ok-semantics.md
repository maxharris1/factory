# #1413: read a `kind=ok` by whether this PR is held

- **Decision:** On a repo whose hold applies, the poller records each approver `kind=ok` by whether this PR is held at its current head, not by whether the repo has a `hold:`.
- **When:** 2026-11-23 ET, by Factory decided (technical).
- **Source:** plan https://github.com/maxharris1/factory/pull/1414#issuecomment-7106633802 (choice 3), split into #1418.

## Why

Before this, every `kind=ok` on a `hold_applies` repo (`ship: merge` or `ship: handoff` with `hold:`) was recorded `hold-ok`, even when the PR carried no hold label and touched no hold path. The record then said an ok cleared a hold that never existed, and nothing told anyone the ok had moved nothing (#1401).

## Outcomes

The poller reads the ok against the hold decision at the PR's current head (the same `lib/hold.py` `decide` that T4H uses), and writes one outcome to `handled_comments`:

| Outcome | When |
| --- | --- |
| `rejected-hold-ok-author` | The author is not `hold.approver`, or the approver may not approve (`trusted_logins`, or `approver_logins` with `github_app.enabled`). Unchanged, and read before any hold input. |
| `hold-ok` | The PR is held (label, label removed by someone else, or a `hold.paths` file) and the ok is the approver's. A covering ok clears the hold as T4H says. |
| `ok-no-hold` | The repo's hold applies, but this PR is not held at its current head. The ok clears nothing and ships nothing. |
| (unhandled) | A hold input (labels, changed files, label events, the `factory.md` trust comparison, the current head, or the rule engine's moved-head read) is unread. The ok stays unhandled and is read again next poll, so it fails closed. |

A `ship: after:<approver>` repo is outside the hold: its `kind=ok` and `decline` still go to the T8 hook, unchanged. `/ok <sha>` is still read only by T4H's `decide` and gets no outcome of its own.

The ticket page (`ticket.html#t=<repo>#<ticket>`) shows each of the ticket's ok outcomes as an `ok: <outcome>` chip in its hero, newest first.

## Not changed

- `lib/hold.py`: label and path rules, ok coverage, `ok_carries`.
- T4H's `awaiting-ok` flow: an APPROVE on a held PR still waits there, and the covering ok still ships it.
- No backfill: older `hold-ok` rows stay as they were recorded.
- Later parts of #1413 add `hold-ok-other-head` and a note on an `ok-no-hold` that cannot move the ticket.
