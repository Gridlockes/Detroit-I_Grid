/**
 * frontend/app.js
 * Detroit I-Grid — map application logic.
 *
 * Layers
 * ──────
 *   PathLayer   — road edges coloured green→amber→red by V/C ratio
 *   TripsLayer  — animated vehicle trails across the peak hour
 *
 * Data flow
 * ─────────
 *   1. On load, fetch GET /graph/edges once → build edgeIndex (edge_id → path).
 *   2. Poll GET /results/latest every POLL_INTERVAL_MS.
 *   3. On status === "complete" (and new scenario_id), rebuild both layers.
 *   4. Animation loop runs independently with requestAnimationFrame.
 *
 * Mode
 * ────
 *   "baseline"  — shows baseline_edges + baseline_trajectories
 *   "scenario"  — shows scenario_edges + scenario_trajectories
 *   Toggle updates layers immediately without re-fetching.
 */

/* ── Config ────────────────────────────────────────────────────── */
// Read ?api= here: API_BASE is fixed at load time, so a later override is too late.
const API_BASE         = window.IGRID_API
                         || new URLSearchParams(window.location.search).get('api')
                         || '';
const POLL_INTERVAL_MS = 3000;
const TRIP_DURATION_S  = 3600;
const TRAIL_LENGTH_S   = 120;
const MAP_CENTER       = [-83.045, 42.331];
const MAP_ZOOM         = 13;

/* ── State ─────────────────────────────────────────────────────── */
let currentMode        = 'baseline';
let animTime           = 0;
let animSpeed          = 60;
let animRunning        = true;
let lastRealTs         = null;
let lastScenarioId     = null;
let resultData         = null;
let deckGL             = null;
let pathData           = [];
let tripData           = [];
let edgeIndex          = {};   // edge_id → [[lon0,lat0],[lon1,lat1]]

// New-scenario form state
let siteLat            = null;
let siteLon            = null;
let siteMarker         = null;  // maplibregl.Marker instance
let mapRef             = null;  // the MapLibre Map, set in init()

/* ── Colour scale  (V/C → RGB) ────────────────────────────────── */
function vcColor(vc) {
  // green [63,185,80] → amber [210,153,34] → red [248,81,73]
  if (vc <= 0.5) {
    const t = vc / 0.5;
    return [
      Math.round(63  + t * (210 - 63)),
      Math.round(185 + t * (153 - 185)),
      Math.round(80  + t * (34  - 80)),
    ];
  } else {
    const t = (vc - 0.5) / 0.5;
    return [
      Math.round(210 + t * (248 - 210)),
      Math.round(153 + t * (81  - 153)),
      Math.round(34  + t * (73  - 34)),
    ];
  }
}

/* ── Coord parsing ─────────────────────────────────────────────── */
function edgeIdToCoords(edgeId, nodesIndex) {
  const parts = edgeId.split('-');
  const u = parts[0], v = parts[1];
  const nu = nodesIndex[u], nv = nodesIndex[v];
  if (!nu || !nv) return null;
  return [[nu.lon, nu.lat], [nv.lon, nv.lat]];
}

/* ── Build deck.gl data from API result ────────────────────────── */
function buildPathData(edges) {
  if (!edges || edges.length === 0) return [];
  return edges.map(e => ({
    path:  e.path,
    color: vcColor(e.vc_ratio),
    width: Math.max(1, Math.min(6, 1 + e.vc_ratio * 4)),
  })).filter(d => d.path);
}

function buildTripData(trajectories) {
  if (!trajectories || trajectories.length === 0) return [];
  return trajectories.map(t => ({
    vendor: 0,
    path: t.waypoints.map(w => [w.lon, w.lat, w.timestamp]),
  })).filter(d => d.path.length >= 2);
}

