# Usage reading fixtures (issue #151)

Every number in these files is a TEST VALUE. They follow the real event shapes,
but no value comes from a real account, and they hold no token, auth value or
account id. Thread ids are the test id `00000000-0000-4000-8000-000000000151`.

- `claude-2.1.322-get_usage.jsonl`: the reply to Claude Code 2.1.322's SDK control request
  `get_usage` (`claude -p --input-format stream-json --output-format stream-json --verbose`,
  run as claude-bot on pc, 2026-11-14). The keys and the reply's shape are the CLI's own.
  The values are replaced with test values (`utilization` 12.5 and 34.5), and the per-model,
  billing and promotion keys are trimmed.
- `claude-2.1.322-get_usage-unavailable.jsonl`: the same reply from a login that cannot report
  limits (`rate_limits_available: false`), as a setup-token login answers.
- `codex-exec-json.jsonl`: a `codex exec --json` stream (CLI 0.162.1 event names); only
  `thread.started`'s `thread_id` is used for usage.
- `codex-rollout.jsonl`: a Codex session rollout (`$CODEX_HOME/sessions/.../rollout-*-<thread id>.jsonl`)
  with `token_count` events carrying `rate_limits.primary`/`secondary` (`used_percent`,
  `window_minutes`, and `resets_at` or the older `resets_in_seconds`). The text lines in it
  exist to prove they are never kept.
