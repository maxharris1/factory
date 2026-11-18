from __future__ import annotations

import fcntl
import json
import os
import secrets
import shlex
import sys
from pathlib import Path


def _matches(labels: dict[str, str], wanted: str) -> bool:
    key, _, value = wanted.partition("=")
    return key in labels and (not value or labels[key] == value)


def main(state: Path, args: list[str]) -> int:
    with open(f"{state}.calls", "a", encoding="utf-8") as calls:
        calls.write(json.dumps(args) + "\n")
    if state.with_name(state.name + ".down").exists():
        print("Cannot connect to the Docker daemon. Is the docker daemon running?", file=sys.stderr)
        return 1
    with open(f"{state}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        containers = json.loads(state.read_text()) if state.exists() else []
        command, rest = args[0], args[1:]
        if command == "run":
            labels = {}
            for index, arg in enumerate(rest):
                if arg == "--label":
                    key, _, value = rest[index + 1].partition("=")
                    labels[key] = value
            container = {"id": secrets.token_hex(6), "labels": labels}
            containers.append(container)
            print(container["id"])
        elif command == "ps":
            wanted = [rest[i + 1].removeprefix("label=") for i, a in enumerate(rest) if a == "--filter"]
            for container in containers:
                if all(_matches(container["labels"], w) for w in wanted):
                    if "-q" in rest or "-aq" in rest:
                        print(container["id"])
                    else:
                        print(container["id"], container["labels"].get("factory.job", ""))
        elif command == "rm":
            gone = {arg for arg in rest if not arg.startswith("-")}
            containers = [c for c in containers if c["id"] not in gone]
        else:
            print(f"fake docker: unknown command {command}", file=sys.stderr)
            return 2
        state.write_text(json.dumps(containers))
    return 0


class Docker:
    def __init__(self, tmp_path: Path):
        root = tmp_path / "fake-docker"
        self.bin = root / "bin"
        self.bin.mkdir(parents=True)
        self.state = root / "containers.json"
        self.state.write_text("[]")
        wrapper = self.bin / "docker"
        wrapper.write_text(
            f'#!/bin/sh\nexec {sys.executable} {Path(__file__).resolve()} {self.state} "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)

    def login_home(self) -> Path:
        home = self.bin.parent / "home"
        home.mkdir(exist_ok=True)
        (home / ".bash_profile").write_text(f"export PATH={shlex.quote(str(self.bin))}:$PATH\n")
        return home

    def path(self, path: str | None = None) -> str:
        return f"{self.bin}{os.pathsep}{path if path is not None else os.environ['PATH']}"

    def start(self, **labels: str) -> str:
        containers = json.loads(self.state.read_text())
        container = {"id": secrets.token_hex(6), "labels": dict(labels)}
        self.state.write_text(json.dumps([*containers, container]))
        return container["id"]

    def start_for(self, job_id: str) -> str:
        return self.start(**{"factory.job": job_id})

    def ids(self) -> set[str]:
        return {c["id"] for c in json.loads(self.state.read_text())}

    def labelled(self) -> list[str]:
        return [c["labels"].get("factory.job", "") for c in json.loads(self.state.read_text())]

    def calls(self) -> list[list[str]]:
        calls = Path(f"{self.state}.calls")
        if not calls.exists():
            return []
        return [json.loads(line) for line in calls.read_text().splitlines()]

    def down(self) -> Path:
        return self.state.with_name(self.state.name + ".down")


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), sys.argv[2:]))
