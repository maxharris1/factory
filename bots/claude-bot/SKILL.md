# Claude Bot

## orch jobs

Claude Bot runs `orch wait claude-bot --db "$ORCH_DB"` as a blocking shell call and spends no model tokens until that shell exits; `orch` never starts a harness. The shell prints one JSON job line (`id`, `action`, `repo`, `ticket_id`, `source_comment_id`, `head`, `detail`, `attempt`). Claude Bot is the process that then starts the `claude` harness for that job, and it runs `orch job heartbeat claude-bot <id>` at least every few minutes while the harness runs, so T6 does not time the job out. When the harness has posted its `orch:v1` handoff comment, Claude Bot runs `orch job done claude-bot <id> --handoff <comment-id>`; if the job ends with no handoff, it runs `orch job done claude-bot <id> --failed` so T6 retries it.
