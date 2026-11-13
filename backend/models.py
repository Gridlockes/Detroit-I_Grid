"""
Pydantic models shared across the application.

Scenario         – a user-submitted development scenario
SimulationResult – stored output of a completed simulation run
UtilityRequest   – input for the utilities demand endpoint
UtilityResult    – water / sewer / power demand estimates
"""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Scenario
# ---------------------------------------------------------------------------

class LaneClosure(BaseModel):
    """A temporary capacity reduction on a named road segment."""

    road_name: str = Field(..., description="OSM name of the affected road, e.g. 'Woodward Ave'")
    start_date: str = Field(..., description="ISO date, e.g. '2027-03-01'")
    end_date: str = Field(..., description="ISO date, e.g. '2027-09-01'")
    lanes_closed: int = Field(1, ge=1, description="Number of lanes closed (≥ 1)")


class Scenario(BaseModel):
    """
    Structured representation of a proposed development, produced by the
    Intake agent from free-text user input.
    """

    location_lat: float = Field(..., description="WGS-84 latitude of the development site")
    location_lon: float = Field(..., description="WGS-84 longitude of the development site")
    description: str = Field("", description="Human-readable description of the project")
    housing_units: int = Field(0, ge=0, description="Number of residential units")
    retail_sqft: float = Field(0.0, ge=0, description="Retail floor area in square feet")
    opening_year: int = Field(2028, description="Year the development opens")
    lane_closures: list[LaneClosure] = Field(
        default_factory=list,
        description="Temporary lane closures during construction",
    )


class ScenarioRecord(Scenario):
    """Scenario with a server-assigned id."""

    id: str


# ---------------------------------------------------------------------------
# Simulation result
# ---------------------------------------------------------------------------

class EdgeMetric(BaseModel):
    """Volume/capacity ratio for one road edge."""

    edge_id: str
    volume: float
    capacity: float
    vc_ratio: float


class Waypoint(BaseModel):
    """A single [lon, lat, timestamp] point for deck.gl TripsLayer."""

    lon: float
    lat: float
    timestamp: float


class VehicleTrajectory(BaseModel):
    vehicle_id: str
    waypoints: list[Waypoint]


class SimulationMetrics(BaseModel):
    added_daily_trips: int
    worst_intersections: list[dict[str, Any]]
    travel_time_change_pct: float
    description: str


class SimulationResult(BaseModel):
    scenario_id: str
    status: str = Field("pending", description="'pending' | 'running' | 'complete' | 'error'")
    baseline_edges: list[EdgeMetric] = Field(default_factory=list)
    scenario_edges: list[EdgeMetric] = Field(default_factory=list)
    baseline_trajectories: list[VehicleTrajectory] = Field(default_factory=list)
    scenario_trajectories: list[VehicleTrajectory] = Field(default_factory=list)
    metrics: SimulationMetrics | None = None


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

class UtilityRequest(BaseModel):
    housing_units: int = Field(0, ge=0)
    retail_sqft: float = Field(0.0, ge=0)
    residents_per_unit: float = Field(2.1, description="Average household size")


class UtilityResult(BaseModel):
    water_gpd: float = Field(..., description="Water demand, gallons per day (estimate)")
    sewer_gpd: float = Field(..., description="Sewer demand, gallons per day (estimate)")
    peak_power_kw: float = Field(..., description="Peak electrical demand, kW (estimate)")
    notes: str = "These are approximate planning-level estimates."
