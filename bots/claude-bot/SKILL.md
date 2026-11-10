# Claude Bot

## orch jobs

Claude Bot runs `orch wait claude-bot` as a blocking shell call and spends no model tokens until that shell exits. The shell prints one job and its `orch:v1` handoff. Claude Bot is the process that then starts the `claude` harness for that job. `orch` does not start it. When the job is finished, Claude Bot runs `orch job done` with the printed job id.
