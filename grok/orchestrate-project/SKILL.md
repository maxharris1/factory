---
name: orchestrate-project
description: Intake, questions, routing, and stop handling for one factory ticket. Use when Max hands Grok Bot a ticket, or when a ticket wakes Grok Bot for a question or a stop.
---

# orchestrate-project

Grok Bot plans with Max, asks him product questions, decides technical ones, picks the builder and a cross-family reviewer, and reports stops. Post every handoff with `lib.orchestrate`. Do not hand-write an `orch:v1` header.

The dispatcher queues work. This skill does not start a harness.

## Do not

- Do not start `claude`, `codex`, or `cursor-agent`. The owning bot starts its own harness after `orch:go`.
- Do not create logins or credentials. Do not read credentials, runner tokens, or `FACTORY_WEBHOOK_*` values. Do not read `.env`, `auth.json`, `.runner`, or `.credentials*`.
- For `maxharris1/*` and `aut0-code/aut0`, run GitHub as `env -u GH_TOKEN gh`.
- Do not treat another agent's approval as Max's. An extra review round needs Max's own words.
- Do not create a GitHub issue or a GitHub label when `tickets` is `linear`, including on `aut0-code/aut0`. State stays in the `orch:v1` header and on the Linear ticket.
- Do not add `orch:v1` keys. The keys already on main are the only keys.
- Do not invent a third risk class. The classes are `low` and `high`.
- Do not post `orch:go` twice for one intake, and do not post it again when a mid-work answer resumes the same step.
- Read `factory.md` from the project's base branch. Do not read it from a pull request head.

## Interface

`lib.orchestrate` is the stable interface. F1-08 imports these names and does not rename them. Do not add a required field to `RouteRequest`.

RouteRequest fields: `config`, `risk`, `urgent`, `summary`, `likely_files`, `scope_changed`

FitChoice fields: `builder_harness`, `builder_model`, `reviewer_harness`, `reviewer_model`

RouteDecision fields: `builder_harness`, `builder_model`, `reviewer_harness`, `reviewer_model`, `builder_family`, `reviewer_family`, `job`, `urgent`, `risk`, `file_count`, `scope_changed`

F1-08 entry point: `lib.usage.route_job(config, *, usage=None, resumed_paused_step=False, past_ship=False, marked_urgent=False) -> JobRoute`

`JobRoute` has `builder`, `reviewer`, `reason`, and `ok`. `ok` is true only when both sides are set and `reason` is none. Each side has `harness` and `model`.

F1-07 entry points: `choose_route(request, usage=None) -> RouteDecision`, `route_from_fit(request, fit) -> RouteDecision`, `assert_cross_family(builder_harness, builder_model, reviewer_harness, reviewer_model)`

`choose_route(request)` calls `route_job(config, marked_urgent=urgent)` and builds a `FitChoice` from the route's builder and reviewer. A usage source passed as `usage` is forwarded and is the only extra argument. This skill does not pass one. A route that is not ok raises `RouteError`. `lib.usage` must not import `FitChoice`. This skill does not rank harnesses and does not read a usage source.

Question classes: `observable`, `default-able`, `product`

`job` is `plan` when `likely_files` has more than 3 paths, or when `scope_changed` is true. Otherwise `job` is `build`. Three files are a build. `route_job` does not set `job`.

## Intake

