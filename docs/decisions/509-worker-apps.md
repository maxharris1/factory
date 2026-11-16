- **Decision:** One GitHub App each for the Claude and Codex bots. Grok stays on Max's login and acts for him. `check-config` no longer requires `github_app.apps.grok-bot` when `github_app.enabled` is true. This changes that one line of [170-four-github-apps.md](170-four-github-apps.md).
- **When:** 2026-11-16, by Max ("Split off the workers" for approval, "One App per bot" for the worker Apps)
- **Source:** https://github.com/maxharris1/factory/issues/509

A remote run also unsets `GH_TOKEN`, `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN` and `GITHUB_ENTERPRISE_TOKEN` after the bot's login profile loads and before the harness starts. After that, only a token the job carries is set. Factory decided this (technical), as part of the same ticket.
