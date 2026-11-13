# Detroit I-Grid — Build Plan

**Theme:** Next-Gen Mobility (smart infrastructure)
**Deadline:** Sat Nov 15, 2026, 2:00 PM ET · solo build
**Pitch:** Describe a future building project in plain English → AI agents simulate it → watch Detroit traffic change, live, on an animated map, before a shovel hits the ground.

---

## 1. What the demo shows

1. User types: *"600-unit apartment tower + grocery at Woodward & Mack, opening 2028, with 6 months of one-lane closure on Woodward during construction."*
2. watsonx Orchestrate agents parse it, run the simulation tools, and reply with an impact report.
3. The map animates **baseline vs. scenario** traffic in Midtown/Downtown Detroit: thousands of moving vehicle trails, roads colored green → red by congestion.
4. Report cards: added daily trips, worst intersections, travel-time change, water/sewer/power demand, and suggested mitigations.

## 2. Architecture

```
Browser (index.html)
  ├─ deck.gl TripsLayer (animated cars) + PathLayer (congestion colors) over a MapLibre basemap
  ├─ Scenario panel (baseline / scenario toggle, time slider)
  └─ watsonx Orchestrate embedded chat  ──┐
                                          │
watsonx Orchestrate                       │
  ├─ I-Grid Planner (collaborator agent, talks to the user)
  ├─ Intake agent      → turns the text into a structured scenario (JSON)
  ├─ Traffic agent     → tool: run_traffic_sim
  ├─ Utilities agent   → tool: estimate_utilities
  └─ (tools imported from the backend OpenAPI spec)
                                          │
Python FastAPI backend (IBM Code Engine) ◄┘
  ├─ POST /scenario          save structured scenario, return id
  ├─ POST /simulate/{id}     run traffic sim, store results
  ├─ GET  /results/{id}      trips + congestion + metrics for the map
  └─ POST /utilities         demand estimates
```

The page polls `/results/latest`, so the map updates as soon as the agent finishes the run. This is the "wow" moment in the video.

## 3. Simulation model (simple, defensible, runs fast)

- **Road network:** OpenStreetMap through `osmnx`, bounded to Downtown + Midtown + New Center (about 3 × 4 km). Cache it as GraphML so it isn't downloaded on every run.
- **Demand:**
  - Background trips come from synthetic origin-destination pairs weighted by Census tract population/jobs (ACS).
  - The new development adds trips using standard planning trip-generation rates, roughly:
    - apartments ≈ 5.4 trips/unit/day
    - grocery ≈ 100 trips per 1,000 sq ft per day
  - Cite the rates as approximate planning figures.
- **Assignment:** iterative shortest-path assignment with the BPR congestion function, `t = t0 · (1 + 0.15·(V/C)^4)`. Run 3–5 iterations.
- **Construction:** a lane closure means capacity × (lanes−1)/lanes on the affected edges.
- **Animation output:** sample about 2,000 vehicles for the peak hour and convert each path into `[lon, lat, timestamp]` waypoints for the TripsLayer.
- **Utilities:**
  - water ≈ 100 gal/person/day
  - sewer ≈ 90% of water
  - power ≈ 1.2 kW peak per unit
  - These are ballpark per-capita figures and are labeled as estimates in the UI.

## 4. Data sources (log every one in `DATA_SOURCES.md`)

| Data | Source | License |
|---|---|---|
| Road network | OpenStreetMap via osmnx | ODbL — attribution required |
| Population / jobs | US Census ACS + LEHD LODES | Public domain |
| Basemap tiles | OpenFreeMap / Carto | check & cite terms |
| (optional) traffic counts | MDOT open data | check terms before use |

No personal data, no social media, no client data.

## 5. Timeline

**Friday (today)**
- [ ] Request the team IBM Cloud account NOW (2 h to activate)
- [ ] Activate Bob trial, sign in, create the repo from the IBM hackathon template
- [ ] Bob: backend skeleton + osmnx network load + baseline simulation
- [ ] Bob: the map page with baseline animation working locally

**Saturday morning (stop building by about 11 AM)**
- [ ] Bob: scenario modifiers (new development + lane closure), comparison metrics
- [ ] Deploy the backend to Code Engine
- [ ] Orchestrate: import tools from the OpenAPI spec, create the agents, embed the chat
- [ ] Full end-to-end run-through

**Saturday 11 AM – 1:30 PM**
- [ ] Record the video (≤ 3 min), screenshot Bob budget → `bob_usage/`
- [ ] Write both statements, secrets check, make the repo public
- [ ] Submit by about 1:30 PM. Leave a buffer.

**Fallback if Orchestrate isn't ready in time:** the page still works with its own scenario form. Show Orchestrate calling the tools in its own chat window during the video.

## 6. Bob prompts (spend Bobcoins on these, in order)

Use **Plan/Architect mode** for #1, **Code mode** for the rest. Keep prompts specific. One big, clear prompt costs less than ten vague ones.

1. *Architect:* "Read PLAN.md. Propose the file structure for a FastAPI backend in `backend/` and a static frontend in `frontend/`. Create `requirements.txt` and empty module stubs."
2. "In `backend/network.py`, load the OSM drive network for a bounding box covering Downtown, Midtown and New Center Detroit with osmnx, add speed/travel-time and a capacity attribute (1800 veh/h/lane), and cache it to `data/detroit.graphml`."
3. "In `backend/sim.py`, implement the iterative BPR traffic assignment from PLAN.md section 3 for a set of OD trips. Return per-edge volume/capacity and a sample of vehicle trajectories as `[lon, lat, t]` lists for deck.gl TripsLayer."
4. "Add scenario support: a Scenario pydantic model (location lat/lon, housing_units, retail_sqft, lane_closures list). New trips use the trip-generation rates from PLAN.md; lane closures reduce capacity. Expose the FastAPI endpoints from section 2, with clean OpenAPI descriptions, because watsonx Orchestrate will import them as tools."
5. "Build `frontend/index.html`: MapLibre + deck.gl (pinned CDN versions), TripsLayer animating trajectories from `/results/latest`, PathLayer colored by V/C, a baseline/scenario toggle, and a metrics panel. Dark theme, looks great on screen recordings."
6. "Write a Dockerfile and the Code Engine deploy steps in README.md. Make sure no secrets are committed — read them from environment variables."
7. "Write the watsonx Orchestrate ADK agent YAML files in `orchestrate/` for the Planner, Intake, Traffic and Utilities agents described in PLAN.md section 2."

Screenshot interesting Bob moments as you go. They're good material for the video.

## 7. Rules checklist

- [ ] Bob IDE is used as a core component; `bob_usage/<name>_midevfest_IBM_Bob_usage.png` is in the repo
- [ ] Orchestrate is clearly shown in the video
- [ ] No API keys in the repo (keep `.gitignore`/`.bobignore` from the template; use `.env`)
- [ ] Don't use the llama-3-405b, mistral-medium-2505 or mistral-small-3-1-24b watsonx.ai models
- [ ] watsonx.ai region = Dallas
- [ ] Watch the $100 credits: scale the Code Engine app to 0 when idle
- [ ] Back up Orchestrate agents/tools before Nov 24, 11 AM ET