/* ── Flatten edge list with node lookup ────────────────────────── */
function extractEdgePaths(edges, nodesMap) {
  return edges.map(e => {
    const parts = e.edge_id.split('-');
    const u = nodesMap[parts[0]], v = nodesMap[parts[1]];
    if (!u || !v) return null;
    return {
      path:     [[u.lon, u.lat], [v.lon, v.lat]],
      color:    vcColor(e.vc_ratio),
      width:    Math.max(1, Math.min(6, 1 + e.vc_ratio * 4)),
      vc_ratio: e.vc_ratio,
    };
  }).filter(Boolean);
}

/* ── deck.gl layer builders ────────────────────────────────────── */
function makePathLayer(data) {
  return new deck.PathLayer({
    id: 'congestion',
    data,
    getPath:       d => d.path,
    getColor:      d => d.color,
    getWidth:      d => d.width,
    widthUnits:    'pixels',
    widthMinPixels: 1,
    widthMaxPixels: 8,
    pickable:      true,
    opacity:       0.85,
    jointRounded:  true,
    capRounded:    true,
  });
}

function makeTripsLayer(data, currentTime) {
  return new deck.TripsLayer({
    id: 'trips',
    data,
    getPath:        d => d.path,
    getTimestamps:  d => d.path.map(p => p[2]),
    getColor:       [253, 128, 93],
    opacity:        0.6,
    widthMinPixels: 2,
    widthMaxPixels: 4,
    rounded:        true,
    trailLength:    TRAIL_LENGTH_S,
    currentTime,
  });
}

/* ── Render loop ───────────────────────────────────────────────── */
function animate(realTs) {
  if (deckGL === null) { requestAnimationFrame(animate); return; }

  if (lastRealTs !== null && animRunning) {
    const dtReal = (realTs - lastRealTs) / 1000;   // real seconds
    animTime = (animTime + dtReal * animSpeed) % TRIP_DURATION_S;
  }
  lastRealTs = realTs;

  // Update time-slider position
  const slider = document.getElementById('time-slider');
  if (slider && !slider._dragging) {
    slider.value = animTime;
    updateTimeLabel(animTime);
  }

  if (tripData.length > 0) {
    deckGL.setProps({ layers: [makePathLayer(pathData), makeTripsLayer(tripData, animTime)] });
  }

  requestAnimationFrame(animate);
}

function updateTimeLabel(t) {
  const el = document.getElementById('time-label');
  if (!el) return;
  const h = Math.floor(t / 3600) + 7;   // assume peak starts 7 AM
  const m = Math.floor((t % 3600) / 60);
  el.textContent = `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}`;
}

/* ── API polling ───────────────────────────────────────────────── */
async function fetchLatest() {
  try {
    setStatus('busy', 'Polling…');
    const r = await fetch(`${API_BASE}/results/latest`);
    if (r.status === 404) { setStatus('ok', 'Waiting for simulation…'); return; }
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();

    if (data.status === 'pending' || data.status === 'running') {
      setStatus('busy', 'Simulation running…');
      return;
    }
    if (data.status === 'error') {
      setStatus('err', 'Simulation error');
      return;
    }

    // "complete" — only rebuild layers when scenario_id changes
    if (data.scenario_id !== lastScenarioId) {
      lastScenarioId = data.scenario_id;
      resultData     = data;
      applyResult(data);
    }
    setStatus('ok', `Result: ${data.scenario_id.slice(0, 8)}…`);
  } catch (err) {
    setStatus('err', err.message);
  }
}

/* ── Fetch edge geometries once at startup ─────────────────────── */
async function fetchEdgeIndex() {
  try {
    const r = await fetch(`${API_BASE}/graph/edges`);
    if (!r.ok) return;
    const { edges } = await r.json();
    edges.forEach(e => {
      edgeIndex[e.edge_id] = [[e.lon0, e.lat0], [e.lon1, e.lat1]];
    });
    console.info(`[I-Grid] edge index loaded: ${edges.length} edges`);
  } catch (err) {
    console.warn('[I-Grid] could not load edge index:', err.message);
  }
}

/* ── Apply complete result to layers and metrics panel ──────────── */
function applyResult(data) {
  updateLayers(data, currentMode);
  updateMetrics(data.metrics);
}

