# Dashboard leaf panel goldens

Server-rendered HTML of every leaf panel, one block per fixture variant. The 35 unsuffixed goldens were rendered
from main a2bb7247d6e85731543c23ceabbd0f72ee98e0fa; the `.<kind>` and `.visual-qa` goldens from main e0183d64d82d37d7a87ac7b1b6754224f8d19db0, whose dashboard code is the same.

Each ticket page section is pinned for every ticket kind whose page shows it: running `maxharris1/cleffy#13`, shipped
`maxharris1/cleffy#88` (added by `_with_shipped`), stopped `aut0-code/aut0#AUT-7`, queued `maxharris1/cleffy#12` and idle `maxharris1/tracy-harris-coaching#5`.
`<section>.html` holds the first of those kinds that shows the section and `<section>.<kind>.html` each other one.
`ticket_cockpit_html` renders waiting-max `maxharris1/tracy-harris-coaching#5` with one open question added, and
`ticket_cockpit_html.visual-qa` the same ticket waiting on a visual QA review.

Regenerate:

    GOLDEN_UPDATE=1 python3 -m pytest -q -p no:cacheprovider tests/test_dashboard_golden.py
