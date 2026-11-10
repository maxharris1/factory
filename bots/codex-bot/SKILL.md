# Codex Bot

## orch jobs

Codex Bot runs `orch wait codex-bot` as a blocking shell call and spends no model tokens until that shell exits. The shell prints one job and its `orch:v1` handoff. Codex Bot is the process that then starts the `codex` harness for that job. `orch` does not start it. When the job is finished, Codex Bot runs `orch job done` with the printed job id.
