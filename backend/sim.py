"""
sim.py — Traffic assignment simulation.

Implements iterative shortest-path (Method of Successive Averages) traffic
assignment with the BPR congestion function:
    t = t0 · (1 + 0.15 · (V/C)^4)

Runs SIM_ITERATIONS iterations.  Returns per-edge volume/capacity ratios
and a sample of up to TRAJECTORY_SAMPLE_SIZE vehicle trajectories as
[lon, lat, timestamp] lists for the deck.gl TripsLayer.

Speed strategy
--------------
OD pairs are compressed to a dict of {(o, d): weight} and each *unique
origin* gets exactly one nx.single_source_dijkstra call per iteration,
yielding shortest paths to all reachable destinations simultaneously.
This avoids re-running Dijkstra for every individual trip.

Usage
-----
    from backend.sim import run_simulation
    result = run_simulation(scenario_id, scenario, G)
"""

from __future__ import annotations

import logging
import random
import re
from collections import defaultdict
from typing import Any

import networkx as nx

from .models import (
    EdgeMetric,
    SimulationMetrics,
    SimulationResult,
    VehicleTrajectory,
    Waypoint,
)
from .network import (
    BBOX_EAST,
    BBOX_NORTH,
    BBOX_SOUTH,
    BBOX_WEST,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BPR_ALPHA = 0.15
BPR_BETA = 4.0
SIM_ITERATIONS = 4
TRAJECTORY_SAMPLE_SIZE = 2000
PEAK_HOUR_DURATION_S = 3600.0

# Background peak-hour OD *pairs* (each counts as 1 trip vehicle).
# Calibrated so that primary/trunk corridors run V/C ≈ 0.75–0.85 at baseline
# on the real OSM graph (~3,000–4,000 edges).  Arteries concentrate flow;
# with 25,000 background trips the busiest few edges see ~1,500–1,700 veh/h
# against capacity of 1,800–3,600 veh/h → V/C 0.5–0.95.
BACKGROUND_PEAK_TRIPS = 25_000

# ITE trip-generation rates (planning-level, cited as approximate)
TRIPS_PER_APARTMENT_UNIT_DAY = 5.4
TRIPS_PER_1000_SQFT_GROCERY_DAY = 100.0
PEAK_HOUR_FRACTION = 0.10      # ~10 % of daily trips occur in the peak hour


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_simulation(
    scenario_id: str,
    scenario: Any,          # backend.models.Scenario
    G: nx.MultiDiGraph,
) -> SimulationResult:
    """
    Run baseline and scenario traffic assignments and return a
    SimulationResult with edge metrics and sampled trajectories.

    Both runs use the same synthetic OD matrix; the scenario run adds
    development-generated trips and applies lane-closure capacity reductions.
    """
    logger.info("[%s] Starting simulation …", scenario_id)

    if len(G.nodes) < 2:
        raise ValueError("Graph has fewer than 2 nodes; cannot simulate.")

    # --- Background demand -------------------------------------------------
    background_od = _generate_od_weights(G, BACKGROUND_PEAK_TRIPS)

    # --- Baseline assignment -----------------------------------------------
    baseline_vols, baseline_paths = _iterative_assignment(
        G, background_od, lane_closures=None,
    )
    baseline_edges = _edge_metrics(G, baseline_vols)
    baseline_trajs = _build_trajectories(
        baseline_paths, G, TRAJECTORY_SAMPLE_SIZE,
    )
    logger.info("[%s] Baseline done — %d edge metrics, %d trajectories",
                scenario_id, len(baseline_edges), len(baseline_trajs))

    # --- Scenario demand ---------------------------------------------------
    added_daily = _added_daily_trips(scenario)
    added_peak = int(added_daily * PEAK_HOUR_FRACTION)
    dev_od = _generate_od_weights_near(
        G, added_peak, scenario.location_lat, scenario.location_lon,
    )
    # Merge background + development OD weights
    scenario_od: dict[tuple[Any, Any], float] = dict(background_od)
    for pair, w in dev_od.items():
        scenario_od[pair] = scenario_od.get(pair, 0.0) + w

    # --- Scenario assignment (with lane closures) --------------------------
    scenario_vols, scenario_paths = _iterative_assignment(
        G, scenario_od, lane_closures=scenario.lane_closures,
    )
    scenario_edges = _edge_metrics(G, scenario_vols)
    scenario_trajs = _build_trajectories(
        scenario_paths, G, TRAJECTORY_SAMPLE_SIZE,
    )
    logger.info("[%s] Scenario done — %d edge metrics, %d trajectories",
                scenario_id, len(scenario_edges), len(scenario_trajs))

    # --- Summary metrics ---------------------------------------------------
    metrics = _compute_metrics(
        G, scenario, baseline_vols, scenario_vols, added_daily,
    )

    return SimulationResult(
        scenario_id=scenario_id,
        status="complete",
        baseline_edges=baseline_edges,
        scenario_edges=scenario_edges,
        baseline_trajectories=baseline_trajs,
        scenario_trajectories=scenario_trajs,
        metrics=metrics,
    )


# ---------------------------------------------------------------------------
# BPR function
# ---------------------------------------------------------------------------

def bpr_travel_time(t0: float, volume: float, capacity: float) -> float:
    """BPR link performance function: t = t0 · (1 + α·(V/C)^β)."""
    if capacity <= 0:
        return t0
    return t0 * (1.0 + BPR_ALPHA * (volume / capacity) ** BPR_BETA)


# ---------------------------------------------------------------------------
# Iterative assignment (Method of Successive Averages)
# ---------------------------------------------------------------------------

def _iterative_assignment(
    G: nx.MultiDiGraph,
    od_weights: dict[tuple[Any, Any], float],
    *,
    lane_closures: list[Any] | None,
) -> tuple[dict[tuple[Any, Any, int], float], list[list[Any]]]:
    """
    Run SIM_ITERATIONS rounds of weighted all-or-nothing shortest-path
    assignment averaged via MSA weights (1/n), with BPR travel-time updates.

    *od_weights* maps (origin, dest) → number of trips.  Each unique origin
    gets one nx.single_source_dijkstra call per iteration, so the total
    number of Dijkstra runs = unique_origins × SIM_ITERATIONS (not n_trips).

    Returns
    -------
    volumes   : dict (u, v, key) → peak-hour vehicle volume
    paths     : list of [node, ...] lists (last iteration, one per OD pair)
    """
    # Pre-compute free-flow capacities (possibly reduced by lane closures)
    edge_cap: dict[tuple[Any, Any, int], float] = {}
    for u, v, k, data in G.edges(keys=True, data=True):
        cap = float(data.get("capacity", 3600.0))
        if lane_closures:
            cap = _apply_closures(data, cap, lane_closures)
        edge_cap[(u, v, k)] = max(cap, 1.0)

    # Working travel-time dict; starts at free-flow
    edge_time: dict[tuple[Any, Any, int], float] = {
        (u, v, k): float(data.get("travel_time", 30.0))
        for u, v, k, data in G.edges(keys=True, data=True)
    }

    # Group OD pairs by origin so we can batch per unique source
    origins_to_dests: dict[Any, dict[Any, float]] = defaultdict(dict)
    for (o, d), w in od_weights.items():
        if o != d:
            origins_to_dests[o][d] = w

    accumulated: dict[tuple[Any, Any, int], float] = {}
    last_paths: list[list[Any]] = []

    for iteration in range(1, SIM_ITERATIONS + 1):
        msa_w = 1.0 / iteration

        # Collapse MultiDiGraph to best-time (u,v) for Dijkstra
        # Build a plain nx.DiGraph with 'weight' = current travel time
        H = nx.DiGraph()
        edge_key_map: dict[tuple[Any, Any], int] = {}
        for (u, v, k), t in edge_time.items():
            if H.has_edge(u, v):
                if t < H[u][v]["weight"]:
                    H[u][v]["weight"] = t
                    edge_key_map[(u, v)] = k
            else:
                H.add_edge(u, v, weight=t)
                edge_key_map[(u, v)] = k

        # All-or-nothing assignment: one Dijkstra per unique origin
        iter_vols: dict[tuple[Any, Any, int], float] = defaultdict(float)
        iter_paths: list[list[Any]] = []

        for origin, dest_weights in origins_to_dests.items():
            if origin not in H:
                continue
            # Single-source shortest paths from this origin to all reachable nodes
            try:
                lengths, path_map = nx.single_source_dijkstra(
                    H, origin, weight="weight",
                )
            except Exception:
                continue

            for dest, w in dest_weights.items():
                if dest not in path_map:
                    continue
                path = path_map[dest]
                iter_paths.append(path)
                for i in range(len(path) - 1):
                    u, v2 = path[i], path[i + 1]
                    k = edge_key_map.get((u, v2), 0)
                    iter_vols[(u, v2, k)] += w

        # MSA blend
        if iteration == 1:
            accumulated = dict(iter_vols)
        else:
            all_keys = set(accumulated) | set(iter_vols)
            accumulated = {
                e: (1 - msa_w) * accumulated.get(e, 0.0) + msa_w * iter_vols.get(e, 0.0)
                for e in all_keys
            }

        # BPR travel-time update
        for (u, v, k), t0 in edge_time.items():
            t0_ff = float(G[u][v][k].get("travel_time", 30.0))
            vol = accumulated.get((u, v, k), 0.0)
            cap = edge_cap[(u, v, k)]
            edge_time[(u, v, k)] = bpr_travel_time(t0_ff, vol, cap)

        last_paths = iter_paths
        logger.debug("Iter %d/%d — %d origins, vol keys: %d",
                     iteration, SIM_ITERATIONS,
                     len(origins_to_dests), len(accumulated))

    return accumulated, last_paths


# ---------------------------------------------------------------------------
# Lane-closure matching — case-insensitive + suffix normalization
# ---------------------------------------------------------------------------

# Canonical suffix map: maps any variant to a single canonical token
_SUFFIX_MAP: dict[str, str] = {
    "avenue": "ave", "av": "ave",
    "street": "st",
    "boulevard": "blvd", "boul": "blvd",
    "drive": "dr",
    "road": "rd",
    "lane": "ln",
    "court": "ct",
    "place": "pl",
    "highway": "hwy",
    "freeway": "fwy",
    "expressway": "expy",
    "parkway": "pkwy", "pky": "pkwy",
    "terrace": "ter",
    "trail": "trl",
    "way": "wy",
    "circle": "cir",
}


def _normalize_road_name(name: str) -> str:
    """
    Lower-case, strip punctuation, collapse whitespace, then replace any
    suffix variant with its canonical abbreviation.
    """
    name = name.lower().strip()
    name = re.sub(r"[.,;'\"]", "", name)
    name = re.sub(r"\s+", " ", name)
    tokens = name.split()
    if tokens:
        tokens[-1] = _SUFFIX_MAP.get(tokens[-1], tokens[-1])
    return " ".join(tokens)


def _apply_closures(
    data: dict[str, Any],
    base_cap: float,
    lane_closures: list[Any],
) -> float:
    """
    Reduce capacity on edges whose OSM 'name' matches a lane closure.
    capacity × max(1, lanes − lanes_closed) / lanes

    Matching is case-insensitive and suffix-normalised (Ave = Avenue, etc.).
    """
    raw_name = data.get("name", "")
    if isinstance(raw_name, list):
        raw_name = raw_name[0] if raw_name else ""
    edge_norm = _normalize_road_name(str(raw_name))

    for lc in lane_closures:
        lc_norm = _normalize_road_name(lc.road_name)
        if lc_norm in edge_norm or edge_norm in lc_norm:
            lanes = max(1, int(data.get("lanes_count", 2)))
            remaining = max(1, lanes - lc.lanes_closed)
            return base_cap * remaining / lanes
    return base_cap


# ---------------------------------------------------------------------------
# OD demand generation
# ---------------------------------------------------------------------------

def _generate_od_weights(
    G: nx.MultiDiGraph,
    n_trips: int,
    seed: int = 42,
) -> dict[tuple[Any, Any], float]:
    """
    Return a dict mapping (origin, dest) → trip weight summing to n_trips.
    Pairs are sampled uniformly from graph nodes (seed-deterministic).
    Duplicate draws accumulate weight on the same pair, reducing the number
    of unique Dijkstra calls.
    """
    nodes = list(G.nodes())
    if len(nodes) < 2:
        return {}
    rng = random.Random(seed)
    weights: dict[tuple[Any, Any], float] = defaultdict(float)
    for _ in range(n_trips):
        o, d = rng.sample(nodes, 2)
        weights[(o, d)] += 1.0
    return dict(weights)


def _generate_od_weights_near(
    G: nx.MultiDiGraph,
    n_trips: int,
    lat: float,
    lon: float,
    radius_nodes: int = 5,
    seed: int = 99,
) -> dict[tuple[Any, Any], float]:
    """
    New-development trips: origins near (lat, lon), destinations anywhere.
    """
    nodes = list(G.nodes())
    if len(nodes) < 2 or n_trips == 0:
        return {}

    def dist2(nid: Any) -> float:
        d = G.nodes[nid]
        return (d.get("y", 0) - lat) ** 2 + (d.get("x", 0) - lon) ** 2

    nearby = sorted(nodes, key=dist2)[:radius_nodes]
    rng = random.Random(seed)
    weights: dict[tuple[Any, Any], float] = defaultdict(float)
    for _ in range(n_trips):
        o = rng.choice(nearby)
        d = rng.choice(nodes)
        if d != o:
            weights[(o, d)] += 1.0
    return dict(weights)


# ---------------------------------------------------------------------------
# Trip-generation rates
# ---------------------------------------------------------------------------

def _added_daily_trips(scenario: Any) -> int:
    """
    Estimate added daily vehicle trips using ITE planning-level rates.
    """
    apt_trips = scenario.housing_units * TRIPS_PER_APARTMENT_UNIT_DAY
    grocery_trips = (scenario.retail_sqft / 1000.0) * TRIPS_PER_1000_SQFT_GROCERY_DAY
    return int(apt_trips + grocery_trips)


# ---------------------------------------------------------------------------
# Edge metrics
# ---------------------------------------------------------------------------

def _edge_metrics(
    G: nx.MultiDiGraph,
    volumes: dict[tuple[Any, Any, int], float],
) -> list[EdgeMetric]:
    metrics: list[EdgeMetric] = []
    for u, v, k, data in G.edges(keys=True, data=True):
        cap = float(data.get("capacity", 3600.0))
        vol = volumes.get((u, v, k), 0.0)
        metrics.append(EdgeMetric(
            edge_id=f"{u}-{v}-{k}",
            volume=round(vol, 2),
            capacity=round(cap, 2),
            vc_ratio=round(vol / cap, 4) if cap > 0 else 0.0,
        ))
    return metrics


# ---------------------------------------------------------------------------
# Trajectory building
# ---------------------------------------------------------------------------

def _build_trajectories(
    paths: list[list[Any]],
    G: nx.MultiDiGraph,
    sample_size: int,
) -> list[VehicleTrajectory]:
    """
    Convert a sample of node-path lists into VehicleTrajectory objects
    with [lon, lat, timestamp] waypoints for the deck.gl TripsLayer.

    Vehicles are staggered in departure time across the peak hour so
    the animation shows continuous flow rather than a single wave.
    """
    if not paths:
        return []

    rng = random.Random(7)
    sampled = rng.sample(paths, min(sample_size, len(paths)))

    trajectories: list[VehicleTrajectory] = []
    for idx, path in enumerate(sampled):
        if len(path) < 2:
            continue

        depart_t = rng.uniform(0.0, PEAK_HOUR_DURATION_S * 0.9)
        waypoints: list[Waypoint] = []
        t = depart_t

        for i, node in enumerate(path):
            nd = G.nodes[node]
            lon = float(nd.get("x", 0.0))
            lat = float(nd.get("y", 0.0))
            waypoints.append(Waypoint(lon=lon, lat=lat, timestamp=round(t, 1)))

            if i < len(path) - 1:
                nxt = path[i + 1]
                edge_data = G.get_edge_data(node, nxt)
                if edge_data:
                    best_t = min(
                        float(d.get("travel_time", 30.0))
                        for d in edge_data.values()
                    )
                else:
                    best_t = 30.0
                t += best_t

        if len(waypoints) >= 2:
            trajectories.append(VehicleTrajectory(
                vehicle_id=f"v{idx}",
                waypoints=waypoints,
            ))

    return trajectories


# ---------------------------------------------------------------------------
# Summary metrics
# ---------------------------------------------------------------------------

def _compute_metrics(
    G: nx.MultiDiGraph,
    scenario: Any,
    baseline_vols: dict[tuple[Any, Any, int], float],
    scenario_vols: dict[tuple[Any, Any, int], float],
    added_daily: int,
) -> SimulationMetrics:
    """Compute summary statistics comparing baseline and scenario runs."""

    def avg_tt(vols: dict[tuple[Any, Any, int], float]) -> float:
        total_veh_s = 0.0
        total_veh = 0.0
        for (u, v, k), vol in vols.items():
            if vol <= 0:
                continue
            t0 = float(G[u][v][k].get("travel_time", 30.0))
            cap = float(G[u][v][k].get("capacity", 3600.0))
            tt = bpr_travel_time(t0, vol, cap)
            total_veh_s += vol * tt
            total_veh += vol
        return total_veh_s / total_veh if total_veh > 0 else 0.0

    base_tt = avg_tt(baseline_vols)
    scen_tt = avg_tt(scenario_vols)
    tt_change_pct = ((scen_tt - base_tt) / base_tt * 100.0) if base_tt > 0 else 0.0

    # Worst intersections by scenario V/C
    vc_by_node: dict[Any, list[float]] = defaultdict(list)
    for (u, v, k), vol in scenario_vols.items():
        cap = float(G[u][v][k].get("capacity", 3600.0))
        vc_by_node[u].append(vol / cap if cap > 0 else 0.0)

    node_max_vc = {n: max(vcs) for n, vcs in vc_by_node.items()}
    top5 = sorted(node_max_vc.items(), key=lambda x: x[1], reverse=True)[:5]
    worst = [
        {
            "node_id": str(nid),
            "lat": G.nodes[nid].get("y", 0.0),
            "lon": G.nodes[nid].get("x", 0.0),
            "max_vc": round(vc, 3),
        }
        for nid, vc in top5
    ]

    return SimulationMetrics(
        added_daily_trips=added_daily,
        worst_intersections=worst,
        travel_time_change_pct=round(tt_change_pct, 2),
        description=(
            f"Development adds ~{added_daily:,} daily trips "
            f"({int(added_daily * PEAK_HOUR_FRACTION):,} in peak hour). "
            f"Network average travel time changes by {tt_change_pct:+.1f}%."
        ),
    )
