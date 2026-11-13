"""
sim.py — Traffic assignment simulation.

Implements iterative shortest-path (Method of Successive Averages) traffic
assignment with the BPR congestion function:
    t = t0 · (1 + 0.15 · (V/C)^4)

Speed design
------------
* The full node set is clustered into N_ZONES = 150 origin-destination zones.
  Each zone is represented by one node sampled uniformly from the graph.
* The inner assignment loop uses a hand-written heapq Dijkstra over a plain
  Python adjacency list (~2 ms/call vs ~11 ms for nx.single_source_dijkstra).
  One call per unique origin per iteration:
    baseline:  150 zones × 4 iterations = 600 calls ≈ 1.2 s
    scenario:  (150 + DEV_EXTRA_ORIGINS) × 4 ≈ 1.6 s
* Total run is comfortably under 15 s on the 7,815-edge Detroit graph.

Demand calibration
------------------
* Background: N_ZONES × (N_ZONES−1) zone pairs, each carrying TRIPS_PER_ZONE_PAIR
  trips.  Calibrated so the network-wide maximum V/C at baseline ≈ 0.80 (the
  busiest motorway ramp/link hits that ceiling; major arteries run 0.3–0.6).
* Development: DEV_EXTRA_ORIGINS graph nodes within DEV_RADIUS_M of the site
  (beyond the zone set) each send equal-weight trips to all zone destinations.
  Total dev-added volume on the network = added_peak_trips.  Because the nearby
  origins all share a small number of egress links, those links accumulate
  concentrated load → V/C > 1.0 near the site.

Usage
-----
    from backend.sim import run_simulation
    result = run_simulation(scenario_id, scenario, G)
"""

from __future__ import annotations

import heapq
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

# Zone count — number of uniform background OD centroids.
N_ZONES = 150

# Trips per zone-pair for the background matrix (peak hour).
# Calibrated on the real 7,815-edge Detroit graph with 150 arterial zones:
# baseline median V/C ≈ 0.46 on primary roads and ≈ 0.37 on Woodward Avenue,
# a plausible PM-peak loading. (The earlier 1.008 left Woodward at ~0.02.)
TRIPS_PER_ZONE_PAIR = 8.064

# Road classes whose intersections may serve as zone centroids. Starting
# trips on residential side streets overloads those streets instead of the
# arterials that carry real peak traffic.
ZONE_HIGHWAY_TYPES = {
    "trunk", "primary", "secondary", "tertiary", "primary_link", "secondary_link",
}

# Development-trip geometry.
DEV_RADIUS_M = 800.0        # radius around site for dev-trip origin nodes
DEV_EXTRA_ORIGINS = 25      # how many nearby non-zone nodes to add as dev origins
                             # 25 × 4 iters × 2 ms ≈ 0.2 s extra

# ITE trip-generation rates (planning-level, cited as approximate)
TRIPS_PER_APARTMENT_UNIT_DAY = 5.4
TRIPS_PER_1000_SQFT_GROCERY_DAY = 100.0
PEAK_HOUR_FRACTION = 0.10      # ~10 % of daily trips occur in the peak hour

