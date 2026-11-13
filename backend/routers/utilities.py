"""
routers/utilities.py — POST /utilities

Estimates water, sewer, and power demand for a development scenario
using per-capita planning figures.  Used by the Utilities agent.
"""

from __future__ import annotations

from fastapi import APIRouter

from ..models import UtilityRequest, UtilityResult

router = APIRouter(tags=["utilities"])

# Planning-level per-capita rates (labeled as estimates in all outputs)
WATER_GAL_PER_PERSON_DAY = 100.0
SEWER_FRACTION = 0.90
PEAK_KW_PER_UNIT = 1.2


@router.post(
    "/utilities",
    response_model=UtilityResult,
    summary="Estimate water, sewer, and power demand",
    description=(
        "Returns approximate planning-level utility demand for a proposed "
        "development.  All figures are estimates; cite sources before use "
        "in any official document."
    ),
)
def estimate_utilities(req: UtilityRequest) -> UtilityResult:
    """
    Water  ≈ 100 gal/person/day
    Sewer  ≈ 90 % of water
    Power  ≈ 1.2 kW peak/unit
    """
    residents = req.housing_units * req.residents_per_unit
    water = residents * WATER_GAL_PER_PERSON_DAY
    sewer = water * SEWER_FRACTION
    power = req.housing_units * PEAK_KW_PER_UNIT

    return UtilityResult(
        water_gpd=round(water, 1),
        sewer_gpd=round(sewer, 1),
        peak_power_kw=round(power, 1),
    )
