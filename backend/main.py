"""
Detroit I-Grid — FastAPI application entry point.

Endpoints (imported from routers):
  POST /scenario          – save a structured scenario, return id
  POST /simulate/{id}     – run traffic simulation, store results
  GET  /results/{id}      – trips + congestion + metrics for the map
  GET  /results/latest    – convenience alias polled by the frontend
  POST /utilities         – water / sewer / power demand estimates
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from .routers import scenarios, simulation, utilities

app = FastAPI(
    title="Detroit I-Grid API",
    description=(
        "Traffic and utilities impact simulation for proposed developments "
        "in Detroit. Imported as tools by watsonx Orchestrate agents."
    ),
    version="0.1.0",
)

# Results JSON (vehicle paths) is ~15 MB raw but compresses ~10x.
app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=5)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten before production
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(scenarios.router)
app.include_router(simulation.router)
app.include_router(utilities.router)


@app.get("/health", tags=["meta"])
def health_check():
    """Liveness probe for Code Engine."""
    return {"status": "ok"}
