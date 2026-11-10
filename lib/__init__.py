"""Factory contracts. Later Phase 1 items import these modules.

- lib.factory_md: factory.md schema and orch check-config
- lib.orch_yml: orch.yml schema and job timeout
- lib.handoff: orch:v1 comments, labels, and the Aut0 no-label path
- lib.queue: durable jobs shared with owning bots
- lib.dispatcher: poll loop and transitions T1, T2 and T6
- lib.pstack_pin: pinned pstack, install-skills, and the doctor canary
- lib.usage: best-fit routing and the usage reserve. F1-07 calls route_job
- lib.eval_report: weekly eval report from a local log directory
"""
