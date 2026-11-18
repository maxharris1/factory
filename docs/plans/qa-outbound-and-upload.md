# QA: Linear uploads and aut0's outside calls (#783)

Operator task #783, done by Grok Bot (Factory) on 2026-11-18, 11:39 AM to 11:55 AM ET. It records two facts the QA tickets build to: how big a file the factory can put on a Linear ticket (#792), and where aut0's form automation, outbound email and payments go when aut0 runs in development (the QA lane allowlists, #797 and #798). No secret value was read or recorded; only variable names, hosts and code defaults.

## Linear uploads

**Measured** against a scratch issue in aut0's Linear team, [AUT-2490](https://linear.app/aut0/issue/AUT-2490) ("[scratch] factory QA upload test (factory #783), will be canceled"), created at 11:39 AM ET in Shaping (no cycle, no project, no labels) and canceled at 11:41 AM ET. All files were generated locally with ffmpeg (a test pattern and random noise); nothing sensitive was uploaded.

| File | Type | Size | Result |
|---|---|---|---|
| `qa-upload-test.png` | `image/png` | 2,687 bytes | Uploaded and attached |
| `qa-upload-test.mp4` (3 s, H.264) | `video/mp4` | 14,298 bytes | Uploaded and attached |
| `qa-size-test-12mib.mp4` (1 s noise) | `video/mp4` | 13,265,650 bytes (12.7 MiB) | Uploaded and attached; downloaded back byte-identical (same SHA-256) |
| size probe, no bytes sent | `video/mp4` | 11,534,336 bytes (11 MiB) | Upload URL granted |
| size probe, no bytes sent | `video/mp4` | 1,992,294,400 bytes (1.9 GiB) | Upload URL granted |

**The size limit.** Linear's pricing page lists file uploads as 10 MB per file on the Free plan and unlimited on Basic, Business and Enterprise. The aut0 workspace is on a paid plan: a 12.7 MiB file went through, and the `fileUpload` mutation granted a signed URL for 1.9 GiB. The connector Grok Bot used refuses sizes of 2 GB or more, so 2 GB is the only hard ceiling observed. **Build to:** no Linear-side limit below 2 GB on this workspace. #792 should still cap each video with its own setting (a factory-side choice, not Linear's), and name any file over that cap in the comment, as its scope says. If the workspace ever drops to Free, the limit becomes 10 MB per file.

**How an upload works** (Linear API docs, "How to upload a file to Linear", and what the test showed):

