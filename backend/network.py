"""
network.py — OSM road-network loader and cache.

Loads the drive network for Downtown + Midtown + New Center Detroit,
attaches speed, travel-time, and capacity attributes, and caches the
result to data/detroit.graphml so it is not re-downloaded on every run.

Usage
-----
    from backend.network import get_graph
    G = get_graph()          # returns a networkx MultiDiGraph

If the Overpass API is unreachable, set the env var IGRID_SYNTHETIC_GRAPH=1
to build a placeholder grid graph for local development.  The real GraphML
cache (data/detroit.graphml) always takes precedence when it exists.
"""

from __future__ import annotations

import logging
import math
import os
import pathlib

import networkx as nx
import osmnx as ox

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# osmnx settings
# ---------------------------------------------------------------------------
# osmnx appends "/interpreter" to overpass_url, so set the *base* URL only.
# Override with OVERPASS_ENDPOINT env var (base URL, no trailing /interpreter).
_OVERPASS_BASE = os.environ.get(
    "OVERPASS_ENDPOINT",
    "https://overpass-api.de/api",   # default; switch to kumi.systems if needed
)
ox.settings.overpass_url = _OVERPASS_BASE
# Our bbox is ~12 km² — set the limit high enough to avoid auto-subdivision.
ox.settings.max_query_area_size = 50_000_000_000
ox.settings.timeout = 180
ox.settings.overpass_rate_limit = False

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
# Drive-network extract cut from the Geofabrik Michigan file with osmium
# (see README "Rebuilding the road network"). Used when Overpass is down.
OSM_XML_PATH = "data/detroit-drive.osm"


# Loaded graphs, keyed by GraphML path. Loading from disk takes seconds, so
# every simulation reuses the same in-memory graph (it is never mutated).
_GRAPH_CACHE: dict[str, nx.MultiDiGraph] = {}


def get_graph(cache_path: str = GRAPHML_PATH) -> nx.MultiDiGraph:
    """
    Return the Detroit drive network as a networkx MultiDiGraph.

    Priority:
      1. Load from *cache_path* (fastest; preferred for production).
      2. Download from OSM via Overpass API, enrich, and save to *cache_path*.
      3. If IGRID_SYNTHETIC_GRAPH=1 is set (or Overpass is unreachable and
         IGRID_SYNTHETIC_GRAPH=1), build a placeholder grid graph sized to
         the Detroit bounding box for local development.

    Attributes added to every edge:
      - speed_kph      (float) — free-flow speed in km/h
      - travel_time    (float) — free-flow travel time in seconds
      - lanes_count    (int)   — number of lanes (parsed/defaulted)
      - capacity       (float) — link capacity in veh/h
    """
    # --- 0. In-memory cache (the graph is read-only once loaded) ---
    if cache_path in _GRAPH_CACHE:
        return _GRAPH_CACHE[cache_path]

    # --- 1. Cache hit ---
    if os.path.exists(cache_path):
        logger.info("Loading cached graph from %s", cache_path)
        G = ox.load_graphml(cache_path)
        _coerce_numeric(G)
        _GRAPH_CACHE[cache_path] = G
        return G

    # --- 1b. Local OSM extract (Geofabrik + osmium; no Overpass needed) ---
    if os.path.exists(OSM_XML_PATH):
        logger.info("Building graph from local OSM extract %s", OSM_XML_PATH)
        G = ox.graph_from_xml(OSM_XML_PATH, bidirectional=False, retain_all=False)
        G = ox.truncate.largest_component(G, strongly=True)
        return _enrich_and_save(G, cache_path)

    # --- 2. Synthetic fallback (dev mode) ---
    if os.environ.get("IGRID_SYNTHETIC_GRAPH", "0") == "1":
        logger.warning(
            "IGRID_SYNTHETIC_GRAPH=1 — building synthetic grid graph "
            "(real OSM data will be used once detroit.graphml is cached)"
        )
        G = _build_synthetic_graph()
        pathlib.Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        ox.save_graphml(G, cache_path)
        logger.info("Synthetic graph saved to %s  (%d nodes, %d edges)",
                    cache_path, len(G.nodes), len(G.edges))
        return G

    # --- 3. Download from OSM ---
    logger.info("Downloading OSM drive network for Detroit bounding box …")
    # osmnx 2.x bbox order: (left, bottom, right, top) = (west, south, east, north)
    G = ox.graph_from_bbox(
        bbox=(BBOX_WEST, BBOX_SOUTH, BBOX_EAST, BBOX_NORTH),
        network_type="drive",
        retain_all=False,
        truncate_by_edge=True,
    )
    return _enrich_and_save(G, cache_path)


def _enrich_and_save(G: nx.MultiDiGraph, cache_path: str) -> nx.MultiDiGraph:
    """Add speed, travel time, lanes and capacity, then cache to GraphML."""
    # Add speed and travel-time using osmnx's built-in imputer
    G = ox.add_edge_speeds(G, fallback=DEFAULT_SPEED_KPH)
    G = ox.add_edge_travel_times(G)

    # Add lanes count and capacity
    _add_lanes_count(G)
    _add_capacity(G)

    # Persist
    pathlib.Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
    ox.save_graphml(G, cache_path)
    logger.info("Graph saved to %s  (%d nodes, %d edges)",
                cache_path, len(G.nodes), len(G.edges))
    return G


