# Detroit I-Grid — Submission Text

Paste-ready text for the My Team submission form. Keep this file updated so a
resubmission (which needs every field again) is a copy-paste job.

- **Repository:** https://github.com/Gridlockes/Detroit-I_Grid
- **Video:** _(add the public / unlisted YouTube link)_

---

## Problem and solution statement (≤ 500 words)

**The problem.** Detroit is rebuilding itself — I-375 is being turned from a
freeway into a boulevard, the future of the Renaissance Center is under
discussion, and new campuses such as Michigan Central and the Hudson's site are
filling with workers and residents. Every one of these projects changes how
thousands of people get around, yet the public, small developers and even many
council discussions only see a traffic impact study months later, if at all.
Those studies are slow, expensive and unreadable to non-engineers, so problems
like an overloaded side street are usually discovered after the cones go up.

**Our solution.** Detroit I-Grid is an AI "what-if" simulator for Detroit
construction, demolition and development. A user describes a project in plain
English — *"Close two lanes of I-375 each way for the boulevard conversion with
120 trucks a day"* or *"Demolish the Renaissance Center"* — and within seconds
sees its impact on the real street network of Downtown, Midtown, New Center and
Corktown:

- An animated map of the PM peak hour on 7,815 real OpenStreetMap road
  segments. Roads are coloured by load versus capacity; simulated vehicles slow
  down and change colour (cyan → yellow → pink) where traffic crawls; work zones
  pulse in amber. Users flip between **Baseline** and **Scenario**.
- An impact report: change in total vehicle-hours of delay, trips added or
  removed, haul-truck traffic, and the named streets near the site whose load
  rises most (e.g. *Beaubien Street 47 % → 94 % of capacity*).
- Practical mitigation ideas tied to those streets.

**Who uses it and how.** City planners and council members screen proposals
before commissioning full studies; community groups ask "what will this do to my
street?"; developers test phasing and truck routes. They either chat with the
watsonx Orchestrate planner agent or use the map's scenario form and one-click
presets (Renaissance Center demolition, I-375 conversion, Hudson's site,
Michigan Central, District Detroit).

**How AI makes it new.** Instead of a static dashboard, a team of AI agents
turns loose human language into a structured engineering scenario — resolving
places like "Woodward & Mack" to the exact intersection, recognising that
"close a lane on Jefferson" is a local work zone, choosing trip-generation
inputs — runs a calibrated traffic assignment (BPR congestion model, iterative
assignment, demolition trips removed, trucks counted as passenger-car
equivalents), and explains the result in plain language with mitigations.
The combination of conversational agents, a physically grounded simulation and
a live animated twin of Detroit's streets is what makes it different: it
answers the question people actually ask — *what happens if we build, or tear
down, this?* — in seconds instead of months.

The results are honest planning-level estimates. The RenCen run, for example,
shows that demolition barely changes citywide delay (−0.5 %) because lost
office trips offset the trucks, while Beaubien Street doubles — exactly the
kind of local insight that should steer mitigation money.

---

## Technology statement

**IBM Bob (core development tool).** Detroit I-Grid was built in the IBM Bob IDE.
Working from our `PLAN.md`, Bob scaffolded the FastAPI backend and frontend,
wrote the road-network loader (osmnx), the traffic simulation engine
(`backend/sim.py`: iterative assignment with the BPR congestion function,
scenario demand, lane-closure capacity reductions, vehicle trajectories), the
REST API, the deck.gl/MapLibre map page with the scenario form, the Dockerfile
and the first draft of the agent definitions. We then iterated with Bob on
performance (286 s → ~15 s per run), calibration and bug fixes. Bob usage
screenshots are in `bob_usage/`.

**watsonx Orchestrate (agentic layer).** Three ADK agents (`orchestrate/`), all
on `groq/openai/gpt-oss-120b`:

- `igrid_planner` — collaborator agent that receives the user's request,
  delegates to the specialists and writes the impact report with mitigations.
- `igrid_traffic_analyst` — converts plain English into a structured scenario
  and calls the `simulate_project` tool; recovers from unknown locations with
  `list_known_locations`.
- `igrid_utilities_analyst` — calls `estimate_utilities` for water, sewer and
  power demand.

The tools are imported from an OpenAPI 3.0 spec generated from our API
(`orchestrate/build_openapi.py`). `simulate_project` (`POST /agent/simulate`)
resolves landmarks and street intersections on the network, runs the
simulation synchronously (~15–20 s, inside Orchestrate's 40 s tool limit),
publishes the result to the live map and returns a ~1 KB summary for the agent.

**Rest of the stack.** Python 3.12, FastAPI, networkx/osmnx; OpenStreetMap data
from a Geofabrik Michigan extract (ODbL); MapLibre GL JS with the CARTO Dark
Matter basemap; deck.gl Path/Trips/Icon/Scatterplot layers for congestion,
hologram vehicles and work zones. All data sources and planning assumptions are
listed in `DATA_SOURCES.md`; no personal, client or social-media data is used,
and no credentials are stored in the repository.
