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
| `hold-ok` | The PR is held (label, label removed by someone else, or a `hold.paths` file), the ok is the approver's, and the ok's own head covers the current head by T4H's rule (same commit, patch-id or a carried base merge; with `rule_engine: on`, same commit or the moved-head rule). It clears the hold and is the merge OK for that head, so an APPROVE after it ships with no second ok. |
| `hold-ok-other-head` | The PR is held and the ok is the approver's, but the ok's head (missing, not a 40-char sha, or another head) does not cover the current head. The hold stays. The poller posts one note naming both heads (#1419). |
| `ok-no-hold` | The repo's hold applies, but this PR is not held at its current head. The ok clears nothing and ships nothing. |
| (unhandled) | A hold input (labels, changed files, label events, the `factory.md` trust comparison, the current head, the rule engine's moved-head probe, or, with the engine off, whether an ok at an earlier head carries to the current one) is unread, or reading this ok's own coverage leaves anything unread or waiting. The ok stays unhandled and is read again next poll, so it fails closed. |

A `ship: after:<approver>` repo is outside the hold: its `kind=ok` and `decline` still go to the T8 hook, unchanged. `/ok <sha>` is still read only by T4H's `decide` and gets no outcome of its own.

The ticket page (`ticket.html#t=<repo>#<ticket>`) shows each of the ticket's ok outcomes as an `ok: <outcome>` chip in its hero, newest first.

## The note on an ok that moves nothing (#1419)

An `ok-no-hold` on a ticket outside `awaiting-ok`, `merge-queue`, `updating`, `handed-off` and the terminal states moves nothing: no hold applies and no APPROVE covers the head. Before the ok is marked handled, the poller posts one PR comment, deduped on the ok's id by an `orch:ok-note <id>` marker, so a crash replays into the dedupe. It names:

- the latest counted verdict, its verdict and its round against the effective cap, or "none yet";
- the held repeat, cap, stall or unsat stop and its finding ids, or none;
- what moves the ticket forward: at a stop, an approver's `/go` naming one of the stop's options; otherwise the next step for the workflow state (the reviewer's APPROVE in `review`, the builder's ready in `fixing` or `building`, an answer in `waiting-max`, an approver's `/go` in `blocked`).

It also raises `ok-without-approve` needs-attention. `write_workflow_state` deletes that row when the ticket enters one of the states above or a terminal state. An ok in those states posts nothing. The note never carries `orch:v1`, so the poller never reads it as a handoff.

## Not changed

- `lib/hold.py`: label and path rules, ok coverage, `ok_carries`.
- T4H's `awaiting-ok` flow: an APPROVE on a held PR still waits there, and the covering ok still ships it.
- No backfill: older `hold-ok` rows stay as they were recorded.
