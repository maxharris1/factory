"""The vendor-neutral interface a QA lane coordinator starts cloud agents through (#784).

A runner is one vendor. lib/qa_cloud.py talks only to this interface, so it
never knows which vendor runs a lane. A Lane carries no secret value: its
environment_name is the name of the vendor's QA environment, never its contents.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

AgentStatus = Literal["running", "ended"]


@dataclass(frozen=True)
class Lane:
    lane_id: str
    brief: str = field(repr=False)
    repo: str
    base_ref: str
    environment_name: str
    model: str | None
    commits: tuple[str, ...] = ()


@dataclass(frozen=True)
class Launched:
    agent_id: str
    url: str


@dataclass(frozen=True)
class AgentInfo:
    machine: str
    version: str


class LaunchError(Exception):
    pass


class RunnerOutage(LaunchError):
    pass


class UsageLimit(LaunchError):
    pass


class LaunchFailed(LaunchError):
    pass


class Runner(Protocol):
    def launch(self, lane: Lane) -> Launched:
        """Launching the same lane_id twice returns the first agent."""

    def status(self, agent_id: str) -> AgentStatus: ...

    def stop(self, agent_id: str) -> None:
        """Stopping an agent that has ended is not an error."""

    def describe(self, agent_id: str) -> AgentInfo: ...

    def ping(self) -> str:
        """Returns the runner's version; raises LaunchError when the runner cannot launch."""


_REGISTRY: dict[str, Callable[[], Runner]] = {}


def register(name: str, factory: Callable[[], Runner]) -> None:
    _REGISTRY[name] = factory


def registered() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def create(name: str) -> Runner:
    try:
        return _REGISTRY[name]()
    except KeyError:
        raise KeyError(f"no QA runner named {name!r}") from None


from lib.qa_runners import local_docker as _local_docker  # noqa: E402, F401
