# Decisions

New decisions go here, one file per decision. The decision log in [docs/SPEC.md](../SPEC.md#decisions-on-2026-11-10-et) is frozen: its entries stay where they are, and nothing new is added to it. One file per decision means two PRs never edit the same lines.

## File name

`docs/decisions/<issue>-<slug>.md`

- `<issue>` is the number of the GitHub issue the decision belongs to, without the `#`.
- `<slug>` is a few lowercase words joined by hyphens.
- Example: `docs/decisions/120-freeze-spec-decision-log.md`.

A later decision that changes an earlier one gets its own file and names the file it replaces. Don't edit another ticket's file.

## Template

```markdown
- **Decision:** <the decision, in one sentence>
- **When:** <YYYY-MM-DD h:mm AM/PM> ET, by <Max | Factory decided (technical)>
- **Source:** <link to the comment, PR or chat where it was made>
```
