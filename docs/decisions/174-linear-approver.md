# #174: the Linear identity that counts as Max for a capped `go`

- **Decision:** On a Linear ticket, a capped `go` lifts the cap only when its author's stable Linear user id is in `approver_linear_users`, in both GitHub App switch states.
- **When:** 2026-11-15 ET, by Factory decided (technical), from the plan on PR #338
- **Source:** https://github.com/maxharris1/factory/pull/338#issuecomment-6891022867

This refines the Linear line of [122-approver-credential.md](122-approver-credential.md), "What this change cannot check". That file is left as it is.

## Identity

Max is the Linear user he signs in to Linear as. The factory knows that user by its stable Linear user id, never by its display name, which anyone can rename. The id is host-local: it goes in the box's gitignored `orch.local.yml` under `approver_linear_users`, beside `trusted_linear_users`, and is committed nowhere. Max confirms the id, as he did for `trusted_linear_users`.

## Rule

A Linear comment's author is `linear:<user id>`. With `approver_linear_users` set, a `go` on a capped Linear ticket raises `cap_extra` only when both hold:

1. It passes today's trust: header `from=grok-bot role=orchestrator`, author trusted for `grok-bot` in `trusted_linear_users`.
2. Its author's id is in `approver_linear_users`.

Any other `go` on a capped Linear ticket changes nothing and wakes Grok once with `go-not-from-approver`, the same reason and wake a GitHub capped `go` uses.

| Check | Before #174 | After #174, key set | After #174, key unset |
| --- | --- | --- | --- |
| Capped Linear `go`, switch off or on | Author trusted for `grok-bot` | Author trusted for `grok-bot` and in `approver_linear_users` | As before |
| Uncapped Linear `go` | Author trusted for `grok-bot` | Unchanged | Unchanged |
| Linear `retry`, Linear `/go` | Today's rule | Unchanged | Unchanged |
| Every GitHub path (capped `go`, hold `ok`, `/go`, retry, harness env) | #122 rules | Unchanged | Unchanged |

## Config

| Field | Meaning |
| --- | --- |
| `approver_linear_users` | A non-empty list of stable Linear user ids. Each must be a `trusted_linear_users` key, or `check-config` reports an error: that user's `go` would fail today's trust and could never lift a cap. |

## Unset default

With the key unset, a capped Linear `go` keeps today's rule, so a deploy does not freeze every capped Aut0 ticket before Max confirms his id. While any project has a `linear_project_id` and the key is unset, `orch check-config` prints `approver_linear_users is unset: any trusted Linear go lifts a cap`.

## What this change cannot check

- The bots post to Linear through `LINEAR_API_KEY`. If that key belongs to Max's own Linear user, a bot's comment and Max's comment have the same author id, and this check cannot tell them apart. That half of the gap stays open until the bots post to Linear under their own identity, such as a Linear OAuth app actor.
- A Linear `retry` still uses today's rule. A GitHub retry goes through the approver check (#216), so gating a Linear retry the same way is a follow-up.
