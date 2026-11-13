"""
locations.py — turn a plain-English place into a point on the road network.

Agents describe sites the way people do ("Woodward & Mack", "the RenCen"),
so the simulation needs coordinates without asking the model to guess them.

Resolution order:
  1. Known landmarks (keyword match), e.g. "Renaissance Center", "I-375".
  2. An intersection of two named streets, e.g. "Woodward & Mack",
     "Cass Ave and Temple St", "corner of Michigan Avenue at 14th Street":
     the network node where edges of both streets meet (or, failing that,
     the closest pair of nodes on the two streets).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

import networkx as nx

# Landmark coordinates (WGS-84). Keywords are matched case-insensitively
# against the location text; the first landmark with a matching keyword wins.
LANDMARKS: list[dict[str, Any]] = [
    {"name": "Renaissance Center", "lat": 42.3293, "lon": -83.0398,
     "keywords": ["renaissance center", "rencen", "ren cen", "gm headquarters", "gm hq"]},
    {"name": "I-375 (between Jefferson Ave and Gratiot Ave)", "lat": 42.3375, "lon": -83.0412,
     "keywords": ["i-375", "i 375", "i375", "chrysler freeway"]},
    {"name": "Hudson's site (Woodward Ave & Grand River Ave)", "lat": 42.3339, "lon": -83.0470,
     "keywords": ["hudson"]},
    {"name": "Michigan Central (Corktown)", "lat": 42.3289, "lon": -83.0776,
     "keywords": ["michigan central", "train station", "corktown"]},
    {"name": "District Detroit (Cass Ave near Temple St)", "lat": 42.3437, "lon": -83.0601,
     "keywords": ["district detroit", "little caesars arena", "lca", "temple"]},
    {"name": "Campus Martius", "lat": 42.3316, "lon": -83.0466,
     "keywords": ["campus martius"]},
    {"name": "Comerica Park / Ford Field", "lat": 42.3390, "lon": -83.0485,
     "keywords": ["comerica park", "ford field"]},
    {"name": "Wayne State University", "lat": 42.3570, "lon": -83.0690,
     "keywords": ["wayne state", "wsu"]},
    {"name": "Eastern Market", "lat": 42.3470, "lon": -83.0410,
     "keywords": ["eastern market"]},
    {"name": "New Center", "lat": 42.3690, "lon": -83.0760,
     "keywords": ["new center", "fisher building"]},
    {"name": "Greektown", "lat": 42.3355, "lon": -83.0410,
     "keywords": ["greektown"]},
]

_INTERSECTION_SPLIT = re.compile(r"\s*(?:&|\band\b|\bat\b|/|\+|\bx\b|\bcorner of\b)\s*", re.I)


@dataclass
class ResolvedLocation:
    name: str
    lat: float
    lon: float
    method: str            # "coordinates" | "landmark" | "intersection" | "street"


def _norm(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[.,;'\"()]", " ", s)
    s = re.sub(r"\b(east|west|north|south|e|w|n|s)\b", " ", s)
    s = re.sub(r"\b(avenue|ave|av|street|st|road|rd|boulevard|blvd|drive|dr|"
               r"highway|hwy|freeway|fwy|place|pl|parkway|pkwy|the|of)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _edge_names(data: dict[str, Any]) -> list[str]:
    raw = data.get("name")
    if isinstance(raw, list):
        return [str(x) for x in raw]
    raw = str(raw or "")
    if raw.startswith("[") and raw.endswith("]"):
        return [p.strip().strip("'\"") for p in raw.strip("[]").split(",")]
    return [raw] if raw and raw.lower() != "none" else []


def _street_nodes(G: nx.MultiDiGraph, street: str) -> tuple[set[Any], str]:
    """Nodes on edges whose normalised name matches *street* (word match)."""
    want = _norm(street)
    if not want:
        return set(), ""
    pattern = re.compile(rf"\b{re.escape(want)}\b")
    nodes: set[Any] = set()
    label = ""
    for u, v, data in G.edges(data=True):
        for name in _edge_names(data):
            if pattern.search(_norm(name)):
                nodes.update((u, v))
                label = label or name
    return nodes, label


def _dist_m(G: nx.MultiDiGraph, a: Any, b: Any) -> float:
    ya, xa, yb, xb = G.nodes[a]["y"], G.nodes[a]["x"], G.nodes[b]["y"], G.nodes[b]["x"]
    return math.hypot((ya - yb) * 111_000, (xa - xb) * 111_000 * math.cos(math.radians(ya)))


def resolve_location(
    G: nx.MultiDiGraph,
    text: str | None,
    lat: float | None = None,
    lon: float | None = None,
) -> ResolvedLocation:
    """Resolve coordinates, a landmark, or a street intersection.

    Raises ValueError with a helpful message when nothing matches.
    """
    if lat is not None and lon is not None:
        return ResolvedLocation(text or f"{lat:.5f}, {lon:.5f}", lat, lon, "coordinates")

    text = (text or "").strip()
    if not text:
        raise ValueError("No location given. Name a landmark or an intersection, "
                         "e.g. 'Woodward Ave & Mack Ave'.")

    low = text.lower()
    for lm in LANDMARKS:
        # Whole-word match, so "local" doesn't hit "lca" and "375 units" doesn't hit I-375.
        if any(re.search(rf"(?<![\w-]){re.escape(k)}(?![\w-])", low) for k in lm["keywords"]):
            return ResolvedLocation(lm["name"], lm["lat"], lm["lon"], "landmark")

    parts = [p for p in _INTERSECTION_SPLIT.split(text) if p and p.strip()]
    if len(parts) >= 2:
        (a_nodes, a_name), (b_nodes, b_name) = _street_nodes(G, parts[0]), _street_nodes(G, parts[1])
        if a_nodes and b_nodes:
            shared = a_nodes & b_nodes
            if shared:
                n = sorted(shared, key=str)[0]
            else:
                # Streets don't touch in the graph: take the closest pair.
                n, _m = min(((x, y) for x in a_nodes for y in b_nodes),
                            key=lambda pair: _dist_m(G, *pair))
            return ResolvedLocation(f"{a_name} & {b_name}",
                                    float(G.nodes[n]["y"]), float(G.nodes[n]["x"]),
                                    "intersection")
        missing = parts[0] if not a_nodes else parts[1]
        raise ValueError(f"Couldn't find a street called {missing!r} in the Detroit "
                         "network (Downtown, Midtown, New Center, Corktown).")

    nodes, name = _street_nodes(G, text)
    if nodes:
        # A single street: use its middle-most node.
        ys = sorted(nodes, key=lambda n: G.nodes[n]["y"])
        n = ys[len(ys) // 2]
        return ResolvedLocation(name, float(G.nodes[n]["y"]), float(G.nodes[n]["x"]), "street")

    known = ", ".join(lm["name"] for lm in LANDMARKS)
    raise ValueError(f"Couldn't locate {text!r}. Use an intersection such as "
                     f"'Woodward Ave & Mack Ave', or a landmark: {known}.")
