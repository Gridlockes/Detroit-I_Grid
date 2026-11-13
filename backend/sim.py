"""
sim.py — Traffic assignment simulation.

Implements iterative shortest-path assignment with the BPR congestion
function:  t = t0 · (1 + 0.15 · (V/C)^4)

Runs 3–5 iterations.  Returns per-edge volume/capacity ratios and a
sample of ~2,000 vehicle trajectories as [lon, lat, timestamp] lists
for the deck.gl TripsLayer.

Usage
-----
    from backend.sim import run_simulation
    result = run_simulation(scenario_id, scenario, G)
"""

from __future__ import annotations

from typing import Any

import networkx as nx

from .models import EdgeMetric, SimulationResult, VehicleTrajectory

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BPR_ALPHA = 0.15
BPR_BETA = 4.0
SIM_ITERATIONS = 4
TRAJECTORY_SAMPLE_SIZE = 2000
PEAK_HOUR_DURATION_S = 3600.0   # seconds in the simulated peak hour


# ---------------------------------------------------------------------------
# Trip-generation rates (planning-level, cited from ITE Trip Generation)
# ---------------------------------------------------------------------------

TRIPS_PER_APARTMENT_UNIT_DAY = 5.4
TRIPS_PER_1000_SQFT_GROCERY_DAY = 100.0


def run_simulation(
    scenario_id: str,
    scenario: Any,       # backend.models.Scenario
    G: nx.MultiDiGraph,
) -> SimulationResult:
    """
    Run baseline and scenario traffic assignments and return a
    SimulationResult with edge metrics and sampled trajectories.
    """
    # TODO (prompt 3): implement BPR iterative assignment
    raise NotImplementedError("sim.py stub — implement in prompt 3")


def bpr_travel_time(t0: float, volume: float, capacity: float) -> float:
    """BPR link performance function."""
    return t0 * (1.0 + BPR_ALPHA * (volume / capacity) ** BPR_BETA)


def _generate_od_pairs(G: nx.MultiDiGraph, n_trips: int) -> list[tuple[Any, Any]]:
    """
    Generate synthetic origin-destination node pairs weighted by
    Census tract population/jobs (ACS).

    Returns a list of (origin_node, destination_node) tuples.
    """
    # TODO (prompt 3): load ACS weights and sample OD pairs
    raise NotImplementedError


def _trajectories_to_waypoints(
    paths: list[list[Any]],
    G: nx.MultiDiGraph,
    edge_times: dict[tuple[Any, Any, Any], float],
    sample_size: int = TRAJECTORY_SAMPLE_SIZE,
) -> list[VehicleTrajectory]:
    """
    Convert a list of node-path lists into VehicleTrajectory objects
    with [lon, lat, timestamp] waypoints for the TripsLayer.
    """
    # TODO (prompt 3): implement path → waypoint conversion
    raise NotImplementedError
