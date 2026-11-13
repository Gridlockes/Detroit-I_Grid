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
let edgeNames          = {};   // edge_id → OSM street name (for hover tooltip)

// New-scenario form state
let siteLat            = null;
let siteLon            = null;
let siteMarker         = null;  // maplibregl.Marker instance
let mapRef             = null;  // the MapLibre Map, set in init()

/* ── Colour scale  (V/C → RGB) ────────────────────────────────── */
function vcColor(vc) {
  // green [63,185,80] → amber [210,153,34] → red [248,81,73] at V/C 1.0,
  // then deepening to crimson [178,16,58] by V/C 2.0. Clamped: values above
  // capacity must never extrapolate into magenta/blue.
  const lerp = (a, b, t) => a.map((v, i) => Math.round(v + (b[i] - v) * t));
  const x = Math.max(0, vc || 0);
  if (x <= 0.5) return lerp([63, 185, 80], [210, 153, 34], x / 0.5);
  if (x <= 1.0) return lerp([210, 153, 34], [248, 81, 73], (x - 0.5) / 0.5);
  return lerp([248, 81, 73], [178, 16, 58], Math.min(1, (x - 1.0) / 1.0));
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
    slow: t.waypoints.map(w => w.slow || 1),   // congestion factor per segment
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
  // Faint cyan light-streaks behind the hologram cars.
  return new deck.TripsLayer({
    id: 'trips',
    data,
    getPath:        d => d.path,
    getTimestamps:  d => d.path.map(p => p[2]),
    getColor:       [0, 229, 255],
    opacity:        0.22,
    widthMinPixels: 1,
    widthMaxPixels: 2,
    rounded:        true,
    trailLength:    TRAIL_LENGTH_S * 0.6,
    currentTime,
  });
}

/* ── Low-res hologram car sprites ────────────────────────────────
 * A tiny pixel-art car (16×8 px) drawn in cyan, with a dim 1-px halo and
 * scan-lines, scaled up with nearest-neighbour filtering so it stays blocky.
 * The sprite points east; IconLayer rotates it to each car's heading.
 */
const HOLO_CAR_PIXELS = [
  '................',
  '....XXXXXX......',
  '...XWWXWWWX.....',
  '.XXXXXXXXXXXXXX.',
  'XXXXXXXXXXXXXXXH',
  '.XXXXXXXXXXXXXX.',
  '..OO.......OO...',
  '................',
];
const HOLO_COLORS = {
  X: [0, 229, 255, 235],    // body
  W: [190, 255, 255, 255],  // windows
  H: [255, 255, 255, 255],  // headlight
  O: [0, 110, 130, 235],    // wheels
};

function makeHoloCarAtlas() {
  const w = HOLO_CAR_PIXELS[0].length, h = HOLO_CAR_PIXELS.length;
  const canvas = document.createElement('canvas');
  canvas.width = w; canvas.height = h;
  const ctx = canvas.getContext('2d');
  const img = ctx.createImageData(w, h);
  const lit = (x, y) => (HOLO_CAR_PIXELS[y] || '')[x] && HOLO_CAR_PIXELS[y][x] !== '.';
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      let rgba = HOLO_COLORS[HOLO_CAR_PIXELS[y][x]];
      if (!rgba && (lit(x - 1, y) || lit(x + 1, y) || lit(x, y - 1) || lit(x, y + 1))) {
        rgba = [0, 229, 255, 70];               // halo glow around the body
      }
      if (!rgba) continue;
      const scan = y % 2 === 1 ? 0.7 : 1.0;     // hologram scan-lines
      const i = (y * w + x) * 4;
      img.data[i] = rgba[0]; img.data[i + 1] = rgba[1]; img.data[i + 2] = rgba[2];
      img.data[i + 3] = Math.round(rgba[3] * scan);
    }
  }
  ctx.putImageData(img, 0, 0);
  return {
    atlas: canvas.toDataURL(),
    // mask:true → the sprite's alpha (shape, halo, scan-lines) is tinted by getColor.
    mapping: { car: { x: 0, y: 0, width: w, height: h, mask: true } },
  };
}
let holoAtlas = null;

