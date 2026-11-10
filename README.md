# factory

Grok Bot orchestrates pstack work across Claude Code, Codex and Cursor.

The specification is [docs/SPEC.md](docs/SPEC.md). This repo has the F1-01 and F1-03 contracts and the F1-06 blocking wait. The F1-04 dispatcher is not built here; `orch wait` uses the stub job queue in [`lib/dispatcher_queue.py`](lib/dispatcher_queue.py).

## Contracts

- [`orch.yml`](orch.yml) lists the projects to poll and the dispatcher settings ([docs/SPEC.md](docs/SPEC.md) section 7). [`config/orch.example.yml`](config/orch.example.yml) is the minimal file.
- `factory.md` is the per-repo policy ([docs/SPEC.md](docs/SPEC.md) section 6). The generic example is [`docs/factory.example.md`](docs/factory.example.md). Aut0, Cleffy and Tracy examples live under [`examples/`](examples/) and are not those repos.
- `orch:v1` handoff comments ([docs/SPEC.md](docs/SPEC.md) section 5) are parsed and written by [`lib/handoff.py`](lib/handoff.py).
- The local job list is [`lib/dispatcher_queue.py`](lib/dispatcher_queue.py). `open_queue` returns `StubJobQueue` until F1-04 replaces it.

## Check config

```sh
bin/orch check-config
```

Exit 0 when `orch.yml` and the factory.md files are valid. An unknown model name is a warning on stderr. Exit 1 when a file is refused.

## Wait

```sh
bin/orch wait claude-bot
bin/orch job done <job-id>
```

`orch wait <role>` blocks until the local job list has a job for that owning bot. It prints one JSON job line (`id`, `role`, `step`, `note`) and the `orch:v1` handoff, then exits. It does not start a harness. The bot that ran the command starts its harness, then runs `orch job done` with the printed id. The default directory is `~/.factory/job-queue` (`ORCH_JOB_QUEUE` or `--queue` overrides it). Claude Bot, Codex Bot, and Cursor Bot each document this in one "orch jobs" paragraph under [`bots/`](bots/).

## Model family

`orch check-config` refuses a reviewer unless it is a different model family from the builder. The check fails closed.

- A `claude` harness is Anthropic. A `codex` harness is OpenAI. The harness decides the family. A hint that names the other family is refused.
- A `cursor` harness takes its family only from the model hint. `claude*` is Anthropic. `gpt*`, `codex*`, and `o<digit>` (`o3`, `o4-mini`) are OpenAI.
- If either side's family cannot be resolved, the check refuses. A cursor side needs one of those hints.
- If both families resolve and they are the same, the check refuses.
