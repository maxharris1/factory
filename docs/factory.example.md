---
base_branch: main
tickets: github
builder:
  harness: claude
reviewer:
  harness: codex
review:
  rounds:
    low: 1
    high: 3
  verify: verify-cleffy
  # followup_from_round: 4  # optional; from this round, variants of an earlier finding become a follow-up ticket
  # auto_extra_rounds: 0  # optional; the poller grants this many extra rounds at the cap itself
  # start_before_ci: true  # optional; review starts when the ready lands, ship still needs green CI
  # pre_ready:  # optional; a check the factory runs before a builder's ready counts
  #   command: pytest -q tests/fast
  #   timeout_seconds: 300
  # block_on: any-failure  # optional; user-impact uses pstack's blocking bar and files follow-ups from round 1
  # stop_on: rounds  # optional; convergence goes on while blocking findings reach a new low, through one flat round
  # verify_unavailable: true  # optional; the verify skill may not be on the base yet, so the reviewer lists it as not run
ship: merge
# finish_line: merge  # optional; omitted, it follows ship
# rule_engine: off  # optional; on lets the rule engine rule at the cap, repeat and moved-head sites
# ci:  # optional; the bases CI runs on. A PR into another base gets no CI wait, and briefs ask for local results
#   branches: [main]
#   local_checks: [pytest]
#   required_checks: [ci-ok]  # optional; when set, only these checks decide PR CI
#   force_rerun_paths: [.github/workflows/**]  # optional; the default. A green head touching these waits for runs started after its commit; [] turns it off
# qa:  # optional; what the QA lane must run. The dispatcher queues qa jobs from it (section 4.2.1)
#   enabled: true
#   gate: report  # report posts the result; block gates the ship
#   runners: [claude-cloud, cursor-cloud]  # one or two; the first is the primary
#   shootout: true  # every run goes to both runners; needs gate: report
#   profile: full-stack  # full-stack | supabase | none
#   lanes: 1  # 1 to 3
#   paths: [src/**]
#   setup: scripts/qa/cloud-setup.sh
#   claude_cloud:
#     environment: qa-example
#   cursor_cloud:
#     environment: qa-example
#   lane:
#     boot: bash scripts/qa/boot-lane.sh
#     doctor: [bash scripts/qa/doctor.sh]
#     base_url: http://127.0.0.1:8080
#   fixtures:
#     load: node scripts/qa/load-fixtures.mjs
#   outside: [EXAMPLE_SERVICE_URL]  # setting names, never values
#   secrets: [EXAMPLE_API_KEY]  # secret names, never values
#   timeout_minutes: 60
skills:
  - verify-cleffy
---

## Review
One reviewer from a different model family than the builder gives the final verdict and runs `verify-cleffy` (UI and database checks). Low-risk changes get one round; leftover minors are batched into a follow-up issue. High-risk changes get up to three rounds; every extra round needs Max's own words.

## Ship
On APPROVE of the covered head, squash-merge with `--match-head-commit`.

## Context
PRs target `main`. Mention any repo-specific gotchas here, not in the YAML.
