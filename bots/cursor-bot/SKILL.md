# Cursor Bot

## orch jobs

Cursor Bot runs `orch wait cursor-bot --db "$ORCH_DB"` as a blocking shell call and spends no model tokens until that shell exits; `orch` never starts a harness. The shell prints one JSON job line (`id`, `action`, `repo`, `ticket_id`, `source_comment_id`, `head`, `detail`, `attempt`, `timeout_seconds`). Cursor Bot is the process that then starts the `cursor-agent` harness for that job, and it runs `orch job heartbeat cursor-bot <id>` while the harness runs at least once every third of the job's `timeout_seconds` (every 20 seconds when `job_timeout` is 1 minute), so T6 does not time the job out. When the harness has posted its `orch:v1` handoff comment, Cursor Bot runs `orch job done cursor-bot <id> --handoff <comment-id>`; if the job ends with no handoff, it runs `orch job done cursor-bot <id> --failed` so T6 retries it. For a Linear-backed ticket, post `ready`, `verdict`, and `stuck` as PR issue
comments. Do not post a review or inline review comment.
