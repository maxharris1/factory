# QA onboarding

This guide adds Live App QA to a factory project. It gives copy-paste starters for the three parts that a project needs:

1. The `qa:` stanza in the project's `factory.md`.
2. A verify skill, `verify-<project>/SKILL.md`, that tells an agent how to start, check and drive the app.
3. A feature map, `verify-<project>/features/`, with one `<journey>.md` file for each user journey.

Use the web starters for a project that a user reaches in a browser: Cleffy, Aut0 and Tracy's site. Use the CLI or service starters for a project whose journeys go through a command or an HTTP endpoint, such as coo's API routes and jobs. A project with both kinds of journey uses one verify skill and mixes both journey shapes in one feature map.

`tests/test_qa_onboarding.py` parses every starter in this file. It substitutes each project name, turns the stanza on and requires `check-config`'s strict parse to accept it. If you change a starter, run that test.

## How the parts connect

- `qa.verify` names the verify skill. A QA lane finds the skill with `git ls-files '*/verify-<project>/SKILL.md'`, so the folder can sit anywhere in the repo. Put it in `.claude/skills/verify-<project>/`, where builders also load skills.
- `review.verify` names the same skill. The build brief then names it as the verify skill that the builder uses for each `qa` proof entry.
- The feeder reads the file names in `features/` at the base branch. Each `<journey>.md` whose stem fits `[a-z0-9][a-z0-9-]{0,63}` is a journey id. `README.md` is not a journey.
- A ticket names journeys on its `Features:` line, such as `Features: sign-in, new:invite-member`. A `qa:` tag on an acceptance criterion marks it for QA, and `qa(sign-in):` binds it to one journey. The feeder refuses an id that the map does not have, unless the ticket writes it as `new:<id>`. A `new:` journey's PR adds `features/<id>.md` and its row in `features/README.md`.
- A QA lane gives input only through a browser at `lane.base_url`. Tag `qa` only on a criterion that a page can show. A command or endpoint journey is proof for the builder's `review` entries, which run the journey's commands.

## 1. The factory.md stanza

Paste one stanza into the front matter of `factory.md`, and replace `<project>` with the project's name. Each stanza ships with `enabled: false`, so a pasted stanza changes nothing until you turn it on.

`profile` only labels the result comment's `env=` field. The factory starts no service for any profile. `lane.boot`, `lane.base_url` and `lane.doctor` are placeholders. Set `lane.boot` to a command that starts the app and every service it needs. Set `lane.base_url` to the URL where the app answers. Set `local_docker.environment` to a prepared image (a name with `:` or `/`) if the runner's default image lacks a tool that the app needs. Each `lane.doctor` command runs after boot and must exit 0 before the lane drives anything.

### Web stanza

```yaml
qa:
  enabled: false
  gate: report
  visual_hold: true
  runners: [local-docker]
  local_docker:
    environment: qa-local
  profile: full-stack
  verify: verify-<project>
  lane:
    boot: npm run dev
    base_url: http://127.0.0.1:3000
    doctor:
      - curl -fsS -o /dev/null http://127.0.0.1:3000/
```

### CLI or service stanza

```yaml
qa:
  enabled: false
  gate: report
  visual_hold: true
  runners: [local-docker]
  local_docker:
    environment: qa-local
  profile: supabase
  verify: verify-<project>
  lane:
    boot: npm run dev
    base_url: http://127.0.0.1:3000
    doctor:
      - curl -fsS -o /dev/null http://127.0.0.1:3000/api/health
```

Also set `review.verify: verify-<project>` in the `review:` block.

## 2. The verify skill

Save one template as `.claude/skills/verify-<project>/SKILL.md`. Replace `<project>` and every other `<...>` fill-in with what the repo actually does. An agent reads this file cold in the middle of a task, so give exact commands, ports and selectors, not examples. Run the skill once end to end before you merge it: launch, doctor, drive one journey, collect evidence, clean up, and confirm that the evidence survived the cleanup.

### Web verify skill

````markdown
---
name: verify-<project>
description: Start <project> locally and drive it in a browser like a user does, with evidence. Use to prove a UI change, to run a feature-map journey, or when a QA brief names verify-<project>.
---

# Verify <project>

Drive the real <project> web app in a headless browser and capture proof. The journeys are in `features/`. Read `features/README.md` first.

## Launch

1. Install dependencies once: `npm ci`.
2. Start the services the app needs: `<command that starts the database and other services>`.
3. Load the seed data: `<seed command>`.
4. Start the app: `npm run dev`.
5. The app is ready when `<log line that says the server listens>` appears and the doctor below passes.

Teardown: stop the processes that this run started, by the process IDs you recorded, and run `<command that stops the services>`.

## Doctor

Run this read-only check before the first journey and after any failure:

```bash
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3000/
```

It passes when it prints `200`. Never drive an instance that this run did not start.

## Drive

- Use the repo's Playwright when it has one (`playwright.config.*`, or `@playwright/test` in `package.json`). Otherwise open `http://127.0.0.1:3000` in a headless browser from a script.
- Give input only by clicks, typing and navigation.
- Find elements by ARIA role and accessible name, such as `getByRole('button', { name: '<label>' })`. Do not use CSS position or tab order.
- Sign in as `<test user>` with `<how the test user signs in>`.

## Evidence

