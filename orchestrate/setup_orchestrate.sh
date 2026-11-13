#!/usr/bin/env bash
# Import the I-Grid tools and agents into watsonx Orchestrate, then deploy them.
#
#   ./orchestrate/setup_orchestrate.sh https://<public-api-url>
#
# Prerequisites (see orchestrate/README.md):
#   - the I-Grid API is reachable at that HTTPS URL
#   - the ADK is installed in .venv-wxo and an environment is active
#     (orchestrate env add ... --type ibm_iam --activate)
set -euo pipefail

cd "$(dirname "$0")/.."
URL="${1:?usage: setup_orchestrate.sh https://<public-api-url>}"

ORCH="orchestrate"
[ -x .venv-wxo/bin/orchestrate ] && ORCH=".venv-wxo/bin/orchestrate"

echo "== Checking the API is reachable at $URL"
curl -fsS -m 15 "$URL/health" >/dev/null || { echo "API not reachable at $URL/health"; exit 1; }

echo "== Building the OpenAPI tool spec"
.venv/bin/python orchestrate/build_openapi.py "$URL"

echo "== Importing tools: simulate_project, list_known_locations, estimate_utilities"
"$ORCH" tools import -k openapi -f orchestrate/igrid_tools.openapi.json

echo "== Importing agents (collaborators first, then the planner)"
"$ORCH" agents import -f orchestrate/traffic_agent.yaml
"$ORCH" agents import -f orchestrate/utilities_agent.yaml
"$ORCH" agents import -f orchestrate/planner_agent.yaml

echo "== Deploying agents (draft -> live)"
for a in igrid_traffic_analyst igrid_utilities_analyst igrid_planner; do
  "$ORCH" agents deploy --name "$a"
done

echo "Done. Open watsonx Orchestrate chat and pick the 'igrid_planner' agent."
