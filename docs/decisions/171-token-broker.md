- **Decision:** a box-user timer mints each bot's App installation token per installation, 15 minutes before expiry at the latest, into a file only that bot's harness user can read, and sends each new token to the bot's remote devices over its own SSH alias on stdin. A `gh` wrapper and a git credential helper read the file; no token travels by environment variable into a job.
- **When:** 2026-11-14, ticket by Max (MF-4, N-04, V4-SF-2); the file layout, margin and helpers were Factory decided (technical) on PR #284.
- **Source:** https://github.com/maxharris1/factory/issues/171, PR https://github.com/maxharris1/factory/pull/284

## How it works

| Piece | What it does |
| --- | --- |
| `lib/github_app_token.py refresh` | Run as the box user, which holds the keys. For each `github_app.apps.<bot>` and each of its `installations`, reuses `<root>/<bot>/<owner>.json` while it has more than 15 minutes left, else signs an RS256 App JWT with `openssl` and POSTs `/app/installations/<id>/access_tokens`. Mints whether or not `github_app.enabled` is set: rollout step 5 of [170](170-four-github-apps.md) moves `gh` before step 6 turns trust on. |
| `deploy/factory-app-token.timer` | Every 5 minutes, so a token always has at least 10 minutes left. Installed by `deploy/app-token-service.sh install-autostart`. Where `systemctl --user` does not work (the box today), it writes `~/.config/factory/app-token-autostart` and each 2-minute self-heal pass runs the broker first. |
| Box files | Root `~/.local/state/factory/app-tokens` is 0711. `<bot>/` is 0750 and each token file 0640, group of the bot's harness user (`machines.yml`: claude-bot, codex-bot, cursor-bot). When that user does not exist or is the box user, both are 0700/0600. `grok-bot` tokens stay box-user only. |
| Remote devices | Every enabled, non-placeholder remote device whose `harnesses` lists the bot's harness gets each new token by `ssh -l <bot> -- <alias> <fixed script>`, token on stdin, into `~/.config/factory/app-tokens/<owner>.json` (0600). A device that was unreachable gets it on the next pass, without a new mint. |
| `deploy/app-token-service.sh install-harness DIR` | Run as a harness user. Writes `~/.local/bin/gh`, which runs the real `gh` with `GH_TOKEN` set for that one child from the repo owner's token (`-R`, `--repo`, `repos/<owner>/...`, else the checkout's `origin`, else the only token). Sets git's helper list for `https://github.com` to one helper reading DIR (with `useHttpPath`, so the owner comes from the path). |

Tokens are never printed, logged, put on a command line, or written to job details or telemetry. Log lines name the bot, the account and the expiry. A refused mint reports the HTTP status only.

## Left to Max

- Make the token files reachable on the box: each harness user needs search on the path down to the token root (today `/home/box` is 0710, group `box`), and the box user must be in each harness user's group to set the files' group. `refresh` warns, without failing, when a harness user cannot read its token.
- Run `install-autostart` as the box user and `install-harness` as each harness user, on the box and on each device.
- Operator step O9: a job that runs longer than one hour keeps working once the Apps exist.