- Save artifacts to `artifacts/<journey>/`.
- For each step, keep a screenshot that shows the action and one that shows the resulting state.
- For a change to stored data, also read the stored value back through a second view of the app.
- Record the journey id and the entry point with every artifact.

## Cleanup

- Remove the test records that this run created.
- Stop only the processes that this run started.
- Keep `artifacts/`. Cleanup never deletes evidence.
````

### CLI or service verify skill

````markdown
---
name: verify-<project>
description: Start <project> locally and drive its commands and HTTP endpoints like a client does, with evidence. Use to prove a CLI or service change, to run a feature-map journey, or when a brief names verify-<project>.
---

# Verify <project>

Drive the real <project> commands and endpoints and capture proof. The journeys are in `features/`. Read `features/README.md` first.

## Launch

1. Install dependencies once: `npm ci`.
2. Start the services the app needs: `<command that starts the database and other services>`.
3. Load the seed data: `<seed command>`.
4. Start the service: `npm run dev`.
5. The service is ready when the doctor below passes.

A short-lived command needs no server. Build it once, then run each journey in its own shell with its own scratch directory.

Teardown: stop the processes that this run started, by the process IDs you recorded, and run `<command that stops the services>`.

## Doctor

Run this read-only check before the first journey and after any failure:

```bash
curl -fsS http://127.0.0.1:3000/api/health
```

It passes when it exits 0 and prints `<expected health body>`. Never drive an instance that this run did not start.

## Drive

- Call endpoints with `curl -sS -w '\n%{http_code}\n'` so the status code goes into the transcript.
- Run commands with their full arguments, as a user types them.
- Authenticate as `<test client>` with `<how the test client gets a token>`. Read the token from the environment, never from a file in the repo.

## Evidence

- Save artifacts to `artifacts/<journey>/`.
- For a command, keep the command, stdout, stderr and the exit code.
- For an endpoint, keep the request, the response body and the status code.
- For a change to stored data, also read the stored value back through a second command or endpoint.

## Cleanup

- Remove the test records that this run created.
- Stop only the processes that this run started.
- Keep `artifacts/`. Cleanup never deletes evidence.
````

## 3. The feature map

Save the index as `.claude/skills/verify-<project>/features/README.md`, and each journey as `features/<journey>.md`. Start with the three to five journeys that users use most. Each journey file has one H1, one paragraph that says what the user sees, then exactly four H2 sections in the order the templates show.

If the seed data lacks a precondition that a journey needs, the journey fails, and the reason goes in its `Gotchas`. Do not weaken a journey to make it pass.

### Feature map README

````markdown
# <project> verification map

This folder is the maintained list of user journeys for <project>. Read this index before you drive the app, then use the matching journey file as the recipe.

## Baseline

- Start <project> with the `Launch` steps in `../SKILL.md`, and require the doctor to pass.
- Start each journey from the seed data unless its preconditions say otherwise.
- Never drive an instance that this run did not start.

## Journeys

- [<Journey title>](./<journey>.md) covers <what the user does, and each entry point>.
````

### Web journey

````markdown
# <Journey title>

<One paragraph: what the user does and what they see when it works.>

## Sub-features

- `<journey>-open` <opens the feature from each entry point>.
- `<journey>-save` <saves the result>.

## How to get to it (user POV)

- Choose the `<button or link name>` in the `<page or menu>`.
- Go to `http://127.0.0.1:3000/<route>`.

## Driving it with verify-<project>

Preconditions:

- The doctor in `../SKILL.md` passes.
- <Seed record or signed-in user that this journey needs.>

- **<Step name>.** <User action>. Run `<Playwright call with a role and a name>`. <Observable result>.
- **<Step name>.** <User action>. Run `<Playwright call>`. <Observable result>.
- **Proof.** Reload the page and open the saved record. Save `artifacts/<journey>/result.png`. The artifact shows <expected end state>.

## Gotchas

- <A trap that can waste or invalidate a run, such as a toast that appears before the save completes.>
````

### CLI or service journey

````markdown
# <Journey title>

<One paragraph: what the client sends and what it gets back when it works.>

## Sub-features

- `<journey>-ok` <returns the expected result for a valid request>.
- `<journey>-refused` <refuses a bad request with a clear error>.

## How to get to it (user POV)

- Run `<command> <arguments>`.
- Send `<METHOD> http://127.0.0.1:3000/<path>`.

## Driving it with verify-<project>

Preconditions:

- The doctor in `../SKILL.md` passes.
- <Seed record or token that this journey needs.>

- **<Step name>.** <Client action>. Run `<exact command or curl call>`. Exit code `0` and stdout contain <expected output>.
- **<Step name>.** <Bad input>. Run `<exact command or curl call>`. Status `<code>` and the body names <the error>.
- **Proof.** Read the stored result back. Run `<second command or endpoint>`. Save the transcript to `artifacts/<journey>/transcript.txt`.

## Gotchas

- <A trap that can waste or invalidate a run, such as a job that finishes after the response returns.>
````

## 4. Turn QA on

1. Copy the verify skill and the feature map into the project repo, and run the skill once end to end.
2. Add the project's `qa_storage` entry under its project in `orch.local.yml`.
3. Set `enabled: true` in the stanza.
4. Run `orch check-config --factory factory.md`. It must report no error.
5. Run `orch qa smoke --repo <owner>/<repo> --runner local-docker` on each machine that runs QA. Every line must start with `ok`.