// Position + heading of every car on the road at time t (binary search per trip).
function carPositionsAt(trips, t) {
  const cars = [];
  for (const trip of trips) {
    const p = trip.path;
    if (t < p[0][2] || t > p[p.length - 1][2]) continue;
    let lo = 0, hi = p.length - 1;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (p[mid][2] <= t) lo = mid; else hi = mid;
    }
    const a = p[lo], b = p[hi];
    const span = b[2] - a[2];
    const f = span > 0 ? (t - a[2]) / span : 0;
    const lon = a[0] + (b[0] - a[0]) * f;
    const lat = a[1] + (b[1] - a[1]) * f;
    // Heading: counter-clockwise degrees from east, corrected for longitude scale.
    const dx = (b[0] - a[0]) * Math.cos(lat * Math.PI / 180);
    const dy = b[1] - a[1];
    const slow = (trip.slow && trip.slow[lo]) || 1;
    cars.push({ position: [lon, lat], angle: Math.atan2(dy, dx) * 180 / Math.PI, slow });
  }
  return cars;
}

// Car colour by congestion slowdown (congested ÷ free-flow travel time):
// cyan = moving freely, yellow = slowed, hot pink = crawling.
const CAR_FREE  = [0, 229, 255];
const CAR_SLOW  = [255, 226, 64];    // yellow: distinct from amber roads
const CAR_CRAWL = [255, 90, 230];    // hot pink: stands out on red, jammed roads
function carColor(slow) {
  const lerp = (a, b, t) => a.map((v, i) => Math.round(v + (b[i] - v) * t));
  if (slow <= 1.1) return CAR_FREE;
  if (slow <= 1.5) return lerp(CAR_FREE, CAR_SLOW, (slow - 1.1) / 0.4);
  if (slow <= 2.2) return lerp(CAR_SLOW, CAR_CRAWL, (slow - 1.5) / 0.7);
  return CAR_CRAWL;
}

/* ── Work zone: closed segments + project site (Scenario mode only) ── */
let workZoneData = [];     // [{path}] for closed road segments
let siteData     = [];     // [{position}] project site

function makeWorkZoneLayers(realTs) {
  if (currentMode !== 'scenario') return [];
  const pulse = 0.5 + 0.5 * Math.sin(realTs / 300);          // 0..1, ~2 s period
  const layers = [];
  if (workZoneData.length) {
    layers.push(new deck.PathLayer({
      id: 'work-zone-glow',
      data: workZoneData,
      getPath: d => d.path,
      getColor: [255, 170, 0],
      getWidth: 9,
      widthUnits: 'pixels',
      opacity: 0.10 + 0.14 * pulse,
      capRounded: true, jointRounded: true,
    }));
    const dashExt = deck.PathStyleExtension ? [new deck.PathStyleExtension({ dash: true })] : [];
    layers.push(new deck.PathLayer({
      id: 'work-zone-dash',
      data: workZoneData,
      getPath: d => d.path,
      getColor: [255, 200, 40],
      getWidth: 2.5,
      widthUnits: 'pixels',
      opacity: 0.85,
      ...(dashExt.length ? { getDashArray: [2, 3], dashJustified: true, extensions: dashExt } : {}),
    }));
  }
  if (siteData.length) {
    layers.push(new deck.ScatterplotLayer({
      id: 'site-ring',
      data: siteData,
      getPosition: d => d.position,
      getRadius: 120 + 160 * pulse,            // metres, expanding ring
      radiusUnits: 'meters',
      stroked: true, filled: true,
      getFillColor: [255, 170, 0, Math.round(40 * (1 - pulse))],
      getLineColor: [255, 190, 40, Math.round(220 * (1 - pulse) + 35)],
      lineWidthUnits: 'pixels', getLineWidth: 2,
    }));
  }
  return layers;
}

