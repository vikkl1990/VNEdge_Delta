# --- frontend build stage: compile the React v2 SPA to static assets ---------
# Isolated Node stage so the final image stays Python-only (no node_modules,
# no npm at runtime). Its output (frontend/dist) is COPYed into the app image
# below and served at /app by the dashboard.
FROM node:20-slim AS frontend
WORKDIR /ui
# Copy manifests first so `npm ci` is cached unless deps change.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- python app stage --------------------------------------------------------
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

COPY research ./research
# docs/ ships too: the dashboard's /runbooks route serves docs/RUNBOOKS.md at
# runtime (without this the route 404s in-container — caught 2026-07-11).
COPY docs ./docs

# The React v2 build. Served at /app == cwd/frontend/dist (WORKDIR is /app).
# Present only because the frontend stage built it; the create_app mount stays
# defensive (no dir → no /app route), so removing this COPY can never 500.
COPY --from=frontend /ui/dist ./frontend/dist

# Build provenance: deploy.sh passes the deployed git sha as a build-arg. Kept
# LAST so changing it never invalidates the pip / COPY layers above.
ARG VNEDGE_BUILD_SHA=dev
RUN echo "$VNEDGE_BUILD_SHA" > /app/BUILD_SHA

# Runtime state lives in mounted volumes: /app/logs, /app/data,
# /app/research/paper_trials (account resume + reports survive the container).
CMD ["python", "-m", "vnedge.runtime.paper_trial", \
     "research/paper_trials/funding_mr_btc_v1_20260703.yaml", \
     "--hours", "720", "--dashboard"]
