"""
sim.py — Traffic assignment simulation.

Implements iterative shortest-path (Method of Successive Averages) traffic
assignment with the BPR congestion function:
    t = t0 · (1 + 0.15 · (V/C)^4)

Speed design
------------
* The full node set is clustered into N_ZONES = 150 origin-destination zones.
  Each zone is represented by one node sampled uniformly from the graph.
* The inner loop runs nx.single_source_dijkstra once per zone per iteration:
  150 zones × 4 iterations = 600 Dijkstra calls → ~6 s on the real graph.
* OD demand is stored as {(origin_node, dest_node): weight} so each pair is
  routed exactly once; the weight (trips) accumulates onto edge volumes.

Impact design
-------------
* Development trips originate from and terminate at nodes within DEV_RADIUS_M
  of the site.  The total peak-hour volume added is small relative to the
  background, but it is concentrated on the handful of links that serve the
  site, which is what causes nearby V/C to exceed 1.0.

Usage
-----
    from backend.sim import run_simulation
    result = run_simulation(scenario_id, scenario, G)
"""

from __future__ import annotations

import logging
import math
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

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------

BPR_ALPHA = 0.15
BPR_BETA = 4.0
SIM_ITERATIONS = 4
TRAJECTORY_SAMPLE_SIZE = 2000
PEAK_HOUR_DURATION_S = 3600.0

# Zone count — number of origin/destination centroids.
# 150 zones × 4 iters = 600 single_source_dijkstra calls ≈ 6 s on the real graph.
N_ZONES = 150

# Trips per zone-pair (calibrated so the busiest motorway-ramp / primary corridor
# reaches V/C ≈ 0.80 at baseline on the real OSM graph with 150 zones).
# Derivation: one all-or-nothing pass with 150 zones produces a max raw load of
# ~1,429 vehicles on the busiest motorway link (cap = 1,800 veh/h).
# Target V/C = 0.80 → target volume = 1,440 → trips_per_pair = 1,440/1,429 ≈ 1.008.
TRIPS_PER_ZONE_PAIR = 1.008

# Radius (metres) within which new-development trips load the local network.
DEV_RADIUS_M = 800.0

# ITE trip-generation rates (planning-level, cited as approximate)
TRIPS_PER_APARTMENT_UNIT_DAY = 5.4
TRIPS_PER_1000_SQFT_GROCERY_DAY = 100.0
PEAK_HOUR_FRACTION = 0.10      # ~10 % of daily trips occur in the peak hour

# ---------------------------------------------------------------------------
# Haversine helper
# ---------------------------------------------------------------------------

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return distance in metres between two WGS-84 points."""
    R = 6_371_000.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


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
    """
    logger.info("[%s] Starting simulation (%d nodes, %d edges) …",
                scenario_id, len(G.nodes), len(G.edges))

    if len(G.nodes) < 2:
        raise ValueError("Graph has fewer than 2 nodes; cannot simulate.")

    nodes = list(G.nodes())

    # --- Zone centroids (shared by baseline and scenario) -------------------
    zones = _pick_zones(nodes, N_ZONES)

    # --- Background OD matrix -----------------------------------------------
    background_od = _build_od_matrix(zones, TRIPS_PER_ZONE_PAIR)

    # --- Baseline assignment ------------------------------------------------
    baseline_vols, baseline_paths = _iterative_assignment(
        G, background_od, lane_closures=None,
    )
    baseline_edges = _edge_metrics(G, baseline_vols)
    baseline_trajs = _build_trajectories(baseline_paths, G, TRAJECTORY_SAMPLE_SIZE)
    logger.info("[%s] Baseline done — max V/C=%.3f, %d trajectories",
                scenario_id,
                max((e.vc_ratio for e in baseline_edges), default=0.0),
                len(baseline_trajs))

    # --- Development demand -------------------------------------------------
    added_daily = _added_daily_trips(scenario)
    added_peak = int(added_daily * PEAK_HOUR_FRACTION)
    dev_od = _build_dev_od(
        G, nodes, added_peak,
        scenario.location_lat, scenario.location_lon, zones,
    )

    # Merge: background + development
    scenario_od: dict[tuple[Any, Any], float] = dict(background_od)
    for pair, w in dev_od.items():
        scenario_od[pair] = scenario_od.get(pair, 0.0) + w

    # --- Scenario assignment (lane closures applied) -------------------------
    scenario_vols, scenario_paths = _iterative_assignment(
        G, scenario_od, lane_closures=scenario.lane_closures,
    )
    scenario_edges = _edge_metrics(G, scenario_vols)
    scenario_trajs = _build_trajectories(scenario_paths, G, TRAJECTORY_SAMPLE_SIZE)
    logger.info("[%s] Scenario done — max V/C=%.3f, %d trajectories",
                scenario_id,
                max((e.vc_ratio for e in scenario_edges), default=0.0),
                len(scenario_trajs))

    # --- Summary metrics ----------------------------------------------------
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
# Zone selection
# ---------------------------------------------------------------------------

