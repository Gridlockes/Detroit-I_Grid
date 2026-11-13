"""
network.py — OSM road-network loader and cache.

Loads the drive network for Downtown + Midtown + New Center Detroit,
attaches speed, travel-time, and capacity attributes, and caches the
result to data/detroit.graphml so it is not re-downloaded on every run.

Usage
-----
    from backend.network import get_graph
    G = get_graph()          # returns a networkx MultiDiGraph
"""

from __future__ import annotations

import networkx as nx

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Rough bounding box: Downtown + Midtown + New Center (~3×4 km)
BBOX_NORTH = 42.3900
BBOX_SOUTH = 42.3250
BBOX_EAST = -83.0300
BBOX_WEST = -83.0950

DEFAULT_SPEED_KPH = 48.0          # ~30 mph urban default
LANES_DEFAULT = 2
CAPACITY_PER_LANE = 1800          # vehicles per hour per lane

GRAPHML_PATH = "data/detroit.graphml"


def get_graph(cache_path: str = GRAPHML_PATH) -> nx.MultiDiGraph:
    """
    Return the Detroit drive network as a networkx MultiDiGraph.

    Loads from *cache_path* if the file exists, otherwise downloads from
    OpenStreetMap via osmnx, enriches the graph, and saves to *cache_path*.
    """
    # TODO (prompt 2): implement download, enrichment, and caching with osmnx
    raise NotImplementedError("network.py stub — implement in prompt 2")


def _add_capacity(G: nx.MultiDiGraph) -> None:
    """
    Add a 'capacity' edge attribute (veh/h) = lanes × CAPACITY_PER_LANE.
    Defaults to LANES_DEFAULT when the OSM 'lanes' tag is absent.
    """
    # TODO (prompt 2): iterate edges and set capacity attribute
    pass