function updateLayers(data, mode) {
  const trajs = mode === 'baseline'
    ? data.baseline_trajectories
    : data.scenario_trajectories;
  const edges = mode === 'baseline'
    ? data.baseline_edges
    : data.scenario_edges;

  // TripsLayer
  tripData = buildTripData(trajs);

  // PathLayer — use edgeIndex from /graph/edges if available;
  // fall back to trajectory segment density if not yet loaded.
  if (Object.keys(edgeIndex).length > 0) {
    pathData = edges.map(e => {
      const coords = edgeIndex[e.edge_id];
      if (!coords) return null;
      return {
        path:  coords,
        color: vcColor(e.vc_ratio),
        width: Math.max(1, Math.min(6, 1 + e.vc_ratio * 5)),
      };
    }).filter(Boolean);
  } else {
    // Fallback: derive density from trajectory waypoint segments
    const maxVC = Math.max(...edges.map(e => e.vc_ratio), 0.01);
    const segCounts = {};
    trajs.forEach(t => {
      for (let i = 0; i < t.waypoints.length - 1; i++) {
        const w0 = t.waypoints[i], w1 = t.waypoints[i + 1];
        const key = `${(w0.lon*200).toFixed(0)},${(w0.lat*200).toFixed(0)}|${(w1.lon*200).toFixed(0)},${(w1.lat*200).toFixed(0)}`;
        segCounts[key] = (segCounts[key] || 0) + 1;
      }
    });
    const maxCount = Math.max(...Object.values(segCounts), 1);
    pathData = Object.entries(segCounts).map(([key, count]) => {
      const [from, to] = key.split('|');
      const [lon0, lat0] = from.split(',').map(x => parseFloat(x) / 200);
      const [lon1, lat1] = to.split(',').map(x => parseFloat(x) / 200);
      return {
        path:  [[lon0, lat0], [lon1, lat1]],
        color: vcColor((count / maxCount) * maxVC),
        width: Math.max(1, Math.min(5, 1 + (count / maxCount) * 4)),
      };
    });
  }
}

/* ── Metrics panel ─────────────────────────────────────────────── */
function updateMetrics(m) {
  if (!m) return;

  set('stat-trips',   m.added_daily_trips.toLocaleString());
  set('stat-peak',    Math.round(m.added_daily_trips * 0.10).toLocaleString());

  const pct = m.travel_time_change_pct;
  const el  = document.getElementById('stat-delay');
  if (el) {
    el.textContent  = (pct >= 0 ? '+' : '') + pct.toFixed(1) + '%';
    el.className    = 'value ' + (pct > 1 ? 'up' : pct < -1 ? 'down' : 'neutral');
  }

  const list = document.getElementById('worst-list');
  if (list && m.worst_intersections) {
    list.innerHTML = m.worst_intersections.slice(0, 5).map((w, i) => `
      <li class="intersection-row">
        <span class="coords">#${i+1} ${w.lat.toFixed(4)}, ${w.lon.toFixed(4)}</span>
        <span class="vc" style="color:${rgbStr(vcColor(w.max_vc))}">${w.max_vc.toFixed(2)}</span>
      </li>
    `).join('');
  }

  const descEl = document.getElementById('metrics-desc');
  if (descEl) descEl.textContent = m.description;
}

function rgbStr([r,g,b]) { return `rgb(${r},${g},${b})`; }
function set(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

/* ── Status bar ────────────────────────────────────────────────── */
function setStatus(state, msg) {
  const dot  = document.getElementById('status-dot');
  const text = document.getElementById('status-text');
  if (dot)  { dot.className = 'status-dot ' + state; }
  if (text) text.textContent = msg;
}

/* ── Tab switching ─────────────────────────────────────────────── */
function initTabs() {
  document.querySelectorAll('.panel-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      const target = tab.dataset.tab;
      document.querySelectorAll('.panel-tab').forEach(t =>
        t.classList.toggle('active', t.dataset.tab === target)
      );
      document.querySelectorAll('.tab-pane').forEach(p =>
        p.classList.toggle('active', p.id === `tab-${target}`)
      );
    });
  });
}

