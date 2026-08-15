#!/usr/bin/env bash
set -euo pipefail

# Read-only VNEdge_Delta dashboard. This process reads local research artifacts
# and constructs no broker, OrderManager, exchange account client, or order route.

export PYTHONPATH="${PYTHONPATH:-src}"
export DASHBOARD_HOST="${DASHBOARD_HOST:-127.0.0.1}"
export DASHBOARD_PORT="${DASHBOARD_PORT:-8080}"
export DASHBOARD_TOKEN="${DASHBOARD_TOKEN:-vnedge-demo}"
# The dashboard consumes continuity evidence; it never runs the CPU-heavy
# qualification loop in the web-serving process.
export DASHBOARD_REFRESH_EVENT_CONTINUITY="0"

python_bin="${VNEDGE_PYTHON:-.venv/bin/python}"
exec "$python_bin" -m vnedge.dashboard.scanner_live
