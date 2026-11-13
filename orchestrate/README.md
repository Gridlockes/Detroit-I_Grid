# watsonx Orchestrate integration

Detroit I-Grid uses **watsonx Orchestrate** as its conversational front door:
a planner agent takes a plain-English project ("close two lanes of I-375 for the
boulevard conversion"), delegates to specialist agents that call the I-Grid API,
and replies with an impact report — while the live map switches to the scenario.

```
User ──► igrid_planner  (collaborator agent, writes the report + mitigations)
            ├─► igrid_traffic_analyst ──► simulate_project      POST /agent/simulate
            │                         └─► list_known_locations  GET  /agent/locations
            └─► igrid_utilities_analyst ─► estimate_utilities   POST /utilities
```

All agents use `groq/openai/gpt-oss-120b`, the default tool-calling model on
IBM Cloud. `simulate_project` runs synchronously (~15 s, under Orchestrate's
40 s tool timeout), resolves landmarks / intersections such as
"Woodward Ave & Mack Ave", publishes the result to `/results/latest` so the map
updates, and returns a ~1 KB summary.

## Files

| File | Purpose |
|---|---|
| `planner_agent.yaml` | `igrid_planner` — routes requests, writes the report and mitigations |
| `traffic_agent.yaml` | `igrid_traffic_analyst` — turns text into a scenario, runs the simulation |
| `utilities_agent.yaml` | `igrid_utilities_analyst` — water / sewer / power estimates |
| `build_openapi.py` | Writes `igrid_tools.openapi.json` (OpenAPI 3.0.3, 3 tools) for a public base URL |
| `setup_orchestrate.sh` | Builds the spec, imports tools and agents, deploys them |

## Setup

1. **Install the ADK** in its own virtualenv (keeps its dependencies away from the backend):

   ```bash
   python3 -m venv .venv-wxo
   .venv-wxo/bin/pip install --upgrade ibm-watsonx-orchestrate
   ```

2. **Connect to the hackathon Orchestrate instance.** In Orchestrate: user icon →
   **Settings → API details** → copy the service instance URL, then
   **Generate API key**. (Don't use credentials from the IBM Cloud resource page.)

   ```bash
   .venv-wxo/bin/orchestrate env add -n hackathon -u <service-instance-url> --type ibm_iam --activate
   ```

   Paste the API key when prompted. Never commit it.

3. **Make the API reachable over HTTPS** (Orchestrate runs in IBM Cloud, so it
   can't call `localhost`): deploy to IBM Code Engine (see the main README), or
   for a quick demo run a tunnel to the local server.

4. **Import and deploy:**

   ```bash
   ./orchestrate/setup_orchestrate.sh https://<public-api-url>
   ```

5. In Orchestrate chat, select **igrid_planner** and try:
   - *"What happens to traffic if we close two lanes of I-375 each way for the boulevard conversion, with 120 trucks a day?"*
   - *"We're demolishing the Renaissance Center: about 2 million sq ft of office, 35 % occupied, 250 haul trucks a day, one lane closed on Jefferson and Beaubien."*
   - *"600 apartments and a 40,000 sq ft grocery at Woodward & Mack, with one Woodward lane closed."*

## Backing up (before Nov 24, 11:00 AM ET)

The hackathon tenant is deleted on Nov 24. Everything here is already in git,
but export the live versions too (flags per the ADK docs' "Managing agents" /
"Managing tools" pages — confirm with `orchestrate agents export --help`):

```bash
.venv-wxo/bin/orchestrate agents export -n igrid_planner -k native -o orchestrate/export/igrid_planner.zip
.venv-wxo/bin/orchestrate tools export -n simulate_project -o orchestrate/export/simulate_project.zip
```