def _pick_zones(nodes: list[Any], n: int, seed: int = 42) -> list[Any]:
    """Sample *n* node IDs as OD zone centroids (deterministic)."""
    rng = random.Random(seed)
    return rng.sample(nodes, min(n, len(nodes)))


def _build_od_matrix(
    zones: list[Any],
    trips_per_pair: float,
) -> dict[tuple[Any, Any], float]:
    """
    Return {(o, d): trips_per_pair} for every ordered (o, d) zone pair
    with o ≠ d.  All pairs get equal weight; real Census-weighted OD
    tables would go here in a production system.
    """
    od: dict[tuple[Any, Any], float] = {}
    for o in zones:
        for d in zones:
            if o != d:
                od[(o, d)] = trips_per_pair
    return od


# ---------------------------------------------------------------------------
# Development OD
# ---------------------------------------------------------------------------

# Maximum number of zone nodes near the development site used as dev-trip origins.
# Using ONLY zone nodes keeps the total unique origins ≤ N_ZONES so no extra
# Dijkstra calls are added for the scenario vs. the baseline.
DEV_NEARBY_ZONES = 10


def _build_dev_od(
    G: nx.MultiDiGraph,
    all_nodes: list[Any],
    n_trips: int,
    site_lat: float,
    site_lon: float,
    zones: list[Any],
) -> dict[tuple[Any, Any], float]:
    """
    Build a development OD matrix using ONLY zone nodes as origins/destinations.

    Strategy:
      • Find the DEV_NEARBY_ZONES zone centroids closest to the site.
      • Route all dev-generated trips outward from those zones to all other zones
        (and inward back), plus internal trips among the nearby zones.

    Because we reuse the same zone node IDs already in the background OD matrix,
    no extra Dijkstra calls are added in _iterative_assignment — the nearby zones
    are already sources in the background run.  The dev trips simply increase the
    weight on pairs that are already being routed, concentrating volume on the
    access links near the development site.

    Total volume loaded ≈ n_trips (peak-hour vehicle trips).
    """
    if n_trips == 0:
        return {}

    # Sort zone nodes by distance to site; take the closest DEV_NEARBY_ZONES
    zones_by_dist = sorted(
        zones,
        key=lambda n: _haversine_m(
            G.nodes[n].get("y", 0.0), G.nodes[n].get("x", 0.0),
            site_lat, site_lon,
        ),
    )
    nearby_zones = zones_by_dist[:DEV_NEARBY_ZONES]

    # Half the trips: nearby_zones ↔ nearby_zones (internal/short trips)
    # Half the trips: nearby_zones ↔ all zones (external generation/attraction)
    od: dict[tuple[Any, Any], float] = defaultdict(float)
    half = n_trips / 2.0

    # --- Internal: nearby_zones ↔ nearby_zones ------------------------------
    internal = [(o, d) for o in nearby_zones for d in nearby_zones if o != d]
    if internal:
        w = half / len(internal)
        for o, d in internal:
            od[(o, d)] += w

    # --- External: nearby_zones ↔ all zones ---------------------------------
    ext_out = [(o, d) for o in nearby_zones for d in zones if o != d]
    ext_in  = [(o, d) for o in zones for d in nearby_zones if o != d]
    ext = ext_out + ext_in
    if ext:
        w = half / len(ext)
        for o, d in ext:
            od[(o, d)] += w

    return dict(od)


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
    Weighted all-or-nothing assignment with MSA blending and BPR updates.

    Speed: uses a hand-written heapq Dijkstra over a plain Python adjacency
    list (~2 ms/call on the 7,815-edge Detroit graph vs. ~11 ms for
    nx.single_source_dijkstra).  One call per unique origin per iteration:
    150 zones × 4 iterations = 600 calls ≈ 1.2 s.

    Returns
    -------
    volumes   : {(u, v, key): veh/h}
    paths     : list of node lists (last iteration's paths, one per OD pair)
    """
    import heapq

    # Pre-compute effective capacities (lane closures applied once)
    edge_cap: dict[tuple[Any, Any, int], float] = {}
    for u, v, k, data in G.edges(keys=True, data=True):
        cap = float(data.get("capacity", 3600.0))
        if lane_closures:
            cap = _apply_closures(data, cap, lane_closures)
        edge_cap[(u, v, k)] = max(cap, 1.0)

    # Free-flow travel times (never mutated; BPR uses these as t₀)
    t0_ff: dict[tuple[Any, Any, int], float] = {
        (u, v, k): float(data.get("travel_time", 30.0))
        for u, v, k, data in G.edges(keys=True, data=True)
    }
    # Working travel times (updated by BPR each iteration)
    edge_time = dict(t0_ff)

    # Group OD pairs by origin to batch Dijkstra per unique origin
    origins_to_dests: dict[Any, dict[Any, float]] = defaultdict(dict)
    for (o, d), w in od_weights.items():
        if o != d and w > 0:
            origins_to_dests[o][d] = origins_to_dests[o].get(d, 0.0) + w

    # Build an index mapping node → index for fast adjacency access
    node_list = list(G.nodes())
    node_idx: dict[Any, int] = {n: i for i, n in enumerate(node_list)}

    accumulated: dict[tuple[Any, Any, int], float] = {}
    last_paths: list[list[Any]] = []

    for iteration in range(1, SIM_ITERATIONS + 1):
        msa_w = 1.0 / iteration

        # Build adjacency list: node → [(neighbour, travel_time, edge_key)]
        # Collapse parallel edges to the minimum-time one per (u, v).
        adj: dict[Any, list[tuple[Any, float, int]]] = defaultdict(list)
        best_key: dict[tuple[Any, Any], int] = {}
        best_time: dict[tuple[Any, Any], float] = {}
        for (u, v, k), t in edge_time.items():
            if (u, v) not in best_time or t < best_time[(u, v)]:
                best_time[(u, v)] = t
                best_key[(u, v)] = k
        # Populate adj from best_time
        adj_map: dict[Any, list[tuple[Any, float]]] = defaultdict(list)
        for (u, v), t in best_time.items():
            adj_map[u].append((v, t))

        iter_vols: dict[tuple[Any, Any, int], float] = defaultdict(float)
        iter_paths: list[list[Any]] = []

        for origin, dest_weights in origins_to_dests.items():
            # --- Single-source Dijkstra (heapq, pure Python) ----------------
            dist: dict[Any, float] = {origin: 0.0}
            prev: dict[Any, Any] = {}
            heap: list[tuple[float, Any]] = [(0.0, origin)]
            visited: set[Any] = set()

            while heap:
                d, u = heapq.heappop(heap)
                if u in visited:
                    continue
                visited.add(u)
                for v2, t in adj_map.get(u, []):
                    nd = d + t
                    if nd < dist.get(v2, math.inf):
                        dist[v2] = nd
                        prev[v2] = u
                        heapq.heappush(heap, (nd, v2))

            # Reconstruct paths for each destination and accumulate volumes
            for dest, w in dest_weights.items():
                if dest not in dist:
                    continue
                # Trace back path
                path: list[Any] = []
                cur = dest
                while cur in prev:
                    path.append(cur)
                    cur = prev[cur]
                path.append(origin)
                path.reverse()
                iter_paths.append(path)
                for i in range(len(path) - 1):
                    u2, v2 = path[i], path[i + 1]
                    k = best_key.get((u2, v2), 0)
                    iter_vols[(u2, v2, k)] += w

        # MSA blend
        if iteration == 1:
            accumulated = dict(iter_vols)
        else:
            all_keys = set(accumulated) | set(iter_vols)
            accumulated = {
                e: (1 - msa_w) * accumulated.get(e, 0.0) + msa_w * iter_vols.get(e, 0.0)
                for e in all_keys
            }

        # BPR update — use original free-flow t0 to avoid drift
        for (u, v, k) in edge_time:
            t0 = t0_ff[(u, v, k)]
            vol = accumulated.get((u, v, k), 0.0)
            cap = edge_cap[(u, v, k)]
            edge_time[(u, v, k)] = bpr_travel_time(t0, vol, cap)

        last_paths = iter_paths
        logger.debug("Iter %d/%d — %d origins, %d loaded edges",
                     iteration, SIM_ITERATIONS,
                     len(origins_to_dests), len(accumulated))

    return accumulated, last_paths


# ---------------------------------------------------------------------------
# Lane-closure matching — case-insensitive + suffix normalization
# ---------------------------------------------------------------------------

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
    """Lower-case, strip punctuation, collapse whitespace, normalise suffix."""
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
    Matching is case-insensitive with suffix normalisation.
    capacity × max(1, lanes − lanes_closed) / lanes
    """
    raw_name = data.get("name", "")
    if isinstance(raw_name, list):
        raw_name = raw_name[0] if raw_name else ""
    edge_norm = _normalize_road_name(str(raw_name))
    if not edge_norm:
        return base_cap

    for lc in lane_closures:
        lc_norm = _normalize_road_name(lc.road_name)
        if lc_norm and (lc_norm in edge_norm or edge_norm in lc_norm):
            lanes = max(1, int(data.get("lanes_count", 2)))
            remaining = max(1, lanes - lc.lanes_closed)
            return base_cap * remaining / lanes
    return base_cap


# ---------------------------------------------------------------------------
# Trip-generation rates
# ---------------------------------------------------------------------------

def _added_daily_trips(scenario: Any) -> int:
    """ITE planning-level trip-generation rates (approximate)."""
    apt = scenario.housing_units * TRIPS_PER_APARTMENT_UNIT_DAY
    grocery = (scenario.retail_sqft / 1000.0) * TRIPS_PER_1000_SQFT_GROCERY_DAY
    return int(apt + grocery)


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
    Sample *sample_size* paths and convert each to a VehicleTrajectory with
    [lon, lat, timestamp] waypoints staggered across the peak hour.
    """
    if not paths:
        return []

    rng = random.Random(7)
    sampled = rng.sample(paths, min(sample_size, len(paths)))

    trajectories: list[VehicleTrajectory] = []
    for idx, path in enumerate(sampled):
        if len(path) < 2:
            continue

        t = rng.uniform(0.0, PEAK_HOUR_DURATION_S * 0.9)
        waypoints: list[Waypoint] = []

        for i, node in enumerate(path):
            nd = G.nodes[node]
            waypoints.append(Waypoint(
                lon=float(nd.get("x", 0.0)),
                lat=float(nd.get("y", 0.0)),
                timestamp=round(t, 1),
            ))
            if i < len(path) - 1:
                nxt = path[i + 1]
                edge_data = G.get_edge_data(node, nxt)
                if edge_data:
                    best_t = min(float(d.get("travel_time", 30.0)) for d in edge_data.values())
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
    """Compute travel-time change and worst-intersection report."""

    def avg_tt(vols: dict[tuple[Any, Any, int], float]) -> float:
        total_veh_s = total_veh = 0.0
        for (u, v, k), vol in vols.items():
            if vol <= 0:
                continue
            t0 = float(G[u][v][k].get("travel_time", 30.0))
            cap = float(G[u][v][k].get("capacity", 3600.0))
            total_veh_s += vol * bpr_travel_time(t0, vol, cap)
            total_veh += vol
        return total_veh_s / total_veh if total_veh > 0 else 0.0

    base_tt = avg_tt(baseline_vols)
    scen_tt = avg_tt(scenario_vols)
    tt_pct = ((scen_tt - base_tt) / base_tt * 100.0) if base_tt > 0 else 0.0

    vc_by_node: dict[Any, list[float]] = defaultdict(list)
    for (u, v, k), vol in scenario_vols.items():
        cap = float(G[u][v][k].get("capacity", 3600.0))
        vc_by_node[u].append(vol / cap if cap > 0 else 0.0)

    top5 = sorted(
        ((n, max(vcs)) for n, vcs in vc_by_node.items()),
        key=lambda x: x[1], reverse=True,
    )[:5]
    worst = [
        {
            "node_id": str(nid),
            "lat": G.nodes[nid].get("y", 0.0),
            "lon": G.nodes[nid].get("x", 0.0),
            "max_vc": round(vc, 3),
        }
        for nid, vc in top5
    ]

    added_peak = int(added_daily * PEAK_HOUR_FRACTION)
    return SimulationMetrics(
        added_daily_trips=added_daily,
        worst_intersections=worst,
        travel_time_change_pct=round(tt_pct, 2),
        description=(
            f"Development adds ~{added_daily:,} daily trips "
            f"({added_peak:,} in peak hour). "
            f"Network-average travel time changes by {tt_pct:+.1f}%."
        ),
    )