# Default lanes per direction by OSM highway tag.
# OSM 'lanes' counts *total* lanes on two-way roads; we halve for per-direction
# capacity.  For one-way roads we use the full count.
_HIGHWAY_DEFAULT_LANES: dict[str, int] = {
    "motorway": 3,
    "motorway_link": 1,
    "trunk": 2,
    "trunk_link": 1,
    "primary": 2,
    "primary_link": 1,
    "secondary": 2,
    "secondary_link": 1,
    "tertiary": 1,
    "tertiary_link": 1,
    "unclassified": 1,
    "residential": 1,
    "living_street": 1,
    "service": 1,
}


def _parse_lanes_raw(raw: object) -> int | None:
    """Parse an OSM lanes tag (string, int, or list) to an int, or None if absent."""
    if raw is None:
        return None
    if isinstance(raw, list):
        try:
            return max(int(v) for v in raw if str(v).strip().isdigit())
        except (ValueError, TypeError):
            return None
    try:
        return int(str(raw).strip())
    except (ValueError, TypeError):
        return None


def _add_lanes_count(G: nx.MultiDiGraph) -> None:
    """
    Set 'lanes_count' = per-direction lane count on every edge.

    OSM 'lanes' is the *total* number of lanes across both directions on a
    two-way road.  We halve it (rounding up) for each directed edge.
    For one-way edges the full lane count applies.

    When 'lanes' is absent, fall back to the highway-type default table;
    final clamp to ≥ 1.
    """
    for _, _, data in G.edges(data=True):
        hw = data.get("highway", "residential")
        if isinstance(hw, list):
            hw = hw[0]
        hw = str(hw)

        oneway = data.get("oneway", False)
        if isinstance(oneway, str):
            oneway = oneway.lower() in ("yes", "true", "1", "-1")

        raw = _parse_lanes_raw(data.get("lanes"))
        if raw is not None:
            # OSM lanes = total; halve for two-way roads
            if oneway:
                val = raw
            else:
                val = math.ceil(raw / 2)
        else:
            val = _HIGHWAY_DEFAULT_LANES.get(hw, LANES_DEFAULT)

        data["lanes_count"] = max(1, val)


def _add_capacity(G: nx.MultiDiGraph) -> None:
    """
    Add a 'capacity' edge attribute (veh/h) = lanes_count × CAPACITY_PER_LANE.
    Must be called after _add_lanes_count.
    """
    for _, _, data in G.edges(data=True):
        data["capacity"] = float(data["lanes_count"] * CAPACITY_PER_LANE)


def _coerce_numeric(G: nx.MultiDiGraph) -> None:
    """
    GraphML serialises all attributes as strings.  Convert the numeric
    attributes we care about back to float/int after loading from cache.
    """
    for _, _, data in G.edges(data=True):
        for attr, cast in (
            ("speed_kph", float),
            ("travel_time", float),
            ("lanes_count", int),
            ("capacity", float),
        ):
            if attr in data:
                try:
                    data[attr] = cast(data[attr])
                except (ValueError, TypeError):
                    pass


def _build_synthetic_graph(
    rows: int = 20,
    cols: int = 25,
) -> nx.MultiDiGraph:
    """
    Build a regular grid graph covering the Detroit bounding box.

    Used as a local-development fallback when Overpass is unreachable.
    Nodes are placed at regular lat/lon intervals; edges have realistic
    speed, travel-time, lanes_count, and capacity attributes.

    The graph is a bidirectional grid (each grid edge becomes two
    directed edges) so it behaves like a real road network.
    """
    lat_step = (BBOX_NORTH - BBOX_SOUTH) / (rows - 1)
    lon_step = (BBOX_EAST - BBOX_WEST) / (cols - 1)

    # Approximate metres per degree for travel-time calculation
    meters_per_lat = 111_000.0
    meters_per_lon = 111_000.0 * math.cos(math.radians((BBOX_NORTH + BBOX_SOUTH) / 2))

    G = nx.MultiDiGraph(
        crs="epsg:4326",
        simplified=True,
    )

    # Add nodes
    node_id = 0
    node_map: dict[tuple[int, int], int] = {}
    for r in range(rows):
        for c in range(cols):
            lat = BBOX_SOUTH + r * lat_step
            lon = BBOX_WEST + c * lon_step
            G.add_node(node_id, y=lat, x=lon, osmid=node_id)
            node_map[(r, c)] = node_id
            node_id += 1

    def _add_edge(u: int, v: int, length_m: float, name: str) -> None:
        attrs = {
            "osmid": u * 10000 + v,
            "name": name,
            "highway": "primary",
            "oneway": False,
            "length": length_m,
            "speed_kph": DEFAULT_SPEED_KPH,
            "travel_time": (length_m / 1000.0) / DEFAULT_SPEED_KPH * 3600.0,
            "lanes_count": LANES_DEFAULT,
            "capacity": float(LANES_DEFAULT * CAPACITY_PER_LANE),
        }
        G.add_edge(u, v, **attrs)
        G.add_edge(v, u, **attrs)   # bidirectional

    # Add horizontal (east-west) edges
    for r in range(rows):
        for c in range(cols - 1):
            u = node_map[(r, c)]
            v = node_map[(r, c + 1)]
            length_m = lon_step * meters_per_lon
            _add_edge(u, v, length_m, f"E-W street row {r}")

    # Add vertical (north-south) edges
    for r in range(rows - 1):
        for c in range(cols):
            u = node_map[(r, c)]
            v = node_map[(r + 1, c)]
            length_m = lat_step * meters_per_lat
            _add_edge(u, v, length_m, f"N-S street col {c}")

    logger.info(
        "Synthetic grid graph: %d×%d = %d nodes, %d edges",
        rows, cols, len(G.nodes), len(G.edges),
    )
    return G
