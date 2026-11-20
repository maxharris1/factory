from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import shlex
import sys
import time
from pathlib import Path

KIND_LABELS = {"compose": "com.docker.compose.project", "supabase": "com.supabase.cli.project"}
_FIELD = re.compile(r'\{\{\.ID\}\}|\{\{\.CreatedAt\}\}|\{\{\.Label "([^"]*)"\}\}')


def _matches(labels: dict[str, str], wanted: str) -> bool:
    key, _, value = wanted.partition("=")
    return key in labels and (not value or labels[key] == value)


def _filters(rest: list[str]) -> list[str]:
    return [rest[i + 1].removeprefix("label=") for i, a in enumerate(rest) if a == "--filter"]


def _render(template: str, container: dict) -> str:
    def field(match: re.Match[str]) -> str:
        if match.group(0) == "{{.ID}}":
            return container["id"]
        if match.group(0) == "{{.CreatedAt}}":
            return time.strftime("%Y-%m-%d %H:%M:%S +0000 UTC", time.gmtime(container["created"]))
        return container["labels"].get(match.group(1), "")

    return _FIELD.sub(field, template)


def _remove_unused(kind: str, state: dict, names: set[str], key: str) -> int:
    code = 0
    for item in list(state[kind]):
        if item[key] not in names:
            continue
        if any(item[key] in c.get(kind, []) for c in state["containers"]):
            print(f"Error response from daemon: {kind[:-1]} {item[key]} is in use", file=sys.stderr)
            code = 1
        else:
            state[kind].remove(item)
    return code


def main(state_path: Path, args: list[str]) -> int:
    with open(f"{state_path}.calls", "a", encoding="utf-8") as calls:
        calls.write(json.dumps(args) + "\n")
    if state_path.with_name(state_path.name + ".down").exists():
        print("Cannot connect to the Docker daemon. Is the docker daemon running?", file=sys.stderr)
        return 1
    with open(f"{state_path}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(state_path.read_text())
        containers = state["containers"]
        command, rest = args[0], args[1:]
        code = 0
        if command == "run":
            labels = {}
            for index, arg in enumerate(rest):
                if arg == "--label":
                    key, _, value = rest[index + 1].partition("=")
                    labels[key] = value
            container = {"id": secrets.token_hex(6), "labels": labels, "created": time.time()}
            containers.append(container)
            print(container["id"])
        elif command == "ps":
            wanted = _filters(rest)
            template = rest[rest.index("--format") + 1] if "--format" in rest else None
            for container in containers:
                if all(_matches(container["labels"], w) for w in wanted):
                    if "-q" in rest or "-aq" in rest:
                        print(container["id"])
                    elif template is not None:
                        print(_render(template, container))
                    else:
                        print(container["id"], container["labels"].get("factory.job", ""))
        elif command == "rm":
            gone = {arg for arg in rest if not arg.startswith("-")}
            state["containers"] = [c for c in containers if c["id"] not in gone]
        elif command in ("network", "volume") and rest[0] == "ls":
            key = "id" if command == "network" else "name"
            wanted = _filters(rest)
            for item in state[f"{command}s"]:
                if all(_matches(item["labels"], w) for w in wanted):
                    print(item[key])
        elif command in ("network", "volume") and rest[0] == "rm":
            key = "id" if command == "network" else "name"
            code = _remove_unused(f"{command}s", state, set(rest[1:]), key)
        else:
            print(f"fake docker: unknown command {command}", file=sys.stderr)
            return 2
        state_path.write_text(json.dumps(state))
    return code


class Docker:
    def __init__(self, tmp_path: Path):
        root = tmp_path / "fake-docker"
        self.bin = root / "bin"
        self.bin.mkdir(parents=True)
        self.state = root / "containers.json"
        self._write({"containers": [], "networks": [], "volumes": []})
        wrapper = self.bin / "docker"
        wrapper.write_text(
            f'#!/bin/sh\nexec {sys.executable} {Path(__file__).resolve()} {self.state} "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)

    def _read(self) -> dict:
        return json.loads(self.state.read_text())

    def _write(self, state: dict) -> None:
        self.state.write_text(json.dumps(state))

    def login_home(self) -> Path:
        home = self.bin.parent / "home"
        home.mkdir(exist_ok=True)
        (home / ".bash_profile").write_text(f"export PATH={shlex.quote(str(self.bin))}:$PATH\n")
        return home

    def path(self, path: str | None = None) -> str:
        return f"{self.bin}{os.pathsep}{path if path is not None else os.environ['PATH']}"

    def start(self, created: float | None = None, networks: tuple[str, ...] = (),
              volumes: tuple[str, ...] = (), **labels: str) -> str:
        state = self._read()
        container = {
            "id": secrets.token_hex(6),
            "labels": dict(labels),
            "created": time.time() if created is None else created,
            "networks": list(networks),
            "volumes": list(volumes),
        }
        state["containers"].append(container)
        self._write(state)
        return container["id"]

    def start_for(self, job_id: str) -> str:
        return self.start(**{"factory.job": job_id})

    def network(self, **labels: str) -> str:
        state = self._read()
        network = {"id": secrets.token_hex(6), "labels": dict(labels)}
        state["networks"].append(network)
        self._write(state)
        return network["id"]

    def volume(self, **labels: str) -> str:
        state = self._read()
        volume = {"name": f"vol_{secrets.token_hex(4)}", "labels": dict(labels)}
        state["volumes"].append(volume)
        self._write(state)
        return volume["name"]

    def stack(self, project: str, n: int, created: float, working_dir: str | None = None,
              kind: str = "compose") -> list[str]:
        labels = {KIND_LABELS[kind]: project}
        network, volume = self.network(**labels), self.volume(**labels)
        if working_dir is not None:
            labels["com.docker.compose.project.working_dir"] = working_dir
        return [
            self.start(created, networks=(network,), volumes=(volume,), **labels)
            for _ in range(n)
        ]

    def ids(self) -> set[str]:
        return {c["id"] for c in self._read()["containers"]}

    def labelled(self) -> list[str]:
        return [c["labels"].get("factory.job", "") for c in self._read()["containers"]]

    def networks(self) -> set[str]:
        return {n["id"] for n in self._read()["networks"]}

    def volumes(self) -> set[str]:
        return {v["name"] for v in self._read()["volumes"]}

    def calls(self) -> list[list[str]]:
        calls = Path(f"{self.state}.calls")
        if not calls.exists():
            return []
        return [json.loads(line) for line in calls.read_text().splitlines()]

    def down(self) -> Path:
        return self.state.with_name(self.state.name + ".down")


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), sys.argv[2:]))
