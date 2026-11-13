"""
routers/agent.py — compact endpoints for watsonx Orchestrate agents.

The map endpoints return ~15 MB of vehicle paths, far too much for an LLM.
These endpoints take a plain description of a project, run the simulation
synchronously, publish the result to the live map (/results/latest), and
return a short JSON summary the agent can explain to the user.

POST /agent/simulate   – run a construction / demolition / development scenario
GET  /agent/locations  – landmarks the location resolver knows by name
"""

from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..locations import LANDMARKS, resolve_location
from ..models import LaneClosure, Scenario, ScenarioRecord
from ..store import store

router = APIRouter(prefix="/agent", tags=["agent"])


class AgentLaneClosure(BaseModel):
    road_name: str = Field(..., description="Street to narrow, e.g. 'Woodward Ave', 'Jefferson Ave', 'I-375'")
    lanes_closed: int = Field(1, ge=1, le=4, description="Lanes closed in each direction")
    extent_m: float = Field(
        800.0, gt=0, le=5000,
        description="Only segments of this road within this many metres of the site are affected",
    )
    start_date: str = Field("", description="Optional ISO start date, e.g. '2027-03-01'")
    end_date: str = Field("", description="Optional ISO end date")


class AgentScenarioRequest(BaseModel):
    """A proposed project in Detroit, as an agent would describe it."""

    project_description: str = Field(..., description="One-sentence description of the project")
    location: str = Field(
        "",
        description=(
            "Where the project is: a landmark ('Renaissance Center', 'I-375', 'Michigan Central', "
            "\"Hudson's site\", 'District Detroit') or an intersection ('Woodward Ave & Mack Ave')"
        ),
    )
    latitude: float | None = Field(None, description="Optional site latitude; overrides location")
    longitude: float | None = Field(None, description="Optional site longitude; overrides location")
    housing_units: int = Field(0, ge=0, description="New residential units")
    retail_sqft: float = Field(0, ge=0, description="New retail floor area (sq ft)")
    office_sqft: float = Field(0, ge=0, description="New office floor area (sq ft)")
    demolished_office_sqft: float = Field(0, ge=0, description="Existing office floor area being demolished (sq ft)")
    existing_occupancy_percent: float = Field(
        100, ge=0, le=100, description="How full the demolished building is today, 0-100 %",
    )
    truck_trips_per_day: int = Field(0, ge=0, description="Construction / demolition haul-truck trips per day")
    opening_year: int = Field(2028, description="Year the project opens or the work happens")
    lane_closures: list[AgentLaneClosure] = Field(default_factory=list, description="Temporary lane closures")


class StreetChange(BaseModel):
    street: str
    load_before_pct: int = Field(..., description="Share of road capacity used before the project")
    load_after_pct: int = Field(..., description="Share of road capacity used with the project")


class ClosureApplied(BaseModel):
    road_name: str
    segments_closed: int = Field(..., description="Road segments whose capacity was reduced (0 = name not found near site)")


class AgentImpact(BaseModel):
    scenario_id: str
    location: str = Field(..., description="Where the simulation placed the project")
    latitude: float
    longitude: float
    summary: str = Field(..., description="Plain-English summary of the result")
    delay_change_pct: float = Field(..., description="Change in total vehicle-hours of delay, Downtown/Midtown, PM peak")
    added_daily_trips: int
    removed_daily_trips: int
    truck_trips_per_day: int
    streets_most_affected: list[StreetChange] = Field(..., description="Up to 5 named streets near the site with the largest increase")
    lane_closures_applied: list[ClosureApplied]
    caveat: str
    map_url_hint: str