# Demolition (planning-level assumptions; shown to users as estimates).
TRIPS_PER_1000_SQFT_OFFICE_DAY = 10.84  # ≈ ITE general office rate
TRUCK_PCE = 2.5                # one heavy haul truck ≈ 2.5 passenger cars of capacity
TRUCK_PEAK_FRACTION = 0.10     # haul trips spread over a ~10-hour work shift
TRUCK_MIN_DISTANCE_M = 800.0   # trucks head for freeway nodes at least this far away
TRUCK_FREEWAY_NODES = 6
TRAJ_POINT_SPACING_M = 40.0    # min spacing of curve points in vehicle paths (payload size)
IMPACT_RADIUS_M = 2000.0       # "biggest increases" list only covers streets this close


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
    return 2.0 * R * math.asin(math.sqrt(a))


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
    zones = _pick_zones(_arterial_nodes(G) or nodes, N_ZONES)

    # --- Background OD matrix -----------------------------------------------
    background_od = _build_od_matrix(zones, TRIPS_PER_ZONE_PAIR)

    # --- Site nodes (development + demolition trips start/end here) --------
    dev_origins = _dev_extra_origins(
        nodes, scenario.location_lat, scenario.location_lon, set(zones), G,
    )

    # --- Existing building at the site (baseline only) ----------------------
    # Demolition removes these trips, so they appear in the baseline but
    # not in the scenario.
    removed_daily = _removed_daily_trips(scenario)
    existing_od = _site_od(
        dev_origins, zones, removed_daily * PEAK_HOUR_FRACTION,
    )
    baseline_od = _merge_od(background_od, existing_od)

    # --- Baseline assignment ------------------------------------------------
    baseline_vols, baseline_paths, _caps_base = _iterative_assignment(
        G, baseline_od,
        extra_origins=None,
        lane_closures=None,
    )
    baseline_edges = _edge_metrics(G, baseline_vols, _caps_base)
    baseline_trajs = _build_trajectories(
        baseline_paths, G, TRAJECTORY_SAMPLE_SIZE,
        edge_times=_congested_times(G, baseline_vols, _caps_base),
    )
    logger.info("[%s] Baseline done — max V/C=%.3f, %d trajectories",
                scenario_id,
                max((e.vc_ratio for e in baseline_edges), default=0.0),
                len(baseline_trajs))

    # --- Development demand -------------------------------------------------
    added_daily = _added_daily_trips(scenario)
    added_peak = int(added_daily * PEAK_HOUR_FRACTION)
    # dev OD weight: distribute added_peak trips equally across all
    # (dev_origin → zone_dest) pairs so total volume added = added_peak.
    n_dev_pairs = len(dev_origins) * len(zones)
    dev_w = added_peak / max(n_dev_pairs, 1)
    dev_od: dict[tuple[Any, Any], float] = {
        (o, d): dev_w
        for o in dev_origins
        for d in zones
        if o != d
    }

    # Merge background + development
    scenario_od: dict[tuple[Any, Any], float] = dict(background_od)
    for pair, w in dev_od.items():
        scenario_od[pair] = scenario_od.get(pair, 0.0) + w

    # --- Demolition haul trucks (site <-> freeway), in passenger-car units ---
    truck_daily = int(getattr(scenario, "demolition_truck_trips_per_day", 0) or 0)
    if truck_daily > 0:
        freeway = _freeway_nodes(G, scenario.location_lat, scenario.location_lon)
        truck_od = _site_od(
            dev_origins, freeway, truck_daily * TRUCK_PEAK_FRACTION * TRUCK_PCE,
        )
        scenario_od = _merge_od(scenario_od, truck_od)

    # --- Scenario assignment (lane closures applied) -------------------------
    scenario_vols, scenario_paths, _caps_scen = _iterative_assignment(
        G, scenario_od,
        extra_origins=dev_origins,
        lane_closures=scenario.lane_closures,
        site=(scenario.location_lat, scenario.location_lon),
    )
    scenario_edges = _edge_metrics(G, scenario_vols, _caps_scen)
    scenario_trajs = _build_trajectories(
        scenario_paths, G, TRAJECTORY_SAMPLE_SIZE,
        edge_times=_congested_times(G, scenario_vols, _caps_scen),
    )
    logger.info("[%s] Scenario done — max V/C=%.3f, %d trajectories",
                scenario_id,
                max((e.vc_ratio for e in scenario_edges), default=0.0),
                len(scenario_trajs))

    # --- Summary metrics ----------------------------------------------------
    metrics = _compute_metrics(
        G, scenario, baseline_vols, scenario_vols,
        _caps_base, _caps_scen, added_daily,
    )
    metrics.biggest_increases = _biggest_increases(
        G, baseline_vols, scenario_vols, _caps_base, _caps_scen,
        site_lat=scenario.location_lat, site_lon=scenario.location_lon,
    )
    metrics.removed_daily_trips = removed_daily
    metrics.truck_trips_per_day = truck_daily
    if removed_daily or truck_daily:
        if added_daily == 0:
            # Drop the "Development adds ~0 daily trips (0 in peak hour)." clause.
            metrics.description = re.sub(r"^Development adds ~0 daily trips \(0 in peak hour\)\. ",
                                         "", metrics.description)
        parts = []
        if removed_daily:
            parts.append(f"Demolition removes ~{removed_daily:,} daily trips from the existing building.")
        if truck_daily:
            parts.append(f"Construction adds {truck_daily:,} haul-truck trips per day "
                         f"(≈{TRUCK_PCE:g} cars each).")
        metrics.description = " ".join(parts + [metrics.description])

    # Segments whose capacity the lane closures reduced (drawn as a work zone).
    closed_edge_ids = [
        f"{u}-{v}-{k}"
        for (u, v, k), cap in _caps_scen.items()
        if cap < _caps_base.get((u, v, k), cap) - 1e-6
    ]

    return SimulationResult(
        scenario_id=scenario_id,
        status="complete",
        baseline_edges=baseline_edges,
        scenario_edges=scenario_edges,
        baseline_trajectories=baseline_trajs,
        scenario_trajectories=scenario_trajs,
        metrics=metrics,
        closed_edge_ids=closed_edge_ids,
        site={"lat": scenario.location_lat, "lon": scenario.location_lon},
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

def _arterial_nodes(G: nx.MultiDiGraph) -> list[Any]:
    """Nodes touching an arterial edge, sorted for deterministic sampling."""
    found = set()
    for u, v, data in G.edges(data=True):
        hw = data.get("highway")
        hw = hw[0] if isinstance(hw, list) else hw
        if hw in ZONE_HIGHWAY_TYPES:
            found.update((u, v))
    return sorted(found, key=str)


def _pick_zones(nodes: list[Any], n: int, seed: int = 42) -> list[Any]:
    """Sample *n* node IDs as OD zone centroids (deterministic)."""
    rng = random.Random(seed)
    return rng.sample(nodes, min(n, len(nodes)))


def _build_od_matrix(
    zones: list[Any],
    trips_per_pair: float,
) -> dict[tuple[Any, Any], float]:
    """
    Return {(o, d): trips_per_pair} for every ordered (o, d) zone pair o ≠ d.
    All pairs receive equal weight.
    """
    od: dict[tuple[Any, Any], float] = {}
    for o in zones:
        for d in zones:
            if o != d:
                od[(o, d)] = trips_per_pair
    return od


# ---------------------------------------------------------------------------
# Development trip origins
# ---------------------------------------------------------------------------

def _dev_extra_origins(
    all_nodes: list[Any],
    site_lat: float,
    site_lon: float,
    zone_set: set[Any],
    G: nx.MultiDiGraph | None = None,
) -> list[Any]:
    """
    Return up to DEV_EXTRA_ORIGINS graph nodes within DEV_RADIUS_M of the
    site that are NOT already zone centroids.
    """
    if G is None:
        return []
    candidates = [
        n for n in all_nodes
        if n not in zone_set
        and _haversine_m(
            G.nodes[n].get("y", 0.0), G.nodes[n].get("x", 0.0),
            site_lat, site_lon,
        ) <= DEV_RADIUS_M
    ]
    candidates.sort(
        key=lambda n: _haversine_m(
            G.nodes[n].get("y", 0.0), G.nodes[n].get("x", 0.0),
            site_lat, site_lon,
        )
    )
    return candidates[:DEV_EXTRA_ORIGINS]


# ---------------------------------------------------------------------------
# Iterative assignment (Method of Successive Averages)
# ---------------------------------------------------------------------------

def _iterative_assignment(
    G: nx.MultiDiGraph,
    od_weights: dict[tuple[Any, Any], float],
    *,
    extra_origins: list[Any] | None,
    lane_closures: list[Any] | None,
    site: tuple[float, float] | None = None,
) -> tuple[
    dict[tuple[Any, Any, int], float],   # volumes
    list[list[Any]],                      # paths
    dict[tuple[Any, Any, int], float],   # effective capacities
]:
    """
    Weighted all-or-nothing assignment with MSA blending and BPR updates.

    *extra_origins* are additional Dijkstra source nodes (dev-trip origins)
    that do not appear as OD destinations; they add volume but no extra
    destination lookups.

    Returns (volumes, paths, edge_cap) — edge_cap includes any lane-closure
    reductions and is passed to _compute_metrics so it uses the same capacity
    that was used during assignment.
    """
    # Pre-compute effective capacities (lane closures applied once)
    edge_cap: dict[tuple[Any, Any, int], float] = {}
    for u, v, k, data in G.edges(keys=True, data=True):
        cap = float(data.get("capacity", 3600.0))
        if lane_closures:
            cap = _apply_closures(data, cap, lane_closures, G=G, u=u, v=v, site=site)
        edge_cap[(u, v, k)] = max(cap, 1.0)

    # Free-flow travel times (never mutated; BPR uses these as t₀)
    t0_ff: dict[tuple[Any, Any, int], float] = {
        (u, v, k): float(data.get("travel_time", 30.0))
        for u, v, k, data in G.edges(keys=True, data=True)
    }
    edge_time = dict(t0_ff)

    # Group OD pairs by origin to batch Dijkstra per unique origin
    origins_to_dests: dict[Any, dict[Any, float]] = defaultdict(dict)
    for (o, d), w in od_weights.items():
        if o != d and w > 0:
            origins_to_dests[o][d] = origins_to_dests[o].get(d, 0.0) + w

    accumulated: dict[tuple[Any, Any, int], float] = {}
    last_paths: list[list[Any]] = []

    for iteration in range(1, SIM_ITERATIONS + 1):
        msa_w = 1.0 / iteration

        # Collapse parallel edges → minimum-time directed graph
        best_time: dict[tuple[Any, Any], float] = {}
        best_key: dict[tuple[Any, Any], int] = {}
        for (u, v, k), t in edge_time.items():
            if (u, v) not in best_time or t < best_time[(u, v)]:
                best_time[(u, v)] = t
                best_key[(u, v)] = k

        adj_map: dict[Any, list[tuple[Any, float]]] = defaultdict(list)
        for (u, v), t in best_time.items():
            adj_map[u].append((v, t))

        iter_vols: dict[tuple[Any, Any, int], float] = defaultdict(float)
        iter_paths: list[list[Any]] = []

        for origin, dest_weights in origins_to_dests.items():
            dist, prev = _dijkstra(origin, adj_map)

            for dest, w in dest_weights.items():
                if dest not in dist:
                    continue
                path = _trace_path(prev, origin, dest)
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

        # BPR update
        for (u, v, k) in edge_time:
            t0 = t0_ff[(u, v, k)]
            vol = accumulated.get((u, v, k), 0.0)
            cap = edge_cap[(u, v, k)]
            edge_time[(u, v, k)] = bpr_travel_time(t0, vol, cap)

        last_paths = iter_paths
        logger.debug("Iter %d/%d — %d origins, %d loaded edges",
                     iteration, SIM_ITERATIONS,
                     len(origins_to_dests), len(accumulated))

    return accumulated, last_paths, edge_cap


def _dijkstra(
    origin: Any,
    adj_map: dict[Any, list[tuple[Any, float]]],
) -> tuple[dict[Any, float], dict[Any, Any]]:
    """Single-source Dijkstra; returns (dist, prev) dicts."""
    dist: dict[Any, float] = {origin: 0.0}
    prev: dict[Any, Any] = {}
    heap: list[tuple[float, Any]] = [(0.0, origin)]
    visited: set[Any] = set()

    while heap:
        d, u = heapq.heappop(heap)
        if u in visited:
            continue
        visited.add(u)
        for v, t in adj_map.get(u, []):
            nd = d + t
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(heap, (nd, v))

    return dist, prev


def _trace_path(prev: dict[Any, Any], origin: Any, dest: Any) -> list[Any]:
    """Reconstruct a path from the *prev* dict."""
    path: list[Any] = []
    cur = dest
    while cur in prev:
        path.append(cur)
        cur = prev[cur]
    path.append(origin)
    path.reverse()
    return path


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
    *,
    G: nx.MultiDiGraph | None = None,
    u: Any = None,
    v: Any = None,
    site: tuple[float, float] | None = None,
) -> float:
    """
    Reduce capacity on edges whose OSM 'name' matches a lane closure.
    capacity × max(1, lanes − lanes_closed) / lanes
    Matching is case-insensitive with suffix normalisation.

    When *site* is given, only segments within the closure's ``extent_m`` of
    the site are affected: a work zone closes part of a road, not all of it.
    """
    dist_to_site = None
    if site is not None and G is not None and u is not None and v is not None:
        mid_lat = (G.nodes[u]["y"] + G.nodes[v]["y"]) / 2
        mid_lon = (G.nodes[u]["x"] + G.nodes[v]["x"]) / 2
        dist_to_site = _haversine_m(mid_lat, mid_lon, site[0], site[1])
    raw_name = data.get("name", "")
    if isinstance(raw_name, list):
        raw_name = raw_name[0] if raw_name else ""
    edge_norm = _normalize_road_name(str(raw_name))
    if not edge_norm:
        return base_cap

    for lc in lane_closures:
        lc_norm = _normalize_road_name(lc.road_name)
        if lc_norm and (lc_norm in edge_norm or edge_norm in lc_norm):
            extent = float(getattr(lc, "extent_m", 800.0) or 800.0)
            if dist_to_site is not None and dist_to_site > extent:
                continue
            lanes = max(1, int(data.get("lanes_count", 2)))
            remaining = max(1, lanes - lc.lanes_closed)
            return base_cap * remaining / lanes
    return base_cap


