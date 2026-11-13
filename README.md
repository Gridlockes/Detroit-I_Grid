# Detroit I-Grid — Build & Deploy Guide

## Quick start (local dev)

```bash
# 1. Create and activate the venv (Python 3.12)
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Build the road-network cache (only needed once)
python build_network.py

# 4. Start the backend
uvicorn backend.main:app --reload --port 8080

# 5. Open the frontend
#    Serve frontend/ with any static file server, e.g.:
python3 -m http.server 3000 --directory frontend
#    Then open http://localhost:3000?api=http://localhost:8080
```

## Running tests

```bash
python test_sim.py --verbose
# Expected: 7/7 checks pass, elapsed < 15 s
```

---

## Environment variables

No secrets are committed to this repo.
Create a `.env` file (gitignored) or set env vars in your shell / Code Engine:

| Variable | Required | Description |
|---|---|---|
| `IGRID_GRAPHML_PATH` | No | Override path to the GraphML cache (default: `data/detroit.graphml`) |
| `IGRID_CORS_ORIGINS` | No | Comma-separated CORS origins (default: `*`) |

> **API keys for watsonx.ai / Orchestrate** are only needed in the agent
> runtime, not in this backend. Set them in the Orchestrate environment.

---

## Docker

```bash
# Build
docker build -t igrid-backend .

# Run (read-only mount of the graph file)
docker run -p 8080:8080 \
  -v $(pwd)/data:/app/data:ro \
  igrid-backend
```

---

## Deploy to IBM Code Engine

> Prerequisites: IBM Cloud CLI + Code Engine plugin installed and logged in.

### 1. Push the container image

```bash
# Log in to IBM Container Registry
ibmcloud cr login

REGISTRY=icr.io
NAMESPACE=<your-namespace>
IMAGE=$REGISTRY/$NAMESPACE/igrid-backend:latest

docker build -t $IMAGE .
docker push $IMAGE
```

### 2. Create the Code Engine application

```bash
ibmcloud ce application create \
  --name igrid-backend \
  --image $IMAGE \
  --port 8080 \
  --min-scale 0 \
  --max-scale 2 \
  --cpu 1 \
  --memory 4G
```

> `--min-scale 0` means the app scales to zero when idle — this preserves
> your $100 credits.  It will cold-start in ~10 s on first request.

### 3. Bind environment variables (no secrets in the image)

```bash
# If you need to override the CORS origins:
ibmcloud ce application update --name igrid-backend \
  --env IGRID_CORS_ORIGINS=https://<your-frontend-domain>
```

### 4. Get the public URL

```bash
ibmcloud ce application get --name igrid-backend --output json \
  | jq -r '.status.url'
# → https://igrid-backend.<region>.codeengine.appdomain.cloud
```

### 5. Update the frontend

Set `window.IGRID_API` in `frontend/index.html` or pass `?api=<URL>` in
the browser.

### 6. Scale to zero when done

```bash
ibmcloud ce application update --name igrid-backend --min-scale 0 --max-scale 0
```

---

## Import tools into watsonx Orchestrate

```bash
# Point Orchestrate at the live OpenAPI spec
orchestrate tools import --kind openapi \
  --url https://<CODE_ENGINE_URL>/openapi.json \
  --app-id igrid-backend
```

Then deploy the agent YAML files in `orchestrate/`:

```bash
cd orchestrate/
orchestrate agents apply -f planner_agent.yaml
orchestrate agents apply -f intake_agent.yaml
orchestrate agents apply -f traffic_agent.yaml
orchestrate agents apply -f utilities_agent.yaml
```

---

## Data sources

See `DATA_SOURCES.md` for full attribution.

| Data | Source | License |
|---|---|---|
| Road network | OpenStreetMap via osmnx + Geofabrik | ODbL — attribution required |
| Basemap tiles | Carto Dark Matter (MapLibre) | Carto terms |
| Trip-gen rates | ITE Trip Generation Manual (approximate) | Cited as planning estimates |
