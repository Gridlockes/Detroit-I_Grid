# syntax=docker/dockerfile:1
# Detroit I-Grid — FastAPI backend
#
# Build:
#   docker build -t igrid-backend .
#
# Run locally (with real graph):
#   docker run -p 8080:8080 \
#     -v $(pwd)/data:/app/data:ro \
#     igrid-backend
#
# No secrets are baked in. All credentials come from environment variables
# at runtime (see README.md § Environment variables).

FROM python:3.12-slim

# System deps for osmnx (needs libspatialindex for rtree)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libspatialindex-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies before copying source code
# so the layer is cached even when source changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the source
COPY backend/ ./backend/
COPY data/     ./data/

# Expose the port used by uvicorn
EXPOSE 8080

# Health-check so Code Engine knows the container is ready
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
