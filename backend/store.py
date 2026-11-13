"""
store.py — In-memory (and optional file-backed) result store.

Keeps scenarios and simulation results keyed by string ids.
In production this would be replaced by a lightweight database or
IBM Cloud Object Storage, but an in-process dict is sufficient for
the hackathon demo.

Usage
-----
    from backend.store import store
    store.save_scenario(record)
    result = store.get_result(scenario_id)
"""

from __future__ import annotations

import threading
import uuid

from .models import ScenarioRecord, SimulationResult


class _Store:
    """Thread-safe in-memory store for scenarios and results."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._scenarios: dict[str, ScenarioRecord] = {}
        self._results: dict[str, SimulationResult] = {}
        self._latest_id: str | None = None

    # ------------------------------------------------------------------
    # Scenarios
    # ------------------------------------------------------------------

    def save_scenario(self, record: ScenarioRecord) -> ScenarioRecord:
        with self._lock:
            self._scenarios[record.id] = record
        return record

    def get_scenario(self, scenario_id: str) -> ScenarioRecord | None:
        return self._scenarios.get(scenario_id)

    def new_id(self) -> str:
        return str(uuid.uuid4())

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    def save_result(self, result: SimulationResult) -> None:
        with self._lock:
            self._results[result.scenario_id] = result
            self._latest_id = result.scenario_id

    def get_result(self, scenario_id: str) -> SimulationResult | None:
        return self._results.get(scenario_id)

    def get_latest_result(self) -> SimulationResult | None:
        if self._latest_id is None:
            return None
        return self._results.get(self._latest_id)


store = _Store()
