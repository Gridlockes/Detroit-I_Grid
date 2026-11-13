# watsonx Orchestrate ADK agents for Detroit I-Grid
#
# This directory contains the agent YAML definitions for the four
# watsonx Orchestrate agents described in PLAN.md § 2.
#
# Files
# ─────
#   planner_agent.yaml     — I-Grid Planner (collaborator; talks to the user)
#   intake_agent.yaml      — Intake agent (free text → structured JSON scenario)
#   traffic_agent.yaml     — Traffic agent (runs the simulation via tool)
#   utilities_agent.yaml   — Utilities agent (water / sewer / power estimates)
#
# Deployment
# ──────────
# 1. Install the Orchestrate ADK:
#      pip install ibm-watsonx-orchestrate
# 2. Log in:
#      orchestrate login --env <your-env-url>
# 3. Import all agents:
#      orchestrate agents apply -f planner_agent.yaml
#      orchestrate agents apply -f intake_agent.yaml
#      orchestrate agents apply -f traffic_agent.yaml
#      orchestrate agents apply -f utilities_agent.yaml
# 4. Import tools from the backend OpenAPI spec:
#      orchestrate tools import --kind openapi \
#        --url https://<CODE_ENGINE_URL>/openapi.json \
#        --app-id igrid-backend
#
# Backup reminder: export agent definitions before Nov 24, 11 AM ET:
#   orchestrate agents export -f planner_agent_backup.yaml --name "I-Grid Planner"
#   (repeat for each agent)