# ---------------------------------------------------------------------------
# Trip-generation rates
# ---------------------------------------------------------------------------

def _edge_street_name(data: dict[str, Any]) -> str:
    name = data.get("name")
    if isinstance(name, list):
        name = name[0] if name else ""
    name = str(name or "").strip()
    # GraphML stores multi-name edges as "['A', 'B']"; keep the first name.
    if name.startswith("[") and name.endswith("]"):
        name = name.strip("[]").split(",")[0].strip().strip("'\"")
    return name


def _biggest_increases(
    G: nx.MultiDiGraph,
    baseline_vols: dict[tuple[Any, Any, int], float],
    scenario_vols: dict[tuple[Any, Any, int], float],
    baseline_caps: dict[tuple[Any, Any, int], float],
    scenario_caps: dict[tuple[Any, Any, int], float],
    top_n: int = 5,
    *,
    site_lat: float | None = None,
    site_lon: float | None = None,
    radius_m: float = IMPACT_RADIUS_M,
) -> list[dict[str, Any]]:
    """Named streets near the site whose V/C rises most from baseline to scenario.

    Limited to *radius_m* around the site: far-away changes are mostly
    route-switching noise between near-equal paths, not real impact.

    For each street, take its single segment with the largest V/C increase,
    so the list points at where the change is actually felt.
    """
    best: dict[str, dict[str, Any]] = {}
    for u, v, k, data in G.edges(keys=True, data=True):
        name = _edge_street_name(data)
        if not name or name.lower() == "none":
            continue
        cap_b = baseline_caps.get((u, v, k), float(data.get("capacity", 3600.0)))
        cap_s = scenario_caps.get((u, v, k), float(data.get("capacity", 3600.0)))
        vc_b = baseline_vols.get((u, v, k), 0.0) / cap_b if cap_b > 0 else 0.0
        vc_s = scenario_vols.get((u, v, k), 0.0) / cap_s if cap_s > 0 else 0.0
        delta = vc_s - vc_b
        if delta <= 0.005:
            continue
        mid_lat = (G.nodes[u]["y"] + G.nodes[v]["y"]) / 2
        mid_lon = (G.nodes[u]["x"] + G.nodes[v]["x"]) / 2
        if site_lat is not None and site_lon is not None and \
                _haversine_m(mid_lat, mid_lon, site_lat, site_lon) > radius_m:
            continue
        if name not in best or delta > best[name]["delta"]:
            best[name] = {
                "name": name,
                "lat": mid_lat,
                "lon": mid_lon,
                "base_vc": round(vc_b, 3),
                "scen_vc": round(vc_s, 3),
                "delta": round(delta, 3),
            }
    return sorted(best.values(), key=lambda r: r["delta"], reverse=True)[:top_n]


