"""Deterministic completed-bar replay gates for the paper agent workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from agent_contracts import DecisionTrace


@dataclass(frozen=True, slots=True)
class ReplayScenario:
    name: str
    signal_bar_completed_at: datetime
    execution_bar_completed_at: datetime

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("replay scenario requires a name")
        if self.execution_bar_completed_at <= self.signal_bar_completed_at:
            raise ValueError("execution must occur on a completed bar after the signal bar")


@dataclass(frozen=True, slots=True)
class ReplayResult:
    scenario: str
    fingerprint: str
    deterministic: bool


class DeterministicReplayRunner:
    def __init__(self, evaluator: Callable[[ReplayScenario], DecisionTrace]) -> None:
        self.evaluator = evaluator

    def run(self, scenarios: tuple[ReplayScenario, ...]) -> tuple[ReplayResult, ...]:
        results = []
        for scenario in scenarios:
            first = self.evaluator(scenario)
            second = self.evaluator(scenario)
            deterministic = first.replay_fingerprint == second.replay_fingerprint
            if not deterministic:
                raise RuntimeError(f"replay drift detected for {scenario.name}")
            results.append(ReplayResult(scenario.name, first.replay_fingerprint, True))
        return tuple(results)