@router.post(
    "/simulate",
    response_model=AgentImpact,
    operation_id="simulate_project",
    summary="Simulate the traffic impact of a Detroit construction, demolition or development project",
    description=(
        "Runs the Detroit I-Grid traffic simulation (real OpenStreetMap road network, PM peak hour) "
        "for a proposed project, shows it on the live map, and returns a short impact summary: "
        "change in delay, trips added or removed, haul trucks, lane closures applied, and the "
        "streets near the site whose load rises most. Takes about 15-30 seconds."
    ),
)
def simulate_project(req: AgentScenarioRequest) -> AgentImpact:
    from ..network import get_graph
    from ..sim import run_simulation, _edge_street_name

    G = get_graph()
    try:
        loc = resolve_location(G, req.location, req.latitude, req.longitude)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    scenario = Scenario(
        location_lat=loc.lat,
        location_lon=loc.lon,
        description=req.project_description,
        housing_units=req.housing_units,
        retail_sqft=req.retail_sqft,
        office_sqft=req.office_sqft,
        opening_year=req.opening_year,
        demolished_office_sqft=req.demolished_office_sqft,
        existing_occupancy=req.existing_occupancy_percent / 100.0,
        demolition_truck_trips_per_day=req.truck_trips_per_day,
        lane_closures=[
            LaneClosure(
                road_name=_closure_road_name(c.road_name),
                lanes_closed=c.lanes_closed,
                extent_m=c.extent_m,
                start_date=c.start_date or "",
                end_date=c.end_date or "",
            )
            for c in req.lane_closures
        ],
    )
    record = ScenarioRecord(id=store.new_id(), **scenario.model_dump())
    store.save_scenario(record)

    result = run_simulation(record.id, record, G)
    store.save_result(result)          # becomes /results/latest → the map updates
    m = result.metrics

    # Which closures actually matched road segments near the site?
    closed_names: Counter[str] = Counter()
    for eid in result.closed_edge_ids:
        u, v, k = _split_edge_id(eid, G)
        closed_names[_edge_street_name(G[u][v][k]).lower()] += 1
    applied = []
    for c in scenario.lane_closures:
        key = _norm_road(c.road_name)
        n = sum(cnt for name, cnt in closed_names.items() if key and key in _norm_road(name))
        applied.append(ClosureApplied(road_name=c.road_name, segments_closed=n))

    return AgentImpact(
        scenario_id=record.id,
        location=loc.name,
        latitude=round(loc.lat, 5),
        longitude=round(loc.lon, 5),
        summary=m.description,
        delay_change_pct=m.travel_time_change_pct,
        added_daily_trips=m.added_daily_trips,
        removed_daily_trips=m.removed_daily_trips,
        truck_trips_per_day=m.truck_trips_per_day,
        streets_most_affected=[
            StreetChange(street=r["name"],
                         load_before_pct=round(r["base_vc"] * 100),
                         load_after_pct=round(r["scen_vc"] * 100))
            for r in m.biggest_increases
        ],
        lane_closures_applied=applied,
        caveat=("Planning-level estimate from a calibrated PM-peak model using approximate "
                "trip-generation rates; not an engineering traffic study."),
        map_url_hint="The I-Grid map now shows this scenario; switch between Baseline and Scenario to compare.",
    )


@router.get(
    "/locations",
    operation_id="list_known_locations",
    summary="List Detroit landmarks the simulator recognises by name",
)
def list_locations() -> dict:
    return {
        "landmarks": [{"name": lm["name"], "latitude": lm["lat"], "longitude": lm["lon"]} for lm in LANDMARKS],
        "intersections": "Any two named streets in Downtown, Midtown, New Center or Corktown, "
                         "e.g. 'Woodward Ave & Mack Ave'.",
    }


# ---------------------------------------------------------------------------

_ROAD_ALIASES = {"i-375": "Walter P. Chrysler Freeway", "i 375": "Walter P. Chrysler Freeway",
                 "i375": "Walter P. Chrysler Freeway", "lodge": "John C Lodge Freeway",
                 "m-10": "John C Lodge Freeway", "i-75": "Fisher Freeway"}


def _closure_road_name(name: str) -> str:
    """Map common names (I-375, the Lodge) to the OSM road name."""
    return _ROAD_ALIASES.get(name.strip().lower(), name.strip())


def _norm_road(name: str) -> str:
    from ..sim import _normalize_road_name
    return _normalize_road_name(name)


def _split_edge_id(eid: str, G):
    u, v, k = eid.rsplit("-", 2)
    cast = (lambda x: int(x)) if isinstance(next(iter(G.nodes)), int) else (lambda x: x)
    return cast(u), cast(v), int(k)
