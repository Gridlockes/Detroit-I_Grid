"""
routers/simulation.py — POST /simulate/{id}  ·  GET /results/{id}  ·  GET /results/latest

POST /simulate/{id}   – kick off the traffic simulation for a saved scenario
GET  /results/{id}    – return trips, congestion, and metrics (polled by the map)
GET  /results/latest  – convenience alias; the frontend polls this endpoint
"""

from __future__ import annotations

import threading

from fastapi import APIRouter, BackgroundTasks, HTTPException

from ..models import SimulationResult
from ..store import store

router = APIRouter(tags=["simulation"])


# ---------------------------------------------------------------------------
# Trigger simulation
# ---------------------------------------------------------------------------

@router.post(
    "/simulate/{scenario_id}",
    response_model=SimulationResult,
    summary="Run the traffic simulation for a scenario",
    description=(
        "Starts an asynchronous traffic assignment run for the given "
        "scenario id.  Returns immediately with status='running'.  "
        "Poll GET /results/{id} until status='complete'."
    ),
)
def trigger_simulation(
    scenario_id: str,
    background_tasks: BackgroundTasks,
) -> SimulationResult:
    """Start a background simulation run and return a pending result."""
    scenario = store.get_scenario(scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail=f"Scenario {scenario_id!r} not found")

    pending = SimulationResult(scenario_id=scenario_id, status="running")
    store.save_result(pending)

    background_tasks.add_task(_run_simulation_task, scenario_id)
    return pending


def _run_simulation_task(scenario_id: str) -> None:
    """Background task: load network, run sim, store result."""
    import logging
    logger = logging.getLogger(__name__)
    try:
        from ..network import get_graph
        from ..sim import run_simulation

        scenario = store.get_scenario(scenario_id)
        if scenario is None:
            logger.error("[%s] scenario not found in store", scenario_id)
            return

        G = get_graph()
        result = run_simulation(scenario_id, scenario, G)
        store.save_result(result)
    except Exception:
        logger.exception("[%s] simulation failed", scenario_id)
        from ..models import SimulationResult
        store.save_result(SimulationResult(scenario_id=scenario_id, status="error"))


# ---------------------------------------------------------------------------
# Fetch results
# ---------------------------------------------------------------------------

@router.get(
    "/results/{scenario_id}",
    response_model=SimulationResult,
    summary="Get simulation results for a scenario",
    description="Returns edge-level V/C ratios, sampled vehicle trajectories, and summary metrics.",
)
def get_results(scenario_id: str) -> SimulationResult:
    result = store.get_result(scenario_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"No results for scenario {scenario_id!r}")
    return result


@router.get(
    "/results/latest",
    response_model=SimulationResult,
    summary="Get results for the most recent simulation",
    description="Convenience endpoint polled by the frontend map page.",
)
def get_latest_results() -> SimulationResult:
    result = store.get_latest_result()
    if result is None:
        raise HTTPException(status_code=404, detail="No simulation has been run yet")
    return result