/* ── Site-location pin ─────────────────────────────────────────── */
function setSiteLocation(lat, lon) {
  siteLat = lat;
  siteLon = lon;

  // Update display
  const display = document.getElementById('location-display');
  const text    = document.getElementById('location-text');
  if (display) display.classList.add('has-location');
  if (text)    text.textContent = `${lat.toFixed(5)}, ${lon.toFixed(5)}`;

  // Drop / move the marker on the map
  if (mapRef) {
    if (siteMarker) {
      siteMarker.setLngLat([lon, lat]);
    } else {
      // Custom element so it's styled without a Mapbox token
      const el = document.createElement('div');
      el.style.cssText = `
        width:22px; height:22px;
        background:#58a6ff; border:2px solid #fff;
        border-radius:50% 50% 50% 0; transform:rotate(-45deg);
        box-shadow:0 2px 6px rgba(0,0,0,0.5);
      `;
      siteMarker = new maplibregl.Marker({ element: el, anchor: 'bottom-left' })
        .setLngLat([lon, lat])
        .addTo(mapRef);
    }
  }

  // Enable the run button once a location is set
  updateRunButton();
}

function updateRunButton() {
  const btn = document.getElementById('run-btn');
  if (!btn) return;
  btn.disabled = (siteLat === null);
}

/* ── New-scenario submission ───────────────────────────────────── */
async function runScenario() {
  const btn      = document.getElementById('run-btn');
  const errEl    = document.getElementById('run-error');
  if (!btn || !errEl) return;

  // Gather form values
  const description  = document.getElementById('sc-description').value.trim();
  const housingUnits = parseInt(document.getElementById('sc-units').value, 10)   || 0;
  const retailSqft   = parseFloat(document.getElementById('sc-retail').value)    || 0;
  const openingYear  = parseInt(document.getElementById('sc-year').value, 10)    || 2028;
  const roadName     = document.getElementById('sc-road').value.trim();
  const lcStart      = document.getElementById('sc-lc-start').value;
  const lcEnd        = document.getElementById('sc-lc-end').value;
  const lanesClosed  = parseInt(document.getElementById('sc-lanes-closed').value, 10) || 1;

  // Build payload
  const payload = {
    location_lat:  siteLat,
    location_lon:  siteLon,
    description,
    housing_units: housingUnits,
    retail_sqft:   retailSqft,
    opening_year:  openingYear,
    lane_closures: roadName ? [{
      road_name:    roadName,
      start_date:   lcStart,
      end_date:     lcEnd,
      lanes_closed: lanesClosed,
    }] : [],
  };

  // ── Set running state
  btn.disabled = true;
  btn.classList.add('running');
  errEl.style.display = 'none';
  setStatus('busy', 'Submitting scenario…');

  try {
    // 1. POST /scenario
    const r1 = await fetch(`${API_BASE}/scenario`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(payload),
    });
    if (!r1.ok) {
      const msg = await r1.text();
      throw new Error(`POST /scenario: ${r1.status} ${msg}`);
    }
    const scenario = await r1.json();
    const id = scenario.id;
    setStatus('busy', `Scenario saved (${id.slice(0,8)}…)`);

    // 2. POST /simulate/{id}
    const r2 = await fetch(`${API_BASE}/simulate/${id}`, { method: 'POST' });
    if (!r2.ok) {
      const msg = await r2.text();
      throw new Error(`POST /simulate: ${r2.status} ${msg}`);
    }
    setStatus('busy', 'Simulation running…');

    // 3. Poll GET /results/{id} until complete (max 60 s)
    const result = await pollResult(id, 60);

    // 4. Apply and switch to Scenario mode
    lastScenarioId = result.scenario_id;
    resultData     = result;
    applyResult(result);

    switchToScenarioMode();
    switchToResultsTab();
    setStatus('ok', `Complete (${id.slice(0,8)}…)`);

  } catch (err) {
    errEl.textContent   = err.message;
    errEl.style.display = 'block';
    setStatus('err', 'Simulation failed');
  } finally {
    btn.classList.remove('running');
    btn.disabled = (siteLat === null);
  }
}

