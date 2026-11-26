# Gemini harness: subscription, CLI and refusal (#1412)

- **Decision:** The factory signs Gemini accounts in with Google sign-in for Gemini Code Assist. The CLI that follows is the upstream `gemini` CLI with its own Google OAuth. Each account's login lives in its folder through `GEMINI_CLI_HOME`, and every API-key or token override the CLI reads is unset. `gemini` starts in `refused_harnesses` and stays there until its accounts are signed in on the devices.
- **Max's words:** "Google sign-in with Gemini Code Assist (gemini CLI OAuth)" and "Yes — start with gemini in refused_harnesses until authenticated on devices".
- **When:** 2026-11-25 15:30 UTC, by Max.
- **Source:** https://github.com/maxharris1/factory/pull/1563#issuecomment-7166953158

## What follows

- `orch accounts doctor` probes a Gemini account with `env NO_BROWSER=true NO_COLOR=1 gemini </dev/null`, run as the account's folder. With a cached Google sign-in the CLI loads it and then exits 42 because stdin is empty, so no prompt reaches a model. With no sign-in it exits 41 before any input is read. `tests/fixtures/gemini_status` has each reply.
- The sign-in command is `NO_BROWSER=true gemini` with the folder set and the overrides unset. It is interactive: choose Sign in with Google, open the link, paste the code back, then `/quit`. `docs/ACCOUNTS.md` gives the full command.
- Quota stayed `not tracked`: the upstream `gemini` CLI had no non-interactive quota command.
  Superseded on 2026-11-26: the Antigravity `agy` CLI (1.5.18) answers `agy -p /usage --output-format json`
  with `num_turns: 0`, no model turn, so `orch job run` reads Gemini quota at each gemini job's start
  (`lib/usage_probe.py`, the README's Dashboard section).

## Live config, not applied

The live `orch.yml` and `machines.yml` are unchanged. The proposed edits are below. They wait for Max's own words.

```yaml
# orch.yml
refused_harnesses:
  - cursor
  - gemini
```

```yaml
# machines.yml
accounts:
  - {id: gemini-code-assist-1, harness: gemini, plan: code-assist, label: "Gemini Code Assist", signed_in_on: [pc, box, mac]}
# under each of those devices' harnesses:
      gemini: gemini-bot
```