def _removed_daily_trips(scenario: Any) -> int:
    """Daily trips generated today by the building that will be demolished."""
    sqft = float(getattr(scenario, "demolished_office_sqft", 0.0) or 0.0)
    occ = float(getattr(scenario, "existing_occupancy", 1.0))
    return int(sqft / 1000.0 * TRIPS_PER_1000_SQFT_OFFICE_DAY * occ)


def _site_od(
    site_nodes: list[Any],
    other_nodes: list[Any],
    peak_trips: float,
) -> dict[tuple[Any, Any], float]:
    """Spread *peak_trips* evenly, half leaving the site and half arriving."""
    pairs = [(o, d) for o in site_nodes for d in other_nodes if o != d]
    if peak_trips <= 0 or not pairs:
        return {}
    w = peak_trips / (2 * len(pairs))
    od: dict[tuple[Any, Any], float] = defaultdict(float)
    for s_node, other in pairs:
        od[(s_node, other)] += w   # outbound
        od[(other, s_node)] += w   # inbound
    return dict(od)


def _merge_od(
    a: dict[tuple[Any, Any], float],
    b: dict[tuple[Any, Any], float],
) -> dict[tuple[Any, Any], float]:
    merged = dict(a)
    for pair, w in b.items():
        merged[pair] = merged.get(pair, 0.0) + w
    return merged