async function pollResult(id, timeoutSec) {
  const deadline = Date.now() + timeoutSec * 1000;
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, 1500));
    const r = await fetch(`${API_BASE}/results/${id}`);
    if (!r.ok) continue;
    const data = await r.json();
    if (data.status === 'complete') return data;
    if (data.status === 'error')    throw new Error('Simulation returned an error');
  }
  throw new Error('Simulation timed out after 60 s');
}

function switchToScenarioMode() {
  currentMode = 'scenario';
  document.querySelectorAll('#mode-toggle button').forEach(b =>
    b.classList.toggle('active', b.dataset.mode === 'scenario')
  );
}

function switchToResultsTab() {
  document.querySelectorAll('.panel-tab').forEach(t =>
    t.classList.toggle('active', t.dataset.tab === 'results')
  );
  document.querySelectorAll('.tab-pane').forEach(p =>
    p.classList.toggle('active', p.id === 'tab-results')
  );
}

/* ── Init ──────────────────────────────────────────────────────── */
function init() {
  // Parse ?api= override for CORS dev usage
  const params = new URLSearchParams(window.location.search);
  if (params.get('api')) window.IGRID_API = params.get('api');

  // ── MapLibre map
  const map = new maplibregl.Map({
    container: 'map',
    style: 'https://basemaps.cartocdn.com/gl/dark-matter-nolabels-gl-style/style.json',
    center: MAP_CENTER,
    zoom:   MAP_ZOOM,
    antialias: true,
  });
  mapRef = map;

  // ── deck.gl overlay (deck = window.deck from the CDN UMD build)
  const deckOverlay = new deck.MapboxOverlay({ layers: [] });
  map.addControl(deckOverlay);

  // deckGL is our thin wrapper; we keep window.deck as the layer namespace
  deckGL = {
    setProps: (props) => deckOverlay.setProps(props),
  };

  // ── Kick off animation
  requestAnimationFrame(animate);

  // ── Map click → set site location
  map.on('click', e => {
    setSiteLocation(e.lngLat.lat, e.lngLat.lng);
  });

  // ── Tab switching
  initTabs();

  // ── Mode toggle
  document.querySelectorAll('#mode-toggle button').forEach(btn => {
    btn.addEventListener('click', () => {
      currentMode = btn.dataset.mode;
      document.querySelectorAll('#mode-toggle button').forEach(b =>
        b.classList.toggle('active', b.dataset.mode === currentMode)
      );
      if (resultData) updateLayers(resultData, currentMode);
    });
  });

  // ── Time slider (manual scrub)
  const slider = document.getElementById('time-slider');
  if (slider) {
    slider.max = TRIP_DURATION_S;
    slider.addEventListener('mousedown', () => { slider._dragging = true;  animRunning = false; });
    slider.addEventListener('mouseup',   () => { slider._dragging = false; animRunning = true; });
    slider.addEventListener('input',     () => { animTime = Number(slider.value); updateTimeLabel(animTime); });
  }

  // ── Speed slider
  const speedRange = document.getElementById('speed-range');
  if (speedRange) {
    speedRange.addEventListener('input', () => {
      animSpeed = Number(speedRange.value);
      const lbl = document.getElementById('speed-label');
      if (lbl) lbl.textContent = `${animSpeed}×`;
    });
  }

  // ── Run simulation button
  const runBtn = document.getElementById('run-btn');
  if (runBtn) runBtn.addEventListener('click', runScenario);

  // ── Fetch edge geometries (once) and start polling
  fetchEdgeIndex();
  fetchLatest();
  setInterval(fetchLatest, POLL_INTERVAL_MS);
}

// Wait for deck.gl and MapLibre to load
window.addEventListener('load', init);
