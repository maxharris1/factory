# factory

Grok Bot orchestrates pstack work across Claude Code, Codex and Cursor.

The specification is [docs/SPEC.md](docs/SPEC.md). This repo has the F1-01 and F1-03 contracts. Later Phase 1 items are not built.

## Contracts

- [`orch.yml`](orch.yml) lists the projects to poll and the dispatcher settings ([docs/SPEC.md](docs/SPEC.md) section 7). [`config/orch.example.yml`](config/orch.example.yml) is the minimal file.
- `factory.md` is the per-repo policy ([docs/SPEC.md](docs/SPEC.md) section 6). The generic example is [`docs/factory.example.md`](docs/factory.example.md). Aut0, Cleffy and Tracy examples live under [`examples/`](examples/) and are not those repos.
- `orch:v1` handoff comments ([docs/SPEC.md](docs/SPEC.md) section 5) are parsed and written by [`lib/handoff.py`](lib/handoff.py).

## Check config

```sh
bin/orch check-config
```

Exit 0 when `orch.yml` and the factory.md files are valid. An unknown model name is a warning on stderr and does not fail the check. Exit 1 when a file is refused.