def _freeway_nodes(G: nx.MultiDiGraph, site_lat: float, site_lon: float) -> list[Any]:
    """Freeway/ramp nodes away from the site: where haul trucks enter and leave."""
    found = set()
    for u, v, data in G.edges(data=True):
        hw = data.get("highway")
        hw = hw[0] if isinstance(hw, list) else hw
        if hw in ("motorway", "motorway_link"):
            found.update((u, v))
    # Haul trucks take the NEAREST ramps (e.g. I-375 / the Lodge downtown),
    # skipping any too close to count as "leaving the site".
    dist = {n: _haversine_m(G.nodes[n]["y"], G.nodes[n]["x"], site_lat, site_lon)
            for n in found}
    near = sorted((n for n in found if dist[n] >= TRUCK_MIN_DISTANCE_M),
                  key=lambda n: (dist[n], str(n)))
    return near[:TRUCK_FREEWAY_NODES]


def _added_daily_trips(scenario: Any) -> int:
    """ITE planning-level trip-generation rates (approximate)."""
    apt = scenario.housing_units * TRIPS_PER_APARTMENT_UNIT_DAY
    grocery = (scenario.retail_sqft / 1000.0) * TRIPS_PER_1000_SQFT_GROCERY_DAY
    office = (float(getattr(scenario, "office_sqft", 0.0) or 0.0) / 1000.0
              * TRIPS_PER_1000_SQFT_OFFICE_DAY)
    return int(apt + grocery + office)


