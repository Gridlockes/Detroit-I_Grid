/**
 * frontend/app.js
 * Detroit I-Grid — map application logic.
 *
 * Responsibilities (implemented in prompt 5):
 *   - Initialise MapLibre GL map (dark basemap)
 *   - Initialise deck.gl overlay
 *   - Poll GET /results/latest every 2 s; update layers on change
 *   - TripsLayer: animate vehicle paths for baseline and scenario
 *   - PathLayer: colour road edges green→red by V/C ratio
 *   - Scenario panel: baseline / scenario toggle, time slider
 *   - Metrics panel: added trips, worst intersections, travel-time Δ
 *
 * TODO (prompt 5): implement all of the above.
 */

const API_BASE = window.location.origin;   // same origin as the FastAPI backend
const POLL_INTERVAL_MS = 2000;

// ---------------------------------------------------------------------------
// Placeholder bootstrap — replaced in prompt 5
// ---------------------------------------------------------------------------

console.info("[I-Grid] app.js loaded — full implementation coming in prompt 5");
