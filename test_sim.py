#!/usr/bin/env python3
"""
test_sim.py — Integration test for the Detroit I-Grid simulation pipeline.

Runs the full baseline + scenario simulation against the real
data/detroit.graphml and prints:
  • Wall-clock time (must be < 15 s)
  • Baseline max V/C
  • Scenario max V/C
  • Number of edges whose capacity is reduced by the lane closure
  • Top-5 loaded links with road names

Usage
-----
    python test_sim.py                   # run from Detroit-I_Grid/
    python test_sim.py --verbose         # also dump per-edge top-10 V/C
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Make sure the project root is on sys.path regardless of cwd
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from backend.network import get_graph, GRAPHML_PATH
from backend.sim import (
    _normalize_road_name,
    _apply_closures,
    _build_od_matrix,
    _pick_zones,
    bpr_travel_time,
    run_simulation,
    N_ZONES,
    TRIPS_PER_ZONE_PAIR,
    DEV_RADIUS_M,
    DEV_EXTRA_ORIGINS,
)
from backend.models import LaneClosure, Scenario


def _banner(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


def main(verbose: bool = False) -> int:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # ------------------------------------------------------------------ graph
    _banner("1. Graph")
    if not Path(GRAPHML_PATH).exists():
        print(f"ERROR: {GRAPHML_PATH!r} not found.  "
              "Run build_network.py first.", file=sys.stderr)
        return 1

    G = get_graph()
    print(f"  Nodes: {len(G.nodes):,}   Edges: {len(G.edges):,}")

    n_nodes = len(G.nodes)
    if n_nodes < 1000:
        print("WARNING: graph has fewer than 1,000 nodes — "
              "may be the synthetic fallback, not real OSM data.")

    # ---------------------------------------------------------------- scenario
    # Woodward & Mack Ave, Detroit (42.355, -83.065)
    scenario = Scenario(
        location_lat=42.355,
        location_lon=-83.065,
        description="600-unit apartment + grocery at Woodward & Mack",
        housing_units=600,
        retail_sqft=20_000,
        opening_year=2028,
        lane_closures=[
            LaneClosure(
                road_name="Woodward Ave",   # deliberately uses abbreviation
                start_date="2027-03-01",
                end_date="2027-09-01",
                lanes_closed=1,
            )
        ],
    )

    # ------------------------------------------------ lane-closure edge count
    _banner("2. Lane-closure matching (Woodward Ave ↔ Woodward Avenue)")
    lc = scenario.lane_closures[0]
    lc_norm = _normalize_road_name(lc.road_name)
    print(f"  Closure normalised:  {lc_norm!r}")

    affected_edges = []
    for u, v, k, data in G.edges(keys=True, data=True):
        cap_before = float(data.get("capacity", 3600.0))
        cap_after = _apply_closures(data, cap_before, [lc])
        if cap_after < cap_before:
            affected_edges.append((u, v, k, data, cap_before, cap_after))

    print(f"  Edges affected by closure: {len(affected_edges)}")
    if affected_edges:
        # Sample first 3
        for u, v, k, data, cb, ca in affected_edges[:3]:
            name = data.get("name", "?")
            lanes = data.get("lanes_count", "?")
            print(f"    ({u}→{v}) name={name!r}  "
                  f"lanes_count={lanes}  "
                  f"cap {cb:.0f}→{ca:.0f} veh/h")
    else:
        print("  WARN: no edges matched — lane-closure matching may be broken")

    # ------------------------------------------------------------- simulation
    _banner("3. Simulation")
    print(f"  OD zones:            {N_ZONES}")
    print(f"  Trips per zone-pair: {TRIPS_PER_ZONE_PAIR}")
    print(f"  Dev radius:          {DEV_RADIUS_M:.0f} m")
    print(f"  Dev extra origins:   {DEV_EXTRA_ORIGINS}")

    t_start = time.perf_counter()
    result = run_simulation("test-real", scenario, G)
    elapsed = time.perf_counter() - t_start

    # ---------------------------------------------------------------- results
    _banner("4. Results")
    max_base_vc = max((e.vc_ratio for e in result.baseline_edges), default=0.0)
    max_scen_vc = max((e.vc_ratio for e in result.scenario_edges), default=0.0)

    vcs_base = sorted(e.vc_ratio for e in result.baseline_edges)
    vcs_scen = sorted(e.vc_ratio for e in result.scenario_edges)
    n = len(vcs_base)
    p90_base = vcs_base[int(n * 0.90)]
    p90_scen = vcs_scen[int(n * 0.90)]

    over1_base = sum(1 for v in vcs_base if v > 1.0)
    over1_scen = sum(1 for v in vcs_scen if v > 1.0)

    print(f"  Wall-clock time:     {elapsed:.2f} s  {'✓ < 15 s' if elapsed < 15 else '✗ EXCEEDS 15 s'}")
    print(f"  Baseline max V/C:    {max_base_vc:.3f}  (p90={p90_base:.3f})")
    print(f"  Scenario max V/C:    {max_scen_vc:.3f}  (p90={p90_scen:.3f})")
    print(f"  Edges V/C > 1.0:     baseline={over1_base}  scenario={over1_scen}")
    print(f"  Travel-time Δ:       {result.metrics.travel_time_change_pct:+.2f}%")
    print(f"  Added daily trips:   {result.metrics.added_daily_trips:,}")
    print(f"  Trajectories (base): {len(result.baseline_trajectories)}")
    print(f"  Trajectories (scen): {len(result.scenario_trajectories)}")

    # Checklist
    _banner("5. Checklist")
    checks = [
        ("Elapsed < 15 s",         elapsed < 15),
        ("Baseline max V/C ≥ 0.5", max_base_vc >= 0.50),
        # A real PM peak has some congested links; gridlock would be >5 %.
        ("Baseline edges over capacity < 5 %", over1_base < 0.05 * len(vcs_base)),
        ("Scenario max V/C > baseline", max_scen_vc > max_base_vc),
        ("Scenario adds over-capacity edges", over1_scen > over1_base),
        ("Travel-time Δ positive", result.metrics.travel_time_change_pct > 0),
        ("Closure affected ≥ 1 edge",  len(affected_edges) >= 1),
        ("2000 baseline trajectories", len(result.baseline_trajectories) == 2000),
        ("2000 scenario trajectories", len(result.scenario_trajectories) == 2000),
    ]
    all_pass = True
    for label, ok in checks:
        mark = "✓" if ok else "✗"
        print(f"  {mark}  {label}")
        if not ok:
            all_pass = False

    # Optional verbose dump
    if verbose:
        _banner("6. Top-10 scenario V/C (edge detail)")
        top10 = sorted(result.scenario_edges, key=lambda e: e.vc_ratio, reverse=True)[:10]
        for em in top10:
            u, v, k = em.edge_id.split("-", 2)
            try:
                name = G[int(u)][int(v)][int(k)].get("name", "?")
            except (KeyError, ValueError):
                name = "?"
            print(f"  {em.edge_id}  V/C={em.vc_ratio:.3f}  "
                  f"V={em.volume:.0f}  C={em.capacity:.0f}  name={name!r}")

        _banner("7. Top-5 suffix-normalisation smoke test")
        pairs = [
            ("Woodward Avenue",   "Woodward Ave"),
            ("WOODWARD AVE.",     "woodward avenue"),
            ("Gratiot Street",    "Gratiot St"),
            ("John C Lodge Freeway", "Lodge Fwy"),
            ("Grand Boulevard",   "Grand Blvd"),
        ]
        for a, b in pairs:
            na, nb = _normalize_road_name(a), _normalize_road_name(b)
            eq = "✓" if na == nb else "✗"
            print(f"  {eq}  {a!r:30s}  ==  {b!r}")

    print()
    return 0 if all_pass else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Detroit I-Grid simulation test")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    sys.exit(main(verbose=args.verbose))
