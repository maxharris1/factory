# Cursor Bot

## orch jobs

Cursor Bot runs `orch wait cursor-bot` as a blocking shell call and spends no model tokens until that shell exits. The shell prints one job and its `orch:v1` handoff. Cursor Bot is the process that then starts the `cursor-agent` harness for that job. `orch` does not start it. When the job is finished, Cursor Bot runs `orch job done` with the printed job id.
