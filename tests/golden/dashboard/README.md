# Dashboard leaf panel goldens

Server-rendered HTML of every leaf panel, one block per fixture variant. The 35 unsuffixed goldens were rendered
from main a2bb7247d6e85731543c23ceabbd0f72ee98e0fa; the `.<kind>` and `.visual-qa` goldens from main e0183d64d82d37d7a87ac7b1b6754224f8d19db0. Run with this harness and the
checked-in `config/routing.yml`, both commits render all 52 goldens byte for byte.

Each ticket page section is pinned for every ticket kind whose page shows it: running `maxharris1/cleffy#13`, shipped
`maxharris1/cleffy#88` (added by `_with_shipped`), stopped `aut0-code/aut0#AUT-7`, queued `maxharris1/cleffy#12` and idle `maxharris1/tracy-harris-coaching#5`.
`<section>.html` holds the first of those kinds that shows the section and `<section>.<kind>.html` each other one.
`ticket_cockpit_html` renders waiting-max `maxharris1/tracy-harris-coaching#5` with one open question added, and
`ticket_cockpit_html.visual-qa` the same ticket waiting on a visual QA review.

Regenerate from the checked-out code:

    GOLDEN_UPDATE=1 python3 -m pytest -q -p no:cacheprovider tests/test_dashboard_golden.py

Update mode deletes every `.html` here that the harness does not render. a2bb7247d6e85731543c23ceabbd0f72ee98e0fa has no harness and the one at
e0183d64d82d37d7a87ac7b1b6754224f8d19db0 renders only the 35 unsuffixed goldens, so copy this harness into a checkout of either commit before
regenerating.
Neither commit has the panels added since, so copy their files too: `lib/dashboard_pages/counts.py`, `lib/dashboard_static/css/counts.css`, `lib/dashboard_static/js/counts.js`.
`_routing.html` renders the checked-in `config/routing.yml`, so copy it too, with `lib/role_router.py` to read its `shares` block.
`_needs_max.html` draws the inbox cards added since, so copy `lib/dashboard_pages/cockpit.py` too.
From the repo root, with `sha` set to one of them:

    git worktree add --detach /tmp/golden-$sha $sha
    cp tests/test_dashboard_golden.py /tmp/golden-$sha/tests/
    cp lib/dashboard_pages/counts.py /tmp/golden-$sha/lib/dashboard_pages/counts.py
    cp lib/dashboard_static/css/counts.css /tmp/golden-$sha/lib/dashboard_static/css/counts.css
    cp lib/dashboard_static/js/counts.js /tmp/golden-$sha/lib/dashboard_static/js/counts.js
    cp config/routing.yml /tmp/golden-$sha/config/routing.yml
    cp lib/role_router.py /tmp/golden-$sha/lib/role_router.py
    cp lib/dashboard_pages/cockpit.py /tmp/golden-$sha/lib/dashboard_pages/cockpit.py
    (cd /tmp/golden-$sha && GOLDEN_UPDATE=1 python3 -m pytest -q -p no:cacheprovider tests/test_dashboard_golden.py)
    diff -r tests/golden/dashboard /tmp/golden-$sha/tests/golden/dashboard
