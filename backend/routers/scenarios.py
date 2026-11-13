"""
routers/scenarios.py — POST /scenario

Accepts a Scenario JSON body, assigns an id, persists it, and returns
the id.  Used by the Intake agent via the OpenAPI tool import.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..models import Scenario, ScenarioRecord
from ..store import store

router = APIRouter(tags=["scenarios"])


@router.post(
    "/scenario",
    response_model=ScenarioRecord,
    summary="Save a structured development scenario",
    description=(
        "Accepts a parsed development scenario (location, housing units, "
        "retail area, lane closures) and returns a scenario id that is "
        "passed to POST /simulate/{id}."
    ),
)
def create_scenario(scenario: Scenario) -> ScenarioRecord:
    """Save a scenario and return the assigned id."""
    record = ScenarioRecord(id=store.new_id(), **scenario.model_dump())
    store.save_scenario(record)
    return record