function makeHoloCarLayer(trips, currentTime, realTs) {
  if (!holoAtlas) holoAtlas = makeHoloCarAtlas();
  const flicker = 0.82 + 0.12 * Math.sin(realTs / 90) + 0.06 * Math.sin(realTs / 23);
  return new deck.IconLayer({
    id: 'holo-cars',
    data: carPositionsAt(trips, currentTime),
    iconAtlas: holoAtlas.atlas,
    iconMapping: holoAtlas.mapping,
    getIcon: () => 'car',
    getPosition: d => d.position,
    getAngle: d => d.angle,
    getColor: d => carColor(d.slow),
    getSize: d => (d.slow > 1.5 ? 13 : 11),      // jammed cars read slightly larger
    sizeUnits: 'pixels',
    sizeMinPixels: 7,
    billboard: false,                 // lie flat on the map, rotate with heading
    opacity: flicker,
    textureParameters: { minFilter: 'nearest', magFilter: 'nearest' },  // keep it low-res
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
    deckGL.setProps({ layers: [
      makePathLayer(pathData),
      makeTripsLayer(tripData, animTime),
      ...makeWorkZoneLayers(realTs),
      makeHoloCarLayer(tripData, animTime, realTs),
    ] });
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
      // Prefer the full curved road shape; fall back to a straight segment.
      edgeIndex[e.edge_id] = (Array.isArray(e.coords) && e.coords.length >= 2)
        ? e.coords
        : [[e.lon0, e.lat0], [e.lon1, e.lat1]];
      edgeNames[e.edge_id] = Array.isArray(e.name) ? e.name.join(' / ') : (e.name || '');
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

  // Work zone (only drawn in Scenario mode, but built once per result)
  workZoneData = (data.closed_edge_ids || [])
    .map(id => edgeIndex[id]).filter(Boolean).map(path => ({ path }));
  siteData = data.site ? [{ position: [data.site.lon, data.site.lat] }] : [];

  // PathLayer — use edgeIndex from /graph/edges if available;
  // fall back to trajectory segment density if not yet loaded.
  if (Object.keys(edgeIndex).length > 0) {
    pathData = edges.map(e => {
      const coords = edgeIndex[e.edge_id];
      if (!coords) return null;
      return {
        path:  coords,
        name:  edgeNames[e.edge_id] || '',
        vc:    e.vc_ratio,
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

  // Demolition rows appear only when the scenario has a demolition; the
  // "added trips" rows hide when nothing was added, so we never show a bare 0.
  const removed = m.removed_daily_trips || 0;
  const trucks  = m.truck_trips_per_day || 0;
  const demo    = removed > 0 || trucks > 0;
  showRow('row-removed', removed > 0);
  showRow('row-trucks',  trucks > 0);
  showRow('row-trips',   !demo || m.added_daily_trips > 0);
  showRow('row-peak',    !demo || m.added_daily_trips > 0);
  set('stat-removed', '−' + removed.toLocaleString());
  set('stat-trucks',  '+' + trucks.toLocaleString());

  const pct = m.travel_time_change_pct;
  const el  = document.getElementById('stat-delay');
  if (el) {
    el.textContent  = (pct >= 0 ? '+' : '') + pct.toFixed(1) + '%';
    el.className    = 'value ' + (pct > 1 ? 'up' : pct < -1 ? 'down' : 'neutral');
  }

  // Streets that get worse because of THIS scenario (named, clickable).
  // Falls back to the old worst-intersection list for older results.
  const list  = document.getElementById('worst-list');
  const title = document.getElementById('worst-title');
  const pctTxt = vc => Math.round(vc * 100) + '%';
  if (list && m.biggest_increases && m.biggest_increases.length) {
    if (title) title.textContent = 'Biggest Increases Near Site (2 km)';
    list.innerHTML = m.biggest_increases.slice(0, 5).map((w, i) => `
      <li class="intersection-row clickable" data-lat="${w.lat}" data-lon="${w.lon}"
          title="Zoom to ${escapeHtml(w.name)}">
        <span class="coords">#${i+1} ${escapeHtml(w.name)}</span>
        <span class="vc" style="color:${rgbStr(vcColor(w.scen_vc))}">
          ${pctTxt(w.base_vc)} → ${pctTxt(w.scen_vc)}
        </span>
      </li>
    `).join('');
    list.querySelectorAll('li.clickable').forEach(li => li.addEventListener('click', () => {
      if (mapRef) mapRef.flyTo({ center: [Number(li.dataset.lon), Number(li.dataset.lat)], zoom: 15.5 });
    }));
  } else if (list && m.worst_intersections) {
    if (title) title.textContent = 'Most Congested Locations (V/C)';
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
function showRow(id, visible) {
  const el = document.getElementById(id);
  if (el) el.style.display = visible ? '' : 'none';
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
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
/* ── Scenario presets: Detroit construction & demolition ───────────
 * Every number here is an ILLUSTRATIVE planning ASSUMPTION, not an official
 * project figure. Presets only fill the form; users can edit any value.
 * Fields left out default to 0 / no closure.
 */
const PRESETS = [
  {
    id: 'rencen', icon: '🏙️', label: 'Renaissance Center demolition',
    lat: 42.3293, lon: -83.0398,                    // Jefferson Ave & Beaubien St
    description: 'Renaissance Center demolition: office towers removed, debris ' +
                 'hauled to I-375 / the Lodge, Jefferson and Beaubien lanes closed',
    year: 2027,
    demoSqft: 2000000, demoOcc: 35, demoTrucks: 250,
    closures: [
      { road: 'Jefferson Ave', lanes: 1, extent: 800, start: '2027-01-01', end: '2028-06-30' },
      { road: 'Beaubien St',   lanes: 1, extent: 500, start: '2027-01-01', end: '2028-06-30' },
    ],
  },
  {
    id: 'i375', icon: '🛣️', label: 'I-375 freeway → boulevard (construction)',
    lat: 42.3375, lon: -83.0412,                    // I-375 between Jefferson & Gratiot
    description: 'I-375 boulevard conversion, construction phase: freeway deck ' +
                 'demolished, two lanes closed each way, Gratiot lane closed at the ramps',
    year: 2027,
    demoSqft: 0, demoTrucks: 120,                   // freeway structure, not office space
    closures: [
      { road: 'Walter P. Chrysler Freeway', lanes: 2, extent: 1200, start: '2027-03-01', end: '2028-11-30' },
      { road: 'Gratiot Ave',                lanes: 1, extent: 400,  start: '2027-03-01', end: '2028-11-30' },
    ],
  },
  {
    id: 'hudsons', icon: '🏢', label: "Hudson's site tower (full occupancy)",
    lat: 42.3339, lon: -83.0470,                    // Woodward & Grand River
    description: "Hudson's site on Woodward fully occupied: offices, residences and " +
                 'retail; final fit-out keeps a Woodward and a Grand River lane closed',
    year: 2027,
    units: 100, retail: 60000, office: 400000, demoTrucks: 40,
    closures: [
      { road: 'Woodward Ave',    lanes: 1, extent: 300, start: '2026-11-01', end: '2027-05-31' },
      { road: 'Grand River Ave', lanes: 1, extent: 300, start: '2026-11-01', end: '2027-05-31' },
    ],
  },
  {
    id: 'mcentral', icon: '🚉', label: 'Michigan Central campus build-out',
    lat: 42.3289, lon: -83.0776,                    // Corktown, Vernor & 15th St
    description: 'Michigan Central campus build-out: obsolete warehouses demolished ' +
                 'for new offices and labs; Michigan Ave and Vernor lanes closed',
    year: 2028,
    retail: 20000, office: 1000000,
    demoSqft: 150000, demoOcc: 0, demoTrucks: 80,   // vacant buildings: no trips removed
    closures: [
      { road: 'Michigan Ave',   lanes: 1, extent: 600, start: '2027-05-01', end: '2028-09-30' },
      { road: 'Vernor Highway', lanes: 1, extent: 400, start: '2027-05-01', end: '2028-09-30' },
    ],
  },
  {
    id: 'district', icon: '🏟️', label: 'District Detroit mixed-use infill',
    lat: 42.3437, lon: -83.0601,                    // Cass Ave near Temple St
    description: 'District Detroit infill near Cass & Temple: vacant buildings ' +
                 'demolished for apartments, offices and retail; Cass and Temple lanes closed',
    year: 2028,
    units: 700, retail: 30000, office: 200000,
    demoSqft: 80000, demoOcc: 0, demoTrucks: 60,
    closures: [
      { road: 'Cass Ave',  lanes: 1, extent: 500, start: '2027-04-01', end: '2028-08-31' },
      { road: 'Temple St', lanes: 1, extent: 300, start: '2027-04-01', end: '2028-08-31' },
    ],
  },
];

/* ── Lane-closure rows ─────────────────────────────────────────── */
function addClosureRow(c = {}) {
  const list = document.getElementById('closure-list');
  if (!list) return;
  const row = document.createElement('div');
  row.className = 'closure-row';
  row.innerHTML = `
    <div class="lc-line">
      <input class="lc-road" type="text" placeholder="Road, e.g. Woodward Ave" aria-label="Road name" />
      <button type="button" class="lc-remove" title="Remove closure">✕</button>
    </div>
    <div class="lc-grid">
      <label>Lanes closed <input class="lc-lanes" type="number" min="1" max="4" value="1" /></label>
      <label>Within (m) <input class="lc-extent" type="number" min="50" step="50" value="800" /></label>
      <label>Start <input class="lc-start" type="date" value="2027-03-01" /></label>
      <label>End <input class="lc-end" type="date" value="2027-09-01" /></label>
    </div>`;
  row.querySelector('.lc-road').value   = c.road   || '';
  row.querySelector('.lc-lanes').value  = c.lanes  || 1;
  row.querySelector('.lc-extent').value = c.extent || 800;
  row.querySelector('.lc-start').value  = c.start  || '2027-03-01';
  row.querySelector('.lc-end').value    = c.end    || '2027-09-01';
  row.querySelector('.lc-remove').addEventListener('click', () => row.remove());
  list.appendChild(row);
}

function setClosureRows(closures) {
  const list = document.getElementById('closure-list');
  if (list) list.innerHTML = '';
  (closures || []).forEach(addClosureRow);
}

function collectClosures() {
  return [...document.querySelectorAll('#closure-list .closure-row')].map(row => ({
    road_name:    row.querySelector('.lc-road').value.trim(),
    lanes_closed: parseInt(row.querySelector('.lc-lanes').value, 10) || 1,
    extent_m:     parseFloat(row.querySelector('.lc-extent').value) || 800,
    start_date:   row.querySelector('.lc-start').value,
    end_date:     row.querySelector('.lc-end').value,
  })).filter(c => c.road_name);
}

function applyPreset(p) {
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
  set('sc-description', p.description);
  set('sc-units', p.units || 0);
  set('sc-retail', p.retail || 0);
  set('sc-office', p.office || 0);
  set('sc-year', p.year || 2028);
  set('sc-demo-sqft', p.demoSqft || 0);
  set('sc-demo-occ', p.demoOcc ?? 100);
  set('sc-demo-trucks', p.demoTrucks || 0);
  setClosureRows(p.closures);
  setSiteLocation(p.lat, p.lon);
  if (mapRef) mapRef.flyTo({ center: [p.lon, p.lat], zoom: 14.5 });
  document.querySelectorAll('.preset-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.preset === p.id));
}

function renderPresets() {
  const box = document.getElementById('preset-list');
  if (!box) return;
  box.innerHTML = PRESETS.map(p => `
    <button type="button" class="preset-btn" data-preset="${p.id}">
      ${p.icon} ${escapeHtml(p.label)}
    </button>`).join('');
  box.querySelectorAll('.preset-btn').forEach(btn => btn.addEventListener('click', () => {
    const p = PRESETS.find(x => x.id === btn.dataset.preset);
    if (p) applyPreset(p);
  }));
}

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
  const officeSqft   = parseFloat(document.getElementById('sc-office').value)    || 0;
  const openingYear  = parseInt(document.getElementById('sc-year').value, 10)    || 2028;
  const demoSqft     = parseFloat(document.getElementById('sc-demo-sqft').value)   || 0;
  const demoOccPct   = parseFloat(document.getElementById('sc-demo-occ').value);
  const demoTrucks   = parseInt(document.getElementById('sc-demo-trucks').value, 10) || 0;

  // Build payload
  const payload = {
    location_lat:  siteLat,
    location_lon:  siteLon,
    description,
    housing_units: housingUnits,
    retail_sqft:   retailSqft,
    office_sqft:   officeSqft,
    opening_year:  openingYear,
    demolished_office_sqft:         demoSqft,
    existing_occupancy:             Number.isFinite(demoOccPct)
                                      ? Math.min(Math.max(demoOccPct, 0), 100) / 100 : 1,
    demolition_truck_trips_per_day: demoTrucks,
    lane_closures: collectClosures(),
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
    // Dark basemap WITH street labels (was the "nolabels" variant).
    style: 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json',
    center: MAP_CENTER,
    zoom:   MAP_ZOOM,
    antialias: true,
  });
  mapRef = map;

  // ── deck.gl overlay (deck = window.deck from the CDN UMD build)
  // Hovering a road shows its street name and congestion level.
  const deckOverlay = new deck.MapboxOverlay({
    layers: [],
    getTooltip: ({ object, layer }) => {
      if (!object || !layer || layer.id !== 'congestion') return null;
      const name = object.name || 'Unnamed road';
      const vc   = typeof object.vc === 'number' ? object.vc : null;
      const load = vc === null ? '' : ` — ${Math.round(vc * 100)}% of capacity`;
      return { text: `${name}${load}` };
    },
  });
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

  renderPresets();
  const addBtn = document.getElementById('add-closure');
  if (addBtn) addBtn.addEventListener('click', () => addClosureRow());

  // ── Fetch edge geometries (once) and start polling
  fetchEdgeIndex();
  fetchLatest();
  setInterval(fetchLatest, POLL_INTERVAL_MS);
}

// Wait for deck.gl and MapLibre to load
window.addEventListener('load', init);
