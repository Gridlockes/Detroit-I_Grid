#!/usr/bin/env python3
"""
Write orchestrate/igrid_tools.openapi.json: the three agent-facing endpoints
of the I-Grid API, pointed at a public base URL, for import into watsonx
Orchestrate as tools.

    python orchestrate/build_openapi.py https://<your-public-api-url>

Tools created on import (named after operationId):
    simulate_project, list_known_locations, estimate_utilities
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from backend.main import app  # noqa: E402

KEEP = {("/agent/simulate", "post"), ("/agent/locations", "get"), ("/utilities", "post")}


def main() -> None:
    if len(sys.argv) != 2 or not sys.argv[1].startswith("https://"):
        sys.exit("usage: build_openapi.py https://<public-api-url>   (Orchestrate needs HTTPS)")
    base = sys.argv[1].rstrip("/")

    spec = app.openapi()
    spec["paths"] = {
        path: {m: op for m, op in ops.items() if (path, m) in KEEP}
        for path, ops in spec["paths"].items()
        if any((path, m) in KEEP for m in ops)
    }
    spec["servers"] = [{"url": base, "description": "Detroit I-Grid API"}]
    spec["info"]["title"] = "Detroit I-Grid tools"
    spec["info"]["description"] = (
        "Traffic-impact simulation for construction, demolition and development "
        "projects in Downtown/Midtown Detroit."
    )

    _prune_schemas(spec)
    spec = _to_openapi_30(spec)

    out = pathlib.Path(__file__).with_name("igrid_tools.openapi.json")
    out.write_text(json.dumps(spec, indent=2))
    ops = [op["operationId"] for ops in spec["paths"].values() for op in ops.values()]
    print(f"wrote {out} with tools: {', '.join(ops)} -> {base}")


def _refs(node, found):
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.rsplit("/", 1)[-1])
        for v in node.values():
            _refs(v, found)
    elif isinstance(node, list):
        for v in node:
            _refs(v, found)


def _prune_schemas(spec: dict) -> None:
    """Keep only component schemas reachable from the kept paths."""
    schemas = spec.get("components", {}).get("schemas", {})
    keep, frontier = set(), set()
    _refs(spec["paths"], frontier)
    while frontier:
        name = frontier.pop()
        if name in keep or name not in schemas:
            continue
        keep.add(name)
        _refs(schemas[name], frontier)
    spec["components"]["schemas"] = {k: v for k, v in schemas.items() if k in keep}


def _to_openapi_30(node):
    """OpenAPI 3.1 → 3.0.3: anyOf[X, null] becomes X + nullable: true."""
    if isinstance(node, dict):
        node = {k: _to_openapi_30(v) for k, v in node.items()}
        if node.get("openapi", "").startswith("3.1"):
            node["openapi"] = "3.0.3"
        any_of = node.get("anyOf")
        if isinstance(any_of, list) and any(o == {"type": "null"} for o in any_of):
            rest = [o for o in any_of if o != {"type": "null"}]
            node.pop("anyOf")
            if len(rest) == 1:
                node.update(rest[0])
            else:
                node["anyOf"] = rest
            node["nullable"] = True
        return node
    if isinstance(node, list):
        return [_to_openapi_30(v) for v in node]
    return node


if __name__ == "__main__":
    main()
