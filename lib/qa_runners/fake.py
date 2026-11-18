"""The fake QA runner, for tests only (#784). factory.md cannot select it."""

from __future__ import annotations

from lib.qa_runners import AgentInfo, AgentStatus, Lane, Launched, LaunchError, register


class FakeRunner:
    def __init__(self, name: str = "fake"):
        self.name = name
        self.launched: list[Lane] = []
        self.agents: dict[str, AgentStatus] = {}
        self.stopped: list[str] = []
        self.launch_errors: list[LaunchError] = []
        self.stop_errors: list[Exception] = []
        self._by_lane: dict[str, Launched] = {}

    def launch(self, lane: Lane) -> Launched:
        if lane.lane_id in self._by_lane:
            return self._by_lane[lane.lane_id]
        if self.launch_errors:
            raise self.launch_errors.pop(0)
        self.launched.append(lane)
        agent_id = f"{self.name}-{len(self.launched)}"
        self.agents[agent_id] = "running"
        self._by_lane[lane.lane_id] = Launched(agent_id, f"https://agents.invalid/{self.name}/{agent_id}")
        return self._by_lane[lane.lane_id]

    def status(self, agent_id: str) -> AgentStatus:
        return self.agents[agent_id]

    def stop(self, agent_id: str) -> None:
        if self.stop_errors:
            raise self.stop_errors.pop(0)
        if self.agents.get(agent_id) == "running":
            self.stopped.append(agent_id)
        self.agents[agent_id] = "ended"

    def end(self, agent_id: str) -> None:
        self.agents[agent_id] = "ended"

    def describe(self, agent_id: str) -> AgentInfo:
        return AgentInfo(machine="fake-4cpu-16gb", version="fake-1.0")


register("fake", FakeRunner)
