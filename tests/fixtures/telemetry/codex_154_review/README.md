# Codex review #154 token fixture (issue #1015)

Every number in these files is a TEST VALUE. They follow the real event shapes,
but no value comes from a real account. The thread id is the test id
`00000000-0000-4000-8000-000000000154`.

They reproduce the #154 review case. The job's `codex exec --json` stream
(`exec.jsonl`) reports per-turn usage that sums to 2,100 tokens (input 2,000
with cached input included, output 100). The same thread's session rollout
(`sessions/2026/11/14/rollout-*-<thread id>.jsonl`, CLI 0.162.1 event names)
ends with a `token_count` event whose `total_token_usage` is Codex's own
running session total: input 5,200,000 (4,900,000 cached), output 40,000.
The folder is the run's `CODEX_HOME`. A job's stored tokens must equal the
rollout total, not the stream's sum.