1. GraphQL `fileUpload(contentType, filename, size)` returns `uploadUrl`, `assetUrl` and a list of headers. `size` is the exact byte count.
2. `PUT` the raw bytes to `uploadUrl` (Google Cloud Storage) with `Content-Type`, `Cache-Control: public, max-age=31536000` and every returned header verbatim. The signed headers include `x-goog-content-length-range: N,N`, so the body must be exactly the declared size, and `Content-Disposition`. A changed or missing signed header gets HTTP 403. Upload promptly: the signed URL expired within about 60 seconds in the test.
3. Use `assetUrl` (`https://uploads.linear.app/<org>/<id>/<id>`) in a comment's Markdown, or link it to the issue as an attachment (`attachmentCreate`; the test used this path).
4. Assets are private. Reading one needs a Linear login or a signed read URL, which Linear issues for about 5 minutes. A reviewer opens them in Linear; they are not public links.
5. The `PUT` must run server-side (Linear's CSP blocks browser uploads). That fits #792: the orchestrator uploads with the Linear credential it already holds, and a lane never sees it.

Not tested: how Linear renders an inline `assetUrl` video inside comment Markdown (the test attached files to the issue). #792's build should check that once on a scratch issue.

## Outside calls

Read from `aut0-code/aut0` at `dev`, commit `3cf6bf80` (2026-11-18), read-only. "Hosted dev" is the dev Supabase project `dpiwkypbpeepxookpjoz` and the `*-dev` Cloud Run services; "local" is `supabase start`. The values set on hosted dev are secrets and were not read; where a target depends on one, the variable is named.

### Form automation

- **Service.** `services/form-automation` (Node, Fastify, Playwright). Its mode is `FORM_AUTOMATION_ENV`: `development` (default; also allows localhost CORS and trims uploads to one document), `remote` (local service against hosted dev data) or `production`. `config.js` refuses to start unless `SUPABASE_URL` matches the mode's project: `dpiwkypbpeepxookpjoz` for development and remote, `corhjzoteytfszzcnawx` for production. Hosted dev runs as Cloud Run `form-automation-dev` (us-central1, `cloudbuild-dev.yaml`, `SENTRY_ENVIRONMENT=development`).
- **Callers.** The queue worker (`supabase/functions/queue-worker/config.ts`) posts jobs to `FORM_AUTOMATION_URL` with `FORM_AUTOMATION_INTERNAL_SECRET`; unset, it logs and retries. The LLM orchestrator uses `FORM_AUTOMATION_URL`, default `http://form-automation:3000` (docker compose). The web app calls `VITE_FORM_AUTOMATION_REMOTE_URL` or `VITE_FORM_AUTOMATION_LOCAL_URL`, both in its CSP list.
- **Browser.** Local Playwright Chromium by default. `USE_BROWSERLESS=true` connects to Browserless.io instead (`production-<region>.browserless.io`, `BROWSERLESS_REGION` default `sfo`, token `BROWSERLESS_API_TOKEN`).
- **What it drives.** Each run opens its template's `target_url` from `form_templates`. The seeded templates all point at the live federal IDR portal, `https://nsa-idr.cms.gov/` (`/paymentdisputes/s/`, `/entityapproval/s/`, `/paymentoffer/s/`). There is no sandbox portal and no dev-mode guard on the final "Submit Notice" step; `USE_TEST_VALUES` only swaps in test field values and `USE_LOCAL_AUTOMATION_FIXTURE` only replaces the case data. **For QA:** a lane must never reach `nsa-idr.cms.gov`. Leave it off the allowlist and run form automation only against a local fixture page (the repo has `test-form/` and `supabase/templates/form-automation/test-idr-portal-form-*.json`).

### Outbound email

- **Sender.** Gmail API (`gmail.googleapis.com/gmail/v1/users/<GOOGLE_IMPERSONATED_USER_EMAIL>/messages/send`) through a Google service account (token minted in `_shared/mailboxAuth.ts`), from `CENTRAL_SENDER_EMAIL_ADDRESS` (`CENTRAL_SENDER_DISPLAY_NAME`, `RETURN_PATH_EMAIL`, `EMAIL_PRECEDENCE`, `LIST_UNSUBSCRIBE_URL`). Used by `outbound-email-handler`, `send-notification-email` and `send-one-time-link`. Case mail uses `CASE_EMAIL_DOMAIN`.
- **Development switch.** `_shared/inbucketClient.ts` `shouldUseInbucket()` sends to local Inbucket SMTP (`INBUCKET_SMTP_HOST`, `INBUCKET_SMTP_PORT`) instead of Gmail when `USE_INBUCKET=true` or `SUPABASE_URL` is localhost or 127.0.0.1. Only then does `DEV_EMAIL_RECIPIENT_OVERRIDE` reroute every recipient to one inbox; set anywhere else it is ignored with a warning. So local `supabase start` never sends real mail, and **hosted dev sends real mail through Gmail** unless its `USE_INBUCKET` is `true`.
- **Not used.** No Resend code. `_shared/sendgrid.ts` (SendGrid, `SENDGRID_API_KEY`) exists but nothing imports it.
- **For QA:** run aut0 locally so mail lands in Inbucket, and leave the Gmail API and Google sign-in hosts (`gmail.googleapis.com`, `*.googleapis.com` token endpoints) off the lane allowlist.

### Payments

- **Provider.** Mercury: Accounts Receivable invoicing (`create-mercury-invoice`, `run-monthly-mercury-invoices`, `refresh-`, `reconcile-` and `download-mercury-invoice-pdf`, `mercury-webhook`, `provision-mercury-customer`) and outgoing ACH payments (`execute-ach-payment`, `reconcile-ach-payments`, `provision-mercury-recipient`). There is no Stripe code; "Stripe" appears only in docs and scraped CSS.
- **Host gate.** `_shared/mercury/environment.ts` fails closed: `MERCURY_API_BASE_URL` must be set, and the live host `api.mercury.com` is allowed only in production (`SENTRY_ENVIRONMENT`, `ENVIRONMENT` or `SUPABASE_ENV` is `prod` or `production`, and `MERCURY_LIVE_PROJECT_REF` equals the `SUPABASE_URL` project, approved ref `corhjzoteytfszzcnawx`). Every other deployment, hosted dev included, must use the sandbox, `https://api-sandbox.mercury.com/api/v1`. The pay link opens `https://sandbox.mercury.com/pay/<slug>` there (`VITE_MERCURY_PAY_ORIGIN`, `VITE_MERCURY_API_BASE_URL`).
- **Credentials and switches.** `MERCURY_API_TOKEN` (or `MERCURY_API_KEY`), `MERCURY_ACCOUNT_ID`, `MERCURY_WEBHOOK_SECRET`. `MERCURY_DRY_RUN=true` skips Mercury calls, but only where a dry run is allowed (`execute-ach-payment/payment_outcome.ts` `isMercuryDryRunAllowed`): a local `SUPABASE_URL` (localhost, 127.0.0.1, `kong:8000`, `.local`) or `ACH_ALLOW_PAYMENT_DRY_RUN=true`. Requested anywhere else, the call is refused rather than going live. Sandbox integration tests read `.env.mercury.sandbox` and refuse any non-sandbox host.
- **For QA:** run aut0 locally with `MERCURY_DRY_RUN=true`, or use the sandbox host only (`api-sandbox.mercury.com`, `sandbox.mercury.com`). Never allowlist `api.mercury.com` or `app.mercury.com`.

### Allowlist summary for the aut0 QA lane

| Call | Development target | Lane allowlist |
|---|---|---|
| Form automation | local service; templates point at `nsa-idr.cms.gov` (live, no sandbox) | Local fixture only. Never `nsa-idr.cms.gov`. Browserless only if a lane needs it. |
| Outbound email | Local: Inbucket. Hosted dev: Gmail API (real mail) | Local Inbucket only. No Google mail or OAuth hosts. |
| Payments | Mercury sandbox (enforced outside production) | Local `MERCURY_DRY_RUN=true`, or `api-sandbox.mercury.com` and `sandbox.mercury.com`. Never live Mercury. |
