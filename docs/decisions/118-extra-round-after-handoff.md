- **Decision:** An approver's go with `state=planning` or `state=building` on a `handed-off` ticket whose next round would pass the effective cap is Max's extra round: it raises `cap_extra` by 1, counts the shipped APPROVE's round as completed, clears the recorded approval, sets `fixing` and queues one fix keyed on the go, and the fix's ready re-enters review at the next round; a go inside the cap still changes nothing.
- **When:** 2026-11-15 5:38 PM ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/issues/118

This replaces the "Known limits" sentence of the frozen #108 row (Nov 13) in the [docs/SPEC.md decision log](../SPEC.md#decisions-on-2026-11-10-et): Max can now raise the cap of a handed-off ticket through the factory, and a go after handoff at the cap queues a fix. A second go needs a second handoff at the raised cap; a go while the ticket is `fixing` keeps the capped ticket's behavior. A `merge-queue` ticket keeps #130's normal go path.
