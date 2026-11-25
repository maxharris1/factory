# #1067: sign in from the Settings page

Decisions from issue #1067, built in #1383, #1384 and #1398 and documented in #1399. Sign in on the Accounts panel starts the harness's own login on the device through the sign-in broker (`lib/signin_broker.py`, `lib/signin_attempt.py`) and the relay directory (`lib/signin_relay.py`). docs/ACCOUNTS.md ("Sign in from Settings") describes the flow, the setup and every refusal. `tests/test_accounts_doc.py` fails when a value below and the code differ.

- **Decision:** An attempt ends `expired` 15 minutes after the CLI starts (`signin_attempt.EXPIRY_SECONDS`). The broker then kills the CLI's process group on the device and runs the doctor. The window covers opening the link, signing in and pasting the code back, and bounds how long a pair is held off placement.
- **When:** 2026-11-19 1:30 PM ET, by Max
- **Source:** https://github.com/maxharris1/factory/issues/1067

- **Decision:** The broker runs one attempt per pair and at most 2 at once (`signin_broker.MAX_CONCURRENT`), and refuses a third with `too-many`. Each attempt holds a live `ssh -tt` login session to a device, so the cap bounds how many the broker holds.
- **When:** 2026-11-19 1:30 PM ET, by Max
- **Source:** https://github.com/maxharris1/factory/issues/1067

- **Decision:** The dashboard server removes a Claude paste code the broker has not taken after 120 seconds (`CODE_SECONDS` in `deploy/factory-dashboard-http.py`). The broker normally takes the code within seconds, so the expiry only keeps a code the broker never took from waiting on disk.
- **When:** 2026-11-19 1:30 PM ET, by Max
- **Source:** https://github.com/maxharris1/factory/issues/1067

- **Decision:** Only an allowlisted link and, for Codex, a code of the allowed shape leave the CLI's output (`account_signin._ALLOW`). Every other byte stays in the broker's memory, because the CLI's output can name the email or carry a token. The dashboard server checks the view against the same patterns (`SIGNIN_URLS` and `SIGNIN_CODE`) before the page sees it. The block below lists the Claude link, the Codex link and the Codex code patterns.
- **When:** 2026-11-19 1:30 PM ET, by Max
- **Source:** https://github.com/maxharris1/factory/issues/1067

```text
https://claude\.com/cai/oauth/authorize(?:\?[A-Za-z0-9._~%&=+-]*)?
https://auth\.openai\.com/codex/device
[A-Z0-9]{4}-[A-Z0-9]{4,5}
```