# ---------------------------------------------------------------------------
# Edge metrics
# ---------------------------------------------------------------------------

def _edge_metrics(
    G: nx.MultiDiGraph,
    volumes: dict[tuple[Any, Any, int], float],
    edge_cap: dict[tuple[Any, Any, int], float],
) -> list[EdgeMetric]:
    """Build edge-level V/C metrics using the assignment's effective capacity."""
    metrics: list[EdgeMetric] = []
    for u, v, k, data in G.edges(keys=True, data=True):
        cap = edge_cap.get((u, v, k), float(data.get("capacity", 3600.0)))
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

def edge_coords(G: nx.MultiDiGraph, u: Any, v: Any, data: dict[str, Any]) -> list[tuple[float, float]]:
    """Road shape from u to v as (lon, lat) points, following its real curve.

    Uses the OSM geometry when present (oriented to start at u); otherwise the
    straight line between the two intersections.
    """
    start = (float(G.nodes[u]["x"]), float(G.nodes[u]["y"]))
    end = (float(G.nodes[v]["x"]), float(G.nodes[v]["y"]))
    geom = data.get("geometry")
    coords = list(getattr(geom, "coords", []) or [])
    if len(coords) < 2:
        return [start, end]
    coords = [(float(x), float(y)) for x, y, *_ in coords]
    d_first = (coords[0][0] - start[0]) ** 2 + (coords[0][1] - start[1]) ** 2
    d_last = (coords[-1][0] - start[0]) ** 2 + (coords[-1][1] - start[1]) ** 2
    if d_last < d_first:
        coords.reverse()
    return coords


def _edge_interior_points(
    G: nx.MultiDiGraph, u: Any, v: Any, data: dict[str, Any],
) -> list[tuple[tuple[float, float], float]]:
    """Interior shape points of edge u→v with their 0–1 distance fraction."""
    pts = edge_coords(G, u, v, data)
    if len(pts) <= 2:
        return []
    seg = [_haversine_m(a[1], a[0], b[1], b[0]) for a, b in zip(pts, pts[1:])]
    total = sum(seg) or 1.0
    out, run, since_last = [], 0.0, 0.0
    for i, (p, d) in enumerate(zip(pts[1:-1], seg)):
        run += d
        since_last += d
        remaining = total - run
        # Thin out shape points: keep one every ~TRAJ_POINT_SPACING_M, and
        # never one right next to the end intersection.
        if since_last >= TRAJ_POINT_SPACING_M and remaining >= TRAJ_POINT_SPACING_M / 2:
            out.append(((round(p[0], 5), round(p[1], 5)), run / total))
            since_last = 0.0
    return out


