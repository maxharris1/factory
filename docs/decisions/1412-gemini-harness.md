# Gemini harness: subscription, CLI and refusal (#1412, superseded by #1639)

## Superseded on 2026-11-25: plan A, `agy` on Google AI Ultra

- **Decision:** Max replaced the Code Assist and upstream `gemini` CLI decision below with plan A. The factory's Gemini harness runs Google's Antigravity CLI `agy` on his Google AI Ultra plan. A Gemini account is each device's `gemini-bot` login, signed in with interactive `agy`. It has no account folder. `gemini` still starts in `refused_harnesses` and stays there until the doctor passes on each device.
- **Max's words:** "Gemini plan A": "the factory's Gemini harness should run Google's Antigravity CLI `agy` on his Google AI Ultra plan, not the upstream `gemini` CLI with Code Assist".
- **When:** 2026-11-25, by Max.
- **Source:** https://github.com/maxharris1/factory/issues/1639

## What follows

- A job runs `agy -p "…" --dangerously-skip-permissions` (`config/waiter-gemini-bot.example.yml`). `orch job run` adds `--output-format stream-json` when no format is set, and the role router adds `--model` and `--effort` (#1642).
- `orch accounts doctor` probes the device's `gemini-bot` login with `agy models`, with every API key and token override unset. Exit 0 with a model list on stdout reads `ok`. Exit 1 with `Please sign in to view available models` reads `signed-out`. No prompt reaches a model. `tests/fixtures/gemini_status` has each reply.
- The sign-in is interactive `agy`, run as `gemini-bot` on each device with the overrides unset. Sign in with the Google AI Ultra account, then quit `agy`. `docs/ACCOUNTS.md` gives the same command.

  ```sh
  env -u GEMINI_API_KEY -u GOOGLE_API_KEY -u GOOGLE_CLOUD_ACCESS_TOKEN -u GOOGLE_APPLICATION_CREDENTIALS -u GOOGLE_GENAI_USE_GCA -u GOOGLE_GENAI_USE_VERTEXAI -u GEMINI_CLI_USE_COMPUTE_ADC -u CLOUD_SHELL -u GOOGLE_GEMINI_BASE_URL -u GEMINI_CLI_CUSTOM_HEADERS -u GEMINI_CLI_HOME agy
  ```

- There is no account folder (#1641). Only a different `HOME` moves the `agy` login, so `machines.yml` names the account inline on each device and refuses `signed_in_on` for a gemini account.
- Gemini quota is tracked. #1639 allowed quota to stay `not tracked` "unless a non-interactive, non-secret quota command is found". #1697 found one: `agy -p /usage --output-format json` answers with `num_turns: 0`, no model turn. `orch job run` reads it at each gemini job's start (`lib/usage_probe.py`, the README's Dashboard section).

## Live config, not applied

`orch.yml` already lists `gemini` in `refused_harnesses`. The live `machines.yml` is unchanged. The proposed account edit below waits for Max's own words.

```yaml
# orch.yml
refused_harnesses:
  - cursor
  - gemini
```

```yaml
# machines.yml
accounts:
  - {id: gemini-ultra, harness: gemini, plan: ultra, label: "Google AI Ultra"}
# under each device's harnesses:
      gemini: {bot: gemini-bot, account: gemini-ultra}
```

## History: the original decision (#1412)

- **Decision:** The factory signs Gemini accounts in with Google sign-in for Gemini Code Assist. The CLI that follows is the upstream `gemini` CLI with its own Google OAuth. Each account's login lives in its folder through `GEMINI_CLI_HOME`, and every API-key or token override the CLI reads is unset. `gemini` starts in `refused_harnesses` and stays there until its accounts are signed in on the devices.
- **Max's words:** "Google sign-in with Gemini Code Assist (gemini CLI OAuth)" and "Yes — start with gemini in refused_harnesses until authenticated on devices".
- **When:** 2026-11-25 15:30 UTC, by Max.
- **Source:** https://github.com/maxharris1/factory/pull/1563#issuecomment-7166953158

Under that decision the doctor ran the upstream CLI with empty stdin as the account's folder and read exit 42 as signed in and exit 41 as signed out. Quota stayed `not tracked`, because that CLI had no non-interactive quota command. #1641, #1642 and #1697 replaced all of it.
