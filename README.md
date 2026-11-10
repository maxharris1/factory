# factory

Grok Bot orchestrates pstack work across Claude Code, Codex and Cursor.

The specification is [docs/SPEC.md](docs/SPEC.md). This repo has the F1-01 and F1-03 contracts and the F1-08 usage reserve. Later Phase 1 items are not built.

## Contracts

- [`orch.yml`](orch.yml) lists the projects to poll and the dispatcher settings ([docs/SPEC.md](docs/SPEC.md) section 7). [`config/orch.example.yml`](config/orch.example.yml) is the minimal file.
- `factory.md` is the per-repo policy ([docs/SPEC.md](docs/SPEC.md) section 6). The generic example is [`docs/factory.example.md`](docs/factory.example.md). Aut0, Cleffy and Tracy examples live under [`examples/`](examples/) and are not those repos.
- `orch:v1` handoff comments ([docs/SPEC.md](docs/SPEC.md) section 5) are parsed and written by [`lib/handoff.py`](lib/handoff.py).

## Check config

```sh
bin/orch check-config
```

Exit 0 when `orch.yml` and the factory.md files are valid. An unknown model name is a warning on stderr. Exit 1 when a file is refused.

## Usage reserve

`orchestrate-project` (F1-07) calls `route_job` in [`lib/usage.py`](lib/usage.py) with the loaded `factory.md`. The best fit for a role is that role's harness and model hint; the other harnesses follow in `claude`, `codex`, `cursor` order. The reviewer is a different model family from the builder this call selected.

The last 20% of a verified usage cycle is a reserve. A non-urgent job moves to the next fit that is outside the reserve. A resumed paused step, past-ship work, or a job Max marks urgent may use the reserve. Each owning bot calls `reserve_check` on its own harness before starting, when it has a verified report. No verified report leaves the route on best fit. Routing reads no live usage; tests use [`tests/fixtures/usage/`](tests/fixtures/usage/).

## Model family

`orch check-config` refuses a reviewer unless it is a different model family from the builder. The check fails closed.

- A `claude` harness is Anthropic. A `codex` harness is OpenAI. The harness decides the family. A hint that names the other family is refused.
- A `cursor` harness takes its family only from the model hint. `claude*` is Anthropic. `gpt*`, `codex*`, and `o<digit>` (`o3`, `o4-mini`) are OpenAI.
- If either side's family cannot be resolved, the check refuses. A cursor side needs one of those hints.
- If both families resolve and they are the same, the check refuses.