def _congested_times(
    G: nx.MultiDiGraph,
    volumes: dict[tuple[Any, Any, int], float],
    caps: dict[tuple[Any, Any, int], float],
) -> dict[tuple[Any, Any, int], float]:
    """BPR travel time per edge at its final assigned volume (seconds)."""
    times = {}
    for u, v, k, data in G.edges(keys=True, data=True):
        t0 = float(data.get("travel_time", 30.0))
        cap = caps.get((u, v, k), float(data.get("capacity", 3600.0)))
        times[(u, v, k)] = bpr_travel_time(t0, volumes.get((u, v, k), 0.0), cap)
    return times


def _build_trajectories(
    paths: list[list[Any]],
    G: nx.MultiDiGraph,
    sample_size: int,
    edge_times: dict[tuple[Any, Any, int], float] | None = None,
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
                lon=round(float(nd.get("x", 0.0)), 5),
                lat=round(float(nd.get("y", 0.0)), 5),
                timestamp=round(t, 1),
            ))
            if i < len(path) - 1:
                nxt = path[i + 1]
                edge_data = G.get_edge_data(node, nxt)
                if edge_data:
                    # Vehicles move at CONGESTED speed (BPR time at the assigned
                    # volume), so they visibly slow down where roads are jammed.
                    def _time(item):
                        k, d = item
                        if edge_times is not None and (node, nxt, k) in edge_times:
                            return edge_times[(node, nxt, k)]
                        return float(d.get("travel_time", 30.0))
                    best_k, best = min(edge_data.items(), key=_time)
                    best_t = _time((best_k, best))
                    t_free = float(best.get("travel_time", 30.0)) or 1.0
                    slow = round(max(1.0, best_t / t_free), 2)
                    waypoints[-1].slow = slow          # segment leaving this node
                    # Follow the road's real curve: add its interior shape
                    # points, timed by distance along the edge.
                    for (lon, lat), frac in _edge_interior_points(G, node, nxt, best):
                        waypoints.append(Waypoint(
                            lon=lon, lat=lat, timestamp=round(t + best_t * frac, 1),
                            slow=slow,
                        ))
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
    baseline_caps: dict[tuple[Any, Any, int], float],
    scenario_caps: dict[tuple[Any, Any, int], float],
    added_daily: int,
) -> SimulationMetrics:
    """
    Compute total vehicle-hours of delay (VHD) for baseline and scenario,
    report percentage change, and identify the worst intersections.

    VHD = Σ_edges  volume × (congested_tt − free_flow_tt)

    Uses the assignment's effective capacity (closure-adjusted for scenario)
    so the lane-closure effect is reflected in the metrics.
    """

    def total_vhd(
        vols: dict[tuple[Any, Any, int], float],
        caps: dict[tuple[Any, Any, int], float],
    ) -> float:
        vhd = 0.0
        for (u, v, k), vol in vols.items():
            if vol <= 0:
                continue
            t0 = float(G[u][v][k].get("travel_time", 30.0))
            cap = caps.get((u, v, k), float(G[u][v][k].get("capacity", 3600.0)))
            tt_cong = bpr_travel_time(t0, vol, cap)
            vhd += vol * max(0.0, tt_cong - t0) / 3600.0  # convert s→h
        return vhd

    base_vhd = total_vhd(baseline_vols, baseline_caps)
    scen_vhd = total_vhd(scenario_vols, scenario_caps)
    vhd_pct = ((scen_vhd - base_vhd) / base_vhd * 100.0) if base_vhd > 0 else 0.0

    # Worst intersections by scenario V/C (using scenario capacity)
    vc_by_node: dict[Any, list[float]] = defaultdict(list)
    for (u, v, k), vol in scenario_vols.items():
        cap = scenario_caps.get((u, v, k), float(G[u][v][k].get("capacity", 3600.0)))
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
        travel_time_change_pct=round(vhd_pct, 2),
        description=(
            f"Development adds ~{added_daily:,} daily trips "
            f"({added_peak:,} in peak hour). "
            f"Total vehicle-hours of delay changes by {vhd_pct:+.1f}% "
            f"(baseline {base_vhd:.1f} vh → scenario {scen_vhd:.1f} vh)."
        ),
    )
