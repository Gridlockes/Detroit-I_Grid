#!/usr/bin/env python3
"""
build_network.py — Download and cache the Detroit OSM drive network.

Run once before starting the server (or in a Dockerfile RUN step):

    python build_network.py

If the Overpass API is unreachable (firewall / CI environment), set:

    IGRID_SYNTHETIC_GRAPH=1 python build_network.py

That generates a placeholder 20×25 grid graph at data/detroit.graphml
so the rest of the app can run end-to-end for local development.
The cache file is committed to the repo; delete it to force a re-download.
"""
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from backend.network import get_graph

G = get_graph()
n, e = len(G.nodes), len(G.edges)
print(f"\nGraph ready: {n} nodes, {e} edges", flush=True)
sample = next(iter(G.edges(data=True)))[2]
print(
    f"Sample edge — "
    f"speed_kph={sample['speed_kph']}, "
    f"travel_time={sample['travel_time']:.2f}s, "
    f"lanes_count={sample['lanes_count']}, "
    f"capacity={sample['capacity']} veh/h",
    flush=True,
)
