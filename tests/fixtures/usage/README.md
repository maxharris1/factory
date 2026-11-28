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
- `agy-1.5.18-usage.json`: the reply to `agy -p /usage --output-format json` (Antigravity CLI
  1.5.18, run as gemini-bot on pc, 2026-11-26): `status: SUCCESS`, `num_turns: 0`, no model turn.
  The keys and the reply's shape are the CLI's own, including the top-level `conversation_id`,
  `response` and `usage` keys and each bucket's `description`, which the parser ignores; their
  values are sanitized and it holds no account id. A real reply may follow a warning line. Only the
  `Gemini Models` group is read; the `Claude and GPT models` group is the unused third-party
  pool, whose `1970-01-01T00:00:00Z` reset time is out of horizon.
- `codex-0.172.0-app-server-rate-limits.jsonl`: one rate-limit read from `codex app-server`
  (Codex CLI 0.172.0, 2026-11-27): the reply to `initialize`, an `account/rateLimits/updated`
  notification, and the reply to `account/rateLimits/read`, after the handshake `initialize` /
  `initialized` / `account/rateLimits/read` with `excludeResetCreditDetails: true`. The shape
  follows the schema from `codex app-server generate-json-schema` of 0.172.0. No signed-in reply
  was captured, so every value is a test value; the account id is `00000000-0000-4000-8000-000000001873`.
  The notification's numbers differ from the read's to prove only the read's are kept.
- `codex-0.172.0-app-server-signed-out.jsonl`: the real reply of a signed-out `codex app-server`
  (Codex CLI 0.172.0, empty `CODEX_HOME`, run as claude-bot on pc, 2026-11-27) to the same
  handshake: the `initialize` reply, two notifications, and the error `-32600` for the read.
  The home, host name and installation id are replaced with test values.