1. Take the ticket Max handed over (a GitHub issue, a Linear issue, or a description that will become one on that project's tracker).
2. Resolve `factory.md` from the project's `base_branch`, not from a pull request head. In a checkout of that branch, the path is the repo-root `factory.md`. Otherwise fetch that file with `env -u GH_TOKEN gh` from `base_branch`.
3. Call `read_factory(path)`. The path's filename must be `factory.md`.
4. If the result has a stop, the config is refused. Report that stop once (below) and stop. Do not post a brief, questions, or `orch:go`.
5. If the result has warnings, keep them on the ticket notes. An unknown model name does not refuse the project.
6. Use `project.review`, `project.ship_notes`, `project.context`, and `project.config` for the rest of the ticket. `project.config` is the `factory.md` schema from `orch check-config`.

## Brief

Assign the risk class before posting the brief.

- `low` — docs, cleanup, monitoring. The round cap is `review.rounds.low`.
- `high` — ship, merge, sign-off, credential, or isolation code. The round cap is `review.rounds.high`.
- If the class is unclear, that is the first product question. Ask Max that one question and wait. Do not post the brief until the answer is `low` or `high`.

Post `brief_comment` with these sections, in this order: **Goal**, **Acceptance criteria**, **Likely files**, **Risk**, **Done-evidence**. Done-evidence is the observation that will show the acceptance criteria are met. Likely files may be `None.` when the estimate is empty.

Header shape:

```
<!-- orch:v1 kind=brief from=grok-bot role=orchestrator risk=low -->
```

Where to post: on the GitHub issue when `tickets` is `github`. On the Linear ticket when `tickets` is `linear`. Never on a new GitHub issue.

## Questions

Always run this step, including when no question is open. Call `TicketFlow.post_questions` with a restatement in your own words and the classified questions. An empty list still posts the comment.

Classify every question as one of:

- `observable` — settled with a prototype during the build or plan. Do not ask Max. It does not block `orch:go`.
- `default-able` — decide it and record the decision as `default` on the question. Do not ask Max. A missing default cannot be posted.
- `product` — Max decides. Ask him in chat, one question at a time.

`post_questions` lists every classified question on the ticket, including every product question, and records each technical default. That list is the record, not the ask. In chat, ask only `next_product_question`. Do not put the other product questions in that chat message. Wait for Max's answer. Then `post_answer` for that question only. Repeat until `next_product_question` is `None`.

A product question sets `state=waiting-max` on the questions comment. When every product question is already absent, the questions comment has no `waiting-max` state.

```
<!-- orch:v1 kind=questions from=grok-bot role=orchestrator state=waiting-max -->
```

```
<!-- orch:v1 kind=questions from=grok-bot role=orchestrator -->
```

An answer comment is Max's words for one product question:

```
<!-- orch:v1 kind=answers from=grok-bot role=orchestrator -->
```

## Routing and go

Build a `RouteRequest` from `project.config`, the brief's risk, the likely files, a one-line summary, `urgent`, and `scope_changed`.

`urgent` is true only for a resumed paused step, past-ship work, or a ticket Max has marked urgent. Otherwise it is false. `choose_route` passes that flag as `marked_urgent` and does not pass `usage`. The owning bot applies its own reserve check when it has one.

Call `choose_route(request)`. It calls `route_job(config, marked_urgent=urgent)`. If the route is not ok, or the reviewer family matches the builder that will actually do the work, `choose_route` raises `RouteError`. Do not post `orch:go`. Report stop `blocked` once, with the error as the evidence. `assert_cross_family` uses the same rule as `orch check-config`.

Call `TicketFlow.post_go(decision, project.config.verify)` only after the questions step has run and every product question has an answer. `post_go` raises `GoBlocked` otherwise. The verify value is the skill name from `factory.md`, or `none`.

`post_go` writes `builder`, `risk`, and `state` (`planning` or `building`). The reviewer, models, verify skill, job, and file count are in the body. Do not add a header key for them.

```
<!-- orch:v1 kind=go from=grok-bot role=orchestrator risk=low state=building builder=claude -->
```

Posting `orch:go` is the handoff. Stop. The builder's owning bot starts the harness and runs as poteto-mode. The reviewer is the other family and runs `review.verify` when it is not `none`.

The dispatcher enforces the round cap from `factory.md`. This skill does not.

## Mid-work questions

A `kind=questions` comment from a builder or reviewer is a T5 wake. Classify those questions the same way. Decide each `default-able` question and post `answers_comment` with the decision. Ask each product question one at a time, then post `answers_comment` with Max's words. Do not post another `orch:go`. The dispatcher resumes the same step. That resume is not a review round.

If an answer changes the scope of the ticket, set `scope_changed` on any later `RouteRequest`. That is this skill's judgment. The job becomes `plan`.

## Stops

Refused, stuck, blocked, capped, and abandoned each wake this skill once. Tell Max once. Do not ask him about the same stop again.

Call `StopLedger.report` for that ticket and stop. A second report of the same stop raises. The chat message is `format_stop_report`: the stop, the evidence, and a recommended next step. Use `RECOMMENDED_NEXT` for the next step.

- `refused` — `read_factory` returned a stop. Evidence is the `orch check-config` errors.
- `stuck` — also post `stuck_comment` when this ticket has no `kind=stuck` comment yet.
- `blocked` — a `verdict=BLOCKED`, a `RouteError`, or anything unparsed. Evidence is the verdict or the text that did not parse.
- `capped` — revise at the cap, or a second acceptance-criterion-unmet verdict.
- `abandoned` — Max dropped the ticket.

```
<!-- orch:v1 kind=stuck from=grok-bot role=orchestrator state=stuck -->
```

Do not invent a comment kind for refused, blocked, capped, or abandoned. Those four are chat reports. This skill does not apply GitHub labels. On a GitHub-issue repo the dispatcher applies `orch:state`, `orch:round`, `orch:risk`, and `orch:builder` from the header. On Linear and on Aut0 the header carries those facts and no label is written.

Recommended next steps:

- refused: Fix factory.md so orch check-config accepts it, then hand the ticket back.
- stuck: Read the evidence. Max decides whether to retry or abandon.
- blocked: Read the evidence. Max decides the unblock.
- capped: An extra round needs Max's own words.
- abandoned: No further work unless Max reopens it.
